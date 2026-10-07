"""Check-in of pending work: every person reports progress on the tasks
assigned to them, and the project owner hears about whoever goes quiet.

    On login the app asks GET /api/checkin/pending. Projects where the person
    has open tasks and a report is due come back, and a BLOCKING modal asks,
    task by task: done / in progress / not started / blocked (why), new date,
    comment.
    POST /api/checkin turns the answers into a PROPOSAL for the leader
    (Intelligence > Replanificación). Nothing is applied by the report
    itself: the leader approves it. It is built by code, not by the LLM:
    the answers are already structured.
    Every hour (POST /api/scheduled/dispatch-pending, EventBridge) the owner
    is notified about each person whose report is due for more than
    CHECKIN_GRACE_DAYS.

A report is due when the person has not reported for CHECKIN_DAYS, or at once
when one of their tasks is overdue or due within URGENT_DAYS (one report per
day is enough then). People without an account cannot log in: their reports
come due anyway and the owner is told, so the owner updates their status.

State lives in onebox-planning, item "checkin#<person>" per project:
    {lastReportAt, firstSeenAt, ownerNotifiedAt}
and each report as "checkinlog#<iso>#<person>".
"""
import os
import uuid
from datetime import date, datetime, timedelta
from typing import List, Optional

from fastapi import HTTPException

from agent.planning.sources import match_person, normalize_participant, norm
from api.services import planning_chat

CHECKIN_DAYS = int(os.environ.get("CHECKIN_DAYS", "3"))
CHECKIN_GRACE_DAYS = int(os.environ.get("CHECKIN_GRACE_DAYS", "1"))
URGENT_DAYS = 2
OPEN = ("pending", "in_progress", "blocked")
STATUSES = ("done", "in_progress", "pending", "blocked")


def _now() -> datetime:
    return datetime.utcnow()


def _iso(d: Optional[datetime]) -> str:
    return d.isoformat() if d else ""


def _dt(s: str) -> Optional[datetime]:
    try:
        return datetime.fromisoformat((s or "")[:26]) if s else None
    except ValueError:
        return None


def _day(s: str) -> Optional[date]:
    try:
        return date.fromisoformat((s or "")[:10]) if s else None
    except ValueError:
        return None


# ── who has what ────────────────────────────────────────────────────────────

def person_key(p: dict) -> str:
    return (p.get("email") or norm(p.get("name", ""))).lower()


def assignments(project: dict, tasks: List[dict]) -> List[tuple]:
    """[(participant, [open tasks assigned to them])] for every participant."""
    parts = [normalize_participant(p) for p in project.get("participants") or []]
    parts = [p for p in parts if p.get("name") or p.get("email")]
    out = {person_key(p): (p, []) for p in parts}
    for t in tasks:
        if t.get("status") not in OPEN or not (t.get("assignedTo") or "").strip():
            continue
        who = (t.get("assignedTo") or "").strip()
        p = next((q for q in parts if q.get("email") and q["email"] == who.lower()), None) or match_person(who, parts)
        if p:
            out[person_key(p)][1].append(t)
    return [v for v in out.values() if v[1]]


def due_state(tasks: List[dict], state: dict, now: datetime) -> dict:
    """{due, due_at, urgent} for one person in one project."""
    last = _dt(state.get("lastReportAt"))
    seen = _dt(state.get("firstSeenAt")) or now
    due_at = (last + timedelta(days=CHECKIN_DAYS)) if last else seen
    today = now.date()
    urgent = any((_day(t.get("dueDate")) or date.max) <= today + timedelta(days=URGENT_DAYS) for t in tasks)
    if urgent and (not last or last.date() < today):
        due_at = min(due_at, datetime.combine(today, datetime.min.time()))
    return {"due": now >= due_at, "due_at": due_at, "urgent": urgent}


def _state(project_id: str, key: str) -> dict:
    return planning_chat.table().get_item(Key={"projectId": project_id, "itemId": f"checkin#{key}"}).get("Item") or {}


def _save_state(project_id: str, key: str, **fields) -> None:
    item = {**_state(project_id, key), **fields, "projectId": project_id, "itemId": f"checkin#{key}"}
    planning_chat.table().put_item(Item=item)


