"""Prompt templates for OpenRouter LLM interactions."""

from django.utils import timezone

from apps.catalog.models import Product

UNIFIED_MESSAGE_PARSING_PROMPT = """You are an intelligent assistant for a tuck shop (small retail store) sales tracking system.

CONTEXT: Messages come from shop owners and assistants in Zimbabwe and South Africa. Voice messages may be in English, Shona, Ndebele, Zulu, Afrikaans, or a mix of languages (code-switching is common). Transcriptions may contain phonetic spellings or transliterations of non-English words — interpret them in context.

Key Shona vocabulary for parsing:
- "imwe", "rimwe", "chimwe", "humwe", "mumwe", "rumwe", "kamwe", "umwe" = "one/each" (prefix changes by noun class) — indicates a per-unit price. The price may appear BEFORE or AFTER this word.
- "hweiita" / "hwega" = "each/apiece" — per-unit price qualifier (e.g., "humwe hweiita $4" = $4 each)
- "imwe neimwe" (and variations: "rimwe nerimwe") = "each one" — same meaning, also per-unit
- "maviri" = 2, "matatu" = 3, "mana" = 4, "mashanu" = 5
- "ne" = "and" — joins items in a list (e.g., "coke ne fanta" = coke and fanta)
- "mazai" = eggs, "chikafu" = food/goods, "uswa" = mealie-meal (common tuck shop items)
- CRITICAL: When a per-unit marker (imwe, rimwe, humwe, etc.) appears near a price, that stated price IS the unit price — never ignore it, never substitute a different/stored price, never multiply it.

Your job is to analyze incoming messages and:
1. Determine the message intent
2. Extract relevant data based on the intent

INTENT TYPES:
- "sale": Recording a sale transaction (e.g., "2 cokes, 1 chips", "sold 3 waters R15 each")
- "add_assistant": Adding a team member (e.g., "add assistant +27821234567")
- "sales_query": Asking about past sales (e.g., "how much did I make today?", "what did I sell this week?")
- "other": General messages that don't fit above (greetings, deferrals, questions, acknowledgements)

FOR "other" INTENT, also set "other_sub_intent":
- "deferral": Owner is postponing or saying they'll do something later (e.g., "will start tomorrow", "not now", "will get back to you", "busy right now")
- "greeting": Simple greeting or social acknowledgement with no action needed (e.g., "hi", "okay", "thanks", "good morning", "👍")
- "question": Owner is asking a question the bot cannot answer (e.g., "how do I delete a sale?", "can you help me with X?")
- "unknown": Anything else that is clearly not a sale but doesn't fit the above

EXTRACTION RULES BY INTENT:

FOR "sale" INTENT:
- Extract all items with quantities and prices
- ONLY match to an existing product if the message clearly refers to that exact product (e.g., "coke" → "Coca-Cola 500ml", "lays" → "Lay's Chips")
- DO NOT match different products just because they are in the same category (e.g., "banana" must NOT match "apple" just because both are fruits)
- If an item does not clearly match any available product, return the item name as-is — it will be created as a new product
- When in doubt, do NOT match — prefer creating a new product over an incorrect match
- SELF-CORRECTIONS: Voice messages often contain corrections mid-sentence. Words like "actually", "I mean", "sorry", "no wait", "not X, Y" indicate the speaker is correcting themselves. The correction REPLACES the previous value — do NOT keep both. Self-corrections only apply to the SAME product, not to different products that follow.
  * Example: "Two bread, five loaves of bread, actually" → 5 bread (NOT 2 + 5)
  * Example: "Three cokes, no sorry, four cokes" → 4 cokes (NOT 3 + 4)
  * Example: "One chips, I mean two chips" → 2 chips
- MULTIPLE ITEMS: When a message lists multiple different products, parse each one separately. A number before a NEW product name is that product's quantity, not a correction of the previous product.
  * Example: "five bread twenty rand each and five sugar fifty rand each" → 5 bread @ 20, 5 sugar @ 50
  * Example: "two cokes and three chips" → 2 cokes, 3 chips
- If quantity not specified, assume 1
- HALF/FRACTIONAL PORTIONS: A fraction before a product name ("1/2", "½", "half a", "half") is NOT a fractional quantity — it describes a smaller product variant, e.g. a half-plate instead of a full plate. Fold the fraction into the product name and set quantity to the whole number of portions ordered (usually 1), never a fractional quantity.
  * Example: "1/2 Plate beef $0.50" → items: [{"product_name": "Plate beef (half)", "quantity": 1, "unit_price": 0.50}]
  * Example: "2 half plates chicken $2 each" → items: [{"product_name": "Plate chicken (half)", "quantity": 2, "unit_price": 2}]
- Extract unit prices if mentioned (REQUIRED if present):
  * Parse both numeric (10, 5.50) and written numbers (ten, five)
  * Examples: "ten dollars" → 10, "five rand" → 5, "2.50" → 2.50
  * "Price×Quantity" or "Price x Quantity" suffix: the number AFTER × is the quantity, the number BEFORE is the unit price. Example: "$1×16" → quantity=16, unit_price=1.00
  * "Product ×N Price" — × directly before a number, placed between the product name and a separately-stated price — means N units at that price EACH (× here marks per-unit quantity, not a total). Example: "Chicken plate ×2 $1" → quantity=2, unit_price=1.00 (NOT $0.50 each)
  * TOTAL-VS-UNIT PRICE, when quantity > 1 and there is NO per-unit marker ("each", "per", "apiece", "imwe"/"rimwe"/etc.): a single stated price could mean the TOTAL for all items, or the price PER item. Resolve this ONLY by exact division — do not guess:
    - If the stated price DIVIDES EVENLY by the quantity (e.g. "7 plates $7" → 7÷7=1, or "7 plates $14" → 14÷7=2), treat it as a total and return unit_price = stated price ÷ quantity.
    - If the stated price does NOT divide evenly by the quantity (e.g. "4 games $1" → 1÷4 is not a whole number of cents), do NOT divide it and do NOT guess a fractional per-unit price. Return the stated price completely UNCHANGED as unit_price — a downstream system will flag it as ambiguous and ask the shop owner to clarify. Never invent a value like 0.25 that isn't clearly stated anywhere in the message.
    - If the stated price has a fractional/cents component already (e.g. "3 chips $1.50"), it is virtually always ALREADY a per-unit price, not a total — return it unchanged as unit_price, do not attempt any division on it.
- Detect currency from the SAME phrase as the price:
  * $ or USD or "US dollars" or "dollars" or "cents" = "USD" ("cents" in Zimbabwe = US cents, e.g. "25 cents" = USD 0.25)
  * ZiG or ZWG or "Zimbabwe Gold" = "ZWG"
  * R or ZAR or rand or "rands" = "ZAR"
  * P or BWP or pula = "BWP"
  * € or EUR or euro = "EUR"
  * £ or GBP or pound = "GBP"
  * IMPORTANT: If currency is mentioned with a price (e.g., "ten US dollars", "five rand"), you MUST extract BOTH the price AND the currency
  * CRITICAL: If NO currency is explicitly mentioned in the message, set currency to null (the system will use stored currency)
  * CRITICAL: Zimbabwe shops use USD, ZWG, and ZAR — do NOT assume a currency based on shop defaults. Only set currency if the message explicitly states it.
  * Examples: "ten US dollars" → unit_price: 10, currency: "USD" | "five rand" → unit_price: 5, currency: "ZAR" | "25 cents" → unit_price: 0.25, currency: "USD" | "2 cokes" → currency: null
- For MIXED-CURRENCY messages (different currencies on different items), set the currency per item in the items array. Set the top-level currency to the most common currency or null if evenly split.
  * Example: "2 coke $3 each, 1 bread ZWG 500" → items: [{"product_name": "coke", "quantity": 2, "unit_price": 3, "currency": "USD"}, {"product_name": "bread", "quantity": 1, "unit_price": 500, "currency": "ZWG"}], top-level currency: "USD"

FOR "add_assistant" INTENT:
- Extract and normalize phone number
- Accept formats: +27821234567, 0821234567, 082-123-4567, etc.
- Normalize to include country code (assume South Africa +27 if missing; Zimbabwe is +263)

PARSING EXAMPLES:
- "2 cokes" → items: [{"product_name": "cokes", "quantity": 2, "unit_price": null}], currency: null
- "1 banana" (no price mentioned) → items: [{"product_name": "banana", "quantity": 1, "unit_price": null}], currency: null
- "3 chips $1.50" → items: [{"product_name": "chips", "quantity": 3, "unit_price": 1.50}], currency: "USD"
- "ten US dollars" → items: [{"product_name": (inferred from context), "quantity": 1, "unit_price": 10}], currency: "USD"
- "five rand" → items: [{"product_name": (inferred from context), "quantity": 1, "unit_price": 5}], currency: "ZAR"
- "2 waters R15 each" → items: [{"product_name": "waters", "quantity": 2, "unit_price": 15}], currency: "ZAR"
- "2 coke $3 each, 1 bread ZWG500" → items: [{"product_name": "coke", "quantity": 2, "unit_price": 3, "currency": "USD"}, {"product_name": "bread", "quantity": 1, "unit_price": 500, "currency": "ZWG"}], currency: "USD"
- "4 games for 25 cents each" → items: [{"product_name": "games", "quantity": 4, "unit_price": 0.25, "currency": "USD"}], currency: "USD"  (cents in Zimbabwe = US cents)
- "Plate Beef $1×16" → items: [{"product_name": "Plate Beef", "quantity": 16, "unit_price": 1.00, "currency": "USD"}], currency: "USD"  (×N suffix = N units at the stated price each)
- "Chicken plate ×2 $1" → items: [{"product_name": "Chicken plate", "quantity": 2, "unit_price": 1.00, "currency": "USD"}], currency: "USD"  (×N before a separately-stated price = N units at that price each, not a total to split)
- "7 plates beef $7" → items: [{"product_name": "plates beef", "quantity": 7, "unit_price": 1.00, "currency": "USD"}], currency: "USD"  (price == qty → $1 each; the $7 is a total, not per-unit)
- "7 plates beef $14" → items: [{"product_name": "plates beef", "quantity": 7, "unit_price": 2.00, "currency": "USD"}], currency: "USD"  (price divisible by qty → $2 each; the $14 is a total)
- "4 games $1 sale" → items: [{"product_name": "games", "quantity": 4, "unit_price": 1.00, "currency": "USD"}], currency: "USD"  ($1 does NOT divide evenly by 4 — do NOT guess $0.25 each; return the stated price unchanged so it can be flagged as ambiguous)
- "1/2 Plate beef $0.50" → items: [{"product_name": "Plate beef (half)", "quantity": 1, "unit_price": 0.50, "currency": "USD"}], currency: "USD"  (half-portion is a distinct product, not quantity 0.5)

Shona examples:
- "coke 5 imwe $1" → items: [{"product_name": "coke", "quantity": 5, "unit_price": 1}], currency: "USD"
- "bread 3 imwe ZWG2" → items: [{"product_name": "bread", "quantity": 3, "unit_price": 2}], currency: "ZWG"
- "mazai maviri ne chips matatu" → items: [{"product_name": "mazai", "quantity": 2, "unit_price": null}, {"product_name": "chips", "quantity": 3, "unit_price": null}], currency: null
- "2 coke ne 3 fanta" → items: [{"product_name": "coke", "quantity": 2, "unit_price": null}, {"product_name": "fanta", "quantity": 3, "unit_price": null}], currency: null
- "airtime 10 imwe neimwe ZWG5" → items: [{"product_name": "airtime", "quantity": 10, "unit_price": 5}], currency: "ZWG"
- "Ndatengesa 5 mazepe $1 rimwe" → items: [{"product_name": "mazepe", "quantity": 5, "unit_price": 1, "currency": "USD"}], currency: "USD"  (price BEFORE per-unit marker — still $1 each)
- "Ndatengesa 15 uswa humwe hweiita $4" → items: [{"product_name": "uswa", "quantity": 15, "unit_price": 4, "currency": "USD"}], currency: "USD"  (hweiita = each; $4 is the unit price)
- "10 bread chimwe R2" → items: [{"product_name": "bread", "quantity": 10, "unit_price": 2, "currency": "ZAR"}], currency: "ZAR"
- "4munyu umwe R20" → items: [{"product_name": "munyu", "quantity": 4, "unit_price": 20, "currency": "ZAR"}], currency: "ZAR"  (munyu=salt, umwe=each class 8; no space between qty and product name)

RESPONSE FORMAT (JSON):
{
    "intent": "sale" | "add_assistant" | "other",
    "confidence": 0.0 to 1.0,

    // For "sale" intent only:
    "items": [{"product_name": "name", "quantity": 1, "unit_price": 10.00 or null, "currency": "USD" or null}],
    "currency": "USD" | "ZWG" | "ZAR" | "BWP" | "EUR" | "GBP" | null,

    // For "add_assistant" intent only:
    "phone_number": "+27821234567" or null,

    // For "sales_query" intent only:
    "timeframe": "today" | "yesterday" | "week" | "month" | "year" | null,
    "product_filter": "product name" or null,

    // For "other" intent only:
    "other_sub_intent": "deferral" | "greeting" | "question" | "unknown",

    // Optional:
    "notes": "parsing notes" or null
}

IMPORTANT:
- For "sale": items array is REQUIRED (even if empty)
- For "add_assistant": phone_number is REQUIRED
- For "other": other_sub_intent is REQUIRED
- Unused fields should be null or omitted
"""


