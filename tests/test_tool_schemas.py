#!/usr/bin/env python3
"""The schema the planner is decoded under must be one it can actually fill.

    python tests/test_tool_schemas.py

No LLM, no network, no database. Runs in about a second.

This is the regression test for the production failure of 2026-09-16, where a
user got, four replans in a row:

    step=2 tool=push_tasks_to_trello params={}
    step=2 tool=resolve_entity       params={}

Nothing was broken in Trello -- the board listing in the same session returned
two real boards. `PlanStep.params` was a bare `dict`, which is
`{"type": "object", "additionalProperties": true}`. Gemini rejects
additionalProperties, the adapter strips it, and what is left is an object
with no declared properties. Gemini decodes UNDER the schema, so with no legal
key the only permitted output is `{}` -- for every tool, every time. Anthropic
reads the same schema as a description and fills it, which is why it worked
locally and failed for the user.

Three things are checked, and each one would have caught it alone.
"""
import os
import sys

os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

PASS, FAIL = "  PASS ", "  FAIL "
_failures = []


def check(ok, label, detail=""):
    print(f"{PASS if ok else FAIL} {label}")
    if not ok:
        _failures.append(label)
        for line in str(detail).splitlines():
            print(f"          {line}")


# ── 1. The union of parameters is derivable from the tools ──────────────────
print("\n1. Parameters derived from the signatures")
try:
    from agent.tools import TOOL_MAP
    from agent.tools.schema import (ConflictingParameterType, all_tool_schemas,
                                    param_union_schema)
    union = param_union_schema()
    props = union.get("properties", {})
    check(len(props) >= 30,
          f"params has declared properties ({len(props)} names from "
          f"{len(TOOL_MAP)} tools)",
          "An empty or tiny union means the tools did not import.")
    for must in ("project_id", "name", "query"):
        check(must in props, f"'{must}' is declared")
    untyped = [k for k, v in props.items() if "type" not in v]
    check(not untyped, "every parameter has a type", f"untyped: {untyped}")
except ConflictingParameterType as e:
    check(False, "parameter names are consistent across tools", e)
except Exception as e:
    check(False, "agent.tools.schema imports", f"{type(e).__name__}: {e}")
    print("\nCannot continue.")
    sys.exit(1)


# ── 2. Every tool yields a schema that matches its real signature ───────────
print("\n2. Per-tool schemas match the signatures")
import inspect  # noqa: E402

bad = []
for s in all_tool_schemas():
    fn = TOOL_MAP[s["name"]]
    sig = inspect.signature(fn)
    real = [n for n, p in sig.parameters.items()
            if p.kind not in (p.VAR_POSITIONAL, p.VAR_KEYWORD)]
    real_required = [n for n in real
                     if sig.parameters[n].default is inspect.Parameter.empty]
    got = s["input_schema"]["properties"]
    if sorted(got) != sorted(real):
        bad.append(f"{s['name']}: properties {sorted(got)} != signature {sorted(real)}")
    if sorted(s["input_schema"]["required"]) != sorted(real_required):
        bad.append(f"{s['name']}: required {s['input_schema']['required']} "
                   f"!= {real_required}")
    if not s["description"]:
        bad.append(f"{s['name']}: no description (the docstring's first paragraph)")
check(not bad, f"all {len(TOOL_MAP)} tool schemas agree with their signatures",
      "\n".join(bad))


# ── 3. The planner schema survives the trip to Gemini ───────────────────────
# The one that matters. Everything above can be right and this still fail.
print("\n3. The planner schema is fillable by a constrained decoder")
try:
    from agent.graph.gemini_adapter import (UnfillableSchema, _assert_fillable,
                                            _normalize_schema_for_gemini)
    from agent.graph.nodes.planner.schemas import PlannerOutput

    normalized = _normalize_schema_for_gemini(PlannerOutput.model_json_schema())
    try:
        _assert_fillable(normalized)
        check(True, "PlannerOutput survives Gemini normalization")
    except UnfillableSchema as e:
        check(False, "PlannerOutput survives Gemini normalization",
              f"{e}\nThis is the 2026-09-16 bug. On Gemini the planner "
              f"returns empty parameters for every tool.")

    step = normalized["properties"]["plan"]["items"]["properties"]["params"]
    check(bool(step.get("properties")),
          f"params keeps its properties after normalization "
          f"({len(step.get('properties', {}))} left)",
          "additionalProperties was stripped and nothing replaced it: "
          "Gemini can only emit {} for this field.")

    # Negative control: the guard must actually reject the old shape. A guard
    # that passes everything passes this file too.
    try:
        _assert_fillable({"type": "object"})
        check(False, "the guard rejects an object with no properties",
              "It accepted one, so it would not have caught the original bug.")
    except UnfillableSchema:
        check(True, "the guard rejects an object with no properties")
except Exception as e:
    check(False, "gemini adapter / planner schema importable",
          f"{type(e).__name__}: {e}")


print()
if _failures:
    print(f"{len(_failures)} check(s) failed:")
    for f in _failures:
        print(f"  - {f}")
    sys.exit(1)
print("The planner can be given parameters on every provider.")
sys.exit(0)
