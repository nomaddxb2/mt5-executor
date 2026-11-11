"""MetaTrader 5 terminal pool used by the GitHub Actions execution worker."""

from __future__ import annotations

import glob
import math
import os
import re
import subprocess
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

try:
    import MetaTrader5 as mt5
except Exception:  # pragma: no cover - Linux and dry runs
    mt5 = None

PRIMARY_TERMINAL_PATH = os.environ.get(
    "MT5_TERMINAL_PATH", r"C:\mt5\terminal64.exe"
)
ALT_TERMINAL_PATH = os.environ.get(
    "MT5_TERMINAL_ALT_PATH", r"C:\mt5b\terminal64.exe"
)
ALT_SERVER_MATCH = str(os.environ.get("MT5_TERMINAL_ALT_MATCH") or "").strip().lower()
DEFAULT_DEVIATION = int(os.environ.get("MT5_DEVIATION", "30"))
WATCHLIST = [
    s.strip().upper()
    for s in os.environ.get(
        "MT5_WATCHLIST", "EURUSD,XAUUSD,BTCUSD,GBPUSD,USDJPY,US30,NAS100"
    ).split(",")
    if s.strip()
]


class Mt5Error(RuntimeError):
    def __init__(self, message: str, code: str = "mt5_error", details: Any = None):
        super().__init__(message)
        self.code = code
        self.details = details


def _iso(ts: Optional[int]) -> Optional[str]:
    if not ts:
        return None
    return datetime.fromtimestamp(int(ts), tz=timezone.utc).isoformat().replace("+00:00", "Z")


def _num(value: Any, digits: int = 2) -> Any:
    if value is None:
        return None
    try:
        return round(float(value), digits)
    except Exception:
        return value


def _clamp(value: Any, lo: float, hi: float) -> float:
    try:
        n = float(value)
    except (TypeError, ValueError):
        return lo
    return max(lo, min(hi, n))


def _timeframe(name: str):
    if mt5 is None:
        return None
    table = {
        "M1": mt5.TIMEFRAME_M1,
        "M5": mt5.TIMEFRAME_M5,
        "M15": mt5.TIMEFRAME_M15,
        "M30": mt5.TIMEFRAME_M30,
        "H1": mt5.TIMEFRAME_H1,
        "H4": mt5.TIMEFRAME_H4,
        "D1": mt5.TIMEFRAME_D1,
    }
    return table.get(str(name or "M15").upper())


def _side(order_type: int) -> str:
    buy_types = {0, 2, 4}
    if mt5 is not None:
        buy_types = {
            mt5.ORDER_TYPE_BUY,
            mt5.ORDER_TYPE_BUY_LIMIT,
            mt5.ORDER_TYPE_BUY_STOP,
        }
    return "buy" if order_type in buy_types else "sell"


_SYMBOL_SUFFIXES = [
    "m", ".m", ".raw", "m.raw", ".m.raw", "z", ".z", ".a", ".pro", ".ecn", ".std",
    ".c", ".r", ".cash", ".mini", ".micro", ".plus", ".prime", ".zero", ".d", ".s",
    ".t", ".fx", ".spot", ".index", ".cfd", "-5", "#", "_i", ".i", ".sv", "_raw", "-raw",
    "c", ".cent", "cent", "_cent", "-cent",
]
_SYMBOL_PREFIXES = ["#", "x", "cfd", "fx", "mt5"]
_SYMBOL_ALIASES = {
    "XAUUSD": ["XAUUSD", "GOLD", "GOLDUSD", "GOLDSPOT", "XAU/USD"],
    "XAGUSD": ["XAGUSD", "SILVER", "XAG/USD"],
    "BTCUSD": ["BTCUSD", "BTCUSDT", "XBTUSD", "BITCOIN", "BTC"],
    "ETHUSD": ["ETHUSD", "ETHUSDT", "ETHEREUM"],
    "US30": ["US30", "DJ30", "DOW30", "DOWJONES", "USA30", "WS30"],
    "NAS100": ["NAS100", "USTEC", "US100", "TECH100", "NDX100"],
    "SPX500": ["SPX500", "US500", "SP500", "SPX"],
    "GER40": ["GER40", "DE40", "DAX40", "GER30", "DE30"],
    "UK100": ["UK100", "FTSE100", "FTSE"],
    "JP225": ["JP225", "JPN225", "NIKKEI225", "NIKKEI"],
    "WTI": ["WTI", "USOIL", "CRUDEOIL", "CL"],
    "USOIL": ["USOIL", "WTI", "CRUDEOIL"],
}


