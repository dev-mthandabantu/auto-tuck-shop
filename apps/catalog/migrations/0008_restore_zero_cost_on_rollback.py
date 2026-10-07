from decimal import Decimal

from django.db import migrations


def nothing(apps, schema_editor):
    """Forward: no change."""


def unknown_cost_to_zero(apps, schema_editor):
    """Rolling back to before 0005 turns Product.cost back into NOT NULL with a 0.00
    default, which fails if any row is NULL (unknown). This only runs on rollback, and
    runs before 0005's own reverse (migrations are undone newest first), so those rows
    get the old 0.00 default instead of an IntegrityError."""
    Product = apps.get_model("catalog", "Product")
    Product.objects.filter(cost__isnull=True).update(cost=Decimal("0.00"))


class Migration(migrations.Migration):

    dependencies = [
        ("catalog", "0007_backfill_product_cost_currency"),
    ]

    operations = [
        migrations.RunPython(nothing, unknown_cost_to_zero),
    ]
