"""Unit tests for apps/sales/services.py sale creation, focused on price resolution."""

from decimal import Decimal

from django.test import TestCase

from apps.catalog.models import Product, ProductPrice
from apps.core.models import Company
from apps.sales.models import Sale
from apps.sales.services import canonicalize_product_name, create_sale_from_parsed_items, find_product_by_name
from apps.whatsapp.services.message_parser import _apply_rule_based_price_hints


def _make_company(name="Test Shop"):
    return Company.objects.create(name=name, slug=name.lower().replace(" ", "-"), active=True)


class CreateSaleFromParsedItemsTest(TestCase):

    def setUp(self):
        self.company = _make_company()

    def test_falls_back_to_stored_price_when_no_price_mentioned(self):
        """No price in the message at all — reuse the last known stored price."""
        product = Product.objects.create(name="Games", company=self.company)
        ProductPrice.objects.create(product=product, price=Decimal("5.00"), currency="ZAR")

        result = create_sale_from_parsed_items(
            items=[{"product_name": "Games", "quantity": 4, "unit_price": None, "currency": None}],
            company=self.company,
        )
        item = result["sale"].items.get()
        self.assertEqual(item.unit_price, Decimal("5.00"))
        self.assertEqual(item.currency, "ZAR")

    def test_ambiguous_price_does_not_fall_back_to_stored_price(self):
        """Regression for #220: a price WAS stated but was ambiguous (total vs
        per-unit, e.g. '4 games $1 sale') — must not silently substitute the
        stored/catalog price. The owner should be asked to clarify instead."""
        product = Product.objects.create(name="Games", company=self.company)
        ProductPrice.objects.create(product=product, price=Decimal("5.00"), currency="ZAR")

        result = create_sale_from_parsed_items(
            items=[{
                "product_name": "Games",
                "quantity": 4,
                "unit_price": None,
                "currency": None,
                "price_ambiguous": True,
            }],
            company=self.company,
        )

        self.assertIsNone(result["sale"])
        self.assertEqual(result["ambiguous_items"], ["Games"])
        self.assertEqual(Sale.objects.count(), 0)

    def test_stated_price_overrides_stored_price(self):
        """A clearly-parsed stated price always overrides the stored catalog price."""
        product = Product.objects.create(name="Games", company=self.company)
        ProductPrice.objects.create(product=product, price=Decimal("5.00"), currency="ZAR")

        result = create_sale_from_parsed_items(
            items=[{"product_name": "Games", "quantity": 4, "unit_price": Decimal("0.25"), "currency": "USD"}],
            company=self.company,
        )
        item = result["sale"].items.get()
        self.assertEqual(item.unit_price, Decimal("0.25"))
        self.assertEqual(item.currency, "USD")

    def test_partial_sale_preserves_valid_items_without_catalog_pollution(self):
        result = create_sale_from_parsed_items(
            items=[
                {
                    "product_name": "Bread",
                    "quantity": 2,
                    "unit_price": Decimal("0.50"),
                    "currency": "USD",
                },
                {
                    "product_name": "Chisp",
                    "quantity": 3,
                    "unit_price": None,
                    "currency": None,
                    "price_ambiguous": True,
                },
                {
                    "product_name": "Unknown",
                    "quantity": 1,
                    "unit_price": None,
                    "currency": None,
                },
            ],
            company=self.company,
        )

        self.assertIsNotNone(result["sale"])
        self.assertEqual(result["ambiguous_items"], ["Chisp"])
        self.assertEqual(result["unmatched_items"], [{"product_name": "Unknown", "quantity": 1}])
        self.assertEqual(result["sale"].items.count(), 1)
        self.assertTrue(Product.objects.filter(name="Bread", company=self.company).exists())
        self.assertFalse(Product.objects.filter(name="Chisp", company=self.company).exists())
        self.assertFalse(Product.objects.filter(name="Unknown", company=self.company).exists())

    def test_all_skipped_items_do_not_create_confirmed_empty_sale_or_products(self):
        result = create_sale_from_parsed_items(
            items=[
                {
                    "product_name": "Chisp",
                    "quantity": 3,
                    "unit_price": None,
                    "currency": None,
                    "price_ambiguous": True,
                },
                {
                    "product_name": "Unknown",
                    "quantity": 1,
                    "unit_price": None,
                    "currency": None,
                },
            ],
            company=self.company,
        )

        self.assertIsNone(result["sale"])
        self.assertEqual(result["ambiguous_items"], ["Chisp"])
        self.assertEqual(result["unmatched_items"], [{"product_name": "Unknown", "quantity": 1}])
        self.assertEqual(Sale.objects.count(), 0)
        self.assertEqual(Product.objects.count(), 0)

    def test_unmatched_item_carries_quantity_not_just_name(self):
        """Regression for #356: the Unmatched message needs the quantity the
        bot understood ("Got 3 x Chips...") — it was previously dropped
        between this function and the response builder in sale_handler.py."""
        result = create_sale_from_parsed_items(
            items=[{"product_name": "Chips", "quantity": 3, "unit_price": None, "currency": None}],
            company=self.company,
        )

        self.assertEqual(result["unmatched_items"], [{"product_name": "Chips", "quantity": 3}])

    def test_emoji_product_name_canonicalized_before_matching(self):
        """Regression for #356: two calls that both mean the same emoji
        product must resolve to the same Product, even if one call returns
        the raw emoji and the other returns repeated emoji characters."""
        result_a = create_sale_from_parsed_items(
            items=[{"product_name": "🚗", "quantity": 1, "unit_price": Decimal("5.00"), "currency": "USD"}],
            company=self.company,
        )
        result_b = create_sale_from_parsed_items(
            items=[{"product_name": "🚗🚗🚗", "quantity": 1, "unit_price": Decimal("5.00"), "currency": "USD"}],
            company=self.company,
        )

        product_a = result_a["sale"].items.get().product
        product_b = result_b["sale"].items.get().product
        self.assertEqual(product_a, product_b)
        self.assertEqual(product_a.name, "automobile")

    def test_emoji_and_text_product_name_resolve_to_same_product(self):
        """Regression for #356's actual repro: the LLM returned raw "🚗" on
        one call and the plain text "car" on another, for the same sale —
        those must land on the same Product row or price/report history for
        that product silently splits in two."""
        result_a = create_sale_from_parsed_items(
            items=[{"product_name": "🚗", "quantity": 1, "unit_price": Decimal("5.00"), "currency": "USD"}],
            company=self.company,
        )
        result_b = create_sale_from_parsed_items(
            items=[{"product_name": "car", "quantity": 1, "unit_price": Decimal("5.00"), "currency": "USD"}],
            company=self.company,
        )
        result_c = create_sale_from_parsed_items(
            items=[{"product_name": "car emoji", "quantity": 1, "unit_price": Decimal("5.00"), "currency": "USD"}],
            company=self.company,
        )

        product_a = result_a["sale"].items.get().product
        product_b = result_b["sale"].items.get().product
        product_c = result_c["sale"].items.get().product
        self.assertEqual(product_a, product_b)
        self.assertEqual(product_a, product_c)
        self.assertEqual(product_a.name, "automobile")
        self.assertEqual(Product.objects.count(), 1)

    def test_singular_and_plural_product_name_resolve_to_same_product(self):
        """Regression for #359: the LLM isn't deterministic about singular
        vs. plural for the same spoken/typed product across messages (e.g.
        "banana" on one call, "bananas" on another) — those must land on the
        same Product row or price/report history for that product silently
        splits in two, same failure mode as #356's emoji/text split. Fixed
        via find_product_by_name's singular/plural fallback lookup, not by
        transforming the incoming name (see FindProductByNameSingularPluralTest
        for why that would be unsafe)."""
        result_a = create_sale_from_parsed_items(
            items=[{"product_name": "banana", "quantity": 1, "unit_price": Decimal("3.00"), "currency": "USD"}],
            company=self.company,
        )
        result_b = create_sale_from_parsed_items(
            items=[{"product_name": "bananas", "quantity": 5, "unit_price": Decimal("3.00"), "currency": "USD"}],
            company=self.company,
        )

        product_a = result_a["sale"].items.get().product
        product_b = result_b["sale"].items.get().product
        self.assertEqual(product_a, product_b)
        self.assertEqual(Product.objects.count(), 1)


