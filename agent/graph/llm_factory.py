"""Per-node LLM factory for the graph (also reusable outside the graph).

Configuration via environment variables, format "provider:model":
  - NODE_LLM_CONTEXT_RESOLVER, NODE_LLM_PLANNER, NODE_LLM_VALIDATOR, NODE_LLM_NARRATOR
  - Supported providers: bedrock | anthropic | gemini
  - E.g.: NODE_LLM_NARRATOR=bedrock:us.anthropic.claude-haiku-4-5-20251001-v1:0
        (split on the FIRST ':' — Bedrock IDs contain ':')

Without an override, all nodes use the default provider (same logic as
agent/llm.py: gemini if GEMINI_API_KEY is set, else anthropic if key is set,
else bedrock) with its default model. We default ALL nodes to the same
Sonnet model already enabled in prod; the lighter per-node models get
activated via env once they're validated in the account.

Provider imports are lazy: only the selected provider's package is imported
(keeps the Lambda image size in check).

IMPORTANT: this module does not import anything from the graph — api/services
can use it directly (e.g. AI-generated tasks with structured output).
"""
import os
from typing import List, Optional

# Reuses the same constants/semantics as agent/llm.py so the system
# configuration lives in a single place.
from agent.llm import (
    ANTHROPIC_API_KEY, ANTHROPIC_MODEL,
    BEDROCK_REGION, MODEL_ID,
    GEMINI_API_KEY, GEMINI_MODEL,
    LLM_PROVIDER,
)

PROVIDERS = ("bedrock", "anthropic", "gemini")

_DEFAULT_MODELS = {
    "bedrock": MODEL_ID,
    "anthropic": ANTHROPIC_MODEL,
    "gemini": GEMINI_MODEL,
}

NODE_NAMES = ("context_resolver", "planner", "validator", "narrator")

# Per-node temperatures (preserve those of the previous agent)
_NODE_TEMPERATURE = {
    "context_resolver": 0.0,
    "planner": 0.2,
    "validator": 0.1,
    "narrator": 0.4,
}


class NodeLLM:
    """Wraps a primary LLM + fallbacks while preserving the chat interface.

    LangChain's `.with_fallbacks()` returns a generic Runnable WITHOUT
    `.with_structured_output()`, so we compose the schema on each provider
    BEFORE chaining the fallbacks (composition risk documented in the plan).
    """

    def __init__(self, primary, fallbacks: Optional[list] = None):
        self._primary = primary
        self._fallbacks = fallbacks or []
        self._plain = (
            primary.with_fallbacks(self._fallbacks) if self._fallbacks else primary
        )

    def invoke(self, input, **kwargs):
        return self._plain.invoke(input, **kwargs)

    def with_structured_output(self, schema, **kwargs):
        structured = self._primary.with_structured_output(schema, **kwargs)
        if self._fallbacks:
            structured = structured.with_fallbacks(
                [f.with_structured_output(schema, **kwargs) for f in self._fallbacks]
            )
        return structured


def _parse_config(value: str) -> tuple:
    """Parses 'provider:model' splitting on the FIRST ':' (Bedrock IDs contain ':')."""
    raw = (value or "").strip()
    if not raw:
        return None, None
    if ":" in raw:
        provider, model = raw.split(":", 1)
        provider = provider.strip().lower()
        if provider in PROVIDERS:
            return provider, model.strip()
    # Provider only, no explicit model
    if raw.lower() in PROVIDERS:
        return raw.lower(), None
    raise ValueError(
        f"Invalid LLM config: '{value}'. Use 'provider:model' with provider in {PROVIDERS}"
    )


