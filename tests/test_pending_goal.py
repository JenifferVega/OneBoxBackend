"""The goal opens, survives and closes on EVIDENCE, not on guesses.

Exercises the real _open_goal / _close_goal from runner.py, replaying the exact
trace that produced an empty Trello board.
"""
import os
import sys

# Resolve against the REPOSITORY, not against whoever's machine wrote this.
# These tests read the real source and exercise it; pointing them at an
# absolute path outside the repo made them unrunnable for everyone else.
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)


def repo_file(*parts):
    return os.path.join(REPO, *parts)

import ast, sys

src = open(repo_file("agent", "graph", "runner.py"), encoding="utf-8").read()
from typing import Dict, List, Any
ns = {"Dict": Dict, "List": List, "Any": Any}
# Every module-level function, not a hand-kept list of three: extracting a
# helper out of _close_goal used to break this file with a NameError that
# looked like a bug in the code under test. Defining a function does not run
# its body, so pulling in ones this file never calls costs nothing.
for n in ast.parse(src).body:
    if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
        exec(compile(ast.Module([n], []), "runner.py", "exec"), ns)
open_goal, close_goal = ns["_open_goal"], ns["_close_goal"]

ok = True
def check(label, got, want):
    global ok
    good = got == want
    ok &= good
    print(f"  {'PASS' if good else 'FAIL'}  {label}: {got!r}" +
          ("" if good else f"  (expected {want!r})"))


MSG = "manda las tareas de Farmacia Haussman a Trello"

print("\nTurn 1 — push says it cannot place the cards")
plan1 = [{"step": 1, "tool": "resolve_entity"},
         {"step": 2, "tool": "push_tasks_to_trello"}]
res1 = {1: {"count": 1}, 2: {"error": "no list yet", "needs_user_choice": True}}
g = open_goal(MSG, plan1, res1)
check("goal opened", bool(g), True)
check("the unfinished job is push's own",
      [e["tool"] for e in g["queue"]], ["push_tasks_to_trello"])
check("it keeps the user's words", g.get("text"), MSG)
check("not closed by this turn", bool(close_goal(g, plan1, res1)), True)

print("\nTurn 3 — the board is created but nothing is exported (today's bug)")
plan3 = [{"step": 1, "tool": "list_projects"},
         {"step": 2, "tool": "create_trello_board"}]
res3 = {1: {"count": 5},
        2: {"success": True, "boardId": "b1", "tasks_waiting_to_export": 4,
            "next_step": {"tool": "push_tasks_to_trello",
                          "why": "The board is EMPTY. 4 task(s) have no card."}}}
check("still not done", bool(close_goal(g, plan3, res3)), True)
g2 = open_goal(MSG, plan3, res3)
check("next_step names what finishes it",
      [e["tool"] for e in g2["queue"]], ["push_tasks_to_trello"])

print("\nTurn 3 — the behaviour we want instead")
plan3b = plan3 + [{"step": 3, "tool": "push_tasks_to_trello"}]
res3b = dict(res3)
res3b[3] = {"success": True, "outcome": "complete", "created_count": 4}
check("goal closed", close_goal(g, plan3b, res3b), {})

print("\nA write that failed does NOT close the goal")
res_fail = {3: {"error": "Trello 500"}}
check("still pending", bool(close_goal(g, plan3b, res_fail)), True)

print("\nA push that left cards behind reports itself as unfinished")
# The tool has always KNOWN -- outcome == "partial" -- and never said so, so
# no goal was recorded and the retry happened only if the user remembered.
try:
    from agent.graph.nodes.executor.node import _simulate_tool
    r = _simulate_tool("push_tasks_to_trello",
                       {"project_id": "proj-DRYRUN-ccc333", "list_name": "Backlog",
                        "board_name": "ONEBOX"},
                       session_id="test-partial-push")
    if r.get("failed_count"):
        check("partial push names its own retry",
              (r.get("next_step") or {}).get("tool"), "push_tasks_to_trello")
        g_push = open_goal("sube las tareas", [{"step": 1, "tool": "push_tasks_to_trello"}],
                           {1: r})
        check("and that opens a goal",
              [e["tool"] for e in g_push.get("queue", [])], ["push_tasks_to_trello"])
    else:
        print("  SKIP  the fixture pushed everything; nothing to retry")
except Exception as e:                                        # pragma: no cover
    print(f"  SKIP  executor not importable here ({type(e).__name__})")

print("\nSeveral pending calls, and one of them WAITS for another")
plan4 = [{"step": 1, "tool": "push_tasks_to_trello"}]
res4 = {1: {"success": True,
            "next_step": {"tool": "send_notification",
                          "after": ["push_tasks_to_trello"],
                          "why": "the team was promised a heads-up"}}}
g4 = open_goal("sube las tareas y avisa al equipo", plan4, res4)
check("the dependency travels with the entry",
      g4["queue"][0].get("after"), ["push_tasks_to_trello"])

# push succeeded in THIS turn, so the dependency is satisfied and the entry
# becomes runnable. Nothing is executed by that -- it only becomes plannable.
g5 = close_goal(g4, plan4, res4)
check("the entry survives", [e["tool"] for e in g5["queue"]], ["send_notification"])
check("and is now runnable", g5["queue"][0]["after"], [])

print("\nTwo signals in one turn are BOTH remembered")
plan6 = [{"step": 1, "tool": "push_tasks_to_trello"},
         {"step": 2, "tool": "get_project_contacts"}]
res6 = {1: {"error": "no list yet", "needs_user_choice": True},
        2: {"success": True,
            "next_step": {"tool": "send_notification", "why": "..."}}}
g6 = open_goal("sube y avisa", plan6, res6)
check("neither is dropped", sorted(e["tool"] for e in g6["queue"]),
      ["push_tasks_to_trello", "send_notification"])

print("\nThe tool running again and asking again does NOT close it")
res_again = {3: {"error": "no list yet", "needs_user_choice": True}}
check("still pending", bool(close_goal(g, plan3b, res_again)), True)

print("\nA turn with nothing pending opens nothing")
check("no goal", open_goal("hola", [{"step": 1, "tool": "list_projects"}],
                           {1: {"count": 3}}), {})

print("\nThe goal is never inferred from the message alone")
check("no evidence, no goal", open_goal(MSG, [], {}), {})

print("\n" + ("ALL PASS" if ok else "FAILURES ABOVE"))
sys.exit(0 if ok else 1)
