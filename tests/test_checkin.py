"""Check-in of pending work, with fake tables. Run: python tests/test_checkin.py"""
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import agent.tools as tools  # noqa: E402
import api.deps as deps  # noqa: E402
import api.services.access as access  # noqa: E402
import api.services.planning_live as PL  # noqa: E402
from api.services import checkin as C  # noqa: E402
from api.services import planning_chat as PC  # noqa: E402

ok = True


def check(name, cond, detail=""):
    global ok
    print(("  PASS " if cond else "  FAIL ") + name + ("" if cond else f"  -> {detail}"))
    ok = ok and bool(cond)


class Table:
    def __init__(self, items=None):
        self.items = dict(items or {})

    def _k(self, d):
        return d.get("itemId") or d.get("taskId") or d.get("projectId")

    def put_item(self, Item):
        self.items[self._k(Item)] = Item

    def get_item(self, Key):
        k = self._k(Key)
        return {"Item": self.items[k]} if k in self.items else {}

    def query(self, **_kw):
        return {"Items": list(self.items.values())}

    def update_item(self, Key, UpdateExpression, ExpressionAttributeValues, ExpressionAttributeNames=None):
        names = ExpressionAttributeNames or {}
        it = self.items.setdefault(self._k(Key), dict(Key))
        for part in UpdateExpression[4:].split(","):
            lhs, rhs = [x.strip() for x in part.split("=")]
            it[names.get(lhs, lhs)] = ExpressionAttributeValues[rhs]


TODAY = datetime.utcnow().date()
PROJ = {"projectId": "p1", "userId": "owner", "name": "Demo", "planVersion": 2,
        "participants": [{"name": "Claudia", "email": "c@x.com"}, {"nombre": "Pedro"}]}
TASKS = [
    {"projectId": "p1", "taskId": "a", "text": "Landing", "status": "pending", "assignedTo": "Claudia"},
    {"projectId": "p1", "taskId": "b", "text": "Banner", "status": "in_progress", "assignedTo": "c@x.com",
     "dueDate": (TODAY + timedelta(days=1)).isoformat()},
    {"projectId": "p1", "taskId": "c", "text": "Videos", "status": "pending", "assignedTo": "Pedro"},
    {"projectId": "p1", "taskId": "d", "text": "Hecha", "status": "done", "assignedTo": "Claudia"},
]
projects, plan, notified = Table({"p1": PROJ}), Table(), []
tools.projects_table = projects
PC._table = plan
C._project_tasks = lambda pid: [dict(t) for t in TASKS]
access.accessible_project_ids = lambda uid, email: {"p1"}
access.has_project_access = lambda uid, email, pid: (True, False, PROJ)
deps.scan_all_pages = lambda table, **kw: [PROJ]
PL._notify = lambda owner, proj, title, msg, kind: notified.append((owner, title, kind, msg))
C._notify_owner_outside = lambda *a: notified.append(("outside", a[1], "outside", ""))

# ── who owes what ──
asg = {p["name"]: [t["taskId"] for t in ts] for p, ts in C.assignments(PROJ, TASKS)}
check("assignments by name, email and Spanish keys; done tasks excluded", asg == {"Claudia": ["a", "b"], "Pedro": ["c"]}, asg)

pend = C.pending_for_user("u-claudia", "c@x.com")
check("first login with open tasks: report due", len(pend["projects"]) == 1 and
      [t["taskId"] for t in pend["projects"][0]["tasks"]] == ["b", "a"], pend)

# ── the report ──
def err(fn):
    try:
        fn()
        return 0
    except Exception as e:
        return getattr(e, "status_code", -1)


check("every task must be answered", err(lambda: C.submit("u", "c@x.com", "p1", [{"taskId": "a", "status": "done"}])) == 400)
check("blocked needs a reason", err(lambda: C.submit("u", "c@x.com", "p1", [
    {"taskId": "a", "status": "blocked"}, {"taskId": "b", "status": "done"}])) == 400)
r = C.submit("u-claudia", "c@x.com", "p1", [
    {"taskId": "a", "status": "blocked", "blockedReason": "falta acceso", "comment": "pedí acceso el lunes"},
    {"taskId": "b", "status": "in_progress", "newDueDate": (TODAY + timedelta(days=5)).isoformat()}],
    comment="todo bien")
prop = PC._plain(PC._loads(PC.pending_proposal("p1")["proposal"]))
check("report becomes a pending proposal (nothing applied)",
      r["proposalId"] and sorted(o["op"] for o in prop["operations"]) == ["block", "update"], prop["operations"])
check("comments become notes", len(prop["notes"]) == 2)
check("owner notified in the app", any(k == "checkin_report" for _o, _t, k, _m in notified), notified)
check("after reporting, no modal (urgent task already reported today)",
      C.pending_for_user("u-claudia", "c@x.com")["projects"] == [])

# ── another person's report merges, it does not cancel the first ──
TASKS[2]["assignedTo"] = "c@x.com"          # pretend Claudia also owns c to reuse the session
r2 = C.submit("u-claudia", "c@x.com", "p1", [{"taskId": "a", "status": "pending"}, {"taskId": "b", "status": "in_progress"},
                                             {"taskId": "c", "status": "done", "comment": "subidos"}])
merged = PC._plain(PC._loads(PC.pending_proposal("p1")["proposal"]))
check("second report merged into the same pending proposal",
      r2["proposalId"] == r["proposalId"] and len(merged["operations"]) > len(prop["operations"]), merged["counts"])
TASKS[2]["assignedTo"] = "Pedro"

# ── the owner hears about who goes quiet ──
notified.clear()
now = datetime.utcnow()
C.notify_overdue(now)                      # first sight of Pedro: starts his clock
check("first sight starts the clock, no alert yet", not any(k == "checkin_overdue" for _o, _t, k, _m in notified))
C.notify_overdue(now + timedelta(days=C.CHECKIN_GRACE_DAYS, hours=1))
alerts = [n for n in notified if n[2] == "checkin_overdue"]
check("owner alerted after the grace day, Pedro listed as without account",
      len(alerts) == 1 and "Pedro" in alerts[0][3] and "sin cuenta" in alerts[0][3], alerts)
C.notify_overdue(now + timedelta(days=C.CHECKIN_GRACE_DAYS, hours=2))
check("only once per due period", len([n for n in notified if n[2] == "checkin_overdue"]) == 1)

print("\nOK" if ok else "\nFAILED")
sys.exit(0 if ok else 1)
