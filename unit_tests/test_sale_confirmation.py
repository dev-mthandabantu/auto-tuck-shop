"""Unit tests for the sale confirm/cancel flow — PENDING → CONFIRMED/CANCELLED,
and the price write that happens at confirmation, not at parse time."""

import asyncio
import threading
from decimal import Decimal

from django.test import TestCase, TransactionTestCase

from apps.catalog.models import Product, ProductPrice
from apps.core.models import Company
from apps.inventory.models import InventoryAdjustment
from apps.sales.models import Sale, SaleItem
from apps.whatsapp.services.sale_handler import _get_confirmed_sale, _get_and_update_sale


def _make_company(name="Test Shop"):
    return Company.objects.create(name=name, slug=name.lower().replace(" ", "-"), active=True)


def _make_pending_sale(company, product, unit_price, currency="USD", sid="sid_1"):
    sale = Sale.objects.create(company=company, confirmation_message_sid=sid)
    SaleItem.objects.create(sale=sale, product=product, quantity=1, unit_price=unit_price, currency=currency)
    return sale


class SaleDefaultStatusTest(TestCase):
    def test_new_sale_defaults_to_pending(self):
        company = _make_company()
        sale = Sale.objects.create(company=company)
        self.assertEqual(sale.status, Sale.Status.PENDING)


class ConfirmSaleWritesPriceTest(TransactionTestCase):
    # _get_confirmed_sale runs on a separate thread (asgiref sync_to_async),
    # which needs its writes actually committed to be visible — a plain
    # TestCase's per-test transaction isn't visible across threads.
    def setUp(self):
        self.company = _make_company()
        self.product = Product.objects.create(name="Apples", company=self.company)

    def test_confirm_writes_new_price(self):
        sale = _make_pending_sale(self.company, self.product, Decimal("2.00"))

        result = asyncio.run(_get_confirmed_sale("sid_1"))

        self.assertIsNotNone(result)
        sale.refresh_from_db()
        self.assertEqual(sale.status, Sale.Status.CONFIRMED)
        self.assertEqual(self.product.current_price, Decimal("2.00"))

    def test_confirm_does_not_duplicate_price_when_unchanged(self):
        ProductPrice.objects.create(product=self.product, price=Decimal("2.00"), currency="USD")
        _make_pending_sale(self.company, self.product, Decimal("2.00"))

        asyncio.run(_get_confirmed_sale("sid_1"))

        self.assertEqual(ProductPrice.objects.filter(product=self.product).count(), 1)

    def test_confirm_on_unknown_sid_returns_none(self):
        result = asyncio.run(_get_confirmed_sale("does_not_exist"))
        self.assertIsNone(result)

    def test_confirm_twice_second_call_returns_none(self):
        """Second 'Looks good' click on an already-confirmed sale should not
        re-match (sale is no longer PENDING) and must not write price again."""
        _make_pending_sale(self.company, self.product, Decimal("2.00"))
        asyncio.run(_get_confirmed_sale("sid_1"))

        result = asyncio.run(_get_confirmed_sale("sid_1"))

        self.assertIsNone(result)
        self.assertEqual(ProductPrice.objects.filter(product=self.product).count(), 1)


class ConcurrentConfirmCancelRaceTest(TransactionTestCase):
    """select_for_update() must prevent a 'Looks good' and 'Fix mistake'
    tap on the same sale from both succeeding (flagged in code review on
    #293's PR: without row locking, two concurrent reads can both observe
    PENDING before either commits)."""

    def setUp(self):
        self.company = _make_company()
        self.product = Product.objects.create(name="Apples", company=self.company)

    def test_concurrent_confirm_and_cancel_only_one_wins(self):
        _make_pending_sale(self.company, self.product, Decimal("2.00"))

        results = {}
        start_confirm = threading.Event()
        start_cancel = threading.Event()

        def run_confirm():
            start_confirm.wait()
            results["confirm"] = asyncio.run(_get_confirmed_sale("sid_1"))

        def run_cancel():
            start_cancel.wait()
            results["cancel"] = asyncio.run(_get_and_update_sale("sid_1", Sale.Status.CANCELLED))

        t1 = threading.Thread(target=run_confirm)
        t2 = threading.Thread(target=run_cancel)
        t1.start()
        t2.start()
        start_confirm.set()
        start_cancel.set()
        t1.join()
        t2.join()

        # Exactly one of the two actions should have found the sale still
        # PENDING and succeeded; the other must see it's already been
        # transitioned and return None — never both succeeding.
        succeeded = [r for r in results.values() if r is not None]
        self.assertEqual(
            len(succeeded), 1,
            f"Expected exactly one winner, got: {results}",
        )

        sale = Sale.objects.get(confirmation_message_sid="sid_1")
        self.assertIn(sale.status, [Sale.Status.CONFIRMED, Sale.Status.CANCELLED])

        # If the confirm won, the price must have been written; if the
        # cancel won, it must not have been.
        price_written = ProductPrice.objects.filter(product=self.product).exists()
        if sale.status == Sale.Status.CONFIRMED:
            self.assertTrue(price_written, "Confirm won but price was never written")
        else:
            self.assertFalse(price_written, "Cancel won but price was written anyway")