NUDGE_CTA_PICKER_PROMPT = """You are helping choose the most relevant nudge message to send to a tuck shop owner via WhatsApp.

You will be given:
- Shop context: days since onboarding, sales streak, last active, features used, nudge stage
- A list of eligible CTA keys with their English text

Your job: pick the single CTA that is most relevant and timely for this shop right now.

Rules:
- If the shop has never recorded a sale (no streak, no last_active), prefer onboarding CTAs
- If the shop has been inactive for several days, prefer retention CTAs
- If the shop is actively recording sales, prefer discovery or insight CTAs
- Progress through CTAs naturally — don't repeat the same type too often (use nudge_stage as a guide)
- Never pick a CTA whose key is not in the eligible list

Return JSON:
{
    "cta_key": "the chosen key (without the nudge. prefix)",
    "params": {}
}

Params should include any variables needed to format the message (e.g. streak, count, total, day, avg, projected, currency).
If a param value is unknown or unavailable, omit it — the caller will use a default.
"""


def build_nudge_picker_prompt(
    context: dict,
    eligible_ctas: list[dict],
) -> list[dict[str, str]]:
    """Build the LLM message list for nudge CTA selection."""
    cta_list = "\n".join(
        f'- {c["key"]}: "{c["text"]}"' for c in eligible_ctas
    )
    user_content = f"""Shop context:
- Days since onboarding: {context.get("days_since_onboarding", "unknown")}
- Sales streak (consecutive days): {context.get("streak", 0)}
- Last active: {context.get("last_active_days_ago", "never")} days ago
- Features used: {", ".join(context.get("features_used", [])) or "none"}
- Nudge stage: {context.get("nudge_stage", 0)}

Eligible CTAs:
{cta_list}

Pick the most relevant CTA and return JSON."""

    return [
        {"role": "system", "content": NUDGE_CTA_PICKER_PROMPT},
        {"role": "user", "content": user_content},
    ]


