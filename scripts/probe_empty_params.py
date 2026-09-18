#!/usr/bin/env python3
"""Does the planner emit empty params for the Trello flow, and only there?

    python scripts/probe_empty_params.py

Runs the REAL planner prompt against the REAL provider, locally. No client, no
CloudWatch, no Gemini needed. It only reads: no tool is executed.

WHAT IS BEING TESTED
On 2026-09-16 a user got, several turns in a row:

    step=2 tool=create_trello_board   params={}
    step=2 tool=push_tasks_to_trello  params={}
    step=? tool=resolve_entity        params={}

and complained about TRELLO ONLY -- not about tasks, projects or email. A
provider-level failure to fill parameters would have broken everything, so the
cause is probably not the provider. The suspicion is the prompt: the Trello
section is the only one that shows plans as bare names in brackets AND tells
the model, twice, to call a tool with no parameters --

    Just push: [resolve_entity, push_tasks_to_trello] with NO list at all.
    CALL push_tasks_to_trello WITH NO list_name AND NO board_name

-- while create_task is the only section that shows a full params object. If
that is the cause, the Trello messages below come back with empty params and
the control messages do not, on the same provider, in the same run.

READING THE RESULT
  Trello empty, controls filled  -> the prompt is the cause. Rewrite the
                                    recipes in the create_task notation.
  everything empty               -> not the prompt. Look at the provider.
  nothing empty                  -> not reproducible this way; the difference
                                    is in the user's data or history.
"""
import os
import sys

os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

from agent.graph.llm_factory import create_llm                     # noqa: E402
from agent.graph.nodes.planner.prompts import PLANNER_PROMPT       # noqa: E402
from agent.graph.nodes.planner.schemas import PlannerOutput        # noqa: E402
from agent.tools import TOOL_MAP                                   # noqa: E402
from langchain_core.messages import HumanMessage, SystemMessage    # noqa: E402
import inspect                                                     # noqa: E402


def required_of(tool: str):
    """Parameters with no default. A tool that needs none is CORRECT with
    params={} -- list_projects() is the obvious case, and counting it as a
    failure is how a probe reports a problem that is not there."""
    fn = TOOL_MAP.get(tool)
    if fn is None:
        return []
    return [n for n, p in inspect.signature(fn).parameters.items()
            if p.default is inspect.Parameter.empty
            and p.kind not in (p.VAR_POSITIONAL, p.VAR_KEYWORD)]

TRELLO = [
    "manda las tareas de Farmacia Haussman a trello",
    "crea un tablero de trello para DW - MOODLE",
    "exporta las tareas del proyecto Alpha a trello",
]
CONTROL = [
    "crea una tarea en el proyecto Alpha: revisar el presupuesto",
    "muestrame las tareas de Farmacia Haussman",
    "avisale a Juan que la entrega se movio",
]

system = (PLANNER_PROMPT
          .replace("{history}", "")
          .replace("{previous_results}", "")
          .replace("{results_this_turn}", "")
          .replace("{project_boards}", "(none)"))

# Force a provider from the command line, e.g.
#     python scripts/probe_empty_params.py gemini
# Without an argument it uses whatever NODE_LLM_PLANNER / LLM_PROVIDER say,
# which locally means .env -- and .env is NOT what production runs on.
forced = sys.argv[1] if len(sys.argv) > 1 else ""
llm = create_llm("planner", env_value=forced, with_fallbacks=not forced)
structured = llm.with_structured_output(PlannerOutput)
print(f"provider config: NODE_LLM_PLANNER="
      f"{os.environ.get('NODE_LLM_PLANNER', '(default)')}  "
      f"LLM_PROVIDER={os.environ.get('LLM_PROVIDER', '(auto)')}\n")

totals = {}
for label, messages in (("TRELLO", TRELLO), ("CONTROL", CONTROL)):
    empty = filled = 0
    print(f"───── {label}")
    for msg in messages:
        print(f'  "{msg}"')
        try:
            out = structured.invoke([SystemMessage(content=system),
                                     HumanMessage(content=f"User message: {msg}")])
        except Exception as e:
            print(f"      ERROR {type(e).__name__}: {e}")
            continue
        if not out.plan:
            print(f"      direct_response (no plan): "
                  f"{(out.direct_response or '')[:90]}")
            continue
        for s in out.plan:
            need = required_of(s.tool)
            missing = [r for r in need if r not in (s.params or {})]
            if missing:
                empty += 1
                mark = f"  <-- MISSING REQUIRED: {missing}"
            else:
                filled += 1
                mark = "  (no parameters needed)" if not need else ""
            print(f"      step={s.step} {s.tool:24} params={s.params}{mark}")
    totals[label] = (empty, filled)
    print()

print("═════ RESULT")
for label, (e, f) in totals.items():
    print(f"  {label:8} steps OK: {f}    steps MISSING a required param: {e}")
te, tf = totals.get("TRELLO", (0, 0))
ce, cf = totals.get("CONTROL", (0, 0))
if te and not ce:
    print("\n  Empty only in the Trello flow -> the prompt is the cause.")
elif te and ce:
    print("\n  Empty everywhere -> not the prompt. Check which provider served this.")
elif not te:
    print("\n  Not reproducible from the prompt alone.")
