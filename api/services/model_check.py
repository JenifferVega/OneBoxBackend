"""Settings > System details > "Test models": one tiny real call per model the
app uses, to confirm that model and its API key answer. No terminal needed.

    POST /api/system/check-models  -> {"checkedAt", "results": [{node, provider,
                                       model, ok, ms, detail, error}]}

Per part:
  agent nodes + replanning   "reply with the single word OK"   (text)
  translator                 a real translation es -> en       (api/i18n)
  documents                  "reply OK" through agent/llm.call_llm
Fallbacks are OFF: a dead provider must show as dead, not be hidden by another
one answering. All parts run in parallel; each has its own time limit.
Same visibility as the System details (SYSTEM_INFO_EMAILS). Costs a few
hundred tokens per run; one run at a time, at most every 15 seconds.
Error texts are cut short and anything that looks like a key is masked.
"""
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Callable, List, Tuple

from fastapi import HTTPException

TIMEOUT_S = 60
MIN_GAP_S = 15
_lock = threading.Lock()
_last_run = 0.0
_SECRETISH = re.compile(r"(key=|api[_-]?key[\"':= ]+|Bearer\s+)[A-Za-z0-9._\-]{8,}|\b(sk-|AIza)[A-Za-z0-9._\-]{10,}", re.I)


def _clean(msg: str) -> str:
    return _SECRETISH.sub(lambda m: (m.group(1) or "") + "***", msg or "")[:300]


def _ok_text(llm) -> Tuple[bool, str]:
    from langchain_core.messages import HumanMessage, SystemMessage
    out = llm.invoke([SystemMessage(content="You are a health check. Follow the instruction exactly."),
                      HumanMessage(content="Reply with the single word OK and nothing else.")])
    text = str(getattr(out, "content", out)).strip()
    return "ok" in text.lower()[:20], text[:40]


def _translate(llm) -> Tuple[bool, str]:
    from api import i18n
    res = i18n.translate({"1": "La tarea quedó bloqueada por falta de presupuesto."}, llm=llm).get("1") or {}
    text = res.get("translation") or ""
    if not text:
        return False, "no translation came back"
    return res.get("lang") == "es" and "budget" in text.lower(), text[:60]


def _targets() -> List[Tuple[str, Callable, Callable]]:
    """(part, make_llm, check). make_llm returns the llm (or None for documents)."""
    from agent.graph import llm_factory as F
    from api import i18n
    out = []
    for node in F.NODE_NAMES:
        out.append((node, lambda n=node: F.create_llm(n, with_fallbacks=False), _ok_text))

    def _planning():
        env = os.environ.get("PLANNING_LLM", "").strip()
        if env:
            return F.create_llm("planner", env_value=env, temperature=0.1, max_tokens=512, with_fallbacks=False)
        return F.create_llm("planner", with_fallbacks=False)
    out.append(("planning", _planning, _ok_text))
    out.append(("translator", lambda: F.create_llm(node="translator", env_value=i18n.TRANSLATE_LLM, temperature=0.0,
                                                   max_tokens=2048, with_fallbacks=False, timeout=TIMEOUT_S),
                _translate))

    def _documents_check(_llm) -> Tuple[bool, str]:
        from agent.llm import call_llm
        text = (call_llm("You are a health check. Follow the instruction exactly.",
                         "Reply with the single word OK and nothing else.", temperature=0.0, max_tokens=50) or "").strip()
        return "ok" in text.lower()[:20], text[:40]
    out.append(("documents", lambda: None, _documents_check))
    return out


def _names(part: str, llm) -> Tuple[str, str]:
    if part == "documents":
        from agent import llm as L
        p = getattr(L, "LLM_PROVIDER", "?")
        return p, {"gemini": getattr(L, "GEMINI_MODEL", ""), "anthropic": getattr(L, "ANTHROPIC_MODEL", ""),
                   "bedrock": getattr(L, "MODEL_ID", "")}.get(p, "?")
    names = getattr(llm, "_names", None)
    return tuple(names[0]) if names else ("?", "?")


def _one(part: str, make: Callable, check: Callable) -> dict:
    t0 = time.perf_counter()
    row = {"node": part, "provider": "", "model": "", "ok": False, "ms": 0, "detail": "", "error": ""}
    try:
        llm = make()
        row["provider"], row["model"] = _names(part, llm)
        ok, detail = check(llm)
        row["ok"], row["detail"] = bool(ok), _clean(detail)
        if not ok:
            row["error"] = "unexpected answer"
    except Exception as e:
        row["error"] = _clean(f"{type(e).__name__}: {e}")
    row["ms"] = int((time.perf_counter() - t0) * 1000)
    return row


def run(email: str) -> dict:
    from api.services.system_info import can_see
    global _last_run
    if not can_see(email):
        raise HTTPException(status_code=403, detail="Not allowed")
    if not _lock.acquire(blocking=False):
        raise HTTPException(status_code=429, detail="A test is already running")
    try:
        wait = MIN_GAP_S - (time.monotonic() - _last_run)
        if wait > 0:
            raise HTTPException(status_code=429, detail=f"Wait {int(wait) + 1} s before testing again")
        _last_run = time.monotonic()
        targets = _targets()
        pool = ThreadPoolExecutor(max_workers=len(targets))
        futures = [(part, pool.submit(_one, part, make, check)) for part, make, check in targets]
        results = []
        deadline = time.monotonic() + TIMEOUT_S
        for part, fut in futures:
            try:
                results.append(fut.result(timeout=max(1.0, deadline - time.monotonic())))
            except Exception:
                results.append({"node": part, "provider": "", "model": "", "ok": False, "ms": TIMEOUT_S * 1000,
                                "detail": "", "error": f"no answer in {TIMEOUT_S} s"})
        pool.shutdown(wait=False)
        return {"checkedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"), "results": results}
    finally:
        _lock.release()
