# -*- coding: utf-8 -*-
"""Thinking tokens come out of maxOutputTokens, so the budget is a contract.

Google's docs: "max_output_tokens sets the maximum number of tokens a response
can generate, INCLUDING thought tokens". The planner ships ~12k tokens of
catalog, the model thinks for a long time on that, and what is left has to
hold the JSON. On 2026-09-21 it did not (document cut mid-string, a real
user); on 2026-09-22 it did not (schema-valid EMPTY object, twice).

Reads the rule out of the source: no network, no key, no model.
"""
import ast
import os
import sys

ok = True


def check(c, l):
    global ok
    ok &= bool(c)
    print(f"  {'OK ' if c else 'FAILS'} {l}")


_src = open("agent/graph/gemini_adapter.py", encoding="utf-8").read()
_ns = {"os": os, "Optional": __import__("typing").Optional}
for _n in ast.parse(_src).body:
    if isinstance(_n, ast.FunctionDef) and _n.name in ("_major", "_thinking_budget_for", "_thinking_level_for"):
        exec(compile(ast.Module([_n], []), "<exec>", "exec"), _ns)
budget = _ns["_thinking_budget_for"]
level = _ns["_thinking_level_for"]

os.environ.pop("GEMINI_THINKING_BUDGET", None)

print("=== the default: off where the API allows it ===")
check(budget("gemini-2.5-flash") == 0, "flash -> 0 (thinking off, whole budget for the answer)")
check(budget("gemini-2.0-flash-lite") == 0, "any 2.x flash -> 0")
# Gemini 3: thinkingBudget=0 is a 400 INVALID_ARGUMENT on gemini-3.5-flash-lite
# (scripts/llm-testing/check_gemini_request.py, 2026-10-11): thinkingLevel instead.
check(budget("gemini-3.5-flash-lite") is None and level("gemini-3.5-flash-lite") == "minimal",
      "3.x flash -> no budget, thinkingLevel=minimal")
check(budget("gemini-3.8-flash") is None and level("gemini-3.8-flash") == "minimal", "3.8 flash -> minimal too")
check(level("gemini-3.8-pro") is None and level("gemini-2.5-flash") is None,
      "no level on pro models, nor on 2.x (they use the budget)")
os.environ["GEMINI_THINKING_LEVEL"] = "low"
check(level("gemini-3.5-flash-lite") == "low", "GEMINI_THINKING_LEVEL wins")
os.environ.pop("GEMINI_THINKING_LEVEL")
os.environ["GEMINI_THINKING_BUDGET"] = "512"
check(level("gemini-3.5-flash-lite") is None, "an explicit budget is never sent together with a level")
os.environ.pop("GEMINI_THINKING_BUDGET")

print("\n=== and NOT off where it would be rejected ===")
# 2.5-pro's minimum budget is 128; sending 0 is an API error, so a blanket
# default of 0 would break the moment GEMINI_MODEL changes.
check(budget("gemini-2.5-pro") is None, "pro -> no thinkingConfig sent at all")
check(budget("") is None and budget(None) is None, "unknown/blank model -> nothing sent")

print("\n=== the env var overrides both ===")
for v, want in (("0", 0), ("-1", -1), ("1024", 1024)):
    os.environ["GEMINI_THINKING_BUDGET"] = v
    check(budget("gemini-2.5-pro") == want, f"GEMINI_THINKING_BUDGET={v} wins on any model")
os.environ["GEMINI_THINKING_BUDGET"] = "no"
check(budget("gemini-2.5-flash") == 0, "junk in the env falls back to the default")
os.environ.pop("GEMINI_THINKING_BUDGET", None)

print("\n=== the response's own account of what happened is read ===")
check("finishReason" in _src and 'finish == "MAX_TOKENS"' in _src,
      "a truncated answer raises instead of reaching the graph half-decoded")
check("thoughtsTokenCount" in _src,
      "the thinking cost is logged, so this is measurable next time")

print("\n=== the ceiling both providers get ===")
_fac = open("agent/graph/llm_factory.py", encoding="utf-8").read()
_mt = [n for n in ast.parse(_fac).body
       if isinstance(n, ast.Assign) and getattr(n.targets[0], "id", "") == "_MAX_TOKENS"]
check(bool(_mt) and ast.literal_eval(_mt[0].value) >= 8192,
      "max_tokens >= 8192 (billed on use, so the headroom is free until needed)")
check("max_tokens: int = 4096" not in _fac,
      "no 4096 left behind to silently override it")

print("\n=== the OTHER Gemini client follows the same rule ===")
# agent/llm.py is a second, independent Gemini path (document_parser,
# project_helpers, gmail sync, insights). It had no thinking budget, no
# finishReason check, and returned "" on an empty response -- which the
# callers fed straight into extract_json_from_response.
_raw = open("agent/llm.py", encoding="utf-8").read()
check("thinkingConfig" in _raw, "agent/llm.py sends a thinking budget too")
check("_thinking_budget_for" in _raw,
      "and gets it from the SAME helper, so the two clients cannot drift")
check("finishReason" in _raw and "MAX_TOKENS" in _raw,
      "a truncated answer raises there as well")
check("\n    return text\n" in _raw and "raise Exception(" in _raw,
      "it no longer returns an empty string as if it were an answer")

print("\n=== and the per-call timeout matches the bigger ceiling ===")
_ad = open("agent/graph/gemini_adapter.py", encoding="utf-8").read()
check("timeout: int = 60" not in _ad, "the adapter no longer times out at 60s")

print("\n" + ("ALL OK" if ok else "FAILURES"))
sys.exit(0 if ok else 1)
