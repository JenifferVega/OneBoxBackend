#!/usr/bin/env python3
"""Contracts: what the system SAYS its actions take must match what they take.

    python tests/test_contracts.py

No LLM, no DynamoDB, no frontend. Everything here is read off the code and the
prompts themselves, so it runs in about a second and is safe before every push.

Why these particular checks exist -- each one is a bug that reached a user:

  * A tool was called with a parameter it did not accept, and with a required
    one missing. Both are pure signature facts nobody was comparing.
  * The planner is told how to call a tool by TOOLS_DESCRIPTION and catalog.py.
    Those are prose, written by hand, and they drift from the signatures they
    describe. A planner told about a parameter that no longer exists writes a
    call that cannot work.
  * A tool with no dry-run fixture returned an empty result, so the validator
    judged the plan incomplete, the planner replanned the same thing, and the
    turn died after three iterations with no answer. It went unnoticed for a
    month because the only symptom is a conversation that goes nowhere.
  * A tool not classified as read or write is treated as a read, so a replan
    may run it twice -- which for a write means doing it twice for real.
"""
import inspect
import os
import re
import sys

# Import-time safety: the package builds boto3 resources when imported. They
# make no network call, but the region must exist for the client to build.
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

PASS, FAIL, SKIP = "  PASS ", "  FAIL ", "  SKIP "
_failures = []


def check(ok: bool, label: str, detail: str = ""):
    print(f"{PASS if ok else FAIL} {label}")
    if not ok:
        _failures.append(label)
        if detail:
            for line in detail.splitlines():
                print(f"          {line}")


def skip(label: str, why: str):
    """Something this run could not observe. NOT a pass and NOT a failure.

    A harness that reports what it could not check as a pass certifies
    behaviour nobody verified; one that reports it as a failure trains people
    to ignore red. Say it plainly instead.
    """
    print(f"{SKIP} {label}  ({why})")


def section(title: str):
    print(f"\n{title}")


# ── 1. The package imports, and every expected tool registered ──────────────
section("1. Registration")
try:
    from agent.tools import TOOL_MAP
    from agent.tools import __dict__ as tools_ns
    check(True, f"agent.tools imports ({len(TOOL_MAP)} tools registered)")
except Exception as e:
    check(False, "agent.tools imports", f"{type(e).__name__}: {e}")
    print("\nCannot continue without the tool registry.")
    sys.exit(1)

expected = tools_ns.get("_EXPECTED_TOOLS", set())
if expected:
    missing = expected - set(TOOL_MAP)
    extra = set(TOOL_MAP) - expected
    check(not missing, "every expected tool registered",
          f"missing: {sorted(missing)}" if missing else "")
    # Extra is not a failure, but an unlisted tool is invisible to the guard:
    # if it stops registering one day, nothing notices.
    check(not extra, "no tool is missing from _EXPECTED_TOOLS",
          f"registered but unlisted: {sorted(extra)}\n"
          f"Add them so the import guard protects them too." if extra else "")


# ── 2. Every tool is callable and documented ────────────────────────────────
section("2. Tools are usable by the agent")
no_doc = [n for n, f in TOOL_MAP.items() if not (inspect.getdoc(f) or "").strip()]
check(not no_doc, "every tool has a docstring",
      f"without one: {sorted(no_doc)}\n"
      f"The narrator builds its capability list from these docstrings; a tool "
      f"without one is a capability the agent cannot describe." if no_doc else "")


def required_params(fn):
    """Parameters with no default: omitting one is a TypeError at call time."""
    out = []
    for name, p in inspect.signature(fn).parameters.items():
        if p.kind in (p.VAR_POSITIONAL, p.VAR_KEYWORD):
            continue
        if p.default is inspect.Parameter.empty:
            out.append(name)
    return out


def all_params(fn):
    return set(inspect.signature(fn).parameters)


# ── 3. What the planner is TOLD matches what the code accepts ───────────────
section("3. Prompt documentation vs real signatures")
# The WHOLE prompt the planner receives, not only the tool reference: a tool
# described in a workflow section is still described. Reading only the
# reference reported a tool as undocumented when it was explained elsewhere --
# and, worse, let a tool documented in NEITHER place slip through whenever it
# happened to be named in prose.
doc_text = ""
try:
    from agent.graph.nodes.planner.prompts import PLANNER_PROMPT
    doc_text = PLANNER_PROMPT
    check(True, "planner prompt loads")
except Exception:
    try:
        from agent.graph.nodes.planner.catalog import TOOL_CATALOG
        doc_text = TOOL_CATALOG
        check(True, "planner catalog loads (full prompt unavailable)")
    except Exception as e:
        check(False, "planner prompt loads", f"{type(e).__name__}: {e}")

# Lines of the shape:  "- push_tasks_to_trello(project_id, list_id): Creates..."
documented = {}
for m in re.finditer(r"^[-•*]?\s*(\w+)\(([^)]*)\)", doc_text, re.MULTILINE):
    tool, raw = m.group(1), m.group(2)
    if tool not in TOOL_MAP:
        continue
    params = [p.strip().split("=")[0].split(":")[0].strip()
              for p in raw.split(",") if p.strip()]
    documented.setdefault(tool, set()).update(p for p in params if p.isidentifier())

