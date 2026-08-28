import asyncio
from datetime import timedelta

from django.contrib import admin, messages
from django.db import close_old_connections, transaction
from django.db.models import Exists, OuterRef, Q, Subquery
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin
from django.contrib.auth.models import User
from django.utils import timezone

from apps.core.models import Company, UserProfile, WaitlistEntry
from apps.core.pilot_test_phones import PILOT_TEST_PHONES

# How many days without a confirmed sale AND without an inbound message before
# an active, already-onboarded shop is flagged as needing attention (#274).
NEEDS_ATTENTION_DAYS = 7


class NeedsAttentionFilter(admin.SimpleListFilter):
    title = f"silent {NEEDS_ATTENTION_DAYS}+ days"
    parameter_name = "needs_attention"

    def lookups(self, request, model_admin):
        return (("yes", "Needs attention"),)

    def queryset(self, request, queryset):
        if self.value() != "yes":
            return queryset
        cutoff = timezone.now() - timedelta(days=NEEDS_ATTENTION_DAYS)
        return (
            queryset.filter(active=True, first_message_date__isnull=False, _is_test_shop=False)
            .filter(Q(_last_confirmed_sale__isnull=True) | Q(_last_confirmed_sale__lt=cutoff))
            .filter(Q(_last_inbound_message__isnull=True) | Q(_last_inbound_message__lt=cutoff))
        )


class UnnamedShopFilter(admin.SimpleListFilter):
    title = "needs a name"
    parameter_name = "needs_name"

    def lookups(self, request, model_admin):
        return (("yes", "Still \"Unnamed Shop\""),)

    def queryset(self, request, queryset):
        if self.value() != "yes":
            return queryset
        return queryset.filter(name="Unnamed Shop")


@admin.register(Company)
class CompanyAdmin(admin.ModelAdmin):
    list_display = [
        "name",
        "slug",
        "active",
        "member_count",
        "last_confirmed_sale",
        "last_inbound_message",
        "nudge_status",
        "needs_attention_flag",
        "needs_name_flag",
        "created_at",
    ]
    list_filter = ["active", NeedsAttentionFilter, UnnamedShopFilter]
    search_fields = ["name", "slug"]
    prepopulated_fields = {"slug": ("name",)}

    def get_queryset(self, request):
        from apps.sales.models import Sale
        from apps.whatsapp.models import WhatsAppMessage

        qs = super().get_queryset(request)
        last_sale_sq = (
            Sale.objects.filter(company=OuterRef("pk"), status=Sale.Status.CONFIRMED)
            .order_by("-sale_timestamp")
            .values("sale_timestamp")[:1]
        )
        last_inbound_sq = (
            WhatsAppMessage.objects.filter(
                company=OuterRef("pk"), direction=WhatsAppMessage.Direction.INBOUND
            )
            .order_by("-timestamp")
            .values("timestamp")[:1]
        )
        # "Any owner-role member matches a test phone" — a single Exists annotation shared
        # by both needs_attention_flag and NeedsAttentionFilter so the two can't disagree
        # (a company can have more than one OWNER-role profile; there's no DB constraint
        # limiting it to one, so picking ".first()" per call site is not reliable — #274 review).
        test_owner_sq = UserProfile.objects.filter(
            company=OuterRef("pk"),
            role=UserProfile.Role.OWNER,
            phone_number__in=PILOT_TEST_PHONES,
        )
        return qs.annotate(
            _last_confirmed_sale=Subquery(last_sale_sq),
            _last_inbound_message=Subquery(last_inbound_sq),
            _is_test_shop=Exists(test_owner_sq),
        )

    def member_count(self, obj):
        return obj.members.count()

    member_count.short_description = "Members"

    def last_confirmed_sale(self, obj):
        return obj._last_confirmed_sale or "never"

    last_confirmed_sale.short_description = "Last sale"
    last_confirmed_sale.admin_order_field = "_last_confirmed_sale"

    def last_inbound_message(self, obj):
        return obj._last_inbound_message or "never"

    last_inbound_message.short_description = "Last inbound msg"
    last_inbound_message.admin_order_field = "_last_inbound_message"

    def nudge_status(self, obj):
        """Nudge state is only ever advanced on confirmed WhatsApp send success
        (see nudge_service.py) — so a stalled date/stage here means either the
        shop isn't eligible yet, or the last attempted send failed."""
        parts = [f"stage {obj.nudge_stage}"]
        parts.append(f"last nudge {obj.last_nudge_date}" if obj.last_nudge_date else "never nudged")
        if obj.onboarding_followup_sent:
            parts.append("onboarding followup sent")
        if obj.zero_contact_followup_count:
            parts.append(f"{obj.zero_contact_followup_count} zero-contact followups")
        return " · ".join(parts)

    nudge_status.short_description = "Nudge status"

    def needs_attention_flag(self, obj):
        if not obj.active or not obj.first_message_date or obj._is_test_shop:
            return False
        cutoff = timezone.now() - timedelta(days=NEEDS_ATTENTION_DAYS)
        sale_stale = obj._last_confirmed_sale is None or obj._last_confirmed_sale < cutoff
        inbound_stale = obj._last_inbound_message is None or obj._last_inbound_message < cutoff
        return sale_stale and inbound_stale

    needs_attention_flag.boolean = True
    needs_attention_flag.short_description = f"Silent {NEEDS_ATTENTION_DAYS}+ days"

    def needs_name_flag(self, obj):
        """Shops approved without a name are permanently stuck on this literal
        placeholder (apps.core.services.approve_waitlist_entry) unless someone
        edits it here or the shop replies to the automated followup (#316)."""
        return obj.name == "Unnamed Shop"

    needs_name_flag.boolean = True
    needs_name_flag.short_description = "Needs a name"

    def has_change_permission(self, request, obj=None):
        return request.user.is_superuser

    def has_delete_permission(self, request, obj=None):
        return request.user.is_superuser