def _project_tasks(project_id: str) -> list:
    from boto3.dynamodb.conditions import Key
    from agent.tools import tasks_table
    from api.services.project_export import _query_all
    return _query_all(tasks_table, KeyConditionExpression=Key("projectId").eq(project_id))


def _task_view(t: dict) -> dict:
    return {k: t.get(k, "") for k in ("taskId", "text", "status", "startDate", "dueDate", "blockedReason")}


# ── the person ──────────────────────────────────────────────────────────────

def pending_for_user(uid: str, email: str, name: str = "") -> dict:
    """Projects where THIS user owes a report now."""
    from agent.tools import projects_table
    from api.services.access import accessible_project_ids
    email = (email or "").lower()
    now = _now()
    out = []
    for pid in sorted(accessible_project_ids(uid, email)):
        proj = projects_table.get_item(Key={"projectId": pid}).get("Item")
        if not proj or proj.get("status") in ("archived", "completed", "closed"):
            continue
        for person, tasks in assignments(proj, _project_tasks(pid)):
            if not ((email and person.get("email") == email) or
                    (not person.get("email") and name and norm(person.get("name", "")) == norm(name))):
                continue
            key = person_key(person)
            st = _state(pid, key)
            if not st.get("firstSeenAt"):
                _save_state(pid, key, firstSeenAt=_iso(now))
                st["firstSeenAt"] = _iso(now)
            d = due_state(tasks, st, now)
            if d["due"]:
                out.append({"projectId": pid, "projectName": proj.get("name", ""), "person": person.get("name", ""),
                            # the owner may postpone their own update; members may not
                            "isOwner": bool(uid) and proj.get("userId") == uid,
                            "urgent": d["urgent"], "lastReportAt": st.get("lastReportAt", ""),
                            "tasks": [_task_view(t) for t in sorted(tasks, key=lambda t: t.get("dueDate") or "9999")]})
    out.sort(key=lambda x: x["isOwner"])    # the mandatory ones (as member) first, own projects last
    return {"projects": out, "checkinDays": CHECKIN_DAYS}


def build_proposal(project: dict, tasks_by_id: dict, person: str, answers: list, comment: str, now: datetime) -> dict:
    """Answers -> operations/notes, the same shape the planning graph proposes."""
    stamp = now.date().isoformat()
    ev = lambda quote: [{"source": f"check-in {person}", "locator": stamp, "quote": (quote or "")[:120]}]  # noqa: E731
    ops, notes, lines = [], [], []
    for a in answers:
        t = tasks_by_id[a["taskId"]]
        st, why, new_due, note = a["status"], (a.get("blockedReason") or "").strip(), \
            (a.get("newDueDate") or "").strip(), (a.get("comment") or "").strip()
        label = (t.get("text") or "")[:80]
        reason = f"Reporte de {person}"
        if st == "done" and t.get("status") != "done":
            ops.append({"op": "close", "task_id": t["taskId"], "reason": f"{reason}: hecha",
                        "evidence": ev(note or "hecha"), "confidence": "high"})
        elif st == "blocked":
            ops.append({"op": "block", "task_id": t["taskId"], "blocked_reason": why or "sin motivo indicado",
                        "reason": f"{reason}: bloqueada", "evidence": ev(why or note or "bloqueada"),
                        "confidence": "high"})
        elif st in ("in_progress", "pending") and st != t.get("status"):
            ops.append({"op": "update", "task_id": t["taskId"], "status": st, "reason": f"{reason}: {st}",
                        "evidence": ev(note or st), "confidence": "high"})
        if new_due and new_due != (t.get("dueDate") or "") and st != "done":
            ops.append({"op": "update", "task_id": t["taskId"], "due_date": new_due, "date_status": "committed",
                        "reason": f"{reason}: nueva fecha {new_due}", "evidence": ev(f"nueva fecha {new_due}"),
                        "confidence": "high"})
        if note:
            notes.append({"kind": "info", "text": f"{person}: {note}"[:300], "who": person, "date": stamp,
                          "task_id": t["taskId"], "evidence": ev(note)})
        lines.append(f"- {label}: {st}" + (f" ({why})" if why else "") + (f", nueva fecha {new_due}" if new_due else "")
                     + (f" — {note}" if note else ""))
    if comment.strip():
        notes.append({"kind": "info", "text": f"{person}: {comment.strip()}"[:300], "who": person, "date": stamp,
                      "task_id": "", "evidence": ev(comment)})
    counts = {}
    for o in ops:
        counts[o["op"]] = counts.get(o["op"], 0) + 1
    return {"proposalId": uuid.uuid4().hex[:12], "projectId": project["projectId"],
            "basePlanVersion": int(project.get("planVersion") or 0), "trigger": "checkin", "createdAt": _iso(now),
            "summary": f"Reporte de avances de {person} ({stamp})", "counts": counts, "operations": ops,
            "questions": [], "notes": notes, "shifts": [], "warnings": [], "unresolved_errors": [],
            "_report": "\n".join(lines) + (f"\nComentario: {comment.strip()}" if comment.strip() else "")}


