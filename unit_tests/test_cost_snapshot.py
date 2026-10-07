"""Cost snapshot on sale lines (#409): the product's cost is frozen onto each
line when the sale is confirmed, so a later cost change never rewrites history."""

from decimal import Decimal

from django.test import TestCase, TransactionTestCase

from apps.catalog.models import Product
from apps.core.models import Company
from apps.sales.models import Sale, SaleItem
from apps.sales.services import snapshot_item_costs
from apps.whatsapp.services.sale_handler import _get_confirmed_sale
from unit_tests.async_helpers import run_async_test


def _company():
    return Company.objects.create(name="Cost Shop", slug="cost-shop", active=True)


def _sale_with(company, product, sid="sid_c", currency="USD"):
    sale = Sale.objects.create(company=company, confirmation_message_sid=sid)
    item = SaleItem.objects.create(
        sale=sale, product=product, quantity=2, unit_price=Decimal("1.00"), currency=currency
    )
    return sale, item


class ProductCostDefaultTest(TestCase):
    def test_new_product_cost_is_unknown_not_zero(self):
        product = Product.objects.create(name="Bread", company=_company())
        self.assertIsNone(product.cost)
        self.assertIsNone(product.cost_currency)


class ProductCostCurrencyDefaultTest(TestCase):
    def test_cost_set_without_a_currency_uses_the_shops_currency(self):
        company = Company.objects.create(name="ZAR Shop", slug="zar-shop", active=True, currency="ZAR")
        product = Product.objects.create(name="Bread", company=company, cost=Decimal("8.00"))
        self.assertEqual(product.cost_currency, "ZAR")

    def test_an_explicit_currency_is_kept(self):
        company = Company.objects.create(name="ZAR Shop", slug="zar-shop", active=True, currency="ZAR")
        product = Product.objects.create(name="Bread", company=company, cost=Decimal("1.00"), cost_currency="USD")
        self.assertEqual(product.cost_currency, "USD")

    def test_unknown_cost_gets_no_currency(self):
        product = Product.objects.create(name="Eggs", company=_company())
        self.assertIsNone(product.cost_currency)


class SnapshotItemCostsTest(TestCase):
    def setUp(self):
        self.company = _company()

    def test_known_cost_is_copied_with_currency(self):
        product = Product.objects.create(
            name="Bread", company=self.company, cost=Decimal("0.80"), cost_currency="USD"
        )
        sale, item = _sale_with(self.company, product)

        self.assertEqual(snapshot_item_costs(sale), 1)

        item.refresh_from_db()
        self.assertEqual(item.unit_cost, Decimal("0.80"))
        self.assertEqual(item.cost_currency, "USD")

    def test_unknown_cost_stays_null(self):
        product = Product.objects.create(name="Eggs", company=self.company)
        sale, item = _sale_with(self.company, product)

        self.assertEqual(snapshot_item_costs(sale), 0)

        item.refresh_from_db()
        self.assertIsNone(item.unit_cost)
        self.assertIsNone(item.cost_currency)

    def test_free_product_is_a_known_zero_cost(self):
        product = Product.objects.create(
            name="Sample", company=self.company, cost=Decimal("0.00"), cost_currency="USD"
        )
        sale, item = _sale_with(self.company, product)

        snapshot_item_costs(sale)

        item.refresh_from_db()
        self.assertEqual(item.unit_cost, Decimal("0.00"))

    def test_existing_snapshot_is_never_overwritten(self):
        product = Product.objects.create(
            name="Bread", company=self.company, cost=Decimal("1.00"), cost_currency="USD"
        )
        sale, item = _sale_with(self.company, product)
        item.unit_cost = Decimal("0.80")
        item.cost_currency = "USD"
        item.save()

        self.assertEqual(snapshot_item_costs(sale), 0)

        item.refresh_from_db()
        self.assertEqual(item.unit_cost, Decimal("0.80"))

    def test_cost_keeps_its_own_currency_when_sale_currency_differs(self):
        product = Product.objects.create(
            name="Airtime", company=self.company, cost=Decimal("4.00"), cost_currency="ZWG"
        )
        sale, item = _sale_with(self.company, product, currency="USD")

        snapshot_item_costs(sale)

        item.refresh_from_db()
        self.assertEqual(item.currency, "USD")
        self.assertEqual(item.cost_currency, "ZWG")


class ConfirmSnapshotsCostTest(TransactionTestCase):
    # Same cross-thread reason as ConfirmSaleWritesPriceTest in test_sale_confirmation.
    def setUp(self):
        self.company = _company()
        self.product = Product.objects.create(
            name="Bread", company=self.company, cost=Decimal("0.80"), cost_currency="USD"
        )

    def test_confirm_freezes_cost_and_later_cost_change_does_not_touch_it(self):
        sale, item = _sale_with(self.company, self.product)

        run_async_test(_get_confirmed_sale("sid_c"))

        self.product.cost = Decimal("1.00")
        self.product.save()

        item.refresh_from_db()
        sale.refresh_from_db()
        self.assertEqual(sale.status, Sale.Status.CONFIRMED)
        self.assertEqual(item.unit_cost, Decimal("0.80"))

    def test_pending_sale_has_no_snapshot_until_confirmed(self):
        _, item = _sale_with(self.company, self.product)

        item.refresh_from_db()
        self.assertIsNone(item.unit_cost)


class ExistingCostCurrencyMigrationTest(TestCase):
    """0007 gives a hand-entered cost (set before cost_currency existed) the shop's currency."""

    def test_backfill_function_sets_the_shop_currency_only_where_missing(self):
        import importlib

        migration = importlib.import_module("apps.catalog.migrations.0007_backfill_product_cost_currency")
        company = Company.objects.create(name="Old Shop", slug="old-shop", active=True, currency="ZAR")
        legacy = Product.objects.create(name="Legacy", company=company)
        Product.objects.filter(id=legacy.id).update(cost=Decimal("5.00"), cost_currency=None)  # bypasses save()
        keeps = Product.objects.create(name="Keeps", company=company, cost=Decimal("1.00"), cost_currency="USD")
        unknown = Product.objects.create(name="Unknown", company=company)

        from django.apps import apps as django_apps
        migration.default_cost_currency(django_apps, None)

        for product in (legacy, keeps, unknown):
            product.refresh_from_db()
        self.assertEqual(legacy.cost_currency, "ZAR")
        self.assertEqual(keeps.cost_currency, "USD")
        self.assertIsNone(unknown.cost_currency)


class ZeroCostMigrationTest(TestCase):
    """0005 turns the old 0.00 default (nothing ever set a cost) into NULL = unknown."""

    def test_zero_becomes_unknown_and_real_costs_are_kept(self):
        import importlib
        from django.apps import apps as django_apps

        migration = importlib.import_module("apps.catalog.migrations.0005_product_cost_currency_alter_product_cost")
        company = _company()
        old_default = Product.objects.create(name="Old", company=company)
        Product.objects.filter(id=old_default.id).update(cost=Decimal("0.00"))
        real = Product.objects.create(name="Real", company=company, cost=Decimal("1.50"), cost_currency="USD")

        migration.zero_cost_to_unknown(django_apps, None)

        old_default.refresh_from_db()
        real.refresh_from_db()
        self.assertIsNone(old_default.cost)
        self.assertEqual(real.cost, Decimal("1.50"))
