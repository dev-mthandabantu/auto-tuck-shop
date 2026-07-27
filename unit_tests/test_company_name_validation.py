"""Unit tests for company name validation in the waitlist flow."""

from django.test import TestCase

from apps.whatsapp.services.waitlist_handler import _is_valid_company_name


class CompanyNameValidationTest(TestCase):
    """Tests for _is_valid_company_name function — ensures greetings are rejected."""

    def test_valid_shop_names_accepted(self):
        """Real shop names should be accepted."""
        valid_names = [
            "Sweetwaters",
            "Mustaff Shop",
            "Pride Shop",
            "Tuck Shop",
            "General Store",
            "My Business",
            "Mama June's Store",
        ]
        for name in valid_names:
            self.assertTrue(
                _is_valid_company_name(name),
                f"'{name}' should be a valid company name"
            )

    def test_common_single_word_greetings_rejected(self):
        """Common single-word greetings should be rejected."""
        greetings = [
            "hello",
            "hi",
            "hie",
            "hey",
            "yo",
            "sup",
            "hi",
            "Hello",  # case-insensitive
            "HELLO",
            "Hi there",  # "hi" is first word
        ]
        for greeting in greetings:
            self.assertFalse(
                _is_valid_company_name(greeting),
                f"'{greeting}' should be rejected as a greeting"
            )

    def test_case_insensitive_greeting_rejection(self):
        """Greetings in any case should be rejected."""
        for greeting in ["HeLLo", "HI", "HeY", "Sup"]:
            self.assertFalse(
                _is_valid_company_name(greeting),
                f"'{greeting}' should be rejected (greetings are case-insensitive)"
            )

    def test_empty_and_whitespace_rejected(self):
        """Empty strings and whitespace should be rejected."""
        invalid = ["", "   ", "\t", "\n", "  \t\n  "]
        for text in invalid:
            self.assertFalse(
                _is_valid_company_name(text),
                f"'{repr(text)}' should be rejected"
            )

    def test_greeting_at_start_of_multiword_rejected(self):
        """Phrases starting with greetings should be rejected."""
        multiword_greetings = [
            "hello mate",
            "hi there",
            "hello my name is",
            "hey shop owner",
        ]
        for phrase in multiword_greetings:
            self.assertFalse(
                _is_valid_company_name(phrase),
                f"'{phrase}' should be rejected (starts with greeting)"
            )

    def test_greeting_not_at_start_accepted(self):
        """Phrases with greetings not at the start should be accepted."""
        valid_names = [
            "Say Hello Shop",
            "My Hi-Fi Store",
        ]
        for name in valid_names:
            self.assertTrue(
                _is_valid_company_name(name),
                f"'{name}' should be accepted (greeting not at start)"
            )

    def test_numbers_and_special_chars_accepted(self):
        """Shop names with numbers and special chars should be accepted."""
        valid_names = [
            "Shop #1",
            "Joe's Place",
            "Mike & Sons",
            "2Tuck",
            "Shop-2-Go",
        ]
        for name in valid_names:
            self.assertTrue(
                _is_valid_company_name(name),
                f"'{name}' should be accepted"
            )

    def test_whitespace_is_stripped(self):
        """Leading/trailing whitespace should not affect validation."""
        # "hello" with whitespace should still be rejected
        self.assertFalse(_is_valid_company_name("   hello   "))
        # Valid name with whitespace should still be accepted
        self.assertTrue(_is_valid_company_name("   Sweetwaters   "))

    def test_all_english_greetings_rejected(self):
        """All defined English greetings should be rejected."""
        english_greetings = [
            "hi", "hello", "hie", "hey", "greetings", "yo",
            "howdy", "sup", "wassup",
        ]
        for greeting in english_greetings:
            self.assertFalse(
                _is_valid_company_name(greeting),
                f"'{greeting}' should be rejected"
            )

    def test_real_shop_from_issue_accepted(self):
        """Real shop names from the codebase should be accepted."""
        real_shops = [
            "Sweetwaters",
            "Pride Shop",
            "General Store",
        ]
        for shop in real_shops:
            self.assertTrue(
                _is_valid_company_name(shop),
                f"Real shop name '{shop}' should be accepted"
            )

    def test_short_names_accepted(self):
        """Short names like "Mama", "Joe" should be accepted."""
        short_names = ["Joe", "Mama", "T", "Shop", "Corner Store"]
        for name in short_names:
            self.assertTrue(
                _is_valid_company_name(name),
                f"Short name '{name}' should be accepted"
            )
