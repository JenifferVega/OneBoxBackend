#!/usr/bin/env python3
"""A tool that leaves OneBox must not run on an answer to a different question.

    python tests/test_consent_gate.py

Observed 2026-09-21, dry run. The assistant asked TWO things in one breath:

    1. "Do you confirm creating the Trello board for Marketing Q3?"
    2. "Which tasks (todos) do you want to create?"

The user replied "sube las que ya estan pendientes" -- an answer to the
SECOND. The next turn executed create_trello_board. In production that is a
board in the user's real Trello account that nobody authorised.

The cause is structural, not a weak prompt rule: a turn that asks for
confirmation runs no tools, so `pending_goal` -- which the runner opens from
what tools report -- can never hold the proposal. Nothing in the state said
"we asked about create_trello_board", so the planner had only the prose of the
previous turn to go on, and prose is exactly what the model is free to read
generously.

So consent became a recorded fact: the planner DECLARES what it is asking for
(`proposing`), the runner keeps it for exactly one turn (`awaiting_consent`),
the resolver judges whether this message authorises it (`consent_granted`),
and the executor refuses otherwise. This test pins the refusal.
"""
import os
import sys

os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent.graph.nodes.executor.node import (  # noqa: E402
    _CONSENT_TOOLS, _READ_TOOLS, _WRITE_TOOLS, executor_node,
)

failures = []
print()


def check(cond, label):
    print(f"  {'PASS' if cond else 'FAIL'}  {label}")
    if not cond:
        failures.append(label)


def run(tool, params, **state):
    base = {
        "plan": [{"step": 1, "tool": tool, "params": params}],
        "results": {}, "tools_used": [], "executed_writes": {},
        "debug_mode": True, "session_id": "test-consent",
        "awaiting_consent": [], "consent_granted": False,
        # Recorded as already done so the prior-read gate -- which runs FIRST
        # -- does not mask the consent behaviour this file is about.
        "tools_by_step": {0: "list_trello_boards"},
        "user_message": "", "resolved_message": "", "history": [],
    }
    base.update(state)
    return executor_node(base)


print("─ the set itself ─────────────────────────────────────────────")
check(_CONSENT_TOOLS <= _WRITE_TOOLS,
      "every consent tool is classified as a write")
check(not (_CONSENT_TOOLS & _READ_TOOLS),
      "no consent tool is also classified as a read")
try:
    from agent.tools import TOOL_MAP
    check(_CONSENT_TOOLS <= set(TOOL_MAP),
          "every consent tool is a registered tool")
except Exception as e:                                    # pragma: no cover
    print(f"  SKIP  TOOL_MAP unavailable ({e})")

print("\n─ nothing on record ──────────────────────────────────────────")
out = run("create_trello_board", {"name": "Marketing Q3", "project_id": "p1"})
r1 = out["results"][1]
check(r1.get("_consent_missing") == "create_trello_board",
      "create_trello_board is blocked when nothing was proposed")
check(r1.get("needs_user_choice") is True,
      "blocked step is shaped as a question, not a bare failure")
check("create_trello_board" not in (out.get("tools_used") or []),
      "a blocked tool is NOT recorded in tools_used -- it never ran")
check(not out.get("executed_writes"),
      "a blocked tool leaves no entry in the write ledger")

print("\n─ the 2026-09-21 case: consent for a DIFFERENT answer ────────")
# The proposal was on record, but the resolver judged the message did not
# authorise it. This is the exact shape of the bug.
out = run("create_trello_board", {"name": "Marketing Q3", "project_id": "p1"},
          awaiting_consent=["create_trello_board"], consent_granted=False)
check(out["results"][1].get("_consent_missing") == "create_trello_board",
      "a proposal on record is NOT consent by itself")

print("\n─ properly authorised ────────────────────────────────────────")
out = run("create_trello_board", {"name": "Marketing Q3", "project_id": "p1"},
          awaiting_consent=["create_trello_board"], consent_granted=True)
check("_consent_missing" not in out["results"][1],
      "runs when it was the proposed tool AND the user authorised it")

print("\n─ consent is per tool, not a blanket pass ────────────────────")
out = run("send_notification", {"recipient": "+34600000000", "message": "hi"},
          awaiting_consent=["create_trello_board"], consent_granted=True)
check(out["results"][1].get("_consent_missing") == "send_notification",
      "authorising one tool does not authorise another")

