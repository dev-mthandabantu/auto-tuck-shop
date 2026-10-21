"""Unit tests for build_itemised_sales_list / format_itemised_sales_list (#408).

Pure data/formatting logic — no LLM call needed, unlike test_shona_parsing.py.

Grouped by product, not one line per individual sale — per Brighton's review
on PR #425: a per-sale view hit the 20-line display limit almost immediately
for a week-long query, and owners asked "how much of each did I sell", which
is inherently a grouped question.
"""

from decimal import Decimal

from django.test import TestCase
from django.utils import timezone

from apps.catalog.models import Product
from apps.core.models import Company
from apps.sales.models import Sale, SaleItem
from apps.whatsapp.services.business_reports import (
    MAX_ITEMISED_LINES,
    build_itemised_sales_list,
    format_itemised_sales_list,
)


def _make_company(name="Test Shop"):
    return Company.objects.create(name=name, slug=name.lower().replace(" ", "-"), active=True)


def _make_product(company, name):
    return Product.objects.create(company=company, name=name)


def _make_confirmed_sale(company, items, when=None):
    """items: list of (product, quantity, unit_price, currency) tuples."""
    sale = Sale.objects.create(company=company, status=Sale.Status.CONFIRMED)
    if when is not None:
        Sale.objects.filter(pk=sale.pk).update(sale_timestamp=when)
    for product, quantity, unit_price, currency in items:
        SaleItem.objects.create(
            sale=sale, product=product, quantity=quantity,
            unit_price=unit_price, currency=currency,
        )
    return sale


class BuildItemisedSalesListTest(TestCase):

    def setUp(self):
        self.company = _make_company()
        self.today = timezone.localdate()

    def test_same_product_across_separate_sales_is_combined_into_one_entry(self):
        """Regression for #408's grouped-by-product design: two separate rice
        sales in the same day must combine into one 'Rice' entry with the
        summed quantity, not appear as two separate lines."""
        rice = _make_product(self.company, "Rice")
        bread = _make_product(self.company, "Bread")
        _make_confirmed_sale(self.company, [(rice, 2, Decimal("2.00"), "USD")], when=timezone.now())
        _make_confirmed_sale(self.company, [(bread, 3, Decimal("1.00"), "USD")], when=timezone.now())
        _make_confirmed_sale(self.company, [(rice, 1, Decimal("2.00"), "USD")], when=timezone.now())

        data = build_itemised_sales_list(self.company, self.today, self.today)

        self.assertEqual(data["sales_count"], 3)  # 3 sale transactions
        self.assertEqual(len(data["entries"]), 2)  # but only 2 distinct products
        rice_entry = next(e for e in data["entries"] if e["product"] == "Rice")
        self.assertEqual(rice_entry["quantity"], 3)  # 2 + 1, combined
        self.assertEqual(rice_entry["revenue"], Decimal("6.00"))  # (2*2) + (1*2)

    def test_same_product_in_different_currencies_stays_as_separate_lines(self):
        """Regression for Brighton's second review on #425: the same product
        sold in two different currencies (common here — USD, rand and ZiG in
        the same day) must NOT be summed together into one nonsensical
        total. "$3 of bread" and "R20 of bread" must appear as two separate
        entries, grouped by (product, currency), not product alone."""
        bread = _make_product(self.company, "Bread")
        _make_confirmed_sale(self.company, [(bread, 1, Decimal("3.00"), "USD")], when=timezone.now())
        _make_confirmed_sale(self.company, [(bread, 1, Decimal("20.00"), "ZAR")], when=timezone.now())

        data = build_itemised_sales_list(self.company, self.today, self.today)

        bread_entries = [e for e in data["entries"] if e["product"] == "Bread"]
        self.assertEqual(len(bread_entries), 2)  # one per currency, not combined
        usd_entry = next(e for e in bread_entries if e["currency"] == "USD")
        zar_entry = next(e for e in bread_entries if e["currency"] == "ZAR")
        self.assertEqual(usd_entry["quantity"], 1)
        self.assertEqual(usd_entry["revenue"], Decimal("3.00"))
        self.assertEqual(zar_entry["quantity"], 1)
        self.assertEqual(zar_entry["revenue"], Decimal("20.00"))
        # Currency totals also stay separate, never summed across currencies
        self.assertEqual(data["currency_totals"]["USD"], Decimal("3.00"))
        self.assertEqual(data["currency_totals"]["ZAR"], Decimal("20.00"))

    def test_entries_are_sorted_by_quantity_descending(self):
        rice = _make_product(self.company, "Rice")
        bread = _make_product(self.company, "Bread")
        _make_confirmed_sale(self.company, [(bread, 3, Decimal("1.00"), "USD")], when=timezone.now())
        _make_confirmed_sale(self.company, [(rice, 10, Decimal("2.00"), "USD")], when=timezone.now())

        data = build_itemised_sales_list(self.company, self.today, self.today)

        self.assertEqual(data["entries"][0]["product"], "Rice")  # higher quantity first
        self.assertEqual(data["entries"][1]["product"], "Bread")

    def test_total_sums_across_entries_by_currency(self):
        rice = _make_product(self.company, "Rice")
        bread = _make_product(self.company, "Bread")
        _make_confirmed_sale(self.company, [(rice, 2, Decimal("2.00"), "USD")], when=timezone.now())
        _make_confirmed_sale(self.company, [(bread, 3, Decimal("1.00"), "USD")], when=timezone.now())

        data = build_itemised_sales_list(self.company, self.today, self.today)

        self.assertEqual(data["currency_totals"]["USD"], Decimal("7.00"))  # (2*2) + (3*1)

    def test_pending_sale_is_excluded(self):
        """Only CONFIRMED sales count — a still-pending sale hasn't actually
        happened yet as far as the owner's records go."""
        rice = _make_product(self.company, "Rice")
        sale = Sale.objects.create(company=self.company, status=Sale.Status.PENDING)
        SaleItem.objects.create(sale=sale, product=rice, quantity=2, unit_price=Decimal("2.00"), currency="USD")

        data = build_itemised_sales_list(self.company, self.today, self.today)

        self.assertEqual(data["sales_count"], 0)
        self.assertEqual(data["entries"], [])

    def test_sale_outside_date_range_is_excluded(self):
        from datetime import timedelta
        rice = _make_product(self.company, "Rice")
        last_week = timezone.now() - timedelta(days=7)
        _make_confirmed_sale(self.company, [(rice, 2, Decimal("2.00"), "USD")], when=last_week)

        data = build_itemised_sales_list(self.company, self.today, self.today)

        self.assertEqual(data["sales_count"], 0)

    def test_no_sales_in_range_returns_empty_not_an_error(self):
        data = build_itemised_sales_list(self.company, self.today, self.today)

        self.assertEqual(data["sales_count"], 0)
        self.assertEqual(data["entries"], [])
        self.assertEqual(data["currency_totals"], {})


