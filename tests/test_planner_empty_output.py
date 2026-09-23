# -*- coding: utf-8 -*-
"""An empty PlannerOutput is a failed attempt, not a decision.

Every field of PlannerOutput has a default, so `{}` decodes cleanly: no plan,
no reply, no exception. Anthropic never returns it. Gemini does -- observed
twice in a row on 2026-09-22 for an ordinary Spanish request that had worked
minutes earlier. The retry loop broke on `output is not None`, so that empty
object was accepted as the planner's answer and the turn ended with nothing.

No LLM, no deps: the predicate is read out of the planner source.
"""
import ast
import sys
import types

ok = True


def check(c, l):
    global ok
    ok &= bool(c)
    print(f"  {'OK ' if c else 'FAILS'} {l}")


_src = open("agent/graph/nodes/planner/node.py", encoding="utf-8").read()
_ns = {}
for _n in ast.parse(_src).body:
    if isinstance(_n, ast.FunctionDef) and _n.name == "_is_empty_output":
        exec(compile(ast.Module([_n], []), "<exec>", "exec"), _ns)
_is_empty_output = _ns.get("_is_empty_output")
check(_is_empty_output is not None, "_is_empty_output is module level (testable)")


def out(plan=None, direct_response=None):
    return types.SimpleNamespace(plan=plan or [], direct_response=direct_response,
                                 proposing=[], reasoning=None)


print("\n=== what counts as an empty decision ===")
check(_is_empty_output(out()) is True,
      "no plan and no reply -> empty (the Gemini `{}` decode)")
check(_is_empty_output(out(direct_response="   ")) is True,
      "whitespace-only reply is still empty")
check(_is_empty_output(out(direct_response=None, plan=[])) is True,
      "explicit nulls are empty")

print("\n=== what must NOT be retried ===")
check(_is_empty_output(out(plan=[{"step": 1, "tool": "list_projects"}])) is False,
      "a plan is a decision")
check(_is_empty_output(out(direct_response="Hola, cuentame mas")) is False,
      "a reply is a decision")
check(_is_empty_output(out(plan=[{"step": 1}], direct_response="ok")) is False,
      "both is still a decision")

print("\n=== and no English is hardcoded into the user's reply any more ===")
canned = [l for l in _src.splitlines()
          if '"direct_response"' in l and l.strip().endswith('(')]
bad = [l for l in _src.splitlines()
       if "I didn't quite understand" in l or "Sorry, I had a problem" in l]
bad = [l for l in bad if not l.lstrip().startswith("#")]
check(not bad, f"no canned English sentence remains: {bad}")

print("\n" + ("ALL OK" if ok else "FAILURES"))
sys.exit(0 if ok else 1)
