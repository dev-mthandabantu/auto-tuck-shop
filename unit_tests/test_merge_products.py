from django.core.management import CommandError, call_command
from django.test import TestCase
from apps.catalog.models import Category, Product, ProductPrice
from apps.core.models import Company
from apps.inventory.models import InventoryAdjustment
from apps.sales.models import Sale, SaleItem
from decimal import Decimal


class MergeProductsCommandTests(TestCase):
    def setUp(self):
        self.company = Company.objects.create(name="Mai Ati Mini", slug="mai-ati-mini")

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

    def test_commit_does_not_crash_on_reverse_relations(self):
        """Regression test: the relation-discovery loop previously matched reverse
        descriptors (e.g. Company.products, via Product.company's related_name)
        as well as genuine forward FKs, because both have remote_field.model == Product.
        Django rejects bulk .update() on a non-concrete reverse field with a FieldError,
        so every real --commit invocation crashed before reassigning anything.
        This company has a category with related_name="category_products" — another
        reverse relation to Product — to make sure that one doesn't trip it either."""
        category = Category.objects.create(name="Meals", company=self.company)
        self.source.category = category
        self.source.save()

        call_command("merge_products", "--source", str(self.source.pk), "--target", str(self.target.pk), "--commit")

        self.assertFalse(Product.objects.filter(pk=self.source.pk).exists())
        self.assertEqual(SaleItem.objects.get(sale=self.sale).product.pk, self.target.pk)
        self.assertEqual(ProductPrice.objects.get(pk__isnull=False, product=self.target).price, Decimal("1.00"))

    def test_cross_company_merge_rejected_without_force(self):
        other_company = Company.objects.create(name="Other Shop", slug="other-shop")
        other_product = Product.objects.create(company=other_company, name="chips")

        with self.assertRaises(CommandError):
            call_command("merge_products", "--source", str(self.source.pk), "--target", str(other_product.pk))

        # nothing changed
        self.assertTrue(Product.objects.filter(pk=self.source.pk).exists())
        self.assertEqual(SaleItem.objects.get(sale=self.sale).product.pk, self.source.pk)

    def test_cross_company_merge_allowed_with_force(self):
        other_company = Company.objects.create(name="Other Shop", slug="other-shop")
        other_product = Product.objects.create(company=other_company, name="chips")

        call_command(
            "merge_products",
            "--source", str(self.source.pk),
            "--target", str(other_product.pk),
            "--commit",
            "--force",
        )

        self.assertFalse(Product.objects.filter(pk=self.source.pk).exists())
        self.assertEqual(SaleItem.objects.get(sale=self.sale).product.pk, other_product.pk)

    def test_same_product_rejected(self):
        with self.assertRaises(CommandError):
            call_command("merge_products", "--source", str(self.source.pk), "--target", str(self.source.pk))

    def test_nonexistent_source_rejected(self):
        with self.assertRaises(CommandError):
            call_command("merge_products", "--source", "999999", "--target", str(self.target.pk))

    def test_nonexistent_target_rejected(self):
        with self.assertRaises(CommandError):
            call_command("merge_products", "--source", str(self.source.pk), "--target", "999999")
