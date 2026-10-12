"""New insight cards get their other language in the background.

    insight saved (put_item) -> queue(item) -> worker thread (inside this
    server) waits ~2 s for more cards, translates the batch in ONE model call
    (api/i18n.translate), writes each card's `i18n` field.
    hourly (dispatch-pending) -> sweep(): any card still missing a version
    (server restarted with cards in the queue, a failed call, a path that
    does not queue) is translated then.

Never blocks or breaks the request that created the card: the card is saved
first, as before, and everything here is best effort. Off when the translator
has no API key, or with I18N_AUTO=off (tests, local work without keys).
"""
import os
import queue
import re
import threading
import time
from typing import Dict, List, Tuple

FIELDS = ("title", "description")
BATCH_TEXTS = 40
WAIT_SECONDS = 2.0
_LETTERS = re.compile(r"[A-Za-zÁÉÍÓÚÑáéíóúñü]{2,}")

_queue: "queue.Queue[dict]" = queue.Queue()
_worker_lock = threading.Lock()
_worker_started = False


def enabled() -> bool:
    if os.environ.get("I18N_AUTO", "on").lower() in ("off", "0", "false", "no"):
        return False
    try:
        from agent.graph.llm_factory import _parse_config, _provider_available
        from api import i18n
        provider, _m = _parse_config(i18n.TRANSLATE_LLM)
        return bool(provider) and _provider_available(provider)
    except Exception:
        return False


def _texts(item: dict) -> List[Tuple[str, str]]:
    """(field, text) still missing a version for this card."""
    from api import i18n
    out = []
    for f in FIELDS:
        text = item.get(f)
        if not isinstance(text, str) or not text.strip() or not _LETTERS.search(text):
            continue
        if not i18n.is_complete((item.get("i18n") or {}).get(f), text.strip()):
            out.append((f, text.strip()))
    return out


def _write(table, item: dict, entries: Dict[str, dict]) -> None:
    cur = dict(item.get("i18n") or {})
    cur.update(entries)
    item["i18n"] = cur
    table.update_item(Key={"userId": item["userId"], "insightId": item["insightId"]},
                      UpdateExpression="SET i18n = :m", ExpressionAttributeValues={":m": cur})


def translate_items(items: List[dict], llm=None) -> int:
    """Translate what these cards are missing (batched). Returns fields written."""
    from agent.tools import insights_table
    from api import i18n
    jobs = [(it, f, t) for it in items if it.get("userId") and it.get("insightId") for f, t in _texts(it)]
    written = 0
    for start in range(0, len(jobs), BATCH_TEXTS):
        chunk = jobs[start:start + BATCH_TEXTS]
        res = i18n.translate({str(n): t for n, (_i, _f, t) in enumerate(chunk)}, llm=llm)
        per_item: Dict[str, Tuple[dict, Dict[str, dict]]] = {}
        for n, (it, f, text) in enumerate(chunk):
            r = res.get(str(n))
            if r:
                per_item.setdefault(it["insightId"], (it, {}))[1][f] = i18n.g_entry(text, r["lang"], r["translation"])
        for it, entries in per_item.values():
            try:
                _write(insights_table, it, entries)
                written += len(entries)
            except Exception as e:
                print(f"[insight_i18n] write failed for {it.get('insightId')}: {e}")
    return written


def _worker() -> None:
    while True:
        batch = [_queue.get()]
        deadline = time.monotonic() + WAIT_SECONDS
        while len(batch) < BATCH_TEXTS:
            left = deadline - time.monotonic()
            if left <= 0:
                break
            try:
                batch.append(_queue.get(timeout=left))
            except queue.Empty:
                break
        try:
            translate_items(batch)
        except Exception as e:                     # never kill the worker
            print(f"[insight_i18n] batch of {len(batch)} failed (the hourly sweep retries): {e}")


def queue_translation(item: dict) -> None:
    """Call right after saving an insight. Best effort, never raises."""
    global _worker_started
    try:
        if not item or not _texts(item) or not enabled():
            return
        with _worker_lock:
            if not _worker_started:
                threading.Thread(target=_worker, name="insight-i18n", daemon=True).start()
                _worker_started = True
        _queue.put(dict(item))
    except Exception as e:
        print(f"[insight_i18n] could not queue {item.get('insightId') if item else ''}: {e}")


def sweep(limit: int = 300) -> dict:
    """Hourly safety net: translate cards still missing a version."""
    if not enabled():
        return {"skipped": "translator off or without API key"}
    from agent.tools import insights_table
    from api.deps import scan_all_pages
    pending = [it for it in scan_all_pages(insights_table) if _texts(it)][:limit]
    if not pending:
        return {"pending": 0}
    return {"pending": len(pending), "written": translate_items(pending)}
