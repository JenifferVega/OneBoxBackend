#!/usr/bin/env python3
"""Runs the service layer -- the REST side -- with the database and the LLM faked.

    python tests/test_smoke_services.py

The agent's tools are covered by test_smoke_tools.py. This covers the other
half of the system: the functions behind the project wizard and the REST API,
which no test touched until a user hit

    UnboundLocalError: local variable 'participants' referenced before assignment

on every project created from a document. That function is called here.

Same rule as the tool smoke test: a function that raises HTTPException or
returns an error has RUN, and passes. Only a crash in the code itself fails.
A function whose arguments cannot be built is reported as SKIP -- never as a
pass, because nothing was verified.
"""
import inspect
import os
import sys
import traceback
from unittest import mock

os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class FakeTable:
    def __init__(self, name):
        self.name = name

    def get_item(self, **kw):
        return {"Item": {**kw.get("Key", {}), "name": "Fake Project",
                         "userId": "test-uid", "status": "active",
                         "participants": [], "channels": [],
                         "description": "fake", "tasks": [],
                         "createdAt": "2026-01-01T00:00:00"}}

    def query(self, **kw):
        return {"Items": [], "Count": 0}

    def scan(self, **kw):
        return {"Items": [], "Count": 0}

    def put_item(self, **kw):
        return {}

    def update_item(self, **kw):
        return {"Attributes": {}}

    def delete_item(self, **kw):
        return {}

    def batch_writer(self):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class FakeDynamo:
    def Table(self, name):
        return FakeTable(name)


mock.patch("boto3.resource", return_value=FakeDynamo()).start()
mock.patch("boto3.client", return_value=mock.MagicMock()).start()
_r = mock.MagicMock(); _r.status_code = 200; _r.json.return_value = {}; _r.text = "{}"
for _v in ("get", "post", "put", "delete"):
    mock.patch(f"requests.{_v}", return_value=_r).start()

# ONE entry point for every LLM call in the service layer, so one patch covers
# them all. The answer is shaped like a real one so the parsing runs for real.
_LLM_ANSWER = """{
  "executive_summary": "A fake summary.",
  "characterization": "Fake.",
  "client_profile": "Fake.",
  "key_insight": "Fake.",
  "insights": [],
  "tasks": [],
  "participants": [],
  "suggested_name": "Fake Project",
  "suggested_type": "Other",
  "description": "Fake."
}"""
try:
    mock.patch("agent.llm.call_llm", return_value=_LLM_ANSWER).start()
except Exception as e:
    print(f"  (could not patch agent.llm.call_llm: {e})")

PASS, FAIL, SKIP = "  PASS ", "  FAIL ", "  SKIP "
failures, skipped, passed = [], [], []

CRASH = (TypeError, NameError, UnboundLocalError, AttributeError,
         IndexError, ZeroDivisionError, RecursionError)


def value_for(name, param):
    n = name.lower()
    ann = param.annotation
    if ann is bool or isinstance(param.default, bool):
        return False
    if ann is int or isinstance(param.default, int):
        return 0
    if ann is list or isinstance(param.default, list):
        return []
    if ann is dict or isinstance(param.default, dict):
        return {}
    if "email" in n:
        return "someone@example.com"
    if "phone" in n:
        return "+50400000000"
    if n.endswith("_id") or n in ("uid", "user_id"):
        return "test-id"
    if "date" in n:
        return "2026-12-31"
    if n in ("participants", "channels", "tasks", "emails", "phones", "lists"):
        return []
    if n == "participants_count":
        return 0
    return "test"


def buildable(fn):
    """Args for a plain-argument function, or None if it needs a request model
    we cannot fabricate. Returning None means SKIP, never a silent pass."""
    args = {}
    for name, p in inspect.signature(fn).parameters.items():
        if p.kind in (p.VAR_POSITIONAL, p.VAR_KEYWORD):
            continue
        if p.default is not inspect.Parameter.empty:
            continue                      # optional: leave the real default
        ann = p.annotation
        if inspect.isclass(ann) and hasattr(ann, "model_fields"):
            return None                   # a Pydantic request: not our job here
        if name.lower() in ("req", "request", "body", "payload"):
            return None
        args[name] = value_for(name, p)
    return args


def smoke(label, fn):
    args = buildable(fn)
    if args is None:
        skipped.append((label, "takes a request model"))
        print(f"{SKIP} {label}  (takes a request model)")
        return
    try:
        fn(**args)
    except CRASH as e:
        failures.append((label, f"{type(e).__name__}: {e}", traceback.format_exc()))
        print(f"{FAIL} {label}")
        return
    except Exception:
        pass                              # ran and refused: that is working code
    passed.append(label)
    print(f"{PASS} {label}")


TARGETS = [
    ("agent.project_helpers", ["generate_insights_for_project", "create_project_full"]),
    ("api.services.projects", ["list_projects", "update_project", "invite_user",
                               "remove_participant", "update_participants",
                               "normalize_participants"]),
    ("api.services.tasks", ["list_tasks", "create_task", "update_task", "delete_task"]),
    ("api.services.documents", None),     # None = every public function
    ("api.services.trello", None),
]

print("\nService layer, database and LLM faked\n")
for modname, names in TARGETS:
    try:
        mod = __import__(modname, fromlist=["*"])
    except Exception as e:
        print(f"{SKIP} {modname}  (import failed: {type(e).__name__}: {e})")
        skipped.append((modname, f"import failed: {e}"))
        continue
    chosen = names or [n for n, o in vars(mod).items()
                       if inspect.isfunction(o) and not n.startswith("_")
                       and o.__module__ == mod.__name__]
    for n in chosen:
        fn = getattr(mod, n, None)
        if not inspect.isfunction(fn):
            continue
        smoke(f"{modname.split('.')[-1]}.{n}", fn)

print()
if failures:
    print(f"{len(failures)} crash(es):\n")
    for label, msg, tb in failures:
        print(f"── {label}")
        print(f"   {msg}")
        for line in tb.strip().splitlines()[-7:]:
            print(f"   {line}")
        print()
    sys.exit(1)

print(f"{len(passed)} function(s) ran without crashing, "
      f"{len(skipped)} not verified (see SKIP above).")
sys.exit(0)
