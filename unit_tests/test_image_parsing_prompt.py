"""Unit tests for build_image_parsing_prompt (services/openrouter/prompts.py).

#294 regression: an image caption must be embedded in the vision prompt sent
to the LLM, not silently dropped.
"""

from django.test import TestCase

from services.openrouter.prompts import build_image_parsing_prompt


class ImageParsingPromptTest(TestCase):

    def test_caption_included_in_prompt_text(self):
        messages = build_image_parsing_prompt(
            "https://cdn.example.com/img.jpg",
            products=[],
            caption="3 pen @ 2usd each",
        )
        text_block = messages[1]["content"][0]["text"]
        self.assertIn("3 pen @ 2usd each", text_block)

    def test_no_caption_omits_caption_line(self):
        messages = build_image_parsing_prompt(
            "https://cdn.example.com/img.jpg",
            products=[],
        )
        text_block = messages[1]["content"][0]["text"]
        self.assertNotIn("Caption sent with this image", text_block)

    def test_image_url_still_included(self):
        messages = build_image_parsing_prompt(
            "https://cdn.example.com/img.jpg",
            products=[],
            caption="sold 3 cokes",
        )
        image_block = messages[1]["content"][1]
        self.assertEqual(image_block["image_url"]["url"], "https://cdn.example.com/img.jpg")
