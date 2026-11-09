import glob
import os
import subprocess
import sys
import time

import MetaTrader5 as mt5

TERMINAL = r"C:\mt5\terminal64.exe"
DATA = r"C:\mt5"
LOGIN = os.environ.get("MT5_TEST_LOGIN", "")
PASSWORD = os.environ.get("MT5_TEST_PASSWORD", "")
SERVER = os.environ.get("MT5_TEST_SERVER", "")
TRADE = os.environ.get("MT5_DIAG_TRADE", "0") == "1"


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


out("python", sys.version)
out("MetaTrader5", mt5.__version__)
out("login", LOGIN, "server", SERVER, "trade", TRADE)
if not LOGIN or not PASSWORD or not SERVER:
    out("missing MT5_TEST_* secrets")
    sys.exit(1)

cfg = os.path.join(DATA, "config", "bridge.ini")
os.makedirs(os.path.dirname(cfg), exist_ok=True)
legacy = os.path.join(DATA, "config", "terminal.ini")
try:
    os.remove(legacy)
except OSError:
    pass
with open(cfg, "w", encoding="utf-8") as fh:
    fh.write(
        "[Common]\nLogin=%s\nPassword=%s\nServer=%s\n\n"
        "[Experts]\nAllowLiveTrading=1\nAllowDllImport=1\nEnabled=1\nAccount=0\n"
        % (LOGIN, PASSWORD, SERVER)
    )
out("wrote", cfg)
show_cfg = open(cfg, "r", encoding="utf-8").read()
out(show_cfg.replace(PASSWORD, "***"))

proc = subprocess.Popen([TERMINAL, "/portable", "/config:" + cfg], cwd=DATA)
out("started pid", proc.pid)

deadline = time.time() + 150
while time.time() < deadline:
    time.sleep(5)
    ps = pipes()
    log = log_tail(2000)
    out("pipes", ps)
    for line in log.splitlines()[-4:]:
        out("log", line)
    low = log.lower()
    if ps and ("authorized" in low or "connected" in low or "account" in low or "login" in low):
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
