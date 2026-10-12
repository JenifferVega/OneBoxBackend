"""Members page: the follow-up status of every person in the projects you own,
and their recent updates.

    GET /api/team/status                     one row per (project, person)
    GET /api/team/updates?projectId=&person= the person's recent update chats

Same rules as the follow-up itself (checkin.py / follow_up.py), so the page
shows exactly what the system will do: who is due or late, who is waiting on
you (a blocker on you, a "done" waiting for your approval), who is paused,
who has no account. Owner only: a member sees their own row nowhere here (the
update window is their view).
"""
import json
from datetime import datetime, timedelta
from typing import Dict, List

from fastapi import HTTPException

from agent.planning.sources import normalize_participant, norm


def _updates_by_person(items: list) -> Dict[str, List[dict]]:
    out: Dict[str, List[dict]] = {}
    for i in items:
        if i.get("itemId", "").startswith("upd#") and i.get("personKey"):
            out.setdefault(i["personKey"], []).append(i)
    for v in out.values():
        v.sort(key=lambda i: i.get("closedAt") or i.get("createdAt") or "", reverse=True)
    return out


def _items_of(sess: dict) -> list:
    try:
        return json.loads(sess.get("items") or "[]")
    except ValueError:
        return []


def status(uid: str, email: str) -> dict:
    from agent.tools import projects_table
    from api.deps import scan_all_pages
    from api.services import checkin, follow_up, planning_chat

    now = datetime.utcnow()
    today = now.date()
    rows = []
    projects = []
    for proj in scan_all_pages(projects_table):
        pid = proj.get("projectId")
        if not pid or proj.get("userId") != uid or proj.get("status") in ("archived", "closed"):
            continue
        projects.append({"projectId": pid, "name": proj.get("name", "")})
        tasks = checkin._project_tasks(pid)
        planning_items = planning_chat._items(pid)                 # read once per project
        states = {i["itemId"][len("checkin#"):]: i for i in planning_items if i.get("itemId", "").startswith("checkin#")}
        updates = _updates_by_person(planning_items)
        assigned = {checkin.person_key(p): ts for p, ts in checkin.assignments(proj, tasks)}
        owner_email = (email or "").lower()
        seen = set()
        for raw in proj.get("participants") or []:
            p = normalize_participant(raw)
            if not (p.get("name") or p.get("email")):
                continue
            key = checkin.person_key(p)
            if key in seen:
                continue
            seen.add(key)
            mine = assigned.get(key, [])
            st = states.get(key, {})
            fu = follow_up.state(proj, key, now)
            sess = updates.get(key, [])
            closed = [s for s in sess if s.get("status") == "closed"]
            last_items = _items_of(closed[0]) if closed else []
            waits = checkin.waiting_on_others(last_items, p, mine) if mine else []
            waiting_ids = {w["task_id"] for w in waits}
            resp = [t for t in mine if t.get("taskId") not in waiting_ids]
            committed = checkin.committed(last_items, today)
            d = checkin.due_state(resp, st, now, fu["resumedAt"], committed) if resp else None
            is_owner = bool(owner_email and p.get("email") == owner_email)
            late = bool(d and d["due"] and now >= d["due_at"] + timedelta(days=checkin.CHECKIN_GRACE_DAYS))
            if is_owner:
                state = "owner"
            elif fu["paused"]:
                state = "paused"
            elif not mine:
                state = "no_tasks"
            elif not p.get("email"):
                state = "no_account"
            elif not resp:
                state = "waiting_you"
            elif late:
                state = "late"
            elif d and d["due"]:
                state = "due"
            else:
                state = "ok"
            rows.append({
                "projectId": pid, "projectName": proj.get("name", ""),
                "key": key, "name": p.get("name") or p.get("email", ""), "email": p.get("email", ""),
                "role": p.get("role", ""), "isOwner": is_owner, "state": state,
                "openTasks": len(mine),
                "overdue": sum(1 for t in mine if (t.get("dueDate") or "9999") < today.isoformat()),
                "lastUpdateAt": (closed[0].get("closedAt") if closed else "") or st.get("lastReportAt", ""),
                "updates": len(closed),
                "openUpdate": any(s.get("status") == "open" for s in sess),
                "dueAt": d["due_at"].isoformat(timespec="minutes") if d else "",
                "urgent": bool(d and d["urgent"]),
                "pausedUntil": fu["until"] if fu["paused"] else "",
                "waitingOnYou": [{"taskId": w["task_id"], "kind": w.get("kind", ""), "owner": w["owner"],
                                  "text": w.get("text", "")} for w in waits],
                "reminderSentAt": st.get("reminderSentAt", ""), "ownerNotifiedAt": st.get("ownerNotifiedAt", ""),
                "datesGiven": sorted(committed),
            })
    rows.sort(key=lambda r: (norm(r["name"]), r["projectName"]))
    return {"rows": rows, "projects": projects, "checkinDays": checkin.CHECKIN_DAYS}


def updates(uid: str, project_id: str, person: str, limit: int = 10) -> dict:
    """The person's recent update chats in that project (owner only)."""
    from agent.tools import projects_table, tasks_table
    from api.services import planning_chat
    from api.services.project_export import _query_all
    from boto3.dynamodb.conditions import Key

    proj = projects_table.get_item(Key={"projectId": project_id}).get("Item")
    if not proj:
        raise HTTPException(status_code=404, detail="Project not found")
    if proj.get("userId") != uid:
        raise HTTPException(status_code=403, detail="Only the project owner can read the members' updates")
    key = (person or "").strip().lower()
    sess = _updates_by_person(planning_chat._items(project_id)).get(key, [])[:limit]
    texts = {t["taskId"]: t.get("text", "") for t in
             _query_all(tasks_table, KeyConditionExpression=Key("projectId").eq(project_id))}
    out = []
    for s in sess:
        items = _items_of(s)
        for i in items:
            if i.get("task_id") in texts:
                i["task_text"] = texts[i["task_id"]][:120]
        out.append({"sessionId": s.get("sessionId", ""), "status": s.get("status", ""),
                    "createdAt": s.get("createdAt", ""), "closedAt": s.get("closedAt", ""),
                    "consolidatedAt": s.get("consolidatedAt", ""),
                    "messages": [{"role": m.get("role"), "content": m.get("content", "")}
                                 for m in s.get("messages") or []],
                    "items": items})
    return planning_chat._plain({"projectId": project_id, "projectName": proj.get("name", ""),
                                 "person": person, "updates": out})
