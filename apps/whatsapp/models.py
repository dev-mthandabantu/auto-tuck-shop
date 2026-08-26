from django.db import models


class WhatsAppMessage(models.Model):
    """Record of all WhatsApp messages (inbound and outbound)."""

    class Direction(models.TextChoices):
        INBOUND = "inbound", "Inbound"
        OUTBOUND = "outbound", "Outbound"

    class MessageType(models.TextChoices):
        TEXT = "text", "Text"
        TEMPLATE = "template", "Template"
        IMAGE = "image", "Image"
        INTERACTIVE_BUTTON = "interactive_button", "Interactive Button"
        BUTTON_RESPONSE = "button_response", "Button Response"
        AUDIO = "audio", "Audio"
        UNKNOWN = "unknown", "Unknown"

    # Message metadata
    direction = models.CharField(max_length=20, choices=Direction.choices)
    message_type = models.CharField(max_length=30, choices=MessageType.choices)
    phone_number = models.CharField(max_length=25, db_index=True, help_text="Normalized phone number with + prefix")
    timestamp = models.DateTimeField(auto_now_add=True, db_index=True)

    # Content
    content = models.TextField(blank=True, help_text="Message text content")
    button_id = models.CharField(max_length=100, blank=True, help_text="Button ID for button responses (e.g., mistake_123, cancel_123)")
    whatsapp_message_id = models.CharField(max_length=100, blank=True, null=True, help_text="Meta API message ID")
    reply_to_message_id = models.CharField(max_length=100, blank=True, help_text="ID of message being replied to")

    # Media fields
    media_id = models.CharField(max_length=200, blank=True, help_text="Meta media ID for audio/image/video")
    media_url = models.URLField(blank=True, help_text="CDN URL for media file (5-minute expiry)")
    r2_media_url = models.URLField(blank=True, help_text="Permanent R2 storage URL for media file")
    transcribed_text = models.TextField(blank=True, help_text="Transcribed text from audio messages")

    # Debug/audit data
    raw_payload = models.JSONField(null=True, blank=True, help_text="Full webhook/API response for debugging")
    api_success = models.BooleanField(null=True, blank=True, help_text="Whether outbound message sent successfully")
    api_error = models.TextField(blank=True, help_text="Error message if send failed")

    # Relationships
    user_profile = models.ForeignKey(
        "core.UserProfile",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="whatsapp_messages",
    )
    company = models.ForeignKey(
        "core.Company",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="whatsapp_messages",
    )
    sale = models.ForeignKey(
        "sales.Sale",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="whatsapp_messages",
    )
    waitlist_entry = models.ForeignKey(
        "core.WaitlistEntry",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="whatsapp_messages",
    )

    class Meta:
        ordering = ["-timestamp"]
        indexes = [
            models.Index(fields=["phone_number", "-timestamp"]),
            models.Index(fields=["direction", "-timestamp"]),
            models.Index(fields=["user_profile", "-timestamp"]),
            models.Index(fields=["company", "-timestamp"]),
            models.Index(fields=["sale"]),
            models.Index(fields=["waitlist_entry"]),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["whatsapp_message_id"],
                condition=models.Q(whatsapp_message_id__isnull=False) & ~models.Q(whatsapp_message_id=""),
                name="unique_whatsapp_message_id_when_set",
            ),
        ]

    def __str__(self):
        direction_arrow = "←" if self.direction == self.Direction.INBOUND else "→"
        content_preview = self.content[:50] if self.content else f"[{self.message_type}]"
        return f"{direction_arrow} {self.phone_number}: {content_preview}"


class LlmParseLog(models.Model):
    """Stores the LLM parse result for each inbound message."""

    message = models.OneToOneField(
        "WhatsAppMessage",
        on_delete=models.CASCADE,
        related_name="llm_parse_log",
    )
    intent = models.CharField(max_length=30, blank=True)
    confidence = models.FloatField(null=True, blank=True)
    raw_response = models.JSONField(null=True, blank=True)
    prompt_tokens = models.IntegerField(null=True, blank=True)
    completion_tokens = models.IntegerField(null=True, blank=True)
    parse_error = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        indexes = [
            models.Index(fields=["intent", "-created_at"]),
        ]

    def __str__(self):
        if self.parse_error:
            return f"LlmParseLog [{self.message_id}] ERROR"
        return f"LlmParseLog [{self.message_id}] {self.intent} ({self.confidence:.2f})"
