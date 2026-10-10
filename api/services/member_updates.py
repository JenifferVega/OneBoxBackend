"""Member updates: the most important loop of the product.

    member (chat in the app) --> recorded report items --> consolidation -->
    proposal + digest for the owner --> owner approves --> app + Trello

This module is the first step. A member tells how their work is going in
their own words; the AI asks at most 2 short questions per turn to make it
concrete and closes with a summary. Nothing in the plan changes here: the
update is stored as classified ITEMS (done, progress, blocker, date_change,
plan, idea, question, hearsay) with certainty and the member's own words.
Consolidation (updates_consolidation.py) turns them into a proposal.

Storage (table onebox-planning), one session per member and conversation:
    upd#<iso>#<person>   {status open|closed, person, email, messages[], items[],
                          closedAt, consolidatedAt}
A closed session counts as the member's report (check-in clock reset), and an
urgent blocker (depends on the owner / due in 2 days) notifies the owner at once.
"""
import json
import uuid
from datetime import date, datetime
from typing import List, Optional

from fastapi import HTTPException

from agent.planning.sources import normalize_participant, norm
from api.services import checkin, planning_chat

WEEKDAYS = ["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"]
TODAY = ""          # tests pin the date


def _today() -> date:
    return date.fromisoformat(TODAY) if TODAY else datetime.utcnow().date()


def _now() -> str:
    return datetime.utcnow().isoformat()


def owner_name(proj: dict) -> str:
    """Name of the owner as the team calls them (participant with the owner's
    email), else a neutral label."""
    email = checkin._owner_email(proj.get("userId", ""))
    for p in proj.get("participants") or []:
        q = normalize_participant(p)
        if email and q.get("email") == email:
            return q.get("name") or email
    return email or "el dueño del proyecto"


def _who(proj: dict, email: str, name: str = "") -> dict:
    email = (email or "").lower()
    for p in proj.get("participants") or []:
        q = normalize_participant(p)
        if (email and q.get("email") == email) or (name and norm(q.get("name", "")) == norm(name)):
            return q
    return {"name": name or email, "email": email}


def _session(project_id: str, key: str) -> Optional[dict]:
    open_ = [i for i in planning_chat._items(project_id)
             if i["itemId"].startswith("upd#") and i.get("personKey") == key and i.get("status") == "open"]
    return sorted(open_, key=lambda i: i["itemId"])[-1] if open_ else None


def _workday(day: str, certainty: str) -> str:
    """A vague date ("la otra semana") that the model put on a weekend goes
    back to the Friday before; an explicit, sure date is left as said."""
    try:
        d = date.fromisoformat(day)
    except ValueError:
        return day
    if certainty == "sure" or d.weekday() < 5:
        return day
    return date.fromordinal(d.toordinal() - (d.weekday() - 4)).isoformat()


CLOSING = {"listo", "lista", "eso", "eso es todo", "es todo", "nada mas", "nada mas x ahora", "nada mas por ahora",
           "gracias", "eso gracias", "ok", "vale", "dale", "ya", "ya esta", "chao", "bye", "nada", "todo bien"}
STATUS = ("done", "progress", "not_started", "blocker")


def _closing_only(messages: List[str]) -> bool:
    return all(norm(m).strip(" .!,") in CLOSING or len(norm(m).split()) <= 2 and
               any(norm(m).startswith(c) for c in ("listo", "eso", "nada", "gracias")) for m in messages)


def _supported(item: dict, text: str) -> bool:
    """The item's quote (or failing that its text) is in what the member wrote."""
    t = norm(text)
    words = [w for w in norm(item.get("quote") or "").split() if len(w) > 2]
    if words and sum(1 for w in words if w in t) / len(words) >= 0.6:
        return True
    words = [w for w in norm(item.get("text") or "").split() if len(w) > 3]   # paraphrase: looser
    return bool(words) and sum(1 for w in words if w in t) / len(words) >= 0.4


def guard_items(prev: list, new: list, turn_msgs: List[str], earlier_msgs: List[str]) -> list:
    """The model rewrites the FULL item list every turn, so a later turn can
    flip an earlier item (the member's closing "listo" read as "seguridad:
    listo"). Code decides what a turn may change:
      * a closing-only turn changes nothing;
      * a new STATUS for a task that already had one needs support in THIS
        turn's words; otherwise the earlier item stays;
      * any other new item needs support somewhere in the member's words
        (questions and ideas never change the plan: always kept);
      * an earlier item the model silently dropped comes back unless this turn
        talks about it.
    """
    if not prev:
        allm = " ".join(turn_msgs + earlier_msgs)
        return [i for i in new if i.get("type") in ("question", "idea") or _supported(i, allm)]
    if _closing_only(turn_msgs):
        return prev
    turn, allm = " ".join(turn_msgs), " ".join(turn_msgs + earlier_msgs)
    sig = lambda i: (i.get("task_id"), i.get("type"), norm(i.get("quote") or ""))  # noqa: E731
    prev_sigs = {sig(i) for i in prev}
    prev_status = {i["task_id"]: i["type"] for i in prev if i.get("task_id") and i.get("type") in STATUS}
    out = []
    for i in new:
        if sig(i) in prev_sigs:
            out.append(i)
            continue
        tid = i.get("task_id")
        if tid and i.get("type") in STATUS and tid in prev_status and prev_status[tid] != i["type"]:
            if _supported(i, turn):
                out.append(i)
            continue
        if i.get("type") in ("question", "idea") or _supported(i, allm):
            out.append(i)
    kept = {sig(i) for i in out}
    for i in prev:
        if sig(i) in kept:
            continue
        same = lambda o: o.get("type") == i.get("type") or (o.get("type") in STATUS and i.get("type") in STATUS)  # noqa: E731
        replaced = any(o.get("task_id") and o.get("task_id") == i.get("task_id") and same(o) for o in out)
        if not replaced and not (_supported(i, turn) and i.get("task_id") in {o.get("task_id") for o in out}):
            out.append(i)
    return out