class UserProfileInline(admin.StackedInline):
    model = UserProfile
    can_delete = False
    verbose_name_plural = "Profile"


class UserAdmin(BaseUserAdmin):
    inlines = [UserProfileInline]
    list_display = [
        "username",
        "email",
        "first_name",
        "last_name",
        "get_company",
        "get_role",
        "is_staff",
    ]
    list_select_related = ["profile", "profile__company"]

    def get_company(self, obj):
        if hasattr(obj, "profile"):
            return obj.profile.company.name
        return "-"

    get_company.short_description = "Company"

    def get_role(self, obj):
        if hasattr(obj, "profile"):
            return obj.profile.get_role_display()
        return "-"

    get_role.short_description = "Role"

    def has_add_permission(self, request):
        return request.user.is_superuser

    def has_change_permission(self, request, obj=None):
        return request.user.is_superuser

    def has_delete_permission(self, request, obj=None):
        return request.user.is_superuser


# Re-register UserAdmin
admin.site.unregister(User)
admin.site.register(User, UserAdmin)


@admin.register(UserProfile)
class UserProfileAdmin(admin.ModelAdmin):
    list_display = ["user", "company", "role", "phone_number"]
    list_filter = ["company", "role"]
    search_fields = ["user__username", "user__email", "phone_number"]
    autocomplete_fields = ["user", "company"]

    def has_add_permission(self, request):
        return request.user.is_superuser

    def has_change_permission(self, request, obj=None):
        return request.user.is_superuser

    def has_delete_permission(self, request, obj=None):
        return request.user.is_superuser


