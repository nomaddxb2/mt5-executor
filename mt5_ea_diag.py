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
STATE = os.path.join(FILES, "bridge_state.json")
CMD = os.path.join(FILES, "bridge_cmd.json")
RES = os.path.join(FILES, "bridge_res.json")


def out(*args):
    print(*args, flush=True)


def read_text(path, limit=8000):
    for enc in ("utf-16", "utf-8-sig", "utf-8"):
        try:
            with open(path, "r", encoding=enc, errors="replace") as fh:
                return fh.read()[-limit:]
        except (UnicodeError, OSError):
            continue
    return ""


def read_json(path):
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            return json.load(fh)
    except Exception:
        return {}


def wait_file(path, timeout, label):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if os.path.exists(path) and os.path.getsize(path) > 0:
            out(label, "ready", path)
            return True
        time.sleep(2)
    out(label, "timeout", path)
    return False


def send(cmd, label, timeout=30):
    try:
        os.remove(RES)
    except OSError:
        pass
    with open(CMD, "w", encoding="utf-8") as fh:
        json.dump(cmd, fh)
    out("sent", label, cmd.get("id"))
    if not wait_file(RES, timeout, label + " result"):
        return {}
    body = read_text(RES, 2000)
    out(label, "result", body)
    try:
        return json.loads(body)
    except Exception:
        return {}


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
    cwd=DATA,
    capture_output=True,
    text=True,
    timeout=120,
)
out("compile rc", res.returncode)
out("compile log", read_text(clog, 4000))
ex5 = os.path.join(EXPERTS, "BridgeEA.ex5")
out("ex5 exists", os.path.exists(ex5), os.path.getsize(ex5) if os.path.exists(ex5) else 0)
if not os.path.exists(ex5):
    sys.exit(2)

for name in ("accounts.dat", "accounts.ini", "common.ini.bak"):
    try:
        os.remove(os.path.join(DATA, "config", name))
    except OSError:
        pass

cfg = os.path.join(DATA, "config", "start.ini")
with open(cfg, "w", encoding="utf-16") as fh:
    fh.write(
        "[Common]\nLogin=%s\nPassword=%s\nServer=%s\n"
        "KeepPrivate=1\nCertInstall=0\nNewsEnable=0\nProxyEnable=0\n\n"
        "[Experts]\nAllowLiveTrading=1\nAllowDllImport=1\nEnabled=1\nAccount=0\n\n"
        "[StartUp]\nExpert=BridgeEA\nSymbol=EURUSD\nPeriod=H1\n"
        % (LOGIN, PASSWORD, SERVER)
    )
out("wrote utf-16", cfg, "bytes", os.path.getsize(cfg))

for path in (CMD, RES, STATE):
    try:
        os.remove(path)
    except OSError:
        pass

proc = subprocess.Popen(
    [
        TERMINAL,
        "/portable",
        "/config:" + cfg,
        "/login:" + LOGIN,
        "/password:" + PASSWORD,
        "/server:" + SERVER,
    ],
    cwd=DATA,
)
out("started pid", proc.pid)

import threading

def keep_files():
    deadline = time.time() + 180
    while time.time() < deadline:
        try:
            os.makedirs(FILES, exist_ok=True)
        except OSError:
            pass
        time.sleep(2)

threading.Thread(target=keep_files, daemon=True).start()

have_state = wait_file(STATE, 150, "state")
out("files dir", sorted(os.listdir(FILES)) if os.path.isdir(FILES) else "missing")
out("experts dir", sorted(os.listdir(EXPERTS)) if os.path.isdir(EXPERTS) else "missing")
state = read_json(STATE)
out("state initial", json.dumps(state))
out("login_ok", str(state.get("login")) == LOGIN, "server_ok", state.get("server") == SERVER)

deadline = time.time() + 120
while TRADE and time.time() < deadline:
    if state.get("login") and state.get("trade_allowed"):
        break
    time.sleep(5)
    state = read_json(STATE)
out("state settled", json.dumps(state))

if TRADE and state.get("login") and state.get("trade_allowed"):
    opened = send(
        {
            "id": "diag-open-1",
            "op": "open",
            "symbol": "EURUSD",
            "volume": 0.01,
            "type": "buy",
            "price": 0,
            "sl": 0,
            "tp": 0,
            "comment": "bridge diag",
        },
        "open",
    )
    ticket = opened.get("order") or opened.get("deal")
    if ticket:
        send(
            {
                "id": "diag-close-1",
                "op": "close",
                "symbol": "EURUSD",
                "ticket": ticket,
                "volume": 0.01,
                "type": "sell",
            },
            "close",
        )
    out("state after", json.dumps(read_json(STATE)))
else:
    out("skipping trade", "login", state.get("login"), "trade_allowed", state.get("trade_allowed"))

def ini_sections(path, wanted):
    text = read_text(path, 30000)
    block = []
    keep = False
    for line in text.splitlines():
        st = line.strip()
        if st.startswith("["):
            keep = any(w.lower() in st.lower() for w in wanted)
        if keep:
            block.append(line)
    return "\n".join(block)

for name in ("terminal.ini", "start.ini", "common.ini"):
    out("=== ini", name)
    out(ini_sections(os.path.join(DATA, "config", name), ["Experts", "StartUp", "Common"]))
out("=== assistant.ini")
out(read_text(os.path.join(DATA, "config", "assistant.ini"), 4000))

out("=== mql logs")
mlogs = os.path.join(DATA, "MQL5", "Logs")
try:
    for name in sorted(os.listdir(mlogs)):
        out("mql log", name)
        out(read_text(os.path.join(mlogs, name), 12000))
except OSError as exc:
    out("mql logs listing failed", exc)

out("=== config")
try:
    for name in sorted(os.listdir(os.path.join(DATA, "config"))):
        p = os.path.join(DATA, "config", name)
        out(name, os.path.getsize(p))
except OSError as exc:
    out("config listing failed", exc)
out("origin", read_text(os.path.join(DATA, "origin.txt"), 2000))

out("=== terminal log")
out(read_text(os.path.join(DATA, "logs", "20261109.log"), 40000))
