#!/usr/bin/env python3
"""A chaining reference must be recognised whatever shape the provider wrote it in.

    python tests/test_chain_ref.py

On 2026-09-18, running the real planner on gemini-2.5-flash, the same plan came
back differently depending on the provider:

    anthropic : 'project_id': {'from_step': 1, 'extract': 'projectId'}
    gemini    : 'project_id': '{"from_step":1,"extract":"projectId"}'

Gemini decodes under the schema, and the schema types project_id as a string
(the signature says `project_id: str`), so it serialised the object. The
resolver only looked for a dict, so the reference was never resolved and the
literal text went to the tool as an id -- a silent failure that looks like a
project that does not exist.

The other half of this test is the negative cases: a resolver that is too
eager turns a real id or a project name into a broken lookup.
"""
import os
import sys

os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent.graph.nodes.executor.resolve import _as_chain_ref, resolve_params  # noqa: E402

CASES = [
    ({"from_step": 1, "extract": "projectId"},                  "dict (anthropic)", True),
    ('{"from_step":1,"match":{"key":"type","value":"project"},"extract":"id"}',
                                                                "string (gemini, observed)", True),
    ('  {"from_step": 2, "path": "projectId"}  ',                "string, padded", True),
    ("abc-123",                                                  "a real id", False),
    ("Farmacia Haussman",                                        "a project name", False),
    ('{"name": "Alpha"}',                                        "json, but not a reference", False),
    ('{"from_step": broken',                                     "malformed json", False),
    ("",                                                         "empty string", False),
    (None,                                                       "None", False),
    (42,                                                         "an int", False),
    ({"project": "x"},                                           "dict without from_step", False),
]

failures = []
print()
for value, label, should in CASES:
    got = _as_chain_ref(value) is not None
    ok = got == should
    print(f"  {'PASS' if ok else 'FAIL'}  {label:30} recognised={got}")
    if not ok:
        failures.append(label)

# End to end: both shapes must resolve to the SAME value.
results = {1: {"projectId": "p-77", "name": "Alpha"}}
a = resolve_params({"project_id": {"from_step": 1, "extract": "projectId"}}, results)
b = resolve_params({"project_id": '{"from_step": 1, "extract": "projectId"}'}, results)
same = a == b == {"project_id": "p-77"}
print(f"  {'PASS' if same else 'FAIL'}  both shapes resolve to the same id")
if not same:
    failures.append(f"end to end: dict={a} string={b}")

# And a plain value must survive untouched.
plain = resolve_params({"project_id": "abc-123", "text": "revisar"}, results)
kept = plain == {"project_id": "abc-123", "text": "revisar"}
print(f"  {'PASS' if kept else 'FAIL'}  plain values are left alone")
if not kept:
    failures.append(f"plain values changed: {plain}")

print()
if failures:
    for f in failures:
        print(f"  - {f}")
    sys.exit(1)
print("Chaining survives the provider.")
sys.exit(0)