@admin.register(WaitlistEntry)
class WaitlistEntryAdmin(admin.ModelAdmin):
    list_display = ["phone_number", "company_name", "status", "created_at", "approved_at"]
    list_filter = ["status", "created_at"]
    search_fields = ["phone_number", "company_name", "notes"]
    readonly_fields = ["created_at", "approved_at", "approved_by", "company", "user_profile", "status"]
    actions = ["approve_entries", "reject_entries"]
    change_form_template = "admin/core/waitlistentry/change_form.html"

    def get_readonly_fields(self, request, obj=None):
        readonly = list(self.readonly_fields)
        if not request.user.is_superuser:
            readonly += ["phone_number", "company_name", "first_message"]
        return readonly

    def change_view(self, request, object_id, form_url="", extra_context=None):
        extra_context = extra_context or {}
        obj = self.get_object(request, object_id)
        extra_context["show_approve_reject"] = (
            obj is not None and obj.status == WaitlistEntry.Status.PENDING
        )
        return super().change_view(request, object_id, form_url, extra_context)

    def response_change(self, request, obj):
        if "_approve" in request.POST and obj.status == WaitlistEntry.Status.PENDING:
            obj.status = WaitlistEntry.Status.APPROVED
            self._approve_entry(request, obj)
            return self.response_post_save_change(request, obj)
        if "_reject" in request.POST and obj.status == WaitlistEntry.Status.PENDING:
            obj.status = WaitlistEntry.Status.REJECTED
            obj.save()
            self.message_user(request, f"Rejected {obj.phone_number}.", messages.SUCCESS)
            return self.response_post_save_change(request, obj)
        return super().response_change(request, obj)

    def has_add_permission(self, request):
        if not request.user.is_superuser:
            return False
        return super().has_add_permission(request)

    def has_delete_permission(self, request, obj=None):
        if not request.user.is_superuser:
            return False
        return super().has_delete_permission(request, obj)

    fieldsets = (
        (None, {
            "fields": ("phone_number", "company_name", "status")
        }),
        ("Message", {
            "fields": ("first_message",),
            "classes": ("collapse",),
        }),
        ("Approval Details", {
            "fields": ("created_at", "approved_at", "approved_by", "company", "user_profile"),
        }),
        ("Notes", {
            "fields": ("notes",),
        }),
    )

    def _approve_entry(self, request, entry):
        """Run the full approval logic for a single entry."""
        from apps.core.services import approve_waitlist_entry, PhoneNumberAlreadyRegisteredError
        from apps.whatsapp.services.whatsapp_client import get_whatsapp_client

        try:
            company, profile = approve_waitlist_entry(entry, approved_by=request.user)
        except PhoneNumberAlreadyRegisteredError:
            entry.status = WaitlistEntry.Status.PENDING  # revert status change
            self.message_user(
                request,
                f"Cannot approve {entry.phone_number}: a user with this phone number already exists.",
                messages.ERROR,
            )
            return

        # Send approval notification via WhatsApp after the transaction commits
        phone = entry.phone_number
        lang = entry.language
        company_name = company.name

        def send_welcome():
            from apps.whatsapp.services.webhook_handler import t
            client = get_whatsapp_client()
            close_old_connections()
            asyncio.run(client.send_message(
                phone,
                t("approval.welcome", lang=lang, company=company_name),
            ))

        transaction.on_commit(send_welcome)
        self.message_user(request, f"Approved {entry.phone_number}.", messages.SUCCESS)

    @admin.action(description="Approve selected waitlist entries")
    def approve_entries(self, request, queryset):
        approved_count = 0
        for entry in queryset.filter(status=WaitlistEntry.Status.PENDING):
            entry.status = WaitlistEntry.Status.APPROVED
            self._approve_entry(request, entry)
            approved_count += 1

        if approved_count > 0:
            self.message_user(
                request,
                f"Successfully approved {approved_count} waitlist entries.",
                messages.SUCCESS
            )
        else:
            self.message_user(
                request,
                "No pending entries to approve.",
                messages.WARNING
            )

    @admin.action(description="Reject selected waitlist entries")
    def reject_entries(self, request, queryset):
        updated = queryset.filter(status=WaitlistEntry.Status.PENDING).update(
            status=WaitlistEntry.Status.REJECTED
        )
        self.message_user(
            request,
            f"Rejected {updated} waitlist entries.",
            messages.SUCCESS
        )
