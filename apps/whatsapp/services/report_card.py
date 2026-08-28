"""Generate a WhatsApp-ready PNG stat card for weekly business reports."""

from __future__ import annotations

import datetime
import io
import logging
from decimal import Decimal
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from apps.whatsapp.services.business_reports import BusinessSnapshot
from apps.core.currencies import format_price

logger = logging.getLogger(__name__)

WIDTH = 1200
HEIGHT = 675

BG_COLOR = (13, 17, 28)
CARD_BG = (22, 30, 46)
ACCENT_COLOR = (74, 222, 128)
RED_COLOR = (248, 113, 113)
MUTED_COLOR = (100, 116, 139)
DIVIDER_COLOR = (38, 52, 72)
BAR_EMPTY_COLOR = (38, 52, 72)
WHITE = (255, 255, 255)
GOLD = (250, 204, 21)
BAR_COLOR = (55, 90, 160)
BAR_TODAY_COLOR = (74, 222, 128)

PADDING = 56
TOP_SELLER_MIN_QTY = 3

_FONTS_DIR = Path(__file__).parent.parent / "static" / "fonts"


def _load_font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    bundled = _FONTS_DIR / ("Inter-Bold.ttf" if bold else "Inter-Regular.ttf")
    if bundled.exists():
        try:
            return ImageFont.truetype(str(bundled), size)
        except Exception:
            pass
    system_candidates: list[Path] = []
    if bold:
        system_candidates = [
            Path("/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf"),
            Path("C:/Windows/Fonts/arialbd.ttf"),
        ]
    else:
        system_candidates = [
            Path("/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf"),
            Path("C:/Windows/Fonts/arial.ttf"),
        ]
    for path in system_candidates:
        if path.exists():
            try:
                return ImageFont.truetype(str(path), size)
            except Exception:
                continue
    return ImageFont.load_default()


def _tw(draw: ImageDraw.ImageDraw, text: str, font) -> int:
    bbox = draw.textbbox((0, 0), text, font=font)
    return bbox[2] - bbox[0]


def _th(draw: ImageDraw.ImageDraw, text: str, font) -> int:
    bbox = draw.textbbox((0, 0), text, font=font)
    return bbox[3] - bbox[1]


