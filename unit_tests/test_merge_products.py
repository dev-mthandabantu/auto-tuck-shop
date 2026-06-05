from django.test import TestCase
from django.core.management import call_command
from apps.catalog.models import Product, ProductPrice
from apps.sales.models import Sale, SaleItem
from apps.inventory.models import InventoryAdjustment
from decimal import Decimal


class MergeProductsCommandTests(TestCase):
    def setUp(self):
        # create a company
        from apps.core.models import Company

        self.company = Company.objects.create(name="Mai Ati Mini")

        # canonical product
        self.target = Product.objects.create(company=self.company, name="plate chicken")
        # duplicate product
        self.source = Product.objects.create(company=self.company, name="sadza and chicken")

        # sale with source product
        self.sale = Sale.objects.create(company=self.company)
        SaleItem.objects.create(sale=self.sale, product=self.source, quantity=2, unit_price=Decimal("1.00"), currency="USD")

        # inventory adjustment for source
        InventoryAdjustment.objects.create(product=self.source, quantity_delta=10, reason=InventoryAdjustment.Reason.INITIAL)

        # price history for source
        ProductPrice.objects.create(product=self.source, price=Decimal("1.00"), currency="USD")

    def test_dry_run_does_not_change(self):
        call_command("merge_products", "--source", str(self.source.pk), "--target", str(self.target.pk))
        # source still exists and saleitem still points to source
        self.assertTrue(Product.objects.filter(pk=self.source.pk).exists())
        self.assertEqual(SaleItem.objects.get(sale=self.sale).product.pk, self.source.pk)

    def test_commit_merges_and_deletes_source(self):
        call_command("merge_products", "--source", str(self.source.pk), "--target", str(self.target.pk), "--commit")
        # source deleted
        self.assertFalse(Product.objects.filter(pk=self.source.pk).exists())
        # saleitem now points to target
        self.assertEqual(SaleItem.objects.get(sale=self.sale).product.pk, self.target.pk)
