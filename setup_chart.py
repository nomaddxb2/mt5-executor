from __future__ import annotations

import bisect
import io
import os
from datetime import datetime
from typing import Any, Dict, List, Optional

from PIL import Image, ImageDraw, ImageFont

W, H = 1000, 560
BG = (17, 21, 30)
PANEL = (22, 27, 38)
GRID = (39, 44, 56)
TEXT = (226, 230, 238)
MUTED = (132, 139, 153)
GREEN = (38, 166, 154)
RED = (239, 83, 80)
BLUE = (41, 121, 255)
AMBER = (255, 179, 0)
WHITE = (255, 255, 255)


def _font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    names = [
        "C:/Windows/Fonts/arialbd.ttf" if bold else "C:/Windows/Fonts/arial.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/system/fonts/Roboto-Bold.ttf" if bold else "/system/fonts/Roboto-Regular.ttf",
    ]
    for path in names:
        try:
            if os.path.exists(path):
                return ImageFont.truetype(path, size)
        except Exception:
            continue
    try:
        return ImageFont.load_default(size=size)
    except Exception:
        return ImageFont.load_default()


def _parse_time(value: Any) -> Optional[float]:
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
    except Exception:
        return None


def _price(value: Any, digits: int = 5) -> str:
    try:
        number = float(value)
    except Exception:
        return "—"
    if abs(number) >= 1000:
        return f"{number:,.2f}"
    if abs(number) >= 10:
        return f"{number:.3f}"
    return f"{number:.{digits}f}"


def _money(value: Any) -> str:
    try:
        number = float(value)
    except Exception:
        return "—"
    return f"{'+' if number >= 0 else '-'}${abs(number):,.2f}"


def _rr(trade: Dict[str, Any]) -> float:
    try:
        entry = float(trade.get("entry") or trade.get("openPrice") or 0)
        sl = float(trade.get("stopLoss") or trade.get("sl") or 0)
        tp = float(trade.get("takeProfit") or trade.get("tp") or 0)
        risk = abs(entry - sl)
        reward = abs(tp - entry)
        return reward / risk if risk > 1e-12 else 0.0
    except Exception:
        return 0.0


def _bar_time(bar: Dict[str, Any]) -> float:
    value = _parse_time(bar.get("time"))
    return value if value is not None else 0.0


