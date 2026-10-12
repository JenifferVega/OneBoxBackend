"""'Hora de actualizar': the AI prepares each member's pending questions and
emails them when their update is due.

    hourly cron (/api/scheduled/dispatch-pending)
      -> send_due(): for each member WITH email whose update is due (same clock
         as the modal: CHECKIN_DAYS without reporting, or something due within
         2 days), from REMINDER_HOUR_UTC on, once per due period:
           prepare()  -> UPDATE_PREP: 2-3 concrete questions from their open
                         tasks and what they said last time
           email      -> greeting + questions + button to the app
      -> the same questions OPEN the update chat in the app (opening_for), so
         the email and the chat are one conversation.

Not reminded: the project owner (their update is optional), people without an
email (the owner updates them in Replanificación). If the member still does not
answer, checkin.notify_overdue tells the owner CHECKIN_GRACE_DAYS later.
Replying to the email is not read: the button takes them to the app.
"""
import json
import os
from datetime import datetime, timedelta
from typing import List, Optional

from api.services import checkin, follow_up

REMINDER_HOUR_UTC = int(os.environ.get("UPDATE_REMINDER_HOUR_UTC", "12"))
APP_URL = os.environ.get("APP_URL", "https://www.oneboxmanager.com")
WEEKDAYS = ["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"]


def last_items(project_id: str, key: str) -> list:
    """Items of the member's last closed update."""
    return checkin.last_update_items(project_id, key)


# Fixed texts, in the member's language ('' = unknown: Spanish, as before).
_TEXT = {
    "es": {"overdue": "«{t}» venció el {d}, ¿cómo va?", "due": "«{t}» es para el {d}, ¿vas bien?",
           "how": "¿Cómo va «{t}»?", "subject": "{p}: hora de actualizar", "greeting": "Hola {n}, ¿cómo vas?",
           "default_greeting": "¿Cómo vas?",
           "email": "{g}\n\nEs hora de actualizar {p}. Esto es lo que me gustaría saber:\n\n{q}\n\n"
                    "Cuéntamelo con tus palabras en OneBox (2 minutos): {u}\n\n"
                    "Nada cambia en el plan hasta que {o} lo apruebe."},
    "en": {"overdue": "\"{t}\" was due on {d}, how is it going?", "due": "\"{t}\" is due on {d}, are you on track?",
           "how": "How is \"{t}\" going?", "subject": "{p}: time to update", "greeting": "Hi {n}, how are you doing?",
           "default_greeting": "How are you doing?",
           "email": "{g}\n\nIt's time to update {p}. This is what I'd like to know:\n\n{q}\n\n"
                    "Tell me in your own words in OneBox (2 minutes): {u}\n\n"
                    "Nothing changes in the plan until {o} approves it."},
}


def _t(lang: str) -> dict:
    return _TEXT["en" if lang == "en" else "es"]


def _fallback(person: dict, tasks: List[dict], today: str, project_name: str, lang: str = "") -> dict:
    """No LLM: overdue / soon first, then the rest."""
    first = (person.get("name") or "").split(" ")[0]
    order = sorted(tasks, key=lambda t: t.get("dueDate") or "9999")
    tx = _t(lang)
    qs = []
    for t in order[:3]:
        label = (t.get("text") or "")[:70]
        if t.get("dueDate") and t["dueDate"] < today:
            qs.append(tx["overdue"].format(t=label, d=t["dueDate"]))
        elif t.get("dueDate"):
            qs.append(tx["due"].format(t=label, d=t["dueDate"]))
        else:
            qs.append(tx["how"].format(t=label))
    return {"subject": tx["subject"].format(p=project_name), "greeting": tx["greeting"].format(n=first),
            "questions": qs}


def prepare(proj: dict, person: dict, tasks: List[dict], today: str = "", llm=None, lang: str = "") -> dict:
    """lang = the member's app language ('es' / 'en'); '' = unknown: the
    project's language, as before."""
    from agent.planning import prompts
    from agent.planning.nodes import _fill, call_structured
    from agent.planning.runner import default_llm
    from agent.planning.schemas import UpdatePrep
    from api.services.member_updates import _tasks_lines, _today, owner_name
    today = today or _today().isoformat()
    d = datetime.fromisoformat(today)
    items = last_items(proj["projectId"], checkin.person_key(person))
    stopped = {w["task_id"] for w in checkin.waiting_on_others(items, person, tasks)}
    tasks = [t for t in tasks if t.get("taskId") not in stopped] or tasks   # not their step: the lead is told
    waiting = checkin.committed(items, d.date())
    language = {"en": "English", "es": "Spanish"}.get(lang, "the language of the project")
    answered = [f"- {t.get('text', '')[:90]} (said: {i.get('due_date')})"
                for i in items for t in tasks if i.get("task_id") == t.get("taskId") and t.get("taskId") in waiting]
    # Dated answers about work that is not a task ("la POC es el martes"):
    # no task to skip, but the AI must not ask about them before that date.
    answered += [f"- {(i.get('text') or '')[:90]} (said: {i.get('due_date')})" for i in items
                 if not i.get("task_id") and (i.get("due_date") or "") >= today
                 and i.get("type") not in ("done", "idea", "question", "hearsay")]
    system = _fill(prompts.UPDATE_PREP, today=today, weekday=WEEKDAYS[d.weekday()], language=language,
                   calendar=checkin.calendar_text(d.date()),
                   waiting="\n".join(answered) or "(none)",
                   project=f"{proj.get('name', '')} — {proj.get('description', '')}"[:300],
                   owner=owner_name(proj), member=person.get("name") or person.get("email", ""),
                   my_tasks=_tasks_lines(tasks),
                   last_items=json.dumps([{k: i.get(k) for k in ("type", "task_id", "text", "due_date", "owner")}
                                          for i in items], ensure_ascii=False) if items else "(none)")
    try:
        out, err = call_structured(llm or default_llm(), UpdatePrep, system, is_empty=lambda o: not o.questions)
    except Exception as e:
        out, err = None, str(e)
    if err or not out or not out.questions:
        prep = _fallback(person, [t for t in tasks if t.get("taskId") not in waiting] or tasks,
                         today, proj.get("name", ""), lang)
    else:
        prep = {"subject": out.subject or _t(lang)["subject"].format(p=proj.get("name", "")),
                "greeting": out.greeting, "questions": [q for q in out.questions if q.strip()][:3]}
    ids = {t["taskId"] for t in tasks}
    try:
        ids |= {t["taskId"] for t in checkin._project_tasks(proj["projectId"])}
    except Exception:
        pass
    prep["questions"] = [q for q in prep["questions"] if not any(i in q for i in ids)]   # never show task ids
    return {**prep, "lang": lang, "preparedAt": datetime.utcnow().isoformat()}


