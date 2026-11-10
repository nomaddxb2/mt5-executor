"""Komz — LO's autonomous trading second brain.

The deterministic engine in ai_engine.py handles fast execution. This module
adds the slow, deliberative layer: NVIDIA-hosted reasoning, market research,
post-trade reflection, recovery/compounding policy, and a chunked journal
stored in Telegram with a Supabase mirror.

Secrets are read from the environment only. They are never written to state,
logs, prompts, or Telegram messages.
"""

from __future__ import annotations

import html
import json
import os
import re
import threading
import time
import urllib.parse
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import requests

NVIDIA_URL = "https://integrate.api.nvidia.com/v1/chat/completions"
NVIDIA_MODELS_URL = "https://integrate.api.nvidia.com/v1/models"
DEFAULT_MODEL = "nvidia/nemotron-3.5-lightning-30b-a3b"
TELEGRAM_API = "https://api.telegram.org/bot%s/%s"
SUPABASE_BUCKET = os.environ.get("MT5_STORE_BUCKET") or "mt5-bridge"

COUNCIL_ROLES = [
    ("Momentum", "trend continuation, breakouts and orderflow alignment"),
    ("Mean Reversion", "exhaustion, fade setups and value"),
    ("Risk Guardian", "drawdown, exposure, spread, min-lot risk and correlation"),
    ("Macro Scout", "news, session timing, liquidity and regime"),
    ("Devil's Advocate", "find the strongest reason this trade fails"),
    ("Portfolio Chair", "choose the best use of capital across candidates"),
]

NEWS_FEEDS = [
    ("FXStreet", "https://www.fxstreet.com/rss/news"),
    ("Investing", "https://www.investing.com/rss/news_1.rss"),
    ("Yahoo Finance", "https://feeds.finance.yahoo.com/rss/2.0/headline?s=EURUSD=X&region=US&lang=en-US"),
    ("CoinDesk", "https://www.coindesk.com/arc/outboundfeeds/rss/"),
]


def _iso(ts: Optional[float] = None) -> str:
    return datetime.fromtimestamp(ts or time.time(), tz=timezone.utc).isoformat().replace("+00:00", "Z")


def _clamp(value: Any, lo: float, hi: float, fallback: float = 0.0) -> float:
    try:
        n = float(value)
    except (TypeError, ValueError):
        return fallback
    return max(lo, min(hi, n))


def _redact(text: Any) -> str:
    s = str(text or "")
    for key in ("NVIDIA_API_KEY", "TELEGRAM_BOT_TOKEN"):
        secret = os.environ.get(key) or ""
        if secret:
            s = s.replace(secret, "<redacted>")
    s = re.sub(r"nvapi-[A-Za-z0-9_\-]+", "nvapi-<redacted>", s)
    s = re.sub(r"\b\d{8,10}:[A-Za-z0-9_\-]{30,}\b", "<telegram-token>", s)
    return s[:600]


