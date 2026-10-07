"""Cost-price flow (#409): ask what a product costs after a confirmed sale, read the
owner's answer, and save it on the product.

The risk this module exists to contain is mixing up a cost price with a selling
price. Three rules keep them apart:

- A reply is only read as a cost while a question is pending (it expires), and
  only when every part of it names a product we asked about.
- Anything else falls through to normal parsing and cancels the question, so
  the owner is never trapped by it ("2 coke $3" is still a sale).
- A cost is never saved silently above the selling price, and every saved cost
  is echoed back next to the selling price.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta
from decimal import Decimal, InvalidOperation

from django.db import close_old_connections
from django.db.models import F, Q
from django.utils import timezone

from apps.catalog.models import Product
from apps.core.currencies import CURRENCIES, format_price
from apps.core.models import Company, UserProfile
from apps.sales.models import Sale, SaleItem
from apps.sales.services import MAX_UNIT_PRICE, find_product_by_name
from apps.whatsapp.services.webhook_handler import (
    DEFAULT_LANGUAGE,
    _extract_phone_number,
    _get_profile_by_phone,
    _send_response,
    _send_response_with_buttons,
    db_sync_to_async,
    run_async,
    t,
)

logger = logging.getLogger(__name__)

# The question goes stale: a "bread 1" sent the next morning is more likely a
# new sale than an answer.
COST_QUESTION_TTL = timedelta(hours=4)
# A question sent as a template (#416) reaches an owner who hasn't been chatting, and who
# may answer hours later. A shorter expiry would turn their "Bread 1.00" into a sale.
TEMPLATE_QUESTION_TTL = timedelta(hours=24)
# After this many unanswered asks for one product, stop asking (don't nag).
MAX_COST_ASKS = 2
# Keep the question short enough to answer in one message.
MAX_PRODUCTS_PER_QUESTION = 5

_CURRENCY_BY_TOKEN = {
    "$": "USD", "usd": "USD", "dollar": "USD", "dollars": "USD",
    "zwg": "ZWG", "zig": "ZWG",
    "zar": "ZAR", "r": "ZAR", "rand": "ZAR",
}

# A comma between two digits is a decimal comma ("0,80"), not a separator.
_SEGMENT_SPLIT_RE = re.compile(r",(?!\d)|(?<!\d),|[;\n]+|\s+(?:and|ne)\s+", re.IGNORECASE)
# Optional currency symbol/word, the number, optional currency word after it.
_AMOUNT_RE = re.compile(
    r"(?P<pre>\$|\bR(?=\s*\d)|\bZiG\b|\bZWG\b|\bUSD\b|\bZAR\b)?\s*"
    r"(?P<num>\d+(?:[.,]\d{1,2})?)\s*"
    r"(?P<post>\busd\b|\bzwg\b|\bzig\b|\bzar\b|\brand\b|\bdollars?\b)?",
    re.IGNORECASE,
)
# Words that sit between a product name and its cost and carry no meaning.
_FILLER_RE = re.compile(r"\b(costs?|is|at|for|pa|ndinotenga|kwandiri)\b|[:=@\-]", re.IGNORECASE)
# "Skip" is the word the question shows. The rest are what owners are likely to say instead,
# including the Shona ones: siya / zvisiye (leave it), gare gare (later), handizivi (I don't know).
_SKIP_RE = re.compile(
    r"^\s*(skip|later|ignore|pass|no|not now|hapana|kwete|siya|zvisiye|gare gare|handizivi)\s*[.!]?\s*$",
    re.IGNORECASE,
)


def is_owner(user_profile) -> bool:
    """What a shop pays for stock is the owner's private information (#409): only
    the owner sets costs or sees profit. Same check as add/remove assistant."""
    return bool(user_profile) and user_profile.role == UserProfile.Role.OWNER


@dataclass
class CostEntry:
    product_id: int | None  # None for LLM entries that haven't been matched yet
    amount: Decimal
    currency: str | None  # None = not stated; falls back to the product's price currency


@dataclass
class CostReply:
    entries: list[CostEntry] = field(default_factory=list)
    skipped: bool = False


def _norm(name: str) -> str:
    return re.sub(r"\s+", " ", name.strip().lower())


def _match_key(name: str) -> str:
    """Normalise a product name the way a reply is normalised (filler words and
    punctuation like '-' removed), so "Coca-Cola" matches the reply "Coca-Cola 1.20"."""
    return _norm(_FILLER_RE.sub(" ", name))


def _name_variants(name: str) -> set[str]:
    n = _norm(name)
    variants = {n}
    if n.endswith("s") and len(n) > 3:
        variants.add(n[:-1])
    else:
        variants.add(n + "s")
    return variants


def _currency_from(pre: str | None, post: str | None) -> str | None:
    token = (pre or post or "").strip().lower()
    return _CURRENCY_BY_TOKEN.get(token)


def parse_cost_reply(
    text: str, pending: dict[int, str], allow_bare: bool = True, strict: bool = False
) -> CostReply | None:
    """Read a reply to the cost question without calling the LLM.

    `pending` maps product id -> name for the products we asked about. Returns
    a CostReply when the whole message is a skip or a list of "<product>
    <amount>" parts (the amount needs a currency or a decimal point, so "bread 2" stays a
    sale) that all name a pending product (a bare amount counts when
    exactly one product is pending and either `allow_bare` is set or it carries a currency like \"$9\"). Returns None for anything else, including
    a message where even one part can't be matched, so it is never half-read.

    `strict` is for a late answer (see `_question_is_late`): a named amount then needs a
    decimal point, exactly as the question's example shows ("Bread 1.00"). A currency alone
    ("bread $1") is too much like a sale by then.
    """
    if _SKIP_RE.match(text or ""):
        return CostReply(skipped=True)

    by_variant: dict[str, int] = {}
    for pid, name in pending.items():
        for v in _name_variants(_match_key(name)):
            by_variant[v] = pid
    # Longest first, so "coke 2l" is tried before "coke" when both are pending.
    variants = sorted(by_variant, key=len, reverse=True)

    entries: list[CostEntry] = []
    seen: set[int] = set()
    segments = [s for s in _SEGMENT_SPLIT_RE.split(text or "") if s and s.strip()]
    if not segments:
        return None

    for segment in segments:
        # Match a pending product's name at the start first, then read one amount from what
        # is left. A name can contain a number of its own ("Coke 2L", "Sugar 2kg"), so the
        # amount is never looked for inside it. A leading number ("1 bread") matches no name,
        # so it is a quantity, not a cost.
        key = _match_key(segment)
        name = next((v for v in variants if key == v or key.startswith(v + " ")), "")
        m = _AMOUNT_RE.fullmatch(key[len(name):].strip())
        if not m:
            return None
        if name:
            pid = by_variant[name]
        elif len(pending) == 1 and (allow_bare or _currency_from(m.group("pre"), m.group("post"))):
            pid = next(iter(pending))
        else:
            pid = None
        if pid is None or pid in seen:
            return None

        # A product followed by a plain whole number ("bread 2") is how an owner says they
        # sold 2 bread, not what it costs. A named cost needs a currency ($1, R2, 5 zwg) or
        # a decimal point (0.80, 1.00), which is also how the question asks for it. Anything
        # else goes to sale parsing, and that closes the question. A bare amount with one
        # product pending ("$1", "0.80") is not a sale shape, so it is unaffected.
        has_decimal = bool(re.search(r"[.,]", m.group("num")))
        if name and not (has_decimal or (not strict and _currency_from(m.group("pre"), m.group("post")))):
            return None
        try:
            amount = Decimal(m.group("num").replace(",", "."))
        except InvalidOperation:
            return None
        if amount > MAX_UNIT_PRICE:
            return None
        seen.add(pid)
        entries.append(CostEntry(pid, amount, _currency_from(m.group("pre"), m.group("post"))))

    return CostReply(entries=entries)


# ---------------------------------------------------------------------------
# Database helpers
# ---------------------------------------------------------------------------

@db_sync_to_async
def _get_pending_products(company_id: int) -> dict[int, str]:
    """Products the open cost question is about; {} if none or it has expired."""
    company = Company.objects.filter(id=company_id).only(
        "pending_cost_product_ids", "pending_cost_asked_at", "pending_cost_expires_at"
    ).first()
    if not company or not company.pending_cost_product_ids or not company.pending_cost_asked_at:
        return {}
    expires_at = company.pending_cost_expires_at or company.pending_cost_asked_at + COST_QUESTION_TTL
    if timezone.now() > expires_at:
        _clear_pending_sync(company_id)
        return {}
    products = Product.objects.filter(company_id=company_id, id__in=company.pending_cost_product_ids)
    return {p.id: p.name for p in products}


@db_sync_to_async
def _question_is_late(company_id: int) -> bool:
    """True once the open question is older than the usual expiry, which only a template
    question (24 hours) lives long enough to be. By then the owner has probably moved on,
    so a reply is read strictly: a mistake they can see (a sale draft they can fix) beats a
    silent one (a sale saved as a cost)."""
    asked_at = Company.objects.filter(id=company_id).values_list("pending_cost_asked_at", flat=True).first()
    return bool(asked_at) and timezone.now() - asked_at > COST_QUESTION_TTL


def _clear_pending_sync(company_id: int) -> None:
    Company.objects.filter(id=company_id).update(
        pending_cost_product_ids=[], pending_cost_asked_at=None, pending_cost_expires_at=None
    )


@db_sync_to_async
def clear_pending_cost_question(company_id: int) -> None:
    _clear_pending_sync(company_id)


def _default_currency(product: Product, company: Company) -> str:
    price = product.current_price_with_currency
    return (price[1] if price and price[1] else None) or company.currency


@dataclass
class _SaveOutcome:
    status: str  # "saved" | "above_price" | "unknown_product"
    name: str = ""
    product_id: int | None = None
    amount: Decimal | None = None
    currency: str = ""
    price: Decimal | None = None
    price_currency: str | None = None
    backfilled: int = 0
    previous_cost: Decimal | None = None  # the product's cost when we asked, to refuse a stale "Yes"


def _save_cost_sync(company: Company, product: Product, amount: Decimal, currency: str | None,
                    force: bool = False) -> _SaveOutcome:
    currency = currency or _default_currency(product, company)
    price = product.current_price_with_currency
    price_amount, price_currency = (price if price else (None, None))

    # A cost of 0 means "free", which is almost never what a typo or a stray "0" meant,
    # and it would show profit as the whole selling price. Ask before saving it.
    if not force and amount == 0:
        return _SaveOutcome(
            "zero_cost", product.name, product.id, amount, currency, price_amount, price_currency,
            previous_cost=product.cost,
        )
    if not force and price_amount is not None and price_currency == currency and amount >= price_amount:
        return _SaveOutcome(
            "above_price", product.name, product.id, amount, currency, price_amount, price_currency,
            previous_cost=product.cost,
        )

    Product.objects.filter(id=product.id).update(cost=amount, cost_currency=currency)
    # Fill in earlier sales of this product that were recorded before its cost
    # was known — but only today's, so a cost given months later isn't stamped
    # onto old sales it may not describe. Existing snapshots are never touched.
    day_start = timezone.make_aware(datetime.combine(timezone.localdate(), time.min))
    backfilled = SaleItem.objects.filter(
        product_id=product.id,
        unit_cost__isnull=True,
        unit_price__isnull=False,
        currency=currency,
        sale__status=Sale.Status.CONFIRMED,
        sale__sale_timestamp__gte=day_start,
    ).update(unit_cost=amount, cost_currency=currency)
    return _SaveOutcome(
        "saved", product.name, product.id, amount, currency, price_amount, price_currency, backfilled
    )


@db_sync_to_async
def _save_entries(company_id: int, entries: list[CostEntry], by_name: list[tuple[str, CostEntry]] | None = None):
    """Save each entry. Replies to the question carry a product id; cost updates
    the LLM picked up carry a product name that is matched against the shop's products."""
    company = Company.objects.get(id=company_id)
    outcomes: list[_SaveOutcome] = []
    for entry in entries:
        product = Product.objects.filter(id=entry.product_id, company=company).first()
        if not product:
            continue
        outcomes.append(_save_cost_sync(company, product, entry.amount, entry.currency))
    for name, entry in by_name or []:
        product = find_product_by_name(name, company=company)
        if not product:
            outcomes.append(_SaveOutcome("unknown_product", name))
            continue
        outcomes.append(_save_cost_sync(company, product, entry.amount, entry.currency))
    return outcomes


@db_sync_to_async
def _save_confirmed_cost(company_id: int, product_id: int, amount: Decimal, currency: str,
                         previous_cost: Decimal | None):
    company = Company.objects.get(id=company_id)
    product = Product.objects.filter(id=product_id, company=company).first()
    if not product:
        return None
    if product.cost != previous_cost:
        return _SaveOutcome("changed", product.name, product.id)
    return _save_cost_sync(company, product, amount, currency, force=True)


@db_sync_to_async
def _products_needing_cost(sale_id: int) -> list[Product]:
    """Distinct active products in this sale with no cost that we may still ask about."""
    seen: dict[int, Product] = {}
    for item in SaleItem.objects.filter(sale_id=sale_id).select_related("product"):
        p = item.product
        if p.active and p.cost is None and p.cost_ask_count < MAX_COST_ASKS:
            seen.setdefault(p.id, p)
    return list(seen.values())[:MAX_PRODUCTS_PER_QUESTION]


@db_sync_to_async
def _open_cost_question(company_id: int, products: list[Product], ttl: timedelta | None = None) -> None:
    """Open the question and count the ask. `ttl` overrides the default expiry."""
    ids = [p.id for p in products]
    now = timezone.now()
    Product.objects.filter(id__in=ids).update(cost_ask_count=F("cost_ask_count") + 1)
    Company.objects.filter(id=company_id).update(
        pending_cost_product_ids=ids, pending_cost_asked_at=now,
        pending_cost_expires_at=now + ttl if ttl else None,
    )


@db_sync_to_async
def _claim_cost_question(company_id: int, products: list[Product], ttl: timedelta | None) -> datetime | None:
    """Open the question only if none is open, in one conditional UPDATE, so two sales
    confirmed at the same moment (by two assistants) can't both open one. Counts the ask
    only when the claim succeeds. Returns the claim's asked_at, or None if one was open."""
    ids = [p.id for p in products]
    now = timezone.now()
    free = (
        Q(pending_cost_asked_at__isnull=True)
        | Q(pending_cost_expires_at__isnull=True, pending_cost_asked_at__lt=now - COST_QUESTION_TTL)
        | Q(pending_cost_expires_at__lt=now)
    )
    claimed = Company.objects.filter(free, id=company_id).update(
        pending_cost_product_ids=ids, pending_cost_asked_at=now,
        pending_cost_expires_at=now + ttl if ttl else None,
    )
    if not claimed:
        return None
    Product.objects.filter(id__in=ids).update(cost_ask_count=F("cost_ask_count") + 1)
    return now


@db_sync_to_async
def _shorten_cost_question(company_id: int, asked_at: datetime) -> None:
    """No template got through, only a normal message: back to the usual expiry."""
    Company.objects.filter(id=company_id, pending_cost_asked_at=asked_at).update(pending_cost_expires_at=None)


@db_sync_to_async
def _undo_cost_question(company_id: int, products: list[Product], asked_at: datetime) -> None:
    """Take back a question that reached nobody, so the ask isn't used up. Only clears the
    question this call opened, never one another sale opened since."""
    ids = [p.id for p in products]
    Product.objects.filter(id__in=ids, cost_ask_count__gt=0).update(cost_ask_count=F("cost_ask_count") - 1)
    Company.objects.filter(id=company_id, pending_cost_asked_at=asked_at).update(
        pending_cost_product_ids=[], pending_cost_asked_at=None, pending_cost_expires_at=None
    )


# ---------------------------------------------------------------------------
# Messages
# ---------------------------------------------------------------------------

def _signed_money(amount: Decimal, currency: str) -> str:
    text = format_price(abs(amount), currency)
    return f"-{text}" if amount < 0 else text


def _saved_line(o: _SaveOutcome, lang: str) -> str:
    """Confirm a saved cost. With a selling price in the same currency, also show
    the sale price, cost and profit per unit so the owner sees what it means."""
    cost = format_price(o.amount, o.currency)
    header = t("cost.saved_header", lang=lang, name=o.name, cost=cost)
    if o.price is None or o.price_currency != o.currency:
        return header
    detail = t(
        "cost.saved_detail", lang=lang,
        price=format_price(o.price, o.price_currency), cost=cost,
        profit=_signed_money(o.price - o.amount, o.currency),
    )
    return f"{header}\n\n{detail}"


async def _report_outcomes(sender: str, outcomes: list[_SaveOutcome], lang: str) -> None:
    saved = [o for o in outcomes if o.status == "saved"]
    if saved:
        body = "\n\n".join(_saved_line(o, lang) for o in saved)
        await _send_response(sender, f"{body}\n\n{t('cost.saved_footer', lang=lang)}")
    for o in outcomes:
        if o.status == "unknown_product":
            await _send_response(sender, t("cost.unknown_product", lang=lang, name=o.name))
        elif o.status in ("above_price", "zero_cost"):
            if o.status == "zero_cost":
                text = t("cost.zero_cost", lang=lang, name=o.name, cost=format_price(o.amount, o.currency))
            else:
                text = t("cost.above_price", lang=lang, name=o.name,
                         cost=format_price(o.amount, o.currency), price=format_price(o.price, o.price_currency))
            # The button remembers the cost the product had when we asked, so a tap on an old
            # prompt can't overwrite a newer cost.
            previous = "none" if o.previous_cost is None else str(o.previous_cost)
            await _send_response_with_buttons(
                sender,
                text,
                [
                    {"id": f"costyes_{o.product_id}_{o.currency}_{o.amount}_{previous}", "title": t("cost.btn_yes", lang=lang)},
                    {"id": "costno", "title": t("cost.btn_no", lang=lang)},
                ],
            )


# ---------------------------------------------------------------------------
# Entry points
# ---------------------------------------------------------------------------

async def open_cost_question_after_sale(company_id: int, sale_id: int) -> list[Product]:
    """Open the cost question for a just-confirmed sale. Returns the products it
    asks about — empty when every product already has a cost (or we've asked enough)."""
    products = await _products_needing_cost(sale_id)
    if products:
        await _open_cost_question(company_id, products)
    return products


def sale_confirmed_with_cost_question(products: list[Product], lang: str) -> str:
    """The 'sale recorded' message and the cost question as one message."""
    if len(products) == 1:
        return t("cost.ask_after_sale_one", lang=lang, name=products[0].name)
    names = "\n".join(f"• {p.name}" for p in products)
    example = ", ".join(f"{p.name} 1.00" for p in products[:2])
    return t("cost.ask_after_sale_many", lang=lang, names=names, example=example)


@db_sync_to_async
def _owners(company_id: int) -> list[tuple[str, str, bool]]:
    """[(phone, language, in_window)] for the shop's owners. `in_window` is True when the
    owner messaged the bot in the last 24 hours, so a free-form message can reach them.
    Outside that window WhatsApp only delivers approved templates."""
    from apps.whatsapp.services.business_reports import _within_service_window

    owners = UserProfile.objects.filter(
        company_id=company_id, role=UserProfile.Role.OWNER, user__is_active=True
    )
    return [(o.phone_number, o.language, _within_service_window(o.phone_number)) for o in owners]


COST_QUESTION_TEMPLATE = "ats_cost_question"


async def _send_cost_question_template(phone: str, products: list[Product], lang: str) -> bool:
    """The cost question as an approved template, for an owner outside the 24 hour window
    (#416). {{1}} is the product names, {{2}} an example reply. The example keeps its
    decimal point: a named amount is only read as a cost with a currency or a decimal."""
    from apps.whatsapp.services.nudge_service import _template_name_for_lang
    from apps.whatsapp.services.whatsapp_client import get_whatsapp_client

    components = [{"type": "body", "parameters": [
        {"type": "text", "text": ", ".join(p.name for p in products)},
        {"type": "text", "text": f"{products[0].name} 1.00"},
    ]}]
    return await get_whatsapp_client().send_template_message(
        phone, _template_name_for_lang(COST_QUESTION_TEMPLATE, lang), components=components
    )


async def ask_owner_after_assistant_sale(company_id: int, sale_id: int) -> bool:
    """An assistant recorded the sale, but the owner is the one who knows what products cost
    (and often keeps it from staff). So the cost question goes to the owner, never to the
    assistant: as a normal message inside WhatsApp's 24 hour window, and as the approved
    template outside it (#416). If no send is accepted, the question is taken back so the
    ask isn't used up. Returns True if a question was sent."""
    products = await _products_needing_cost(sale_id)
    if not products:
        return False
    # One open question at a time: don't pile a new one on the owner for every assistant sale.
    if await _get_pending_products(company_id):
        return False
    owners = await _owners(company_id)
    if not owners:
        return False
    # Claim it before sending, so a second sale confirmed during the send sees it and doesn't
    # ask again. A template owner may answer hours later, so their question lasts longer.
    any_template = any(not in_window for _, _, in_window in owners)
    asked_at = await _claim_cost_question(company_id, products, TEMPLATE_QUESTION_TTL if any_template else None)
    if asked_at is None:
        return False
    text_reached = template_reached = False
    for phone, lang, in_window in owners:
        if in_window:
            if await _send_response(phone, owner_cost_question(products, lang)) is not False:
                text_reached = True
            else:
                logger.warning("Cost question not accepted for owner of company %s", company_id)
        elif await _send_cost_question_template(phone, products, lang):
            template_reached = True
        else:
            logger.warning("Cost question template not accepted for owner of company %s", company_id)
    if not (text_reached or template_reached):
        await _undo_cost_question(company_id, products, asked_at)
        return False
    if any_template and not template_reached:
        await _shorten_cost_question(company_id, asked_at)
    return True


def owner_cost_question(products: list[Product], lang: str) -> str:
    """The cost question as it is sent to the owner when an assistant made the sale."""
    if len(products) == 1:
        return t("cost.ask_owner_one", lang=lang, name=products[0].name)
    names = "\n".join(f"• {p.name}" for p in products)
    example = ", ".join(f"{p.name} 1.00" for p in products[:2])
    return t("cost.ask_owner_many", lang=lang, names=names, example=example)


async def handle_pending_cost_reply(
    sender: str, company: Company, text: str, lang: str, user_profile=None, closing_prompt_open: bool = False
) -> bool:
    """If a cost question is open, try to read `text` as the answer.

    Returns True when the message was handled here. Returns False when it should
    carry on to normal parsing — the question is then closed, so a sale or any
    other message is never held hostage by it.
    """
    # The question is for the owner. An assistant's message is never read as the
    # answer, and it leaves the question open for the owner.
    if not is_owner(user_profile):
        return False

    pending = await _get_pending_products(company.id)
    if not pending:
        return False
    strict = await _question_is_late(company.id)

    # A bare number ("6") is also a plausible answer to an unanswered closing-time
    # prompt, so while one is open it must not be taken as a cost. Leave the cost
    # question open and let the message carry on.
    if closing_prompt_open and parse_cost_reply(text, pending, allow_bare=False, strict=strict) is None \
            and parse_cost_reply(text, pending, strict=strict) is not None:
        return False

    reply = parse_cost_reply(text, pending, strict=strict)
    if reply is None:
        await clear_pending_cost_question(company.id)
        return False

    await clear_pending_cost_question(company.id)
    if reply.skipped:
        await _send_response(sender, t("cost.skipped", lang=lang))
        return True
    outcomes = await _save_entries(company.id, reply.entries)
    await _report_outcomes(sender, outcomes, lang)
    return True


async def handle_set_cost_intent(sender: str, company: Company, result, lang: str, user_profile=None) -> None:
    """The LLM classified the message as a cost update ("bread costs $0.80 now")."""
    if not is_owner(user_profile):
        await _send_response(sender, t("cost.owner_only", lang=lang))
        return
    named: list[tuple[str, CostEntry]] = []
    for item in result.cost_items:
        if item.cost is None:
            await _send_response(sender, t("cost.amount_missing", lang=lang, name=item.product_name))
            continue
        named.append((item.product_name, CostEntry(None, item.cost, item.currency)))
    if not named:
        if not result.cost_items:
            await _send_response(sender, t("cost.not_understood", lang=lang))
        return
    await clear_pending_cost_question(company.id)
    outcomes = await _save_entries(company.id, [], by_name=named)
    await _report_outcomes(sender, outcomes, lang)


async def _process_cost_confirm_async(sender: str, button_id: str) -> None:
    profile = await _get_profile_by_phone(_extract_phone_number(sender))
    lang = profile.language if profile else DEFAULT_LANGUAGE
    if not profile or not profile.company_id:
        return
    if not is_owner(profile):
        await _send_response(sender, t("cost.owner_only", lang=lang))
        return

    if button_id == "costno":
        await _send_response(sender, t("cost.not_saved", lang=lang))
        return

    # costyes_{product_id}_{currency}_{amount}_{cost the product had when we asked, or "none"}
    try:
        _, product_id, currency, raw_amount, raw_previous = button_id.split("_", 4)
        amount = Decimal(raw_amount)
        if currency not in CURRENCIES or not amount.is_finite() or not 0 <= amount <= MAX_UNIT_PRICE:
            raise ValueError("cost out of range")
        previous = None if raw_previous == "none" else Decimal(raw_previous)
        if previous is not None and not previous.is_finite():
            raise ValueError("previous cost out of range")
        outcome = await _save_confirmed_cost(profile.company_id, int(product_id), amount, currency, previous)
    except (ValueError, InvalidOperation):
        logger.warning("Malformed cost confirm button id: %s", button_id)
        return
    if outcome and outcome.status == "changed":
        await _send_response(sender, t("cost.changed", lang=lang, name=outcome.name))
    elif outcome:
        await _send_response(sender, _saved_line(outcome, lang))


def handle_cost_confirm_button(sender: str, button_id: str) -> None:
    try:
        close_old_connections()
        run_async(_process_cost_confirm_async(sender, button_id))
    except Exception as e:
        logger.exception(f"Error handling cost confirm {button_id}: {e}")
