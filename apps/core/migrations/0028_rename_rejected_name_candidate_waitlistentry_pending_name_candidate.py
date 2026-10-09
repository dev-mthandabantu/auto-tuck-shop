from django.db import migrations, models


class Migration(migrations.Migration):
    """#423 replaces #419's send-again rule with a Yes/No confirmation. The field now holds
    the text the bot asked about, not the text it turned away."""

    dependencies = [
        ("core", "0027_waitlistentry_rejected_name_candidate"),
    ]

    operations = [
        migrations.RenameField(
            model_name="waitlistentry",
            old_name="rejected_name_candidate",
            new_name="pending_name_candidate",
        ),
        migrations.AlterField(
            model_name="waitlistentry",
            name="pending_name_candidate",
            field=models.CharField(
                blank=True,
                help_text="Text the bot asked 'Is this your shop's name?' about; saved as the name only "
                          "if the owner taps Yes on that question (#423)",
                max_length=200,
            ),
        ),
    ]