print("\n─ ordinary writes are untouched ──────────────────────────────")
out = run("create_task", {"project_id": "p1", "text": "revisar el diseno"})
check("_consent_missing" not in out["results"][1],
      "create_task needs no ceremony: it is reversible and stays in OneBox")

print("\n─ the read that must come first ──────────────────────────────")
# create_trello_board may not be proposed blind: you cannot offer someone the
# choice between their existing boards and a new one without having looked.
out = run("create_trello_board", {"name": "X", "project_id": "p1"},
          tools_by_step={}, awaiting_consent=[], consent_granted=False)
check(out["results"][1].get("_missing_prior_read") == "list_trello_boards",
      "blocked when list_trello_boards has not run")
check(out["results"][1].get("needs_user_choice") is None,
      "a missing prior read is a plain error -- a replan can fix it")

# Consent proves the choice was already offered, in the turn where the read
# also already happened. Demanding it again re-reads what the user has been
# shown, and costs a replan to find that out.
out = run("create_trello_board", {"name": "X", "project_id": "p1"},
          tools_by_step={}, awaiting_consent=["create_trello_board"],
          consent_granted=True)
check("_missing_prior_read" not in out["results"][1],
      "an authorised tool does NOT have to re-read what the user already saw")



print("\n─ a destination the user never named ─────────────────────────")
# 2026-09-22: "sube las pendientes de Onebox2026" -- a project with no board --
# and the planner passed board ONEBOX / list "MVP - TO DO", picked because the
# name resembles the project. Listing the boards is a read; deciding THIS
# project belongs on one of them is the user's call.
DEST = {"project_id": "proj-DRYRUN-bbb222", "only_pending": True,
        "board_name": "ONEBOX", "list_name": "MVP - TO DO"}

out = run("push_tasks_to_trello", DEST,
          user_message="las que ya tiene pendientes, subelas y ya esta")
check(out["results"][1].get("_unchosen_destination") is not None,
      "a board the user never named is blocked")
check(out["results"][1].get("needs_user_choice") is True,
      "and comes back as a question, so the agent asks instead of replanning")

out = run("push_tasks_to_trello", DEST,
          user_message="subelas al tablero ONEBOX, lista MVP - TO DO")
check("_unchosen_destination" not in out["results"][1],
      "the same board IS allowed once the user names it")

out = run("push_tasks_to_trello", DEST, user_message="si, dale",
          history=[{"role": "user", "content": "ponlas en ONEBOX en MVP - TO DO"},
                   {"role": "assistant", "content": "confirmas?"}])
check("_unchosen_destination" not in out["results"][1],
      "a choice made in an EARLIER user turn still counts")

out = run("push_tasks_to_trello", DEST, user_message="si, dale",
          history=[{"role": "assistant",
                    "content": "puedo subirlas a ONEBOX en MVP - TO DO"}])
check(out["results"][1].get("_unchosen_destination") is not None,
      "the ASSISTANT proposing a board is not the user choosing it")

out = run("push_tasks_to_trello",
          {"project_id": "proj-DRYRUN-ccc333", "only_pending": True},
          user_message="subelas")
check("_unchosen_destination" not in out["results"][1],
      "no board given at all is the tool's own needs_user_choice, not this")

print("\n─ a block must not burn the replan budget ────────────────────")
from agent.graph.nodes.validator.node import validator_node  # noqa: E402

class NeverCalled:
    def with_structured_output(self, schema):
        raise AssertionError("the validator called the LLM on a consent block")

out = validator_node({
    "results": {1: {"_consent_missing": "create_trello_board",
                    "needs_user_choice": True, "error": "not authorised"}},
    "plan": [], "iteration": 0, "user_message": "si, dale",
}, NeverCalled())
check(out.get("status") == "narrate",
      "a consent block narrates instead of replanning")
check(out.get("validation_decision") == "CONTINUE",
      "and carries a verdict so the narrator asks properly")


# ════════════════════════════════════════════════════════════════════════
# 2026-10-05: consent decided in CODE, not left to the planner
# ════════════════════════════════════════════════════════════════════════

def run_plan(steps, **state):
    plan = [{"step": i + 1, "tool": t, "params": p} for i, (t, p) in enumerate(steps)]
    return run(steps[0][0], steps[0][1], plan=plan, **state)


