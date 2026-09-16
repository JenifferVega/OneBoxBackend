"""Does a replan repeat a write? Runs the REAL executor_node twice, the way
the graph does when the validator says "replan"."""
import agent.tools as T
from agent.graph.nodes.executor.node import executor_node

PLAN = [
    {"step": 1, "tool": "list_projects", "params": {}},
    {"step": 2, "tool": "create_trello_board",
     "params": {"name": "Onebox2026",
                "project_id": {"from_step": 1,
                               "match": {"key": "name", "value": "Onebox2026"},
                               "extract": "projectId"},
                "lists": ["Pendiente", "En curso"]}},
    {"step": 3, "tool": "push_tasks_to_trello",
     "params": {"project_id": {"from_step": 1,
                               "match": {"key": "name", "value": "Onebox2026"},
                               "extract": "projectId"}}},
]

PROJECTS = {"count": 1, "projects": [{"projectId": "p-1", "name": "Onebox2026"}]}


def run(plan, behaviour, iterations=2, state=None):
    T.CALLS.clear()
    T.set_behaviour(behaviour)
    st = dict(state or {})
    st["plan"] = plan
    for _ in range(iterations):
        upd = executor_node(st)
        st.update(upd)
    return st


def counts():
    from collections import Counter
    return Counter(t for t, _ in T.CALLS)


def check(label, got, want):
    ok = got == want
    print(f"  {'PASS' if ok else 'FAIL'}  {label}: {got}" + ("" if ok else f"  (expected {want})"))
    return ok


ok = True
print("\n1. Replan with a partially-failing last step")
push_n = {"n": 0}
def push(p):
    push_n["n"] += 1
    if push_n["n"] == 1:
        return {"success": True, "outcome": "partial", "created_count": 2, "failed_count": 1}
    return {"success": True, "outcome": "complete", "created_count": 1, "failed_count": 0}
run(PLAN, {"list_projects": lambda p: PROJECTS,
           "create_trello_board": lambda p: {"success": True, "boardId": "b1"},
           "push_tasks_to_trello": push})
c = counts()
ok &= check("create_trello_board calls", c["create_trello_board"], 1)
ok &= check("push_tasks_to_trello calls", c["push_tasks_to_trello"], 1)
ok &= check("list_projects calls (a read, may repeat)", c["list_projects"], 2)

print("\n2. A write that FAILED outright must be retried")
run(PLAN, {"list_projects": lambda p: PROJECTS,
           "create_trello_board": lambda p: {"error": "Trello 500"},
           "push_tasks_to_trello": lambda p: {"success": True}})
ok &= check("create_trello_board calls", counts()["create_trello_board"], 2)

print("\n3. success:False is a failure, not a write to protect")
run(PLAN, {"list_projects": lambda p: PROJECTS,
           "create_trello_board": lambda p: {"success": False},
           "push_tasks_to_trello": lambda p: {"success": True}})
ok &= check("create_trello_board calls", counts()["create_trello_board"], 2)

print("\n4. Different params = a different write, must run")
plan2 = [PLAN[1], {"step": 2, "tool": "create_trello_board",
                   "params": {"name": "Marketing Q3", "project_id": "p-2"}}]
run(plan2, {"create_trello_board": lambda p: {"success": True}}, iterations=1)
ok &= check("two distinct boards", counts()["create_trello_board"], 2)

print("\n5. send_notification over a foreach must not message everyone twice")
plan3 = [{"step": 1, "tool": "get_project_contacts", "params": {}},
         {"step": 2, "tool": "send_notification",
          "params": {"recipient": {"from_step": 1, "foreach": "contacts", "extract": "phone"},
                     "message": "hola"}}]
run(plan3, {"get_project_contacts": lambda p: {"contacts": [
                {"name": "Ana", "phone": "+1"}, {"name": "Carlos", "phone": "+2"}]},
            "send_notification": lambda p: {"success": True}})
ok &= check("send_notification calls (2 contacts x 1 send)", counts()["send_notification"], 2)

print("\n6. A read is never frozen by the ledger")
plan4 = [{"step": 1, "tool": "list_tasks", "params": {"project_id": "p-1"}}]
run(plan4, {"list_tasks": lambda p: {"count": 0, "tasks": []}}, iterations=3)
ok &= check("list_tasks calls", counts()["list_tasks"], 3)

print("\n" + ("ALL PASS" if ok else "FAILURES ABOVE"))
raise SystemExit(0 if ok else 1)