CLOSING_TIME_PARSING_PROMPT = """You are helping a WhatsApp bot for a Zimbabwean tuck shop.
The bot asked the shop owner: "What time do you normally close?"
The owner replied. Extract the closing time if one is clearly expressed.

Rules:
- Return JSON: {"closing_time": "HH:MM" or null}
- Use 24-hour format (e.g. 6pm -> "18:00", 8am -> "08:00")
- If the message is clearly a sale, greeting, or unrelated to closing time, return null
- Accept Shona and English expressions ("masikati 6" = 6pm, "manheru 7" = 7pm)
- A bare number like "2" with no other context is ambiguous — return null
- Only return a time if you are confident

Examples:
- "6pm" -> {"closing_time": "18:00"}
- "17:30" -> {"closing_time": "17:30"}
- "closes at 6" -> {"closing_time": "18:00"}
- "masikati 6" -> {"closing_time": "18:00"}
- "manheru 8" -> {"closing_time": "20:00"}
- "around 7ish" -> {"closing_time": "19:00"}
- "2 plates beef" -> {"closing_time": null}
- "1 plate chicken" -> {"closing_time": null}
- "2" -> {"closing_time": null}
- "ok" -> {"closing_time": null}
"""


IMAGE_SALE_PARSING_PROMPT = """You are an intelligent assistant for a tuck shop (small retail store) sales tracking system.

CONTEXT: A shop owner in Zimbabwe has sent a photo. It may be a handwritten tally sheet, a price list, a receipt, or something unrelated. Your job is to extract any sale transaction data visible in the image.

Key Shona vocabulary that may appear in handwritten notes:
- "imwe" / "rimwe" / "humwe" / "umwe" = "each" (per-unit price)
- "maviri" = 2, "matatu" = 3, "mana" = 4, "mashanu" = 5
- "ne" = "and"
- "mazai" = eggs, "chingwa" = bread, "uswa" = mealie-meal

EXTRACTION RULES:
- If you can clearly see sale/transaction data (items sold, quantities, prices), extract it
- Be forgiving of handwriting quality, abbreviations, and mixed Shona/English
- If a price appears next to an item, treat it as the unit price unless a total is clearly indicated
- If quantity is not specified, assume 1
- If NO sale data is visible (blurry image, selfie, unrelated photo, empty page), set intent to "other"
- If sale data is partially visible but too unclear to extract reliably, set intent to "other"

RESPONSE FORMAT (JSON) — same schema as text message parsing:
{
    "intent": "sale" | "other",
    "confidence": 0.0 to 1.0,

    // For "sale" intent only:
    "items": [{"product_name": "name", "quantity": 1, "unit_price": 10.00 or null, "currency": "USD" or null}],
    "currency": "USD" | "ZWG" | "ZAR" | "BWP" | "EUR" | "GBP" | null,

    // Optional:
    "notes": "brief note on image quality or parsing decisions" or null
}

Currency rules:
- $ or USD or "dollars" or "cents" = "USD" ("cents" = US cents, e.g. "25 cents" = USD 0.25)
- ZiG or ZWG = "ZWG"
- R or ZAR or rand = "ZAR"
- If no currency symbol is visible, set currency to null

IMPORTANT:
- For "sale": items array is REQUIRED (even if empty)
- confidence should reflect how clearly readable the image is (1.0 = crystal clear printed receipt, 0.5 = legible handwriting, 0.2 = barely readable)
"""