print("\n─ the loop: a block records its own proposal ─────────────────")
# The planner skipped the two-turn pattern and put the send in the plan with
# proposing=[]. The executor blocked it -- correctly -- but nothing recorded
# that a question was being asked, so the user's "yes" authorised nothing and
# the send was blocked again on every turn.
out = run("send_notification",
          {"recipient": "+34600000000", "message": "revisa el informe",
           "channel": "whatsapp"})
check(out.get("proposing") == ["send_notification"],
      "a blocked tool is returned as `proposing`, without the planner")
check((out["results"][1].get("would_do") or {}).get("message") == "revisa el informe",
      "the block carries what it WOULD have done, for a concrete question")

out2 = run("send_notification",
           {"recipient": "+34600000000", "message": "revisa el informe",
            "channel": "whatsapp"},
           awaiting_consent=out["proposing"], consent_granted=True)
check("_consent_missing" not in out2["results"][1],
      "the next turn's 'yes' now authorises it: no loop")
check(not out2.get("proposing"),
      "a run that blocked nothing proposes nothing")

print("\n─ update_task: judged by what it would DO to the task ─────────")
P = "proj-DRYRUN-aaa111"
PENDING = "Evaluar la viabilidad tecnica de la automatizacion"   # Carlos, pending
BLOCKED = "Definir la arquitectura y el stack tecnologico"       # Ana, blocked


def on_task(text, **change):
    return [("list_tasks", {"project_id": P}),
            ("update_task", {"task_id": {"from_step": 1, "match":
                             {"key": "text", "value": text}, "extract": "taskId"},
                             **change})]


out = run_plan(on_task(PENDING, status="blocked"))
r = out["results"][2]
check(r.get("_consent_missing") == "update_task",
      "blocking a pending task asks: the whole team is messaged")
check(any("EVERY participant" in w for w in r.get("would_notify") or []),
      "and says who would be notified")
check(out.get("proposing") == ["update_task"],
      "and records it as the proposal")

out = run_plan(on_task(BLOCKED, status="blocked"))
check("_consent_missing" not in out["results"][2],
      "'blocking' a task that is already blocked sends nothing: no question")

out = run_plan(on_task(PENDING, assigned_to="Ana Torres"))
check(out["results"][2].get("_consent_missing") == "update_task",
      "reassigning to someone else asks: the new assignee is notified")

out = run_plan(on_task(PENDING, assigned_to="carlos lopez"))
check("_consent_missing" not in out["results"][2],
      "re-saving the SAME assignee sends nothing: no question")

out = run_plan(on_task(PENDING, status="done"))
check("_consent_missing" not in out["results"][2],
      "marking done notifies nobody: no question")

out = run_plan(on_task(PENDING, status="blocked"),
               awaiting_consent=["update_task"], consent_granted=True)
check("_consent_missing" not in out["results"][2],
      "with consent on record, the block runs")

print("\n─ create_task: an assignee is notified ───────────────────────")
out = run("create_task", {"project_id": P, "text": "QA", "assigned_to": "Marta"})
check(out["results"][1].get("_consent_missing") == "create_task",
      "a standalone task WITH an assignee asks")

out = run_plan([("create_project", {"name": "Alpha", "type": "Other",
                                    "description": "fases A y B con Marta"}),
                ("create_task", {"project_id": {"from_step": 1, "extract": "projectId"},
                                 "text": "Fase A", "assigned_to": "Marta"})])
check("_consent_missing" not in out["results"][2],
      "the same task inside a project being created does not (catalog exception)")

print("\n─ what comes after a blocked step waits for the same 'yes' ────")
out = run_plan([("send_notification", {"recipient": "+34600000000",
                                       "message": "hola", "channel": "whatsapp"}),
                ("create_task", {"project_id": P, "text": "seguimiento"}),
                ("list_tasks", {"project_id": P})])
check(out["results"][2].get("_held_for_consent") == ["send_notification"],
      "a later write is held, not run")
check("create_task" not in (out.get("tools_used") or []),
      "a held write never ran")
check("_held_for_consent" not in out["results"][3],
      "a later read that depends on nothing blocked still runs")

from agent.graph.runner import _succeeded_tools  # noqa: E402
check("create_task" not in _succeeded_tools(
          [{"step": 2, "tool": "create_task"}], {2: out["results"][2]}),
      "a held step is not counted as done by the runner")

print()
if failures:
    print(f"{len(failures)} FAILED:")
    for f in failures:
        print(f"  - {f}")
    sys.exit(1)
print("consent gate: all clear\n")
