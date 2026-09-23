# -*- coding: utf-8 -*-
"""A plan that names a parameter the tool does not accept must not kill the step.

Reproduces the 2026-09-21 production ticket. The planner sent
push_tasks_to_trello(project_id, project_name, task_id, text, type). Four of
those are real parameters of OTHER tools, offered to the decoder by the union
schema. `tool_func(**params)` raised TypeError, and the validator's error
message blamed the from_step chaining -- which had resolved project_id
correctly -- so the planner abandoned it and invented literal ids.

No AWS, no LLM: the tools are stubbed with the real signatures.
"""
import ast
import sys
import types

ok = True


def check(c, l):
    global ok
    ok &= bool(c)
    print(f"  {'OK ' if c else 'FAILS'} {l}")


# ── Stub agent.tools with the REAL signatures of the tools involved ────────
def push_tasks_to_trello(project_id: str, list_id: str = "", list_name: str = "",
                         board_name: str = "", only_pending: bool = True):
    return {"success": True}


def update_task(task_id: str, text: str = None, status: str = None):
    return {"success": True}


def takes_anything(**kwargs):
    return {"success": True}


_tools = types.ModuleType("agent.tools")
_tools.TOOL_MAP = {"push_tasks_to_trello": push_tasks_to_trello,
                   "update_task": update_task,
                   "takes_anything": takes_anything}
_tools.execute_tool = lambda n, p: _tools.TOOL_MAP[n](**p)
sys.modules["agent.tools"] = _tools

# ── Load _accepted_params out of the executor without its dependencies ─────
_src = open("agent/graph/nodes/executor/node.py", encoding="utf-8").read()
_mod = ast.parse(_src)
_ns = {"__name__": "_exec_fragment"}
for _n in _mod.body:
    if isinstance(_n, ast.FunctionDef) and _n.name == "_accepted_params":
        exec(compile(ast.Module([_n], []), "<exec>", "exec"), _ns)
    if isinstance(_n, ast.AnnAssign) and getattr(_n.target, "id", "") == "_SIGNATURE_CACHE":
        exec(compile(ast.Module([_n], []), "<exec>", "exec"), _ns)
_accepted_params = _ns["_accepted_params"]

print("=== the exact call from the 2026-09-21 ticket ===")
PLAN_PARAMS = {
    "project_id": "proj-b8f8cea7",        # correctly resolved by from_step
    "project_name": "Farmacia Haussman",  # belongs to send_email & co.
    "task_id": None,                      # belongs to update_task
    "text": "Enviar las tareas pendientes...",   # belongs to create_task
    "type": "task_created",               # belongs to create_project
}
kept, dropped = _accepted_params("push_tasks_to_trello", PLAN_PARAMS)
check(kept == {"project_id": "proj-b8f8cea7"},
      f"only what the signature accepts survives: {kept}")
check(sorted(dropped) == ["project_name", "task_id", "text", "type"],
      f"the other four are reported, not silently gone: {sorted(dropped)}")

print("\n=== and the call now works instead of raising TypeError ===")
try:
    _tools.execute_tool("push_tasks_to_trello", PLAN_PARAMS)
    check(False, "unfiltered params still raise (this is the production bug)")
except TypeError as e:
    check("unexpected keyword argument" in str(e),
          f"unfiltered: TypeError -- {str(e)[:60]}")
try:
    r = _tools.execute_tool("push_tasks_to_trello", kept)
    check(r.get("success") is True, "filtered: the step executes")
except TypeError as e:
    check(False, f"filtered still raised: {e}")

print("\n=== a tool taking **kwargs is left alone ===")
kept2, dropped2 = _accepted_params("takes_anything", {"whatever": 1})
check(kept2 == {"whatever": 1} and dropped2 == [], "nothing dropped")

print("\n=== an unknown tool is not second-guessed here ===")
kept3, dropped3 = _accepted_params("no_such_tool", {"a": 1})
check(kept3 == {"a": 1} and dropped3 == [], "left to execute_tool to reject")

print("\n=== the misleading diagnosis is gone ===")
# validators.py imports nothing heavy, so it loads directly.
sys.path.insert(0, ".")
import importlib.util
spec = importlib.util.spec_from_file_location(
    "_vals", "agent/graph/nodes/executor/validators.py")
V = importlib.util.module_from_spec(spec)
spec.loader.exec_module(V)

err = V.validate_tool_params("push_tasks_to_trello", 2, {"task_id": None})
check(err is None,
      "task_id=None on a tool with no task_id no longer blames from_step")

err2 = V.validate_tool_params("update_task", 2, {"task_id": None})
check(err2 is not None and "from_step" in err2.get("error", ""),
      "but on update_task, which DOES take task_id, it still fires")

err3 = V.validate_tool_params("push_tasks_to_trello", 2,
                              {"project_id": {"from_step": 1}})
check(err3 is not None, "a real bad project_id is still caught")

print("\n" + ("ALL OK" if ok else "FAILURES"))
sys.exit(0 if ok else 1)
