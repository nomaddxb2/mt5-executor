import glob
import json
import os
import subprocess
import sys
import time

TERMINAL = r"C:\mt5\terminal64.exe"
METAEDITOR = r"C:\mt5\MetaEditor64.exe"
DATA = r"C:\mt5"
FILES = os.path.join(DATA, "MQL5", "Files")
EXPERTS = os.path.join(DATA, "MQL5", "Experts")
LOGIN = os.environ.get("MT5_TEST_LOGIN", "")
PASSWORD = os.environ.get("MT5_TEST_PASSWORD", "")
SERVER = os.environ.get("MT5_TEST_SERVER", "")
TRADE = os.environ.get("MT5_DIAG_TRADE", "0") == "1"


def out(*args):
    print(*args, flush=True)


def read_text(path, limit=6000):
    for enc in ("utf-16", "utf-8-sig", "utf-8"):
        try:
            with open(path, "r", encoding=enc, errors="replace") as fh:
                return fh.read()[-limit:]
        except (UnicodeError, OSError):
            continue
    return ""


def wait_file(path, timeout, label):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if os.path.exists(path) and os.path.getsize(path) > 0:
            out(label, "ready", path)
            return True
        time.sleep(2)
    out(label, "timeout", path)
    return False


out("python", sys.version)
out("trade", TRADE)
if not LOGIN or not PASSWORD or not SERVER:
    out("missing MT5_TEST_* secrets")
    sys.exit(1)

os.makedirs(EXPERTS, exist_ok=True)
os.makedirs(FILES, exist_ok=True)
src = os.path.join(os.getcwd(), "BridgeEA.mq5")
dst = os.path.join(EXPERTS, "BridgeEA.mq5")
with open(src, "r", encoding="utf-8") as fh:
    source = fh.read()
with open(dst, "w", encoding="utf-8") as fh:
    fh.write(source)
out("copied ea", dst, os.path.getsize(dst))

clog = os.path.join(DATA, "ea_compile.log")
res = subprocess.run(
    [METAEDITOR, "/compile:" + dst, "/log:" + clog],
    capture_output=True,
    text=True,
    timeout=120,
)
out("compile rc", res.returncode)
out("compile log", read_text(clog, 4000))
out("ex5 exists", os.path.exists(os.path.join(EXPERTS, "BridgeEA.ex5")))

cfg = os.path.join(DATA, "config", "common.ini")
with open(cfg, "w", encoding="utf-16") as fh:
    fh.write(
        "[Common]\nLogin=%s\nPassword=%s\nServer=%s\n"
        "KeepPrivate=1\nCertInstall=0\nNewsEnable=0\nProxyEnable=0\n\n"
        "[Experts]\nAllowLiveTrading=1\nAllowDllImport=1\nEnabled=1\nAccount=0\n\n"
        "[StartUp]\nExpert=BridgeEA\nSymbol=EURUSD\nPeriod=H1\n"
        % (LOGIN, PASSWORD, SERVER)
    )
out("wrote utf-16", cfg, "bytes", os.path.getsize(cfg))

for name in ("bridge_cmd.json", "bridge_res.json", "bridge_state.json"):
    try:
        os.remove(os.path.join(FILES, name))
    except OSError:
        pass

proc = subprocess.Popen([TERMINAL, "/portable", "/config:" + cfg], cwd=DATA)
out("started pid", proc.pid)

state_path = os.path.join(FILES, "bridge_state.json")
have_state = wait_file(state_path, 150, "state")
if have_state:
    out("state", read_text(state_path, 2000))
else:
    out("terminal log", read_text(os.path.join(DATA, "logs", "20261109.log"), 6000))

if TRADE and have_state:
    cmd = {
        "id": "diag-open-1",
        "op": "open",
        "symbol": "EURUSD",
        "volume": 0.01,
        "type": "buy",
        "price": 0,
        "sl": 0,
        "tp": 0,
        "comment": "bridge diag",
    }
    with open(os.path.join(FILES, "bridge_cmd.json"), "w", encoding="utf-8") as fh:
        json.dump(cmd, fh)
    out("wrote open command")
    respath = os.path.join(FILES, "bridge_res.json")
    if wait_file(respath, 30, "open result"):
        body = read_text(respath, 2000)
        out("open result", body)
        try:
            data = json.loads(body)
            ticket = data.get("order") or data.get("deal")
        except Exception:
            ticket = None
        if ticket:
            close = {
                "id": "diag-close-1",
                "op": "close",
                "symbol": "EURUSD",
                "ticket": ticket,
                "volume": 0.01,
                "type": "sell",
            }
            try:
                os.remove(respath)
            except OSError:
                pass
            with open(os.path.join(FILES, "bridge_cmd.json"), "w", encoding="utf-8") as fh:
                json.dump(close, fh)
            out("wrote close command", ticket)
            if wait_file(respath, 30, "close result"):
                out("close result", read_text(respath, 2000))
    out("state after", read_text(state_path, 2000))

out("=== terminal log")
out(read_text(os.path.join(DATA, "logs", "20261109.log"), 8000))