def _draw_bar_chart(
    draw: ImageDraw.ImageDraw,
    week_revenues: list[dict[str, Decimal]],
    report_date: datetime.date,
    x: int,
    y: int,
    w: int,
    h: int,
    font_tiny,
    font_small,
    lang: str,
) -> None:
    from apps.whatsapp.services.webhook_handler import t
    n_days = len(week_revenues)
    if n_days == 0:
        return

    # Ordered list of all currencies seen across the week (insertion order preserved)
    all_currencies: list[str] = list({cur: None for day_dict in week_revenues for cur in day_dict})
    n_cur = len(all_currencies)

    monday = report_date - datetime.timedelta(days=report_date.weekday())

    label_h = 28
    value_h = 24
    bar_area_h = h - label_h - value_h

    group_gap = 20
    bar_gap = 4
    group_w = (w - group_gap * (n_days - 1)) // n_days
    bar_w = max(4, (group_w - bar_gap * (n_cur - 1)) // n_cur)

    all_values = [amt for day_dict in week_revenues for amt in day_dict.values()]
    max_rev = max(all_values) if any(v > 0 for v in all_values) else Decimal("1")

    for i, day_dict in enumerate(week_revenues):
        is_today = (i == n_days - 1)
        group_x = x + i * (group_w + group_gap)

        for j, cur in enumerate(all_currencies):
            bx = group_x + j * (bar_w + bar_gap)
            amt = day_dict.get(cur, Decimal("0"))
            bar_color = BAR_TODAY_COLOR if is_today else BAR_COLOR

            if amt <= 0:
                draw.rounded_rectangle(
                    [bx, y + value_h, bx + bar_w, y + value_h + bar_area_h],
                    radius=4,
                    outline=BAR_EMPTY_COLOR,
                    width=2,
                )
            else:
                bar_h = max(6, int(bar_area_h * float(amt) / float(max_rev)))
                by = y + value_h + bar_area_h - bar_h
                draw.rounded_rectangle([bx, by, bx + bar_w, y + value_h + bar_area_h], radius=4, fill=bar_color)

                val_text = format_price(amt, cur)
                val_color = ACCENT_COLOR if is_today else MUTED_COLOR
                vw = _tw(draw, val_text, font_tiny)
                draw.text(
                    (bx + (bar_w - vw) // 2, by - 26),
                    val_text, font=font_tiny, fill=val_color,
                )

        # Day label centred under the whole group
        label_text = t("report_card.today", lang=lang) if is_today else str((monday + datetime.timedelta(days=i)).day)
        group_centre = group_x + group_w // 2
        lw = _tw(draw, label_text, font_tiny)
        label_color = WHITE if is_today else MUTED_COLOR
        draw.text(
            (group_centre - lw // 2, y + value_h + bar_area_h + 8),
            label_text, font=font_tiny, fill=label_color,
        )


def generate_stat_card(
    snapshot: BusinessSnapshot, comparison: dict, shop_name: str = "", lang: str | None = None
) -> bytes:
    from apps.whatsapp.services.webhook_handler import t, DEFAULT_LANGUAGE

    lang = lang or DEFAULT_LANGUAGE
    img = Image.new("RGB", (WIDTH, HEIGHT), color=BG_COLOR)
    draw = ImageDraw.Draw(img)

    font_tiny = _load_font(18)
    font_small = _load_font(22)
    font_medium = _load_font(28)
    font_hero = _load_font(80, bold=True)

    draw.rounded_rectangle(
        [PADDING // 2, PADDING // 2, WIDTH - PADDING // 2, HEIGHT - PADDING // 2],
        radius=20,
        fill=CARD_BG,
    )

    y = PADDING + 12

    # Header
    date_label = snapshot.report_date.strftime("%d %b %Y")
    display_name = (shop_name or t("report_card.your_shop", lang=lang)).upper()
    header = f"{display_name}  |  {date_label}"
    draw.text((PADDING, y), header, font=font_small, fill=MUTED_COLOR)
    y += 38
    draw.line([(PADDING, y), (WIDTH - PADDING, y)], fill=DIVIDER_COLOR, width=1)
    y += 24

    content_top = y

    col_gap = 48
    left_w = WIDTH // 2 - PADDING - col_gap // 2
    right_x = WIDTH // 2 + col_gap // 2
    right_w = WIDTH - right_x - PADDING

    # Weekly revenue hero
    week_currency_revenues: dict[str, Decimal] = comparison.get("week_currency_revenues", {})
    if not week_currency_revenues:
        week_currency_revenues = {snapshot.currency: snapshot.revenue}

    if len(week_currency_revenues) > 1:
        hero_line = " / ".join(format_price(amt, cur) for cur, amt in week_currency_revenues.items())
        if _tw(draw, hero_line, font_hero) <= left_w:
            draw.text((PADDING, y), hero_line, font=font_hero, fill=WHITE)
            y += font_hero.size + 16
        else:
            for cur, amt in week_currency_revenues.items():
                draw.text((PADDING, y), format_price(amt, cur), font=font_hero, fill=WHITE)
                y += font_hero.size + 4
            y += 8
    else:
        cur, amt = next(iter(week_currency_revenues.items()))
        draw.text((PADDING, y), format_price(amt, cur), font=font_hero, fill=WHITE)
        y += font_hero.size + 16

    # Sales count — weekly
    week_sales_count: int = comparison.get("week_sales_count", snapshot.sales_count)
    sales_label = t(
        "report_card.sales_this_week", lang=lang,
        count=week_sales_count, plural="s" if week_sales_count != 1 else "",
    )
    draw.text((PADDING, y), sales_label, font=font_small, fill=MUTED_COLOR)
    y += 36

    # Best day badge
    week_revenues: list[dict] = comparison.get("week_revenues", [])
    prior_days_had_sales = any(sum(d.values(), Decimal("0")) > 0 for d in week_revenues[:-1])
    if comparison.get("is_best_day_this_week") and prior_days_had_sales:
        best_day_label = comparison.get("best_day_label") or t("report_card.today", lang=lang)
        best_day_text = t("report_card.best_day_this_week", lang=lang, day=best_day_label)
        draw.text((PADDING, y), best_day_text, font=font_medium, fill=GOLD)
        y += 40

    y += 8
    draw.line([(PADDING, y), (PADDING + left_w, y)], fill=DIVIDER_COLOR, width=1)
    y += 18

    # Top sellers
    top = [(name, qty) for name, qty in snapshot.top_products if qty >= TOP_SELLER_MIN_QTY]
    if top:
        draw.text((PADDING, y), t("report_card.top_sellers", lang=lang), font=font_small, fill=MUTED_COLOR)
        y += 28
        for pname, qty in top[:3]:
            draw.text((PADDING, y), pname, font=font_medium, fill=WHITE)
            qty_text = f"x{qty}"
            qty_x = PADDING + left_w - _tw(draw, qty_text, font_medium)
            draw.text((qty_x, y), qty_text, font=font_medium, fill=MUTED_COLOR)
            y += 36

    # Right column: bar chart
    if week_revenues:
        chart_label = t("report_card.this_week", lang=lang)
        draw.text((right_x, content_top), chart_label, font=font_small, fill=MUTED_COLOR)
        chart_top = content_top + 32
        chart_h = HEIGHT - chart_top - PADDING - 8
        _draw_bar_chart(
            draw, week_revenues, snapshot.report_date,
            right_x, chart_top, right_w, chart_h,
            font_tiny, font_small, lang,
        )

    # Footer
    footer = "Auto Tuck Shop"
    fw = _tw(draw, footer, font_tiny)
    draw.text((WIDTH - PADDING - fw, HEIGHT - PADDING + 4), footer, font=font_tiny, fill=MUTED_COLOR)

    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()