class CanonicalizeProductNameTest(TestCase):
    """Direct tests for canonicalize_product_name — no LLM or DB needed."""

    def test_raw_emoji_resolves_to_canonical_name(self):
        self.assertEqual(canonicalize_product_name("🚗"), "automobile")
        self.assertEqual(canonicalize_product_name("🏠"), "house")
        self.assertEqual(canonicalize_product_name("🍌"), "banana")

    def test_repeated_emoji_collapses_to_single_name(self):
        self.assertEqual(canonicalize_product_name("🚗🚗🚗"), "automobile")
        self.assertEqual(canonicalize_product_name("🏠🏠🏠"), "house")

    def test_text_with_emoji_suffix_strips_to_plain_word(self):
        self.assertEqual(canonicalize_product_name("house emoji"), "house")
        self.assertEqual(canonicalize_product_name("emoji house"), "house")

    def test_mapped_word_with_emoji_suffix_also_resolves_to_canonical(self):
        """"Car Emoji" strips to "Car" via _EMOJI_SUFFIX_RE, then "Car"
        (lowercased) hits _TEXT_TO_EMOJI_CANONICAL, same as bare "car"."""
        self.assertEqual(canonicalize_product_name("Car Emoji"), "automobile")

    def test_emoji_variants_for_same_emoji_converge(self):
        """The exact inconsistency from #356's repro: same real-world
        product, different LLM outputs across calls — raw emoji, that
        emoji's English word, and the word suffixed with "emoji" — must all
        canonicalize to the same name, or the sale/report history for one
        product silently splits across two Product rows."""
        emoji_variants = {"🚗", "🚗 emoji", "car", "car emoji", "Car Emoji"}
        canonicalized = {canonicalize_product_name(v) for v in emoji_variants}
        self.assertEqual(canonicalized, {"automobile"})

    def test_plain_text_name_is_left_unchanged(self):
        self.assertEqual(canonicalize_product_name("Mazoe"), "Mazoe")
        self.assertEqual(canonicalize_product_name("bread"), "bread")
        self.assertEqual(canonicalize_product_name("plates beef"), "plates beef")

    def test_mapped_text_synonyms_converge_with_their_emoji(self):
        """Words the LLM uses interchangeably with an emoji (not just the
        exact word the emoji demojizes to) must also converge, e.g. "home"
        for the house emoji."""
        self.assertEqual(canonicalize_product_name("home"), "house")
        self.assertEqual(canonicalize_product_name("Home"), "house")
        self.assertEqual(canonicalize_product_name("house"), "house")
        self.assertEqual(canonicalize_product_name("🏠"), "house")

    def test_mixed_text_and_emoji_left_unchanged(self):
        """Only strings composed entirely of emoji (after the 'emoji' suffix
        strip) are canonicalized — a name that mixes real text and an emoji
        character is ambiguous and left as the LLM produced it."""
        self.assertEqual(canonicalize_product_name("🚗 car"), "🚗 car")

    def test_empty_string_returned_unchanged(self):
        self.assertEqual(canonicalize_product_name(""), "")

    def test_reactive_emoji_still_canonicalizes(self):
        """Not just the pilot's demo emoji (house/car/banana) — any emoji
        composed string should canonicalize, since the LLM can return any of
        the full Unicode emoji set as a product_name."""
        self.assertEqual(canonicalize_product_name("👍"), "thumbs up")


