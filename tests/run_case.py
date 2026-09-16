#!/usr/bin/env python3
"""Run a JSON conversation case against a running OneBox backend.

    python tests/run_case.py tests/cases/trello_create_board_and_export.json

Environment:
    ONEBOX_URL    default http://localhost:8000
    ONEBOX_UID    x-user-id header (required)
    ONEBOX_EMAIL  x-user-email header

An assertion the run cannot OBSERVE is reported as SKIPPED, never as passed.
A harness that quietly passes what it did not check is worse than no harness:
it certifies behaviour nobody verified. `results` assertions need debug=true,
because that is the only mode in which the endpoint returns per-tool output.
"""
import json
import os
import re
import sys
import urllib.error
import urllib.request

URL = os.environ.get("ONEBOX_URL", "http://localhost:8000").rstrip("/")
UID = os.environ.get("ONEBOX_UID", "")
EMAIL = os.environ.get("ONEBOX_EMAIL", "")

PASS, FAIL, SKIP = "PASS", "FAIL", "SKIP"
_ICON = {PASS: "  PASS ", FAIL: "  FAIL ", SKIP: "  SKIP "}


def _post(path, payload):
    req = urllib.request.Request(
        f"{URL}{path}",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json",
                 "x-user-id": UID, "x-user-email": EMAIL},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=180) as r:
        return json.loads(r.read().decode("utf-8"))


def _delete(path):
    req = urllib.request.Request(f"{URL}{path}", method="DELETE",
                                 headers={"x-user-id": UID})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return {"cleared": False, "status": e.code}


def _calls(reply):
    """[(tool, result)] for this turn, or None when the run cannot see them."""
    info = reply.get("debug_info") or {}
    sim = info.get("simulated_calls")
    if sim is None:
        return None
    return [(c.get("tool", ""), c.get("simulated_result") or {}) for c in sim]


_OPS = {
    "eq": lambda a, b: a == b,
    "ne": lambda a, b: a != b,
    "gt": lambda a, b: isinstance(a, (int, float)) and a > b,
    "gte": lambda a, b: isinstance(a, (int, float)) and a >= b,
    "lt": lambda a, b: isinstance(a, (int, float)) and a < b,
    "contains": lambda a, b: b in (a or ""),
}


def check_turn(spec, reply):
    """-> [(status, text)]"""
    out = []
    a = spec.get("assert") or {}
    tools = reply.get("toolsUsed") or []
    text = reply.get("response") or ""
    calls = _calls(reply)

    for t in a.get("tools_used_includes", []):
        out.append((PASS if t in tools else FAIL, f"used {t}"))

    for t in a.get("tools_used_excludes", []):
        out.append((PASS if t not in tools else FAIL, f"did NOT use {t}"))

    for t, n in (a.get("tool_call_count") or {}).items():
        got = tools.count(t)
        out.append((PASS if got == n else FAIL, f"{t} called {n}x (got {got})"))

    for pat in a.get("response_matches", []):
        ok = re.search(pat, text) is not None
        out.append((PASS if ok else FAIL, f"reply matches /{pat}/"))

    for pat in a.get("response_not_matches", []):
        ok = re.search(pat, text) is None
        out.append((PASS if ok else FAIL, f"reply does NOT match /{pat}/"))

    for r in a.get("results", []):
        label = f"{r['tool']}.{r['field']} {r['op']} {r['value']!r}"
        if calls is None:
            out.append((SKIP, f"{label}  (needs debug=true; tool output not returned)"))
            continue
        hits = [res for tool, res in calls if tool == r["tool"]]
        if not hits:
            out.append((FAIL, f"{label}  ({r['tool']} was never called)"))
            continue
        got = hits[-1].get(r["field"])
        ok = _OPS[r["op"]](got, r["value"])
        out.append((PASS if ok else FAIL, f"{label}  (got {got!r})"))

    return out


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    if not UID:
        print("ONEBOX_UID is not set: /chat answers 401 without it.")
        return 2

    case = json.load(open(sys.argv[1], encoding="utf-8"))
    session = case.get("session_id") or "case"
    debug = bool(case.get("debug", True))

    print(f"\n{case['name']}")
    print(f"{case.get('description','')}\n")
    if not debug:
        print("debug=false: per-tool assertions cannot be observed and will be "
              "reported as SKIP.\n")

    if case.get("reset_before", True):
        print(f"reset session '{session}': {_delete('/debug/cache/' + session)}\n")

    history, totals = [], {PASS: 0, FAIL: 0, SKIP: 0}
    for spec in case["turns"]:
        msg = spec["message"]
        print(f"Turn {spec['n']}: {msg!r}")
        if spec.get("why"):
            print(f"   {spec['why']}")
        try:
            reply = _post("/chat", {"message": msg, "history": history,
                                    "debug": debug, "session_id": session})
        except Exception as e:
            print(f"{_ICON[FAIL]} the request itself failed: {e}\n")
            totals[FAIL] += 1
            break

        history = history + [{"role": "user", "content": msg},
                             {"role": "assistant", "content": reply.get("response", "")}]
        print(f"   tools: {reply.get('toolsUsed') or '(none)'}")
        for status, label in check_turn(spec, reply):
            totals[status] += 1
            print(f"{_ICON[status]} {label}")
        print()

    print(f"{totals[PASS]} passed, {totals[FAIL]} failed, {totals[SKIP]} not observable")
    return 1 if totals[FAIL] else 0


if __name__ == "__main__":
    sys.exit(main())