def submit(uid: str, email: str, project_id: str, answers: list, comment: str = "", name: str = "") -> dict:
    from agent.tools import projects_table
    from api.services.access import has_project_access
    from api.services.planning_live import _notify
    email = (email or "").lower()
    has, _owner, proj = has_project_access(uid, email, project_id)
    if not proj:
        raise HTTPException(status_code=404, detail="Project not found")
    if not has:
        raise HTTPException(status_code=403, detail="No access to this project")
    proj = projects_table.get_item(Key={"projectId": project_id}).get("Item") or proj
    mine = next(((p, ts) for p, ts in assignments(proj, _project_tasks(project_id))
                 if (email and p.get("email") == email) or (name and norm(p.get("name", "")) == norm(name))), None)
    if not mine:
        raise HTTPException(status_code=400, detail="You have no open tasks assigned in this project")
    person, tasks = mine
    by_id = {t["taskId"]: t for t in tasks}
    answers = [a for a in answers or [] if a.get("taskId") in by_id]
    missing = set(by_id) - {a["taskId"] for a in answers}
    if missing:
        raise HTTPException(status_code=400, detail=f"Every task needs an answer ({len(missing)} missing)")
    for a in answers:
        if a.get("status") not in STATUSES:
            raise HTTPException(status_code=400, detail="status must be done, in_progress, pending or blocked")
        if a["status"] == "blocked" and not (a.get("blockedReason") or "").strip():
            raise HTTPException(status_code=400, detail="A blocked task needs a reason")
    now = _now()
    who = person.get("name") or email
    proposal = build_proposal(proj, by_id, who, answers, comment, now)
    report = proposal.pop("_report")
    key = person_key(person)
    planning_chat.table().put_item(Item={"projectId": project_id, "itemId": f"checkinlog#{_iso(now)}#{key}",
                                         "person": who, "email": email, "answers": planning_chat._loads(
                                             planning_chat._dumps(answers)), "comment": comment, "createdAt": _iso(now)})
    _save_state(project_id, key, lastReportAt=_iso(now), lastReportBy=email or uid)
    item = None
    if proposal["operations"] or proposal["notes"]:
        item = planning_chat.merge_or_save_proposal(project_id, proposal, f"checkin:{who}", actor=email or uid)
    planning_chat._put_message(project_id, "assistant", f"Reporte de avances de {who}:\n{report}",
                               proposalId=item["proposalId"] if item else None)
    owner = proj.get("userId", "")
    if owner and owner != uid:
        _notify(owner, proj, f"{who} reportó sus avances",
                (planning_chat._describe(proposal) if item else "Sin cambios en sus tareas.")
                + " Revísalo en Intelligence > Replanificación.", "checkin_report")
    return {"ok": True, "proposalId": item["proposalId"] if item else None, "operations": len(proposal["operations"])}


# ── the owner ───────────────────────────────────────────────────────────────

