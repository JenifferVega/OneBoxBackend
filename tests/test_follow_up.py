"""Pausing the follow-up of a member / the whole team (api/services/follow_up.py).

No network, no AWS, no model. Run: python tests/test_follow_up.py
"""
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import agent.tools as tools  # noqa: E402
import api.deps as deps  # noqa: E402
import api.services.access as access  # noqa: E402
import api.services.planning_live as PL  # noqa: E402
from api.services import checkin, follow_up, update_reminders  # noqa: E402

ok = True


def check(name, cond, detail=""):
    global ok
    print(("  PASS " if cond else "  FAIL ") + name + ("" if cond else f"  -> {detail}"))
    ok = ok and bool(cond)


class Projects:
    def __init__(self, items):
        self.items = {i["projectId"]: dict(i) for i in items}

    def get_item(self, Key):
        k = Key["projectId"]
        return {"Item": dict(self.items[k])} if k in self.items else {}

    def update_item(self, Key, UpdateExpression, ExpressionAttributeValues, **_kw):
        assert UpdateExpression == "SET followUp = :f"
        self.items[Key["projectId"]]["followUp"] = ExpressionAttributeValues[":f"]


NOW = datetime.utcnow().replace(microsecond=0)   # pauses are stamped with the real clock
TODAY = NOW.date()
PROJ = {"projectId": "p1", "userId": "own", "name": "Demo", "status": "active",
        "participants": [{"name": "Owner", "email": "own@x.com"}, {"name": "Ana", "email": "ana@x.com"},
                         {"name": "Luis", "email": "luis@x.com"}, {"name": "Rafa"}]}
TASKS = [{"taskId": "t1", "text": "Diseño", "status": "pending", "assignedTo": "Ana"},
         {"taskId": "t2", "text": "Código", "status": "in_progress", "assignedTo": "Luis"},
         {"taskId": "t3", "text": "Datos", "status": "pending", "assignedTo": "Rafa"}]
projects = Projects([PROJ])
tools.projects_table = projects
STATE: dict = {}
NOTIFIED: list = []
checkin._project_tasks = lambda pid: [dict(t) for t in TASKS]
checkin._state = lambda pid, key: dict(STATE.get(key, {}))
checkin._save_state = lambda pid, key, **f: STATE.setdefault(key, {}).update(f)
checkin._owner_email = lambda uid: "own@x.com"
checkin._notify_owner_outside = lambda *a, **k: None
access.accessible_project_ids = lambda uid, email: {"p1"}
deps.scan_all_pages = lambda table: [dict(i) for i in projects.items.values()]
PL._notify = lambda owner, proj, title, body, kind: NOTIFIED.append((title, body))
_real_prepare = update_reminders.prepare
update_reminders.prepare = lambda proj, person, tasks, today="", llm=None, lang="": {"subject": "s", "questions": ["q"]}
from api.services import planning_chat, users  # noqa: E402
UPDATES: list = []                       # closed member updates (upd# items)
planning_chat._items = lambda pid: list(UPDATES)
users.language_of = lambda uid="", email="": {"ana@x.com": "en"}.get(email, "")

# Everyone last reported 10 days ago: all are due and late.
old = (NOW - timedelta(days=10)).isoformat()
for k in ("ana@x.com", "luis@x.com", "rafa"):
    STATE[k] = {"firstSeenAt": old, "lastReportAt": old}


def pending(email):
    return [p["person"] for p in checkin.pending_for_user("u", email)["projects"]]


def reminded():
    return sorted(s["person"] for s in update_reminders.send_due(now=NOW, send=False)["sent"])


def late():
    NOTIFIED.clear()
    checkin.notify_overdue(now=NOW, send_external=False)
    return NOTIFIED[0][1] if NOTIFIED else ""


print("── no pause: everyone is followed up ──")
checkin._now = lambda: NOW
check("Ana gets the update modal", pending("ana@x.com") == ["Ana"])
check("members with email get the reminder email", reminded() == ["Ana", "Luis"], reminded())
body = late()
check("the owner hears about Ana, Luis and Rafa", all(n in body for n in ("Ana", "Luis", "Rafa")), body)

print("── only the owner can pause ──")
try:
    follow_up.set_pause("someone", "x@x.com", "p1", "ana@x.com", True)
    check("a member cannot pause", False)
except Exception as e:
    check("a member cannot pause", getattr(e, "status_code", 0) == 403, e)
try:
    follow_up.set_pause("own", "own@x.com", "p1", "nobody@x.com", True)
    check("unknown person refused", False)
except Exception as e:
    check("unknown person refused", getattr(e, "status_code", 0) == 404, e)

print("── pause one member ──")
follow_up.set_pause("own", "own@x.com", "p1", "ana@x.com", True, note="vacaciones")
for k in STATE:
    STATE[k].pop("ownerNotifiedAt", None)
    STATE[k].pop("reminderSentAt", None)