class FindProductByNameSingularPluralTest(TestCase):
    """Direct tests for find_product_by_name's singular/plural fallback
    (#359) — a lookup-only fallback, never a rename, so it can't corrupt an
    existing product's stored name the way transforming the name up front
    did (see the "Games" regression this replaced)."""

    def setUp(self):
        self.company = _make_company()

    def test_plural_query_finds_existing_singular_product(self):
        product = Product.objects.create(name="banana", company=self.company)
        self.assertEqual(find_product_by_name("bananas", self.company), product)

    def test_singular_query_finds_existing_plural_product(self):
        product = Product.objects.create(name="cokes", company=self.company)
        self.assertEqual(find_product_by_name("coke", self.company), product)

    def test_exact_match_preferred_over_variant(self):
        exact = Product.objects.create(name="books", company=self.company)
        Product.objects.create(name="book", company=self.company)
        self.assertEqual(find_product_by_name("books", self.company), exact)

    def test_singular_lookup_matches_existing_plural_only_product_without_renaming_it(self):
        """The #359 regression this replaced: an up-front transform that
        stripped "Games" -> "Game" before the lookup silently failed to find
        the existing "Games" product at all (mismatch, not a merge). The
        fallback-lookup approach instead finds it correctly as a genuine
        singular/plural match — and, critically, never renames the stored
        product: it stays "Games" in the database."""
        product = Product.objects.create(name="Games", company=self.company)
        found = find_product_by_name("Game", self.company)
        self.assertEqual(found, product)
        self.assertEqual(found.name, "Games")

    def test_no_existing_product_at_all_returns_none(self):
        self.assertIsNone(find_product_by_name("Widget", self.company))

    def test_short_word_has_no_variant_lookup(self):
        """Words of 3 characters or fewer are skipped, matching #220-era
        exact-match-only behavior for short/ambiguous tokens like "gas"."""
        Product.objects.create(name="gas", company=self.company)
        self.assertIsNone(find_product_by_name("ga", self.company))

    def test_irregular_es_plural_resolves_to_singular(self):
        """"boxes" -> "box": a bare trailing-"s" strip alone gives "boxe",
        which never matches. Words ending in s/x/z/ch/sh pluralize with
        "-es", not just "-s" — the fallback needs to try stripping "es" too."""
        product = Product.objects.create(name="box", company=self.company)
        self.assertEqual(find_product_by_name("boxes", self.company), product)

    def test_irregular_es_singular_resolves_to_plural(self):
        product = Product.objects.create(name="matches", company=self.company)
        self.assertEqual(find_product_by_name("match", self.company), product)

    def test_double_s_word_does_not_produce_triple_s_variant(self):
        """"glass" must not generate "glasss" as a fallback (a bare "+s"
        append on a word already ending "ss") — it should try the real "-es"
        plural instead."""
        product = Product.objects.create(name="glasses", company=self.company)
        self.assertEqual(find_product_by_name("glass", self.company), product)

    def test_es_suffix_class_does_not_also_produce_bare_s_variant(self):
        """"match" pluralizes to "matches" only — not "matchs" as well.
        Any word ending in s/x/z/ch/sh always malforms under a bare "+s"
        append, so it should never be generated for that class, not just
        for the "ss" case "glass" covers."""
        product = Product.objects.create(name="matches", company=self.company)
        self.assertEqual(find_product_by_name("match", self.company), product)
        decoy = Product.objects.create(name="matchs", company=self.company)
        self.assertNotEqual(find_product_by_name("match", self.company), decoy)

    def test_case_variant_duplicates_resolve_deterministically(self):
        """If a company already has both "Banana" and "banana" as separate
        rows (a pre-existing duplicate, not something this lookup creates),
        the fallback must not depend on undefined DB ordering — it should
        consistently resolve to the same (oldest) row."""
        first = Product.objects.create(name="Banana", company=self.company)
        Product.objects.create(name="banana", company=self.company)
        self.assertEqual(find_product_by_name("bananas", self.company), first)


