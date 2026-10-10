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

# Ollama Cloud: open-weight models (gpt-oss, deepseek, glm, kimi, qwen...)
# billed per token. Never a FALLBACK by itself: it only runs a node that
# NODE_LLM_<NODE> or LLM_PROVIDER_ALL explicitly routes to it, because a model
# nobody measured must not silently answer for one that failed.
OLLAMA_API_KEY = os.environ.get("OLLAMA_API_KEY", "")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "gpt-oss:120b")

PROVIDERS = ("bedrock", "anthropic", "gemini", "ollama-cloud")

_DEFAULT_MODELS = {
    "bedrock": MODEL_ID,
    "anthropic": ANTHROPIC_MODEL,
    "gemini": GEMINI_MODEL,
    "ollama-cloud": OLLAMA_MODEL,
}

NODE_NAMES = ("context_resolver", "planner", "validator", "narrator")

# Output budget per call. Billed per token USED, not per token allowed, so a
# larger ceiling costs nothing until it is needed -- and on Gemini it is not
# just the answer that has to fit: thought tokens are charged against this
# same limit, which is how a planner answer came back truncated on
# 2026-09-21 and empty on 2026-09-22. 4096 was too close to the edge.
_MAX_TOKENS = 8192

# Per-node temperatures (preserve those of the previous agent)
_NODE_TEMPERATURE = {
    "context_resolver": 0.0,
    "planner": 0.2,
    "validator": 0.1,
    "narrator": 0.4,
}


def _attributed(runnable, provider: str, model: str, node: str):
    """Wraps one provider's runnable so that USING it leaves a trace.

    LangChain's .with_fallbacks() switches provider on an exception and says
    nothing at all. On 2026-09-22 that meant every planner call in a 10-minute
    session was served by a fallback while the logs looked healthy: Gemini was
    failing 8 times out of 12 and the only visible symptom was the turn taking
    143 seconds. A fallback that is invisible is not a safety net, it is a
    blindfold -- and it makes "which provider should run which node?"
    unanswerable, because no measurement can tell which one answered.
    """
    import time

    from langchain_core.runnables import RunnableLambda

    def _run(x):
        t0 = time.perf_counter()
        try:
            out = runnable.invoke(x)
        except Exception as e:
            ms = int((time.perf_counter() - t0) * 1000)
            print(f"   [LLM] {node}: {provider} FAILED after {ms} ms -> "
                  f"{type(e).__name__}: {str(e)[:160]}")
            try:
                from agent import obs
                obs.log("llm_attempt", node=node, provider=provider, model=model,
                        ok=False, ms=ms, error=f"{type(e).__name__}: {str(e)[:200]}")
            except Exception:
                pass
            raise
        ms = int((time.perf_counter() - t0) * 1000)
        print(f"   [LLM] {node}: answered by {provider} ({model}) in {ms} ms")
        try:
            from agent import obs
            obs.log("llm_attempt", node=node, provider=provider, model=model,
                    ok=True, ms=ms)
        except Exception:
            pass
        return out

    return RunnableLambda(_run)


class NodeLLM:
    """Wraps a primary LLM + fallbacks while preserving the chat interface.

    LangChain's `.with_fallbacks()` returns a generic Runnable WITHOUT
    `.with_structured_output()`, so we compose the schema on each provider
    BEFORE chaining the fallbacks (composition risk documented in the plan).
    """

    def __init__(self, primary, fallbacks: Optional[list] = None,
                 node: str = "?", names: Optional[list] = None):
        self._primary = primary
        self._fallbacks = fallbacks or []
        self._node = node
        # [(provider, model)] for primary + each fallback, in the same order.
        self._names = names or [("?", "?")] * (1 + len(self._fallbacks))
        self._plain = self._chain([primary] + self._fallbacks)

    def _chain(self, runnables):
        """primary + fallbacks, each one attributed."""
        wrapped = [
            _attributed(r, self._names[i][0] if i < len(self._names) else "?",
                        self._names[i][1] if i < len(self._names) else "?",
                        self._node)
            for i, r in enumerate(runnables)
        ]
        return wrapped[0].with_fallbacks(wrapped[1:]) if len(wrapped) > 1 else wrapped[0]

    def invoke(self, input, **kwargs):
        return self._plain.invoke(input, **kwargs)

    def with_structured_output(self, schema, **kwargs):
        return self._chain(
            [self._primary.with_structured_output(schema, **kwargs)]
            + [f.with_structured_output(schema, **kwargs) for f in self._fallbacks]
        )


