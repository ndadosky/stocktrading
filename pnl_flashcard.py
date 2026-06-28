"""Generate a compact P/L flash-card PNG from the PostgreSQL paper account."""

from __future__ import annotations

import argparse
from datetime import datetime
from pathlib import Path

from db import connect, init_schema
from scanner_config import WATCHLIST_EXPORT_DIR, ensure_directories


def _require_pillow():
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError as exc:
        raise SystemExit(
            "Pillow is required for PNG generation. Run with the bundled Codex "
            "Python or install pillow in the app Python environment."
        ) from exc
    return Image, ImageDraw, ImageFont


def _font(loader, size: int, bold: bool = False):
    paths = [
        "/System/Library/Fonts/Supplemental/Arial Bold.ttf" if bold else "/System/Library/Fonts/Supplemental/Arial.ttf",
        "/System/Library/Fonts/Helvetica.ttc",
    ]
    for path in paths:
        try:
            return loader.truetype(path, size)
        except Exception:
            pass
    return loader.load_default()


def _money(value: float) -> str:
    return f"-${abs(value):,.2f}" if value < 0 else f"${value:,.2f}"


def _pct(value: float) -> str:
    return f"{value:.2f}%"


def _load_rows(target_date: str | None) -> tuple[str, list[dict], float]:
    init_schema()
    with connect() as (_, cursor):
        if target_date is None:
            cursor.execute("SELECT max(report_date) FROM paper_performance")
            row = cursor.fetchone()
            target_date = row["max"] if row and row["max"] else datetime.now().astimezone().date().isoformat()
        cursor.execute(
            "SELECT * FROM paper_performance WHERE report_date = %s ORDER BY ticker",
            (target_date,),
        )
        rows = [dict(row) for row in cursor.fetchall()]
        cursor.execute(
            "SELECT COALESCE(SUM(amount), 0) AS total FROM account_events WHERE event_type = 'deposit'"
        )
        deposits = float(cursor.fetchone()["total"] or 0)
    return target_date, rows, deposits


def _draw_wrapped(draw, xy, text: str, font, fill, max_width: int, gap: int = 5) -> None:
    x, y = xy
    current = ""
    lines: list[str] = []
    for word in text.split():
        candidate = f"{current} {word}".strip()
        if draw.textlength(candidate, font=font) <= max_width or not current:
            current = candidate
        else:
            lines.append(current)
            current = word
    if current:
        lines.append(current)
    for line in lines:
        draw.text((x, y), line, font=font, fill=fill)
        y += font.size + gap


