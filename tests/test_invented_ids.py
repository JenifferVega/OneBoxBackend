# -*- coding: utf-8 -*-
"""A write may not act on an id that no read in this turn produced.

2026-09-23, reproduced with two different providers on the planner: the user
asked to mark the task "Backlog fichas" as done. No such task exists. The plan
went straight to update_task with a LITERAL task_id -- no list_tasks anywhere
in it -- and closed "Definir la arquitectura y el stack tecnologico" instead.
Every existing check passed: the id was well formed, the tool returned
success, and the narrator reported the task as done.

The from_step path already handles this: a `match` that finds nothing resolves
to None and is stopped. The hole was the literal id, which looks like data the
planner was given rather than data it invented.

No AWS, no LLM.
"""
import ast
import json
import sys

ok = True


def check(c, l):
    global ok
    ok &= bool(c)
    print(f"  {'OK ' if c else 'FAILS'} {l}")


_src = open("agent/graph/nodes/executor/node.py", encoding="utf-8").read()
_ns = {"json": json}
for _n in ast.parse(_src).body:
    if isinstance(_n, ast.FunctionDef) and _n.name == "_invented_id":
        exec(compile(ast.Module([_n], []), "<x>", "exec"), _ns)
    if isinstance(_n, ast.Assign) and getattr(_n.targets[0], "id", "") == "_ID_MUST_COME_FROM_A_READ":
        exec(compile(ast.Module([_n], []), "<x>", "exec"), _ns)
_invented_id = _ns["_invented_id"]

# What a list_tasks step actually returns in this turn.
READ_RESULT = {1: {"count": 2, "tasks": [
    {"taskId": "task-DRYRUN-a3", "text": "Definir la arquitectura", "status": "blocked"},
    {"taskId": "task-DRYRUN-a5", "text": "Rehacer las fichas", "status": "pending"}]}}

print("=== the exact 2026-09-23 case ===")
err = _invented_id("update_task", {"task_id": "task-DRYRUN-a3", "status": "done"}, {},
                   raw_params={"task_id": "task-DRYRUN-a3"}, state={})
check(err is not None, "a literal id with NO read in this turn is blocked")
check("written as a literal id" in (err or {}).get("error", ""),
      "the error says why: a literal cannot be checked against the request")
check("do NOT substitute a similar record" in (err or {}).get("hint", ""),
      "and forbids the actual mistake: swapping in a similar record")

print("\n=== the SECOND failure of that same turn ===")
# After the first block the planner replanned, ran list_tasks, and picked a
# different row from the five it had just read. Provenance was now perfect.
err2 = _invented_id("update_task", {"task_id": "task-DRYRUN-a3", "status": "done"},
                    READ_RESULT, raw_params={"task_id": "task-DRYRUN-a3"}, state={})
check(err2 is not None,
      "an id READ this turn but CHOSEN by the planner is still blocked")
check("PICKED it rather than matching" in (err2 or {}).get("error", ""),
      "and the error distinguishes that case from an invented id")

print("\n=== what IS allowed ===")
chained = {"task_id": {"from_step": 2, "match": {"key": "text", "value": "Rehacer"},
                       "extract": "taskId"}}
check(_invented_id("update_task", {"task_id": "task-DRYRUN-a5"}, READ_RESULT,
                   raw_params=chained, state={}) is None,
      "an id selected by a match passes: a wrong name would have failed instead")
check(_invented_id("update_task", {"task_id": "task-DRYRUN-a5"}, READ_RESULT,
                   raw_params={"task_id": '{"from_step": 2, "extract": "taskId"}'},
                   state={}) is None,
      "including the string-serialised reference Gemini emits")
check(_invented_id("update_task", {"task_id": "task-DRYRUN-a3"}, {},
                   raw_params={"task_id": "task-DRYRUN-a3"},
                   state={"user_message": "marca task-DRYRUN-a3 como hecha"}) is None,
      "a literal the USER typed is their own instruction, not a guess")
check(_invented_id("update_task", {"task_id": None}, {}, {}, {}) is None,
      "an unresolved id is left to validate_tool_params, which owns that case")
check(_invented_id("update_task", {"status": "done"}, {}, {}, {}) is None,
      "an absent id is not this check's business")

print("\n=== scope ===")
check(_invented_id("create_task", {"project_id": "proj-invented"}, {},
                   {"project_id": "proj-invented"}, {}) is None,
      "create_task is NOT covered: it creates, it does not overwrite a record")
check(_invented_id("delete_task", {"task_id": "task-xyz"}, {},
                   {"task_id": "task-xyz"}, {}) is not None,
      "delete_task is covered -- the most destructive of the three")
check(_invented_id("update_project", {"project_id": "proj-xyz"}, {},
                   {"project_id": "proj-xyz"}, {}) is not None,
      "update_project is covered")
check(_invented_id("list_tasks", {"project_id": "proj-xyz"}, {},
                   {"project_id": "proj-xyz"}, {}) is None,
      "reads are never blocked")

print("\n=== provenance is still reported accurately ===")
deep = {2: {"board": {"cards": [{"taskId": "task-DRYRUN-a9"}]}}}
err3 = _invented_id("update_task", {"task_id": "task-DRYRUN-a9"}, deep,
                    {"task_id": "task-DRYRUN-a9"}, {})
check(err3 is not None and "PICKED it" in err3["error"],
      "a literal is blocked even when the id exists deep inside a result")

print("\n" + ("ALL OK" if ok else "FAILURES"))
sys.exit(0 if ok else 1)
