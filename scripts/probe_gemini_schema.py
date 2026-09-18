#!/usr/bin/env python3
"""Asks the real Gemini API what it does with each schema shape. Run it once.

    python scripts/probe_gemini_schema.py

Needs GEMINI_API_KEY in .env. Makes 4 small calls (cents at most).

WHY: the claim "Gemini returns empty params because the object has no declared
properties" is a MECHANISM, inferred from the code and from what Google
documents. It has not been observed on this account. Two other outcomes are
possible and they lead to different fixes:

  * the API rejects the schema (HTTP 400) -> then the empty params in the
    2026-09-16 logs did NOT come from Gemini, because a rejected call produces
    no plan at all, and the logs show a well-formed plan with the right tool
    names and only the parameters missing. Look at Anthropic/Bedrock instead.
  * the API accepts it and returns {} -> mechanism confirmed.

Variant D is the one that matters for the fix: since 2025-11-05 Gemini
supports full JSON Schema through responseJsonSchema (a DIFFERENT field from
responseSchema), where additionalProperties is legal and nothing has to be
stripped.
"""
import json
import os
import sys
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

KEY = os.environ.get("GEMINI_API_KEY", "")
MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
if not KEY:
    sys.exit("GEMINI_API_KEY is not set")

PROMPT = ("Plan exactly one step: call the tool push_tasks_to_trello for the "
          "project whose id is abc-123. Its parameters are project_id "
          "(required), list_id, only_pending.")


def plan_schema(params_fragment):
    return {"type": "object", "properties": {"plan": {"type": "array", "items": {
        "type": "object",
        "properties": {"step": {"type": "integer"},
                       "tool": {"type": "string"},
                       "params": params_fragment},
        "required": ["step", "tool"]}}}}


def call(label, schema_field, params_fragment):
    body = {
        "contents": [{"role": "user", "parts": [{"text": PROMPT}]}],
        "generationConfig": {"responseMimeType": "application/json",
                             schema_field: plan_schema(params_fragment)},
    }
    url = (f"https://generativelanguage.googleapis.com/v1beta/models/"
           f"{MODEL}:generateContent?key={KEY}")
    req = urllib.request.Request(
        url, data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"})
    print(f"\n===== {label}")
    print(f"      field={schema_field}  params={json.dumps(params_fragment)}")
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            data = json.loads(r.read())
        text = data["candidates"][0]["content"]["parts"][0]["text"]
        print(f"  HTTP 200")
        print(f"  model returned: {text.strip()[:400]}")
        try:
            step = json.loads(text)["plan"][0]
            got = step.get("params", "<no params key>")
            print(f"  --> params = {json.dumps(got)}"
                  f"{'   <-- EMPTY' if got == {} else ''}")
        except Exception:
            pass
    except urllib.error.HTTPError as e:
        detail = e.read().decode()[:400]
        print(f"  HTTP {e.code}  <-- REJECTED")
        print(f"  {detail}")
    except Exception as e:
        print(f"  {type(e).__name__}: {e}")


print(f"model: {MODEL}")
call("A) responseSchema, params = object with NO properties "
     "(what the sanitiser produces today)",
     "responseSchema", {"type": "object", "description": "Tool parameters."})
call("B) responseSchema, params = object with additionalProperties "
     "(what pydantic emits, before stripping)",
     "responseSchema",
     {"type": "object", "description": "Tool parameters.", "additionalProperties": True})
call("C) responseSchema, params = object WITH declared properties "
     "(the change already made)",
     "responseSchema",
     {"type": "object", "description": "Parameters for this tool.",
      "properties": {"project_id": {"type": "string"},
                     "list_id": {"type": "string"},
                     "only_pending": {"type": "boolean"}}})
call("D) responseJsonSchema + additionalProperties "
     "(full JSON Schema, added by Google 2025-11-05)",
     "responseJsonSchema",
     {"type": "object", "description": "Tool parameters.", "additionalProperties": True})
print("\nRead it like this:")
print("  A rejected (400)     -> the empty params did NOT come from Gemini.")
print("  A returns {}         -> mechanism confirmed.")
print("  D works              -> the adapter can stop stripping; use that field.")
