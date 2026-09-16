"""The goal opens, survives and closes on EVIDENCE, not on guesses.

Exercises the real _open_goal / _close_goal from runner.py, replaying the exact
trace that produced an empty Trello board.
"""
import ast, sys

src = open("/home/claude/v11/runner.py").read()
from typing import Dict, List, Any
ns = {"Dict": Dict, "List": List, "Any": Any}
for n in ast.parse(src).body:
    if isinstance(n, ast.FunctionDef) and n.name in (
            "_tool_of_step", "_open_goal", "_close_goal"):
        exec(compile(ast.Module([n], []), "x", "exec"), ns)
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
check("the unfinished job is push's own", g.get("done_tool"), "push_tasks_to_trello")
check("it keeps the user's words", g.get("text"), MSG)
check("not closed by this turn", close_goal(g, plan1, res1), False)

print("\nTurn 3 — the board is created but nothing is exported (today's bug)")
plan3 = [{"step": 1, "tool": "list_projects"},
         {"step": 2, "tool": "create_trello_board"}]
res3 = {1: {"count": 5},
        2: {"success": True, "boardId": "b1", "tasks_waiting_to_export": 4,
            "next_step": {"tool": "push_tasks_to_trello",
                          "why": "The board is EMPTY. 4 task(s) have no card."}}}
check("still not done", close_goal(g, plan3, res3), False)
g2 = open_goal(MSG, plan3, res3)
check("next_step names what finishes it", g2.get("done_tool"), "push_tasks_to_trello")

print("\nTurn 3 — the behaviour we want instead")
plan3b = plan3 + [{"step": 3, "tool": "push_tasks_to_trello"}]
res3b = dict(res3)
res3b[3] = {"success": True, "outcome": "complete", "created_count": 4}
check("goal closed", close_goal(g, plan3b, res3b), True)

print("\nA write that failed does NOT close the goal")
res_fail = {3: {"error": "Trello 500"}}
check("still pending", close_goal(g, plan3b, res_fail), False)

print("\nThe tool running again and asking again does NOT close it")
res_again = {3: {"error": "no list yet", "needs_user_choice": True}}
check("still pending", close_goal(g, plan3b, res_again), False)

print("\nA turn with nothing pending opens nothing")
check("no goal", open_goal("hola", [{"step": 1, "tool": "list_projects"}],
                           {1: {"count": 3}}), {})

print("\nThe goal is never inferred from the message alone")
check("no evidence, no goal", open_goal(MSG, [], {}), {})

print("\n" + ("ALL PASS" if ok else "FAILURES ABOVE"))
sys.exit(0 if ok else 1)