def opening_text(prep: dict) -> str:
    qs = prep.get("questions") or []
    return (prep.get("greeting") or _t(prep.get("lang", ""))["default_greeting"]) + ("\n" + "\n".join(f"• {q}" for q in qs) if qs else "")


def email_text(proj: dict, prep: dict) -> str:
    from api.services.member_updates import owner_name
    qs = "\n".join(f"{n}. {q}" for n, q in enumerate(prep.get("questions") or [], 1))
    return _t(prep.get("lang", ""))["email"].format(g=prep.get("greeting", ""), p=proj.get("name", ""), q=qs,
                                                      u=APP_URL, o=owner_name(proj))


def opening_for(proj: dict, person: dict, tasks: List[dict], llm=None, lang: str = "") -> Optional[dict]:
    """The prepared questions for the member's NEXT update (prepared after
    their last report, in their language); prepared now if missing."""
    key = checkin.person_key(person)
    st = checkin._state(proj["projectId"], key)
    prep = json.loads(st["prep"]) if st.get("prep") else None
    if prep and (prep.get("preparedAt", "") > (st.get("lastReportAt") or "")) and prep.get("lang", "") == lang:
        return prep
    if not tasks:
        return None
    prep = prepare(proj, person, tasks, llm=llm, lang=lang)
    checkin._save_state(proj["projectId"], key, prep=json.dumps(prep, ensure_ascii=False))
    return prep


def _send(proj: dict, person: dict, prep: dict) -> dict:
    from agent.tools import clear_current_user, set_current_user
    from agent.tools.emails import send_email
    set_current_user(proj.get("userId", ""), checkin._owner_email(proj.get("userId", "")))
    try:
        return send_email(person["email"], prep.get("subject", ""), email_text(proj, prep),
                          project_id=proj["projectId"], project_name=proj.get("name", ""))
    finally:
        clear_current_user()


def send_due(now: Optional[datetime] = None, llm=None, send: bool = True, force: bool = False,
             only: str = "") -> dict:
    """Hourly: one 'hora de actualizar' email per member and due period.
    force (testing): no hour gate, and the owner is included."""
    from agent.tools import projects_table
    from api.deps import scan_all_pages
    now = now or datetime.utcnow()
    sent = []
    if now.hour < REMINDER_HOUR_UTC and not force:
        return {"sent": sent}
    for proj in scan_all_pages(projects_table):
        pid, owner = proj.get("projectId"), proj.get("userId")
        if not pid or proj.get("status") in ("archived", "completed", "closed"):
            continue
        owner_email = checkin._owner_email(owner or "")
        for person, tasks in checkin.assignments(proj, checkin._project_tasks(pid)):
            email = (person.get("email") or "").lower()
            if not email or (email == owner_email and not force) or (only and email != only):
                continue
            key = checkin.person_key(person)
            fu = follow_up.state(proj, key, now)
            if fu["paused"]:
                continue                 # the owner paused this person's follow-up
            tasks, _waits, items = checkin.responsible_tasks(pid, key, person, tasks)
            if not tasks:
                continue                 # everything waits on someone else: the lead is told instead
            st = checkin._state(pid, key)
            if not st.get("firstSeenAt"):
                checkin._save_state(pid, key, firstSeenAt=checkin._iso(now))
                st["firstSeenAt"] = checkin._iso(now)
            d = checkin.due_state(tasks, st, now, fu["resumedAt"], checkin.committed(items, now.date()))
            seen = checkin._dt(st.get("firstSeenAt")) or now
            if not st.get("lastReportAt") and now - seen < timedelta(days=checkin.CHECKIN_DAYS) and not only:
                # Someone the system has just started tracking (new project, new
                # assignment, cron switched on for the first time): the modal asks
                # them at their next login, the email waits a full period. Avoids
                # one email to every person the day the cron starts.
                continue
            last = checkin._dt(st.get("reminderSentAt"))
            if not d["due"] or (last and last >= d["due_at"]) or (last and last.date() == now.date()):
                continue
            from api.services.users import language_of
            prep = prepare(proj, person, tasks, today=now.date().isoformat(), llm=llm, lang=language_of(email=email))
            checkin._save_state(pid, key, prep=json.dumps(prep, ensure_ascii=False), reminderSentAt=checkin._iso(now))
            res = _send(proj, person, prep) if send else {"status": "not_sent"}
            sent.append({"projectId": pid, "person": person.get("name") or email, "status": res.get("status"),
                         "questions": prep["questions"]})
    return {"sent": sent}
