# -*- coding: utf-8 -*-
"""'Which task is missing from the Gantt?' is a data question, not a UI one.

2026-09-22, a client: "the dashboard says 8 blocked and the gantt shows 5".
Asked which one was missing, the agent answered "I have no way to access your
Gantt chart" -- while holding every field needed to compute it. The rule lived
only in a React component, so the backend could not see it and no model could
guess it.

Also pins the matching of names the way people say them, which is how the same
turn burned an iteration: "el proyecto del sistema de gestión de inventarios"
matched nothing because the comparison demanded exact equality.

No AWS, no LLM.
"""
import ast
import sys

ok = True


def check(c, l):
    global ok
    ok &= bool(c)
    print(f"  {'OK ' if c else 'FAILS'} {l}")


def load(path, *names):
    ns = {}
    for n in ast.parse(open(path, encoding="utf-8").read()).body:
        if isinstance(n, ast.FunctionDef) and n.name in names:
            exec(compile(ast.Module([n], []), "<x>", "exec"), ns)
    return ns


_t = load("agent/tools/tasks.py", "_timeline_issue")
issue = _t["_timeline_issue"]

print("=== a task is on the timeline only with BOTH dates, in order ===")
check(issue({"startDate": "2026-01-01", "dueDate": "2026-02-01"}) == "",
      "both dates, in order -> drawn")
check(issue({"startDate": "2026-01-01", "dueDate": "2026-01-01"}) == "",
      "a one-day task (same date) is still drawn")
check("no due date" in issue({"startDate": "2026-01-01", "dueDate": ""}),
      "only a start date -> not drawn, and says which is missing")
check("no start date" in issue({"startDate": "", "dueDate": "2026-02-01"}),
      "only a due date -> not drawn")
check("no start date and no due date" == issue({"text": "sin fechas"}),
      "neither -> not drawn")
check("before the start" in issue({"startDate": "2026-03-01", "dueDate": "2026-01-01"}),
      "inverted dates -> not drawn (the frontend drops these too)")
check(issue({"startDate": "  2026-01-01 ", "dueDate": " 2026-02-01 "}) == "",
      "whitespace around a date is not a missing date")

print("\n=== the two copies of the rule agree ===")
# The frontend filter is `t.start && t.end && t.end >= t.start`. If someone
# loosens one side, this is where it shows up.
gantt = open("../OneBoxFrontEnd/OneBox/src/components/ProjectGantt.tsx",
             encoding="utf-8").read() if __import__("os").path.exists(
    "../OneBoxFrontEnd/OneBox/src/components/ProjectGantt.tsx") else ""
if gantt:
    check("t.start && t.end && t.end! >= t.start!" in gantt,
          "ProjectGantt still filters on both dates with end >= start")
    check("ProjectGantt.tsx" in open("agent/tools/tasks.py", encoding="utf-8").read(),
          "and the backend rule points at it, so the pair is findable")
else:
    print("  SKIP (frontend not checked out next to the backend)")

print("\n=== names are matched the way people say them ===")
_r = load("agent/graph/nodes/executor/resolve.py", "_normalize", "_match_and_extract")
match = _r["_match_and_extract"]
PROJECTS = {"count": 3, "projects": [
    {"projectId": "p1", "name": "Sistema de Gestión de Inventarios"},
    {"projectId": "p2", "name": "Farmacia Haussman"},
    {"projectId": "p3", "name": "Onebox2026"}]}

check(match(PROJECTS, "name", "Sistema de Gestión de Inventarios", "projectId") == "p1",
      "exact still works")
check(match(PROJECTS, "name", "sistema de gestion de inventarios", "projectId") == "p1",
      "without accents or capitals -> same project (the 2026-09-23 case)")
check(match(PROJECTS, "name", "  SISTEMA  DE   GESTION de inventarios ", "projectId") == "p1",
      "extra whitespace and shouting -> same project")
check(match(PROJECTS, "name", "inventarios", "projectId") == "p1",
      "a fragment the user actually said -> found")

print("\n=== but ambiguity is never resolved by guessing ===")
TWO = {"projects": [{"projectId": "a", "name": "Marketing Q3"},
                    {"projectId": "b", "name": "Marketing Q4"}]}
check(match(TWO, "name", "Marketing", "projectId") is None,
      "two records match -> None, so the user gets asked instead of guessed at")
check(match(PROJECTS, "name", "proyecto que no existe", "projectId") is None,
      "no match -> None, as before")

print("\n" + ("ALL OK" if ok else "FAILURES"))
sys.exit(0 if ok else 1)
