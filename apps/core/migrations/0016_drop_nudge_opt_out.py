from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0015_drop_nudge_stage"),
    ]

    operations = [
        migrations.RunSQL(
            sql="""
                ALTER TABLE core_userprofile
                DROP COLUMN IF EXISTS nudge_opt_out;
            """,
            reverse_sql=migrations.RunSQL.noop,
        ),
    ]