# ── Which provider runs which node ────────────────────────────────────────
#
# Measured over two days of live testing, not guessed:
#
#   planner            13.973 tokens of prompt, output is a NESTED structure
#                      (a list of steps, each with an open params object of 43
#                      possible keys), zero Literal anchors, and it is the only
#                      node whose output is EXECUTED -- a mistake here becomes a
#                      WhatsApp sent or a card created in a real Trello board.
#                      On gemini-2.5-flash it failed 8 of 12 calls (MAX_TOKENS
#                      after emitting 28k-40k chars); on gemini-2.5-pro it
#                      silently marked the WRONG task as done. On anthropic it
#                      ran a full test session -- multi-step plans, from_step
#                      chaining, foreach, the consent gate -- with none of that.
#
#   the other three    931-1.531 tokens of prompt, flat output anchored by
#                      Literal enums, 0 failures in 10 calls, 0.3-2.0 s each.
#                      Classification with a closed answer is where flash is
#                      both reliable and much cheaper.
#
# The model is PINNED for gemini on purpose: GEMINI_MODEL is what someone
# changes to try 2.5-pro, and that must not silently move these three nodes
# onto a model they were never measured on. anthropic is left unpinned so
# ANTHROPIC_MODEL still chooses between sonnet and haiku.
#
# Precedence: NODE_LLM_<NODE> > LLM_PROVIDER_ALL > this table > LLM_PROVIDER.
_NODE_PROVIDER = {
    "planner":          "anthropic",
    "context_resolver": "gemini:gemini-2.5-flash",
    "validator":        "gemini:gemini-2.5-flash",
    "narrator":         "gemini:gemini-2.5-flash",
}


def _checked(var: str, value: str) -> tuple:
    """_parse_config, but the error names the variable that is wrong.

    A typo here is fatal ON PURPOSE. Routing decides which model plans actions
    that write to real Trello boards and send real messages; booting with a
    provider nobody chose, because `antropic` was misspelled, is worse than
    not booting.
    """
    try:
        return _parse_config(value)
    except ValueError as e:
        raise ValueError(
            f"{var}={value!r} in your environment is not valid. {e} "
            f"Examples: {var}=anthropic  |  {var}=gemini:gemini-2.5-flash"
        ) from None


def _provider_available(provider: str) -> bool:
    if provider == "anthropic":
        return bool(ANTHROPIC_API_KEY)
    if provider == "gemini":
        return bool(GEMINI_API_KEY)
    if provider == "ollama-cloud":
        return bool(OLLAMA_API_KEY)
    return True          # bedrock uses ambient IAM credentials


def _routing_for(node: str) -> tuple:
    """(provider, model) for this node, honouring the precedence above.

    Returns (None, None) when nothing applies, so the caller falls back to the
    global default exactly as before.
    """
    # 1. Explicit per-node override.
    var = f"NODE_LLM_{node.upper()}"
    env_node = os.environ.get(var, "").strip()
    if env_node:
        return _checked(var, env_node)

    # 2. One provider for every node -- for an honest A/B, where the routing
    #    table would otherwise make "measure flash on the planner" impossible.
    env_all = os.environ.get("LLM_PROVIDER_ALL", "").strip()
    if env_all:
        return _checked("LLM_PROVIDER_ALL", env_all)

    # 3. The routing table.
    configured = _NODE_PROVIDER.get(node)
    if configured:
        provider, model = _parse_config(configured)
        if _provider_available(provider):
            return provider, model
        print(f"[llm_factory] node '{node}' is routed to '{provider}' but it has "
              f"no credentials -> falling back to the global default. This node "
              f"has NOT been measured on that provider.")
    return None, None


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


