# -*- coding: utf-8 -*-
"""Which provider runs which node, pinned so it cannot drift by accident.

Two days of live testing produced one routing decision:

  planner            -> anthropic. On gemini-2.5-flash it failed 8 of 12 calls
                        (MAX_TOKENS after emitting 28k-40k chars for a two-step
                        plan); on gemini-2.5-pro it silently marked the WRONG
                        task as done. It is the only node whose output is
                        executed, so its mistakes reach real Trello boards and
                        real phones.
  the other three    -> gemini-2.5-flash. 0 failures in 10 calls, 0.3-2.0 s.

No network, no keys: the routing rule is read out of the source.
"""
import ast
import os
import sys

ok = True


def check(c, l):
    global ok
    ok &= bool(c)
    print(f"  {'OK ' if c else 'FAILS'} {l}")


src = open("agent/graph/llm_factory.py", encoding="utf-8").read()

# Load just the routing helpers, with credentials stubbed as present.
_ns = {"os": os, "PROVIDERS": ("bedrock", "anthropic", "gemini"),
       "ANTHROPIC_API_KEY": "sk-test", "GEMINI_API_KEY": "gm-test"}
for n in ast.parse(src).body:
    if isinstance(n, ast.FunctionDef) and n.name in (
            "_parse_config", "_provider_available", "_routing_for", "_checked"):
        exec(compile(ast.Module([n], []), "<x>", "exec"), _ns)
    if isinstance(n, ast.Assign) and getattr(n.targets[0], "id", "") == "_NODE_PROVIDER":
        exec(compile(ast.Module([n], []), "<x>", "exec"), _ns)

routing = _ns["_routing_for"]
for var in ("NODE_LLM_PLANNER", "NODE_LLM_VALIDATOR", "NODE_LLM_NARRATOR",
            "NODE_LLM_CONTEXT_RESOLVER", "LLM_PROVIDER_ALL"):
    os.environ.pop(var, None)

print("=== the default routing ===")
check(routing("planner") == ("anthropic", None),
      "planner -> anthropic (model left to ANTHROPIC_MODEL)")
for node in ("context_resolver", "validator", "narrator"):
    check(routing(node) == ("gemini", "gemini-2.5-flash"),
          f"{node} -> gemini-2.5-flash")

print("\n=== the flash model is PINNED, not inherited ===")
# Someone switching GEMINI_MODEL to try 2.5-pro must not silently move the
# three measured nodes onto a model nobody measured.
os.environ["GEMINI_MODEL"] = "gemini-2.5-pro"
check(routing("validator") == ("gemini", "gemini-2.5-flash"),
      "GEMINI_MODEL=gemini-2.5-pro does not drag the validator along")
os.environ.pop("GEMINI_MODEL", None)

print("\n=== precedence ===")
os.environ["LLM_PROVIDER_ALL"] = "gemini:gemini-2.5-flash"
check(routing("planner") == ("gemini", "gemini-2.5-flash"),
      "LLM_PROVIDER_ALL beats the table (so one provider can be A/B'd alone)")
os.environ["NODE_LLM_PLANNER"] = "anthropic:claude-haiku-4"
check(routing("planner") == ("anthropic", "claude-haiku-4"),
      "NODE_LLM_<NODE> beats LLM_PROVIDER_ALL")
os.environ.pop("NODE_LLM_PLANNER", None)
os.environ.pop("LLM_PROVIDER_ALL", None)

print("\n=== a routed provider with no credentials degrades loudly ===")
_ns["ANTHROPIC_API_KEY"] = ""
check(routing("planner") == (None, None),
      "no ANTHROPIC_API_KEY -> falls through to the global default")
_ns["ANTHROPIC_API_KEY"] = "sk-test"

print("\n=== every graph node is routed ===")
nodes = [n for n in ast.parse(src).body
         if isinstance(n, ast.Assign) and getattr(n.targets[0], "id", "") == "NODE_NAMES"]
declared = ast.literal_eval(nodes[0].value) if nodes else ()
check(set(declared) == set(_ns["_NODE_PROVIDER"]),
      f"the table covers exactly the four graph nodes: {sorted(declared)}")

print("\n=== .env.example and the code table say the same thing ===")
# Two copies of the same decision drift. This is the repo's own lesson from
# descriptions.py vs the tool signatures, applied before it happens again.
import os.path
if os.path.exists(".env.example"):
    env = {}
    for line in open(".env.example", encoding="utf-8"):
        line = line.strip()
        if line.startswith("NODE_LLM_") and "=" in line:
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip()
    for node, expected in _ns["_NODE_PROVIDER"].items():
        got = env.get(f"NODE_LLM_{node.upper()}")
        check(got == expected,
              f"NODE_LLM_{node.upper()}: .env.example={got!r} table={expected!r}")
    check("LLM_FALLBACKS" in open(".env.example", encoding="utf-8").read(),
          "the measurement switches are documented where someone will find them")
else:
    check(False, ".env.example exists")

print("\n=== a typo in the routing is fatal, and says which variable ===")
os.environ["NODE_LLM_PLANNER"] = "antropic"
try:
    routing("planner")
    check(False, "a misspelled provider must not boot silently")
except ValueError as e:
    check("NODE_LLM_PLANNER" in str(e),
          "the error names the variable and shows a valid example")
os.environ.pop("NODE_LLM_PLANNER", None)

print("\n" + ("ALL OK" if ok else "FAILURES"))
sys.exit(0 if ok else 1)
