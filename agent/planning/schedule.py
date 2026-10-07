"""Deterministic plan arithmetic: apply operations, propagate dates, find
incongruences, analyse the dependency graph. No LLM here, ever.

The LLM decides WHAT changed. Turning that into dates is arithmetic, and
arithmetic done by a model is arithmetic that changes every time you ask.
"""
import copy
from datetime import date, datetime, timedelta
from typing import Dict, List, Optional, Tuple

from agent.planning.sources import match_person

OPEN = ("pending", "in_progress", "blocked")


def parse_date(s: str) -> Optional[date]:
    s = (s or "").strip()[:10]
    if len(s) != 10:
        return None
    try:
        return datetime.strptime(s, "%Y-%m-%d").date()
    except ValueError:
        return None


def _op(o) -> dict:
    return o if isinstance(o, dict) else o.model_dump()


# ── apply ───────────────────────────────────────────────────────────────────

def apply_operations(tasks: List[dict], operations: List) -> Tuple[List[dict], List[dict], List[str]]:
    """Apply operations to a COPY of the tasks.

    Returns (draft_tasks, changes, errors). `changes` is one entry per applied
    operation with the task before and after, which is what the log shows.
    """
    draft: Dict[str, dict] = {t["taskId"]: copy.deepcopy(t) for t in tasks}
    order = [t["taskId"] for t in tasks]
    changes, errors = [], []
    new_n = 0
    for i, raw in enumerate(operations, 1):
        o = _op(raw)
        kind, tid = o.get("op"), (o.get("task_id") or "").strip()
        if kind == "create":
            new_n += 1
            tid = tid if tid and tid not in draft else f"new-{new_n}"
            draft[tid] = {
                "taskId": tid, "text": o.get("text", ""), "status": o.get("status") or "pending",
                "assignedTo": o.get("assigned_to", ""), "startDate": o.get("start_date", ""),
                "dueDate": o.get("due_date", ""), "dateStatus": o.get("date_status") or (
                    "estimated" if (o.get("due_date") or o.get("start_date")) else ""),
                "dependsOn": list(o.get("depends_on") or []), "blockedReason": o.get("blocked_reason", ""),
                "parentTaskId": "", "createdBy": "planning",
            }
            order.append(tid)
            changes.append({"index": i, "op": "create", "taskId": tid, "before": None, "after": copy.deepcopy(draft[tid])})
            continue
        if tid not in draft:
            errors.append(f"op {i} ({kind}): task_id '{tid}' does not exist")
            continue
        before = copy.deepcopy(draft[tid])
        t = draft[tid]
        if kind == "delete":
            del draft[tid]
            changes.append({"index": i, "op": "delete", "taskId": tid, "before": before, "after": None})
            continue
        if kind == "close":
            t["status"] = "done"
        elif kind == "reopen":
            t["status"] = o.get("status") or "pending"
        elif kind == "block":
            t["status"] = "blocked"
            t["blockedReason"] = o.get("blocked_reason") or o.get("reason", "")
        elif kind == "link":
            t["dependsOn"] = sorted(set(t.get("dependsOn") or []) | set(o.get("depends_on") or []))
        if kind == "update":
            for src, dst in (("text", "text"), ("status", "status"), ("assigned_to", "assignedTo"),
                             ("start_date", "startDate"), ("due_date", "dueDate"),
                             ("blocked_reason", "blockedReason")):
                if o.get(src):
                    t[dst] = o[src]
            if o.get("depends_on"):
                t["dependsOn"] = list(o["depends_on"])
        if o.get("date_status"):
            t["dateStatus"] = o["date_status"]
        changes.append({"index": i, "op": kind, "taskId": tid, "before": before, "after": copy.deepcopy(t)})
    return [draft[k] for k in order if k in draft], changes, errors


# ── dependencies ────────────────────────────────────────────────────────────

def find_cycle(tasks: List[dict]) -> List[str]:
    ids = {t["taskId"] for t in tasks}
    deps = {t["taskId"]: [d for d in (t.get("dependsOn") or []) if d in ids] for t in tasks}
    state: Dict[str, int] = {}
    stack: List[str] = []

    def visit(n) -> List[str]:
        state[n] = 1
        stack.append(n)
        for d in deps[n]:
            if state.get(d) == 1:
                return stack[stack.index(d):] + [d]
            if not state.get(d):
                c = visit(d)
                if c:
                    return c
        state[n] = 2
        stack.pop()
        return []

    for n in deps:
        if not state.get(n):
            c = visit(n)
            if c:
                return c
    return []