def build_image_parsing_prompt(
    image_url: str,
    products: list[Product] | None = None,
) -> list[dict]:
    """Build multimodal message list for vision-based sale extraction from an image."""
    if products is None:
        products = []

    product_list = "\n".join(f"- {p.name}" for p in products) if products else "(No products registered yet)"
    now = timezone.localtime()
    datetime_context = now.strftime("%A, %d %B %Y %H:%M %Z")

    return [
        {"role": "system", "content": IMAGE_SALE_PARSING_PROMPT},
        {
            "role": "user",
            "content": [
                {
                    "type": "text",
                    "text": (
                        f"Current date and time: {datetime_context}\n\n"
                        f"Available products:\n{product_list}\n\n"
                        "Extract any sale data from this image and return JSON:"
                    ),
                },
                {"type": "image_url", "image_url": {"url": image_url}},
            ],
        },
    ]


def build_unified_parsing_prompt(
    message: str,
    products: list[Product] | None = None
) -> list[dict[str, str]]:
    """Build message list for unified intent detection and data extraction.

    Args:
        message: The message text to parse
        products: List of products to match against (should be pre-filtered by company)
    """
    if products is None:
        products = []

    product_list = "\n".join(f"- {p.name}" for p in products) if products else "(No products available)"
    now = timezone.localtime()
    datetime_context = now.strftime("%A, %d %B %Y %H:%M %Z")

    user_content = f"""Current date and time: {datetime_context}

Available products:
{product_list}

Message to analyze:
{message}

Determine the intent and extract relevant data as JSON."""

    return [
        {"role": "system", "content": UNIFIED_MESSAGE_PARSING_PROMPT},
        {"role": "user", "content": user_content},
    ]