check("Ana gets no modal", pending("ana@x.com") == [], pending("ana@x.com"))
check("Ana gets no reminder email; Luis still does", reminded() == ["Luis"], reminded())
body = late()
check("the owner is not told Ana is late; Luis and Rafa still", "Ana" not in body and "Luis" in body and "Rafa" in body, body)
check("the project lists the pause", "ana@x.com" in follow_up.view(projects.items["p1"])["people"])

print("── pause a person without email (by name) ──")
follow_up.set_pause("own", "own@x.com", "p1", "rafa", True)
STATE["rafa"].pop("ownerNotifiedAt", None)
STATE["luis@x.com"].pop("ownerNotifiedAt", None)
body = late()
check("Rafa (no email) paused by name", "Rafa" not in body and "Luis" in body, body)

print("── resume: no avalanche ──")
follow_up.set_pause("own", "own@x.com", "p1", "ana@x.com", False)
check("after resuming, Ana is not overdue at once (the clock restarts)", pending("ana@x.com") == [],
      pending("ana@x.com"))
later = datetime.utcnow() + timedelta(days=checkin.CHECKIN_DAYS, minutes=5)
checkin._now = lambda: later
check(f"...but she is due again {checkin.CHECKIN_DAYS} days later", pending("ana@x.com") == ["Ana"])
checkin._now = lambda: NOW

print("── pause the whole team ──")
follow_up.set_pause("own", "own@x.com", "p1", "all", True)
for k in STATE:
    STATE[k].pop("ownerNotifiedAt", None)
    STATE[k].pop("reminderSentAt", None)
check("nobody gets a modal", pending("luis@x.com") == [])
check("nobody gets a reminder email", reminded() == [], reminded())
check("the owner gets no 'late' notice", late() == "", late())
follow_up.set_pause("own", "own@x.com", "p1", "all", False)

print("── a dated pause ends by itself ──")
follow_up.set_pause("own", "own@x.com", "p1", "luis@x.com", True, until=TODAY.isoformat())
check("paused until today (inclusive)", follow_up.is_paused(projects.items["p1"], "luis@x.com", NOW))
tomorrow = NOW + timedelta(days=1)
st = follow_up.state(projects.items["p1"], "luis@x.com", tomorrow)
check("tomorrow it is over, and the clock restarts at midnight",
      not st["paused"] and st["resumedAt"] == datetime.combine(TODAY + timedelta(days=1), datetime.min.time()), st)
check("an expired pause is not shown", "luis@x.com" not in follow_up.view(projects.items["p1"], tomorrow)["people"])
try:
    follow_up.set_pause("own", "own@x.com", "p1", "luis@x.com", True, until="2020-01-01")
    check("a date in the past is refused", False)
except Exception as e:
    check("a date in the past is refused", getattr(e, "status_code", 0) == 400, e)

print("── the daily digest says 'paused', not 'did not report' ──")
from api.services import updates_consolidation as UC  # noqa: E402
follow_up.set_pause("own", "own@x.com", "p1", "rafa", True, until=(TODAY + timedelta(days=5)).isoformat())
b = UC.build(projects.items["p1"], {t["taskId"]: dict(t) for t in TASKS}, [], None, TODAY.isoformat())
check("Rafa is under 'paused', not under 'did not report'",
      any("Rafa" in p for p in b["paused"]) and "Rafa" not in b["not_reported"], (b["paused"], b["not_reported"]))
txt = UC.digest_text(projects.items["p1"], {t["taskId"]: dict(t) for t in TASKS}, b, TODAY.isoformat())
check("the digest text shows it with its end date", "Seguimiento pausado:" in txt and "Rafa (hasta" in txt, txt[:300])

print("── a date the member already gave is not asked every day ──")
import json  # noqa: E402
follow_up.set_pause("own", "own@x.com", "p1", "rafa", False)
TASKS[1]["dueDate"] = (TODAY + timedelta(days=2)).isoformat()      # Luis: due in 2 days -> urgent
YESTERDAY_NOON = datetime.combine(TODAY - timedelta(days=1), datetime.min.time()) + timedelta(hours=12)
STATE["luis@x.com"] = {"firstSeenAt": old, "lastReportAt": YESTERDAY_NOON.isoformat()}
projects.items["p1"]["followUp"] = {}
check("due in 2 days and last report yesterday: asked again today (urgent)", pending("luis@x.com") == ["Luis"])
UPDATES.append({"itemId": "upd#1#luis@x.com", "personKey": "luis@x.com", "status": "closed", "closedAt": "1",
                "items": json.dumps([{"type": "plan", "task_id": "t2", "due_date": TASKS[1]["dueDate"],
                                      "text": "La presentación es el martes"}])})
