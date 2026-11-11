"""Autonomous, self-adapting MT5 trading engine.

Pure-Python indicators + signal ensemble + risk manager + online learning.
The worker feeds it candles and executes its decisions; every decision carries
a human-readable reason that the terminal streams live.
"""

from __future__ import annotations

import json
import math
import os
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

MAGIC = int(os.environ.get("MT5_AI_MAGIC", "20261109"))

DEFAULT_WEIGHTS = {"trend": 0.35, "momentum": 0.25, "breakout": 0.25, "meanrev": 0.15}

DEFAULT_PARAMS = {
    "atrSl": 1.8,
    "atrTp": 3.2,
    "trailAt": 1.2,
    "trailDistance": 1.2,
    "threshold": 0.15,
    "minConfidence": 0.15,
    "riskPct": 0.5,
    "maxPositions": 3,
    "maxDailyLossPct": 3.0,
    "cooldownSec": 180,
    "timeStopHours": 24,
    "maxSpreadAtr": 0.35,
}

ZERO_STATS = {
    "trades": 0,
    "wins": 0,
    "losses": 0,
    "winRate": 0,
    "profitFactor": 0,
    "expectancy": 0,
    "maxDrawdownPct": 0,
    "avgR": 0,
}


def _iso(ts: Optional[float] = None) -> str:
    return datetime.fromtimestamp(ts if ts is not None else time.time(), tz=timezone.utc).isoformat().replace("+00:00", "Z")


def _num(value: Any, digits: int = 5) -> Any:
    try:
        return round(float(value), digits)
    except Exception:
        return value


def _clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


def _last(seq: List[float], default: float = 0.0) -> float:
    return float(seq[-1]) if seq else default


def _base(symbol: str) -> str:
    s = str(symbol or "").upper()
    for suffix in (".CENT", "CENT", ".RAW", ".M", ".Z", ".C", "M", "RAW", "Z"):
        if s.endswith(suffix) and len(s) > len(suffix) + 2:
            return s[: -len(suffix)]
    if s.endswith("C") and len(s) > 3 and s[:-1].endswith(("USD", "EUR", "GBP", "JPY", "AUD", "NZD", "CAD", "CHF", "XAU", "XAG", "BTC", "ETH")):
        return s[:-1]
    return s


def flip_lot(step: int, base: float = 0.01) -> float:
    return round(float(base) * (2 ** max(0, int(step))), 2)


def ensure_flip_sequence(fr: Dict[str, Any], up_to: int) -> List[float]:
    seq = [float(x) for x in (fr.get("sequence") or [])]
    base = float(fr.get("baseLot") or 0.01)
    if not seq:
        seq = [flip_lot(0, base)]
    while len(seq) <= int(up_to):
        seq.append(flip_lot(len(seq), base))
    fr["sequence"] = seq
    return seq


def flip_required_lot(target_usd: float, width: float, tick_value: float, tick_size: float) -> float:
    vpp = (float(tick_value) / float(tick_size)) if tick_size else 0.0
    return (float(target_usd) / (float(width) * vpp)) if width > 0 and vpp > 0 else 0.0


def update_flip_pair(fr: Dict[str, Any], seq: List[float], symbol: str, pnl: float, side: str = "") -> None:
    base = _base(symbol)
    key = "%s:%s" % (base, str(side or "").upper()) if side else base
    pairs = fr.setdefault("pairs", {})
    pair = pairs.setdefault(key, {"step": 0, "lot": seq[0], "deficit": 0.0, "missedProfit": float(fr.get("missedProfit") or 0.0)})
    if pnl < 0:
        pair["deficit"] = round(max(0.0, float(pair.get("deficit") or 0) + abs(pnl) + float(pair.get("missedProfit") or fr.get("missedProfit") or 0.0)), 2)
        pair["step"] = max(0, int(pair.get("step") or 0) + 1)
    elif pnl > 0:
        pair["step"] = 0
        pair["deficit"] = 0.0
    step = max(0, int(pair.get("step") or 0))
    while len(seq) <= step:
        seq.append(flip_lot(len(seq), float(fr.get("baseLot") or 0.01)))
    pair["lot"] = float(seq[step])
    pairs[key] = pair
    fr["sequence"] = seq
    fr["lot"] = max(float(p.get("lot") or seq[0]) for p in pairs.values())
    fr["step"] = max(int(p.get("step") or 0) for p in pairs.values())
    fr["deficit"] = round(sum(float(p.get("deficit") or 0) for p in pairs.values()), 2)


# ---------------------------------------------------------------- indicators
def sma(values: List[float], period: int) -> List[float]:
    out: List[float] = []
    if not values or period <= 0:
        return out
    running = 0.0
    for i, v in enumerate(values):
        running += float(v)
        if i >= period:
            running -= float(values[i - period])
        out.append(running / min(i + 1, period))
    return out


def ema(values: List[float], period: int) -> List[float]:
    if not values:
        return []
    period = max(1, int(period))
    k = 2.0 / (period + 1.0)
    out = [float(values[0])]
    for v in values[1:]:
        out.append(float(v) * k + out[-1] * (1 - k))
    return out


def rsi(values: List[float], period: int = 14) -> List[float]:
    if len(values) < 2:
        return [50.0] * len(values)
    period = max(2, int(period))
    gains = 0.0
    losses = 0.0
    out: List[float] = [50.0]
    for i in range(1, len(values)):
        diff = float(values[i]) - float(values[i - 1])
        gain = max(diff, 0.0)
        loss = max(-diff, 0.0)
        if i <= period:
            gains += gain
            losses += loss
            avg_gain = gains / i
            avg_loss = losses / i
        else:
            avg_gain = (avg_gain * (period - 1) + gain) / period
            avg_loss = (avg_loss * (period - 1) + loss) / period
        rs = avg_gain / avg_loss if avg_loss > 1e-12 else 100.0
        out.append(100.0 - 100.0 / (1.0 + rs))
    return out


def atr(highs: List[float], lows: List[float], closes: List[float], period: int = 14) -> List[float]:
    n = min(len(highs), len(lows), len(closes))
    if n == 0:
        return []
    period = max(2, int(period))
    trs: List[float] = []
    for i in range(n):
        if i == 0:
            trs.append(float(highs[i]) - float(lows[i]))
        else:
            trs.append(max(
                float(highs[i]) - float(lows[i]),
                abs(float(highs[i]) - float(closes[i - 1])),
                abs(float(lows[i]) - float(closes[i - 1])),
            ))
    out: List[float] = []
    running = 0.0
    for i, tr in enumerate(trs):
        if i < period:
            running += tr
            out.append(running / (i + 1))
        else:
            prev = out[-1]
            out.append((prev * (period - 1) + tr) / period)
    return out


def macd(values: List[float], fast: int = 12, slow: int = 26, signal: int = 9):
    if not values:
        return [], [], []
    f = ema(values, fast)
    s = ema(values, slow)
    line = [a - b for a, b in zip(f, s)]
    sig = ema(line, signal)
    hist = [a - b for a, b in zip(line, sig)]
    return line, sig, hist


def bollinger(values: List[float], period: int = 20, mult: float = 2.0):
    if not values:
        return [], [], []
    period = max(2, int(period))
    mid: List[float] = []
    upper: List[float] = []
    lower: List[float] = []
    for i in range(len(values)):
        window = values[max(0, i - period + 1): i + 1]
        m = sum(window) / len(window)
        var = sum((v - m) ** 2 for v in window) / len(window)
        sd = math.sqrt(var)
        mid.append(m)
        upper.append(m + mult * sd)
        lower.append(m - mult * sd)
    return mid, upper, lower


def adx(highs: List[float], lows: List[float], closes: List[float], period: int = 14) -> List[float]:
    n = min(len(highs), len(lows), len(closes))
    if n < 2:
        return [0.0] * n
    period = max(2, int(period))
    plus_dm: List[float] = [0.0]
    minus_dm: List[float] = [0.0]
    trs: List[float] = [float(highs[0]) - float(lows[0])]
    for i in range(1, n):
        up = float(highs[i]) - float(highs[i - 1])
        down = float(lows[i - 1]) - float(lows[i])
        plus_dm.append(up if up > down and up > 0 else 0.0)
        minus_dm.append(down if down > up and down > 0 else 0.0)
        trs.append(max(
            float(highs[i]) - float(lows[i]),
            abs(float(highs[i]) - float(closes[i - 1])),
            abs(float(lows[i]) - float(closes[i - 1])),
        ))
    dx: List[float] = []
    sp = 0.0
    sm = 0.0
    st = 0.0
    for i in range(n):
        if i < period:
            sp += plus_dm[i]
            sm += minus_dm[i]
            st += trs[i]
        else:
            sp = sp - sp / period + plus_dm[i]
            sm = sm - sm / period + minus_dm[i]
            st = st - st / period + trs[i]
        if st <= 1e-12:
            dx.append(0.0)
            continue
        pdi = 100.0 * sp / st
        mdi = 100.0 * sm / st
        denom = pdi + mdi
        dx.append(100.0 * abs(pdi - mdi) / denom if denom > 1e-12 else 0.0)
    out: List[float] = []
    running = 0.0
    for i, d in enumerate(dx):
        if i < period:
            running += d
            out.append(running / (i + 1))
        else:
            out.append((out[-1] * (period - 1) + d) / period)
    return out


def _digits(price: float) -> int:
    a = abs(float(price))
    if a >= 1000:
        return 2
    if a >= 100:
        return 3
    if a >= 10:
        return 3
    return 5


