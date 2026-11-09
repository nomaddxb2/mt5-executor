import glob
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request

import MetaTrader5 as mt5

TERMINAL = r"C:\mt5\terminal64.exe"
DATA = r"C:\mt5"
LOGIN = os.environ.get("MT5_TEST_LOGIN", "")
PASSWORD = os.environ.get("MT5_TEST_PASSWORD", "")
SERVER = os.environ.get("MT5_TEST_SERVER", "")
TRADE = os.environ.get("MT5_DIAG_TRADE", "0") == "1"
MCP_URL = "http://127.0.0.1:22346/mcp"


def out(*args):
    print(*args, flush=True)


def pipes():
    try:
        return sorted(n for n in os.listdir("\\\\.\\pipe\\") if "mt5" in n.lower())
    except Exception as exc:
        return ["<listdir error %s>" % exc]


def log_tail(limit=4000):
    paths = sorted(glob.glob(os.path.join(DATA, "logs", "*.log")))
    if not paths:
        return ""
    try:
        with open(paths[-1], "r", encoding="utf-16", errors="replace") as fh:
            return fh.read()[-limit:]
    except Exception as exc:
        return "<log error %s>" % exc


def read_text(path):
    for enc in ("utf-16", "utf-8-sig", "utf-8"):
        try:
            with open(path, "r", encoding=enc) as fh:
                return fh.read()
        except (UnicodeError, OSError):
            continue
    return ""


def mcp_key():
    text = read_text(os.path.join(DATA, "config", "assistant.ini"))
    section = None
    key = ""
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("["):
            section = line
        elif section == "[MCP.MetaTrader]" and line.lower().startswith("apikey="):
            key = line.split("=", 1)[1].strip()
    return key


def mcp_post(key, payload):
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    if key:
        headers["Authorization"] = "Bearer " + key
    req = urllib.request.Request(
        MCP_URL, data=json.dumps(payload).encode("utf-8"), headers=headers, method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as res:
            return res.status, res.read(4000).decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read(500).decode("utf-8", "replace")
    except Exception as exc:
        return None, str(exc)


out("python", sys.version)
out("MetaTrader5", mt5.__version__)
out("trade", TRADE)
if not LOGIN or not PASSWORD or not SERVER:
    out("missing MT5_TEST_* secrets")
    sys.exit(1)

cfg = os.path.join(DATA, "config", "common.ini")
os.makedirs(os.path.dirname(cfg), exist_ok=True)
with open(cfg, "w", encoding="utf-16") as fh:
    fh.write(
        "[Common]\nLogin=%s\nPassword=%s\nServer=%s\n"
        "KeepPrivate=1\nCertInstall=0\nNewsEnable=0\nProxyEnable=0\n\n"
        "[Experts]\nAllowLiveTrading=1\nAllowDllImport=1\nEnabled=1\nAccount=0\n"
        % (LOGIN, PASSWORD, SERVER)
    )
out("wrote utf-16", cfg, "bytes", os.path.getsize(cfg))

proc = subprocess.Popen([TERMINAL, "/portable", "/config:" + cfg], cwd=DATA)
out("started pid", proc.pid)

deadline = time.time() + 75
authorized = False
while time.time() < deadline:
    time.sleep(5)
    log = log_tail(4000)
    low = log.lower()
    out("pipes", pipes())
    for line in log.splitlines()[-6:]:
        out("log", line)
    if "authorized" in low or "connected to" in low or "login" in low:
        authorized = True
        break
out("authorized-marker", authorized)

key = mcp_key()
out("assistant.ini key length", len(key))
init = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "mt5-bridge", "version": "1.0"},
    },
}
for label, candidate in (("ini-key", key), ("no-key", "")):
    status, body = mcp_post(candidate, init)
    out("mcp initialize", label, "status", status, "body", body[:300])
    if status == 200:
        status2, body2 = mcp_post(
            candidate, {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}
        )
        out("mcp tools/list status", status2)
        try:
            tools = json.loads(body2).get("result", {}).get("tools", [])
            out("mcp tools", [t.get("name") for t in tools])
        except Exception as exc:
            out("tools parse error", exc, body2[:300])
        break

out("=== initialize")
ok = mt5.initialize(path=TERMINAL, portable=True, timeout=120000)
out("initialize", ok, mt5.last_error())
if ok:
    out("terminal_info", mt5.terminal_info())
    ai = mt5.account_info()
    if ai is None:
        out("login", mt5.login(int(LOGIN), password=PASSWORD, server=SERVER))
        out("login_error", mt5.last_error())
        ai = mt5.account_info()
    out("account_info", ai)
    tick = mt5.symbol_info_tick("EURUSD")
    out("tick EURUSD", tick)
    if tick:
        req = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": "EURUSD",
            "volume": 0.01,
            "type": mt5.ORDER_TYPE_BUY,
            "price": tick.ask,
            "deviation": 20,
            "magic": 0,
            "comment": "diag",
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": mt5.ORDER_FILLING_IOC,
        }
        out("order_check", mt5.order_check(req))
        if TRADE:
            res = mt5.order_send(req)
            out("order_send", res)
            if res and res.retcode == mt5.TRADE_RETCODE_DONE:
                out("positions", mt5.positions_get(symbol="EURUSD"))
                for pos in mt5.positions_get(symbol="EURUSD") or []:
                    close = {
                        "action": mt5.TRADE_ACTION_DEAL,
                        "symbol": "EURUSD",
                        "volume": pos.volume,
                        "type": mt5.ORDER_TYPE_SELL if pos.type == 0 else mt5.ORDER_TYPE_BUY,
                        "position": pos.ticket,
                        "price": tick.bid if pos.type == 0 else tick.ask,
                        "deviation": 20,
                        "magic": 0,
                        "comment": "diag close",
                        "type_time": mt5.ORDER_TIME_GTC,
                        "type_filling": mt5.ORDER_FILLING_IOC,
                    }
                    out("close", mt5.order_send(close))
    mt5.shutdown()
out("=== log tail")
out(log_tail(8000))