class FormatItemisedSalesListTest(TestCase):

    def setUp(self):
        self.company = _make_company()
        self.today = timezone.localdate()

    def test_no_sales_message(self):
        data = build_itemised_sales_list(self.company, self.today, self.today)
        text = format_itemised_sales_list("today", data, lang="en")
        self.assertIn("No sales recorded", text)

    def test_lists_each_product_with_quantity_and_price(self):
        rice = _make_product(self.company, "Rice")
        _make_confirmed_sale(self.company, [(rice, 2, Decimal("2.00"), "USD")], when=timezone.now())

        data = build_itemised_sales_list(self.company, self.today, self.today)
        text = format_itemised_sales_list("today", data, lang="en")

        self.assertIn("Rice x 2", text)
        self.assertIn("Total", text)

    def test_no_dead_dashboard_link(self):
        """Regression for Brighton's review on #425: the 'See more sales' link
        pointed at a page that doesn't exist. Removed — tracked separately
        in #427 for once a real dashboard exists."""
        rice = _make_product(self.company, "Rice")
        _make_confirmed_sale(self.company, [(rice, 1, Decimal("1.00"), "USD")], when=timezone.now())

        data = build_itemised_sales_list(self.company, self.today, self.today)
        text = format_itemised_sales_list("today", data, lang="en")

        self.assertNotIn("dashboard", text.lower())
        self.assertNotIn("See more", text)

    def test_long_list_is_truncated_with_a_count_of_the_rest(self):
        """Regression for #408's own stated risk: a message with too many
        distinct products must not be sent in full — truncate and say how
        many more."""
        for i in range(MAX_ITEMISED_LINES + 7):
            product = _make_product(self.company, f"Product {i}")
            _make_confirmed_sale(self.company, [(product, 1, Decimal("1.00"), "USD")], when=timezone.now())

        data = build_itemised_sales_list(self.company, self.today, self.today)
        text = format_itemised_sales_list("today", data, lang="en")

        shown_lines = text.count(" x 1,")
        self.assertEqual(shown_lines, MAX_ITEMISED_LINES)
        self.assertIn("...and 7 more", text)

    def test_exactly_at_the_limit_has_no_truncation_note(self):
        for i in range(MAX_ITEMISED_LINES):
            product = _make_product(self.company, f"Product {i}")
            _make_confirmed_sale(self.company, [(product, 1, Decimal("1.00"), "USD")], when=timezone.now())

        data = build_itemised_sales_list(self.company, self.today, self.today)
        text = format_itemised_sales_list("today", data, lang="en")

        self.assertNotIn("more.", text)

    def test_truncated_list_total_still_counts_every_product_not_just_shown(self):
        """The total must sum ALL products in the period, even the ones cut
        off from display — not just the (MAX_ITEMISED_LINES) shown. Pins
        down the exact risk a fresh-context review flagged: a future change
        that 'simplifies' the total to sum only `shown` would silently
        under-report a busy shop's real total."""
        total_products = MAX_ITEMISED_LINES + 7
        for i in range(total_products):
            product = _make_product(self.company, f"Product {i}")
            _make_confirmed_sale(self.company, [(product, 1, Decimal("1.00"), "USD")], when=timezone.now())

        data = build_itemised_sales_list(self.company, self.today, self.today)
        text = format_itemised_sales_list("today", data, lang="en")

        expected_total = Decimal("1.00") * total_products  # all 27, not just the 20 shown
        self.assertIn(f"${expected_total:.2f}", text)
        self.assertEqual(data["currency_totals"]["USD"], expected_total)

    def test_item_with_no_price_shows_a_placeholder_not_a_crash(self):
        """A sale item can have unit_price=None (e.g. ambiguous price never
        clarified) — formatting must not crash on it."""
        rice = _make_product(self.company, "Rice")
        _make_confirmed_sale(self.company, [(rice, 2, None, None)], when=timezone.now())

        data = build_itemised_sales_list(self.company, self.today, self.today)
        text = format_itemised_sales_list("today", data, lang="en")

        self.assertIn("Rice x 2", text)
        self.assertIn("—", text)

    def test_shona_is_the_default_language_when_none_is_given(self):
        """DEFAULT_LANGUAGE is "sn" (Shona), not English, across this whole
        codebase — calling without a lang argument must render Shona, not
        silently fall back to English."""
        rice = _make_product(self.company, "Rice")
        _make_confirmed_sale(self.company, [(rice, 2, Decimal("2.00"), "USD")], when=timezone.now())

        data = build_itemised_sales_list(self.company, self.today, self.today)
        text = format_itemised_sales_list("today", data)  # no lang= passed

        self.assertIn("Zvawatengesa", text)
        self.assertIn("Zvese pamwe", text)

    def test_shona_no_sales_message(self):
        data = build_itemised_sales_list(self.company, self.today, self.today)
        text = format_itemised_sales_list("today", data, lang="sn")
        self.assertIn("Hapana zvatengeswa", text)