def propagate(tasks: List[dict]) -> List[dict]:
    """Push dates forward along dependencies. Mutates `tasks`; returns shifts.

    A task that starts before one of its open dependencies is due moves to the
    day after, keeping its duration. Tasks without dates are left alone: there
    is nothing to move, and inventing dates is exactly what this replaces.
    """
    if find_cycle(tasks):
        return []
    by_id = {t["taskId"]: t for t in tasks}
    done: set = set()
    shifts: List[dict] = []

    def settle(tid):
        if tid in done:
            return
        t = by_id[tid]
        for d in t.get("dependsOn") or []:
            if d in by_id:
                settle(d)
        start, due = parse_date(t.get("startDate")), parse_date(t.get("dueDate"))
        latest = None
        for d in t.get("dependsOn") or []:
            dep = by_id.get(d)
            if dep and dep.get("status") != "done":
                dd = parse_date(dep.get("dueDate"))
                if dd and (latest is None or dd > latest):
                    latest = dd
        if latest and start and start <= latest and t.get("status") != "done":
            new_start = latest + timedelta(days=1)
            delta = new_start - start
            shifts.append({"taskId": tid, "from": [t.get("startDate"), t.get("dueDate")],
                           "to": [new_start.isoformat(), (due + delta).isoformat() if due else ""],
                           "because": [d for d in t.get("dependsOn") or [] if d in by_id]})
            t["startDate"] = new_start.isoformat()
            if due:
                t["dueDate"] = (due + delta).isoformat()
        done.add(tid)

    for tid in list(by_id):
        settle(tid)
    return shifts


# ── incongruences ───────────────────────────────────────────────────────────

def incongruences(tasks: List[dict], project: dict, participants: List[dict],
                  today: date, aliases: Optional[dict] = None) -> List[dict]:
    out = []
    by_id = {t["taskId"]: t for t in tasks}
    delivery_raw = (project.get("deliveryDate") or "").strip()
    delivery = parse_date(delivery_raw)
    if delivery_raw and not delivery:
        out.append({"kind": "invalid_delivery_date", "taskId": "", "detail": delivery_raw})
    cyc = find_cycle(tasks)
    if cyc:
        out.append({"kind": "dependency_cycle", "taskId": cyc[0], "detail": " -> ".join(cyc)})
    for t in tasks:
        st = t.get("status", "")
        if st not in OPEN:
            continue
        tid, start, due = t["taskId"], parse_date(t.get("startDate")), parse_date(t.get("dueDate"))
        est = t.get("dateStatus") == "estimated"
        if due and due < today:
            out.append({"kind": "overdue", "taskId": tid, "detail": t.get("dueDate"), "estimated": est})
        elif start and start < today and st == "pending":
            out.append({"kind": "not_started", "taskId": tid, "detail": t.get("startDate"), "estimated": est})
        if st == "blocked" and not (t.get("blockedReason") or "").strip():
            out.append({"kind": "blocked_without_reason", "taskId": tid, "detail": ""})
        parent = by_id.get(t.get("parentTaskId") or "")
        if parent and due and parse_date(parent.get("dueDate")) and due > parse_date(parent.get("dueDate")):
            out.append({"kind": "child_after_parent", "taskId": tid, "detail": parent["taskId"]})
        if delivery and due and due > delivery:
            out.append({"kind": "after_delivery", "taskId": tid, "detail": project.get("deliveryDate")})
        who = (t.get("assignedTo") or "").strip()
        if not who:
            out.append({"kind": "unassigned", "taskId": tid, "detail": ""})
        elif not match_person(who, participants, aliases):
            out.append({"kind": "assignee_not_participant", "taskId": tid, "detail": who})
    return out


# ── analysis ────────────────────────────────────────────────────────────────

def analyze_plan(tasks: List[dict], project: dict, participants: List[dict],
                 today: date, aliases: Optional[dict] = None) -> dict:
    by_id = {t["taskId"]: t for t in tasks}
    open_ids = [t["taskId"] for t in tasks if t.get("status") in OPEN]
    counts: Dict[str, int] = {}
    for t in tasks:
        counts[t.get("status") or "unknown"] = counts.get(t.get("status") or "unknown", 0) + 1

    # Longest chain of open work (by number of tasks): what has to happen, in
    # order, before the last thing can be done.
    memo: Dict[str, List[str]] = {}

    def chain(tid, seen=()):
        if tid in memo:
            return memo[tid]
        best: List[str] = []
        for d in by_id[tid].get("dependsOn") or []:
            if d in by_id and d not in seen and by_id[d].get("status") in OPEN:
                c = chain(d, seen + (tid,))
                if len(c) > len(best):
                    best = c
        memo[tid] = best + [tid]
        return memo[tid]

    critical: List[str] = []
    if not find_cycle(tasks):
        for tid in open_ids:
            c = chain(tid)
            if len(c) > len(critical):
                critical = c

    # Everything waiting, directly or not, on a blocked task.
    blocked = {t["taskId"] for t in tasks if t.get("status") == "blocked"}
    waiting: Dict[str, List[str]] = {}
    for tid in open_ids:
        stack, seen, roots = list(by_id[tid].get("dependsOn") or []), set(), []
        while stack:
            d = stack.pop()
            if d in seen or d not in by_id:
                continue
            seen.add(d)
            if d in blocked:
                roots.append(d)
            stack.extend(by_id[d].get("dependsOn") or [])
        if roots:
            waiting[tid] = roots

    load: Dict[str, dict] = {}
    for t in tasks:
        if t.get("status") not in OPEN:
            continue
        who = (t.get("assignedTo") or "").strip() or "(sin asignar)"
        p = match_person(who, participants, aliases)
        key = p["name"] if p else who
        e = load.setdefault(key, {"open": 0, "overdue": 0, "registered": bool(p)})
        e["open"] += 1
        due = parse_date(t.get("dueDate"))
        if due and due < today:
            e["overdue"] += 1

    last_due = max((parse_date(by_id[t].get("dueDate")) for t in open_ids
                    if parse_date(by_id[t].get("dueDate"))), default=None)
    delivery = parse_date(project.get("deliveryDate"))
    return {
        "today": today.isoformat(),
        "counts": counts,
        "open": len(open_ids),
        "critical_chain": [{"taskId": t, "text": by_id[t].get("text", "")} for t in critical],
        "waiting_on_blocked": [{"taskId": k, "blocked_by": v} for k, v in waiting.items()],
        "load": load,
        "last_open_due": last_due.isoformat() if last_due else "",
        "delivery": project.get("deliveryDate", ""),
        "delivery_at_risk": bool(delivery and last_due and last_due > delivery),
        "estimated_dates_open": sum(1 for t in open_ids if by_id[t].get("dateStatus") == "estimated"),
        "incongruences": incongruences(tasks, project, participants, today, aliases),
    }


