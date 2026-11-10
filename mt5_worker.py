"""GitHub Actions execution worker for MT5 Bridge.

Claims queued jobs from the Vercel control plane, executes them on a warm
MetaTrader 5 terminal, posts results, and keeps account snapshots fresh.
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
import traceback
from typing import Any, Dict, Optional

import requests

from mt5_pool import Mt5Error, TerminalPool, WATCHLIST
from ai_engine import AIAgent
from komz_brain import KomzBrain

API = (os.environ.get("MT5_API_URL") or "").rstrip("/")
SECRET = os.environ.get("MT5_WORKER_SECRET") or ""
WORKER_ID = os.environ.get("MT5_WORKER_ID") or "github-%s" % os.environ.get("GITHUB_RUN_ID", "local")
SHARD = int(os.environ.get("MT5_SHARD", "0"))
SHARDS = int(os.environ.get("MT5_SHARDS", "1"))
DURATION = int(os.environ.get("MT5_DURATION_SECONDS", "20700"))
POLL = float(os.environ.get("MT5_POLL_SECONDS", "5"))
SNAPSHOT_INTERVAL = float(os.environ.get("MT5_SNAPSHOT_SECONDS", "1"))
DRY_RUN = os.environ.get("MT5_DRY_RUN") == "1"
AI_ENABLED = os.environ.get("MT5_AI_ENABLED", "1") == "1"
AI_INTERVAL = float(os.environ.get("MT5_AI_INTERVAL", "5"))
AI_SHARD = int(os.environ.get("MT5_AI_SHARD", "0"))
AI_SHARDS = max(1, int(os.environ.get("MT5_AI_SHARDS", "1")))

HEADERS = {"X-Worker-Secret": SECRET, "Content-Type": "application/json"}
WAKE = threading.Event()
STOP = threading.Event()
ACTIVE: Dict[str, Dict[str, Any]] = {}
ACTIVE_LOCK = threading.Lock()
POOL_LOCK = threading.RLock()


def log(message: str) -> None:
    print("[mt5-worker] %s" % message, flush=True)


def api(path: str, method: str = "POST", body: Optional[Dict[str, Any]] = None, timeout: int = 60) -> Dict[str, Any]:
    if not API or not SECRET:
        raise RuntimeError("MT5_API_URL and MT5_WORKER_SECRET are required")
    res = requests.request(
        method,
        API + path,
        headers=HEADERS,
        json=body if body is not None else {},
        timeout=timeout,
    )
    if res.status_code >= 400:
        raise RuntimeError("API %s -> %s %s" % (path, res.status_code, res.text[:300]))
    if not res.text:
        return {}
    return res.json()


def claim() -> Optional[Dict[str, Any]]:
    payload = api("/v1/worker/claim", body={"workerId": WORKER_ID, "shard": SHARD, "shards": SHARDS})
    job = payload.get("job")
    if not job:
        return None
    return payload


def execute(pool: TerminalPool, job: Dict[str, Any], creds: Dict[str, Any]) -> Any:
    op = job.get("op")
    payload = job.get("payload") or {}
    if op == "snapshot":
        return pool.snapshot(creds)
    if op == "connect":
        return pool.connect(creds)
    if op == "info":
        return pool.account(creds)
    if op == "positions":
        return {"positions": pool.positions(creds)}
    if op == "open_position":
        return pool.open_position(creds, payload)
    if op == "close_position":
        return pool.close_position(creds, payload)
    if op == "modify_position":
        return pool.modify_position(creds, payload)
    if op == "orders":
        return {"orders": pool.orders(creds)}
    if op == "place_order":
        return pool.place_order(creds, payload)
    if op == "cancel_order":
        return pool.cancel_order(creds, payload)
    if op == "history":
        return pool.history(creds, payload)
    if op == "symbols":
        return pool.symbols(creds, payload)
    if op == "prices":
        raw = payload.get("symbols") or ""
        symbols = [s.strip() for s in str(raw).split(",") if s.strip()] or WATCHLIST
        return pool.prices(creds, symbols)
    if op == "status":
        return pool.status(creds)
    if op == "close_all":
        return pool.close_all(creds)
    raise Mt5Error("Unsupported op: %s" % op, "unsupported_op")


def push_snapshot(pool: TerminalPool, account_id: str, creds: Dict[str, Any]) -> None:
    try:
        with POOL_LOCK:
            snap = pool.snapshot(creds)
        api("/v1/worker/snapshot", body={"accountId": account_id, "snapshot": snap})
    except Exception as e:
        log("snapshot failed for %s: %s" % (account_id, e))


def ai_account_seen(account_id: str, creds: Dict[str, Any]) -> None:
    with ACTIVE_LOCK:
        ACTIVE[account_id] = creds
    pool = pool_ref[0]
    if pool is not None:
        push_snapshot(pool, account_id, creds)


pool_ref: list = [None]


def ai_loop(pool: TerminalPool) -> None:
    if not AI_ENABLED:
        log("ai engine disabled")
        return
    if SHARD != AI_SHARD:
        log("ai engine skipped on shard %s (owner shard %s)" % (SHARD, AI_SHARD))
        return
    pool_ref[0] = pool
    brain = KomzBrain(log)
    agent = AIAgent(
        pool,
        lambda path, body: api(path, body=body, timeout=90),
        log,
        lock=POOL_LOCK,
        interval=AI_INTERVAL,
        on_account=ai_account_seen,
        brain=brain,
        shard=AI_SHARD,
        shards=AI_SHARDS,
    )
    log("komz brain online=%s model=%s" % (brain.available(), brain.model))
    log("ai engine online interval=%ss magic=20261109 shard=%s/%s" % (AI_INTERVAL, AI_SHARD, AI_SHARDS))
    agent.run_forever(STOP)


def process(pool: TerminalPool, claimed: Dict[str, Any]) -> None:
    job = claimed.get("job") or {}
    creds = claimed.get("credentials") or {}
    job_id = job.get("id")
    account_id = job.get("accountId")
    op = job.get("op")
    started = time.time()
    try:
        if not creds:
            raise Mt5Error("Missing credentials for job", "credential_error")
        with POOL_LOCK:
            result = execute(pool, job, creds)
        if op == "snapshot":
            api("/v1/worker/snapshot", body={"accountId": account_id, "snapshot": result})
            result = {"saved": True}
        api(
            "/v1/worker/result",
            body={"jobId": job_id, "status": "completed", "result": result},
        )
        with ACTIVE_LOCK:
            ACTIVE[account_id] = creds
        log("job %s %s completed in %.1fs" % (job_id, op, time.time() - started))
        if op not in ("snapshot", "prices", "symbols", "history", "status"):
            push_snapshot(pool, account_id, creds)
    except Exception as e:
        code = getattr(e, "code", "worker_error")
        details = getattr(e, "details", None)
        log("job %s %s failed: %s" % (job_id, op, e))
        try:
            api(
                "/v1/worker/result",
                body={
                    "jobId": job_id,
                    "status": "failed",
                    "error": {"code": code, "message": str(e), "details": details},
                },
            )
        except Exception as post_error:
            log("failed to post job error: %s" % post_error)


def heartbeat_loop(pool: TerminalPool) -> None:
    while not STOP.is_set():
        try:
            with ACTIVE_LOCK:
                accounts = list(ACTIVE.keys())
            api(
                "/v1/worker/heartbeat",
                body={
                    "workerId": WORKER_ID,
                    "shard": SHARD,
                    "shards": SHARDS,
                    "accounts": accounts,
                    "version": "1.1.0",
                    "ai": AI_ENABLED,
                    "dryRun": DRY_RUN,
                },
            )
        except Exception as e:
            log("heartbeat failed: %s" % e)
        STOP.wait(20)


def snapshot_loop(pool: TerminalPool) -> None:
    index = 0
    while not STOP.is_set():
        STOP.wait(SNAPSHOT_INTERVAL)
        with ACTIVE_LOCK:
            items = list(ACTIVE.items())
        if not items:
            continue
        index = (index + 1) % len(items)
        account_id, creds = items[index]
        push_snapshot(pool, account_id, creds)


def realtime_loop() -> None:
    url = (os.environ.get("MT5_SUPABASE_URL") or "").rstrip("/")
    key = os.environ.get("MT5_SUPABASE_KEY") or ""
    if not url or not key:
        log("realtime disabled, polling only")
        return
    try:
        import websocket
    except Exception:
        log("websocket-client not installed, polling only")
        return
    ws_url = url.replace("https://", "wss://").replace("http://", "ws://")
    ws_url += "/realtime/v1/websocket?apikey=%s&vsn=1.0.0" % key
    ref = 0
    while not STOP.is_set():
        try:
            ws = websocket.create_connection(ws_url, timeout=40)
            ref += 1
            ws.send(
                json.dumps(
                    {
                        "topic": "realtime:mt5:jobs",
                        "event": "phx_join",
                        "payload": {"config": {"broadcast": {"self": False}}},
                        "ref": str(ref),
                    }
                )
            )
            last_beat = time.time()
            while not STOP.is_set():
                try:
                    message = ws.recv()
                except Exception:
                    break
                if not message:
                    break
                if time.time() - last_beat > 25:
                    ref += 1
                    ws.send(
                        json.dumps(
                            {"topic": "phoenix", "event": "heartbeat", "payload": {}, "ref": str(ref)}
                        )
                    )
                    last_beat = time.time()
                try:
                    data = json.loads(message)
                except Exception:
                    continue
                if data.get("event") == "broadcast":
                    payload = data.get("payload") or {}
                    if payload.get("event") == "job":
                        WAKE.set()
            try:
                ws.close()
            except Exception:
                pass
        except Exception as e:
            log("realtime connection error: %s" % e)
            STOP.wait(5)


def main() -> int:
    if not API or not SECRET:
        log("MT5_API_URL and MT5_WORKER_SECRET are required")
        return 2
    pool = TerminalPool(dry_run=DRY_RUN)
    log(
        "starting worker=%s shard=%s/%s duration=%ss dryRun=%s api=%s"
        % (WORKER_ID, SHARD, SHARDS, DURATION, DRY_RUN, API)
    )
    threading.Thread(target=heartbeat_loop, args=(pool,), daemon=True).start()
    threading.Thread(target=snapshot_loop, args=(pool,), daemon=True).start()
    threading.Thread(target=realtime_loop, daemon=True).start()
    threading.Thread(target=ai_loop, args=(pool,), daemon=True).start()

    deadline = time.time() + DURATION
    processed = 0
    try:
        while time.time() < deadline:
            try:
                claimed = claim()
            except Exception as e:
                log("claim failed: %s" % e)
                time.sleep(5)
                continue
            if claimed:
                process(pool, claimed)
                processed += 1
                continue
            WAKE.wait(POLL)
            WAKE.clear()
    except KeyboardInterrupt:
        pass
    finally:
        STOP.set()
        log("worker finished, processed=%s" % processed)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        traceback.print_exc()
        sys.exit(1)