class PriceHintsTest(TestCase):
    """Direct tests for _apply_rule_based_price_hints — no LLM needed."""

    def test_price_equals_qty_resolves_to_one_dollar(self):
        """'7 plates beef $7' — price == qty → $1 each, not ambiguous."""
        items = [{"product_name": "plates beef", "quantity": 7, "unit_price": Decimal("7.00"), "currency": "USD"}]
        result, _ = _apply_rule_based_price_hints("7 plates beef $7", items, "USD")
        self.assertEqual(result[0]["unit_price"], Decimal("1.00"))
        self.assertFalse(result[0].get("price_ambiguous"))

    def test_price_divisible_by_qty_resolves_unit_price(self):
        """'7 plates beef $14' — 14 ÷ 7 = $2 exactly → $2 each, not ambiguous."""
        items = [{"product_name": "plates beef", "quantity": 7, "unit_price": Decimal("14.00"), "currency": "USD"}]
        result, _ = _apply_rule_based_price_hints("7 plates beef $14", items, "USD")
        self.assertEqual(result[0]["unit_price"], Decimal("2.00"))
        self.assertFalse(result[0].get("price_ambiguous"))

    def test_non_divisible_price_stays_ambiguous(self):
        """'7 plates beef $15' — 15 ÷ 7 = $2.142… (non-exact) → ambiguous."""
        items = [{"product_name": "plates beef", "quantity": 7, "unit_price": Decimal("15.00"), "currency": "USD"}]
        result, _ = _apply_rule_based_price_hints("7 plates beef $15", items, "USD")
        self.assertIsNone(result[0]["unit_price"])
        self.assertTrue(result[0].get("price_ambiguous"))

    def test_cents_price_not_treated_as_total(self):
        """Regression: '3 chips $1.50' — a price with cents is essentially
        never a stated total in this domain. Must stay $1.50 each, NOT be
        treated as a total-to-divide (which produced the wrong $0.50/each
        and, worse, previously flagged it ambiguous since 1.50 % 3 != 0)."""
        items = [{"product_name": "chips", "quantity": 3, "unit_price": Decimal("1.50"), "currency": "USD"}]
        result, _ = _apply_rule_based_price_hints("3 chips $1.50", items, "USD")
        self.assertEqual(result[0]["unit_price"], Decimal("1.50"))
        self.assertFalse(result[0].get("price_ambiguous"))

    def test_cents_price_below_qty_not_treated_as_total(self):
        """'4 games $0.25' — cents price smaller than qty. Locks in the
        no-cents-means-skip-the-check reasoning explicitly, rather than
        relying on the coincidental non-zero remainder (0.25 % 4 != 0)."""
        items = [{"product_name": "games", "quantity": 4, "unit_price": Decimal("0.25"), "currency": "USD"}]
        result, _ = _apply_rule_based_price_hints("4 games $0.25", items, "USD")
        self.assertEqual(result[0]["unit_price"], Decimal("0.25"))
        self.assertFalse(result[0].get("price_ambiguous"))

    def test_llm_already_resolved_whole_number_total_trusted(self):
        """Regression: the LLM sometimes pre-resolves a stated total into a
        per-unit price itself (observed live: '7 plates beef $7' -> LLM
        returns unit_price=1.00 directly, with a note explaining the
        division). The literal price typed in the message ($7) differs from
        the LLM's returned unit_price (1.00) — that mismatch means the LLM
        already did the math, so the heuristic must NOT re-check divisibility
        against 1.00 (1 % 7 != 0 would wrongly flag it ambiguous)."""
        items = [{"product_name": "plates beef", "quantity": 7, "unit_price": Decimal("1.00"), "currency": "USD"}]
        result, _ = _apply_rule_based_price_hints("7 plates beef $7", items, "USD")
        self.assertEqual(result[0]["unit_price"], Decimal("1.00"))
        self.assertFalse(result[0].get("price_ambiguous"))

    def test_llm_already_resolved_second_case(self):
        """Same shape as above for '7 plates beef $14' -> LLM pre-resolved to
        unit_price=2.00 (literal stated price 14 differs from 2.00 -> trust
        the LLM, don't re-check 2 % 7 != 0)."""
        items = [{"product_name": "plates beef", "quantity": 7, "unit_price": Decimal("2.00"), "currency": "USD"}]
        result, _ = _apply_rule_based_price_hints("7 plates beef $14", items, "USD")
        self.assertEqual(result[0]["unit_price"], Decimal("2.00"))
        self.assertFalse(result[0].get("price_ambiguous"))

    def test_llm_passthrough_whole_number_still_divides(self):
        """Confirms the ORIGINAL #173 behavior is preserved when the LLM
        passes the raw stated price through unresolved: same message
        '7 plates beef $7', but this time unit_price=7.00 (matches the
        literal stated price -> LLM did NOT pre-resolve) -> the heuristic
        should still divide it to $1.00 each, exactly as before."""
        items = [{"product_name": "plates beef", "quantity": 7, "unit_price": Decimal("7.00"), "currency": "USD"}]
        result, _ = _apply_rule_based_price_hints("7 plates beef $7", items, "USD")
        self.assertEqual(result[0]["unit_price"], Decimal("1.00"))
        self.assertFalse(result[0].get("price_ambiguous"))

    def test_llm_passthrough_non_divisible_still_ambiguous(self):
        """Original #220 case unchanged: '4 games $1 sale', unit_price=1.00
        matches the literal stated price (1) -> passthrough case -> 1 % 4 != 0
        -> genuinely ambiguous, clear and flag."""
        items = [{"product_name": "games", "quantity": 4, "unit_price": Decimal("1.00"), "currency": "USD"}]
        result, _ = _apply_rule_based_price_hints("4 games $1 sale", items, "USD")
        self.assertIsNone(result[0]["unit_price"])
        self.assertTrue(result[0].get("price_ambiguous"))

    def test_extraction_ambiguous_falls_back_to_existing_behavior(self):
        """A message with no clear single currency-adjacent price token (no
        '$'/'R'/etc. marker at all) makes _extract_literal_stated_price
        return None — the heuristic should fall through to the pre-existing
        cents/divisibility logic unchanged, not skip processing entirely."""
        items = [{"product_name": "plates beef", "quantity": 7, "unit_price": Decimal("14.00"), "currency": "USD"}]
        result, _ = _apply_rule_based_price_hints("7 plates beef 14", items, "USD")
        # No currency marker -> extraction returns None -> falls back to
        # divisibility check on the raw LLM price (14 % 7 == 0 -> $2 each).
        self.assertEqual(result[0]["unit_price"], Decimal("2.00"))
        self.assertFalse(result[0].get("price_ambiguous"))