def _strip_tags(value: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", "", value or "")).strip()


def _extract_json(text: str) -> Optional[Dict[str, Any]]:
    raw = (text or "").strip()
    if not raw:
        return None
    raw = re.sub(r"^```(?:json)?\s*", "", raw, flags=re.I)
    raw = re.sub(r"\s*```$", "", raw)
    try:
        value = json.loads(raw)
        return value if isinstance(value, dict) else None
    except Exception:
        pass
    start = raw.find("{")
    end = raw.rfind("}")
    if start >= 0 and end > start:
        try:
            value = json.loads(raw[start:end + 1])
            return value if isinstance(value, dict) else None
        except Exception:
            return None
    return None


def _chunks(text: str, limit: int = 3200) -> List[str]:
    text = text or ""
    out: List[str] = []
    while text:
        if len(text) <= limit:
            out.append(text)
            break
        cut = text.rfind("\n", 0, limit)
        if cut < limit // 2:
            cut = text.rfind(" ", 0, limit)
        if cut < limit // 2:
            cut = limit
        out.append(text[:cut].rstrip())
        text = text[cut:].lstrip()
    return out or [""]


class KomzMemory:
    """Chunked journal: Telegram first, Supabase storage as the durable mirror."""

    def __init__(self, log) -> None:
        self.log = log
        self.token = os.environ.get("TELEGRAM_BOT_TOKEN") or ""
        self.chat_id = str(os.environ.get("TELEGRAM_CHAT_ID") or "").strip()
        self.supabase_url = (os.environ.get("MT5_SUPABASE_URL") or "").rstrip("/")
        self.supabase_key = os.environ.get("MT5_SUPABASE_KEY") or ""
        self.chunks = 0
        self.last_write_at: Optional[str] = None
        self.last_transport = "none"
        self._lock = threading.Lock()

    def status(self) -> Dict[str, Any]:
        return {
            "telegram": bool(self.token and self.chat_id),
            "chat": (self.chat_id[:4] + "…") if self.chat_id else None,
            "supabase": bool(self.supabase_url and self.supabase_key),
            "chunks": self.chunks,
            "lastWriteAt": self.last_write_at,
            "lastTransport": self.last_transport,
        }

    def _discover_chat(self) -> bool:
        if not self.token or self.chat_id:
            return bool(self.chat_id)
        try:
            res = requests.get(
                TELEGRAM_API % (self.token, "getUpdates"),
                params={"limit": 20, "timeout": 0},
                timeout=12,
            )
            data = res.json() if res.content else {}
            for update in reversed(data.get("result") or []):
                message = update.get("message") or update.get("channel_post") or {}
                chat = message.get("chat") or {}
                chat_id = chat.get("id")
                if chat_id:
                    self.chat_id = str(chat_id)
                    self.log("komz telegram chat discovered: %s…" % self.chat_id[:4])
                    return True
        except Exception as e:
            self.log("komz telegram discovery failed: %s" % _redact(e))
        return False

    def _telegram(self, text: str, kind: str) -> bool:
        if not self.token:
            return False
        if not self.chat_id and not self._discover_chat():
            return False
        parts = _chunks(text)
        sent = 0
        for index, part in enumerate(parts, 1):
            header = "[Komz · %s · %s · %d/%d]\n" % (kind, _iso(), index, len(parts))
            try:
                res = requests.post(
                    TELEGRAM_API % (self.token, "sendMessage"),
                    json={
                        "chat_id": self.chat_id,
                        "text": header + part,
                        "disable_web_page_preview": True,
                    },
                    timeout=20,
                )
                if res.status_code == 429:
                    retry = 2
                    try:
                        retry = int((res.json().get("parameters") or {}).get("retry_after") or 2)
                    except Exception:
                        pass
                    time.sleep(min(retry, 8))
                    res = requests.post(
                        TELEGRAM_API % (self.token, "sendMessage"),
                        json={"chat_id": self.chat_id, "text": header + part, "disable_web_page_preview": True},
                        timeout=20,
                    )
                if res.status_code >= 400:
                    self.log("komz telegram send failed: %s" % _redact(res.text[:200]))
                    return sent > 0
                sent += 1
                time.sleep(0.35)
            except Exception as e:
                self.log("komz telegram send error: %s" % _redact(e))
                return sent > 0
        return sent > 0

    def _supabase(self, account_id: str, kind: str, text: str) -> bool:
        api_url = (os.environ.get("MT5_API_URL") or "").rstrip("/")
        worker_secret = os.environ.get("MT5_WORKER_SECRET") or ""
        if api_url and worker_secret:
            try:
                res = requests.post(
                    api_url + "/v1/worker/komz/memory",
                    headers={"x-worker-secret": worker_secret},
                    json={"accountId": account_id, "kind": kind, "text": text},
                    timeout=30,
                )
                if res.status_code < 400:
                    return True
                self.log("komz mirror api failed: %s" % _redact(res.text[:200]))
            except Exception as e:
                self.log("komz mirror api error: %s" % _redact(e))
        if not (self.supabase_url and self.supabase_key):
            return False
        safe_account = re.sub(r"[^A-Za-z0-9_-]", "_", account_id or "account")
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        parts = _chunks(text)
        ok = 0
        for index, part in enumerate(parts, 1):
            path = "komz/%s/%s-%s-%02d.txt" % (safe_account, stamp, kind, index)
            url = "%s/storage/v1/object/%s/%s" % (self.supabase_url, SUPABASE_BUCKET, urllib.parse.quote(path))
            try:
                res = requests.post(
                    url,
                    headers={
                        "Authorization": "Bearer " + self.supabase_key,
                        "Content-Type": "application/json",
                        "x-upsert": "true",
                    },
                    data=part.encode("utf-8"),
                    timeout=20,
                )
                if res.status_code >= 400:
                    self.log("komz supabase write failed: %s" % _redact(res.text[:200]))
                    return ok > 0
                ok += 1
            except Exception as e:
                self.log("komz supabase write error: %s" % _redact(e))
                return ok > 0
        return ok > 0

    def write(self, account_id: str, kind: str, text: str) -> Dict[str, Any]:
        with self._lock:
            payload = "%s\n\n%s" % (_iso(), text)
            telegram_ok = self._telegram(payload, kind)
            supabase_ok = self._supabase(account_id, kind, payload)
            self.chunks += len(_chunks(payload))
            self.last_write_at = _iso()
            self.last_transport = "telegram" if telegram_ok else ("supabase" if supabase_ok else "none")
            return {
                "telegram": telegram_ok,
                "supabase": supabase_ok,
                "chunks": len(_chunks(payload)),
                "transport": self.last_transport,
            }


class KomzCouncil:
    """A family of models that debate a candidate and hand a verdict to the chair."""

    def __init__(self, brain) -> None:
        self.brain = brain
        self.enabled = os.environ.get("KOMZ_COUNCIL", "1") != "0"
        self.max_models = max(1, min(6, int(os.environ.get("KOMZ_COUNCIL_MODELS", "4") or 4)))
        self.rounds = max(1, min(3, int(os.environ.get("KOMZ_COUNCIL_ROUNDS", "2") or 2)))
        self.models: List[Dict[str, Any]] = []
        self._models_at = 0.0
        self._lock = threading.Lock()
        self._cache: Dict[str, Dict[str, Any]] = {}
        self.running = False
        self.last: Optional[Dict[str, Any]] = None
        self.discoveries: List[Dict[str, Any]] = []

    @staticmethod
    def _short(model: str) -> str:
        text = str(model or "model").split("/")[-1]
        return text[:34]

    def _discover(self) -> List[Dict[str, Any]]:
        with self._lock:
            if self.models and time.time() - self._models_at < 3600:
                return self.models
        chosen: List[str] = []
        try:
            res = requests.get(
                NVIDIA_MODELS_URL,
                headers={"Authorization": "Bearer " + (self.brain.key or "")},
                timeout=15,
            )
            if res.status_code < 400:
                scored = []
                for row in (res.json().get("data") or []):
                    mid = str(row.get("id") or "")
                    low = mid.lower()
                    if not mid or mid in chosen:
                        continue
                    if any(x in low for x in ("embed", "rerank", "guard", "safety", "vision", "image", "video", "audio", "tts", "asr", "ocr", "parse")):
                        continue
                    score = 0
                    if "instruct" in low or "chat" in low:
                        score += 5
                    if "nemotron" in low:
                        score += 4
                    if "llama" in low:
                        score += 3
                    if "qwen" in low:
                        score += 2
                    if "mistral" in low or "mixtral" in low:
                        score += 2
                    if "deepseek" in low:
                        score += 2
                    if score:
                        scored.append((score, mid))
                scored.sort(key=lambda x: (-x[0], x[1]))
                chosen = [mid for _, mid in scored]
        except Exception as e:
            self.brain.log("komz council model discovery failed: %s" % _redact(e))
        if self.brain.model and self.brain.model not in chosen:
            chosen.insert(0, self.brain.model)
        if not chosen:
            chosen = [self.brain.model or DEFAULT_MODEL]
        chosen = chosen[: self.max_models]
        models = []
        for index, mid in enumerate(chosen):
            role, lens = COUNCIL_ROLES[index % len(COUNCIL_ROLES)]
            models.append({"id": mid, "label": self._short(mid), "role": role, "lens": lens})
        with self._lock:
            self.models = models
            self._models_at = time.time()
        return models

    def _member(self, model: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "id": model["id"],
            "label": model["label"],
            "role": model["role"],
            "lens": model["lens"],
            "ok": False,
            "stance": None,
            "confidence": 0.0,
            "thesis": "",
            "risk": "",
            "discovery": "",
            "latency": None,
            "at": None,
            "error": None,
        }

    def _notify(self, progress) -> None:
        if not progress:
            return
        try:
            progress()
        except Exception:
            pass

    def status(self) -> Dict[str, Any]:
        with self._lock:
            last = self.last or {}
            models = [dict(m) for m in self.models]
            members = [dict(m) for m in (last.get("members") or [])]
            transcript = list(last.get("transcript") or [])[-12:]
            discoveries = [dict(d) for d in self.discoveries[-8:]]
        return {
            "enabled": self.enabled,
            "running": self.running,
            "rounds": self.rounds,
            "models": models,
            "members": members,
            "judge": dict(last.get("judge") or {}) if last.get("judge") else None,
            "phase": last.get("phase"),
            "summary": last.get("summary"),
            "lastMeeting": last.get("finishedAt"),
            "meetingId": last.get("meetingId"),
            "transcript": transcript,
            "discoveries": discoveries,
        }

    def _ask(self, member: Dict[str, Any], phase: str, context: str, candidate: Dict[str, Any], peers: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
        role = member["role"]
        lens = member["lens"]
        system = (
            "You are %s, one agent in Komz's trading family. Your lens: %s. "
            "Be decisive and concrete. You judge the deterministic candidate; never invent a trade, "
            "never change volume, never exceed the 2%% min-lot risk cap. "
            "Return ONLY one strict JSON object." % (role, lens)
        )
        if phase == "round1":
            user = (
                "Independent take on this candidate. Look for a real edge, but do not demand perfection; "
                "we want good trades, not zero trades. Say hold only when the setup is genuinely poor.\n"
                + context
                + '\nReturn JSON: {"stance":"buy|sell|hold","confidence":0.0,"thesis":"one sentence","risk":"one sentence","discovery":"one reusable observation"}'
            )
            max_tokens = 520
        else:
            family = "\n".join(
                "- %s (%s): %s %.2f — %s" % (p.get("role"), p.get("label"), str(p.get("stance") or "hold").upper(), float(p.get("confidence") or 0), str(p.get("thesis") or "")[:180])
                for p in (peers or [])
            )
            user = (
                "Round 2 cross-examination. Read your family's takes, then revise only if their evidence changes your mind. "
                "The chair wants the best available trade, not a perfect one.\n"
                + context
                + "\nYour round-1 take: %s %.2f — %s\nFamily:\n%s\n" % (
                    str(member.get("stance") or "hold").upper(),
                    float(member.get("confidence") or 0),
                    str(member.get("thesis") or "")[:200],
                    family,
                )
                + '\nReturn JSON: {"stance":"buy|sell|hold","confidence":0.0,"changed":false,"comment":"one sentence","discovery":"one reusable observation"}'
            )
            max_tokens = 380
        started = time.time()
        parsed = self.brain._json(system, user, max_tokens=max_tokens, reasoning="none", model=member["id"], timeout=32)
        latency = round(time.time() - started, 2)
        if not parsed:
            return {"ok": False, "latency": latency, "error": "no JSON"}
        stance = str(parsed.get("stance") or parsed.get("action") or "hold").lower()
        if stance in ("long", "enter", "buy"):
            stance = "buy"
        elif stance in ("short", "sell"):
            stance = "sell"
        else:
            stance = "hold"
        return {
            "ok": True,
            "stance": stance,
            "confidence": round(_clamp(parsed.get("confidence"), 0.0, 1.0, 0.45), 3),
            "thesis": str(parsed.get("thesis") or parsed.get("comment") or "")[:280],
            "risk": str(parsed.get("risk") or "")[:220],
            "discovery": str(parsed.get("discovery") or "")[:220],
            "latency": latency,
            "error": None,
        }

    def review(self, account_id: str, cfg: Dict[str, Any], state: Dict[str, Any], candidate: Dict[str, Any], market: Dict[str, Any], info: Dict[str, Any], progress=None) -> Optional[Dict[str, Any]]:
        if not self.enabled or not self.brain.available():
            return None
        sym = str(candidate.get("symbol") or "")
        side = str(candidate.get("side") or "")
        if not sym or side not in ("buy", "sell"):
            return None
        cache_key = "%s:%s:%d" % (sym, side, int(time.time() // 60))
        with self._lock:
            cached = self._cache.get(cache_key)
            if cached and time.time() - cached.get("_at", 0) < 60:
                return cached
        models = self._discover()
        meeting: Dict[str, Any] = {
            "meetingId": "c%d" % int(time.time() * 1000),
            "startedAt": _iso(),
            "phase": "round1",
            "candidate": {k: candidate.get(k) for k in ("symbol", "side", "confidence", "price", "spread", "atr", "regime", "summary")},
            "members": [self._member(m) for m in models],
            "judge": None,
            "summary": "",
            "transcript": [],
            "discoveries": [],
        }
        self.running = True
        with self._lock:
            self.last = meeting
        self._notify(progress)
        context = self.brain._context(state, info, market, [candidate])
        context["candidate"] = context["candidates"][0] if context.get("candidates") else {}
        context_text = json.dumps(context, separators=(",", ":"))[:6800]
        members = meeting["members"]
        with ThreadPoolExecutor(max_workers=max(1, len(members))) as pool:
            futures = {pool.submit(self._ask, m, "round1", context_text, candidate): m for m in members}
            for fut in as_completed(futures):
                member = futures[fut]
                try:
                    result = fut.result()
                except Exception as e:
                    result = {"ok": False, "error": _redact(e), "latency": None}
                member.update(result)
                member["at"] = _iso()
                meeting["transcript"].append("%s [%s] %s %.2f — %s" % (
                    member["role"], member["label"], str(member.get("stance") or "hold").upper(),
                    float(member.get("confidence") or 0), member.get("thesis") or member.get("error") or "",
                ))
                if member.get("discovery"):
                    meeting["discoveries"].append({"at": member["at"], "role": member["role"], "model": member["label"], "text": member["discovery"]})
                self._notify(progress)
        if self.rounds >= 2:
            meeting["phase"] = "round2"
            ok_members = [m for m in members if m.get("ok")]
            peers = [{"role": m["role"], "label": m["label"], "stance": m.get("stance"), "confidence": m.get("confidence"), "thesis": m.get("thesis")} for m in ok_members]
            if len(ok_members) >= 2:
                with ThreadPoolExecutor(max_workers=max(1, len(ok_members))) as pool:
                    futures = {pool.submit(self._ask, m, "round2", context_text, candidate, peers): m for m in ok_members}
                    for fut in as_completed(futures):
                        member = futures[fut]
                        try:
                            result = fut.result()
                        except Exception as e:
                            result = {"ok": False, "error": _redact(e), "latency": None}
                        member.update(result)
                        member["at"] = _iso()
                        meeting["transcript"].append("%s revised %s %.2f — %s" % (
                            member["role"], str(member.get("stance") or "hold").upper(),
                            float(member.get("confidence") or 0), member.get("thesis") or "",
                        ))
                        if member.get("discovery"):
                            meeting["discoveries"].append({"at": member["at"], "role": member["role"], "model": member["label"], "text": member["discovery"]})
                        self._notify(progress)
        meeting["phase"] = "judgement"
        self._notify(progress)
        ok_members = [m for m in members if m.get("ok")]
        side_votes = [m for m in ok_members if m.get("stance") == side]
        avg_conf = sum(float(m.get("confidence") or 0) for m in side_votes) / len(side_votes) if side_votes else 0.0
        judge_model = self.brain.model if self.brain.model in [m["id"] for m in models] else (models[0]["id"] if models else self.brain.model)
        family_text = "\n".join(
            "- %s (%s): %s %.2f — %s | risk: %s" % (
                m["role"], m["label"], str(m.get("stance") or "hold").upper(),
                float(m.get("confidence") or 0), str(m.get("thesis") or "")[:220], str(m.get("risk") or "")[:140],
            ) for m in ok_members
        )
        judge_system = (
            "You are the Portfolio Chair of Komz's agent family. Decide whether the deterministic candidate is worth trading now. "
            "Protect capital, but do not demand perfection: LO wants more good trades, recoveries and discoveries. "
            "Approve when there is a coherent edge and no hard risk violation. Reject on genuine danger. "
            "Return ONLY one strict JSON object."
        )
        judge_user = (
            "Candidate and portfolio:\n" + context_text
            + "\nFamily views:\n" + family_text
            + '\nReturn JSON: {"decision":"approve|reject|wait","confidence":0.0,"reason":"one sentence","risk_note":"one sentence","size_multiplier":0.5,"discoveries":["..."]}'
        )
        judge = self.brain._json(judge_system, judge_user, max_tokens=900, reasoning="none", model=judge_model, timeout=32) or {}
        decision_raw = str(judge.get("decision") or "").lower()
        if decision_raw in ("approve", "buy", "sell", "enter", "long", "short"):
            approve = True
        elif decision_raw in ("reject", "no", "skip"):
            approve = False
        else:
            approve = len(side_votes) >= max(1, len(ok_members) // 2 + 1) and avg_conf >= 0.45
        risk_member = next((m for m in ok_members if "risk" in str(m.get("role") or "").lower()), None)
        if risk_member and risk_member.get("stance") == "hold" and float(risk_member.get("confidence") or 0) >= 0.80:
            approve = False
        if not ok_members:
            approve = False
        confidence = _clamp(judge.get("confidence"), 0.0, 1.0, avg_conf or 0.45)
        judge_out = {
            "model": self._short(judge_model),
            "decision": "approve" if approve else "reject",
            "confidence": round(confidence, 3),
            "reason": str(judge.get("reason") or ("%d/%d family members aligned with %s" % (len(side_votes), len(ok_members), side.upper())))[:300],
            "riskNote": str(judge.get("risk_note") or (risk_member.get("risk") if risk_member else ""))[:240],
            "sizeMultiplier": round(_clamp(judge.get("size_multiplier"), 0.25, 1.0, 1.0), 3),
            "at": _iso(),
        }
        for text in (judge.get("discoveries") or [])[:4]:
            if text:
                meeting["discoveries"].append({"at": _iso(), "role": "Chair", "model": judge_out["model"], "text": str(text)[:220]})
        meeting["judge"] = judge_out
        meeting["summary"] = "%d/%d aligned · chair %s %.2f · %s" % (
            len(side_votes), len(ok_members), judge_out["decision"], judge_out["confidence"], judge_out["reason"][:120])
        meeting["finishedAt"] = _iso()
        meeting["phase"] = "done"
        with self._lock:
            self.discoveries = (self.discoveries + meeting["discoveries"])[-40:]
            self.last = meeting
            self.running = False
        self._notify(progress)
        result = {
            "action": "approve" if approve else "reject",
            "confidence": round(confidence, 3),
            "reason": judge_out["reason"],
            "riskNote": judge_out["riskNote"],
            "lesson": (meeting["discoveries"][-1]["text"] if meeting["discoveries"] else ""),
            "council": meeting,
            "at": _iso(),
            "_at": time.time(),
        }
        with self._lock:
            self._cache[cache_key] = result
            if len(self._cache) > 60:
                self._cache = dict(list(self._cache.items())[-40:])
        return result


class KomzBrain:
    def __init__(self, log, model: Optional[str] = None) -> None:
        self.log = log
        self.key = os.environ.get("NVIDIA_API_KEY") or ""
        self.enabled = os.environ.get("KOMZ_ENABLED", "1") == "1"
        self.model = model or os.environ.get("KOMZ_MODEL") or DEFAULT_MODEL
        self.brain_interval = float(os.environ.get("KOMZ_BRAIN_INTERVAL", "90"))
        self.research_interval = float(os.environ.get("KOMZ_RESEARCH_SECONDS", "900"))
        self.reasoning = os.environ.get("KOMZ_REASONING", "low")
        self.memory = KomzMemory(log)
        self.council = KomzCouncil(self)
        self.last_call_at = 0.0
        self.last_error: Optional[str] = None
        self._review_cache: Dict[str, Dict[str, Any]] = {}
        self._lock = threading.Lock()

    def available(self) -> bool:
        return bool(self.enabled and self.key)

    def status(self) -> Dict[str, Any]:
        return {
            "name": "Komz",
            "enabled": self.enabled,
            "available": self.available(),
            "model": self.model,
            "lastCallAt": _iso(self.last_call_at) if self.last_call_at else None,
            "lastError": self.last_error,
            "memory": self.memory.status(),
            "council": self.council.status(),
            "brainInterval": self.brain_interval,
            "researchInterval": self.research_interval,
        }

    def _chat(self, system: str, user: str, max_tokens: int = 3000, temperature: float = 0.2, reasoning: Optional[str] = None, model: Optional[str] = None, timeout: float = 85) -> Optional[str]:
        if not self.available():
            return None
        payload = {
            "model": model or self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": temperature,
            "max_tokens": max_tokens,
            "reasoning_effort": reasoning or self.reasoning,
        }
        headers = {"Authorization": "Bearer " + self.key, "Content-Type": "application/json"}
        for attempt in (1, 2):
            try:
                self.last_call_at = time.time()
                res = requests.post(NVIDIA_URL, headers=headers, json=payload, timeout=timeout)
                if res.status_code == 429 and attempt == 1:
                    time.sleep(3)
                    continue
                if res.status_code >= 400:
                    self.last_error = "nvidia %s: %s" % (res.status_code, _redact(res.text[:200]))
                    self.log("komz brain: %s" % self.last_error)
                    return None
                data = res.json()
                choice = (data.get("choices") or [{}])[0]
                content = (choice.get("message") or {}).get("content")
                finish = choice.get("finish_reason")
                if not content and finish == "length":
                    self.log("komz brain: length limit hit (max_tokens=%s, reasoning=%s)" % (payload["max_tokens"], (data.get("usage") or {}).get("completion_tokens_details")))
                    if attempt == 1:
                        payload["max_tokens"] = min(4200, int(payload["max_tokens"]) + 900)
                        continue
                self.last_error = None
                return content or None
            except Exception as e:
                self.last_error = _redact(e)
                self.log("komz brain error: %s" % self.last_error)
                if attempt == 2:
                    return None
                time.sleep(1.5)
        return None

    def _json(self, system: str, user: str, max_tokens: int = 3000, reasoning: Optional[str] = None, model: Optional[str] = None, timeout: float = 85) -> Optional[Dict[str, Any]]:
        content = self._chat(system, user, max_tokens=max_tokens, reasoning=reasoning, model=model, timeout=timeout)
        if not content:
            return None
        parsed = _extract_json(content)
        if not parsed:
            self.last_error = "model returned non-JSON"
            self.log("komz brain: non-JSON response")
        return parsed

    def _portfolio(self, info: Dict[str, Any], state: Dict[str, Any]) -> Dict[str, Any]:
        equity = float(info.get("equity") or info.get("balance") or state.get("equity") or 0)
        return {
            "balance": round(float(info.get("balance") or 0), 2),
            "equity": round(equity, 2),
            "floating": round(float(state.get("floating") or 0), 2),
            "todayPnl": state.get("todayPnl"),
            "aiPnl": state.get("aiPnl"),
            "openPositions": [
                {k: p.get(k) for k in ("ticket", "symbol", "side", "volume", "openPrice", "profit", "sl", "tp")}
                for p in (state.get("positions") or [])[:6]
            ],
            "stats": state.get("stats"),
        }

    def _context(self, state: Dict[str, Any], info: Dict[str, Any], market: Dict[str, Any], rows: List[Dict[str, Any]]) -> Dict[str, Any]:
        top = sorted(rows, key=lambda r: r.get("confidence") or 0, reverse=True)[:4]
        return {
            "portfolio": self._portfolio(info, state),
            "candidates": [
                {
                    "symbol": r.get("symbol"),
                    "side": r.get("side"),
                    "confidence": r.get("confidence"),
                    "price": r.get("price"),
                    "spread": r.get("spread"),
                    "spread_atr_ratio": round(float(r.get("spread") or 0) / float(r.get("atr") or 1), 4) if r.get("atr") else None,
                    "atr": r.get("atr"),
                    "atrPct": r.get("atrPct"),
                    "trend": r.get("trend"),
                    "regime": r.get("regime"),
                    "summary": r.get("summary"),
                    "flow": {
                        "score": r.get("flowScore"),
                        "pressure": r.get("flowPressure"),
                        "depthImbalance": r.get("depthImbalance"),
                        "tickDeltaRatio": r.get("tickDeltaRatio"),
                        "bidDepth": r.get("bidDepth"),
                        "askDepth": r.get("askDepth"),
                    },
                    "reasons": (r.get("reasons") or [])[:3],
                }
                for r in top
            ],
            "recentDecisions": (state.get("decisions") or [])[-6:],
            "lessons": (state.get("komz", {}).get("lessons") or [])[-6:],
            "research": (state.get("komz", {}).get("research") or [])[-2:],
            "recovery": state.get("komz", {}).get("recovery"),
            "rules": {
                "minLotRiskCapPct": 2.0,
                "maxPositions": state.get("params", {}).get("maxPositions"),
                "riskPct": state.get("params", {}).get("riskPct"),
                "dailyLossPct": state.get("params", {}).get("maxDailyLossPct"),
            },
        }

    def _system(self) -> str:
        return (
            "You are Komz, LO's autonomous MT5 trading second brain. "
            "You are disciplined, adaptive and research-driven: protect capital first, compound second, recover without revenge trading. "
            "The deterministic engine gives you a candidate. You may only approve or reject that candidate; never invent a trade, never change volume, never exceed the 2% min-lot risk cap. "
            "If the setup is unclear, reject. Learn from every outcome and keep a compact, reusable playbook. "
            "Return ONLY one strict JSON object with the requested keys. No markdown, no prose outside JSON."
        )

    def review_entry(
        self,
        account_id: str,
        cfg: Dict[str, Any],
        state: Dict[str, Any],
        candidate: Dict[str, Any],
        market: Dict[str, Any],
        info: Dict[str, Any],
        progress=None,
    ) -> Optional[Dict[str, Any]]:
        if not self.available():
            return None
        sym = str(candidate.get("symbol") or "")
        side = str(candidate.get("side") or "")
        if not sym or side not in ("buy", "sell"):
            return None
        if self.council.enabled:
            result = self.council.review(account_id, cfg, state, candidate, market, info, progress=progress)
            if result:
                cache_key = "%s:%s:%d" % (sym, side, int(time.time() // max(45, self.brain_interval)))
                with self._lock:
                    self._review_cache[cache_key] = result
                    if len(self._review_cache) > 40:
                        self._review_cache = dict(list(self._review_cache.items())[-30:])
                return result
        cache_key = "%s:%s:%d" % (sym, side, int(time.time() // max(45, self.brain_interval)))
        with self._lock:
            cached = self._review_cache.get(cache_key)
            if cached and time.time() - cached.get("_at", 0) < max(60, self.brain_interval):
                return cached
        context = self._context(state, info, market, [candidate])
        context["candidate"] = context["candidates"][0] if context["candidates"] else {}
        user = (
            "Review this deterministic candidate. Approve only if the edge is coherent, spread/ATR is sane, "
            "the portfolio is not overexposed, and recovery rules are respected.\n"
            + json.dumps(context, separators=(",", ":"))[:7000]
            + '\nReturn JSON: {"action":"approve|reject","confidence":0.0,"reason":"one sentence","risk_note":"one sentence","lesson":"optional one sentence"}'
        )
        parsed = self._json(self._system(), user, max_tokens=1100, reasoning="none")
        if not parsed:
            return None
        action = str(parsed.get("action") or "").lower()
        approve = action in ("approve", "buy", "sell", "enter", "long", "short")
        if action in ("reject", "hold", "no", "wait", "skip"):
            approve = False
        confidence = _clamp(parsed.get("confidence"), 0.0, 1.0, 0.5)
        if confidence < 0.35:
            approve = False
        result = {
            "action": "approve" if approve else "reject",
            "confidence": round(confidence, 3),
            "reason": str(parsed.get("reason") or "")[:280],
            "riskNote": str(parsed.get("risk_note") or "")[:240],
            "lesson": str(parsed.get("lesson") or "")[:240],
            "at": _iso(),
            "_at": time.time(),
        }
        with self._lock:
            self._review_cache[cache_key] = result
            if len(self._review_cache) > 40:
                self._review_cache = dict(list(self._review_cache.items())[-30:])
        return result

    def _news(self) -> List[Dict[str, str]]:
        out: List[Dict[str, str]] = []
        for name, url in NEWS_FEEDS:
            try:
                res = requests.get(
                    url,
                    timeout=7,
                    headers={"User-Agent": "Mozilla/5.0 (Komz research; +https://mt5bridge.vercel.app)"},
                )
                if res.status_code >= 400:
                    continue
                root = ET.fromstring(res.content)
                for item in root.findall(".//item")[:6]:
                    title = _strip_tags(item.findtext("title") or "")
                    published = _strip_tags(item.findtext("pubDate") or "")
                    if title:
                        out.append({"source": name, "title": title[:180], "published": published[:40]})
            except Exception:
                continue
        seen = set()
        deduped = []
        for item in out:
            key = item["title"].lower()
            if key in seen:
                continue
            seen.add(key)
            deduped.append(item)
        return deduped[:18]

    def research(
        self,
        account_id: str,
        cfg: Dict[str, Any],
        state: Dict[str, Any],
        market: Dict[str, Any],
        rows: List[Dict[str, Any]],
        info: Dict[str, Any],
        force: bool = False,
    ) -> Optional[Dict[str, Any]]:
        if not self.available():
            return None
        komz = state.setdefault("komz", {})
        last = komz.get("lastResearchAt")
        if not force and last:
            try:
                age = time.time() - datetime.fromisoformat(str(last).replace("Z", "+00:00")).timestamp()
                if age < self.research_interval:
                    return None
            except Exception:
                pass
        headlines = self._news()
        context = self._context(state, info, market, rows)
        user = (
            "Research the current market regime and update the playbook. Use live headlines plus price/portfolio context. "
            "Be concrete and risk-aware.\n"
            + json.dumps({"headlines": headlines, "context": context}, separators=(",", ":"))[:8000]
            + '\nReturn JSON: {"regime":"trend|range|volatile|quiet","bias":"bullish|bearish|neutral","confidence":0.0,"notes":["..."],"risks":["..."],"playbook":["..."]}'
        )
        parsed = self._json(self._system(), user, max_tokens=3000, reasoning="low")
        if not parsed:
            return None
        note = {
            "at": _iso(),
            "model": self.model,
            "regime": str(parsed.get("regime") or "unknown")[:40],
            "bias": str(parsed.get("bias") or "neutral")[:20],
            "confidence": round(_clamp(parsed.get("confidence"), 0.0, 1.0, 0.5), 3),
            "notes": [str(x)[:240] for x in (parsed.get("notes") or [])[:5]],
            "risks": [str(x)[:240] for x in (parsed.get("risks") or [])[:5]],
            "playbook": [str(x)[:240] for x in (parsed.get("playbook") or [])[:5]],
            "headlines": headlines[:8],
        }
        research = list(komz.get("research") or [])
        research.append(note)
        komz["research"] = research[-20:]
        komz["lastResearchAt"] = note["at"]
        text = (
            "RESEARCH %s\nregime=%s bias=%s confidence=%.2f\n"
            "notes:\n- %s\nrisks:\n- %s\nplaybook:\n- %s\nheadlines:\n- %s"
            % (
                note["at"],
                note["regime"],
                note["bias"],
                note["confidence"],
                "\n- ".join(note["notes"]),
                "\n- ".join(note["risks"]),
                "\n- ".join(note["playbook"]),
                "\n- ".join("%s: %s" % (h.get("source"), h.get("title")) for h in headlines[:8]),
            )
        )
        memory = self.memory.write(account_id, "research", text)
        komz["memory"] = self.memory.status()
        komz["lastMemoryWrite"] = memory
        return note

    def reflect(
        self,
        account_id: str,
        cfg: Dict[str, Any],
        state: Dict[str, Any],
        trade: Dict[str, Any],
        info: Dict[str, Any],
    ) -> Optional[Dict[str, Any]]:
        if not self.available():
            return None
        context = self._context(state, info, {}, [])
        context["closedTrade"] = trade
        user = (
            "Reflect on this closed trade. Be honest about process, not just outcome. "
            "Extract one reusable lesson and a small risk bias adjustment (-0.25 to +0.25).\n"
            + json.dumps(context, separators=(",", ":"))[:7000]
            + '\nReturn JSON: {"lesson":"one sentence","mistake":"one sentence or empty","strength":"one sentence or empty","next_action":"one sentence","risk_bias":0.0}'
        )
        parsed = self._json(self._system(), user, max_tokens=1200, reasoning="none")
        if not parsed:
            return None
        lesson = {
            "at": _iso(),
            "trade": {k: trade.get(k) for k in ("ticket", "symbol", "side", "volume", "pnl", "r", "reason")},
            "lesson": str(parsed.get("lesson") or "")[:300],
            "mistake": str(parsed.get("mistake") or "")[:240],
            "strength": str(parsed.get("strength") or "")[:240],
            "nextAction": str(parsed.get("next_action") or "")[:240],
            "riskBias": round(_clamp(parsed.get("risk_bias"), -0.25, 0.25, 0.0), 3),
        }
        komz = state.setdefault("komz", {})
        lessons = list(komz.get("lessons") or [])
        lessons.append(lesson)
        komz["lessons"] = lessons[-50:]
        old_bias = float(komz.get("riskBias") or 0)
        komz["riskBias"] = round(max(-0.25, min(0.25, old_bias * 0.7 + lesson["riskBias"] * 0.3)), 3)
        text = (
            "REFLECTION %s\n%s %s %.2f lots pnl=%s R=%s\nlesson: %s\nmistake: %s\nstrength: %s\nnext: %s\nrisk_bias: %s"
            % (
                lesson["at"],
                trade.get("symbol"),
                trade.get("side"),
                float(trade.get("volume") or 0),
                trade.get("pnl"),
                trade.get("r"),
                lesson["lesson"],
                lesson["mistake"],
                lesson["strength"],
                lesson["nextAction"],
                lesson["riskBias"],
            )
        )
        memory = self.memory.write(account_id, "reflection", text)
        komz["memory"] = self.memory.status()
        komz["lastMemoryWrite"] = memory
        return lesson

    def note(self, account_id: str, state: Dict[str, Any], kind: str, text: str) -> None:
        komz = state.setdefault("komz", {})
        try:
            result = self.memory.write(account_id, kind, text)
            komz["memory"] = self.memory.status()
            komz["lastMemoryWrite"] = result
        except Exception as e:
            self.log("komz memory note failed: %s" % _redact(e))
