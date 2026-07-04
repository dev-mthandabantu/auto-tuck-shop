from django.contrib.auth.models import User
from django.db import models

from apps.core.currencies import CURRENCY_CHOICES, DEFAULT_CURRENCY, format_price


class Company(models.Model):
    """A company (shop) in the system."""

    name = models.CharField(max_length=200)
    slug = models.SlugField(unique=True)
    created_at = models.DateTimeField(auto_now_add=True)
    active = models.BooleanField(default=True)
    daily_summary_enabled = models.BooleanField(default=True)
    normal_closing_time = models.TimeField(null=True, blank=True)
    daily_closing_time = models.TimeField(null=True, blank=True)
    daily_closing_date = models.DateField(null=True, blank=True)
    last_closing_prompt_date = models.DateField(null=True, blank=True)
    last_summary_date = models.DateField(null=True, blank=True)
    currency = models.CharField(
        max_length=3,
        choices=CURRENCY_CHOICES,
        default=DEFAULT_CURRENCY,
        help_text="Default currency for this company's prices",
    )

    # Currency clarification
    pending_clarification_sale = models.OneToOneField(
        "sales.Sale",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
        help_text="Sale awaiting currency clarification from the owner",
    )

    # Nudge system
    first_message_date = models.DateField(null=True, blank=True, help_text="Date of first inbound message — nudge eligibility starts here")
    last_nudge_date = models.DateField(null=True, blank=True, help_text="Last date a nudge was sent — prevents double-sending")
    nudge_stage = models.PositiveSmallIntegerField(default=0, help_text="Nudge sequence stage — increments after each send")
    onboarding_followup_sent = models.BooleanField(default=False, help_text="ats_onboarding_followup template has been sent — don't send again")
    last_reengagement_date = models.DateField(null=True, blank=True, help_text="Last date ats_reengagement template was sent — throttles re-sends to once per 7 days")
    zero_contact_followup_count = models.PositiveSmallIntegerField(default=0, help_text="Number of ats_zero_contact_followup sends for a shop that has never sent a single message — stops at 3 (day 2/5/7 after approval)")
    last_zero_contact_followup_date = models.DateField(null=True, blank=True, help_text="Last date ats_zero_contact_followup was sent — prevents double-sending same day")

    def format_price(self, amount):
        """Format a price using this company's currency."""
        return format_price(amount, self.currency)

    class Meta:
        verbose_name_plural = "companies"
        ordering = ["name"]

    def __str__(self):
        return self.name


class UserProfile(models.Model):
    """Extended user profile with company membership and role."""

    class Role(models.TextChoices):
        OWNER = "owner", "Owner"
        ASSISTANT = "assistant", "Assistant"

    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name="profile")
    company = models.ForeignKey(
        Company,
        on_delete=models.CASCADE,
        related_name="members",
    )
    role = models.CharField(max_length=20, choices=Role.choices, default=Role.ASSISTANT)
    phone_number = models.CharField(max_length=25, unique=True)
    language = models.CharField(
        max_length=10,
        default="sn",
        help_text="User's preferred language (en, sn)",
    )
    nudge_opt_out = models.BooleanField(default=False, help_text="User replied 'stop' — skip nudges for this user")

    class Meta:
        ordering = ["user__username"]

    def __str__(self):
        return f"{self.user.username} ({self.company.name} - {self.get_role_display()})"


class WaitlistEntry(models.Model):
    """Track users waiting for approval to use the system."""

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        APPROVED = "approved", "Approved"
        REJECTED = "rejected", "Rejected"

    phone_number = models.CharField(max_length=25, unique=True)
    first_message = models.TextField(blank=True)
    company_name = models.CharField(max_length=200, blank=True)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PENDING)
    created_at = models.DateTimeField(auto_now_add=True)
    approved_at = models.DateTimeField(null=True, blank=True)
    approved_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True)
    company = models.ForeignKey(Company, on_delete=models.SET_NULL, null=True, blank=True)
    user_profile = models.ForeignKey(UserProfile, on_delete=models.CASCADE, null=True, blank=True)
    notes = models.TextField(blank=True)
    confirmation_message_sid = models.CharField(max_length=100, blank=True, null=True)
    language = models.CharField(
        max_length=10,
        default="sn",
        help_text="User's preferred language (en, sn)",
    )

    class Meta:
        verbose_name_plural = "waitlist entries"
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.phone_number} ({self.get_status_display()})"