class ProductFilterTest(TestCase):
    """Regression for #436: "show me all the bread I sold this week" must
    only return bread, not every product the shop sold."""

    def setUp(self):
        self.company = _make_company()
        self.today = timezone.localdate()

    def test_filter_returns_only_the_matching_product(self):
        bread = _make_product(self.company, "Bread")
        book = _make_product(self.company, "Book")
        _make_confirmed_sale(self.company, [(bread, 2, Decimal("1.00"), "USD")], when=timezone.now())
        _make_confirmed_sale(self.company, [(book, 1, Decimal("5.00"), "USD")], when=timezone.now())

        data = build_itemised_sales_list(self.company, self.today, self.today, product_filter="bread")

        self.assertEqual(len(data["entries"]), 1)
        self.assertEqual(data["entries"][0]["product"], "Bread")
        self.assertEqual(data["sales_count"], 1)

    def test_filter_is_case_insensitive_substring_match(self):
        bread = _make_product(self.company, "White Bread")
        _make_confirmed_sale(self.company, [(bread, 1, Decimal("1.00"), "USD")], when=timezone.now())

        data = build_itemised_sales_list(self.company, self.today, self.today, product_filter="BREAD")

        self.assertEqual(len(data["entries"]), 1)

    def test_filter_excludes_other_products_in_a_mixed_sale(self):
        """A single sale can contain several products ("2 bread and 3
        coke") — the query-level filter only narrows which SALES are
        included; the unrelated line within a matching sale must still be
        excluded."""
        bread = _make_product(self.company, "Bread")
        coke = _make_product(self.company, "Coke")
        _make_confirmed_sale(
            self.company,
            [(bread, 2, Decimal("1.00"), "USD"), (coke, 3, Decimal("1.00"), "USD")],
            when=timezone.now(),
        )

        data = build_itemised_sales_list(self.company, self.today, self.today, product_filter="bread")

        self.assertEqual(len(data["entries"]), 1)
        self.assertEqual(data["entries"][0]["product"], "Bread")

    def test_filter_matching_nothing_gives_a_clear_message_not_everything(self):
        bread = _make_product(self.company, "Bread")
        _make_confirmed_sale(self.company, [(bread, 2, Decimal("1.00"), "USD")], when=timezone.now())

        data = build_itemised_sales_list(self.company, self.today, self.today, product_filter="samosa")
        text = format_itemised_sales_list("today", data, lang="en", product_filter="samosa")

        self.assertEqual(data["sales_count"], 0)
        self.assertIn("samosa", text.lower())
        self.assertNotIn("Bread", text)

    def test_no_filter_still_returns_everything(self):
        """Regression check: adding the filter parameter must not change
        behaviour when no filter is given — the existing unfiltered tests
        in this file must keep passing unmodified."""
        bread = _make_product(self.company, "Bread")
        book = _make_product(self.company, "Book")
        _make_confirmed_sale(self.company, [(bread, 1, Decimal("1.00"), "USD")], when=timezone.now())
        _make_confirmed_sale(self.company, [(book, 1, Decimal("5.00"), "USD")], when=timezone.now())

        data = build_itemised_sales_list(self.company, self.today, self.today)

        self.assertEqual(len(data["entries"]), 2)