check("...but not once he said 'it is on Tuesday' (a date that has not arrived)", pending("luis@x.com") == [],
      pending("luis@x.com"))
checkin._now = lambda: NOW + timedelta(days=3)                      # the day after that date
check("the day after the date, he is asked again (did it happen?)", pending("luis@x.com") == ["Luis"])
checkin._now = lambda: NOW
check("committed(): ignores 'done' items and past dates",
      checkin.committed([{"type": "done", "task_id": "a", "due_date": "2999-01-01"},
                         {"type": "plan", "task_id": "b", "due_date": "2000-01-01"},
                         {"type": "date_change", "task_id": "c", "due_date": "2999-01-01"}], TODAY) == {"c"})

print("── the questions come in the member's language ──")
update_reminders.prepare = _real_prepare


class NoLLM:
    def with_structured_output(self, schema, **_kw):
        class R:
            def invoke(self, _m):
                raise RuntimeError("offline")
        return R()


en = update_reminders.prepare(projects.items["p1"], {"name": "Ana", "email": "ana@x.com"}, [TASKS[0]],
                              today=TODAY.isoformat(), llm=NoLLM(), lang="en")
es = update_reminders.prepare(projects.items["p1"], {"name": "Ana", "email": "ana@x.com"}, [TASKS[0]],
                              today=TODAY.isoformat(), llm=NoLLM(), lang="es")
check("English member: English greeting and questions", en["greeting"].startswith("Hi Ana") and "How is" in en["questions"][0], en)
check("Spanish member: Spanish", es["greeting"].startswith("Hola Ana") and "¿Cómo va" in es["questions"][0], es)
check("the language is stored with the prepared questions", en["lang"] == "en" and es["lang"] == "es")
check("email text follows the language", "time to update" in update_reminders.email_text(projects.items["p1"], en).lower()
      or "It's time to update" in update_reminders.email_text(projects.items["p1"], en))
STATE["ana@x.com"] = {"firstSeenAt": old, "prep": json.dumps({**es, "preparedAt": "9999"})}
update_reminders.prepare = lambda proj, person, tasks, today="", llm=None, lang="": {"greeting": "Hi", "questions": ["q"], "lang": lang, "preparedAt": "9999"}
again = update_reminders.opening_for(projects.items["p1"], {"name": "Ana", "email": "ana@x.com"}, [TASKS[0]], lang="en")
check("questions prepared in Spanish are prepared again when she uses English", again["lang"] == "en", again)

print("── whoever is responsible now gets asked: waiting on the lead ──")
update_reminders.prepare = lambda proj, person, tasks, today="", llm=None, lang="": {"subject": "s", "questions": ["q"]}
UPDATES.clear()
projects.items["p1"]["followUp"] = {}
TASKS[1]["dueDate"] = ""
for k in ("ana@x.com", "luis@x.com", "rafa"):
    STATE[k] = {"firstSeenAt": old, "lastReportAt": old}
NOTIFIED.clear()
UPDATES.append({"itemId": "upd#2#ana@x.com", "personKey": "ana@x.com", "status": "closed", "closedAt": "2",
                "items": json.dumps([{"type": "blocker", "task_id": "t1", "owner": "Owner",
                                      "text": "Necesita el acceso a Meta Business", "quote": "sin el acceso no puedo"}])})
UPDATES.append({"itemId": "upd#3#luis@x.com", "personKey": "luis@x.com", "status": "closed", "closedAt": "3",
                "items": json.dumps([{"type": "blocker", "task_id": "t2", "owner": "Luis",
                                      "text": "Tengo que terminar el diseño primero"}])})
check("Ana (waiting on the lead) gets no mandatory window", pending("ana@x.com") == [], pending("ana@x.com"))
check("...and no reminder email; Luis (blocked on his own work) still does", reminded() == ["Luis"], reminded())
body = late()
check("the lead is not told Ana is 'late'", "Ana" not in body and "Luis" in body, body)
NOTIFIED.clear()
r = checkin.notify_waiting(now=NOW, send_external=False)
title, text = NOTIFIED[0] if NOTIFIED else ("", "")
check("the lead is told who waits on THEM, with the task", r["projects"] and r["projects"][0]["onLead"] == 1
      and "Ana espera de ti" in text and "Meta Business" in text and "Diseño" in text, (title, text))
check("...and that the member was not asked", "No le pedí actualización a Ana" in text, text)
check("Luis (blocked on himself) is not in the lead's notice", "Luis" not in text, text)
NOTIFIED.clear()
checkin.notify_waiting(now=NOW + timedelta(hours=2), send_external=False)
check("the same list is not sent again within a day", NOTIFIED == [], NOTIFIED)
checkin.notify_waiting(now=NOW + timedelta(hours=25), send_external=False)
check("...but it is reminded the next day if still waiting", len(NOTIFIED) == 1)

