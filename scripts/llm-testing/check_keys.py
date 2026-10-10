#!/usr/bin/env python3
"""Are the Gemini, Anthropic and Ollama Cloud API keys valid and answering?

    python scripts/llm-testing/check_keys.py              # every provider with a key in .env
    python scripts/llm-testing/check_keys.py --only ollama
    python scripts/llm-testing/check_keys.py --no-generate   # key check only: costs nothing

Two steps per provider, straight to its HTTP API (no agent code involved, so a
failure here is the key, the account or the network -- not OneBox):
  key       lists the models the key can see             (free)
  generate  one tiny completion ("Reply OK") on EVERY model .env uses with
            that provider: the provider's default (GEMINI_MODEL,
            ANTHROPIC_MODEL, OLLAMA_MODEL) plus each NODE_LLM_<NODE>   (a few tokens)

Reads the same .env as the backend. Keys are never printed, only their last
4 characters. Exit code 1 if any check fails.
"""
import argparse
import os
import sys
import time

import requests

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
TIMEOUT = 30


def _mask(key: str) -> str:
    return f"…{key[-4:]}" if key else "(not set)"


def _timed(fn):
    t0 = time.perf_counter()
    try:
        ok, detail = fn()
    except requests.RequestException as e:
        ok, detail = False, f"network: {type(e).__name__}: {str(e)[:160]}"
    except Exception as e:
        ok, detail = False, f"{type(e).__name__}: {str(e)[:160]}"
    return ok, int((time.perf_counter() - t0) * 1000), detail


def _http_error(r) -> str:
    hint = {401: "key rejected", 403: "key has no access / billing", 404: "model not found",
            429: "rate limit or no credit left"}.get(r.status_code, "")
    return f"HTTP {r.status_code}{' (' + hint + ')' if hint else ''}: {r.text[:160]}"


# ── Gemini ──────────────────────────────────────────────────────────────────

def gemini_checks(key: str, model: str):
    base = "https://generativelanguage.googleapis.com/v1beta"
    h = {"x-goog-api-key": key}

    def key_check():
        r = requests.get(f"{base}/models", headers=h, timeout=TIMEOUT)
        if r.status_code != 200:
            return False, _http_error(r)
        names = [m.get("name", "").split("/")[-1] for m in r.json().get("models", [])]
        return True, f"{len(names)} models visible; '{model}' {'available' if model in names else 'NOT in the list'}"

    def generate():
        body = {"contents": [{"role": "user", "parts": [{"text": "Reply with the single word OK."}]}],
                "generationConfig": {"maxOutputTokens": 20, "temperature": 0,
                                     **({"thinkingConfig": {"thinkingBudget": 0}} if "flash" in model else {})}}
        r = requests.post(f"{base}/models/{model}:generateContent", headers=h, json=body, timeout=TIMEOUT)
        if r.status_code != 200:
            return False, _http_error(r)
        d = r.json()
        text = "".join(p.get("text", "") for p in ((d.get("candidates") or [{}])[0].get("content") or {}).get("parts", []))
        u = d.get("usageMetadata") or {}
        return "ok" in text.lower(), f"{text.strip()[:30]!r} · tokens {u.get('promptTokenCount', 0)} in / {u.get('candidatesTokenCount', 0)} out"
    return key_check, generate


# ── Anthropic ───────────────────────────────────────────────────────────────

def anthropic_checks(key: str, model: str):
    base = "https://api.anthropic.com/v1"
    h = {"x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"}

    def key_check():
        r = requests.get(f"{base}/models", headers=h, params={"limit": 100}, timeout=TIMEOUT)
        if r.status_code != 200:
            return False, _http_error(r)
        names = [m.get("id", "") for m in r.json().get("data", [])]
        return True, f"{len(names)} models visible; '{model}' {'available' if model in names else 'NOT in the list'}"

    def generate():
        body = {"model": model, "max_tokens": 10,
                "messages": [{"role": "user", "content": "Reply with the single word OK."}]}
        r = requests.post(f"{base}/messages", headers=h, json=body, timeout=TIMEOUT)
        if r.status_code != 200:
            return False, _http_error(r)
        d = r.json()
        text = "".join(c.get("text", "") for c in d.get("content", []) if c.get("type") == "text")
        u = d.get("usage") or {}
        return "ok" in text.lower(), f"{text.strip()[:30]!r} · tokens {u.get('input_tokens', 0)} in / {u.get('output_tokens', 0)} out"
    return key_check, generate


