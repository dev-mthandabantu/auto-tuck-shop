from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0014_restore_company_closing_fields"),
    ]

    operations = [
        migrations.RunSQL(
            sql="""
                ALTER TABLE core_company
                DROP COLUMN IF EXISTS nudge_stage;
            """,
            reverse_sql=migrations.RunSQL.noop,
        ),
    ]
