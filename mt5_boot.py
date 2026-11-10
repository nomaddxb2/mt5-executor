import glob
import json
import os
import subprocess
import sys
import time
import urllib.request

import MetaTrader5 as mt5

TERMINAL = r"C:\mt5\terminal64.exe"
DATA = r"C:\mt5"
API = (os.environ.get("MT5_API_URL") or "").rstrip("/")
SECRET = os.environ.get("MT5_WORKER_SECRET") or ""
SHARD = int(os.environ.get("MT5_SHARD") or 0)
SHARDS = max(1, int(os.environ.get("MT5_SHARDS") or 1))


def out(*args):
    text = " ".join(str(a).encode("ascii", "backslashreplace").decode("ascii") for a in args)
    print("[mt5-boot] " + text, flush=True)


def api(path, body):
    req = urllib.request.Request(
        API + path,
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json", "X-Worker-Secret": SECRET},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as res:
        return json.loads(res.read().decode("utf-8"))


def log_tail(limit=4000):
    paths = sorted(glob.glob(os.path.join(DATA, "logs", "*.log")))
    if not paths:
        return ""
    try:
        with open(paths[-1], "r", encoding="utf-16", errors="replace") as fh:
            return fh.read()[-limit:]
    except Exception as exc:
        return "<log error %s>" % exc


def write_ini(creds):
    path = os.path.join(DATA, "config", "start.ini")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    body = "\n".join(
        [
            "[Common]",
            "Login=%s" % creds.get("login"),
            "Password=%s" % creds.get("password"),
            "Server=%s" % creds.get("server"),
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


def main():
    if not API or not SECRET:
        out("MT5_API_URL and MT5_WORKER_SECRET are required")
        return 2
    if not os.path.exists(TERMINAL):
        out("terminal missing", TERMINAL)
        return 2
    payload = api("/v1/worker/ai/accounts", {"shard": SHARD, "shards": SHARDS})
    rows = payload.get("accounts") or []
    out("accounts", len(rows))
    if not rows:
        out("no enabled accounts on this shard")
        return 0
    creds = rows[0].get("credentials") or {}
    if not creds.get("login") or not creds.get("password") or not creds.get("server"):
        out("account credentials incomplete")
        return 2
    out("login", creds.get("login"), "server", creds.get("server"))

    subprocess.run(["taskkill", "/F", "/IM", "terminal64.exe"], capture_output=True, text=True, timeout=25)
    time.sleep(3)
    ini = write_ini(creds)
    out("start.ini", ini, "bytes", os.path.getsize(ini))
    proc = subprocess.Popen([TERMINAL, "/portable", "/config:" + ini], cwd=DATA, close_fds=True)
    out("terminal pid", proc.pid)

    deadline = time.time() + 120
    last = None
    while time.time() < deadline:
        time.sleep(3)
        try:
            ok = mt5.initialize(path=TERMINAL, timeout=15000, portable=True)
        except Exception as exc:
            ok = False
            last = str(exc)
        if not ok:
            last = mt5.last_error()
            out("initialize pending", last)
            try:
                mt5.shutdown()
            except Exception:
                pass
            continue
        ai = mt5.account_info()
        ti = mt5.terminal_info()
        out("terminal", ti)
        out("account", ai)
        if ai is not None and str(ai.login) == str(creds.get("login")):
            out("session ready")
            mt5.shutdown()
            return 0
        login_ok = mt5.login(int(creds.get("login")), password=str(creds.get("password")), server=str(creds.get("server")))
        out("login result", login_ok, mt5.last_error())
        ai = mt5.account_info()
        if login_ok and ai is not None and str(ai.login) == str(creds.get("login")):
            out("session ready after login")
            mt5.shutdown()
            return 0
        try:
            mt5.shutdown()
        except Exception:
            pass
    out("boot failed", last)
    out("log tail", log_tail(8000))
    return 1


if __name__ == "__main__":
    sys.exit(main())
