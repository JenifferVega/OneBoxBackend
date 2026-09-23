#!/usr/bin/env python3
"""When something breaks, the apology must be in the user's language.

    python tests/test_degraded_language.py

Five sentences used to reach the user without passing through any model:

    narrator/node.py:37   the plan returned no results
    narrator/node.py:64   the narrator's own LLM failed
    planner/node.py:181   the planner ran out of iterations
    runner.py:154         the graph raised
    runner.py:215         the response came back empty

All five were written in English, on a product whose users write Spanish, and
all five fire exactly when something has ALREADY gone wrong -- so the one
moment the wording matters most was the one moment nothing chose it.

`personality.LANGUAGE` is in every prompt for this reason. These paths never
reach a prompt.

Two of them can be fixed properly (let the narrator write them, with the
conversation in front of it). The three that fire when no model is reachable
cannot, so they at least read the language the resolver detected.
"""
import os
import sys

os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent.graph.nodes.narrator.helpers import last_resort  # noqa: E402
from agent.graph.nodes.narrator.node import narrator_node  # noqa: E402

failures = []
print()


def check(cond, label):
    print(f"  {'PASS' if cond else 'FAIL'}  {label}")
    if not cond:
        failures.append(label)


class BoomLLM:
    """The narrator's provider is down. Nothing can be phrased on demand."""
    def invoke(self, messages):
        raise RuntimeError("provider unavailable")


class EchoLLM:
    """Captures the prompt it was given and answers in a recognisable way."""
    def __init__(self):
        self.prompts = []

    def invoke(self, messages):
        self.prompts.append("\n".join(getattr(m, "content", "") for m in messages))

        class R:
            content = "RESPUESTA GENERADA POR EL MODELO"
        return R()


SPANISH_TURN = {
    "user_message": "subeme las tareas pendientes de Marketing Q3 a Trello",
    "language": "es",
    "intent": "generic",
    "results": {1: {"count": 3, "tasks": []}},
    "tools_by_step": {1: "list_tasks"},
}

print("─ the narrator's LLM is down ─────────────────────────────────")
out = narrator_node(dict(SPANISH_TURN), BoomLLM())
resp = out.get("response", "")
print(f"     -> {resp!r}")
check("I ran your request" not in resp,
      "does NOT fall back to the English sentence")
check(any(w in resp.lower() for w in ("ejecut", "resumen", "datos")),
      "the reply a Spanish user gets is in Spanish")

out_en = narrator_node(dict(SPANISH_TURN, language="en"), BoomLLM())
check("I ran your request" in out_en.get("response", ""),
      "an English user still gets English")

out_none = narrator_node(dict(SPANISH_TURN, language=""), BoomLLM())
check(out_none.get("response", "") != "",
      "an unknown language still produces something, not an empty reply")

print("\n─ the plan returned nothing ──────────────────────────────────")
llm = EchoLLM()
out = narrator_node(dict(SPANISH_TURN, results={}), llm)
check(len(llm.prompts) == 1,
      "an empty result set GOES THROUGH the model instead of a canned string")
check("I didn't get any results" not in out.get("response", ""),
      "the old English sentence is gone")
check("No tool returned any data" in (llm.prompts[0] if llm.prompts else ""),
      "the model is told what happened, so it can say it in any language")

print("\n─ last_resort table ──────────────────────────────────────────")
check(last_resort("narrator_failed", "es") != last_resort("narrator_failed", "en"),
      "es and en are actually different sentences")
check(last_resort("no_response", "es-419") == last_resort("no_response", "es"),
      "a regional tag falls back to its base language")
check(last_resort("no_response", "de") == last_resort("no_response", "en"),
      "an unsupported language falls back to English, never to empty")
check(last_resort("does_not_exist", "es") == "",
      "an unknown kind returns empty rather than raising")

print("\n─ the planner stops writing the user's message ───────────────")
import agent.graph.nodes.planner.node as P  # noqa: E402

out = P.planner_node({"user_message": "hola", "iteration": P.MAX_PLANNER_ITERATIONS},
                     llm=BoomLLM())
check(out.get("direct_response") is None,
      "the iteration limit no longer hands the user a canned English line")
check(out.get("validation_decision") == "CONTINUE",
      "it passes a verdict instead, so the narrator knows what to say")
check(out.get("status") == "direct",
      "and routes straight to the narrator")

print()
if failures:
    print(f"{len(failures)} FAILED:")
    for f in failures:
        print(f"  - {f}")
    sys.exit(1)
print("degraded paths speak the user's language: all clear\n")
