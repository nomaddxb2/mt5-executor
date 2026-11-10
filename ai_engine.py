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
    for suffix in (".RAW", ".M", ".Z", ".C", "M", "RAW", "Z"):
        if s.endswith(suffix) and len(s) > len(suffix) + 2:
            return s[: -len(suffix)]
    return s


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
def analyze_symbol(symbol: str, tfs: Dict[str, List[Dict[str, Any]]], quote: Dict[str, Any],
                   weights: Dict[str, float], params: Dict[str, Any],
                   flow: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    m15 = [c for c in (tfs.get("M15") or []) if c]
    if len(m15) < 60:
        m15 = [c for c in (tfs.get("M5") or []) if c]
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
            confidence = _clamp(confidence + flow_alignment * 0.10, 0.0, 1.0)
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
        "flowPressure": flow.get("pressure"),
        "depthImbalance": flow.get("depthImbalance"),
        "tickDeltaRatio": flow.get("tickDeltaRatio"),
        "bidDepth": flow.get("bidDepth"),
        "askDepth": flow.get("askDepth"),
        "flowNotes": flow.get("notes") or [],
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


def adapt(params: Dict[str, Any], stats: Dict[str, Any]) -> (Dict[str, Any], str):
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
        p["threshold"] = round(_clamp(float(p["threshold"]), 0.15, 0.70), 4)
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
    def __init__(self, pool, api, log, lock=None, interval: float = 5.0, on_account=None, brain=None):
        self.pool = pool
        self.api = api
        self.log = log
        self.lock = lock
        self.interval = interval
        self.on_account = on_account
        self.brain = brain
        self.accounts: List[Dict[str, Any]] = []
        self.last_refresh = 0.0
        self.cycle = 0

    def _locked(self):
        return self.lock if self.lock is not None else _NullLock()

    def refresh(self, force: bool = False) -> None:
        if not force and self.accounts and time.time() - self.last_refresh < 5:
            return
        try:
            payload = self.api("/v1/worker/ai/accounts", {})
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
        for key in ("riskPct", "maxPositions", "minConfidence", "maxDailyLossPct"):
            if cfg.get(key) is not None:
                params[key] = cfg[key]
        if cfg.get("minConfidence") is not None:
            params["threshold"] = min(float(params.get("threshold", 0.15)), float(params["minConfidence"]))
        weights = state.get("weights") or dict(DEFAULT_WEIGHTS)
        symbols = [str(s).upper() for s in (cfg.get("symbols") or [])][:6]
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
                for tf in ("M5", "M15", "H1"):
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

        rows = [analyze_symbol(sym, m["tfs"], m["quote"], weights, params, m.get("flow")) for sym, m in market.items()]
        rows.sort(key=lambda r: r.get("confidence") or 0, reverse=True)
        by_symbol = {r.get("symbol"): r for r in rows if r.get("ok")}

        now = time.time()
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
            }
            closed_now.append(record)
            state["closed"].append(record)
            state["closed"] = state["closed"][-100:]
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

            close_reason = ""
            age_h = 0.0
            try:
                opened = datetime.fromisoformat(str(pos.get("openedAt")).replace("Z", "+00:00"))
                age_h = (datetime.now(timezone.utc) - opened).total_seconds() / 3600.0
            except Exception:
                age_h = 0.0
            if age_h >= float(params.get("timeStopHours", 24)):
                close_reason = "Time stop: position open %.1fh" % age_h
            elif row.get("side") and row.get("side") != side and float(row.get("confidence") or 0) >= max(0.45, float(params.get("threshold", 0.40))):
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
                          "closedAt": _iso(), "reason": close_reason}
                state["closed"].append(record)
                state["closed"] = state["closed"][-100:]
                state["decisions"].append({
                    "id": "x%d" % int(time.time() * 1000), "at": _iso(), "action": "close",
                    "symbol": pos.get("symbol"), "side": side, "confidence": float(row.get("confidence") or 0),
                    "price": price, "volume": pos.get("volume"),
                    "reason": "%s · locked %+.2fR." % (close_reason, reward),
                    "ticket": pos.get("ticket"), "outcome": "win" if pnl > 0 else "loss" if pnl < 0 else "flat",
                    "pnl": round(pnl, 2),
                })
                continue

            # trailing stop / break-even
            if not dry and atr_v > 0 and open_price > 0 and price > 0:
                move = (price - open_price) if side == "buy" else (open_price - price)
                cur_sl = float(pos.get("stopLoss") or 0)
                if move >= float(params.get("trailAt", 1.2)) * atr_v:
                    new_sl = price - float(params.get("trailDistance", 1.2)) * atr_v if side == "buy" else price + float(params.get("trailDistance", 1.2)) * atr_v
                    better = (side == "buy" and (cur_sl == 0 or new_sl > cur_sl + 1e-9)) or (side == "sell" and (cur_sl == 0 or new_sl < cur_sl - 1e-9))
                    if better:
                        try:
                            with self._locked():
                                self.pool.modify_position(creds, {"ticket": pos.get("ticket"), "stopLoss": round(new_sl, _digits(price)), "takeProfit": pos.get("takeProfit") or 0})
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
            kept.append(entry)

        state["positions"] = kept

        # daily loss guard
        today = datetime.now(timezone.utc).date().isoformat()
        today_pnl = sum(float(c.get("pnl") or 0) for c in state["closed"] if str(c.get("closedAt", ""))[:10] == today)
        state["todayPnl"] = round(today_pnl, 2)
        loss_limit = -abs(equity * float(params.get("maxDailyLossPct", 3)) / 100.0)
        halted = bool(equity and today_pnl <= loss_limit)
        state["halted"] = halted
        cooldown_until = float(state.get("cooldownUntil") or 0)

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
        komz_state["recovery"] = {
            "active": risk_mult < 1.0,
            "drawdownPct": round(drawdown_pct, 2),
            "riskMultiplier": risk_mult,
            "peakEquity": round(peak, 2),
        }
        start_equity = float(state.get("startingEquity") or equity or 0)
        komz_state["compounding"] = {
            "active": bool(start_equity and equity > start_equity),
            "startingEquity": round(start_equity, 2),
            "gainPct": round(((equity / start_equity) - 1.0) * 100.0, 2) if start_equity else 0.0,
        }
        risk_pct = float(params.get("riskPct", 0.5)) * risk_mult

        # open new positions
        open_count = len(state["positions"])
        open_symbols = {_base(p.get("symbol")) for p in state["positions"]}
        reviews_done = 0
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
            if now < cooldown_until:
                continue
            last_at = float((state.get("lastTradeAt") or {}).get(sym) or 0)
            if now - last_at < float(params.get("cooldownSec", 180)):
                continue
            spec = (market.get(sym) or {}).get("spec") or {}
            price = float(cand.get("ask") if cand.get("side") == "buy" else cand.get("bid"))
            sl = float(cand.get("sl") or 0)
            sl_dist = abs(price - sl)
            lots = position_size(equity, risk_pct, sl_dist, spec)
            if lots <= 0:
                continue
            tick_value = float(spec.get("tickValue") or 1.0)
            tick_size = float(spec.get("tickSize") or 0.00001)
            risk_per_lot = sl_dist * (tick_value / tick_size) if tick_size else sl_dist * 100000.0
            actual_risk = risk_per_lot * lots
            risk_cap = max(0.0, equity * 0.02)
            if actual_risk > risk_cap:
                last_risk = float((state.get("lastRiskHoldAt") or {}).get(sym) or 0)
                if now - last_risk > 300:
                    state["decisions"].append({
                        "id": "r%d" % int(time.time() * 1000), "at": _iso(), "action": "hold",
                        "symbol": sym, "side": None, "confidence": cand.get("confidence"),
                        "price": price, "volume": lots,
                        "reason": "Min-lot risk $%.2f is %.1f%% of equity, above the 2.0%% hard cap. Skipping %s." % (
                            actual_risk, (actual_risk / equity * 100.0) if equity else 0, sym),
                    })
                    state.setdefault("lastRiskHoldAt", {})[sym] = now
                continue
            review = None
            if use_brain and reviews_done < 2:
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
                        "takeProfit": cand.get("tp"),
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
                "tp": cand.get("tp"), "openedAt": _iso(), "reason": decision["reason"],
                "contributions": cand.get("contributions") or {}, "riskAmount": risk_amount,
                "decisionId": decision["id"],
            })
            state.setdefault("lastTradeAt", {})[sym] = now
            open_count += 1
            open_symbols.add(sym)

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
        params, learn_note = adapt(params, stats)
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
        state["market"] = [
            {k: r.get(k) for k in ("symbol", "price", "bid", "ask", "spread", "rsi", "atr", "atrPct", "trend", "regime", "score", "confidence", "side", "holdReason", "flowAvailable", "flowScore", "flowAlignment", "flowPressure", "depthImbalance", "tickDeltaRatio", "bidDepth", "askDepth")}
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
