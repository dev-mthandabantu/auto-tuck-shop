"""Shared service functions for core business logic.

These are plain synchronous functions that can be called from both
the Django admin (sync) and the webhook handler (via sync_to_async).
"""

from django.contrib.auth.models import User
from django.db import IntegrityError
from django.utils import timezone
from django.utils.text import slugify

from apps.core.models import Company, UserProfile, WaitlistEntry


class PhoneNumberAlreadyRegisteredError(ValueError):
    """Raised when trying to approve a waitlist entry for a phone number that already has a profile."""
    pass


def approve_waitlist_entry(
    entry: WaitlistEntry,
    approved_by: User | None = None,
) -> tuple[Company, UserProfile]:
    """Run the full approval logic for a waitlist entry.

    Creates a Company, User, and UserProfile, then updates the entry.
    Can be called from the Django admin or the WhatsApp webhook handler.

    Args:
        entry: The waitlist entry to approve (must be pending).
        approved_by: The admin user who approved (optional, for admin audit trail).

    Returns:
        Tuple of (company, user_profile).
    """
    # Guard: reject only if an ACTIVE profile with this phone number already
    # exists. A deactivated profile (e.g. a removed assistant) still occupies
    # the number — reactivate and re-home it below rather than block signup.
    existing_profile = UserProfile.objects.select_related("user").filter(
        phone_number=entry.phone_number
    ).first()
    if existing_profile and existing_profile.user.is_active:
        raise PhoneNumberAlreadyRegisteredError(
            f"A user profile with phone number {entry.phone_number} already exists."
        )

    # Create company name from entry or generate fallback
    company_name = entry.company_name.strip() if entry.company_name else "Unnamed Shop"

    # Generate unique slug
    base_slug = slugify(company_name)
    slug = base_slug or "shop"
    counter = 1
    while Company.objects.filter(slug=slug).exists():
        slug = f"{base_slug or 'shop'}-{counter}"
        counter += 1

    # Create company
    company = Company.objects.create(name=company_name, slug=slug)

    if existing_profile:
        # Reactivate the existing (deactivated) row as the new owner, rather
        # than inserting a new row — phone_number is unique.
        existing_profile.user.is_active = True
        existing_profile.user.save(update_fields=["is_active"])
        existing_profile.company = company
        existing_profile.role = UserProfile.Role.OWNER
        existing_profile.language = entry.language
        existing_profile.save(update_fields=["company", "role", "language"])
        profile = existing_profile
    else:
        # Create user (username from phone, removing non-alphanumeric)
        username = "".join(c for c in entry.phone_number if c.isalnum())

        # Ensure unique username
        base_username = username
        counter = 1
        while User.objects.filter(username=username).exists():
            username = f"{base_username}_{counter}"
            counter += 1

        user = User.objects.create_user(username=username)

        # Create user profile as owner
        profile = UserProfile.objects.create(
            user=user,
            company=company,
            role=UserProfile.Role.OWNER,
            phone_number=entry.phone_number,
            language=entry.language,
        )

    # Update waitlist entry
    entry.status = WaitlistEntry.Status.APPROVED
    entry.approved_at = timezone.now()
    if approved_by:
        entry.approved_by = approved_by
    entry.company = company
    entry.user_profile = profile
    entry.save(update_fields=["status", "approved_at", "approved_by", "company", "user_profile"])

    return company, profile