# ── Ollama Cloud ────────────────────────────────────────────────────────────

def ollama_checks(key: str, model: str):
    base = os.environ.get("OLLAMA_BASE_URL", "https://ollama.com").rstrip("/")
    h = {"Authorization": f"Bearer {key}"}

    def key_check():
        r = requests.get(f"{base}/api/tags", headers=h, timeout=TIMEOUT)
        if r.status_code != 200:
            return False, _http_error(r)
        names = [m.get("name") or m.get("model") or "" for m in r.json().get("models", [])]
        return True, f"{len(names)} models visible; '{model}' {'available' if model in names else 'NOT in the list'}"

    def generate():
        # /api/tags answers without a valid key on some setups; a completion
        # is what proves the key and the account's credit.
        body = {"model": model, "stream": False,
                "messages": [{"role": "user", "content": "Reply with the single word OK."}],
                "options": {"temperature": 0, "num_predict": 200}}
        r = requests.post(f"{base}/api/chat", headers=h, json=body, timeout=max(TIMEOUT, 90))
        if r.status_code != 200:
            return False, _http_error(r)
        d = r.json()
        text = (d.get("message") or {}).get("content") or ""
        return "ok" in text.lower(), (f"{text.strip()[:30]!r} · tokens {d.get('prompt_eval_count', 0)} in / "
                                      f"{d.get('eval_count', 0)} out · done_reason={d.get('done_reason')}")
    return key_check, generate


_PROVIDER_PREFIX = {"gemini": "gemini", "anthropic": "anthropic", "ollama": "ollama-cloud"}


def _models_for(name: str, default: str) -> list:
    """The provider's default model plus every model a NODE_LLM_* routes to it."""
    models = [default]
    for var, value in sorted(os.environ.items()):
        if not var.startswith("NODE_LLM_") and var not in ("LLM_PROVIDER_ALL", "PLANNING_LLM"):
            continue
        prov, _, model = value.strip().partition(":")
        if prov.lower() == _PROVIDER_PREFIX[name] and model and model not in models:
            models.append(model)
    return models


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--only", default="", help="gemini | anthropic | ollama (comma-separated)")
    ap.add_argument("--no-generate", action="store_true", help="only validate the keys (free)")
    args = ap.parse_args()
    try:
        from dotenv import load_dotenv
        load_dotenv(os.path.join(REPO, ".env"))
    except ImportError:
        print("python-dotenv is not installed: using this shell's environment only")

    providers = [
        ("gemini", "GEMINI_API_KEY", os.environ.get("GEMINI_MODEL", "gemini-2.5-flash"), gemini_checks),
        ("anthropic", "ANTHROPIC_API_KEY", os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-5-20250929"),
         anthropic_checks),
        ("ollama", "OLLAMA_API_KEY", os.environ.get("OLLAMA_MODEL", "gpt-oss:120b"), ollama_checks),
    ]
    only = {p.strip() for p in args.only.split(",") if p.strip()}

    print(f"\n{'provider':10} {'key':10} {'model':30} {'check':9} {'ok':4} {'ms':>6}  detail")
    print("-" * 110)
    failed = 0
    for name, var, model, make in providers:
        if only and name not in only:
            continue
        key = os.environ.get(var, "").strip()
        if not key:
            print(f"{name:10} {_mask(key):10} {model[:30]:30} {'key':9} {'--':4} {'':>6}  {var} is not set in .env")
            failed += 1
            continue
        ok, ms, detail = _timed(make(key, model)[0])
        failed += not ok
        print(f"{name:10} {_mask(key):10} {'(listing)':30} {'key':9} {'yes' if ok else 'NO':4} {ms:>6}  {detail}")
        if not ok or args.no_generate:
            continue                       # no point generating with a rejected key
        for m in _models_for(name, model):
            ok, ms, detail = _timed(make(key, m)[1])
            failed += not ok
            print(f"{'':10} {'':10} {m[:30]:30} {'generate':9} {'yes' if ok else 'NO':4} {ms:>6}  {detail}")
    print("-" * 110)
    print("ALL KEYS OK" if not failed else f"{failed} check(s) FAILED")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
