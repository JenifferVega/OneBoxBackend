"""Rules every proposal must pass before a human sees it. No LLM.

Hard errors go back to `reconcile` as feedback (max 2 retries). Soft issues
are fixed in place (a deletion of human work becomes a question; an
unsupported 'committed' date becomes 'estimated') and listed as warnings.
"""
from typing import List

from agent.planning.schedule import apply_operations, find_cycle
from agent.planning.sources import match_person, norm

NEEDS_EVIDENCE = ("close", "delete", "create", "block", "reopen")
MACHINE_AUTHORS = ("IA", "OneBox IA", "planning", "")


def _d(o):
    return o if isinstance(o, dict) else o.model_dump()


def validate(tasks: List[dict], operations: List, participants: List[dict],
             known_people: List[str]) -> dict:
    by_id = {t["taskId"]: t for t in tasks}
    # A replaced task is a "duplicate" of the task that supersedes it, which
    # is often created by this same proposal ('new-3').
    created_ids = {(_d(o).get("task_id") or "").strip() for o in operations if _d(o).get("op") == "create"}
    ops, questions, errors, warnings = [], [], [], []
    touched = {}
    known = {norm(p) for p in known_people or []}

    for i, raw in enumerate(operations, 1):
        o = dict(_d(raw))
        if o.get("date_status") not in ("estimated", "committed"):
            o["date_status"] = ""
        kind, tid = o.get("op"), (o.get("task_id") or "").strip()
        label = f"op {i} ({kind} {tid or o.get('text', '')[:40]})"

        if kind != "create" and tid not in by_id:
            errors.append(f"{label}: task_id does not exist. Use only taskIds from the task list.")
            continue
        if kind == "create" and not (o.get("text") or "").strip():
            errors.append(f"{label}: create without text.")
            continue
        if kind in NEEDS_EVIDENCE and not o.get("evidence"):
            errors.append(f"{label}: needs at least one evidence item (source + locator + quote).")
            continue
        if kind != "create":
            prev = touched.get(tid)
            if prev and {prev, kind} & {"delete"} and prev != kind:
                errors.append(f"{label}: conflicts with an earlier '{prev}' on the same task.")
                continue
            touched[tid] = kind

        if kind == "delete":
            t = by_id[tid]
            dup = (o.get("duplicate_of") or "").strip()
            if dup and dup not in by_id and dup not in created_ids:
                errors.append(f"{label}: duplicate_of '{dup}' does not exist.")
                continue
            if t.get("createdBy") not in MACHINE_AUTHORS:
                questions.append({"id": f"v{i}", "text": f"¿Borro la tarea '{t.get('text','')[:80]}'? La creó una persona. Motivo propuesto: {o.get('reason','')}",
                                  "about_task_ids": [tid], "options": ["Sí, borrar", "No, conservar"]})
                warnings.append(f"{label}: deleting human-created work turned into a question.")
                continue

        if (o.get("due_date") or o.get("start_date")) and o.get("date_status") == "committed":
            quotes = " ".join(e.get("quote", "") + " " + e.get("locator", "") for e in o.get("evidence") or [])
            if not any(ch.isdigit() for ch in quotes):
                o["date_status"] = "estimated"
                warnings.append(f"{label}: 'committed' date without a dated quote -> estimated.")
        if (o.get("due_date") or o.get("start_date")) and not o.get("date_status"):
            o["date_status"] = "estimated"

        who = (o.get("assigned_to") or "").strip()
        if who and not match_person(who, participants) and norm(who) not in known:
            warnings.append(f"{label}: '{who}' is not a project participant nor named in the sources.")
        ops.append(o)

    draft, _changes, apply_errors = apply_operations(tasks, ops)
    errors += apply_errors
    cyc = find_cycle(draft)
    if cyc:
        errors.append("dependency cycle: " + " -> ".join(cyc) + ". Remove one of these links.")
    return {"operations": ops, "questions": questions, "errors": errors, "warnings": warnings}


def validate_notes(tasks: List[dict], operations: List[dict], notes: List[dict]):
    """Notes are context, never work. Soft rules only: a note without text or
    evidence is dropped, a link to an unknown task is cleared, credentials are
    redacted, repeated notes are merged. Returns (notes, warnings)."""
    from agent.planning.sources import redact
    ids = {t["taskId"] for t in tasks} | {(o.get("task_id") or "") for o in operations if o.get("op") == "create"}
    deleted = {o.get("task_id") for o in operations if o.get("op") == "delete"}
    out, warnings, seen = [], [], set()
    for i, n in enumerate(notes, 1):
        n = dict(_d(n))
        text = redact((n.get("text") or "").strip())[0]
        if not text or not n.get("evidence"):
            warnings.append(f"note {i}: dropped (no text or no evidence).")
            continue
        key = norm(text)[:80]
        if key in seen:
            continue
        seen.add(key)
        tid = (n.get("task_id") or "").strip()
        if tid and (tid not in ids or tid in deleted):
            warnings.append(f"note {i}: task '{tid}' unknown or deleted; note kept without task.")
            tid = ""
        n.update(text=text, task_id=tid)
        out.append(n)
    return out, warnings