def _create_llm(provider: str, model: str, temperature: float,
                max_tokens: int = _MAX_TOKENS, timeout: int = 60):
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
            timeout=timeout,
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
            timeout=max(timeout, 120),
        )
    if provider == "ollama-cloud":
        from agent.graph.ollama_adapter import ChatOllamaCloud
        if not OLLAMA_API_KEY:
            raise RuntimeError("OLLAMA_API_KEY is not set: add it to .env to use ollama-cloud")
        return ChatOllamaCloud(
            model=model,
            api_key=OLLAMA_API_KEY,
            temperature=temperature,
            max_tokens=max_tokens,
            timeout=max(timeout, 120),
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
        if p == primary or p == "ollama-cloud":
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
               max_tokens: int = _MAX_TOKENS, with_fallbacks: bool = True,
               timeout: int = 60) -> NodeLLM:
    """`timeout` is per request. The chat graph keeps 60 s; the planning graph
    asks for more, because reading a 50k-character document and writing the
    operations takes minutes, and 60 s x 3 attempts timed out every time."""
    """Creates the NodeLLM for a graph node (or an external factory consumer)."""
    if env_value:
        provider, model = _parse_config(env_value)
    else:
        provider, model = _routing_for(node)
    provider = provider or _default_provider()
    model = model or _DEFAULT_MODELS[provider]
    temp = temperature if temperature is not None else _NODE_TEMPERATURE.get(node, 0.2)

    primary = _create_llm(provider, model, temp, max_tokens, timeout)
    fallbacks = []
    # LLM_FALLBACKS=off measures one provider honestly. With fallbacks on, a
    # provider that fails every single call still produces correct answers --
    # someone else's. You cannot compare providers through a safety net.
    if os.environ.get("LLM_FALLBACKS", "").strip().lower() in ("off", "0", "false", "no"):
        with_fallbacks = False
    if with_fallbacks:
        for p in _available_fallback_providers(provider):
            try:
                fallbacks.append(_create_llm(p, _DEFAULT_MODELS[p], temp, max_tokens, timeout))
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
    names = [(provider, model)] + [
        (p, _DEFAULT_MODELS[p]) for p in (
            _available_fallback_providers(provider) if with_fallbacks else [])]
    return NodeLLM(primary, fallbacks, node=node, names=names)


def create_node_llms() -> dict:
    """Creates LLMs for the 4 graph nodes (keys = node names)."""
    llms = {}
    for node in NODE_NAMES:
        llms[node] = create_llm(node)

    # Printed once at startup, because "LLM_PROVIDER says gemini but the
    # planner runs on anthropic" is a surprise worth removing. LLM_PROVIDER
    # has NOT stopped mattering -- it still selects the provider for
    # agent/llm.py (document analysis, insights, Gmail classification), which
    # is a different client entirely. It simply does not decide the graph.
    print("\n[llm_factory] Graph node routing:")
    for node in NODE_NAMES:
        provider, model = llms[node]._names[0]
        source = ("NODE_LLM_" + node.upper() if os.environ.get(f"NODE_LLM_{node.upper()}", "").strip()
                  else "LLM_PROVIDER_ALL" if os.environ.get("LLM_PROVIDER_ALL", "").strip()
                  else "routing table" if node in _NODE_PROVIDER
                  else "LLM_PROVIDER")
        print(f"   {node:18} {provider:10} {model:32} (from {source})")
    print(f"   LLM_PROVIDER={LLM_PROVIDER or '(auto)'} is NOT used for these four "
          f"nodes; it still selects the provider for document analysis, insights "
          f"and Gmail classification (agent/llm.py).")
    print("   Override one node with NODE_LLM_<NODE>=provider[:model], or every "
          "node at once with LLM_PROVIDER_ALL=provider[:model].\n")
    return llms
