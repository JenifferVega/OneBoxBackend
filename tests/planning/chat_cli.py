#!/usr/bin/env python3
"""One turn of the PLANNING chat against a test case, without a server or DB.

    python tests/planning/chat_cli.py <case> <session> "<message>"
    python tests/planning/chat_cli.py <case> <session> --reset

State lives in tests/planning/sessions/<case>__<session>/:
    state.json     the project export as it is now (starts as the case's before.json)
    proposal.json  the open proposal, if any
    history.json   the conversation
    events.json    every change applied, with evidence (what the Log tab shows)
Prints one JSON object (used by the MCP tool planning_chat).
"""
import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env")
from agent.planning.runner import run_planning  # noqa: E402

CASES = ROOT / "tests" / "planning" / "cases"
SESSIONS = ROOT / "tests" / "planning" / "sessions"


def _read(p, default):
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else default


def _write(p, data):
    p.write_text(json.dumps(data, ensure_ascii=False, indent=2, default=str), encoding="utf-8")


def chat_turn(case, session, message) -> dict:
    """One planning-chat turn. Returns the dict that main() prints."""
    sdir = SESSIONS / f"{case}__{session}"
    if message == "--reset":
        shutil.rmtree(sdir, ignore_errors=True)
        return {"reset": True, "session": sdir.name}
    sdir.mkdir(parents=True, exist_ok=True)
    if not (sdir / "state.json").exists():
        from tests.planning.fixtures import load_case_export
        _write(sdir / "state.json", load_case_export(CASES / case))
    export = _read(sdir / "state.json", {})
    history = _read(sdir / "history.json", [])
    proposal = _read(sdir / "proposal.json", None)
    today = _read(CASES / case / "input.json", {}).get("today", "")

    res = run_planning(export, "chat", today=today, message=message, history=history, open_proposal=proposal)

    reply = res.get("narration") or res.get("answer") or ""
    new_prop = res.get("proposal")
    if res.get("result_export"):
        _write(sdir / "state.json", res["result_export"])
        events = _read(sdir / "events.json", [])
        added = [e for e in res["result_export"].get("events", []) if e not in events]
        _write(sdir / "events.json", events + added)
        (sdir / "proposal.json").unlink(missing_ok=True)
    elif new_prop and new_prop.get("status") == "pending":
        _write(sdir / "proposal.json", new_prop)
    elif new_prop and new_prop.get("status") in ("rejected", "stale"):
        (sdir / "proposal.json").unlink(missing_ok=True)
    history += [{"role": "user", "content": message}, {"role": "assistant", "content": reply}]
    _write(sdir / "history.json", history)

    by_id = {t["taskId"]: t for t in export.get("tasks") or []}
    shown = new_prop if (new_prop and new_prop.get("status") == "pending") else None
    return {
        "reply": reply,
        "intent": (res.get("intent") or {}).get("kind"),
        "proposal": None if not shown else {
            "counts": shown.get("counts"),
            "operations": [f"{i}. {o['op']} {o.get('task_id','')[:10]} "
                           f"{(o.get('text') or by_id.get(o.get('task_id'), {}).get('text',''))[:70]} — {o.get('reason','')[:90]}"
                           for i, o in enumerate(shown.get("operations") or [], 1)],
            "questions": [q.get("text", "") for q in shown.get("questions") or []],
            "warnings": shown.get("warnings") or []},
        "applied": bool(res.get("result_export")),
        "open_tasks": sum(1 for t in (res.get("result_export") or export).get("tasks", [])
                          if t.get("status") in ("pending", "in_progress", "blocked")),
        "nodes": [t["node"] for t in res.get("trace") or []],
        "session_dir": str(sdir),
    }


def main(case, session, message):
    print(json.dumps(chat_turn(case, session, message), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    if len(sys.argv) < 4:
        print(__doc__)
        sys.exit(2)
    main(sys.argv[1], sys.argv[2], " ".join(sys.argv[3:]))