LABEL = {"done": "Hecho", "progress": "Avanza", "not_started": "Sin empezar", "blocker": "Bloqueo",
         "date_change": "Nueva fecha", "plan": "Plan", "idea": "Propuesta", "question": "Pregunta",
         "hearsay": "Por confirmar"}


def closing_summary(items: list, name: str) -> str:
    first = (name or "").split(" ")[0]
    if not items:
        return f"Gracias{', ' + first if first else ''}. No registré cambios esta vez."
    lines = []
    for i in items:
        extra = []
        if i.get("due_date"):
            extra.append(("si se cumple la condición, " if i.get("certainty") == "conditional" else "") + i["due_date"])
        if i.get("type") == "blocker" and i.get("owner"):
            extra.append(f"necesita a {i['owner']}")
        lines.append(f"• {LABEL.get(i.get('type'), i.get('type', ''))}: {i.get('text', '')}"
                     + (f" ({'; '.join(extra)})" if extra else ""))
    return (f"Gracias{', ' + first if first else ''}. Registré esto:\n" + "\n".join(lines)
            + "\nSe lo paso al dueño del proyecto; nada cambia en el plan hasta que lo apruebe.")


def _tasks_lines(tasks, with_owner=False) -> str:
    out = []
    for t in tasks:
        parts = [t["taskId"]] + ([t.get("assignedTo") or "-"] if with_owner else []) + \
                [t.get("status", ""), t.get("dueDate") or "-", (t.get("text") or "")[:120]]
        out.append(" | ".join(parts))
    return "\n".join(out) or "(none)"


def converse(uid: str, email: str, project_id: str, messages: List[str], name: str = "", llm=None) -> dict:
    """One member turn. Returns {reply, finished, items, sessionId}."""
    from agent.planning import prompts
    from agent.planning.nodes import _fill, _project_line, call_structured
    from agent.planning.runner import default_llm
    from agent.planning.schemas import UpdateTurn
    from agent.planning.sources import load_snapshot
    from agent.tools import projects_table
    from api.services.planning_live import snapshot

    messages = [m.strip() for m in messages or [] if (m or "").strip()]
    if not messages:
        raise HTTPException(status_code=400, detail="Empty message")
    proj = planning_chat._access(uid, email, project_id)
    proj = projects_table.get_item(Key={"projectId": project_id}).get("Item") or proj
    from api.services.projects import ensure_open
    ensure_open(proj)
    person = _who(proj, email, name)
    key = checkin.person_key(person)
    export = snapshot(project_id)
    tasks = export["tasks"]
    mine_ids = set()
    for p, ts in checkin.assignments(proj, tasks):
        if checkin.person_key(p) == key:
            mine_ids = {t["taskId"] for t in ts}
    mine = [t for t in tasks if t["taskId"] in mine_ids]
    others = [t for t in tasks if t["taskId"] not in mine_ids and t.get("status") != "done"]

    sess = _session(project_id, key)
    if not sess:
        opening = []
        prep = _prepared(proj, person, mine)
        if prep:     # the questions of the 'hora de actualizar' email open the chat
            from api.services.update_reminders import opening_text
            opening = [{"role": "assistant", "content": opening_text(prep), "at": _now()}]
        sess = {"projectId": project_id, "itemId": f"upd#{_now()}#{key}", "sessionId": uuid.uuid4().hex[:12],
                "status": "open", "person": person.get("name") or email, "email": email, "personKey": key,
                "messages": opening, "items": "[]", "createdAt": _now()}
    convo = "\n".join(f"{m['role']}: {m['content']}" for m in sess.get("messages") or []) or "(start)"
    today = _today()
    snap = load_snapshot(export)
    system = _fill(prompts.MEMBER_UPDATE, today=today.isoformat(), weekday=WEEKDAYS[today.weekday()],
                   project=_project_line(snap), owner=owner_name(proj), member=person.get("name") or email,
                   my_tasks=_tasks_lines(mine), other_tasks=_tasks_lines(others, with_owner=True),
                   items=sess.get("items") or "[]", conversation=convo, messages="\n".join(messages))
    out, err = call_structured(llm or default_llm(), UpdateTurn, system, is_empty=lambda o: not o.reply)
    if err:
        raise HTTPException(status_code=502, detail=f"The assistant could not answer: {err[:200]}")
    items = guard_items(json.loads(sess.get("items") or "[]"), [i.model_dump() for i in out.items], messages,
                        [m["content"] for m in sess.get("messages") or [] if m.get("role") == "member"])
    valid = {t["taskId"] for t in tasks}
    for i in items:                       # an id the model invented is dropped, not trusted
        if i.get("task_id") and i["task_id"] not in valid:
            match = next((t for t in valid if t.startswith(i["task_id"])), "")
            i["task_id"] = match
        if i.get("depends_on") and i["depends_on"] not in valid:
            i["depends_on"] = next((t for t in valid if t.startswith(i["depends_on"])), "")
        i["due_date"] = _workday(i.get("due_date") or "", i.get("certainty") or "")
    if out.finished:
        # The closing summary is built from what was RECORDED (after the
        # guards), so the member never reads a status they did not give.
        out.reply = closing_summary(items, person.get("name") or "")
    sess["messages"] = list(sess.get("messages") or []) + \
        [{"role": "member", "content": m, "at": _now()} for m in messages] + \
        [{"role": "assistant", "content": out.reply, "at": _now()}]
    sess["items"] = json.dumps(items, ensure_ascii=False)
    sess["updatedAt"] = _now()
    if out.finished:
        sess["status"], sess["closedAt"] = "closed", _now()
    planning_chat.table().put_item(Item=sess)
    if out.finished:
        _on_close(proj, sess, items)
    return {"reply": out.reply, "finished": bool(out.finished), "items": items, "sessionId": sess["sessionId"]}


