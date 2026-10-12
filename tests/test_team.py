"""Members page (api/services/team.py): one row per person and project, with
the same follow-up rules the system applies. No AWS, no model.
Run: python tests/test_team.py
"""
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import agent.tools as tools  # noqa: E402
import api.deps as deps  # noqa: E402
from api.services import checkin, planning_chat, team  # noqa: E402

ok = True


def check(name, cond, detail=""):
    global ok
    print(("  PASS " if cond else "  FAIL ") + name + ("" if cond else f"  -> {detail}"))
    ok = ok and bool(cond)


NOW = datetime.utcnow()
TODAY = NOW.date()
old = (NOW - timedelta(days=10)).isoformat()
recent = (NOW - timedelta(hours=1)).isoformat()
PROJ = {"projectId": "p1", "userId": "own", "name": "Demo", "status": "active",
        "participants": [{"name": "Owner", "email": "own@x.com"}, {"name": "Ana", "email": "ana@x.com"},
                         {"name": "Luis", "email": "luis@x.com"}, {"name": "Rafa"}, {"name": "Eva", "email": "eva@x.com"},
                         {"name": "Paz", "email": "paz@x.com"}, {"name": "Tom", "email": "tom@x.com"}],
        "followUp": {"people": {"paz@x.com": {"until": "", "by": "own", "at": recent}}}}
TASKS = [
    {"taskId": "t1", "text": "Diseño", "status": "pending", "assignedTo": "Ana", "dueDate": (TODAY - timedelta(days=5)).isoformat()},
    {"taskId": "t2", "text": "Código", "status": "pending", "assignedTo": "Luis"},
    {"taskId": "t3", "text": "Datos", "status": "pending", "assignedTo": "Rafa"},
    {"taskId": "t4", "text": "Pruebas", "status": "pending", "assignedTo": "Eva"},
    {"taskId": "t5", "text": "Docs", "status": "pending", "assignedTo": "Paz"},
    {"taskId": "t6", "text": "Plan", "status": "pending", "assignedTo": "Owner"},
]
ITEMS = [
    {"itemId": "checkin#ana@x.com", "firstSeenAt": old, "lastReportAt": old},
    {"itemId": "checkin#luis@x.com", "firstSeenAt": old, "lastReportAt": recent},
    {"itemId": "checkin#eva@x.com", "firstSeenAt": old, "lastReportAt": recent},
    {"itemId": "upd#1#eva@x.com", "personKey": "eva@x.com", "status": "closed", "closedAt": recent, "sessionId": "s1",
     "messages": [{"role": "assistant", "content": "¿Cómo vas?"}, {"role": "member", "content": "Pruebas terminadas"}],
     "items": json.dumps([{"type": "done", "task_id": "t4", "text": "Pruebas terminadas"}])},
]


class Projects:
    def get_item(self, Key):
        return {"Item": dict(PROJ)} if Key["projectId"] == "p1" else {}


class Tasks:
    def query(self, **_kw):
        return {"Items": [dict(t) for t in TASKS]}


tools.projects_table = Projects()
tools.tasks_table = Tasks()
deps.scan_all_pages = lambda table: [dict(PROJ), {"projectId": "p2", "userId": "someone-else", "name": "Not mine"}]
checkin._project_tasks = lambda pid: [dict(t) for t in TASKS]
planning_chat._items = lambda pid: [dict(i) for i in ITEMS]

print("── status: one row per person, the state the system acts on ──")
r = team.status("own", "own@x.com")
rows = {row["name"]: row for row in r["rows"]}
check("only projects I own", [p["name"] for p in r["projects"]] == ["Demo"], r["projects"])
check("Ana: overdue task, no update for 10 days -> late", rows["Ana"]["state"] == "late" and rows["Ana"]["overdue"] == 1, rows["Ana"])
check("Luis: reported an hour ago -> ok", rows["Luis"]["state"] == "ok", rows["Luis"])
check("Rafa: no email -> no account", rows["Rafa"]["state"] == "no_account", rows["Rafa"])
check("Eva: reported her task done, waiting for MY approval -> waiting_you",
      rows["Eva"]["state"] == "waiting_you" and rows["Eva"]["waitingOnYou"][0]["kind"] == "approval", rows["Eva"])
check("Eva: her update is counted", rows["Eva"]["updates"] == 1 and rows["Eva"]["lastUpdateAt"] == recent)
check("Paz: follow-up paused", rows["Paz"]["state"] == "paused", rows["Paz"])
check("Tom: no tasks", rows["Tom"]["state"] == "no_tasks", rows["Tom"])
check("the owner is marked as such", rows["Owner"]["state"] == "owner" and rows["Owner"]["isOwner"])

print("── updates: the person's recent chats ──")
u = team.updates("own", "p1", "eva@x.com")
check("returns her update with the conversation and items (task text attached)",
      len(u["updates"]) == 1 and u["updates"][0]["messages"][1]["content"] == "Pruebas terminadas"
      and u["updates"][0]["items"][0]["task_text"] == "Pruebas", u)
try:
    team.updates("intruder", "p1", "eva@x.com")
    check("only the owner can read them", False)
except Exception as e:
    check("only the owner can read them", getattr(e, "status_code", 0) == 403, e)

print("\nOK" if ok else "\nFAILED")
sys.exit(0 if ok else 1)
