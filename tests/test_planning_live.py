"""planning_live: the planning graph writing to (fake) DynamoDB.

No network, no AWS, no real model. Run: python tests/test_planning_live.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import agent.tools as tools  # noqa: E402
from agent.planning.schemas import CoverOutput, FactsOutput, ReconcileOutput, TextOutput  # noqa: E402
from api.services import planning_live as PL  # noqa: E402

ok = True


def check(name, cond, detail=""):
    global ok
    print(("  PASS " if cond else "  FAIL ") + name + ("" if cond else f"  -> {detail}"))
    ok = ok and bool(cond)


class Table:
    def __init__(self, items=None):
        self.items = {k: dict(v) for k, v in (items or {}).items()}
        self.calls = []

    def put_item(self, Item):
        self.calls.append(("put", Item))
        self.items[Item.get("taskId") or Item.get("itemId") or Item.get("insightId") or Item.get("projectId")] = Item

    def update_item(self, Key, UpdateExpression, ExpressionAttributeValues, ExpressionAttributeNames=None):
        self.calls.append(("update", Key, UpdateExpression, ExpressionAttributeValues))
        k = Key.get("taskId") or Key.get("itemId") or Key.get("projectId")
        names = ExpressionAttributeNames or {}
        for part in UpdateExpression[4:].split(","):
            lhs, rhs = [x.strip() for x in part.split("=")]
            self.items.setdefault(k, {})[names.get(lhs, lhs)] = ExpressionAttributeValues[rhs]

    def delete_item(self, Key):
        self.calls.append(("delete", Key))
        self.items.pop(Key.get("taskId") or Key.get("insightId") or Key.get("itemId"), None)

    def get_item(self, Key):
        k = Key.get("taskId") or Key.get("itemId") or Key.get("projectId")
        return {"Item": self.items[k]} if k in self.items else {}

    def query(self, **_kw):
        return {"Items": list(self.items.values())}


EV = [{"source": "acta.txt", "locator": "1", "quote": "listo"}]
TASKS = [
    {"projectId": "p1", "taskId": "a", "text": "Instalar Tag Manager", "status": "pending", "createdBy": "IA"},
    {"projectId": "p1", "taskId": "b", "text": "Instalar Tag Manager en la web", "status": "pending", "createdBy": "IA"},
    {"projectId": "p1", "taskId": "c", "text": "Lanzar campañas", "status": "pending", "createdBy": "IA"},
]
SNAP = {"project": {"projectId": "p1", "userId": "own", "name": "Demo", "participants": [{"name": "Claudia"}]},
        "tasks": [dict(t) for t in TASKS], "insights": [],
        "attachments": [{"fileName": "acta.txt", "fullText": "Tag Manager listo. Faltan videos de YouTube.", "createdAt": "1"}]}


class Scripted:
    def __init__(self, answers):
        self.answers = answers

    def with_structured_output(self, schema):
        outer = self

        class R:
            def invoke(self, _m):
                return outer.answers.get(schema.__name__, schema())
        return R()


LLM = Scripted({
    "FactsOutput": FactsOutput(facts=[{"kind": "done", "text": "Tag Manager instalado", "evidence": EV},
                                      {"kind": "request_to_client", "text": "Subir videos a YouTube", "evidence": EV}]),
    "ReconcileOutput": ReconcileOutput(
        reviews=[{"task_id": "a", "verdict": "close"}, {"task_id": "b", "verdict": "delete"},
                 {"task_id": "c", "verdict": "update"}],
        operations=[{"op": "close", "task_id": "a", "reason": "hecho", "evidence": EV},
                    {"op": "delete", "task_id": "b", "duplicate_of": "c", "reason": "dup", "evidence": EV},
                    {"op": "update", "task_id": "c", "assigned_to": "Claudia", "reason": "r", "evidence": EV}]),
    "CoverOutput": CoverOutput(
        coverage=[{"fact": 1, "action": "covered", "task_id": "a"}, {"fact": 2, "action": "create", "task_id": "new-1"}],
        creates=[{"op": "create", "task_id": "new-1", "text": "Subir videos a YouTube", "depends_on": [],
                  "evidence": EV}],
        notes=[{"kind": "email", "text": "Se envió el acta", "task_id": "new-1", "evidence": EV}]),
    "TextOutput": TextOutput(text="ok"),
})

tasks_t, ins_t, proj_t, notified = Table({t["taskId"]: t for t in TASKS}), Table(), Table(), []
tools.tasks_table, tools.insights_table, tools.projects_table = tasks_t, ins_t, proj_t
PL.build_snapshot = lambda pid, name, text: {**SNAP, "tasks": [dict(t) for t in TASKS]}
PL._log_to_s3 = lambda pid, stamp, payload: "planning/p1/x.json"
PL._notify = lambda *a: notified.append(a)

import os  # noqa: E402
os.environ["PLANNING_UPLOADS"] = "apply"
res = PL.run_document_planning("p1", "own", "acta.txt", "Tag Manager listo.", llm=LLM)
check("run ok", res.get("ok"), res)
check("close written as status done", tasks_t.items["a"].get("status") == "done", tasks_t.items["a"])
check("duplicate deleted", "b" not in tasks_t.items)
check("update written", tasks_t.items["c"].get("assignedTo") == "Claudia", tasks_t.items["c"])
new = [t for k, t in tasks_t.items.items() if k not in ("a", "b", "c")]
check("create written with a real uuid", len(new) == 1 and len(new[0]["taskId"]) == 32 and new[0]["createdBy"] == "IA", new)
notes = [i for i in ins_t.items.values() if i.get("type") == "note"]
check("note written and linked to the real id of the new task",
      len(notes) == 1 and new and notes[0]["taskId"] == new[0]["taskId"], notes)
feed = sorted(i["type"] for i in ins_t.items.values() if i.get("source") == "planning" and i.get("type") != "note")
check("feed entries for created and closed tasks", feed == ["task_created", "work_done"], feed)
check("planVersion bumped on the project", proj_t.items.get("p1", {}).get("planVersion") == 1, proj_t.items)
check("owner notified", notified and "Plan actualizado" in notified[0][2], notified)

# The old writer must not write tasks while the engine is on.
os.environ["PLANNING_ENGINE"] = "off"
check("PLANNING_ENGINE=off disables it", not PL.enabled() and not PL.start_document_planning("p1", "o", "x", "t"))
os.environ["PLANNING_ENGINE"] = "on"
check("on by default", PL.enabled())


# ── planning chat: uploads propose, the leader approves, reverts ──
from api.services import planning_chat as PC  # noqa: E402
os.environ["PLANNING_UPLOADS"] = "proposal"
plan_t = Table()
PC._table = plan_t
PC._access = lambda uid, email, pid: dict(proj_t.items.get(pid, {}), projectId=pid, userId="own")
PC._store_source = lambda *a, **k: None
tasks_t.items = {t["taskId"]: dict(t) for t in TASKS}
ins_t.items, proj_t.items = {}, {"p1": {"projectId": "p1", "userId": "own", "name": "Demo", "planVersion": 3}}


def _snap(pid, new_sources=(), read_all=False):
    return {"project": dict(proj_t.items["p1"], participants=[{"name": "Claudia"}]),
            "tasks": [dict(t) for t in tasks_t.items.values()], "insights": [],
            "attachments": SNAP["attachments"] + [{"fileName": n, "fullText": t, "createdAt": "2"} for n, t in new_sources]}


PL.snapshot = _snap
PL.build_snapshot = lambda pid, name, text: _snap(pid, [(name, text)])
res = PL.run_document_planning("p1", "own", "acta.txt", "Tag Manager listo.", llm=LLM)
props = [i for i in plan_t.items.values() if i["itemId"].startswith("prop#")]
check("upload to an existing plan leaves a pending proposal, writes no task",
      res.get("applied") is False and len(props) == 1 and props[0]["status"] == "pending"
      and tasks_t.items["a"]["status"] == "pending", res)
check("...and tells the planning chat", any(i.get("role") == "assistant" for i in plan_t.items.values()))

out = PC.start_message("u1", "lead@x.com", "p1", "replanea con esto", context_text="Tag Manager listo; faltan videos",
                       run_async=False, llm=LLM)
job = PC._JOBS[out["jobId"]]
props = sorted((i for i in plan_t.items.values() if i["itemId"].startswith("prop#")), key=lambda i: i["createdAt"])
check("chat run finishes with a new pending proposal; the previous one is superseded",
      job["status"] == "done" and props[-1]["status"] == "pending" and props[0]["status"] == "superseded",
      [(p["status"], job["error"]) for p in props])
pid = props[-1]["proposalId"]
before_c = dict(tasks_t.items["c"])
r = PC.decide("u1", "lead@x.com", "p1", pid, "approve_all")
check("approve applies the proposal", r["status"] == "applied" and tasks_t.items["a"]["status"] == "done"
      and "b" not in tasks_t.items, r)
check("plan version moved", proj_t.items["p1"]["planVersion"] == 4, proj_t.items["p1"])
tools.tasks_table = tasks_t
st = PC.get_state("u1", "lead@x.com", "p1")
check("state lists messages, proposals and what can be reverted", st["revertible"] == pid and st["messages"], st["revertible"])
r = PC.revert("u1", "lead@x.com", "p1", pid)
new_ids = [k for k in tasks_t.items if k not in ("a", "b", "c")]
check("revert restores closed/deleted/updated tasks and removes created ones",
      tasks_t.items["a"]["status"] == "pending" and "b" in tasks_t.items and tasks_t.items["c"] == before_c
      and not new_ids, (tasks_t.items.get("a"), new_ids))
check("revert bumps plan version and marks the proposal", proj_t.items["p1"]["planVersion"] == 5
      and plan_t.items[f"prop#{pid}"]["status"] == "reverted")

PC.start_message("u1", "lead@x.com", "p1", "otra vez", context_text="x", run_async=False, llm=LLM)
pend = PC.pending_proposal("p1")
proj_t.items["p1"]["planVersion"] = 99            # someone changed the plan meanwhile
try:
    PC.decide("u1", "lead@x.com", "p1", pend["proposalId"], "approve_all")
    check("stale proposal refused", False)
except Exception as e:
    check("stale proposal refused", getattr(e, "status_code", 0) == 409 and
          plan_t.items[pend["itemId"]]["status"] == "stale", e)

print("\nOK" if ok else "\nFAILED")
sys.exit(0 if ok else 1)