print("── waiting on someone else (a client) ──")
UPDATES[0]["items"] = json.dumps([{"type": "blocker", "task_id": "t1", "owner": "Cliente BION", "text": "Faltan los creativos"}])
NOTIFIED.clear()
checkin.notify_waiting(now=NOW + timedelta(hours=26), send_external=False)
text = NOTIFIED[0][1] if NOTIFIED else ""
check("a change in the list is sent at once, naming who they wait on", "Ana espera de Cliente BION" in text, text)
check("Ana is still not asked", pending("ana@x.com") == [])

print("── in the lead's language ──")
users.language_of = lambda uid="", email="": "en" if uid == "own" else {"ana@x.com": "en"}.get(email, "")
UPDATES[0]["items"] = json.dumps([{"type": "blocker", "task_id": "t1", "owner": "Owner", "text": "Needs Meta access"}])
NOTIFIED.clear()
checkin.notify_waiting(now=NOW + timedelta(hours=27), send_external=False)
title, text = NOTIFIED[0] if NOTIFIED else ("", "")
check("English lead: English notice", "waiting on you" in text and "waiting on something" in title, (title, text))

print("── it stops by itself ──")
TASKS[0]["status"] = "done"
NOTIFIED.clear()
r = checkin.notify_waiting(now=NOW + timedelta(hours=60), send_external=False)
check("once the task is done, nobody is told anything", NOTIFIED == [] and r["projects"] == [], r)
TASKS[0]["status"] = "pending"
UPDATES[0]["items"] = json.dumps([{"type": "progress", "task_id": "t1", "text": "Ya me llegó el acceso"}])
check("once her next update no longer reports the blocker, Ana is asked as usual again", pending("ana@x.com") == ["Ana"])

print("── reported done, not approved yet: the next step is the owner's ──")
from datetime import date as _date  # noqa: E402
users.language_of = lambda uid="", email="": ""
UPDATES.clear()
TASKS[0]["status"] = "pending"
TASKS[0]["dueDate"] = (TODAY - timedelta(days=60)).isoformat()      # overdue since long ago -> urgent
STATE["ana@x.com"] = {"firstSeenAt": old, "lastReportAt": YESTERDAY_NOON.isoformat()}
check("overdue task, last report yesterday: Ana is asked (urgent)", pending("ana@x.com") == ["Ana"])
UPDATES.append({"itemId": "upd#9#ana@x.com", "personKey": "ana@x.com", "status": "closed", "closedAt": "9",
                "items": json.dumps([{"type": "done", "task_id": "t1", "owner": "Ana", "text": "Diseño terminado"}])})
check("once she reported it done, she is not asked about it again", pending("ana@x.com") == [], pending("ana@x.com"))
NOTIFIED.clear()
checkin.notify_waiting(now=NOW + timedelta(days=5), send_external=False)
text = NOTIFIED[0][1] if NOTIFIED else ""
check("the owner is told it is waiting for HIS approval", "Ana reportó como terminadas" in text and "Diseño" in text, text)
TASKS[0]["status"] = "done"                                          # the owner approved it
NOTIFIED.clear()
checkin.notify_waiting(now=NOW + timedelta(days=7), send_external=False)
check("approved (task closed): nothing more to tell", NOTIFIED == [])
TASKS[0]["status"] = "pending"

print("── a project-level blocker ('the project was cancelled') ──")
UPDATES[:] = [{"itemId": "upd#10#ana@x.com", "personKey": "ana@x.com", "status": "closed", "closedAt": "10",
               "items": json.dumps([{"type": "blocker", "task_id": "", "owner": "Owner", "text": "El proyecto fue cancelado"}])}]
person = {"name": "Ana", "email": "ana@x.com"}
resp, waits, _i = checkin.responsible_tasks("p1", "ana@x.com", person, [TASKS[0]])
check("every open task of hers waits on the owner; she is responsible for none", resp == [] and len(waits) == 1, (resp, waits))
check("...so she gets no window at all", pending("ana@x.com") == [])
NOTIFIED.clear()
checkin.notify_waiting(now=NOW + timedelta(days=9), send_external=False)
text = NOTIFIED[0][1] if NOTIFIED else ""
check("the owner is told, with the reason", "Ana espera de ti: El proyecto fue cancelado" in text, text)

print("── the calendar for relative dates ──")
cal = checkin.calendar_text(_date(2026, 10, 10))
check("Sat Oct 10: next Tuesday is Oct 13 (not 14)", cal.startswith("Sat 2026-10-10") and "Tue 2026-10-13" in cal
      and "Wed 2026-10-14" in cal, cal[:120])

print("\nOK" if ok else "\nFAILED")
sys.exit(0 if ok else 1)