def build_flashcard(target_date: str | None = None) -> Path:
    Image, ImageDraw, ImageFont = _require_pillow()
    ensure_directories()
    report_date, rows, deposits = _load_rows(target_date)

    width, height = 1400, 900
    bg = (246, 248, 251)
    card = (255, 255, 255)
    ink = (20, 32, 51)
    muted = (101, 113, 131)
    line = (223, 231, 240)
    blue = (36, 87, 214)
    green = (22, 132, 74)
    red = (200, 60, 60)
    amber = (183, 121, 31)
    soft = (237, 242, 247)

    fonts = {
        "eyebrow": _font(ImageFont, 18, True),
        "h1": _font(ImageFont, 64, True),
        "value": _font(ImageFont, 112, True),
        "metric": _font(ImageFont, 36, True),
        "body": _font(ImageFont, 22),
        "body_b": _font(ImageFont, 22, True),
        "small": _font(ImageFont, 17),
        "small_b": _font(ImageFont, 17, True),
    }

    base = 25_000.0 + deposits
    total_cost = sum(float(row["cost"] or 0) for row in rows)
    total_market_value = sum(float(row["market_value"] or 0) for row in rows)
    total_pl = sum(float(row["p_l"] or 0) for row in rows)
    cash = base - total_cost + sum(float(row["realized_proceeds"] or 0) for row in rows)
    equity = cash + total_market_value
    open_rows = [row for row in rows if str(row["status"] or "").upper() == "OPEN"]
    heat = (
        sum(
            float(row["initial_risk"] or 0)
            * (float(row["remaining_shares"] or 0) / float(row["shares"] or 1))
            for row in open_rows
        )
        / base
        * 100
        if base
        else 0
    )
    return_on_bankroll = total_pl / base * 100 if base else 0
    return_on_deployed = total_pl / total_cost * 100 if total_cost else 0
    best = max(rows, key=lambda row: float(row["p_l_%"] or 0), default=None)
    worst = min(rows, key=lambda row: float(row["p_l_%"] or 0), default=None)
    winners = sum(1 for row in rows if float(row["p_l"] or 0) > 0)
    losers = sum(1 for row in rows if float(row["p_l"] or 0) < 0)
    flat = len(rows) - winners - losers
    accent = green if total_pl >= 0 else red

    image = Image.new("RGB", (width, height), bg)
    draw = ImageDraw.Draw(image)
    for x in range(0, width, 44):
        draw.line((x, 0, x, height), fill=(232, 238, 246), width=1)
    for y in range(0, height, 44):
        draw.line((0, y, width, y), fill=(232, 238, 246), width=1)

    def rounded(box, radius=28, fill=card, outline=line, stroke=2):
        draw.rounded_rectangle(box, radius=radius, fill=fill, outline=outline, width=stroke)

    rounded((50, 50, width - 50, height - 50), 34)
    rounded((88, 92, 820, 808), 28, (251, 253, 255))
    draw.text((124, 124), "P/L FLASH CARD", font=fonts["eyebrow"], fill=muted)
    draw.text((124, 176), "UP ON THE DAY" if total_pl >= 0 else "DOWN ON THE DAY", font=fonts["h1"], fill=ink)
    draw.text((124, 300), _money(total_pl), font=fonts["value"], fill=accent)
    draw.text(
        (130, 430),
        f"{_pct(return_on_bankroll)} of bankroll  |  {_pct(return_on_deployed)} on deployed capital",
        font=fonts["body_b"],
        fill=muted,
    )
    metrics = [
        ("EQUITY", _money(equity), ink),
        ("DEPLOYED", _money(total_cost), blue),
        ("CASH", _money(cash), ink),
        ("HEAT", _pct(heat), green),
    ]
    for index, (label, value, color) in enumerate(metrics):
        x = 124 + index * 165
        draw.text((x, 495), label, font=fonts["small_b"], fill=muted)
        draw.text((x, 528), value, font=fonts["body_b"], fill=color)

    note_fill = (255, 248, 238) if total_pl < 0 else (240, 252, 246)
    note_line = (246, 224, 180) if total_pl < 0 else (194, 230, 209)
    draw.rounded_rectangle((124, 650, 784, 764), radius=20, fill=note_fill, outline=note_line, width=2)
    _draw_wrapped(
        draw,
        (154, 674),
        "Open positions are marked to current prices. No trades are resolved yet, so this is unrealized paper P/L, not a final strategy result.",
        fonts["body"],
        ink,
        590,
    )

    rounded((852, 92, 1312, 330), 26)
    draw.text((884, 124), "POSITION MIX", font=fonts["eyebrow"], fill=muted)
    center_x, center_y, radius = 1030, 228, 72
    total = max(1, len(rows))
    start = -90
    for count, color in ((winners, green), (losers, red), (flat, amber)):
        if count <= 0:
            continue
        end = start + count / total * 360
        draw.pieslice((center_x - radius, center_y - radius, center_x + radius, center_y + radius), start, end, fill=color)
        start = end
    draw.ellipse((center_x - 44, center_y - 44, center_x + 44, center_y + 44), fill=card, outline=line, width=2)
    draw.text((center_x - draw.textlength(str(len(rows)), font=fonts["metric"]) / 2, center_y - 25), str(len(rows)), font=fonts["metric"], fill=ink)
    draw.text((center_x - draw.textlength("OPEN", font=fonts["small_b"]) / 2, center_y + 14), "OPEN", font=fonts["small_b"], fill=muted)
    draw.text((1130, 170), f"{winners} green", font=fonts["body_b"], fill=green)
    draw.text((1130, 212), f"{losers} red", font=fonts["body_b"], fill=red)
    draw.text((1130, 254), f"{flat} flat", font=fonts["body_b"], fill=amber)

    for top, title, row, color in ((360, "BEST CARD", best, green), (610, "WORST CARD", worst, red)):
        rounded((852, top, 1312, top + 198), 26)
        draw.text((884, top + 34), title, font=fonts["eyebrow"], fill=muted)
        if row is not None:
            draw.text((884, top + 86), str(row["ticker"] or ""), font=fonts["h1"], fill=ink)
            draw.text((1048, top + 96), str(row["sector"] or ""), font=fonts["body"], fill=muted)
            draw.text(
                (884, top + 148),
                f"{_money(float(row['p_l'] or 0))}  {_pct(float(row['p_l_%'] or 0))}",
                font=fonts["metric"],
                fill=color,
            )

    stamp = f"Generated from PostgreSQL {datetime.now().astimezone().strftime('%Y-%m-%d %H:%M %Z')}"
    draw.text((width - 78 - draw.textlength(stamp, font=fonts["small"]), height - 38), stamp, font=fonts["small"], fill=muted)

    output = WATCHLIST_EXPORT_DIR / f"pnl_flashcard_{report_date}.png"
    image.save(output)
    return output


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--date", help="Report date in YYYY-MM-DD format. Defaults to latest paper_performance date.")
    args = parser.parse_args()
    output = build_flashcard(args.date)
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