def _on_close(proj: dict, sess: dict, items: list) -> None:
    """The update counts as the member's report; urgent blockers go to the owner now."""
    from api.services.planning_live import _notify
    checkin._save_state(proj["projectId"], sess["personKey"], lastReportAt=_now(), lastReportBy=sess.get("email", ""))
    urgent = [i for i in items if i.get("urgent")]
    owner_is_reporting = (sess.get("email") or "") == checkin._owner_email(proj.get("userId", ""))
    if urgent and proj.get("userId") and not owner_is_reporting:   # no alert to yourself
        lines = [f"• {i.get('text', '')}" + (f" (necesita: {i['owner']})" if i.get("owner") else "") for i in urgent]
        title = f"Urgente en {proj.get('name', '')}: {sess.get('person', '')} reportó un bloqueo"
        _notify(proj["userId"], proj, title, "\n".join(lines), "update_urgent")
        checkin._notify_owner_outside(proj, title, lines)


def pending(uid: str, email: str, name: str = "") -> dict:
    """Projects where this person owes an update (same clock as the check-in)."""
    return checkin.pending_for_user(uid, email, name)


def sessions(project_id: str, since: str = "", only_unconsolidated: bool = True) -> list:
    out = []
    for i in planning_chat._items(project_id):
        if not i["itemId"].startswith("upd#") or i.get("status") != "closed":
            continue
        if only_unconsolidated and i.get("consolidatedAt"):
            continue
        if since and i.get("closedAt", "") < since:
            continue
        out.append({**i, "items": json.loads(i.get("items") or "[]")})
    return sorted(out, key=lambda i: i.get("closedAt", ""))


def _prepared(proj: dict, person: dict, tasks: list) -> Optional[dict]:
    """Prepared questions for this member's next update (never raises)."""
    try:
        from api.services.update_reminders import opening_for
        return opening_for(proj, person, tasks)
    except Exception as e:
        print(f"[member_updates] opening failed: {e}")
        return None


def open_session(uid: str, email: str, project_id: str, name: str = "") -> dict:
    """The member's open conversation (to resume after a reload), or the
    prepared opening questions of a new one."""
    from agent.tools import projects_table
    proj = planning_chat._access(uid, email, project_id)
    proj = projects_table.get_item(Key={"projectId": project_id}).get("Item") or proj
    from api.services.projects import ensure_open
    ensure_open(proj)
    person = _who(proj, email, name)
    key = checkin.person_key(person)
    sess = _session(project_id, key)
    if not sess:
        mine = next((ts for p, ts in checkin.assignments(proj, checkin._project_tasks(project_id))
                     if checkin.person_key(p) == key), [])
        prep = _prepared(proj, person, mine) if mine else None
        if prep:
            from api.services.update_reminders import opening_text
            return {"sessionId": None, "items": [], "messages": [{"role": "assistant", "content": opening_text(prep)}]}
        return {"sessionId": None, "messages": [], "items": []}
    return planning_chat._plain({"sessionId": sess["sessionId"], "items": json.loads(sess.get("items") or "[]"),
                                 "messages": [{"role": m.get("role"), "content": m.get("content")}
                                              for m in sess.get("messages") or []]})