def _create_llm(provider: str, model: str, temperature: float, max_tokens: int = 4096):
    """Creates the provider's chat model (lazy per-provider import)."""
    if provider == "bedrock":
        from langchain_aws import ChatBedrockConverse
        return ChatBedrockConverse(
            model=model,
            region_name=BEDROCK_REGION,
            temperature=temperature,
            max_tokens=max_tokens,
        )
    if provider == "anthropic":
        from langchain_anthropic import ChatAnthropic
        return ChatAnthropic(
            model=model,
            api_key=ANTHROPIC_API_KEY,
            temperature=temperature,
            max_tokens=max_tokens,
            timeout=60,
            max_retries=2,
        )
    if provider == "gemini":
        # Our own HTTP adapter (no langchain-google-genai, which is incompatible
        # with langgraph 1.x). See agent/graph/gemini_adapter.py for details.
        from agent.graph.gemini_adapter import ChatGeminiHTTP
        return ChatGeminiHTTP(
            model=model,
            api_key=GEMINI_API_KEY,
            temperature=temperature,
            max_tokens=max_tokens,
        )
    raise ValueError(f"Unknown provider: {provider}")


def _gemini_available() -> bool:
    """Gemini is available if there is an API key. The ChatGeminiHTTP adapter
    is native to this repo, so it does not depend on any external package."""
    return bool(GEMINI_API_KEY)


def _default_provider() -> str:
    """Default provider, degrading if the auto-selected one isn't usable.

    LLM_PROVIDER inherits the auto-selection from agent/llm.py. We used to
    have to degrade gemini→bedrock when langchain-google-genai wasn't
    installed. Since we have ChatGeminiHTTP (our own adapter), Gemini works
    without an external package as long as GEMINI_API_KEY is set.
    """
    provider = LLM_PROVIDER if LLM_PROVIDER in PROVIDERS else "bedrock"
    if provider == "gemini" and not _gemini_available():
        # Only degrade if there is LITERALLY no GEMINI_API_KEY.
        fallback = "anthropic" if ANTHROPIC_API_KEY else "bedrock"
        print(f"[llm_factory] ⚠️ default 'gemini' without GEMINI_API_KEY → using '{fallback}'")
        return fallback
    return provider


def _available_fallback_providers(primary: str) -> List[str]:
    """Usable alternative providers (with credentials/package available)."""
    candidates = []
    for p in PROVIDERS:
        if p == primary:
            continue
        if p == "anthropic" and not ANTHROPIC_API_KEY:
            continue
        if p == "gemini":
            if not GEMINI_API_KEY:
                continue
            # We no longer probe langchain_google_genai: we use our own
            # ChatGeminiHTTP (always available if the key is set).
        # bedrock: uses IAM credentials from the environment; we consider it available
        candidates.append(p)
    return candidates


def create_llm(node: str = "narrator", env_value: str = "", temperature: float = None,
               max_tokens: int = 4096, with_fallbacks: bool = True) -> NodeLLM:
    """Creates the NodeLLM for a graph node (or an external factory consumer)."""
    provider, model = _parse_config(
        env_value or os.environ.get(f"NODE_LLM_{node.upper()}", "")
    )
    provider = provider or _default_provider()
    model = model or _DEFAULT_MODELS[provider]
    temp = temperature if temperature is not None else _NODE_TEMPERATURE.get(node, 0.2)

    primary = _create_llm(provider, model, temp, max_tokens)
    fallbacks = []
    if with_fallbacks:
        for p in _available_fallback_providers(provider):
            try:
                fallbacks.append(_create_llm(p, _DEFAULT_MODELS[p], temp, max_tokens))
            except Exception as e:
                print(f"[llm_factory] fallback {p} not available: {e}")
    # Which provider actually serves each node, recorded at startup. Reading
    # this in CloudWatch answers "is prod on Gemini?" -- the question that took
    # half an hour to answer by deduction after the 2026-09-16 incident.
    try:
        from agent import obs
        obs.log("llm_configured", node=node, provider=provider, model=model,
                fallbacks=[f"{p}" for p in _available_fallback_providers(provider)])
    except Exception:
        pass
    return NodeLLM(primary, fallbacks)


def create_node_llms() -> dict:
    """Creates LLMs for the 4 graph nodes (keys = node names)."""
    llms = {}
    for node in NODE_NAMES:
        llms[node] = create_llm(node)
    return llms
