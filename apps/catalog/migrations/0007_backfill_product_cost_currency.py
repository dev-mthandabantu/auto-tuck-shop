from django.db import migrations


def default_cost_currency(apps, schema_editor):
    """A cost entered by hand (e.g. in the admin) before cost_currency existed has
    no currency, which would leave every sale of the product out of profit. Give it
    the shop's own currency, the same default Product.save() now applies."""
    Product = apps.get_model("catalog", "Product")
    for product in Product.objects.filter(cost__isnull=False, cost_currency__isnull=True).select_related("company"):
        product.cost_currency = product.company.currency
        product.save(update_fields=["cost_currency"])


class Migration(migrations.Migration):

    dependencies = [
        ("catalog", "0006_product_cost_ask_count"),
        ("core", "0025_company_pending_cost_asked_at_and_more"),
    ]

    operations = [
        migrations.RunPython(default_cost_currency, migrations.RunPython.noop),
    ]
