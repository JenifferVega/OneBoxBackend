#!/usr/bin/env python3
"""Which part of our Gemini request does a model reject?

    python scripts/llm-testing/check_gemini_request.py                       # gemini-3.5-flash-lite
    python scripts/llm-testing/check_gemini_request.py gemini-3.8-flash

Sends the same tiny request in several variants (thinking off with
thinkingBudget, thinkingLevel, JSON schema with an enum, temperature 0) and
prints OK or the error of each, so agent/graph/gemini_adapter.py can send what
the model accepts. Uses GEMINI_API_KEY from the repo's .env. Costs a fraction
of a cent.
"""
import json
import os
import sys
import urllib.error
import urllib.request

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(REPO, ".env"))
except ImportError:
    pass

KEY = os.environ.get("GEMINI_API_KEY", "")
MODEL = sys.argv[1] if len(sys.argv) > 1 else "gemini-3.5-flash-lite"
SCHEMA = {"type": "object", "properties": {
    "items": {"type": "array", "items": {"type": "object", "properties": {
        "id": {"type": "string"}, "lang": {"type": "string", "enum": ["es", "en"]},
        "translation": {"type": "string"}}}}}}
JSON = {"responseMimeType": "application/json", "responseSchema": SCHEMA}

VARIANTS = [
    ("plain", {}),
    ("temperature 0", {"temperature": 0.0}),
    ("thinkingBudget 0  (what the adapter sends on 'flash' models)", {"thinkingConfig": {"thinkingBudget": 0}}),
    ("thinkingLevel low", {"thinkingConfig": {"thinkingLevel": "low"}}),
    ("thinkingLevel minimal", {"thinkingConfig": {"thinkingLevel": "minimal"}}),
    ("JSON schema with enum", dict(JSON)),
    ("the translator's full request (temp 0 + budget 0 + schema)",
     {"temperature": 0.0, "maxOutputTokens": 8192, "thinkingConfig": {"thinkingBudget": 0}, **JSON}),
    ("same, without thinkingConfig", {"temperature": 0.0, "maxOutputTokens": 8192, **JSON}),
]


def call(gen: dict):
    body = {"contents": [{"role": "user", "parts": [{"text":
            'Translate to English: {"id": "1", "text": "Resumen del proyecto"}. Detect the language.'}]}],
            "generationConfig": gen}
    req = urllib.request.Request(
        f"https://generativelanguage.googleapis.com/v1beta/models/{MODEL}:generateContent",
        data=json.dumps(body).encode(), headers={"Content-Type": "application/json", "x-goog-api-key": KEY})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            data = json.loads(r.read())
            text = "".join(p.get("text", "") for p in
                           ((data.get("candidates") or [{}])[0].get("content") or {}).get("parts") or [])
            return "OK", text.strip().replace("\n", " ")[:90]
    except urllib.error.HTTPError as e:
        try:
            msg = json.loads(e.read()).get("error", {}).get("message", "")
        except Exception:
            msg = ""
        return f"HTTP {e.code}", msg[:160]
    except Exception as e:
        return "ERROR", str(e)[:160]


def main() -> int:
    if not KEY:
        print("GEMINI_API_KEY is not set (.env)")
        return 1
    print(f"model: {MODEL}\n")
    for name, gen in VARIANTS:
        status, detail = call(gen)
        print(f"{'✓' if status == 'OK' else '✗'} {name}\n    {status}: {detail}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