def render_setup_png(bars: List[Dict[str, Any]], trade: Dict[str, Any], phase: str = "before") -> Optional[bytes]:
    if not bars:
        return None
    rows = sorted(
        [
            {
                "time": _bar_time(bar),
                "open": float(bar.get("open") or 0),
                "high": float(bar.get("high") or 0),
                "low": float(bar.get("low") or 0),
                "close": float(bar.get("close") or 0),
            }
            for bar in bars
            if bar.get("time") is not None
        ],
        key=lambda row: row["time"],
    )
    if len(rows) < 2:
        return None

    gaps = sorted(rows[i]["time"] - rows[i - 1]["time"] for i in range(1, len(rows)) if rows[i]["time"] > rows[i - 1]["time"])
    step = gaps[len(gaps) // 2] if gaps else 900.0
    entry = float(trade.get("entry") or trade.get("openPrice") or rows[-1]["close"])
    sl = float(trade.get("stopLoss") or trade.get("sl") or 0)
    tp = float(trade.get("takeProfit") or trade.get("tp") or 0)
    exit_price = float(trade.get("exit") or trade.get("closePrice") or 0)
    opened = _parse_time(trade.get("openedAt")) or rows[0]["time"]
    closed = _parse_time(trade.get("closedAt")) or rows[-1]["time"]
    if phase == "before":
        rows = [row for row in rows if row["time"] <= opened + step * 0.5] or rows
        closed = opened
    pnl = float(trade.get("pnl") or 0)
    r_value = float(trade.get("r") or 0)
    rr = _rr(trade)
    side = str(trade.get("side") or "buy").upper()
    symbol = str(trade.get("symbol") or "—")
    timeframe = str(trade.get("timeframe") or "M15").upper()
    model = str(trade.get("modelName") or trade.get("model") or "Arena")
    ticket = str(trade.get("ticket") or "")
    pattern = str(trade.get("pattern") or "setup")

    image = Image.new("RGB", (W, H), BG)
    draw = ImageDraw.Draw(image)
    left, right = 18, W - 132
    top, bottom = 82, H - 70

    values = [row["high"] for row in rows] + [row["low"] for row in rows] + [entry]
    if sl:
        values.append(sl)
    if tp:
        values.append(tp)
    if exit_price and phase == "after":
        values.append(exit_price)
    low = min(values)
    high = max(values)
    pad = max((high - low) * 0.08, abs(high) * 0.0002, 1e-9)
    low -= pad
    high += pad
    span = max(high - low, 1e-12)

    def y_price(price: float) -> float:
        return top + (high - price) / span * (bottom - top)

    times = [row["time"] for row in rows]
    start_time = times[0] - step * 0.5
    end_time = times[-1] + step * 0.5
    if phase == "before":
        end_time = max(end_time, opened + step * 22)
    else:
        end_time = max(end_time, closed + step * 7)

    def x_time(ts: float) -> float:
        if end_time <= start_time:
            return left
        return left + (ts - start_time) / (end_time - start_time) * (right - left)

    x_entry = x_time(opened)
    x_exit = x_time(closed)
    x_box_end = right if phase == "before" else max(x_entry + 6, min(right, x_exit))

    draw.rectangle([0, 0, W, H], fill=BG)
    draw.rectangle([left, top, right, bottom], fill=PANEL, outline=GRID)
    for i in range(1, 5):
        y = top + (bottom - top) * i / 5
        draw.line([left, y, right, y], fill=GRID, width=1)
        draw.text((right + 8, y - 6), _price(high - span * i / 5), fill=MUTED, font=_font(11))
    for i in range(1, 6):
        x = left + (right - left) * i / 6
        draw.line([x, top, x, bottom], fill=GRID, width=1)

    entry_y = y_price(entry)
    sl_y = y_price(sl) if sl else None
    tp_y = y_price(tp) if tp else None

    overlay = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    od = ImageDraw.Draw(overlay)
    if sl_y is not None:
        od.rectangle([x_entry, min(entry_y, sl_y), x_box_end, max(entry_y, sl_y)], fill=(239, 83, 80, 40), outline=(239, 83, 80, 120))
    if tp_y is not None:
        od.rectangle([x_entry, min(entry_y, tp_y), x_box_end, max(entry_y, tp_y)], fill=(38, 166, 154, 38), outline=(38, 166, 154, 120))
    image = Image.alpha_composite(image.convert("RGBA"), overlay).convert("RGB")
    draw = ImageDraw.Draw(image)

    bar_width = max(2.0, (right - left) * step / max(end_time - start_time, step) * 0.62)
    for row in rows:
        x = x_time(row["time"])
        color = GREEN if row["close"] >= row["open"] else RED
        draw.line([x, y_price(row["high"]), x, y_price(row["low"])], fill=color, width=1)
        y_open = y_price(row["open"])
        y_close = y_price(row["close"])
        draw.rectangle([x - bar_width / 2, min(y_open, y_close), x + bar_width / 2, max(y_open, y_close) + 1], fill=color)

    if sl_y is not None:
        draw.line([x_entry, sl_y, x_box_end, sl_y], fill=RED, width=1)
        draw.text((right - 8, sl_y - 15), "SL " + _price(sl), fill=RED, font=_font(10, True), anchor="ra")
    if tp_y is not None:
        draw.line([x_entry, tp_y, x_box_end, tp_y], fill=GREEN, width=1)
        draw.text((right - 8, tp_y + 3), "TP " + _price(tp), fill=GREEN, font=_font(10, True), anchor="ra")

    for x, price, color, label in [
        (x_entry, entry, BLUE, "ENTRY " + _price(entry)),
        (x_exit if phase == "after" else None, exit_price if phase == "after" else None, AMBER, "EXIT " + _price(exit_price)),
    ]:
        if x is None or not price:
            continue
        draw.line([x, top, x, bottom], fill=color, width=1)
        draw.text((x + 4, top + 5), label, fill=color, font=_font(10, True))

    if phase == "after" and exit_price:
        ex = x_exit
        ey = y_price(exit_price)
        draw.ellipse([ex - 5, ey - 5, ex + 5, ey + 5], outline=AMBER, width=2)

    label = "BEFORE" if phase == "before" else "AFTER"
    label_color = BLUE if phase == "before" else AMBER
    draw.rounded_rectangle([left, 18, left + 92, 48], radius=7, fill=label_color)
    draw.text((left + 17, 25), label, fill=WHITE, font=_font(15, True))
    draw.text((left + 108, 15), symbol, fill=TEXT, font=_font(24, True))
    draw.text((left + 109, 45), f"{side} · {timeframe} · {model}", fill=MUTED, font=_font(12))
    draw.text((W - 14, 18), f"#{ticket}", fill=MUTED, font=_font(11), anchor="ra")
    if phase == "after":
        result_color = GREEN if pnl > 0 else RED if pnl < 0 else MUTED
        result_text = "WIN" if pnl > 0 else "LOSS" if pnl < 0 else "FLAT"
        draw.text((W - 14, 40), f"{result_text} {_money(pnl)}", fill=result_color, font=_font(14, True), anchor="ra")
    else:
        draw.text((W - 14, 40), f"SETUP · R:R 1:{rr:.1f}" if rr else "SETUP", fill=BLUE, font=_font(12, True), anchor="ra")

    box_label = f"R:R 1:{rr:.1f}" if rr else "R:R —"
    draw.text((right - 8, bottom - 22), box_label, fill=GREEN if tp_y is not None else MUTED, font=_font(12, True), anchor="ra")
    draw.text((x_entry + 6, bottom - 24), "REWARD", fill=GREEN, font=_font(9, True))
    draw.text((x_entry + 6, max(top + 8, (sl_y or entry_y) + 5)), "RISK", fill=RED, font=_font(9, True))

    draw.line([left, H - 58, right, H - 58], fill=GRID, width=1)
    if phase == "after":
        draw.text((left, H - 50), f"Entry {_price(entry)}   SL {_price(sl)}   TP {_price(tp)}   Exit {_price(exit_price)}", fill=TEXT, font=_font(12))
        draw.text((left, H - 30), f"R:R 1:{rr:.1f}   Result {r_value:+.2f}R   {_money(pnl)}   {pattern}", fill=MUTED, font=_font(11))
    else:
        draw.text((left, H - 50), f"Entry {_price(entry)}   SL {_price(sl)}   TP {_price(tp)}", fill=TEXT, font=_font(12))
        draw.text((left, H - 30), f"Planned R:R 1:{rr:.1f}   {pattern}   {side}", fill=MUTED, font=_font(11))
    draw.text((W - 14, H - 24), "MT5 BRIDGE · ARENA", fill=(70, 76, 90), font=_font(9), anchor="ra")

    out = io.BytesIO()
    image.save(out, format="PNG", optimize=True)
    return out.getvalue()
