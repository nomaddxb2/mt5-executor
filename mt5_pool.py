"""MetaTrader 5 terminal pool used by the GitHub Actions execution worker."""

from __future__ import annotations

import glob
import os
import subprocess
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

try:
    import MetaTrader5 as mt5
except Exception:  # pragma: no cover - Linux and dry runs
    mt5 = None

TERMINAL_PATH = os.environ.get(
    "MT5_TERMINAL_PATH", r"C:\mt5\terminal64.exe"
)
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


def _side(order_type: int) -> str:
    buy_types = {0, 2, 4}
    if mt5 is not None:
        buy_types = {
            mt5.ORDER_TYPE_BUY,
            mt5.ORDER_TYPE_BUY_LIMIT,
            mt5.ORDER_TYPE_BUY_STOP,
        }
    return "buy" if order_type in buy_types else "sell"


class TerminalPool:
    def __init__(self, dry_run: bool = False):
        self.dry_run = dry_run or os.environ.get("MT5_DRY_RUN") == "1"
        self.current: Optional[Dict[str, Any]] = None
        self.connected_at: Optional[float] = None

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
        if self.current and (str(self.current.get("login")), str(self.current.get("server"))) == key:
            return self.status(credentials)
        try:
            mt5.shutdown()
        except Exception:
            pass
        started = time.time()
        timeout_ms = int(os.environ.get("MT5_CONNECT_TIMEOUT_MS", "120000"))
        last_code, last_message = -1, "unknown"
        ok = False
        # Attach to the terminal that the warm workflow already started.
        try:
            ok = mt5.initialize(path=TERMINAL_PATH, timeout=timeout_ms, portable=True)
        except Exception:
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
            ok = self._restart_with_experts(credentials)
        for attempt in range(3):
            if ok:
                break
            ok = mt5.initialize(
                path=TERMINAL_PATH,
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
                    "path": TERMINAL_PATH,
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
        if not os.path.exists(TERMINAL_PATH):
            raise Mt5Error("Terminal not found at %s" % TERMINAL_PATH, "mt5_missing")
        subprocess.Popen(
            [TERMINAL_PATH, "/portable"],
            cwd=os.path.dirname(TERMINAL_PATH),
            close_fds=True,
        )
        time.sleep(5)

    def _tail_logs(self, limit: int = 20) -> List[str]:
        try:
            files = sorted(
                glob.glob(os.path.join(os.path.dirname(TERMINAL_PATH), "logs", "*.log")),
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
            return {"name": "MT5 dry-run", "build": 0, "company": "MT5 Bridge"}
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
            "comment": "mt5-bridge close",
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
        path = os.path.join(os.path.dirname(TERMINAL_PATH), "config", "start.ini")
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
                [TERMINAL_PATH, "/portable", "/config:" + ini],
                cwd=os.path.dirname(TERMINAL_PATH),
                close_fds=True,
            )
        except Exception:
            return False
        deadline = time.time() + 100
        while time.time() < deadline:
            time.sleep(3)
            try:
                if mt5.initialize(path=TERMINAL_PATH, timeout=15000, portable=True):
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
        info = mt5.symbol_info(name)
        if info is not None:
            return info.name
        for suffix in ("m", ".m", ".raw", "m.raw", "z", ".z"):
            cand = name + suffix
            info = mt5.symbol_info(cand)
            if info is not None:
                return info.name
        try:
            for row in mt5.symbols_get() or []:
                if str(row.name).upper() == name:
                    return row.name
        except Exception:
            pass
        return name

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