# ---------------------------------------------------------------- analysis
def _closed(candles: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [c for c in (candles or []) if c]


def detect_patterns(m15: List[Dict[str, Any]], m5: List[Dict[str, Any]], atr_v: float,
                    adx_v: float, ema_fast: List[float], ema_slow: List[float],
                    rsi_v: float) -> List[Dict[str, Any]]:
    bars = _closed(m15)
    if len(bars) < 40 or atr_v <= 0:
        return []
    last = bars[-2]
    o = float(last.get("open") or 0); h = float(last.get("high") or 0)
    l = float(last.get("low") or 0); c = float(last.get("close") or 0)
    body = abs(c - o)
    rng = max(h - l, atr_v * 1e-6)
    upper_wick = h - max(o, c)
    lower_wick = min(o, c) - l
    look = 20
    prior = bars[-look - 2:-2]
    if len(prior) < 10:
        return []
    prior_high = max(float(x["high"]) for x in prior)
    prior_low = min(float(x["low"]) for x in prior)
    vols = [float(x.get("tickVolume") or 0) for x in bars[-21:-1]]
    avg_vol = sum(vols) / len(vols) if vols else 0.0
    last_vol = float(last.get("tickVolume") or 0)
    vol_ratio = (last_vol / avg_vol) if avg_vol > 0 else 1.0
    trend_up = len(ema_fast) > 1 and len(ema_slow) > 1 and ema_fast[-1] > ema_slow[-1]
    trend_down = len(ema_fast) > 1 and len(ema_slow) > 1 and ema_fast[-1] < ema_slow[-1]
    patterns: List[Dict[str, Any]] = []

    if h > prior_high and c < prior_high and upper_wick >= atr_v * 0.20:
        score = _clamp(0.45 + upper_wick / atr_v * 0.25 + body / atr_v * 0.10 + (0.08 if trend_down else 0.0), 0.0, 1.0)
        patterns.append({"name": "liquidity_sweep", "side": "sell", "score": round(score, 3),
                         "reason": "Swept 20-bar high %.5f and closed back below it (wick %.2f ATR)." % (prior_high, upper_wick / atr_v)})
    if l < prior_low and c > prior_low and lower_wick >= atr_v * 0.20:
        score = _clamp(0.45 + lower_wick / atr_v * 0.25 + body / atr_v * 0.10 + (0.08 if trend_up else 0.0), 0.0, 1.0)
        patterns.append({"name": "liquidity_sweep", "side": "buy", "score": round(score, 3),
                         "reason": "Swept 20-bar low %.5f and closed back above it (wick %.2f ATR)." % (prior_low, lower_wick / atr_v)})

    if c > prior_high and trend_up and adx_v >= 18 and vol_ratio >= 1.05:
        score = _clamp(0.45 + (c - prior_high) / atr_v * 0.30 + min(0.15, (vol_ratio - 1.0) * 0.15), 0.0, 1.0)
        patterns.append({"name": "trend_breakout", "side": "buy", "score": round(score, 3),
                         "reason": "Closed above 20-bar high %.5f with ADX %.1f and volume %.2fx." % (prior_high, adx_v, vol_ratio)})
    if c < prior_low and trend_down and adx_v >= 18 and vol_ratio >= 1.05:
        score = _clamp(0.45 + (prior_low - c) / atr_v * 0.30 + min(0.15, (vol_ratio - 1.0) * 0.15), 0.0, 1.0)
        patterns.append({"name": "trend_breakout", "side": "sell", "score": round(score, 3),
                         "reason": "Closed below 20-bar low %.5f with ADX %.1f and volume %.2fx." % (prior_low, adx_v, vol_ratio)})

    if trend_up and l <= ema_fast[-1] + atr_v * 0.20 and c > ema_fast[-1] and c > o:
        score = _clamp(0.42 + (c - ema_fast[-1]) / atr_v * 0.20 + (0.10 if vol_ratio >= 1.0 else 0.0), 0.0, 1.0)
        patterns.append({"name": "trend_pullback", "side": "buy", "score": round(score, 3),
                         "reason": "Pulled into EMA20 %.5f and closed bullish above it." % ema_fast[-1]})
    if trend_down and h >= ema_fast[-1] - atr_v * 0.20 and c < ema_fast[-1] and c < o:
        score = _clamp(0.42 + (ema_fast[-1] - c) / atr_v * 0.20 + (0.10 if vol_ratio >= 1.0 else 0.0), 0.0, 1.0)
        patterns.append({"name": "trend_pullback", "side": "sell", "score": round(score, 3),
                         "reason": "Pulled into EMA20 %.5f and closed bearish below it." % ema_fast[-1]})

    try:
        hour = datetime.fromisoformat(str(last.get("time") or "").replace("Z", "+00:00")).hour
    except Exception:
        hour = -1
    session = (7 <= hour <= 11) or (12 <= hour <= 16)
    if session and len(bars) >= 12:
        opening = bars[-10:-2]
        opening_high = max(float(x["high"]) for x in opening)
        opening_low = min(float(x["low"]) for x in opening)
        if c > opening_high and trend_up and vol_ratio >= 1.05:
            score = _clamp(0.45 + (c - opening_high) / atr_v * 0.25 + min(0.12, (vol_ratio - 1.0) * 0.12), 0.0, 1.0)
            patterns.append({"name": "session_breakout", "side": "buy", "score": round(score, 3),
                             "reason": "London/NY session break above 8-bar range %.5f with volume %.2fx." % (opening_high, vol_ratio)})
        if c < opening_low and trend_down and vol_ratio >= 1.05:
            score = _clamp(0.45 + (opening_low - c) / atr_v * 0.25 + min(0.12, (vol_ratio - 1.0) * 0.12), 0.0, 1.0)
            patterns.append({"name": "session_breakout", "side": "sell", "score": round(score, 3),
                             "reason": "London/NY session break below 8-bar range %.5f with volume %.2fx." % (opening_low, vol_ratio)})

    if body >= atr_v * 1.15 and vol_ratio >= 1.45:
        side_m = "buy" if c > o else "sell"
        score = _clamp(0.50 + min(0.25, (body / atr_v - 1.15) * 0.20) + min(0.12, (vol_ratio - 1.45) * 0.10), 0.0, 1.0)
        patterns.append({"name": "momentum_ignition", "side": side_m, "score": round(score, 3),
                         "reason": "Displacement candle %.2f ATR on %.2fx volume." % (body / atr_v, vol_ratio)})

    if adx_v < 20:
        if rsi_v >= 65 and c > ema_fast[-1]:
            score = _clamp(0.35 + (rsi_v - 65) / 35.0 + (0.10 if vol_ratio < 1.0 else 0.0), 0.0, 1.0)
            patterns.append({"name": "range_fade", "side": "sell", "score": round(score, 3),
                             "reason": "Range regime: RSI %.1f stretched above EMA20." % rsi_v})
        elif rsi_v <= 35 and c < ema_fast[-1]:
            score = _clamp(0.35 + (35 - rsi_v) / 35.0 + (0.10 if vol_ratio < 1.0 else 0.0), 0.0, 1.0)
            patterns.append({"name": "range_fade", "side": "buy", "score": round(score, 3),
                             "reason": "Range regime: RSI %.1f stretched below EMA20." % rsi_v})

    return patterns


def analyze_symbol(symbol: str, tfs: Dict[str, List[Dict[str, Any]]], quote: Dict[str, Any],
                   weights: Dict[str, float], params: Dict[str, Any],
                   flow: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    primary = "M1" if params.get("flip") else "M15"
    m15 = [c for c in (tfs.get(primary) or []) if c]
    if len(m15) < 60:
        m15 = [c for c in (tfs.get("M5") or []) if c]
    if len(m15) < 60:
        m15 = [c for c in (tfs.get("M15") or []) if c]
    if len(m15) < 60:
        return {
            "symbol": symbol, "ok": False, "side": None, "confidence": 0,
            "holdReason": "Not enough candles from broker yet", "reasons": ["Waiting for history"],
        }

    closes = [float(c["close"]) for c in m15]
    highs = [float(c["high"]) for c in m15]
    lows = [float(c["low"]) for c in m15]
    price = closes[-1]
    atr_series = atr(highs, lows, closes, 14)
    atr_v = max(_last(atr_series, price * 0.001), price * 1e-6)
    ema_fast = ema(closes, 20)
    ema_slow = ema(closes, 50)
    rsi_series = rsi(closes, 14)
    rsi_v = _last(rsi_series, 50.0)
    _, _, hist = macd(closes)
    hist_v = _last(hist, 0.0)
    hist_prev = hist[-2] if len(hist) > 1 else hist_v
    bb_mid, bb_up, bb_lo = bollinger(closes, 20, 2.0)
    mid_v = _last(bb_mid, price)
    up_v = _last(bb_up, price)
    lo_v = _last(bb_lo, price)
    adx_v = _last(adx(highs, lows, closes, 14), 0.0)

    spread = float(quote.get("spread") or 0.0) if quote else 0.0
    bid = float(quote.get("bid") or price) if quote else price
    ask = float(quote.get("ask") or price) if quote else price

    diff = ema_fast[-1] - ema_slow[-1]
    trend_dir = 1 if diff > 0 else -1
    trend_strength = _clamp(abs(diff) / (atr_v * 0.8), 0.0, 1.0) * (0.55 + 0.45 * _clamp(adx_v / 40.0, 0.0, 1.0))
    h1 = [c for c in (tfs.get("H1") or []) if c]
    h1_note = ""
    if len(h1) >= 50:
        h1c = [float(c["close"]) for c in h1]
        h1f = ema(h1c, 20)[-1]
        h1s = ema(h1c, 50)[-1]
        if (h1f - h1s) * trend_dir > 0:
            trend_strength = min(1.0, trend_strength + 0.15)
            h1_note = "H1 bias agrees"
        else:
            trend_strength *= 0.75
            h1_note = "H1 bias diverges"

    mom_dir = 1 if hist_v > 0 else -1 if hist_v < 0 else 0
    mom_strength = _clamp(abs(hist_v) / (atr_v * 0.12), 0.0, 1.0)
    if mom_dir > 0 and rsi_v > 70:
        mom_strength *= 0.55
    if mom_dir < 0 and rsi_v < 30:
        mom_strength *= 0.55

    window = 20
    hi = max(highs[-window - 1:-1]) if len(highs) > window else max(highs)
    lo = min(lows[-window - 1:-1]) if len(lows) > window else min(lows)
    brk_dir = 0
    brk_strength = 0.0
    if price > hi:
        brk_dir = 1
        brk_strength = _clamp((price - hi) / (atr_v * 0.5), 0.0, 1.0)
    elif price < lo:
        brk_dir = -1
        brk_strength = _clamp((lo - price) / (atr_v * 0.5), 0.0, 1.0)

    std = max((up_v - mid_v) / 2.0, price * 1e-6)
    z = (price - mid_v) / std if std else 0.0
    mr_dir = -1 if z > 1.2 else 1 if z < -1.2 else 0
    mr_strength = _clamp((abs(z) - 1.2) / 1.2, 0.0, 1.0)

    atr_pct = atr_v / price if price else 0.0
    if adx_v >= 22 and abs(diff) >= 0.6 * atr_v:
        regime = "trend"
    elif atr_pct >= 0.006:
        regime = "volatile"
    else:
        regime = "range"

    w = dict(weights)
    if regime == "range":
        w["meanrev"] = w.get("meanrev", 0.15) * 1.5
        w["trend"] = w.get("trend", 0.35) * 0.6
    elif regime == "trend":
        w["trend"] = w.get("trend", 0.35) * 1.35
        w["meanrev"] = w.get("meanrev", 0.15) * 0.5
    elif regime == "volatile":
        w["breakout"] = w.get("breakout", 0.25) * 1.2
        w["meanrev"] = w.get("meanrev", 0.15) * 0.8
    total_w = sum(w.values()) or 1.0
    w = {k: v / total_w for k, v in w.items()}

    signals = {
        "trend": {"dir": trend_dir, "strength": trend_strength},
        "momentum": {"dir": mom_dir, "strength": mom_strength},
        "breakout": {"dir": brk_dir, "strength": brk_strength},
        "meanrev": {"dir": mr_dir, "strength": mr_strength},
    }
    score = 0.0
    contributions: Dict[str, float] = {}
    for key, sig in signals.items():
        c = w.get(key, 0.0) * sig["dir"] * sig["strength"]
        contributions[key] = round(c, 5)
        score += c
    confidence = abs(score)
    side = "buy" if score > 0 else "sell" if score < 0 else None
    patterns = detect_patterns(m15, tfs.get("M5") or [], atr_v, adx_v, ema_fast, ema_slow, rsi_v)
    aligned = [p for p in patterns if side and p.get("side") == side]
    pattern_score = max([float(p.get("score") or 0) for p in aligned] or [0.0])
    if side and pattern_score:
        confidence = _clamp(confidence + pattern_score * 0.12, 0.0, 1.0)

    threshold = max(float(params.get("threshold", 0.15)), float(params.get("minConfidence", 0.15)))
    reasons: List[str] = []
    hold_reason = ""

    trend_txt = "up" if diff > 0 else "down"
    reasons.append("M15 trend %s: EMA20 %.5f vs EMA50 %.5f, ADX %.1f%s" % (
        trend_txt, ema_fast[-1], ema_slow[-1], adx_v, (", " + h1_note) if h1_note else ""))
    reasons.append("Momentum: MACD hist %+.5f %s, RSI %.1f" % (
        hist_v, "rising" if hist_v > hist_prev else "falling", rsi_v))
    if brk_dir:
        reasons.append("Breakout %s %d-bar %s %.5f" % (
            "above" if brk_dir > 0 else "below", window, "high" if brk_dir > 0 else "low", hi if brk_dir > 0 else lo))
    else:
        reasons.append("Inside 20-bar range %.5f–%.5f" % (lo, hi))
    reasons.append("Regime %s · ATR %.5f (%.2f%%) · BB z %.2f" % (regime, atr_v, atr_pct * 100, z))
    if aligned:
        reasons.append("Pattern %s %.2f: %s" % (aligned[0].get("name"), pattern_score, aligned[0].get("reason")))
    elif patterns:
        reasons.append("Pattern conflict: %s" % ", ".join("%s %s" % (p.get("name"), p.get("side")) for p in patterns[:2]))

    if not side or confidence < threshold:
        hold_reason = "Confidence %.2f below threshold %.2f" % (confidence, threshold)
        side = None
    if spread > float(params.get("maxSpreadAtr", 0.35)) * atr_v:
        hold_reason = "Spread %.5f above %.2f×ATR filter" % (spread, float(params.get("maxSpreadAtr", 0.35)))
        side = None
    if side and (bid <= 0 or ask <= 0):
        hold_reason = "No live quote"
        side = None

    flow = flow or {}
    flow_available = bool(flow.get("available"))
    flow_score = float(flow.get("score") or 0.0)
    flow_alignment = 0.0
    if flow_available:
        reasons.append("Orderflow %s: score %+.2f · depth %+.2f · tick delta %+.2f" % (
            flow.get("pressure") or "balanced",
            flow_score,
            float(flow.get("depthImbalance") or 0.0),
            float(flow.get("tickDeltaRatio") or 0.0),
        ))
        if side:
            flow_alignment = flow_score if side == "buy" else -flow_score
            confidence = _clamp(confidence + flow_alignment * 0.10 * float(flow.get("quality") or 0.5), 0.0, 1.0)
            if flow_alignment < -0.30:
                hold_reason = "Orderflow opposes %s: %s" % (side.upper(), flow.get("pressure") or "heavy opposite pressure")
                side = None
            elif flow_alignment > 0.15:
                reasons.append("Orderflow confirms %s (alignment %+.2f)" % (side.upper(), flow_alignment))

    digits = _digits(price)
    sl = tp = None
    if side:
        sl_dist = atr_v * float(params.get("atrSl", 1.8))
        tp_dist = atr_v * float(params.get("atrTp", 3.2))
        if side == "buy":
            sl = round(ask - sl_dist, digits)
            tp = round(ask + tp_dist, digits)
            reasons.append("Risk %.1f%% · SL %.5f (%.1f×ATR) · TP %.5f (%.1f×ATR)" % (
                float(params.get("riskPct", 0.5)), sl, float(params.get("atrSl", 1.8)), tp, float(params.get("atrTp", 3.2))))
        else:
            sl = round(bid + sl_dist, digits)
            tp = round(bid - tp_dist, digits)
            reasons.append("Risk %.1f%% · SL %.5f (%.1f×ATR) · TP %.5f (%.1f×ATR)" % (
                float(params.get("riskPct", 0.5)), sl, float(params.get("atrSl", 1.8)), tp, float(params.get("atrTp", 3.2))))
        reasons.append("Confidence %.2f ≥ %.2f" % (confidence, threshold))

    summary = ""
    if side:
        summary = "%s %s: %s regime, %s trend, score %.2f. %s" % (
            symbol, side.upper(), regime, trend_txt, confidence,
            ("Entry near %.5f, invalidation %.5f, target %.5f." % (ask if side == "buy" else bid, sl, tp)))
    else:
        summary = "%s: standing down — %s." % (symbol, hold_reason or "no edge")

    return {
        "symbol": symbol,
        "ok": True,
        "side": side,
        "score": round(score, 4),
        "confidence": round(confidence, 4),
        "regime": regime,
        "trend": trend_txt,
        "price": round(price, digits),
        "bid": round(bid, digits),
        "ask": round(ask, digits),
        "spread": round(spread, digits),
        "rsi": round(rsi_v, 2),
        "atr": round(atr_v, digits),
        "atrPct": round(atr_pct * 100, 3),
        "adx": round(adx_v, 2),
        "sl": sl,
        "tp": tp,
        "contributions": contributions,
        "signals": {k: {"dir": v["dir"], "strength": round(v["strength"], 3)} for k, v in signals.items()},
        "reasons": reasons,
        "holdReason": hold_reason,
        "summary": summary,
        "weights": {k: round(v, 4) for k, v in w.items()},
        "flowAvailable": flow_available,
        "flowScore": round(flow_score, 3),
        "flowAlignment": round(flow_alignment, 3),
        "barDeltaRatio": flow.get("barDeltaRatio"),
        "m1DeltaRatio": flow.get("m1DeltaRatio"),
        "absorption": flow.get("absorption"),
        "flowQuality": flow.get("quality"),
        "macdHist": round(hist_v, 6),
        "bbZ": round(z, 3),
        "flowPressure": flow.get("pressure"),
        "depthImbalance": flow.get("depthImbalance"),
        "tickDeltaRatio": flow.get("tickDeltaRatio"),
        "bidDepth": flow.get("bidDepth"),
        "askDepth": flow.get("askDepth"),
        "flowNotes": flow.get("notes") or [],
        "patterns": patterns,
        "patternScore": round(pattern_score, 3),
        "patternName": aligned[0].get("name") if aligned else None,
    }


def build_factor_board(rows: List[Dict[str, Any]], equity: float, drawdown_pct: float,
                       fr: Dict[str, Any], research_rows: List[Dict[str, Any]],
                       positions: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
    positions = positions or []
    research = (research_rows[-1] if research_rows else {}) or {}
    macro_state = str(research.get("regime") or research.get("bias") or "unknown")
    macro_note = str(((research.get("notes") or [""])[0]) or "research queued")[:140]
    symbols: List[Dict[str, Any]] = []
    aggregate: Dict[str, List[Dict[str, Any]]] = {}
    for row in rows:
        if not row.get("ok"):
            continue
        sig = row.get("signals") or {}
        weights = row.get("weights") or {}
        atr_v = float(row.get("atr") or 0)
        spread = float(row.get("spread") or 0)
        spread_atr = (spread / atr_v) if atr_v > 0 else 0.0
        factors: List[Dict[str, Any]] = []

        def add(key: str, label: str, score: float, weight: float, state: str, detail: str, source: str) -> None:
            score = _clamp(float(score), -1.0, 1.0)
            factors.append({
                "key": key, "label": label, "score": round(score, 3), "weight": round(float(weight), 3),
                "state": state, "detail": detail, "source": source,
            })

        flow_quality = float(row.get("flowQuality") or 0.0)
        add("orderflow", "Orderflow", float(row.get("flowScore") or 0), 0.22 * max(0.25, flow_quality),
            str(row.get("flowPressure") or "balanced"),
            "DOM %+.2f · tick %+.2f · M5 %+.2f · M1 %+.2f · absorption %+.2f · quality %.0f%%" % (
                float(row.get("depthImbalance") or 0), float(row.get("tickDeltaRatio") or 0),
                float(row.get("barDeltaRatio") or 0), float(row.get("m1DeltaRatio") or 0),
                float(row.get("absorption") or 0), flow_quality * 100.0),
            "DOM / ticks / M5+M1")
        for key, label, source in (("trend", "Trend", "EMA/ADX"), ("momentum", "Momentum", "MACD/RSI"),
                                   ("breakout", "Structure", "swings"), ("meanrev", "Mean reversion", "Bollinger")):
            item = sig.get(key) or {}
            direction = int(item.get("dir") or 0)
            strength = float(item.get("strength") or 0)
            state = "up" if direction > 0 else "down" if direction < 0 else "flat"
            detail = "%s %.2f" % (state, strength)
            if key == "trend":
                detail = "EMA20/50 %s · ADX %.1f" % (row.get("trend") or "flat", float(row.get("adx") or 0))
            elif key == "momentum":
                detail = "MACD %+.5f · RSI %.1f" % (float(row.get("macdHist") or 0), float(row.get("rsi") or 50))
            elif key == "breakout":
                detail = "20-bar range %s" % ("broken" if direction else "inside")
            elif key == "meanrev":
                detail = "BB z %+.2f" % float(row.get("bbZ") or 0)
            add(key, label, direction * strength, float(weights.get(key) or 0), state, detail, source)
        p_side = str(row.get("side") or "")
        p_score = float(row.get("patternScore") or 0.0)
        p_signed = p_score if p_side == "buy" else -p_score if p_side == "sell" else 0.0
        add("patterns", "Patterns", p_signed, 0.15,
            str(row.get("patternName") or "none"),
            "best aligned pattern %.2f · %s" % (p_score, row.get("patternName") or "none"),
            "M15 structure")
        add("execution", "Execution", -_clamp(spread_atr / 0.35, 0.0, 1.0), 0.0,
            "tight" if spread_atr <= 0.10 else "wide",
            "spread %.5f · %.2f%% of ATR" % (spread, spread_atr * 100.0), "quote")
        add("volatility", "Volatility", 0.0, 0.0, str(row.get("regime") or "range"),
            "ATR %.5f · %.2f%% · ADX %.1f" % (atr_v, float(row.get("atrPct") or 0), float(row.get("adx") or 0)),
            "M1/M5")
        add("macro", "Macro / news", 0.0, 0.0, macro_state, macro_note, "Komz research")
        add("risk", "Risk", -_clamp(drawdown_pct / 20.0, 0.0, 1.0), 0.0,
            "high" if drawdown_pct >= 10 else "normal",
            "drawdown %.1f%% · deficit $%.2f" % (drawdown_pct, float(fr.get("deficit") or 0)), "account")
        base = _base(row.get("symbol"))
        legs = [p for p in positions if _base(p.get("symbol")) == base]
        buy_lots = sum(float(p.get("volume") or 0) for p in legs if str(p.get("side") or "").lower() == "buy")
        sell_lots = sum(float(p.get("volume") or 0) for p in legs if str(p.get("side") or "").lower() == "sell")
        net_lots = buy_lots - sell_lots
        add("exposure", "Net exposure", 0.0, 0.0,
            "long" if net_lots > 0 else "short" if net_lots < 0 else "flat",
            "buy %.2f · sell %.2f · net %+.2f lots" % (buy_lots, sell_lots, net_lots), "positions")

        directional = [f for f in factors if f["weight"] > 0]
        total_w = sum(f["weight"] for f in directional) or 1.0
        score = sum(f["score"] * f["weight"] for f in directional) / total_w
        aligned = sum(1 for f in directional if f["score"] * score > 0)
        confidence = abs(score) * (aligned / len(directional) if directional else 0.0)
        bias = "buy" if score > 0.12 else "sell" if score < -0.12 else "neutral"
        symbols.append({
            "symbol": _base(row.get("symbol")), "bias": bias, "score": round(score, 3),
            "confidence": round(confidence, 3), "regime": row.get("regime"),
            "atrPct": row.get("atrPct"), "spreadAtr": round(spread_atr, 4),
            "netLots": round(net_lots, 4), "factors": factors,
        })
        for f in factors:
            aggregate.setdefault(f["key"], []).append(f)

    factors_out: List[Dict[str, Any]] = []
    for key, values in aggregate.items():
        scores = [v["score"] for v in values]
        states = [v["state"] for v in values]
        state = max(set(states), key=states.count) if states else "unknown"
        factors_out.append({
            "key": key, "label": values[0]["label"],
            "score": round(sum(scores) / len(scores), 3),
            "weight": round(sum(v["weight"] for v in values) / len(values), 3),
            "state": state, "detail": values[0]["detail"], "source": values[0]["source"],
            "symbols": len(values),
        })
    directional = [f for f in factors_out if f["weight"] > 0]
    total_w = sum(f["weight"] for f in directional) or 1.0
    score = sum(f["score"] * f["weight"] for f in directional) / total_w
    bias = "buy" if score > 0.12 else "sell" if score < -0.12 else "neutral"
    regimes = [s.get("regime") for s in symbols if s.get("regime")]
    regime = max(set(regimes), key=regimes.count) if regimes else "range"
    confidence = sum(float(s.get("confidence") or 0) for s in symbols) / len(symbols) if symbols else 0.0
    return {
        "updatedAt": _iso(), "bias": bias, "score": round(score, 3),
        "confidence": round(confidence, 3), "regime": regime,
        "factors": factors_out, "symbols": symbols,
        "observations": len(symbols), "equity": round(float(equity or 0), 2),
    }


def position_size(equity: float, risk_pct: float, sl_distance: float, spec: Dict[str, Any]) -> float:
    equity = float(equity or 0)
    risk_pct = _clamp(float(risk_pct or 0.5), 0.05, 5.0)
    sl_distance = abs(float(sl_distance or 0))
    if equity <= 0 or sl_distance <= 0:
        return 0.0
    tick_size = float(spec.get("tickSize") or spec.get("point") or 0.00001)
    tick_value = float(spec.get("tickValue") or 0)
    contract = float(spec.get("contractSize") or 0)
    if tick_value > 0 and tick_size > 0:
        loss_per_lot = (sl_distance / tick_size) * tick_value
    elif contract > 0:
        loss_per_lot = sl_distance * contract
    else:
        loss_per_lot = sl_distance * 100000.0
    if loss_per_lot <= 1e-9:
        return 0.0
    raw = (equity * risk_pct / 100.0) / loss_per_lot
    step = float(spec.get("volumeStep") or 0.01)
    vmin = float(spec.get("volumeMin") or 0.01)
    vmax = float(spec.get("volumeMax") or 100.0)
    if step <= 0:
        step = 0.01
    lots = math.floor(raw / step) * step
    lots = max(vmin, min(vmax, lots))
    return round(lots, 4)


def learn(weights: Dict[str, float], contributions: Dict[str, float], reward: float, eta: float = 0.18) -> Dict[str, float]:
    reward = _clamp(float(reward or 0), -2.0, 2.0)
    out: Dict[str, float] = {}
    for key, weight in (weights or DEFAULT_WEIGHTS).items():
        c = float((contributions or {}).get(key, 0.0))
        try:
            out[key] = float(weight) * math.exp(eta * reward * c)
        except OverflowError:
            out[key] = float(weight) * 1e6
        out[key] = _clamp(out[key], 0.02, 0.85)
    total = sum(out.values()) or 1.0
    return {k: round(v / total, 5) for k, v in out.items()}


def adapt(params: Dict[str, Any], stats: Dict[str, Any], min_threshold: float = 0.15) -> (Dict[str, Any], str):
    p = dict(DEFAULT_PARAMS)
    p.update(params or {})
    trades = int(stats.get("trades") or 0)
    note = "Collecting samples"
    if trades >= 8:
        wr = float(stats.get("winRate") or 0)
        pf = float(stats.get("profitFactor") or 0)
        dd = float(stats.get("maxDrawdownPct") or 0)
        if wr < 0.45:
            p["threshold"] = min(0.70, float(p["threshold"]) + 0.01)
            note = "Win rate %.0f%% → raising confidence bar" % (wr * 100)
        elif wr > 0.60:
            p["threshold"] = max(0.15, float(p["threshold"]) - 0.005)
            note = "Win rate %.0f%% → easing confidence bar" % (wr * 100)
        if pf > 1.4:
            p["riskPct"] = min(1.5, float(p["riskPct"]) * 1.02)
        elif pf and pf < 0.9:
            p["riskPct"] = max(0.1, float(p["riskPct"]) * 0.97)
        if dd > 6:
            p["riskPct"] = max(0.1, float(p["riskPct"]) * 0.9)
            note = "Drawdown %.1f%% → cutting risk" % dd
        p["threshold"] = round(_clamp(float(p["threshold"]), float(min_threshold), 0.70), 4)
        p["riskPct"] = round(_clamp(float(p["riskPct"]), 0.1, 1.5), 3)
    return p, note


def compute_stats(closed: List[Dict[str, Any]], starting_equity: float) -> Dict[str, Any]:
    if not closed:
        return dict(ZERO_STATS)
    pnls = [float(c.get("pnl") or 0) for c in closed]
    wins = sum(1 for p in pnls if p > 0)
    losses = sum(1 for p in pnls if p < 0)
    gross_win = sum(p for p in pnls if p > 0)
    gross_loss = abs(sum(p for p in pnls if p < 0))
    peak = float(starting_equity or 0)
    equity = peak
    max_dd = 0.0
    for p in pnls:
        equity += p
        peak = max(peak, equity)
        if peak > 0:
            max_dd = max(max_dd, (peak - equity) / peak * 100.0)
    rs = [float(c.get("r") or 0) for c in closed]
    return {
        "trades": len(pnls),
        "wins": wins,
        "losses": losses,
        "winRate": round(wins / len(pnls), 4) if pnls else 0,
        "profitFactor": round(gross_win / gross_loss, 3) if gross_loss > 1e-9 else (round(gross_win, 3) if gross_win else 0),
        "expectancy": round(sum(pnls) / len(pnls), 3),
        "maxDrawdownPct": round(max_dd, 2),
        "avgR": round(sum(rs) / len(rs), 3) if rs else 0,
    }


def initial_state() -> Dict[str, Any]:
    return {
        "running": False,
        "mode": "live",
        "cycle": 0,
        "generation": 0,
        "equity": None,
        "balance": None,
        "aiPnl": 0,
        "floating": 0,
        "todayPnl": 0,
        "positions": [],
        "closed": [],
        "decisions": [],
        "thoughts": [],
        "thesis": None,
        "market": [],
        "stats": dict(ZERO_STATS),
        "learning": {
            "weights": dict(DEFAULT_WEIGHTS),
            "eta": 0.18,
            "threshold": DEFAULT_PARAMS["threshold"],
            "riskPct": DEFAULT_PARAMS["riskPct"],
            "generation": 0,
            "note": "Warming up",
        },
        "params": dict(DEFAULT_PARAMS),
        "weights": dict(DEFAULT_WEIGHTS),
        "halted": False,
        "error": None,
        "startedAt": _iso(),
        "updatedAt": _iso(),
        "lastTradeAt": {},
        "lastHoldAt": {},
        "lastRiskHoldAt": {},
        "startingEquity": None,
        "peakEquity": None,
        "flipRecovery": {
            "baseLot": 0.01, "maxLot": None, "step": 0, "lot": 0.01,
            "deficit": 0.0, "missedProfit": 0.0,
            "sequence": [0.01, 0.02, 0.04, 0.08, 0.16, 0.32],
            "pairs": {},
        },
        "komz": {
            "name": "Komz",
            "enabled": False,
            "available": False,
            "model": None,
            "lastReview": None,
            "lastResearchAt": None,
            "research": [],
            "lessons": [],
            "riskBias": 0.0,
            "recovery": {"active": False, "drawdownPct": 0.0, "riskMultiplier": 1.0},
            "compounding": {"active": False},
            "memory": {"telegram": False, "supabase": False, "chunks": 0},
        },
    }


# ---------------------------------------------------------------- agent
class AIAgent:
    def __init__(self, pool, api, log, lock=None, interval: float = 5.0, on_account=None, brain=None, shard: int = 0, shards: int = 1):
        self.pool = pool
        self.api = api
        self.log = log
        self.lock = lock
        self.interval = interval
        self.on_account = on_account
        self.brain = brain
        self.shard = int(shard or 0)
        self.shards = max(1, int(shards or 1))
        self.accounts: List[Dict[str, Any]] = []
        self.last_refresh = 0.0
        self.cycle = 0

    def _locked(self):
        return self.lock if self.lock is not None else _NullLock()

    def refresh(self, force: bool = False) -> None:
        if not force and self.accounts and time.time() - self.last_refresh < 5:
            return
        try:
            payload = self.api("/v1/worker/ai/accounts", {"shard": self.shard, "shards": self.shards})
            self.accounts = payload.get("accounts") or []
            self.last_refresh = time.time()
            if self.accounts:
                self.log("ai accounts: %d" % len(self.accounts))
        except Exception as e:
            self.log("ai account refresh failed: %s" % e)

    def run_forever(self, stop_event) -> None:
        while not stop_event.is_set():
            try:
                self.refresh()
                for row in list(self.accounts):
                    try:
                        self.run_account(row)
                    except Exception as e:
                        self.log("ai account %s failed: %s" % (row.get("accountId"), e))
                        self._post_error(row, e)
            except Exception as e:
                self.log("ai loop error: %s" % e)
            stop_event.wait(self.interval)

    def _post(self, account_id: str, state: Dict[str, Any]) -> None:
        state["updatedAt"] = _iso()
        try:
            self.api("/v1/worker/ai/state", {"accountId": account_id, "state": state})
        except Exception as e:
            self.log("ai state post failed: %s" % e)

    def _post_error(self, row: Dict[str, Any], error: Exception) -> None:
        state = row.get("state") or initial_state()
        state["error"] = str(error)
        state["running"] = False
        self._post(row.get("accountId"), state)

    def run_account(self, row: Dict[str, Any]) -> None:
        cfg = row.get("config") or {}
        creds = row.get("credentials") or {}
        account_id = row.get("accountId")
        use_brain = bool(self.brain and cfg.get("komz", True))
        if not account_id or not creds or not cfg.get("enabled"):
            return
        state = row.get("state") or initial_state()
        state.setdefault("params", dict(DEFAULT_PARAMS))
        state.setdefault("weights", dict(DEFAULT_WEIGHTS))
        state.setdefault("learning", {})
        state.setdefault("closed", [])
        state.setdefault("decisions", [])
        state.setdefault("thoughts", [])
        state.setdefault("positions", [])
        state.setdefault("lastTradeAt", {})
        state.setdefault("lastHoldAt", {})
        state.setdefault("lastRiskHoldAt", {})
        komz_state = state.setdefault("komz", initial_state()["komz"])
        if self.brain:
            try:
                komz_state.update(self.brain.status())
            except Exception as e:
                self.log("komz status failed: %s" % e)

        def progress():
            if self.brain:
                try:
                    komz_state.update(self.brain.status())
                except Exception:
                    pass
            self._post(account_id, state)
        params = dict(DEFAULT_PARAMS)
        params.update(state.get("params") or {})
        profile = str(cfg.get("mode") or "balanced").lower()
        profit = profile == "profit"
        flip = (bool(cfg.get("flipMode")) or profile == "flip") and not profit
        if flip:
            profile = "flip"
        aggressive = profile == "aggressive"
        for key in ("riskPct", "maxPositions", "minConfidence", "maxDailyLossPct"):
            if cfg.get(key) is not None:
                params[key] = cfg[key]
        if flip:
            params["flip"] = True
            params["minConfidence"] = min(float(params.get("minConfidence", 0.15)), 0.05)
            params["threshold"] = min(float(params.get("threshold", 0.15)), 0.05)
            params["riskPct"] = max(float(params.get("riskPct", 0.5)), 1.5)
            params["maxPositions"] = 4
            params["cooldownSec"] = min(float(params.get("cooldownSec", 180)), 5)
            params["maxDailyLossPct"] = min(float(params.get("maxDailyLossPct", 3)), 3.0)
            params["timeStopHours"] = min(float(params.get("timeStopHours", 24)), 0.08)
            params["maxSpreadAtr"] = min(float(params.get("maxSpreadAtr", 0.35)), 0.30)
            params["atrSl"] = 0.7
            params["atrTp"] = 0.7
            params["trailAt"] = 0.35
            params["trailDistance"] = 0.25
        else:
            params["flip"] = False
            if (state.get("params") or {}).get("flip"):
                for key in ("threshold", "minConfidence", "riskPct", "maxPositions", "maxDailyLossPct", "cooldownSec", "timeStopHours", "maxSpreadAtr", "atrSl", "atrTp", "trailAt", "trailDistance"):
                    params[key] = DEFAULT_PARAMS[key]
        if aggressive:
            params["minConfidence"] = min(float(params.get("minConfidence", 0.15)), 0.08)
            params["threshold"] = min(float(params.get("threshold", 0.15)), 0.08)
            params["riskPct"] = max(float(params.get("riskPct", 0.5)), 1.0)
            params["maxPositions"] = max(int(params.get("maxPositions", 3)), 5)
            params["cooldownSec"] = min(float(params.get("cooldownSec", 180)), 60)
            params["maxDailyLossPct"] = max(float(params.get("maxDailyLossPct", 3)), 4.0)
        if profit:
            params["flip"] = False
            params["profit"] = True
            params["riskPct"] = min(float(cfg.get("riskPct") or 0.5), 0.5)
            params["maxPositions"] = 2
            params["minConfidence"] = max(float(cfg.get("minConfidence") or 0.55), 0.55)
            params["threshold"] = max(float(params.get("threshold") or 0.55), 0.55)
            params["maxDailyLossPct"] = min(float(cfg.get("maxDailyLossPct") or 2.0), 2.0)
            params["cooldownSec"] = max(float(params.get("cooldownSec") or 300), 300)
            params["timeStopHours"] = 4.0
            params["maxSpreadAtr"] = 0.12
            params["atrSl"] = 1.2
            params["atrTp"] = 2.4
            params["trailAt"] = 1.0
            params["trailDistance"] = 0.8
        if cfg.get("minConfidence") is not None and not profit:
            params["threshold"] = min(float(params.get("threshold", 0.15)), float(params["minConfidence"]))
        if flip:
            params["minConfidence"] = min(float(params.get("minConfidence") or 0.05), 0.05)
            params["threshold"] = min(float(params.get("threshold") or 0.05), 0.05)
        state["profile"] = profile
        state["flipMode"] = bool(flip)
        state["profitMode"] = bool(profit)
        komz_state["profile"] = profile
        komz_state["flipMode"] = bool(flip)
        komz_state["profitMode"] = bool(profit)
        weights = state.get("weights") or dict(DEFAULT_WEIGHTS)
        symbols = [str(s).upper() for s in (cfg.get("symbols") or [])][:6]
        focus = [str(s).upper() for s in (cfg.get("focusSymbols") or [])][:4]
        focus_set = {_base(s) for s in focus}
        state["focusSymbols"] = focus
        komz_state["focusSymbols"] = focus
        dry = bool(cfg.get("dryRun"))
        state["mode"] = "dry-run" if dry else "live"
        state["running"] = True
        state["error"] = None
        state["cycle"] = int(state.get("cycle") or 0) + 1

        command = cfg.get("command")
        if command == "reset":
            state = initial_state()
            komz_state = state["komz"]
            state["mode"] = "dry-run" if dry else "live"
            state["profile"] = profile
            komz_state["profile"] = profile
            state["commandAck"] = "reset"
            state["thoughts"].append({"at": _iso(), "text": "Learning state reset by operator."})
            params = dict(DEFAULT_PARAMS)
            weights = dict(DEFAULT_WEIGHTS)
            command = None
        elif command == "close_all":
            with self._locked():
                try:
                    positions = [p for p in (self.pool.positions(creds) or []) if int(p.get("magic") or 0) == MAGIC]
                    for p in positions:
                        self.pool.close_position(creds, {"ticket": p.get("ticket")})
                except Exception as e:
                    state["error"] = "close_all: %s" % e
            state["commandAck"] = "close_all"
            state["thoughts"].append({"at": _iso(), "text": "Operator requested close-all of AI positions."})
            state["cooldownUntil"] = time.time() + 30
            command = None

        info = {}
        ai_positions: List[Dict[str, Any]] = []
        market: Dict[str, Any] = {}
        with self._locked():
            self.pool.connect(creds)
            info = self.pool.account(creds) or {}
            all_positions = self.pool.positions(creds) or []
            ai_positions = [p for p in all_positions if int(p.get("magic") or 0) == MAGIC]
            for sym in symbols:
                tfs: Dict[str, List[Dict[str, Any]]] = {}
                tfs_list = ("M1", "M5", "M15", "H1") if flip else ("M5", "M15", "H1")
                for tf in tfs_list:
                    try:
                        tfs[tf] = self.pool.rates(creds, sym, tf, 300)
                    except Exception:
                        tfs[tf] = []
                quote = {}
                try:
                    prices = self.pool.prices(creds, [sym]).get("prices") or []
                    if prices:
                        quote = prices[0]
                except Exception:
                    quote = {}
                spec = {}
                try:
                    spec = self.pool.spec(creds, sym) or {}
                except Exception:
                    spec = {}
                flow = {}
                try:
                    flow = self.pool.orderflow(creds, sym, quote) or {}
                except Exception:
                    flow = {}
                market[sym] = {"tfs": tfs, "quote": quote, "spec": spec, "flow": flow}

        equity = float(info.get("equity") or info.get("balance") or 0)
        if state.get("startingEquity") is None:
            state["startingEquity"] = equity
        fr = state.setdefault("flipRecovery", {
            "baseLot": 0.01, "maxLot": None, "step": 0, "lot": 0.01,
            "deficit": 0.0, "missedProfit": 0.0,
            "sequence": [0.01, 0.02, 0.04, 0.08, 0.16, 0.32], "pairs": {},
        })
        fr["baseLot"] = float(fr.get("baseLot") or 0.01)
        seq = ensure_flip_sequence(fr, 6)
        fr["maxLot"] = None
        if not isinstance(fr.get("pairs"), dict):
            fr["pairs"] = {}
            fr["legacyDeficit"] = round(float(fr.get("deficit") or 0), 2)
            fr["deficit"] = 0.0
            fr["step"] = 0
            fr["lot"] = float(seq[0])
        missed = round(max(0.5, equity * 0.001), 2)
        for key in list(fr["pairs"].keys()):
            if ":" not in str(key):
                fr["pairs"]["%s:BUY" % key] = fr["pairs"].pop(key)
        for key, pair in list(fr["pairs"].items()):
            if not isinstance(pair, dict):
                pair = {}
            pair["step"] = max(0, int(pair.get("step") or 0))
            seq = ensure_flip_sequence(fr, pair["step"])
            pair["lot"] = float(seq[pair["step"]])
            pair["deficit"] = round(max(0.0, float(pair.get("deficit") or 0)), 2)
            pair["missedProfit"] = missed
            fr["pairs"][key] = pair
        fr["missedProfit"] = missed

        rows = [analyze_symbol(sym, m["tfs"], m["quote"], weights, params, m.get("flow")) for sym, m in market.items()]
        rows.sort(key=lambda r: (0 if _base(r.get("symbol")) in focus_set else 1, -(float(r.get("confidence") or 0))))
        focus_available = any(_base(r.get("symbol")) in focus_set and r.get("ok") and r.get("side") for r in rows)
        by_symbol = {r.get("symbol"): r for r in rows if r.get("ok")}

        now = time.time()
        recent_losses = {}
        if flip:
            for rec in reversed(state.get("closed") or []):
                sym0 = _base(rec.get("symbol"))
                if not sym0 or sym0 in recent_losses:
                    continue
                try:
                    age = now - datetime.fromisoformat(str(rec.get("closedAt") or "").replace("Z", "+00:00")).timestamp()
                except Exception:
                    continue
                if age < 60 and float(rec.get("pnl") or 0) < 0:
                    recent_losses[sym0] = rec
        current_tickets = {str(p.get("ticket")) for p in ai_positions}
        previous = list(state.get("positions") or [])
        closed_now: List[Dict[str, Any]] = []

        for old in previous:
            if str(old.get("ticket")) in current_tickets:
                continue
            pnl = 0.0
            with self._locked():
                try:
                    res = self.pool.deal_result(creds, old.get("ticket"))
                    pnl = float(res.get("profit") or 0)
                except Exception:
                    pnl = 0.0
            risk_amount = float(old.get("riskAmount") or 1.0)
            reward = pnl / risk_amount if risk_amount > 0 else 0.0
            weights = learn(weights, old.get("contributions") or {}, reward, float(state.get("learning", {}).get("eta") or 0.18))
            record = {
                "ticket": old.get("ticket"),
                "symbol": old.get("symbol"),
                "side": old.get("side"),
                "volume": old.get("volume"),
                "openPrice": old.get("openPrice"),
                "closePrice": by_symbol.get(_base(old.get("symbol")), {}).get("price"),
                "pnl": round(pnl, 2),
                "r": round(reward, 3),
                "closedAt": _iso(),
                "reason": old.get("reason"),
                "pattern": old.get("pattern"),
                "patternScore": old.get("patternScore"),
                "flip": bool(old.get("flip", flip)),
            }
            closed_now.append(record)
            state["closed"].append(record)
            state["closed"] = state["closed"][-100:]
            if flip and old.get("flip", True):
                update_flip_pair(fr, seq, old.get("symbol"), float(record.get("pnl") or 0), old.get("side"))
            for dec in reversed(state["decisions"]):
                if str(dec.get("ticket")) == str(old.get("ticket")):
                    dec["outcome"] = "win" if pnl > 0 else "loss" if pnl < 0 else "flat"
                    dec["pnl"] = round(pnl, 2)
                    break
            state["decisions"].append({
                "id": "c%d" % int(time.time() * 1000),
                "at": _iso(),
                "action": "close",
                "symbol": old.get("symbol"),
                "side": old.get("side"),
                "confidence": 1.0,
                "price": record["closePrice"],
                "volume": old.get("volume"),
                "reason": "Position closed at %.2f (%+.2fR). %s" % (pnl, reward, "Learning weights updated." if reward else ""),
                "ticket": old.get("ticket"),
                "outcome": "win" if pnl > 0 else "loss" if pnl < 0 else "flat",
                "pnl": round(pnl, 2),
            })

        if closed_now and use_brain:
            try:
                lesson = self.brain.reflect(account_id, cfg, state, closed_now[0], info)
                if lesson:
                    state.setdefault("thoughts", []).append({
                        "at": _iso(),
                        "text": "Komz reflection: %s" % (lesson.get("lesson") or lesson.get("nextAction") or "trade reviewed"),
                    })
            except Exception as e:
                self.log("komz reflection failed: %s" % e)

        # manage open positions
        kept: List[Dict[str, Any]] = []
        for pos in ai_positions:
            sym = _base(pos.get("symbol"))
            row = by_symbol.get(sym) or by_symbol.get(pos.get("symbol"))
            if not row:
                kept.append(self._serialize_position(pos, pos.get("reason")))
                continue
            side = str(pos.get("side") or "").lower()
            price = float(row.get("bid") if side == "buy" else row.get("ask") or row.get("price") or 0)
            atr_v = float(row.get("atr") or 0)
            open_price = float(pos.get("openPrice") or 0)
            entry = {"ticket": pos.get("ticket"), "symbol": pos.get("symbol"), "side": side,
                     "volume": pos.get("volume"), "openPrice": open_price, "currentPrice": pos.get("currentPrice"),
                     "profit": pos.get("profit"), "sl": pos.get("stopLoss"), "tp": pos.get("takeProfit"),
                     "openedAt": pos.get("openedAt")}
            old = next((p for p in previous if str(p.get("ticket")) == str(pos.get("ticket"))), {})
            entry["reason"] = old.get("reason")
            entry["contributions"] = old.get("contributions") or {}
            entry["riskAmount"] = old.get("riskAmount") or 1.0
            entry["decisionId"] = old.get("decisionId")
            entry["flip"] = bool(old.get("flip", flip))
            entry["targetTp"] = old.get("targetTp") or pos.get("takeProfit") or 0
            entry["tpArmed"] = bool(old.get("tpArmed"))
            if not dry and flip and float(pos.get("takeProfit") or 0) > 0:
                try:
                    with self._locked():
                        self.pool.modify_position(creds, {"ticket": pos.get("ticket"), "stopLoss": float(pos.get("stopLoss") or 0), "takeProfit": 0})
                except Exception as e:
                    state["error"] = "tp strip failed: %s" % e

            close_reason = ""
            hedge_leg = bool(flip and _base(pos.get("symbol")) in ("BTCUSD", "XAUUSD"))
            age_h = 0.0
            try:
                opened = datetime.fromisoformat(str(pos.get("openedAt")).replace("Z", "+00:00"))
                age_h = (datetime.now(timezone.utc) - opened).total_seconds() / 3600.0
            except Exception:
                age_h = 0.0
            if age_h >= float(params.get("timeStopHours", 24)) and not (flip and entry.get("tpArmed")) and not hedge_leg:
                close_reason = "Time stop: position open %.1fh" % age_h
            elif not hedge_leg and row.get("side") and row.get("side") != side and float(row.get("confidence") or 0) >= (max(0.20, float(params.get("threshold", 0.05))) if flip else max(0.45, float(params.get("threshold", 0.40)))):
                close_reason = "Opposite signal: %s confidence %.2f" % (row.get("side").upper(), float(row.get("confidence") or 0))
            if close_reason and not dry:
                with self._locked():
                    try:
                        self.pool.close_position(creds, {"ticket": pos.get("ticket")})
                        res = self.pool.deal_result(creds, pos.get("ticket"))
                        pnl = float(res.get("profit") or 0)
                    except Exception as e:
                        state["error"] = "close failed: %s" % e
                        pnl = 0.0
                risk_amount = float(entry.get("riskAmount") or 1.0)
                reward = pnl / risk_amount if risk_amount > 0 else 0.0
                weights = learn(weights, entry.get("contributions") or {}, reward, float(state.get("learning", {}).get("eta") or 0.18))
                record = {"ticket": pos.get("ticket"), "symbol": pos.get("symbol"), "side": side,
                          "volume": pos.get("volume"), "openPrice": open_price,
                          "closePrice": price, "pnl": round(pnl, 2), "r": round(reward, 3),
                          "closedAt": _iso(), "reason": close_reason,
                          "pattern": entry.get("pattern"), "patternScore": entry.get("patternScore")}
                state["closed"].append(record)
                state["closed"] = state["closed"][-100:]
                if flip and entry.get("flip", True):
                    update_flip_pair(fr, seq, pos.get("symbol"), pnl, side)
                state["decisions"].append({
                    "id": "x%d" % int(time.time() * 1000), "at": _iso(), "action": "close",
                    "symbol": pos.get("symbol"), "side": side, "confidence": float(row.get("confidence") or 0),
                    "price": price, "volume": pos.get("volume"),
                    "reason": "%s · locked %+.2fR." % (close_reason, reward),
                    "ticket": pos.get("ticket"), "outcome": "win" if pnl > 0 else "loss" if pnl < 0 else "flat",
                    "pnl": round(pnl, 2),
                })
                continue

            # virtual TP: only arm after target + spread; lock enough to cover the opposite side + target
            if flip and float(entry.get("targetTp") or 0) <= 0 and atr_v > 0 and open_price > 0:
                entry["targetTp"] = round(open_price + (0.7 * atr_v if side == "buy" else -0.7 * atr_v), _digits(price))
            target_tp = float(entry.get("targetTp") or 0)
            if not dry and flip and atr_v > 0 and target_tp > 0 and not entry.get("tpArmed"):
                spread = abs(float(row.get("ask") or 0) - float(row.get("bid") or 0))
                hit = (side == "buy" and price >= target_tp + spread) or (side == "sell" and price <= target_tp - spread)
                if hit:
                    base = _base(pos.get("symbol"))
                    opposite = next((p for p in ai_positions if _base(p.get("symbol")) == base and str(p.get("side") or "").lower() != side), None)
                    opp_loss = max(0.0, -float((opposite or {}).get("profit") or 0.0))
                    pair_key = entry.get("pairKey") or "%s:%s" % (base, str(side or "").upper())
                    pair = (fr.get("pairs") or {}).get(pair_key) or {}
                    target_usd = max(0.5, float(pair.get("deficit") or 0) + float(pair.get("missedProfit") or fr.get("missedProfit") or 0))
                    tick_value = float((market.get(pos.get("symbol")) or {}).get("spec", {}).get("tickValue") or 1.0)
                    tick_size = float((market.get(pos.get("symbol")) or {}).get("spec", {}).get("tickSize") or 0.00001)
                    usd_per_price = (tick_value / tick_size) * float(entry.get("volume") or 0.01) if tick_size else 0.0
                    need = (opp_loss + target_usd) / usd_per_price if usd_per_price > 0 else 0.0
                    lock_sl = open_price + need if side == "buy" else open_price - need
                    buffer = max(atr_v * 0.05, spread * 2.0, 0.0)
                    armed_sl = max(target_tp + buffer, lock_sl) if side == "buy" else min(target_tp - buffer, lock_sl)
                    can_arm = (side == "buy" and armed_sl < price) or (side == "sell" and armed_sl > price)
                    cur_sl = float(pos.get("stopLoss") or 0)
                    better = (side == "buy" and (cur_sl == 0 or armed_sl > cur_sl + 1e-9)) or (side == "sell" and (cur_sl == 0 or armed_sl < cur_sl - 1e-9))
                    if can_arm:
                        entry["tpArmed"] = True
                    if can_arm and better:
                        try:
                            with self._locked():
                                self.pool.modify_position(creds, {"ticket": pos.get("ticket"), "stopLoss": round(armed_sl, _digits(price)), "takeProfit": 0})
                            entry["sl"] = round(armed_sl, _digits(price))
                            entry["tpArmed"] = True
                            state["decisions"].append({
                                "id": "a%d" % int(time.time() * 1000), "at": _iso(), "action": "arm",
                                "symbol": pos.get("symbol"), "side": side, "confidence": float(row.get("confidence") or 0),
                                "price": price, "volume": pos.get("volume"),
                                "reason": "TP+spread %.5f reached; opposite loss $%.2f + target $%.2f; SL %.5f locks net profit." % (target_tp + spread if side == "buy" else target_tp - spread, opp_loss, target_usd, armed_sl),
                                "ticket": pos.get("ticket"),
                            })
                        except Exception as e:
                            state["error"] = "arm failed: %s" % e

            # profit mode: protect the dollar gain at +1 ATR, then trail
            if not dry and profit and atr_v > 0 and open_price > 0 and price > 0 and not entry.get("beMoved"):
                move = (price - open_price) if side == "buy" else (open_price - price)
                if move >= 1.0 * atr_v:
                    spread = abs(float(row.get("ask") or 0) - float(row.get("bid") or 0))
                    be_sl = open_price + max(spread * 2.0, atr_v * 0.05) if side == "buy" else open_price - max(spread * 2.0, atr_v * 0.05)
                    cur_sl = float(pos.get("stopLoss") or 0)
                    better = (side == "buy" and (cur_sl == 0 or be_sl > cur_sl + 1e-9)) or (side == "sell" and (cur_sl == 0 or be_sl < cur_sl - 1e-9))
                    if better:
                        try:
                            with self._locked():
                                self.pool.modify_position(creds, {"ticket": pos.get("ticket"), "stopLoss": round(be_sl, _digits(price)), "takeProfit": pos.get("takeProfit") or 0})
                            entry["sl"] = round(be_sl, _digits(price))
                            entry["beMoved"] = True
                            state["decisions"].append({
                                "id": "b%d" % int(time.time() * 1000), "at": _iso(), "action": "protect",
                                "symbol": pos.get("symbol"), "side": side, "confidence": float(row.get("confidence") or 0),
                                "price": price, "volume": pos.get("volume"),
                                "reason": "Profit mode: +1 ATR confirmed; SL moved to %.5f to protect the gain." % be_sl,
                                "ticket": pos.get("ticket"),
                            })
                        except Exception as e:
                            state["error"] = "breakeven failed: %s" % e

            # trailing stop / break-even
            if not dry and atr_v > 0 and open_price > 0 and price > 0:
                move = (price - open_price) if side == "buy" else (open_price - price)
                cur_sl = float(pos.get("stopLoss") or 0)
                if entry.get("tpArmed") or move >= float(params.get("trailAt", 1.2)) * atr_v:
                    new_sl = price - float(params.get("trailDistance", 1.2)) * atr_v if side == "buy" else price + float(params.get("trailDistance", 1.2)) * atr_v
                    better = (side == "buy" and (cur_sl == 0 or new_sl > cur_sl + 1e-9)) or (side == "sell" and (cur_sl == 0 or new_sl < cur_sl - 1e-9))
                    if better:
                        try:
                            with self._locked():
                                self.pool.modify_position(creds, {"ticket": pos.get("ticket"), "stopLoss": round(new_sl, _digits(price)), "takeProfit": 0 if flip else (pos.get("takeProfit") or 0)})
                            entry["sl"] = round(new_sl, _digits(price))
                            state["decisions"].append({
                                "id": "t%d" % int(time.time() * 1000), "at": _iso(), "action": "trail",
                                "symbol": pos.get("symbol"), "side": side, "confidence": float(row.get("confidence") or 0),
                                "price": price, "volume": pos.get("volume"),
                                "reason": "Trailing stop to %.5f after %.1f×ATR move." % (new_sl, move / atr_v),
                                "ticket": pos.get("ticket"),
                            })
                        except Exception as e:
                            state["error"] = "trail failed: %s" % e
            if flip and _base(pos.get("symbol")) in ("BTCUSD", "XAUUSD"):
                entry["hedge"] = True
                entry["pairKey"] = entry.get("pairKey") or "%s:%s" % (_base(pos.get("symbol")), str(side or "").upper())
            kept.append(entry)

        state["positions"] = kept

        # daily loss guard
        today = datetime.now(timezone.utc).date().isoformat()
        today_pnl = sum(float(c.get("pnl") or 0) for c in state["closed"] if str(c.get("closedAt", ""))[:10] == today)
        state["todayPnl"] = round(today_pnl, 2)
        loss_limit = -abs(equity * float(params.get("maxDailyLossPct", 3)) / 100.0)
        halted = bool(equity and today_pnl <= loss_limit)
        if flip:
            halted = False
        state["halted"] = halted
        cooldown_until = 0.0 if flip else float(state.get("cooldownUntil") or 0)

        peak = max(float(state.get("peakEquity") or equity or 0), float(equity or 0))
        state["peakEquity"] = peak
        drawdown_pct = ((peak - equity) / peak * 100.0) if peak else 0.0
        risk_mult = 1.0
        if drawdown_pct >= 10.0:
            risk_mult = 0.25
        elif drawdown_pct >= 5.0:
            risk_mult = 0.5
        elif drawdown_pct >= 2.5:
            risk_mult = 0.75
        if flip:
            base_risk = max(float(params.get("riskPct", 1.5)), 0.1)
            if drawdown_pct >= 10.0:
                risk_mult = min(1.33, 2.0 / base_risk)
            elif drawdown_pct >= 5.0:
                risk_mult = min(1.25, 2.0 / base_risk)
            elif drawdown_pct >= 2.5:
                risk_mult = min(1.15, 2.0 / base_risk)
            else:
                risk_mult = 1.0
        komz_state["recovery"] = {
            "active": bool(flip and drawdown_pct > 0) or risk_mult < 1.0,
            "mode": "recovery" if flip and drawdown_pct > 0 else "protect",
            "drawdownPct": round(drawdown_pct, 2),
            "riskMultiplier": risk_mult,
            "peakEquity": round(peak, 2),
            "targetEquity": round(peak, 2),
            "flipOverride": bool(flip),
            "lot": float(fr.get("lot") or 0.01),
            "step": int(fr.get("step") or 0),
            "deficit": round(float(fr.get("deficit") or 0), 2),
            "missedProfit": round(float(fr.get("missedProfit") or 0), 2),
            "targetProfit": round(float(fr.get("deficit") or 0) + float(fr.get("missedProfit") or 0), 2),
            "sequence": list(seq),
            "pairs": {k: {"lot": float(v.get("lot") or 0.01), "step": int(v.get("step") or 0), "deficit": round(float(v.get("deficit") or 0), 2)} for k, v in (fr.get("pairs") or {}).items()},
        }
        start_equity = float(state.get("startingEquity") or equity or 0)
        komz_state["compounding"] = {
            "active": bool(start_equity and equity > start_equity),
            "startingEquity": round(start_equity, 2),
            "gainPct": round(((equity / start_equity) - 1.0) * 100.0, 2) if start_equity else 0.0,
        }
        members = [str(m.get("label") or m.get("id") or "model") for m in ((komz_state.get("council") or {}).get("members") or [])]
        if not members:
            members = [str(komz_state.get("model") or "komz").split("/")[-1]]
        best = max(rows, key=lambda r: float(r.get("confidence") or 0)) if rows else {}
        best_sym = _base(best.get("symbol")) if best else "none"
        pair_bits = ", ".join("%s %.2f" % (k, float(v.get("lot") or 0.01)) for k, v in sorted((fr.get("pairs") or {}).items()))
        stats = state.get("stats") or {}
        research_rows = komz_state.get("research") or []
        research_note = str(((research_rows[-1] or {}).get("notes") or [""])[0])[:140] if research_rows else "research queued"
        hedge_bits = ", ".join("%s %s" % (_base(p.get("symbol")), str(p.get("side") or "").upper()) for p in state.get("positions") or [] if p.get("hedge")) or "opening"
        factor_board = build_factor_board(rows, equity, drawdown_pct, fr, research_rows, state.get("positions"))
        hedge_net = ", ".join("%s %+.2f" % (s.get("symbol"), float(s.get("netLots") or 0)) for s in factor_board.get("symbols", []) if s.get("symbol") in ("BTCUSD", "XAUUSD")) or "flat"
        history = list(komz_state.get("factorHistory") or [])
        history.append({"at": factor_board["updatedAt"], "bias": factor_board["bias"],
                        "score": factor_board["score"], "confidence": factor_board["confidence"],
                        "regime": factor_board["regime"]})
        komz_state["factorHistory"] = history[-60:]
        factor_board["observations"] = len(komz_state["factorHistory"])
        komz_state["factorBoard"] = factor_board
        if int(state.get("cycle") or 0) % 20 == 0:
            state.setdefault("thoughts", []).append({
                "at": _iso(),
                "text": "Factor board %s %.2f (%s): orderflow, trend, momentum, structure, macro and risk are on the board." % (
                    factor_board["bias"].upper(), factor_board["score"], factor_board["regime"]),
            })
        best_flow = max(rows, key=lambda r: abs(float(r.get("flowScore") or 0))) if rows else {}
        best_flow_sym = _base(best_flow.get("symbol")) if best_flow else "none"
        flow_note = "orderflow %s %+.2f · DOM %+.2f · tick %+.2f" % (
            best_flow.get("flowPressure") or "balanced", float(best_flow.get("flowScore") or 0),
            float(best_flow.get("depthImbalance") or 0), float(best_flow.get("tickDeltaRatio") or 0)) if best_flow else "orderflow warming"
        structure_note = "structure %s · regime %s" % (
            (best.get("side") or "neutral").upper() if best else "neutral", factor_board["regime"])
        jobs = []
        job_defs = [
            ("Data", "live quotes, ATR, spread and orderflow", "%d live symbols · best %s %.2f · quotes current" % (len(rows), best_sym, float(best.get("confidence") or 0))),
            ("Orderflow", "DOM, tick delta, M5 delta and absorption", "%s · %s" % (best_flow_sym, flow_note)),
            ("Structure", "swings, range position and liquidity", structure_note),
            ("Tests", "SL/TP presence, unbounded per-pair ladder, 5% flip cap", "SL/TP fields present · ladders %s · 5%% flip cap active" % (pair_bits or "none")),
            ("Research", "news, regime and session context", research_note),
            ("Macro", "USD, yields and cross-asset regime", "%s · %s" % (factor_board["regime"], research_note)),
            ("Risk", "drawdown, pair recovery lots and missed profit", "drawdown %.1f%% · multiplier %.2fx · deficit $%.2f" % (drawdown_pct, risk_mult, float(fr.get("deficit") or 0))),
            ("Hedge", "two-sided BTC/Gold grid, net exposure and opposite TP/SL alignment", "active %s · net %s · side ladders %s" % (hedge_bits, hedge_net, pair_bits or "none")),
            ("Execution", "spread/ATR, fills and slippage", "best spread/ATR %s · %d symbols" % (
                min([("%s %.3f" % (s.get("symbol"), float(s.get("spreadAtr") or 0))) for s in factor_board["symbols"]], default="—"),
                len(factor_board["symbols"]))),
            ("Profit", "win rate, profit factor and expectancy", "PF %.2f · expectancy %.2f · win %.1f%%" % (float(stats.get("profitFactor") or 0), float(stats.get("expectancy") or 0), float(stats.get("winRate") or 0) * 100.0)),
        ]
        for i, (role, task, finding) in enumerate(job_defs):
            jobs.append({"model": members[i % len(members)], "role": role, "task": task, "status": "ok", "finding": finding, "updatedAt": _iso()})
        komz_state["jobs"] = jobs
        if int(state.get("cycle") or 0) % 20 == 0:
            state["thoughts"].append({"at": _iso(), "text": "Model jobs live: %s" % " · ".join("%s %s" % (j["role"], j["model"]) for j in jobs)})
        risk_pct = float(params.get("riskPct", 0.5)) * risk_mult

        loss_streak = 0
        for closed in reversed(state.get("closed") or []):
            if float(closed.get("pnl") or 0) < 0:
                loss_streak += 1
            else:
                break
        today_trades = sum(1 for c in (state.get("closed") or []) if str(c.get("closedAt", ""))[:10] == today)
        state["profitGuard"] = {
            "lossStreak": loss_streak, "todayTrades": today_trades,
            "maxTradesPerDay": 4, "maxLossStreak": 2,
            "blocked": bool(profit and (loss_streak >= 2 or today_trades >= 4)),
        }

        # open new positions
        open_count = len(state["positions"])
        open_symbols = {_base(p.get("symbol")) for p in state["positions"]}
        non_focus_open = sum(1 for p in state["positions"] if focus_set and _base(p.get("symbol")) not in focus_set)
        reviews_done = 0

        # flip hedge: keep one buy and one sell on BTC and Gold, each side with its own ladder
        if flip and not dry:
            open_sides = {(_base(p.get("symbol")), str(p.get("side") or "").lower()) for p in state["positions"]}
            for base in ("BTCUSD", "XAUUSD"):
                row = next((r for r in rows if _base(r.get("symbol")) == base), None)
                if not row:
                    continue
                atr_v = float(row.get("atr") or 0)
                if atr_v <= 0:
                    continue
                spec = (market.get(row.get("symbol")) or {}).get("spec") or {}
                for side in ("buy", "sell"):
                    if (base, side) in open_sides:
                        continue
                    if open_count >= int(params.get("maxPositions", 4)):
                        break
                    pair_key = "%s:%s" % (base, side.upper())
                    pair = (fr.get("pairs") or {}).get(pair_key) or {"step": 0, "lot": seq[0], "deficit": 0.0, "missedProfit": float(fr.get("missedProfit") or 0.0)}
                    step = max(0, int(pair.get("step") or 0))
                    seq = ensure_flip_sequence(fr, step)
                    ladder_lot = float(seq[step])
                    vmin = float(spec.get("volumeMin") or 0.01)
                    vstep = float(spec.get("volumeStep") or 0.01)
                    vmax = float(spec.get("volumeMax") or ladder_lot)
                    price = float(row.get("ask") if side == "buy" else row.get("bid") or row.get("price") or 0)
                    if price <= 0:
                        continue
                    deficit = float(pair.get("deficit") or 0)
                    missed = float(pair.get("missedProfit") or fr.get("missedProfit") or 0.0)
                    target_usd = max(missed, deficit + missed)
                    tick_value = float(spec.get("tickValue") or 1.0)
                    tick_size = float(spec.get("tickSize") or 0.00001)
                    required_lot = flip_required_lot(target_usd, max(0.7 * atr_v, 1e-9), tick_value, tick_size) if deficit > 0 else 0.0
                    lots = max(ladder_lot, required_lot)
                    if vstep > 0:
                        lots = math.ceil((lots - 1e-9) / vstep) * vstep
                    lots = max(vmin, lots)
                    lots = min(lots, vmax)
                    usd_per_price = (tick_value / tick_size) * lots if tick_size else 0.0
                    target_dist = (target_usd / usd_per_price) if usd_per_price > 0 else 0.7 * atr_v
                    width = max(0.7 * atr_v, min(target_dist, 4.0 * atr_v))
                    spread = abs(float(row.get("ask") or 0) - float(row.get("bid") or 0))
                    buffer = max(0.05 * atr_v, spread * 2.0, 0.0)
                    if side == "buy":
                        target_tp = price + width
                        sl = price - width + buffer
                    else:
                        target_tp = price - width
                        sl = price + width - buffer
                    opposite_before = next((p for p in state["positions"] if _base(p.get("symbol")) == base and str(p.get("side") or "").lower() != side and float(p.get("targetTp") or 0) > 0), None)
                    if opposite_before:
                        opp_tp = float(opposite_before.get("targetTp") or 0)
                        sl = opp_tp - buffer if side == "sell" else opp_tp + buffer
                    risk_per_lot = abs(price - sl) * (tick_value / tick_size) if tick_size else abs(price - sl) * 100000.0
                    actual_risk = risk_per_lot * lots
                    broken = False
                    if actual_risk > equity * 0.05 and risk_per_lot > 0:
                        max_lots = (equity * 0.05) / risk_per_lot
                        if vstep > 0:
                            max_lots = math.floor((max_lots + 1e-9) / vstep) * vstep
                        max_lots = max(vmin, min(vmax, max_lots))
                        if max_lots < vmin or max_lots <= 0:
                            state["decisions"].append({
                                "id": "h%d" % int(time.time() * 1000), "at": _iso(), "action": "hold",
                                "symbol": base, "side": side, "confidence": float(row.get("confidence") or 0),
                                "price": price, "volume": lots,
                                "reason": "Hedge skipped: even min lot risk exceeds the 5.0%% flip cap." % (),
                            })
                            continue
                        lots = max_lots
                        broken = True
                        actual_risk = risk_per_lot * lots
                        usd_per_price = (tick_value / tick_size) * lots if tick_size else 0.0
                        target_dist = (target_usd / usd_per_price) if usd_per_price > 0 else 0.7 * atr_v
                        width = max(0.7 * atr_v, min(target_dist, 4.0 * atr_v))
                        if opposite_before:
                            opp_tp = float(opposite_before.get("targetTp") or 0)
                            sl = opp_tp - buffer if side == "sell" else opp_tp + buffer
                        else:
                            sl = price - width + buffer if side == "buy" else price + width - buffer
                        target_tp = price + width if side == "buy" else price - width
                        risk_per_lot = abs(price - sl) * (tick_value / tick_size) if tick_size else abs(price - sl) * 100000.0
                        actual_risk = risk_per_lot * lots
                    try:
                        with self._locked():
                            res = self.pool.open_position(creds, {
                                "symbol": row.get("symbol"), "side": side, "volume": lots,
                                "stopLoss": round(sl, _digits(price)), "takeProfit": 0,
                                "magic": MAGIC, "comment": "flip-hedge",
                            })
                        order = res.get("order") or {}
                        ticket = order.get("ticket")
                        fill = float(order.get("price") or price)
                    except Exception as e:
                        state["error"] = "hedge open failed: %s" % e
                        state["decisions"].append({
                            "id": "he%d" % int(time.time() * 1000), "at": _iso(), "action": "hold",
                            "symbol": base, "side": side, "confidence": float(row.get("confidence") or 0),
                            "price": price, "volume": lots, "reason": "Hedge execution rejected: %s" % e,
                        })
                        continue
                    pairs = fr.setdefault("pairs", {})
                    pair["lot"] = lots
                    pair["ladderLot"] = round(ladder_lot, 4)
                    pair["requiredLot"] = round(required_lot, 4)
                    pair["lotAdded"] = round(max(0.0, lots - ladder_lot), 4)
                    pair["lotBroken"] = bool(broken)
                    pair["missedProfit"] = missed
                    pairs[pair_key] = pair
                    open_count += 1
                    open_sides.add((base, side))
                    state["positions"].append({
                        "ticket": ticket, "symbol": row.get("symbol"), "side": side, "volume": lots,
                        "openPrice": fill, "currentPrice": fill, "profit": 0,
                        "sl": round(sl, _digits(price)), "tp": None, "targetTp": round(target_tp, _digits(price)),
                        "tpArmed": False, "openedAt": _iso(),
                        "reason": "Flip hedge %s: SL %.5f near opposite TP, virtual TP %.5f." % (side.upper(), sl, target_tp),
                        "contributions": {}, "riskAmount": actual_risk, "flip": True, "hedge": True, "pairKey": pair_key,
                    })
                    state["decisions"].append({
                        "id": "ho%d" % int(time.time() * 1000), "at": _iso(), "action": side,
                        "symbol": base, "side": side, "confidence": float(row.get("confidence") or 0),
                        "price": fill, "volume": lots,
                        "reason": "Flip hedge: %s %.2f lots (ladder %.2f, added %.2f for $%.2f demand%s); SL %.5f near opposite TP; virtual TP %.5f; ladder step %d." % (side.upper(), lots, ladder_lot, max(0.0, lots - ladder_lot), target_usd, ", risk-capped" if broken else "", sl, target_tp, step),
                    })
                    opposite = next((p for p in state["positions"] if _base(p.get("symbol")) == base and str(p.get("side") or "").lower() != side and not p.get("tpArmed")), None)
                    if opposite:
                        opp_sl = target_tp + buffer if side == "sell" else target_tp - buffer
                        try:
                            with self._locked():
                                self.pool.modify_position(creds, {"ticket": opposite.get("ticket"), "stopLoss": round(opp_sl, _digits(price)), "takeProfit": 0})
                            opposite["sl"] = round(opp_sl, _digits(price))
                            state["decisions"].append({
                                "id": "ha%d" % int(time.time() * 1000), "at": _iso(), "action": "align",
                                "symbol": base, "side": opposite.get("side"), "confidence": float(row.get("confidence") or 0),
                                "price": price, "volume": opposite.get("volume"),
                                "reason": "Hedge alignment: opposite SL moved to %.5f near the new %s TP." % (opp_sl, side.upper()),
                            })
                        except Exception as e:
                            state["error"] = "hedge align failed: %s" % e
                    open_count += 1
                    open_sides.add((base, side))
                    open_symbols.add(base)
                    state.setdefault("lastTradeAt", {})[row.get("symbol")] = now

        for cand in rows:
            if not cand.get("ok") or not cand.get("side"):
                continue
            sym = cand.get("symbol")
            if halted:
                break
            if open_count >= int(params.get("maxPositions", 3)):
                break
            if sym in open_symbols:
                continue
            price = float(cand.get("price") or 0)
            if flip and _base(sym) in recent_losses:
                key = "%s:reentry" % _base(sym)
                last_note = float((state.get("lastReentryHoldAt") or {}).get(key) or 0)
                if now - last_note > 60:
                    state["decisions"].append({
                        "id": "q%d" % int(time.time() * 1000), "at": _iso(), "action": "hold",
                        "symbol": sym, "side": None, "confidence": cand.get("confidence"),
                        "price": price, "volume": None,
                        "reason": "Flip recovery: last %s trade lost; waiting 60 seconds." % _base(sym),
                    })
                    state.setdefault("lastReentryHoldAt", {})[key] = now
                continue
            if focus_available and focus_set and _base(sym) not in focus_set and non_focus_open >= 2:
                continue
            if now < cooldown_until:
                continue
            last_at = float((state.get("lastTradeAt") or {}).get(sym) or 0)
            if now - last_at < float(params.get("cooldownSec", 180)):
                continue
            if profit:
                if loss_streak >= 2 or today_trades >= 4:
                    break
                if float(cand.get("flowQuality") or 0) < 0.25:
                    continue
                if float(cand.get("flowAlignment") or 0) < 0:
                    continue
                if float(cand.get("patternScore") or 0) < 0.25:
                    continue
                p_name = str(cand.get("patternName") or "")
                p_stat = (state.get("patternStats") or {}).get(p_name) or {}
                if int(p_stat.get("trades") or 0) >= 5 and float(p_stat.get("pnl") or 0) <= 0:
                    continue
                atr_gate = float(cand.get("atr") or 0)
                if atr_gate <= 0 or float(cand.get("spread") or 0) > float(params.get("maxSpreadAtr", 0.12)) * atr_gate:
                    continue
                if str(cand.get("regime") or "") not in ("trend", "volatile"):
                    continue
                if float(cand.get("adx") or 0) < 18:
                    continue
                board_bias = str((komz_state.get("factorBoard") or {}).get("bias") or "neutral")
                if board_bias in ("buy", "sell") and board_bias != str(cand.get("side") or ""):
                    continue
            spec = (market.get(sym) or {}).get("spec") or {}
            price = float(cand.get("ask") or cand.get("bid") or price)
            sl = float(cand.get("sl") or 0)
            sl_dist = abs(price - sl)
            lots = position_size(equity, risk_pct, sl_dist, spec)
            if flip:
                base_sym = _base(sym)
                side_key = str(cand.get("side") or "").upper()
                pair_key = "%s:%s" % (base_sym, side_key)
                pairs = fr.setdefault("pairs", {})
                pair = pairs.setdefault(pair_key, {"step": 0, "lot": seq[0], "deficit": 0.0, "missedProfit": float(fr.get("missedProfit") or 0.0)})
                step = max(0, int(pair.get("step") or 0))
                seq = ensure_flip_sequence(fr, step)
                ladder_lot = float(seq[step])
                vmin = float(spec.get("volumeMin") or 0.01)
                vstep = float(spec.get("volumeStep") or 0.01)
                vmax = float(spec.get("volumeMax") or ladder_lot)
                atr_v = float(cand.get("atr") or 0)
                base_tp_dist = max(0.7 * atr_v, abs(price - float(cand.get("tp") or price)))
                deficit = float(pair.get("deficit") or 0)
                missed = float(pair.get("missedProfit") or fr.get("missedProfit") or 0.0)
                target_usd = max(missed, deficit + missed)
                tick_value = float(spec.get("tickValue") or 1.0)
                tick_size = float(spec.get("tickSize") or 0.00001)
                required_lot = flip_required_lot(target_usd, base_tp_dist, tick_value, tick_size) if deficit > 0 else 0.0
                lots = max(ladder_lot, required_lot)
                if vstep > 0:
                    lots = math.ceil((lots - 1e-9) / vstep) * vstep
                lots = max(vmin, lots)
                lots = min(lots, vmax)
                usd_per_price = (tick_value / tick_size) * lots if tick_size else 0.0
                target_dist = (target_usd / usd_per_price) if usd_per_price > 0 else base_tp_dist
                max_tp = max(base_tp_dist, atr_v * 4.0)
                tp_dist = max(base_tp_dist, min(target_dist, max_tp))
                cand["tp"] = price + tp_dist if cand.get("side") == "buy" else price - tp_dist
                pair["lot"] = lots
                pair["ladderLot"] = round(ladder_lot, 4)
                pair["requiredLot"] = round(required_lot, 4)
                pair["lotAdded"] = round(max(0.0, lots - ladder_lot), 4)
                pair["missedProfit"] = missed
                pairs[pair_key] = pair
            if lots <= 0:
                continue
            tick_value = float(spec.get("tickValue") or 1.0)
            tick_size = float(spec.get("tickSize") or 0.00001)
            risk_per_lot = sl_dist * (tick_value / tick_size) if tick_size else sl_dist * 100000.0
            actual_risk = risk_per_lot * lots
            risk_cap = max(0.0, equity * (0.05 if flip else 0.02))
            if actual_risk > risk_cap:
                last_risk = float((state.get("lastRiskHoldAt") or {}).get(sym) or 0)
                if now - last_risk > 300:
                    state["decisions"].append({
                        "id": "r%d" % int(time.time() * 1000), "at": _iso(), "action": "hold",
                        "symbol": sym, "side": None, "confidence": cand.get("confidence"),
                        "price": price, "volume": lots,
                        "reason": "Min-lot risk $%.2f is %.1f%% of equity, above the %.1f%% cap. Skipping %s." % (
                            actual_risk, (actual_risk / equity * 100.0) if equity else 0, 5.0 if flip else 2.0, sym),
                    })
                    state.setdefault("lastRiskHoldAt", {})[sym] = now
                continue
            review = None
            if use_brain and reviews_done < 2 and not flip:
                reviews_done += 1
                try:
                    review = self.brain.review_entry(account_id, cfg, state, cand, market, info, progress=progress)
                except Exception as e:
                    self.log("komz review failed: %s" % e)
                if review:
                    komz_state["lastReview"] = review
                    komz_state["council"] = review.get("council") or self.brain.council.status()
                    if review.get("action") == "reject":
                        state["decisions"].append({
                            "id": "k%d" % int(time.time() * 1000), "at": _iso(), "action": "hold",
                            "symbol": sym, "side": None, "confidence": cand.get("confidence"),
                            "price": price, "volume": lots,
                            "reason": "Komz veto: %s%s" % (
                                review.get("reason") or "setup rejected",
                                (" · " + review.get("riskNote")) if review.get("riskNote") else "",
                            ),
                        })
                        continue
                    cand["komzReview"] = review
                    mult = max(0.25, min(1.0, float(review.get("sizeMultiplier") or 1.0)))
                    if mult < 1.0:
                        scaled = position_size(equity, risk_pct * mult, sl_dist, spec)
                        if scaled > 0:
                            lots = scaled
            if dry:
                state["decisions"].append({
                    "id": "d%d" % int(time.time() * 1000), "at": _iso(), "action": "hold",
                    "symbol": sym, "side": cand.get("side"), "confidence": cand.get("confidence"),
                    "price": price, "volume": lots,
                    "reason": "Dry run: would %s %.2f lots. %s" % (cand.get("side").upper(), lots, " ".join(cand.get("reasons") or [])),
                })
                continue
            ticket = None
            try:
                with self._locked():
                    res = self.pool.open_position(creds, {
                        "symbol": sym,
                        "side": cand.get("side"),
                        "volume": lots,
                        "stopLoss": cand.get("sl"),
                        "takeProfit": 0 if flip else cand.get("tp"),
                        "magic": MAGIC,
                        "comment": "ai-evolve",
                    })
                order = res.get("order") or {}
                ticket = order.get("ticket")
                fill = float(order.get("price") or price)
            except Exception as e:
                state["error"] = "open failed: %s" % e
                state["decisions"].append({
                    "id": "e%d" % int(time.time() * 1000), "at": _iso(), "action": "hold",
                    "symbol": sym, "side": cand.get("side"), "confidence": cand.get("confidence"),
                    "price": price, "volume": lots, "reason": "Execution rejected: %s" % e,
                })
                continue
            risk_amount = abs(fill - sl) * (float(spec.get("tickValue") or 1.0) / float(spec.get("tickSize") or 1.0)) * lots if spec else abs(fill - sl) * lots * 100000.0
            decision = {
                "id": "o%d" % int(time.time() * 1000), "at": _iso(),
                "action": cand.get("side"), "symbol": sym, "side": cand.get("side"),
                "confidence": cand.get("confidence"), "price": fill, "volume": lots,
                "reason": " ".join(cand.get("reasons") or []) + (
                    (" · Komz: " + str((cand.get("komzReview") or {}).get("reason") or "")) if cand.get("komzReview") else ""
                ) + (
                    (" · Council: " + str(((cand.get("komzReview") or {}).get("council") or {}).get("summary") or "")) if (cand.get("komzReview") or {}).get("council") else ""
                ),
                "ticket": ticket, "outcome": None, "pnl": None,
            }
            state["decisions"].append(decision)
            state["positions"].append({
                "ticket": ticket, "symbol": sym, "side": cand.get("side"), "volume": lots,
                "openPrice": fill, "currentPrice": fill, "profit": 0, "sl": cand.get("sl"),
                "tp": None if flip else cand.get("tp"), "targetTp": cand.get("tp"), "tpArmed": False,
                "openedAt": _iso(), "reason": decision["reason"],
                "contributions": cand.get("contributions") or {}, "riskAmount": risk_amount,
                "decisionId": decision["id"], "flip": bool(flip),
                "pattern": cand.get("patternName"), "patternScore": cand.get("patternScore"),
            })
            state.setdefault("lastTradeAt", {})[sym] = now
            open_count += 1
            open_symbols.add(sym)
            if focus_set and _base(sym) not in focus_set:
                non_focus_open += 1

        # hold note for the top candidate
        top = rows[0] if rows else None
        if top and not top.get("side") and top.get("holdReason"):
            key = "%s:%s" % (top.get("symbol"), top.get("holdReason"))
            last_hold = float((state.get("lastHoldAt") or {}).get(top.get("symbol")) or 0)
            if state.get("lastHoldKey") != key and now - last_hold > 60:
                state["decisions"].append({
                    "id": "h%d" % int(time.time() * 1000), "at": _iso(), "action": "hold",
                    "symbol": top.get("symbol"), "side": None, "confidence": top.get("confidence"),
                    "price": top.get("price"), "volume": None,
                    "reason": top.get("holdReason") + ". " + " ".join((top.get("reasons") or [])[:2]),
                })
                state["lastHoldKey"] = key
                state.setdefault("lastHoldAt", {})[top.get("symbol")] = now

        if use_brain:
            try:
                note = self.brain.research(account_id, cfg, state, market, rows, info)
                if note:
                    state.setdefault("thoughts", []).append({
                        "at": _iso(),
                        "text": "Komz research: %s · %s. %s" % (
                            note.get("regime"), note.get("bias"), " ".join((note.get("notes") or [])[:2])),
                    })
            except Exception as e:
                self.log("komz research failed: %s" % e)
            try:
                komz_state.update(self.brain.status())
            except Exception:
                pass

        # stats + learning
        closed = state.get("closed") or []
        stats = compute_stats(closed, float(state.get("startingEquity") or equity))
        state["stats"] = stats
        pattern_stats: Dict[str, Dict[str, Any]] = {}
        for c in closed:
            name = str(c.get("pattern") or "unclassified")
            bucket = pattern_stats.setdefault(name, {"trades": 0, "wins": 0, "pnl": 0.0, "r": 0.0})
            bucket["trades"] += 1
            pnl_c = float(c.get("pnl") or 0)
            bucket["pnl"] += pnl_c
            bucket["r"] += float(c.get("r") or 0)
            if pnl_c > 0:
                bucket["wins"] += 1
        for bucket in pattern_stats.values():
            bucket["winRate"] = round(bucket["wins"] / bucket["trades"], 3) if bucket["trades"] else 0.0
            bucket["pnl"] = round(bucket["pnl"], 2)
            bucket["avgR"] = round(bucket["r"] / bucket["trades"], 3) if bucket["trades"] else 0.0
        state["patternStats"] = pattern_stats
        params, learn_note = adapt(params, stats, 0.55 if profit else 0.05 if flip else 0.08 if aggressive else 0.15)
        if profit:
            params["riskPct"] = min(float(params.get("riskPct") or 0.5), 0.5)
            params["threshold"] = max(float(params.get("threshold") or 0.55), 0.55)
            params["minConfidence"] = max(float(params.get("minConfidence") or 0.55), 0.55)
            params["maxPositions"] = 2
            params["flip"] = False
        generation = int(stats.get("trades") or 0) // 20
        state["generation"] = generation
        state["params"] = params
        state["weights"] = weights
        state["learning"] = {
            "weights": weights,
            "eta": float(state.get("learning", {}).get("eta") or 0.18),
            "threshold": params.get("threshold"),
            "riskPct": params.get("riskPct"),
            "generation": generation,
            "note": learn_note,
            "lastUpdate": _iso(),
        }

        floating = sum(float(p.get("profit") or 0) for p in state["positions"])
        realized = sum(float(c.get("pnl") or 0) for c in closed)
        state["floating"] = round(floating, 2)
        state["aiPnl"] = round(realized + floating, 2)
        state["equity"] = round(equity, 2)
        state["balance"] = round(float(info.get("balance") or 0), 2)
        confirmed = round(sum(float(c.get("pnl") or 0) for c in closed), 2)
        additions = round(sum(max(0.0, float(c.get("pnl") or 0)) for c in closed), 2)
        losses = round(sum(min(0.0, float(c.get("pnl") or 0)) for c in closed), 2)
        curve = list(state.get("equityCurve") or [])
        prev_equity = float(curve[-1].get("equity") or equity) if curve else equity
        curve.append({"at": _iso(), "equity": round(equity, 2), "balance": round(float(info.get("balance") or 0), 2),
                      "confirmedPnl": confirmed, "delta": round(equity - prev_equity, 2)})
        state["equityCurve"] = curve[-240:]
        state["confirmedProfit"] = confirmed
        state["confirmedAdditions"] = additions
        state["confirmedLosses"] = losses
        ledger = []
        running = 0.0
        for c in closed[-50:]:
            running += float(c.get("pnl") or 0)
            ledger.append({"at": c.get("closedAt"), "symbol": c.get("symbol"), "side": c.get("side"),
                           "pnl": round(float(c.get("pnl") or 0), 2), "cumulative": round(running, 2),
                           "confirmed": True})
        state["profitLedger"] = ledger
        peak_now = float(state.get("peakEquity") or equity or 0)
        state["recoveryStatus"] = {
            "targetEquity": round(peak_now, 2), "equity": round(equity, 2),
            "deficit": round(max(0.0, peak_now - equity), 2),
            "recovered": bool(peak_now and equity >= peak_now and not state["positions"]),
            "confirmedProfit": confirmed, "confirmedAdditions": additions, "confirmedLosses": losses,
            "note": "Full recovery is confirmed only when equity >= peak and all positions are closed.",
        }
        state["market"] = [
            {k: r.get(k) for k in ("symbol", "price", "bid", "ask", "spread", "rsi", "atr", "atrPct", "trend", "regime", "score", "confidence", "side", "holdReason", "flowAvailable", "flowScore", "flowAlignment", "flowQuality", "flowPressure", "depthImbalance", "tickDeltaRatio", "bidDepth", "askDepth", "patterns", "patternScore", "patternName")}
            for r in rows if r.get("ok")
        ]
        if top and top.get("ok"):
            state["thesis"] = {
                "symbol": top.get("symbol"),
                "bias": top.get("side") or "flat",
                "confidence": top.get("confidence"),
                "regime": top.get("regime"),
                "summary": top.get("summary"),
                "komz": komz_state.get("lastReview"),
                "at": _iso(),
            }

        thoughts = state.get("thoughts") or []
        thoughts.append({"at": _iso(), "text": "Cycle %d · scanned %d symbols · best %s %.2f · %s" % (
            state["cycle"], len(rows), top.get("symbol") if top else "—", float(top.get("confidence") or 0) if top else 0, "halted" if halted else "active")})
        if state["positions"]:
            thoughts.append({"at": _iso(), "text": "Managing %d AI position(s), floating %+.2f." % (len(state["positions"]), floating)})
        if state.get("error"):
            thoughts.append({"at": _iso(), "text": "Risk note: %s" % state["error"]})
        state["thoughts"] = thoughts[-30:]
        state["decisions"] = state["decisions"][-60:]
        state["nextReviewAt"] = _iso(now + self.interval)
        self._post(account_id, state)
        if self.on_account:
            try:
                self.on_account(account_id, creds)
            except Exception as e:
                self.log("ai account callback failed: %s" % e)

    def _serialize_position(self, pos: Dict[str, Any], reason: Optional[str]) -> Dict[str, Any]:
        return {
            "ticket": pos.get("ticket"), "symbol": pos.get("symbol"),
            "side": str(pos.get("side") or "").lower(), "volume": pos.get("volume"),
            "openPrice": pos.get("openPrice"), "currentPrice": pos.get("currentPrice"),
            "profit": pos.get("profit"), "sl": pos.get("stopLoss"), "tp": pos.get("takeProfit"),
            "openedAt": pos.get("openedAt"), "reason": reason, "contributions": {}, "riskAmount": 1.0,
        }


class _NullLock:
    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False