check(bool(documented), f"tool calls found in the prompt ({len(documented)} tools)")

ghosts = []
for tool, params in sorted(documented.items()):
    real = all_params(TOOL_MAP[tool])
    for p in sorted(params - real):
        ghosts.append(f"{tool}(...{p}...) -- the signature has: {sorted(real)}")
check(not ghosts,
      "the prompt names no parameter that does not exist",
      "\n".join(ghosts) + "\nThe planner is being taught a call that cannot "
      "work; it will write it and the tool will reject it." if ghosts else "")

undocumented_required = []
for tool, params in sorted(documented.items()):
    for p in required_params(TOOL_MAP[tool]):
        if p not in params:
            undocumented_required.append(f"{tool}: '{p}' is REQUIRED and not documented")
check(not undocumented_required,
      "every required parameter is documented",
      "\n".join(undocumented_required) + "\nA required parameter the planner is "
      "never told about is a parameter it will omit." if undocumented_required else "")

# Appearing with its PARAMETERS, not merely being named. A tool mentioned in
# passing but never shown as a call is a tool the planner has to guess at --
# which is how create_trello_board came to be called without its `name`.
undocumented_tools = sorted(set(TOOL_MAP) - set(documented))
check(not undocumented_tools,
      "every tool is documented WITH its parameters",
      f"named nowhere as a call: {undocumented_tools}\n"
      f"The planner has to invent the call, and will omit what it was never "
      f"shown." if undocumented_tools else "")


# ── 4. The dry run can simulate every tool ──────────────────────────────────
section("4. Dry-run coverage")
try:
    from agent.graph.nodes.executor import node as ex
    fixtures = set(getattr(ex, "_DRY_RUN_RESULTS", {}))
    src = inspect.getsource(ex._simulate_tool)
    special = set(re.findall(r'tool_name == "(\w+)"', src))
    covered = fixtures | special
    gaps = sorted(set(TOOL_MAP) - covered)
    check(not gaps, f"every tool can be simulated ({len(covered)} covered)",
          f"no fixture: {gaps}\nIn debug mode these return an empty result, the "
          f"validator calls the plan incomplete, and the turn burns every "
          f"iteration for nothing." if gaps else "")
except Exception as e:
    check(False, "dry-run fixtures inspectable", f"{type(e).__name__}: {e}")


# ── 5. Every tool is classified read or write ───────────────────────────────
section("5. Read / write classification")
try:
    reads = set(getattr(ex, "_READ_TOOLS", set()))
    writes = set(getattr(ex, "_WRITE_TOOLS", set()))
    both = reads & writes
    check(not both, "no tool is both a read and a write", f"in both: {sorted(both)}")
    unclassified = sorted(set(TOOL_MAP) - reads - writes)
    check(not unclassified, "every tool is classified",
          f"unclassified: {unclassified}\nAn unclassified tool is treated as a "
          f"READ, so a replan may run it twice. For a write that means doing it "
          f"twice for real." if unclassified else "")
except Exception as e:
    check(False, "classification lists readable", f"{type(e).__name__}: {e}")


# ── 6. The HTTP surface builds, and its request models are coherent ─────────
section("6. HTTP surface")
try:
    import fastapi  # noqa: F401
except ImportError:
    skip("the FastAPI app builds", "fastapi is not installed in this environment")
else:
    try:
        from api.app import create_app
        app = create_app()
        routes = [getattr(r, "path", "") for r in getattr(app, "routes", [])]
        check(bool(routes), f"the FastAPI app builds ({len(routes)} routes)")
        for must in ("/chat", "/api/projects"):
            check(any(r == must or r.startswith(must) for r in routes),
                  f"route exists: {must}")
    except Exception as e:
        check(False, "the FastAPI app builds", f"{type(e).__name__}: {e}")

try:
    import api.schemas as schemas
    from pydantic import BaseModel
    models = [(n, o) for n, o in vars(schemas).items()
              if inspect.isclass(o) and issubclass(o, BaseModel) and o is not BaseModel]
    bad = []
    for name, model in models:
        req = [f for f, info in model.model_fields.items() if info.is_required()] \
            if hasattr(model, "model_fields") else []
        for f in req:
            if f.startswith("_"):
                bad.append(f"{name}.{f} is required and starts with '_'")
    check(not bad, f"request models are coherent ({len(models)} models)",
          "\n".join(bad) if bad else "")
except ImportError as e:
    skip("request models are coherent", f"{e}")
except Exception as e:
    check(False, "api.schemas imports", f"{type(e).__name__}: {e}")


# ── Result ──────────────────────────────────────────────────────────────────
print()
if _failures:
    print(f"{len(_failures)} contract(s) broken:")
    for f in _failures:
        print(f"  - {f}")
    sys.exit(1)
print("All contracts hold.")
sys.exit(0)
