"""Guard tests for placeholder consistency between locale files.

`t()` in webhook_handler formats a locale string with **kwargs supplied by the
caller. Callers pass one set of kwargs regardless of language, so a placeholder
that appears in a non-English locale but not in en.json raises KeyError at
runtime on the *English* path — the untranslated locale is what breaks.

The asymmetry these tests allow is deliberate: en.json uses {plural} on two
keys where Shona has no equivalent slot (Shona marks plurality in the noun-class
prefix, not a suffix). Extra kwargs are harmless — str.format ignores them — so
{plural} is documented as EN-only rather than forced into sn.json.

Run with:
    python manage.py test unit_tests.test_locale_placeholders
"""

import json
import re
from pathlib import Path

from django.test import SimpleTestCase

LOCALES_DIR = Path(__file__).parent.parent / "apps" / "whatsapp" / "locales"
REFERENCE_LANG = "en"

# Keys where a non-reference locale is allowed to omit a placeholder the
# reference locale uses. Additions here need a grammatical reason, not just a
# desire to make the test pass.
KNOWN_EN_ONLY_PLACEHOLDERS = {
    "report_card.sales_this_week": {"plural"},
    "summary.sale_word": {"plural"},
}

_PLACEHOLDER_RE = re.compile(r"\{(\w+)\}")


def _load(lang):
    return json.loads((LOCALES_DIR / f"{lang}.json").read_text(encoding="utf-8"))


def _flatten(obj, prefix=""):
    """Yield (dotted.key, {placeholder names}) for every string in the tree."""
    if isinstance(obj, dict):
        for key, value in obj.items():
            yield from _flatten(value, f"{prefix}.{key}" if prefix else key)
    elif isinstance(obj, str):
        yield prefix, set(_PLACEHOLDER_RE.findall(obj))


def _placeholders(lang):
    return dict(_flatten(_load(lang)))


def _other_langs():
    return sorted(
        p.stem for p in LOCALES_DIR.glob("*.json") if p.stem != REFERENCE_LANG
    )


class LocalePlaceholderConsistencyTests(SimpleTestCase):
    """No DB or API key needed — these read the locale files directly."""

    def test_locale_files_discovered(self):
        """Guards the tests below: they pass vacuously if globbing finds nothing."""
        self.assertIn("sn", _other_langs())

    def test_no_placeholder_absent_from_reference_locale(self):
        """A placeholder only in a translated locale breaks the English path.

        This is the failure the suite exists to catch: callers pass the kwargs
        en.json needs, so a {foo} added to sn.json alone raises KeyError when
        the English string is rendered.
        """
        reference = _placeholders(REFERENCE_LANG)
        for lang in _other_langs():
            for key, names in _placeholders(lang).items():
                extra = names - reference.get(key, set())
                self.assertEqual(
                    extra,
                    set(),
                    f"{lang}.json key '{key}' uses {sorted(extra)}, absent from "
                    f"{REFERENCE_LANG}.json. Callers pass one set of kwargs for "
                    f"all languages, so this raises KeyError on the "
                    f"{REFERENCE_LANG} path. Add it to {REFERENCE_LANG}.json and "
                    f"to every t() call for this key.",
                )

    def test_omissions_are_declared(self):
        """A locale may omit a reference placeholder only if declared above."""
        reference = _placeholders(REFERENCE_LANG)
        for lang in _other_langs():
            current = _placeholders(lang)
            for key, ref_names in reference.items():
                if key not in current:
                    continue
                missing = ref_names - current[key]
                allowed = KNOWN_EN_ONLY_PLACEHOLDERS.get(key, set())
                self.assertEqual(
                    missing - allowed,
                    set(),
                    f"{lang}.json key '{key}' omits {sorted(missing - allowed)} "
                    f"used by {REFERENCE_LANG}.json. If the omission is "
                    f"grammatical, declare it in KNOWN_EN_ONLY_PLACEHOLDERS with "
                    f"a reason; otherwise add the placeholder.",
                )

    def test_known_exceptions_are_still_real(self):
        """Stops the allowlist rotting once a string is reworded."""
        reference = _placeholders(REFERENCE_LANG)
        langs = _other_langs()
        for key, allowed in KNOWN_EN_ONLY_PLACEHOLDERS.items():
            self.assertIn(
                key,
                reference,
                f"KNOWN_EN_ONLY_PLACEHOLDERS lists '{key}', absent from "
                f"{REFERENCE_LANG}.json. Remove the stale entry.",
            )
            stale = allowed - reference[key]
            self.assertEqual(
                stale,
                set(),
                f"KNOWN_EN_ONLY_PLACEHOLDERS allows {sorted(stale)} on '{key}', "
                f"which {REFERENCE_LANG}.json no longer uses. Remove the entry.",
            )
            for lang in langs:
                current = _placeholders(lang)
                if key not in current:
                    continue
                unnecessary = allowed & current[key]
                self.assertEqual(
                    unnecessary,
                    set(),
                    f"KNOWN_EN_ONLY_PLACEHOLDERS allows {sorted(unnecessary)} on "
                    f"'{key}', but {lang}.json now uses it. Remove the entry.",
                )

    def test_every_key_exists_in_every_locale(self):
        """A key missing from a locale falls through to a KeyError in t()."""
        reference = _placeholders(REFERENCE_LANG)
        for lang in _other_langs():
            current = _placeholders(lang)
            self.assertEqual(
                set(reference) - set(current),
                set(),
                f"{lang}.json is missing keys present in {REFERENCE_LANG}.json.",
            )
            self.assertEqual(
                set(current) - set(reference),
                set(),
                f"{lang}.json has keys absent from {REFERENCE_LANG}.json.",
            )


class LocaleRenderingTests(SimpleTestCase):
    """The two #386 keys render correctly in both locales at the boundary."""

    def _render(self, lang, key, count):
        from apps.whatsapp.services.webhook_handler import t

        return t(key, lang=lang, count=count, plural="s" if count != 1 else "")

    def test_english_pluralises_on_the_boundary(self):
        self.assertEqual(self._render("en", "summary.sale_word", 1), "1 sale")
        self.assertEqual(self._render("en", "summary.sale_word", 2), "2 sales")
        self.assertEqual(self._render("en", "summary.sale_word", 0), "0 sales")

    def test_shona_renders_with_the_unused_plural_kwarg(self):
        """The extra kwarg is ignored rather than raising — why sn.json is fine."""
        for count in (0, 1, 2):
            rendered = self._render("sn", "summary.sale_word", count)
            self.assertEqual(rendered, f"{count} zvatengeswa")
            self.assertNotIn("{", rendered)

    def test_report_card_label_renders_in_both_locales(self):
        self.assertEqual(
            self._render("en", "report_card.sales_this_week", 1), "1 sale this week"
        )
        self.assertEqual(
            self._render("en", "report_card.sales_this_week", 3), "3 sales this week"
        )
        self.assertEqual(
            self._render("sn", "report_card.sales_this_week", 3),
            "3 zvatengeswa vhiki rino",
        )