def notify_overdue(now: Optional[datetime] = None, send_external: bool = True, only: str = "") -> dict:
    """Hourly: tell each owner who has not reported for more than
    CHECKIN_GRACE_DAYS past the due date. Once per due period per person."""
    from agent.tools import projects_table
    from api.deps import scan_all_pages
    from api.services.planning_live import _notify
    now = now or _now()
    sent = []
    for proj in scan_all_pages(projects_table):
        pid, owner = proj.get("projectId"), proj.get("userId")
        if not pid or not owner or proj.get("status") in ("archived", "completed", "closed"):
            continue
        late = []
        owner_email = _owner_email(owner)
        for person, tasks in assignments(proj, _project_tasks(pid)):
            if owner_email and person.get("email") == owner_email:
                continue                 # the owner is not reminded about themself
            if only and person.get("email") != only:
                continue                 # testing: one person only
            key = person_key(person)
            st = _state(pid, key)
            if not st.get("firstSeenAt"):
                _save_state(pid, key, firstSeenAt=_iso(now))
                continue
            d = due_state(tasks, st, now)
            notified = _dt(st.get("ownerNotifiedAt"))
            if now >= d["due_at"] + timedelta(days=CHECKIN_GRACE_DAYS) and not (notified and notified >= d["due_at"]):
                late.append((person, tasks, st))
                _save_state(pid, key, ownerNotifiedAt=_iso(now))
        if not late:
            continue
        lines = []
        for person, tasks, st in late:
            since = (st.get("lastReportAt") or "")[:10]
            lines.append(f"• {person.get('name') or person.get('email')}: {len(tasks)} tareas abiertas, "
                         + (f"último reporte {since}" if since else "nunca ha reportado")
                         + ("" if person.get("email") else " (sin cuenta: cuéntame su avance en Replanificación)"))
        title = f"{len(late)} persona(s) sin reportar avances en {proj.get('name', '')}"
        _notify(owner, proj, title, "\n".join(lines), "checkin_overdue")
        if send_external:
            _notify_owner_outside(proj, title, lines)
        sent.append({"projectId": pid, "people": [p.get("name") for p, _t, _s in late]})
    return {"projects": sent}


_OWNER_EMAILS: dict = {}


def _owner_email(owner_uid: str) -> str:
    """Projects keep only the owner's sub; the email comes from Cognito.

    The Cognito USERNAME is not the sub: invited users are created with the
    email as username and Google users get 'google_<id>'. So the user is found
    by the sub attribute (list_users filter), with admin_get_user only as a
    fallback for pools where username == sub. Cached per process."""
    if not owner_uid:
        return ""
    if owner_uid in _OWNER_EMAILS:
        return _OWNER_EMAILS[owner_uid]
    email = ""
    try:
        import boto3
        pool = os.environ.get("COGNITO_USER_POOL_ID", "us-east-1_b76prubhx")
        cip = boto3.client("cognito-idp", region_name=os.environ.get("AWS_REGION", "us-east-1"))
        users = cip.list_users(UserPoolId=pool, Filter=f'sub = "{owner_uid}"', Limit=1).get("Users", [])
        if users:
            email = next(((a.get("Value") or "").lower() for a in users[0].get("Attributes", [])
                          if a.get("Name") == "email"), "")
        else:
            resp = cip.admin_get_user(UserPoolId=pool, Username=owner_uid)
            email = next(((a.get("Value") or "").lower() for a in resp.get("UserAttributes", [])
                          if a.get("Name") == "email"), "")
    except Exception as e:
        print(f"[checkin] owner email lookup failed for {owner_uid}: {e}")
    _OWNER_EMAILS[owner_uid] = email
    return email


def _notify_owner_outside(proj: dict, title: str, lines: list) -> None:
    """Email to the owner, and WhatsApp when the owner is also a participant
    with a phone. Failures are logged, never raised."""
    try:
        from agent.tools import clear_current_user, send_notification, set_current_user
        email = _owner_email(proj.get("userId", ""))
        me = next((q for q in (normalize_participant(p) for p in proj.get("participants") or [])
                   if email and q.get("email") == email), {})
        set_current_user(proj.get("userId", ""), email)
        try:
            body = title + "\n" + "\n".join(lines) + "\n\nEntra a OneBox para actualizar su estado o escríbeles."
            if email:
                send_notification(recipient=email, message=body, channel="email",
                                  project_id=proj.get("projectId", ""), project_name=proj.get("name", ""))
            if me.get("phone"):
                send_notification(recipient=me["phone"], message=body, channel="whatsapp",
                                  project_id=proj.get("projectId", ""), project_name=proj.get("name", ""))
        finally:
            clear_current_user()
    except Exception as e:
        print(f"[checkin] external owner notification failed: {e}")
