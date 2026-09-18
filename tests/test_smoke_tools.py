#!/usr/bin/env python3
"""Runs EVERY registered tool once, with the database and the network faked.

    python tests/test_smoke_tools.py

The contract tests check that signatures and documentation agree. They cannot
tell you a function is broken INSIDE, because they never run it. This does.

It is not checking that the answers are right -- the data is fake, so they
cannot be. It checks that calling the function does not CRASH: no
UnboundLocalError, no missing attribute, no bad call. That is exactly the
class that produced a 500 on every project created from a document, sat in
the code for weeks, and was invisible to every test we had, because the only
tests we had drove the chat agent and the bug was on the REST side.

A tool that returns an error dict, or raises HTTPException, PASSES: it ran,
looked at the (fake) data, and refused politely. That is working code.
"""
import os
import sys
import traceback
from unittest import mock

os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# ── Fake AWS, installed BEFORE agent.tools is imported ──────────────────────
# The tables are module-level objects that other modules import BY NAME
# (`from agent.tools.db import projects_table`), so each module keeps its own
# reference. Patching after the fact would miss them. Patching boto3 itself,
# before the first import, cannot be missed.


class FakeTable:
    """Answers with the shapes DynamoDB really returns, so the code under test
    takes its normal paths instead of dying on a Mock."""

    def __init__(self, name):
        self.name = name

    def get_item(self, **kw):
        key = kw.get("Key", {})
        return {"Item": {**key, "name": "Fake Project", "userId": "test-uid",
                         "status": "active", "participants": [],
                         "channels": [], "description": "fake",
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


_boto_patch = mock.patch("boto3.resource", return_value=FakeDynamo())
_boto_patch.start()
_client_patch = mock.patch("boto3.client", return_value=mock.MagicMock())
_client_patch.start()

_resp = mock.MagicMock()
_resp.status_code = 200
_resp.json.return_value = {"emails": [], "messages": [], "boards": []}
_resp.text = "{}"
for _verb in ("get", "post", "put", "delete"):
    mock.patch(f"requests.{_verb}", return_value=_resp).start()

try:
    from agent.tools import TOOL_MAP, set_current_user
except Exception as e:
    print(f"  FAIL  agent.tools imports: {type(e).__name__}: {e}")
    traceback.print_exc()
    sys.exit(1)

set_current_user("test-uid", "test@example.com")

# ── Plausible values, derived from the parameter NAME ────────────────────────
# Hand-writing arguments for 29 tools would rot the moment a signature moves.
# Deriving them from the name keeps this test honest as the code changes.
import inspect  # noqa: E402


def value_for(name: str, param):
    n = name.lower()
    if param.annotation is bool or isinstance(param.default, bool):
        return True
    if param.annotation is int or isinstance(param.default, int):
        return 1
    if param.annotation is list or isinstance(param.default, list) or n.endswith("s") and n in (
            "participants", "channels", "lists", "tasks", "emails", "phones"):
        return []
    if "email" in n:
        return "someone@example.com"
    if "phone" in n or "recipient" in n:
        return "+50400000000"
    if n.endswith("_id") or n in ("project_id", "task_id", "list_id", "board_id"):
        return "test-id"
    if "date" in n:
        return "2026-12-31"
    if n in ("query", "name", "text", "title", "message", "subject", "body"):
        return "test"
    return "test"


def build_args(fn, include_optional: bool):
    args = {}
    for name, p in inspect.signature(fn).parameters.items():
        if p.kind in (p.VAR_POSITIONAL, p.VAR_KEYWORD):
            continue
        required = p.default is inspect.Parameter.empty
        if required or include_optional:
            args[name] = value_for(name, p)
    return args


# Exceptions that mean THE CODE is wrong, not that the data was fake.
CRASH = (TypeError, NameError, UnboundLocalError, AttributeError,
         IndexError, ZeroDivisionError, RecursionError)

failures = []


def run(tool, fn, include_optional):
    label = "all params" if include_optional else "required only"
    try:
        args = build_args(fn, include_optional)
    except Exception as e:
        failures.append((tool, label, f"cannot read the signature: {e}", ""))
        return False
    try:
        out = fn(**args)
    except CRASH as e:
        failures.append((tool, label, f"{type(e).__name__}: {e}",
                         traceback.format_exc()))
        return False
    except Exception:
        # HTTPException, botocore errors, anything the function raises on
        # purpose about the data: the code RAN. That is what we are testing.
        return True
    if out is not None and not isinstance(out, dict):
        failures.append((tool, label,
                         f"returned {type(out).__name__}, the executor expects a dict", ""))
        return False
    return True


print(f"\nRunning {len(TOOL_MAP)} tools, twice each (minimum call and full call)\n")
ok_count = 0
for tool in sorted(TOOL_MAP):
    fn = TOOL_MAP[tool]
    a = run(tool, fn, include_optional=False)
    b = run(tool, fn, include_optional=True)
    if a and b:
        ok_count += 1
        print(f"  PASS  {tool}")
    else:
        print(f"  FAIL  {tool}")

print()
if failures:
    print(f"{len(failures)} crash(es):\n")
    for tool, label, msg, tb in failures:
        print(f"── {tool}  ({label})")
        print(f"   {msg}")
        if tb:
            for line in tb.strip().splitlines()[-6:]:
                print(f"   {line}")
        print()
    sys.exit(1)

print(f"All {ok_count} tools ran without crashing.")
sys.exit(0)