# ── approval ────────────────────────────────────────────────────────────────

def apply_proposal(export: dict, proposal: dict, approved: Optional[List[int]] = None,
                   actor: str = "", now: Optional[str] = None) -> Tuple[dict, List[dict]]:
    """Return (new_export, events) after applying the approved operations.

    `approved` holds 1-based indexes into proposal['operations']; None = all.
    Works on the export shape so the same function produces the 'after.json'
    of a test case and, later, the writes against DynamoDB.
    """
    ops = proposal.get("operations") or []
    chosen = [o for i, o in enumerate(ops, 1) if approved is None or i in approved]
    tasks, changes, errors = apply_operations(export.get("tasks") or [], chosen)
    shifts = propagate(tasks)
    stamp = now or datetime.utcnow().isoformat()
    for t in tasks:
        if any(c["taskId"] == t["taskId"] and c["after"] for c in changes) or \
           any(s["taskId"] == t["taskId"] for s in shifts):
            t["updatedAt"] = stamp
    new = copy.deepcopy(export)
    new["tasks"] = tasks
    proj = new.setdefault("project", {})
    proj["planVersion"] = int(proj.get("planVersion") or 0) + 1
    events = [{"at": stamp, "actor": actor, "proposalId": proposal.get("proposalId", ""),
               "op": c["op"], "taskId": c["taskId"], "before": c["before"], "after": c["after"],
               "reason": _op(chosen[c["index"] - 1]).get("reason", ""),
               "evidence": _op(chosen[c["index"] - 1]).get("evidence", [])} for c in changes]
    events += [{"at": stamp, "actor": "schedule", "proposalId": proposal.get("proposalId", ""),
                "op": "shift", "taskId": s["taskId"], "before": s["from"], "after": s["to"],
                "reason": "dependencia", "evidence": []} for s in shifts]
    new["events"] = list(export.get("events") or []) + events
    # Notes go to the insights feed as type 'note' (observations, not actions).
    created = {}
    for c in changes:
        if c["op"] == "create":
            created[(_op(chosen[c["index"] - 1]).get("task_id") or "").strip()] = c["taskId"]
    alive = {t["taskId"] for t in tasks}
    proj_id, proj_name = proj.get("projectId", ""), proj.get("name", "")
    notes = []
    for i, n in enumerate(proposal.get("notes") or [], 1):
        tid = (n.get("task_id") or "").strip()
        tid = created.get(tid, tid)
        notes.append({
            "insightId": f"{stamp}#note{i:02d}{(proposal.get('proposalId') or '')[:6]}",
            "projectId": proj_id, "projectName": proj_name, "userId": proj.get("userId", ""),
            "type": "note", "noteKind": n.get("kind") or "info", "title": n.get("text", ""),
            "description": "", "relatedPerson": n.get("who", ""), "eventDate": n.get("date", ""),
            "taskId": tid if tid in alive else "", "evidence": n.get("evidence") or [],
            "source": "planning", "proposalId": proposal.get("proposalId", ""),
            "status": "created", "createdAt": stamp,
        })
    if notes:
        new["insights"] = list(export.get("insights") or []) + notes
        events.append({"at": stamp, "actor": actor, "proposalId": proposal.get("proposalId", ""),
                       "op": "notes", "taskId": "", "before": None, "after": len(notes),
                       "reason": "notas registradas", "evidence": []})
        new["events"].append(events[-1])
    if errors:
        new["applyErrors"] = errors
    return new, events
