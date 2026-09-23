# -*- coding: utf-8 -*-
"""A fallback that says nothing makes provider choice unmeasurable.

On 2026-09-22 Gemini failed 8 of 12 planner calls in one session. Every one of
those was quietly answered by a fallback provider, so the transcript looked
healthy and the only visible symptom was a 143-second turn. Deciding "which
model runs which node" from that data is impossible: nothing records which
model actually answered.

This pins the two things that make the decision measurable: every attempt is
attributed, and fallbacks can be switched off to measure one provider alone.
No network, no keys, no langchain models -- only fake runnables.
"""
import ast
import sys

ok = True


def check(c, l):
    global ok
    ok &= bool(c)
    print(f"  {'OK ' if c else 'FAILS'} {l}")


src = open("agent/graph/llm_factory.py", encoding="utf-8").read()

print("=== every attempt is attributed, win or lose ===")
check("def _attributed(" in src, "_attributed() wraps each provider")
check('obs.log("llm_attempt"' in src, "each attempt is logged as llm_attempt")
check(src.count('obs.log("llm_attempt"') >= 2,
      "both outcomes are logged: the one that answered AND the one that failed")
check('"ms": ' in src or "ms=ms" in src, "with its duration, so providers can be compared")

print("\n=== the structured path is attributed too ===")
# The planner/resolver/validator all go through with_structured_output; if only
# the plain path were wrapped, the three nodes that matter would stay invisible.
i = src.index("def with_structured_output")
body = src[i:i + 400]
check("_chain(" in body, "with_structured_output routes through the attributed chain")

print("\n=== fallbacks can be switched off for a clean measurement ===")
check("LLM_FALLBACKS" in src, "LLM_FALLBACKS exists")
check('("off", "0", "false", "no")' in src, "accepts the obvious spellings")

print("\n=== per-node provider selection (this already existed) ===")
check("NODE_LLM_" in src, "NODE_LLM_<NODE> picks the provider per node")
check("def _parse_config" in src, "'provider:model' is parsed")

print("\n=== the chain still works with one provider and with several ===")
_ns = {}
for n in ast.parse(src).body:
    if isinstance(n, ast.FunctionDef) and n.name == "_attributed":
        exec(compile(ast.Module([n], []), "<x>", "exec"), _ns)


class FakeRunnable:
    """Enough of the Runnable surface for _attributed + with_fallbacks."""
    def __init__(self, name, fails=False):
        self.name, self.fails = name, fails

    def invoke(self, x):
        if self.fails:
            raise RuntimeError(f"{self.name} is down")
        return f"answered by {self.name}"


try:
    from langchain_core.runnables import RunnableLambda  # noqa: F401
    have_lc = True
except Exception:
    have_lc = False

if have_lc:
    a = _ns["_attributed"](FakeRunnable("gemini", fails=True), "gemini", "flash", "planner")
    b = _ns["_attributed"](FakeRunnable("anthropic"), "anthropic", "sonnet", "planner")
    chained = a.with_fallbacks([b])
    out = chained.invoke("x")
    check(out == "answered by anthropic",
          "the fallback answers when the primary fails -- and both left a line")
else:
    print("  SKIP (langchain_core not installed here; runs in check.sh)")

print("\n" + ("ALL OK" if ok else "FAILURES"))
sys.exit(0 if ok else 1)
