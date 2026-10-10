"""System details for the app (Settings > System details): which model each
part of the agent really uses, and why.

Answers "is production on the model I think?" without reading the server's
env file -- the question behind the 2026-10 incident where the planner ran on
Gemini because ANTHROPIC_API_KEY was missing in production.

Never returns a secret: keys and tokens are reported only as set / missing.
Who can see it: SYSTEM_INFO_EMAILS (comma separated) when set; otherwise any
signed-in user (it only shows model names).
"""
import os
import platform
import re
from datetime import datetime, timezone
from typing import Optional

from fastapi import HTTPException

STARTED_AT = datetime.now(timezone.utc).isoformat(timespec="seconds")

# Shown with their values: model routing, never credentials.
_SHOWN = re.compile(r"^(NODE_LLM_\w+|LLM_\w+|PLANNING_LLM\w*|\w+_MODEL|MODEL_ID|BEDROCK_REGION|OLLAMA_BASE_URL"
                    r"|OLLAMA_THINK|PLANNING_ENGINE|PLANNING_UPLOADS)$")
_SECRET = re.compile(r"(KEY|SECRET|TOKEN|PASSWORD|CREDENTIAL)", re.I)
_KEYS = ("ANTHROPIC_API_KEY", "GEMINI_API_KEY", "OLLAMA_API_KEY", "TRELLO_API_KEY", "VIEW_AS_TOKEN")

_WHAT = {
    "context_resolver": "Understands which project / task / person the message is about",
    "planner": "Decides the actions (the most important node)",
    "validator": "Checks the plan before anything is done",
    "narrator": "Writes the reply to the user",
    "planning": "Replanning chat, uploads and daily consolidation (Intelligence)",
    "documents": "Document analysis, insights, Gmail classification (agent/llm.py)",
}


def can_see(email: str) -> bool:
    allowed = {e.strip().lower() for e in os.environ.get("SYSTEM_INFO_EMAILS", "").split(",") if e.strip()}
    return not allowed or (email or "").lower() in allowed


def _source(node: str) -> str:
    from agent.graph import llm_factory as F
    if os.environ.get(f"NODE_LLM_{node.upper()}", "").strip():
        return f"NODE_LLM_{node.upper()}"
    if os.environ.get("LLM_PROVIDER_ALL", "").strip():
        return "LLM_PROVIDER_ALL"
    if node in F._NODE_PROVIDER:
        return "routing table (code)"
    return "LLM_PROVIDER (default)"


def _node(node: str) -> dict:
    from agent.graph import llm_factory as F
    warning: Optional[str] = None
    code, wanted_p = "", ""
    try:
        provider, model = F._routing_for(node)
    except ValueError as e:                     # a typo in NODE_LLM_*
        return {"node": node, "what": _WHAT.get(node, ""), "error": str(e)[:300]}
    source = _source(node)
    if provider is None and node in F._NODE_PROVIDER and source.startswith("routing table"):
        wanted, _m = F._parse_config(F._NODE_PROVIDER[node])
        warning = (f"Meant to run on '{wanted}' but {wanted.upper()} credentials are missing: "
                   f"it is falling back to the default provider.")
        code, wanted_p = "fallback", wanted
        source = "default (fallback)"
    provider = provider or F._default_provider()
    model = model or F._DEFAULT_MODELS.get(provider, "?")
    if not F._provider_available(provider):
        warning = f"{provider} has no API key: calls will fail."
        code = "missing_key"
    off = os.environ.get("LLM_FALLBACKS", "").strip().lower() in ("off", "0", "false", "no")
    return {"node": node, "what": _WHAT.get(node, ""), "provider": provider, "model": model, "source": source,
            "fallbacks": [] if off else F._available_fallback_providers(provider), "warning": warning,
            # The interface translates warningCode (settings.system.warn.*); warning is the English text.
            "warningCode": code, "wanted": wanted_p}


def _planning() -> dict:
    from agent.graph import llm_factory as F
    env = os.environ.get("PLANNING_LLM", "").strip()
    if env:
        provider, model = F._parse_config(env)
        source = "PLANNING_LLM"
    else:
        provider, model = F._routing_for("planner")
        source = "same as the planner"
    provider = provider or F._default_provider()
    model = model or F._DEFAULT_MODELS.get(provider, "?")
    on = os.environ.get("PLANNING_LLM_FALLBACKS", "off").lower() in ("on", "1", "true")
    return {"node": "planning", "what": _WHAT["planning"], "provider": provider, "model": model, "source": source,
            "fallbacks": F._available_fallback_providers(provider) if on else [],
            "warning": None if F._provider_available(provider) else f"{provider} has no API key: calls will fail.",
            "warningCode": "" if F._provider_available(provider) else "missing_key"}


def _documents() -> dict:
    from agent import llm as L
    provider = getattr(L, "LLM_PROVIDER", "") or "?"
    model = {"gemini": getattr(L, "GEMINI_MODEL", ""), "anthropic": getattr(L, "ANTHROPIC_MODEL", ""),
             "bedrock": getattr(L, "MODEL_ID", "")}.get(provider, "?")
    return {"node": "documents", "what": _WHAT["documents"], "provider": provider, "model": model,
            "source": "LLM_PROVIDER", "fallbacks": [], "warning": None}


def info(email: str) -> dict:
    if not can_see(email):
        raise HTTPException(status_code=403, detail="Not allowed")
    from agent.graph import llm_factory as F
    env = {k: ("set" if v else "missing") if _SECRET.search(k) else v
           for k, v in sorted(os.environ.items()) if _SHOWN.match(k)}
    return {
        "environment": os.environ.get("APP_ENV") or os.environ.get("ENVIRONMENT") or os.environ.get("ENV") or "",
        "startedAt": STARTED_AT,
        "python": platform.python_version(),
        "nodes": [_node(n) for n in F.NODE_NAMES] + [_planning(), _documents()],
        "keys": {k: bool(os.environ.get(k)) for k in _KEYS},
        "providers": {p: F._provider_available(p) for p in F.PROVIDERS},
        "env": env,
    }
