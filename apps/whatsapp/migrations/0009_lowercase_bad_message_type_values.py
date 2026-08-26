# Data migration for #334: whatsapp_client.py's outbound send functions
# previously wrote uppercase string literals ("TEXT", "INTERACTIVE_BUTTON",
# "IMAGE") instead of the lowercase MessageType enum values. CharField
# doesn't enforce choices at the DB level, so these were written silently —
# no error, but any filter by the enum constant (the normal way to query
# this field) never matched these rows. Fixed at the write site in the
# same PR; this migration corrects rows already written before that fix.

from django.db import migrations

# Exact bad values the old code could have written — not a blanket
# lowercase of the whole column, to avoid touching anything unexpected.
_BAD_VALUE_MAP = {
    "TEXT": "text",
    "INTERACTIVE_BUTTON": "interactive_button",
    "IMAGE": "image",
}


def lowercase_bad_values(apps, schema_editor):
    WhatsAppMessage = apps.get_model("whatsapp", "WhatsAppMessage")
    for bad_value, good_value in _BAD_VALUE_MAP.items():
        WhatsAppMessage.objects.filter(message_type=bad_value).update(message_type=good_value)


class Migration(migrations.Migration):

    dependencies = [
        ("whatsapp", "0008_alter_whatsappmessage_message_type"),
    ]

    operations = [
        migrations.RunPython(lowercase_bad_values, migrations.RunPython.noop),
    ]
