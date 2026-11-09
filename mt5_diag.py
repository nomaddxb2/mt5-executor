import glob
import os
import subprocess
import sys
import threading
import time

import MetaTrader5 as mt5

TERMINAL = r"C:\mt5\terminal64.exe"
DATA = r"C:\mt5"


def out(*args):
    print(*args, flush=True)


def pipes():
    try:
        names = os.listdir("\\\\.\\pipe\\")
        return sorted(n for n in names if "mt5" in n.lower())
    except Exception as exc:
        return ["<listdir error %s>" % exc]


def show(label, path):
    out("--- %s %s" % (label, path))
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            out(fh.read()[:4000])
    except Exception as exc:
        out("missing:", exc)


def log_tail():
    for path in sorted(glob.glob(os.path.join(DATA, "logs", "*.log")))[-2:]:
        out("###", path)
        try:
            with open(path, "r", encoding="utf-16", errors="replace") as fh:
                out(fh.read()[-8000:])
        except Exception as exc:
            out("read error", exc)


def attempt(label, kwargs):
    out("=== initialize %s %s" % (label, kwargs))
    result = {}

    def run():
        result["ok"] = mt5.initialize(**kwargs)
        result["err"] = mt5.last_error()

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    for i in range(35):
        time.sleep(1)
        current = pipes()
        out("t=%02d pipes=%s" % (i, current))
        if not thread.is_alive():
            break
    thread.join(timeout=70)
    out("result", result)
    if result.get("ok"):
        out("terminal_info", mt5.terminal_info())
        out("account_info", mt5.account_info())
        mt5.shutdown()
    out("pipes-after", pipes())
    return bool(result.get("ok"))


out("python", sys.version)
out("MetaTrader5", mt5.__version__)
out("cwd", os.getcwd())
out("terminal exists", os.path.exists(TERMINAL))
out("pipes-before", pipes())
show("portable.txt before", os.path.join(DATA, "portable.txt"))
show("origin.txt before", os.path.join(DATA, "origin.txt"))

if not attempt("path+portable", {"path": TERMINAL, "portable": True}):
    attempt("path", {"path": TERMINAL})

out("processes")
subprocess.run(["tasklist", "/FI", "IMAGENAME eq terminal64.exe"], check=False)
show("portable.txt after", os.path.join(DATA, "portable.txt"))
show("origin.txt after", os.path.join(DATA, "origin.txt"))
show("terminal.ini", os.path.join(DATA, "config", "terminal.ini"))
log_tail()