def _norm_symbol(value: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", str(value or "").upper())


def _parse_symbol_map(raw: str) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for pair in str(raw or "").split(","):
        if ":" not in pair:
            continue
        key, value = pair.split(":", 1)
        key, value = key.strip(), value.strip()
        if key and value:
            out[_norm_symbol(key)] = value
    return out


SYMBOL_MAP = _parse_symbol_map(os.environ.get("MT5_SYMBOL_MAP", ""))


def _terminal_for(server: Any) -> str:
    if ALT_SERVER_MATCH and ALT_SERVER_MATCH in str(server or "").lower():
        return ALT_TERMINAL_PATH
    return PRIMARY_TERMINAL_PATH


def _symbol_score(requested: str, broker: str, aliases: List[str]) -> int:
    req = _norm_symbol(requested)
    got = _norm_symbol(broker)
    if not req or not got:
        return 0
    for alias in aliases:
        a = _norm_symbol(alias)
        if not a:
            continue
        if got == a:
            return 100
        if got.startswith(a) and len(got) - len(a) <= 8:
            return 86
        if got.endswith(a) and len(got) - len(a) <= 6:
            return 82
        if a in got and len(got) <= len(a) + 8:
            return 60
    return 0


class TerminalPool:
    def __init__(self, dry_run: bool = False):
        self.dry_run = dry_run or os.environ.get("MT5_DRY_RUN") == "1"
        self.current: Optional[Dict[str, Any]] = None
        self.connected_at: Optional[float] = None
        self.terminal_path = PRIMARY_TERMINAL_PATH
        self._symbol_cache: Dict[str, Any] = {}
        self._symbol_rows_cache = None
        self._symbol_rows_at = 0.0

    # ---------------------------------------------------------------- session
    def connect(self, credentials: Dict[str, Any]) -> Dict[str, Any]:
        if self.dry_run:
            self.current = dict(credentials)
            self.connected_at = time.time()
            return {
                "connected": True,
                "mode": "dry-run",
                "login": str(credentials.get("login")),
                "server": credentials.get("server"),
                "latencyMs": 1,
                "terminal": {"name": "MT5 dry-run", "build": 0},
            }
        if mt5 is None:
            raise Mt5Error("MetaTrader5 package is not available on this host", "mt5_missing")
        key = (str(credentials.get("login")), str(credentials.get("server")))
        self.terminal_path = _terminal_for(credentials.get("server"))
        if self.current and (str(self.current.get("login")), str(self.current.get("server"))) == key:
            return self.status(credentials)
        self._symbol_cache.clear()
        self._symbol_rows_cache = None
        self._symbol_rows_at = 0.0
        try:
            mt5.shutdown()
        except Exception:
            pass
        started = time.time()
        timeout_ms = int(os.environ.get("MT5_CONNECT_TIMEOUT_MS", "30000"))
        last_code, last_message = -1, "unknown"
        ok = False
        print("[mt5-pool] attach login=%s server=%s timeout=%sms" % (key[0], key[1], timeout_ms), flush=True)
        # Attach to the terminal that the warm workflow already started.
        try:
            ok = mt5.initialize(path=self.terminal_path, timeout=timeout_ms, portable=True)
            print("[mt5-pool] initialize=%s error=%s" % (ok, mt5.last_error()), flush=True)
        except Exception as exc:
            print("[mt5-pool] initialize exception=%s" % exc, flush=True)
            ok = False
        if ok:
            ai = mt5.account_info()
            ti = mt5.terminal_info()
            same_login = ai is not None and str(ai.login) == str(credentials.get("login"))
            same_server = ai is not None and str(ai.server or "").lower() == str(credentials.get("server") or "").lower()
            trading = ti is not None and bool(getattr(ti, "trade_allowed", False))
            if not (same_login and same_server and trading):
                ok = False
                try:
                    mt5.shutdown()
                except Exception:
                    pass
        # Restart the terminal with an explicit startup config that enables AutoTrading.
        if not ok:
            print("[mt5-pool] attach failed, restarting terminal", flush=True)
            ok = self._restart_with_experts(credentials)
            print("[mt5-pool] restart result=%s" % ok, flush=True)
        for attempt in range(3):
            if ok:
                break
            ok = mt5.initialize(
                path=self.terminal_path,
                login=int(credentials.get("login")),
                password=str(credentials.get("password")),
                server=str(credentials.get("server")),
                timeout=timeout_ms,
                portable=True,
            )
            if ok:
                ti = mt5.terminal_info()
                if ti is not None and not bool(getattr(ti, "trade_allowed", False)):
                    ok = self._restart_with_experts(credentials)
                break
            error = mt5.last_error()
            if error:
                last_code, last_message = error
            try:
                mt5.shutdown()
            except Exception:
                pass
            if attempt < 2:
                time.sleep(10)
        if not ok:
            raise Mt5Error(
                "MT5 initialize failed: %s" % last_message,
                "mt5_connect_failed",
                {
                    "code": last_code,
                    "message": last_message,
                    "path": self.terminal_path,
                    "terminalRunning": self._terminal_running(),
                    "logs": self._tail_logs(),
                },
            )
        self.current = dict(credentials)
        self.connected_at = time.time()
        return {
            "connected": True,
            "mode": "live",
            "login": str(credentials.get("login")),
            "server": credentials.get("server"),
            "latencyMs": int((time.time() - started) * 1000),
            "terminal": self._terminal_info(),
        }

    def _terminal_running(self) -> bool:
        try:
            out = subprocess.run(
                ["tasklist", "/FI", "IMAGENAME eq terminal64.exe"],
                capture_output=True,
                text=True,
                timeout=15,
            )
            return "terminal64.exe" in (out.stdout or "")
        except Exception:
            return False

    def _launch_terminal(self) -> None:
        if not os.path.exists(self.terminal_path):
            raise Mt5Error("Terminal not found at %s" % self.terminal_path, "mt5_missing")
        subprocess.Popen(
            [self.terminal_path, "/portable"],
            cwd=os.path.dirname(self.terminal_path),
            close_fds=True,
        )
        time.sleep(5)

    def _tail_logs(self, limit: int = 20) -> List[str]:
        try:
            files = sorted(
                glob.glob(os.path.join(os.path.dirname(self.terminal_path), "logs", "*.log")),
                key=os.path.getmtime,
            )
            if not files:
                return []
            with open(files[-1], "r", encoding="utf-8", errors="replace") as handle:
                lines = handle.readlines()
            return [line.rstrip() for line in lines[-limit:]]
        except Exception:
            return []

    def _ensure(self, credentials: Dict[str, Any]) -> None:
        if self.dry_run:
            self.current = dict(credentials)
            return
        key = (str(credentials.get("login")), str(credentials.get("server")))
        if self.current and (str(self.current.get("login")), str(self.current.get("server"))) == key:
            return
        self.connect(credentials)

    def _terminal_info(self) -> Dict[str, Any]:
        if self.dry_run or mt5 is None:
            return {"name": "MT5 dry-run", "build": 0, "company": "dry-run"}
        info = mt5.terminal_info()
        if info is None:
            return {}
        d = info._asdict()
        return {
            "name": d.get("name"),
            "build": d.get("build"),
            "company": d.get("company"),
            "path": d.get("path"),
            "connected": d.get("connected"),
            "tradeAllowed": d.get("trade_allowed"),
        }

    # ---------------------------------------------------------------- reads
    def account(self, credentials: Dict[str, Any]) -> Dict[str, Any]:
        self._ensure(credentials)
        if self.dry_run:
            return self._dry_account(credentials)
        info = mt5.account_info()
        if info is None:
            code, message = mt5.last_error()
            raise Mt5Error("account_info failed: %s" % message, "mt5_account_failed", {"code": code})
        d = info._asdict()
        return {
            "login": str(d.get("login")),
            "server": d.get("server"),
            "name": d.get("name"),
            "currency": d.get("currency"),
            "balance": _num(d.get("balance")),
            "credit": _num(d.get("credit")),
            "equity": _num(d.get("equity")),
            "margin": _num(d.get("margin")),
            "freeMargin": _num(d.get("margin_free")),
            "marginLevel": _num(d.get("margin_level")),
            "leverage": d.get("leverage"),
            "profit": _num(d.get("profit")),
            "openPositions": len(mt5.positions_get() or []),
            "pendingOrders": len(mt5.orders_get() or []),
            "mode": "live",
        }

    def positions(self, credentials: Dict[str, Any]) -> List[Dict[str, Any]]:
        self._ensure(credentials)
        if self.dry_run:
            return []
        rows = mt5.positions_get() or []
        out = []
        for p in rows:
            d = p._asdict()
            out.append(
                {
                    "ticket": d.get("ticket"),
                    "symbol": d.get("symbol"),
                    "side": _side(d.get("type")),
                    "volume": _num(d.get("volume")),
                    "openPrice": _num(d.get("price_open"), 5),
                    "currentPrice": _num(d.get("price_current"), 5),
                    "stopLoss": _num(d.get("sl"), 5) or None,
                    "takeProfit": _num(d.get("tp"), 5) or None,
                    "profit": _num(d.get("profit")),
                    "swap": _num(d.get("swap")),
                    "commission": 0,
                    "comment": d.get("comment") or "",
                    "magic": d.get("magic") or 0,
                    "openedAt": _iso(d.get("time")),
                }
            )
        return out

    def orders(self, credentials: Dict[str, Any]) -> List[Dict[str, Any]]:
        self._ensure(credentials)
        if self.dry_run:
            return []
        rows = mt5.orders_get() or []
        out = []
        for o in rows:
            d = o._asdict()
            otype = d.get("type")
            if mt5 is not None and otype in (mt5.ORDER_TYPE_BUY_LIMIT, mt5.ORDER_TYPE_SELL_LIMIT):
                kind = "limit"
            else:
                kind = "stop"
            out.append(
                {
                    "ticket": d.get("ticket"),
                    "symbol": d.get("symbol"),
                    "side": _side(otype),
                    "type": kind,
                    "volume": _num(d.get("volume_current") or d.get("volume_initial")),
                    "price": _num(d.get("price_open"), 5),
                    "stopLoss": _num(d.get("sl"), 5) or None,
                    "takeProfit": _num(d.get("tp"), 5) or None,
                    "status": "placed",
                    "comment": d.get("comment") or "",
                    "createdAt": _iso(d.get("time_setup")),
                }
            )
        return out

    def history(self, credentials: Dict[str, Any], query: Dict[str, Any]) -> Dict[str, Any]:
        self._ensure(credentials)
        if self.dry_run:
            return {"deals": [], "count": 0, "mode": "dry-run"}
        limit = int(query.get("limit") or 100)
        frm = _parse_time(query.get("from"))
        to = _parse_time(query.get("to"))
        if frm is None:
            frm = datetime(2000, 1, 1, tzinfo=timezone.utc)
        if to is None:
            to = datetime.now(timezone.utc)
        rows = mt5.history_deals_get(frm, to) or []
        out = []
        for deal in rows[-limit:]:
            d = deal._asdict()
            out.append(
                {
                    "id": str(d.get("ticket")),
                    "ticket": d.get("position_id") or d.get("order"),
                    "symbol": d.get("symbol"),
                    "side": "buy" if d.get("type") in (0, 2) else "sell",
                    "volume": _num(d.get("volume")),
                    "openPrice": None,
                    "closePrice": _num(d.get("price"), 5),
                    "profit": _num(d.get("profit")),
                    "commission": _num(d.get("commission")),
                    "swap": _num(d.get("swap")),
                    "comment": d.get("comment") or "",
                    "openedAt": None,
                    "closedAt": _iso(d.get("time")),
                }
            )
        out.reverse()
        return {"deals": out, "count": len(out), "mode": "live"}

    def ticks(self, credentials: Dict[str, Any], symbol: str, count: int = 400) -> Dict[str, Any]:
        self._ensure(credentials)
        if self.dry_run or mt5 is None:
            return {"available": False, "ticks": 0}
        broker = self._resolve(symbol)
        mt5.symbol_select(broker, True)
        flags = getattr(mt5, "COPY_TICKS_ALL", 0)
        rows = mt5.copy_ticks_from_pos(broker, 0, int(count), flags)
        if rows is None or len(rows) == 0:
            return {"available": False, "ticks": 0, "brokerSymbol": broker}
        buy_flag = int(getattr(mt5, "TICK_FLAG_BUY", 32))
        sell_flag = int(getattr(mt5, "TICK_FLAG_SELL", 64))
        buy_vol = sell_vol = 0.0
        up = down = flat = 0
        last_mid = None
        for row in rows:
            if hasattr(row, "_asdict"):
                d = row._asdict()
            elif getattr(row, "dtype", None) is not None and getattr(row.dtype, "names", None):
                d = {key: row[key] for key in row.dtype.names}
            else:
                d = dict(row)
            volume = float(d.get("volume_real") or d.get("volume") or 0.0) or 1.0
            flag = int(d.get("flags") or 0)
            bid = float(d.get("bid") or 0.0)
            ask = float(d.get("ask") or 0.0)
            mid = (bid + ask) / 2.0 if bid and ask else float(d.get("last") or 0.0)
            if flag & buy_flag:
                buy_vol += volume
            elif flag & sell_flag:
                sell_vol += volume
            elif last_mid is not None and mid:
                if mid > last_mid:
                    buy_vol += volume
                    up += 1
                elif mid < last_mid:
                    sell_vol += volume
                    down += 1
                else:
                    flat += 1
            if mid:
                last_mid = mid
        total = buy_vol + sell_vol
        delta = buy_vol - sell_vol
        return {
            "available": total > 0,
            "brokerSymbol": broker,
            "ticks": int(len(rows)),
            "buyVolume": round(buy_vol, 2),
            "sellVolume": round(sell_vol, 2),
            "tickDelta": round(delta, 2),
            "tickDeltaRatio": round(delta / total, 4) if total else 0.0,
            "upTicks": up,
            "downTicks": down,
            "flatTicks": flat,
        }

    def depth(self, credentials: Dict[str, Any], symbol: str, levels: int = 8) -> Dict[str, Any]:
        self._ensure(credentials)
        if self.dry_run or mt5 is None:
            return {"available": False, "bids": [], "asks": []}
        broker = self._resolve(symbol)
        mt5.symbol_select(broker, True)
        added = False
        try:
            added = bool(mt5.market_book_add(broker))
            raw = mt5.market_book_get(broker) or []
        except Exception:
            raw = []
        finally:
            if added:
                try:
                    mt5.market_book_release(broker)
                except Exception:
                    pass
        buy_type = int(getattr(mt5, "BOOK_TYPE_BUY", 0))
        sell_type = int(getattr(mt5, "BOOK_TYPE_SELL", 1))
        bids: List[Dict[str, float]] = []
        asks: List[Dict[str, float]] = []
        for row in raw:
            if hasattr(row, "_asdict"):
                d = row._asdict()
            elif getattr(row, "dtype", None) is not None and getattr(row.dtype, "names", None):
                d = {key: row[key] for key in row.dtype.names}
            else:
                d = dict(row)
            price = float(d.get("price") or 0.0)
            volume = float(d.get("volume") or 0.0)
            kind = int(d.get("type") or 0)
            if price <= 0 or volume <= 0:
                continue
            if kind == sell_type:
                asks.append({"price": price, "volume": volume})
            elif kind == buy_type:
                bids.append({"price": price, "volume": volume})
        bids.sort(key=lambda x: x["price"], reverse=True)
        asks.sort(key=lambda x: x["price"])
        bids = bids[:levels]
        asks = asks[:levels]
        bid_depth = sum(x["volume"] for x in bids)
        ask_depth = sum(x["volume"] for x in asks)
        total = bid_depth + ask_depth
        return {
            "available": bool(bids or asks),
            "brokerSymbol": broker,
            "bids": bids,
            "asks": asks,
            "bidDepth": round(bid_depth, 2),
            "askDepth": round(ask_depth, 2),
            "depthImbalance": round((bid_depth - ask_depth) / total, 4) if total else 0.0,
            "bestBid": bids[0]["price"] if bids else None,
            "bestAsk": asks[0]["price"] if asks else None,
        }

    def orderflow(self, credentials: Dict[str, Any], symbol: str, quote: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        self._ensure(credentials)
        if self.dry_run or mt5 is None or os.environ.get("MT5_ORDERFLOW", "1") == "0":
            return {"available": False, "notes": ["Orderflow disabled"]}
        notes: List[str] = []
        tick_data: Dict[str, Any] = {}
        depth_data: Dict[str, Any] = {}
        try:
            tick_data = self.ticks(credentials, symbol, 500)
        except Exception as e:
            notes.append("ticks: %s" % str(e)[:80])
        try:
            depth_data = self.depth(credentials, symbol, 8)
        except Exception as e:
            notes.append("depth: %s" % str(e)[:80])
        broker = tick_data.get("brokerSymbol") or depth_data.get("brokerSymbol") or symbol
        bars: List[Dict[str, Any]] = []
        try:
            bars = self.rates(credentials, symbol, "M5", 120)
        except Exception:
            bars = []
        m1_bars: List[Dict[str, Any]] = []
        try:
            m1_bars = self.rates(credentials, symbol, "M1", 120)
        except Exception:
            m1_bars = []

        def _delta(rows: List[Dict[str, Any]], lookback: int = 24):
            delta = 0.0
            volume_total = 0.0
            absorb = 0.0
            if not rows:
                return 0.0, 0.0, 0.0
            recent = rows[-40:]
            vols = [float(b.get("tickVolume") or 0) for b in recent]
            mean_vol = sum(vols) / len(vols) if vols else 0.0
            var = sum((v - mean_vol) ** 2 for v in vols) / len(vols) if vols else 0.0
            sd = math.sqrt(var) if var > 0 else 0.0
            for b in recent[-lookback:]:
                high = float(b.get("high") or 0)
                low = float(b.get("low") or 0)
                close = float(b.get("close") or 0)
                volume = float(b.get("tickVolume") or 0)
                rng = high - low
                pos = ((close - low) / rng) if rng > 0 else 0.5
                bar_delta = volume * (2.0 * pos - 1.0)
                delta += bar_delta
                volume_total += volume
                if mean_vol > 0 and volume > mean_vol + 1.5 * sd and rng > 0:
                    absorb += (-bar_delta) * 0.15
            return delta, volume_total, absorb

        bar_delta, bar_volume, absorption = _delta(bars)
        m1_delta, m1_volume, m1_absorption = _delta(m1_bars)
        bar_delta_ratio = (bar_delta / bar_volume) if bar_volume else 0.0
        m1_delta_ratio = (m1_delta / m1_volume) if m1_volume else 0.0
        depth_imbalance = float(depth_data.get("depthImbalance") or 0.0)
        tick_ratio = float(tick_data.get("tickDeltaRatio") or 0.0)
        depth_available = bool(depth_data.get("available"))
        tick_available = bool(tick_data.get("available")) and int(tick_data.get("ticks") or 0) > 0
        bar_available = bool(bar_volume)
        m1_available = bool(m1_volume)
        parts = []
        if depth_available:
            parts.append((depth_imbalance, 0.35))
        if tick_available:
            parts.append((tick_ratio, 0.25))
        if bar_available:
            parts.append((_clamp(bar_delta_ratio + absorption, -1.0, 1.0), 0.25))
        if m1_available:
            parts.append((_clamp(m1_delta_ratio + m1_absorption, -1.0, 1.0), 0.15))
        total_weight = sum(w for _, w in parts)
        score = sum(v * w for v, w in parts) / total_weight if total_weight else 0.0
        quality = min(1.0, total_weight / 1.0)
        pressure = "buyers" if score > 0.15 else "sellers" if score < -0.15 else "balanced"
        bid = float((quote or {}).get("bid") or depth_data.get("bestBid") or 0.0)
        ask = float((quote or {}).get("ask") or depth_data.get("bestAsk") or 0.0)
        if depth_data.get("available"):
            notes.append("DOM %s: bid %.2f vs ask %.2f" % (pressure, depth_data.get("bidDepth") or 0, depth_data.get("askDepth") or 0))
        if tick_data.get("available"):
            notes.append("Ticks %s: delta %+.2f (%+.1f%%)" % (pressure, tick_data.get("tickDelta") or 0, tick_ratio * 100))
        if bar_delta_ratio:
            notes.append("M5 volume delta %+.1f%%" % (bar_delta_ratio * 100))
        if m1_delta_ratio:
            notes.append("M1 volume delta %+.1f%%" % (m1_delta_ratio * 100))
        if quality:
            notes.append("Orderflow quality %.0f%%" % (quality * 100))
        return {
            "available": bool(tick_data.get("available") or depth_data.get("available") or bar_available or m1_available),
            "symbol": symbol,
            "brokerSymbol": broker,
            "score": round(score, 4),
            "pressure": pressure,
            "depthImbalance": round(depth_imbalance, 4),
            "tickDeltaRatio": round(tick_ratio, 4),
            "barDeltaRatio": round(bar_delta_ratio, 4),
            "m1DeltaRatio": round(m1_delta_ratio, 4),
            "absorption": round(absorption, 4),
            "m1Absorption": round(m1_absorption, 4),
            "quality": round(quality, 3),
            "depthAvailable": depth_available,
            "tickAvailable": tick_available,
            "barAvailable": bar_available,
            "tickDelta": tick_data.get("tickDelta"),
            "buyVolume": tick_data.get("buyVolume"),
            "sellVolume": tick_data.get("sellVolume"),
            "bidDepth": depth_data.get("bidDepth"),
            "askDepth": depth_data.get("askDepth"),
            "bids": depth_data.get("bids") or [],
            "asks": depth_data.get("asks") or [],
            "spread": round(ask - bid, 6) if bid and ask else None,
            "notes": notes[:6],
        }

    def symbols(self, credentials: Dict[str, Any], query: Dict[str, Any]) -> Dict[str, Any]:
        self._ensure(credentials)
        if self.dry_run:
            return {"symbols": [], "count": 0, "mode": "dry-run"}
        search = str(query.get("search") or "").upper()
        limit = int(query.get("limit") or 200)
        rows = mt5.symbols_get() or []
        out = []
        for s in rows:
            d = s._asdict()
            name = d.get("name") or ""
            desc = d.get("description") or ""
            if search and search not in name.upper() and search not in desc.upper():
                continue
            out.append(
                {
                    "name": name,
                    "description": desc,
                    "digits": d.get("digits"),
                    "point": d.get("point"),
                    "contractSize": d.get("trade_contract_size"),
                    "minVolume": d.get("volume_min"),
                    "maxVolume": d.get("volume_max"),
                    "volumeStep": d.get("volume_step"),
                }
            )
            if len(out) >= limit:
                break
        return {"symbols": out, "count": len(out), "mode": "live"}

    def prices(self, credentials: Dict[str, Any], symbols: List[str]) -> Dict[str, Any]:
        self._ensure(credentials)
        if self.dry_run:
            return {"prices": [], "count": 0, "mode": "dry-run"}
        wanted = [s.strip().upper() for s in symbols if s and s.strip()]
        if not wanted:
            wanted = WATCHLIST
        out = []
        for name in wanted:
            broker = self._resolve(name)
            tick = self._tick(broker)
            if tick is None:
                continue
            info = mt5.symbol_info(broker)
            digits = info.digits if info else 5
            out.append(
                {
                    "symbol": name,
                    "brokerSymbol": broker,
                    "bid": round(float(tick.bid), digits),
                    "ask": round(float(tick.ask), digits),
                    "spread": round(float(tick.ask) - float(tick.bid), digits),
                    "time": _iso(tick.time),
                }
            )
        return {"prices": out, "count": len(out), "mode": "live"}

    def rates(self, credentials: Dict[str, Any], symbol: str, timeframe: str = "M15", count: int = 300) -> List[Dict[str, Any]]:
        self._ensure(credentials)
        if self.dry_run or mt5 is None:
            return []
        broker = self._resolve(symbol)
        tf = _timeframe(timeframe)
        if tf is None:
            raise Mt5Error("Unsupported timeframe: %s" % timeframe, "invalid_timeframe")
        mt5.symbol_select(broker, True)
        rows = mt5.copy_rates_from_pos(broker, tf, 0, int(count))
        if rows is None:
            code, message = mt5.last_error()
            raise Mt5Error("copy_rates failed for %s: %s" % (broker, message), "rates_failed", {"code": code})
        out = []
        for row in rows:
            out.append({
                "time": _iso(int(row["time"])),
                "open": float(row["open"]),
                "high": float(row["high"]),
                "low": float(row["low"]),
                "close": float(row["close"]),
                "tickVolume": float(row["tick_volume"]),
                "spread": float(row["spread"]),
            })
        return out

    def spec(self, credentials: Dict[str, Any], symbol: str) -> Dict[str, Any]:
        self._ensure(credentials)
        if self.dry_run or mt5 is None:
            return {
                "symbol": symbol, "digits": 5, "point": 0.00001,
                "tickSize": 0.00001, "tickValue": 1.0, "contractSize": 100000.0,
                "volumeMin": 0.01, "volumeMax": 100.0, "volumeStep": 0.01,
            }
        broker = self._resolve(symbol)
        info = mt5.symbol_info(broker)
        if info is None:
            raise Mt5Error("Symbol not found: %s" % broker, "symbol_not_found")
        d = info._asdict()
        return {
            "symbol": broker,
            "digits": int(d.get("digits") or 5),
            "point": float(d.get("point") or 0.00001),
            "tickSize": float(d.get("trade_tick_size") or d.get("point") or 0.00001),
            "tickValue": float(d.get("trade_tick_value") or 1.0),
            "contractSize": float(d.get("trade_contract_size") or 100000.0),
            "volumeMin": float(d.get("volume_min") or 0.01),
            "volumeMax": float(d.get("volume_max") or 100.0),
            "volumeStep": float(d.get("volume_step") or 0.01),
        }

    def deal_result(self, credentials: Dict[str, Any], ticket: Any) -> Dict[str, Any]:
        self._ensure(credentials)
        if self.dry_run or mt5 is None:
            return {"ticket": ticket, "profit": 0.0, "deals": 0}
        rows = []
        try:
            rows = mt5.history_deals_get(position=int(ticket)) or []
        except TypeError:
            now = datetime.now(timezone.utc)
            rows = [d for d in (mt5.history_deals_get(now - timedelta(days=30), now) or [])
                    if int(getattr(d, "position_id", 0) or 0) == int(ticket)]
        profit = 0.0
        for d in rows:
            try:
                profit += float(d.profit) + float(d.commission) + float(d.swap)
            except Exception:
                continue
        return {"ticket": ticket, "profit": round(profit, 2), "deals": len(rows)}

    # ---------------------------------------------------------------- writes
    def open_position(self, credentials: Dict[str, Any], payload: Dict[str, Any]) -> Dict[str, Any]:
        self._ensure(credentials)
        if self.dry_run:
            return {"order": {"ticket": 1, "status": "dry-run"}, "mode": "dry-run"}
        symbol = str(payload.get("symbol") or "").upper()
        side = str(payload.get("side") or "").lower()
        volume = float(payload.get("volume") or 0)
        if not symbol or side not in ("buy", "sell") or volume <= 0:
            raise Mt5Error("symbol, side and volume are required", "invalid_order")
        symbol = self._resolve(symbol)
        tick = self._tick(symbol)
        if tick is None or not tick.ask:
            raise Mt5Error("No tick for %s" % symbol, "symbol_unavailable")
        price = tick.ask if side == "buy" else tick.bid
        request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": symbol,
            "volume": volume,
            "type": mt5.ORDER_TYPE_BUY if side == "buy" else mt5.ORDER_TYPE_SELL,
            "price": float(price),
            "sl": float(payload.get("stopLoss") or payload.get("sl") or 0),
            "tp": float(payload.get("takeProfit") or payload.get("tp") or 0),
            "deviation": int(payload.get("deviation") or DEFAULT_DEVIATION),
            "magic": int(payload.get("magic") or 0),
            "comment": str(payload.get("comment") or "mt5-bridge")[:31],
            "type_time": mt5.ORDER_TIME_GTC,
        }
        result = self._send(request)
        return {"order": self._order_result(result, symbol, side, volume, price), "mode": "live"}

    def close_position(self, credentials: Dict[str, Any], payload: Dict[str, Any]) -> Dict[str, Any]:
        self._ensure(credentials)
        if self.dry_run:
            return {"deal": {"status": "dry-run"}, "mode": "dry-run"}
        ticket = int(payload.get("ticket"))
        rows = mt5.positions_get(ticket=ticket) or []
        if not rows:
            raise Mt5Error("Position %s not found" % ticket, "position_not_found")
        pos = rows[0]
        volume = float(payload.get("volume") or pos.volume)
        if volume > pos.volume + 1e-9:
            raise Mt5Error("Close volume exceeds open volume", "invalid_volume")
        tick = self._tick(pos.symbol)
        if tick is None:
            raise Mt5Error("No tick for %s" % pos.symbol, "symbol_unavailable")
        is_buy = pos.type == mt5.ORDER_TYPE_BUY
        request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": pos.symbol,
            "volume": volume,
            "type": mt5.ORDER_TYPE_SELL if is_buy else mt5.ORDER_TYPE_BUY,
            "position": ticket,
            "price": float(tick.bid if is_buy else tick.ask),
            "deviation": DEFAULT_DEVIATION,
            "magic": pos.magic,
            "comment": str(payload.get("comment") or "mt5-bridge close")[:26],
            "type_time": mt5.ORDER_TIME_GTC,
        }
        result = self._send(request)
        return {
            "deal": {
                "ticket": ticket,
                "symbol": pos.symbol,
                "volume": volume,
                "closePrice": float(request["price"]),
                "profit": _num(pos.profit),
                "retcode": result.retcode,
                "order": result.order,
            },
            "mode": "live",
        }

    def modify_position(self, credentials: Dict[str, Any], payload: Dict[str, Any]) -> Dict[str, Any]:
        self._ensure(credentials)
        if self.dry_run:
            return {"position": {"status": "dry-run"}, "mode": "dry-run"}
        ticket = int(payload.get("ticket"))
        request = {
            "action": mt5.TRADE_ACTION_SLTP,
            "position": ticket,
            "sl": float(payload.get("stopLoss") or payload.get("sl") or 0),
            "tp": float(payload.get("takeProfit") or payload.get("tp") or 0),
        }
        result = self._send(request)
        rows = mt5.positions_get(ticket=ticket) or []
        pos = rows[0]._asdict() if rows else {}
        return {
            "position": {
                "ticket": ticket,
                "stopLoss": _num(pos.get("sl"), 5) or None,
                "takeProfit": _num(pos.get("tp"), 5) or None,
                "retcode": result.retcode,
            },
            "mode": "live",
        }

    def place_order(self, credentials: Dict[str, Any], payload: Dict[str, Any]) -> Dict[str, Any]:
        self._ensure(credentials)
        if self.dry_run:
            return {"order": {"ticket": 1, "status": "dry-run"}, "mode": "dry-run"}
        symbol = self._resolve(str(payload.get("symbol") or ""))
        side = str(payload.get("side") or "").lower()
        kind = str(payload.get("type") or "limit").lower()
        volume = float(payload.get("volume") or 0)
        price = float(payload.get("price") or 0)
        if not symbol or side not in ("buy", "sell") or volume <= 0 or price <= 0:
            raise Mt5Error("symbol, side, volume and price are required", "invalid_order")
        mt5.symbol_select(symbol, True)
        if kind == "limit":
            otype = mt5.ORDER_TYPE_BUY_LIMIT if side == "buy" else mt5.ORDER_TYPE_SELL_LIMIT
        else:
            otype = mt5.ORDER_TYPE_BUY_STOP if side == "buy" else mt5.ORDER_TYPE_SELL_STOP
        request = {
            "action": mt5.TRADE_ACTION_PENDING,
            "symbol": symbol,
            "volume": volume,
            "type": otype,
            "price": price,
            "sl": float(payload.get("stopLoss") or payload.get("sl") or 0),
            "tp": float(payload.get("takeProfit") or payload.get("tp") or 0),
            "deviation": DEFAULT_DEVIATION,
            "magic": int(payload.get("magic") or 0),
            "comment": str(payload.get("comment") or "mt5-bridge")[:31],
            "type_time": mt5.ORDER_TIME_GTC,
        }
        result = self._send(request)
        return {"order": self._order_result(result, symbol, side, volume, price, kind), "mode": "live"}

    def cancel_order(self, credentials: Dict[str, Any], payload: Dict[str, Any]) -> Dict[str, Any]:
        self._ensure(credentials)
        if self.dry_run:
            return {"ticket": payload.get("ticket"), "status": "dry-run"}
        ticket = int(payload.get("ticket"))
        result = self._send({"action": mt5.TRADE_ACTION_REMOVE, "order": ticket})
        return {"ticket": ticket, "status": "cancelled", "retcode": result.retcode, "mode": "live"}

    def close_all(self, credentials: Dict[str, Any]) -> Dict[str, Any]:
        self._ensure(credentials)
        if self.dry_run:
            return {"closed": [], "count": 0, "mode": "dry-run"}
        closed = []
        for pos in mt5.positions_get() or []:
            try:
                result = self.close_position(
                    credentials, {"ticket": pos.ticket, "volume": pos.volume}
                )
                closed.append(result["deal"])
            except Exception as e:
                closed.append({"ticket": pos.ticket, "error": str(e)})
        return {"closed": closed, "count": len(closed), "mode": "live"}

    def status(self, credentials: Dict[str, Any]) -> Dict[str, Any]:
        self._ensure(credentials)
        info = self.account(credentials)
        return {
            "connected": True,
            "mode": "dry-run" if self.dry_run else "live",
            "login": info.get("login"),
            "server": info.get("server"),
            "agent": os.environ.get("MT5_WORKER_ID", "github-actions"),
            "terminal": self._terminal_info(),
            "latencyMs": 1,
            "lastError": None,
        }

    def snapshot(self, credentials: Dict[str, Any]) -> Dict[str, Any]:
        info = self.account(credentials)
        positions = self.positions(credentials)
        orders = self.orders(credentials)
        watch = list(dict.fromkeys(WATCHLIST + [p["symbol"] for p in positions if p.get("symbol")]))
        prices = self.prices(credentials, watch)
        status = {
            "connected": True,
            "mode": "dry-run" if self.dry_run else "live",
            "login": info.get("login"),
            "server": info.get("server"),
            "agent": os.environ.get("MT5_WORKER_ID", "github-actions"),
            "terminal": self._terminal_info(),
            "latencyMs": 1,
            "lastError": None,
        }
        return {
            "info": info,
            "positions": positions,
            "orders": orders,
            "prices": prices.get("prices", []),
            "status": status,
            "watchlist": watch,
        }

    # ---------------------------------------------------------------- helpers
    def _write_start_ini(self, credentials: Dict[str, Any]) -> str:
        path = os.path.join(os.path.dirname(self.terminal_path), "config", "start.ini")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        body = "\n".join(
            [
                "[Common]",
                "Login=%s" % credentials.get("login"),
                "Password=%s" % credentials.get("password"),
                "Server=%s" % credentials.get("server"),
                "KeepPrivate=1",
                "CertInstall=0",
                "NewsEnable=0",
                "ProxyEnable=0",
                "",
                "[Experts]",
                "AllowLiveTrading=1",
                "AllowDllImport=1",
                "Enabled=1",
                "Account=0",
                "",
                "[StartUp]",
                "",
                "",
            ]
        )
        with open(path, "w", encoding="utf-16") as fh:
            fh.write(body)
        return path

    def _restart_with_experts(self, credentials: Dict[str, Any]) -> bool:
        try:
            mt5.shutdown()
        except Exception:
            pass
        try:
            subprocess.run(
                ["taskkill", "/F", "/IM", "terminal64.exe"],
                capture_output=True,
                text=True,
                timeout=25,
            )
        except Exception:
            pass
        time.sleep(3)
        try:
            ini = self._write_start_ini(credentials)
            subprocess.Popen(
                [self.terminal_path, "/portable", "/config:" + ini],
                cwd=os.path.dirname(self.terminal_path),
                close_fds=True,
            )
        except Exception:
            return False
        deadline = time.time() + 100
        while time.time() < deadline:
            time.sleep(3)
            try:
                if mt5.initialize(path=self.terminal_path, timeout=15000, portable=True):
                    ai = mt5.account_info()
                    ti = mt5.terminal_info()
                    if (
                        ai is not None
                        and str(ai.login) == str(credentials.get("login"))
                        and ti is not None
                        and bool(getattr(ti, "trade_allowed", False))
                    ):
                        return True
                    mt5.shutdown()
            except Exception:
                pass
        try:
            mt5.shutdown()
        except Exception:
            pass
        return False

    def _resolve(self, symbol: str) -> str:
        name = str(symbol or "").strip().upper()
        if not name or mt5 is None:
            return name
        cached = self._symbol_cache.get(name)
        if cached and time.time() - cached[1] < 60:
            return cached[0]
        mapped = SYMBOL_MAP.get(_norm_symbol(name))
        if mapped:
            try:
                if mt5.symbol_info(mapped) is not None:
                    self._symbol_cache[name] = (mapped, time.time())
                    return mapped
            except Exception:
                pass
        aliases = _SYMBOL_ALIASES.get(_norm_symbol(name), [name])
        variants = [name] + [name + s for s in _SYMBOL_SUFFIXES] + [p + name for p in _SYMBOL_PREFIXES]
        for variant in variants:
            try:
                info = mt5.symbol_info(variant)
            except Exception:
                info = None
            if info is not None:
                self._symbol_cache[name] = (info.name, time.time())
                return info.name
        rows = self._symbol_rows()
        best = None
        best_score = 0
        for row in rows:
            broker = str(getattr(row, "name", "") or "")
            score = _symbol_score(name, broker, aliases)
            if score > best_score or (score == best_score and best and len(broker) < len(best)):
                best = broker
                best_score = score
        resolved = best if best_score >= 50 else name
        self._symbol_cache[name] = (resolved, time.time())
        return resolved

    def _symbol_rows(self):
        now = time.time()
        if self._symbol_rows_cache is not None and now - self._symbol_rows_at < 60:
            return self._symbol_rows_cache
        try:
            rows = mt5.symbols_get() or []
        except Exception:
            rows = []
        self._symbol_rows_cache = rows
        self._symbol_rows_at = now
        return rows

    def _tick(self, symbol: str):
        mt5.symbol_select(symbol, True)
        tick = None
        for _ in range(10):
            tick = mt5.symbol_info_tick(symbol)
            if tick is not None and tick.bid and tick.ask:
                return tick
            time.sleep(0.5)
        return tick

    def _send(self, request: Dict[str, Any]):
        last = None
        for filling in (mt5.ORDER_FILLING_IOC, mt5.ORDER_FILLING_FOK, mt5.ORDER_FILLING_RETURN):
            request["type_filling"] = filling
            last = mt5.order_send(request)
            if last is None:
                continue
            if last.retcode == mt5.TRADE_RETCODE_DONE:
                return last
            if last.retcode != 10030:
                break
        if last is None:
            code, message = mt5.last_error()
            raise Mt5Error("order_send failed: %s" % message, "order_failed", {"code": code})
        if last.retcode != mt5.TRADE_RETCODE_DONE:
            raise Mt5Error(
                "order rejected: %s" % (last.comment or last.retcode),
                "order_rejected",
                {"retcode": last.retcode, "comment": last.comment},
            )
        return last

    def _order_result(self, result, symbol, side, volume, price, kind="market") -> Dict[str, Any]:
        return {
            "ticket": result.order or result.deal,
            "symbol": symbol,
            "side": side,
            "type": kind,
            "volume": volume,
            "price": price,
            "status": "filled" if kind == "market" else "placed",
            "retcode": result.retcode,
            "deal": result.deal,
            "order": result.order,
            "comment": result.comment,
        }

    def _dry_account(self, credentials: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "login": str(credentials.get("login")),
            "server": credentials.get("server"),
            "name": "Dry run",
            "currency": "USD",
            "balance": 10000.0,
            "credit": 0.0,
            "equity": 10000.0,
            "margin": 0.0,
            "freeMargin": 10000.0,
            "marginLevel": None,
            "leverage": 500,
            "profit": 0.0,
            "openPositions": 0,
            "pendingOrders": 0,
            "mode": "dry-run",
        }


def _parse_time(value: Any) -> Optional[datetime]:
    if not value:
        return None
    if isinstance(value, datetime):
        return value
    try:
        text = str(value).replace("Z", "+00:00")
        return datetime.fromisoformat(text)
    except Exception:
        return None
