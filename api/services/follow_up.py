"""Pause the follow-up of a member, or of the whole team, in one project.

"Follow-up" is the loop that asks members for their status:
    * update_reminders.send_due  -- the AI emails "hora de actualizar"
    * checkin.pending_for_user   -- the update modal at login
    * checkin.notify_overdue     -- the owner hears "X has not reported"
and, through the reports, the daily consolidation and its proposal. A paused
person is skipped by all three; with nobody reporting, nothing of theirs is
consolidated or replanned. Everything else keeps working: the member may still
report on their own, the owner edits tasks, Trello syncs, and "you were assigned
a task" notices are still sent (information, not follow-up).

Stored on the project item (owner only):
    followUp = {
      "all":     {"until": "YYYY-MM-DD" | "", "by": email, "at": iso, "note": ""},   # whole team
      "people":  {personKey: {...same...}},
      "resumed": {personKey | "all": iso},     # when a pause was lifted by hand
    }
A pause with "until" ends by itself at the end of that day. When it ends (by
hand or by date) the clock restarts from that moment, so the member is not
"overdue" at once and the owner does not get the backlog of the paused days.
"""
from datetime import date, datetime, timedelta
from typing import Optional

from fastapi import HTTPException

ALL = "all"


def _day(s: str) -> Optional[date]:
    try:
        return date.fromisoformat((s or "")[:10])
    except ValueError:
        return None


def _dt(s: str) -> Optional[datetime]:
    try:
        return datetime.fromisoformat((s or "").replace("Z", ""))
    except ValueError:
        return None


def _active(entry: Optional[dict], today: date) -> bool:
    if not entry:
        return False
    until = _day(entry.get("until", ""))
    return until is None or today <= until


def _ended_at(entry: Optional[dict], today: date) -> Optional[datetime]:
    """When a dated pause ended by itself (start of the day after 'until')."""
    if not entry:
        return None
    until = _day(entry.get("until", ""))
    if until and today > until:
        return datetime.combine(until + timedelta(days=1), datetime.min.time())
    return None


def state(proj: dict, key: str, now: Optional[datetime] = None) -> dict:
    """{paused, scope ('all' | 'person' | ''), until, resumedAt} for one person."""
    now = now or datetime.utcnow()
    today = now.date()
    fu = proj.get("followUp") or {}
    person = (fu.get("people") or {}).get(key)
    team = fu.get(ALL)
    if _active(team, today):
        return {"paused": True, "scope": ALL, "until": team.get("until", ""), "resumedAt": None}
    if _active(person, today):
        return {"paused": True, "scope": "person", "until": person.get("until", ""), "resumedAt": None}
    resumed = [d for d in (_dt((fu.get("resumed") or {}).get(key, "")), _dt((fu.get("resumed") or {}).get(ALL, "")),
                           _ended_at(person, today), _ended_at(team, today)) if d]
    return {"paused": False, "scope": "", "until": "", "resumedAt": max(resumed) if resumed else None}


def is_paused(proj: dict, key: str, now: Optional[datetime] = None) -> bool:
    return state(proj, key, now)["paused"]


def view(proj: dict, now: Optional[datetime] = None) -> dict:
    """For the interface: the pauses in force (expired ones left out)."""
    today = (now or datetime.utcnow()).date()
    fu = proj.get("followUp") or {}
    people = {k: v for k, v in (fu.get("people") or {}).items() if _active(v, today)}
    team = fu.get(ALL) if _active(fu.get(ALL), today) else None
    return {"all": team, "people": people}


def set_pause(uid: str, email: str, project_id: str, person: str, paused: bool,
              until: str = "", note: str = "") -> dict:
    """Owner only. person = a participant key (email, or name for people
    without email) or 'all'."""
    from agent.tools import projects_table
    from api.services.checkin import person_key
    from agent.planning.sources import normalize_participant

    proj = projects_table.get_item(Key={"projectId": project_id}).get("Item")
    if not proj:
        raise HTTPException(status_code=404, detail="Project not found")
    if proj.get("userId") != uid:
        raise HTTPException(status_code=403, detail="Only the project owner can pause the follow-up")
    key = (person or "").strip().lower()
    if key != ALL:
        keys = {person_key(normalize_participant(p)) for p in proj.get("participants") or []}
        if key not in keys:
            raise HTTPException(status_code=404, detail="That person is not in the project")
    if until:
        d = _day(until)
        if d is None:
            raise HTTPException(status_code=400, detail="until must be YYYY-MM-DD")
        if d < datetime.utcnow().date():
            raise HTTPException(status_code=400, detail="until is in the past")
    now = datetime.utcnow().isoformat(timespec="seconds")
    fu = dict(proj.get("followUp") or {})
    people = dict(fu.get("people") or {})
    resumed = dict(fu.get("resumed") or {})
    entry = {"until": (until or "")[:10], "by": email or uid, "at": now, "note": (note or "").strip()[:300]}
    if key == ALL:
        if paused:
            fu[ALL] = entry
        elif fu.pop(ALL, None) is not None:
            resumed[ALL] = now
    else:
        if paused:
            people[key] = entry
        elif people.pop(key, None) is not None:
            resumed[key] = now
    fu["people"], fu["resumed"] = people, resumed
    projects_table.update_item(Key={"projectId": project_id}, UpdateExpression="SET followUp = :f",
                               ExpressionAttributeValues={":f": fu})
    return {"projectId": project_id, "followUp": view({**proj, "followUp": fu})}