class RejectedSaleDoesNotWritePriceTest(TransactionTestCase):
    """Regression test for #293: a rejected sale must never overwrite the
    product's stored price."""

    def setUp(self):
        self.company = _make_company()
        self.product = Product.objects.create(name="Apples", company=self.company)
        ProductPrice.objects.create(product=self.product, price=Decimal("2.00"), currency="USD")

    def test_fix_mistake_does_not_write_price(self):
        _make_pending_sale(self.company, self.product, Decimal("100.00"))

        result = asyncio.run(_get_and_update_sale("sid_1", Sale.Status.CANCELLED))

        self.assertIsNotNone(result)
        self.assertEqual(self.product.current_price, Decimal("2.00"))
        self.assertEqual(ProductPrice.objects.filter(product=self.product).count(), 1)

    def test_parsing_a_stated_price_does_not_write_it_at_parse_time(self):
        """The price write must happen at confirm time, not when
        create_sale_from_parsed_items runs — parsing a sale must never touch
        the product's stored price on its own."""
        from apps.sales.services import create_sale_from_parsed_items

        create_sale_from_parsed_items(
            items=[{"product_name": "Apples", "quantity": 1, "unit_price": Decimal("100.00"), "currency": "USD"}],
            company=self.company,
        )

        self.assertEqual(self.product.current_price, Decimal("2.00"))
        self.assertEqual(ProductPrice.objects.filter(product=self.product).count(), 1)

    def test_cancelled_sale_price_never_leaks_to_next_unpriced_sale(self):
        """End-to-end regression matching the original bug report: parse and
        reject a $100 sale, then an unpriced sale of the same product must
        still use the original $2 stored price, not $100."""
        from apps.sales.services import create_sale_from_parsed_items

        bad_sale_result = create_sale_from_parsed_items(
            items=[{"product_name": "Apples", "quantity": 1, "unit_price": Decimal("100.00"), "currency": "USD"}],
            company=self.company,
        )
        bad_sale = bad_sale_result["sale"]
        bad_sale.confirmation_message_sid = "sid_1"
        bad_sale.save(update_fields=["confirmation_message_sid"])

        asyncio.run(_get_and_update_sale("sid_1", Sale.Status.CANCELLED))

        result = create_sale_from_parsed_items(
            items=[{"product_name": "Apples", "quantity": 1, "unit_price": None, "currency": None}],
            company=self.company,
        )
        item = result["sale"].items.get()
        self.assertEqual(item.unit_price, Decimal("2.00"))


class CurrentStockExcludesNonConfirmedSalesTest(TestCase):
    """Regression coverage for the current_stock fix: pending/cancelled sales
    must not affect stock, only confirmed ones."""

    def setUp(self):
        self.company = _make_company()
        self.product = Product.objects.create(name="Apples", company=self.company)
        InventoryAdjustment.objects.create(
            product=self.product, quantity_delta=10, reason=InventoryAdjustment.Reason.INITIAL
        )

    def _make_sale(self, status, quantity=1, sid="sid_1"):
        sale = Sale.objects.create(company=self.company, status=status, confirmation_message_sid=sid)
        SaleItem.objects.create(
            sale=sale, product=self.product, quantity=quantity, unit_price=Decimal("2.00"), currency="USD"
        )
        return sale

    def test_pending_sale_does_not_reduce_stock(self):
        self._make_sale(Sale.Status.PENDING, quantity=3)
        self.assertEqual(self.product.current_stock, 10)

    def test_cancelled_sale_does_not_reduce_stock(self):
        self._make_sale(Sale.Status.CANCELLED, quantity=3)
        self.assertEqual(self.product.current_stock, 10)

    def test_confirmed_sale_reduces_stock(self):
        self._make_sale(Sale.Status.CONFIRMED, quantity=3)
        self.assertEqual(self.product.current_stock, 7)
