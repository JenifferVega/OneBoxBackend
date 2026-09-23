"""LangChain-compatible adapter for Gemini using direct HTTP.

Why this file exists:
  - The agent graph (llm_factory) consumes models via the LangChain
    interface (.invoke + .with_structured_output + .with_fallbacks).
  - The official `langchain-google-genai` package CANNOT be installed
    alongside `langgraph 1.x` because it requires `langchain-core<1.0`
    (its latest release hasn't aligned with langchain-core 1.x). It is a
    conscious decision documented in requirements.txt.
  - Result: if LLM_PROVIDER=gemini, llm_factory silently fell back to
    Bedrock via `_gemini_available()` returning False. Under Bedrock
    throttling, the chatbot broke.

Solution: implement ChatGeminiHTTP that talks to Gemini's REST API
(generativelanguage.googleapis.com) meeting the minimal interface the
graph uses. It is independent of the official package and compatible
with langchain-core>=1.0.

What it DOES support:
  - invoke([SystemMessage, HumanMessage]) → AIMessage with .content
  - with_structured_output(PydanticSchema) → Runnable that returns an
    instance (using Gemini's native responseSchema to guarantee valid JSON)
  - with_fallbacks([...]) → inherited from BaseChatModel

What it does NOT support (not used in the graph):
  - Tool calling
  - Streaming
  - Function calling
"""
from __future__ import annotations

import json
import os
from typing import Any, List, Optional, Type

import requests
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.runnables import Runnable, RunnableLambda
from pydantic import BaseModel, Field

# v1beta API endpoint (the stable one for generateContent + structured output).
# The model goes in the URL ({model}) and the API key in the query string.
_GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"


def _messages_to_gemini(messages: List[BaseMessage]) -> tuple[str, list]:
    """Converts LangChain messages to the format Gemini expects.

    Gemini separates the system prompt (systemInstruction) from the
    dialog body (contents). The body alternates 'user' and 'model' roles.
    """
    system_parts: list[str] = []
    contents: list = []
    for msg in messages:
        if isinstance(msg, SystemMessage):
            system_parts.append(str(msg.content))
        elif isinstance(msg, HumanMessage):
            contents.append({"role": "user", "parts": [{"text": str(msg.content)}]})
        elif isinstance(msg, AIMessage):
            contents.append({"role": "model", "parts": [{"text": str(msg.content)}]})
        else:
            # Unknown message: treat as user so we don't lose content.
            contents.append({"role": "user", "parts": [{"text": str(msg.content)}]})
    return "\n\n".join(system_parts).strip(), contents


def _resolve_refs(schema: Any, defs: dict) -> Any:
    """Recursively inline every schema $ref with its definition.

    Pydantic generates schemas with sub-models referenced as
    `{"$ref": "#/$defs/PlannerStep"}` and the definitions in `$defs`.
    Gemini does NOT accept $ref → we substitute each $ref with the
    content of the referenced definition (also resolving nested refs).
    """
    if isinstance(schema, dict):
        if "$ref" in schema and isinstance(schema["$ref"], str):
            ref = schema["$ref"]
            # Typical formats: "#/$defs/ModelName" or "#/definitions/X"
            if ref.startswith("#/$defs/"):
                name = ref[len("#/$defs/"):]
            elif ref.startswith("#/definitions/"):
                name = ref[len("#/definitions/"):]
            else:
                # External or unknown ref: return as-is so it fails loudly
                return schema
            referenced = defs.get(name)
            if referenced is None:
                return schema
            # Recursively resolve refs inside the referenced schema too
            return _resolve_refs(referenced, defs)
        return {k: _resolve_refs(v, defs) for k, v in schema.items()}
    if isinstance(schema, list):
        return [_resolve_refs(v, defs) for v in schema]
    return schema


def _strip_unsupported_keys(schema: Any) -> Any:
    """Removes keywords Gemini does not support.

    Keeps: type, properties, items, required, description, enum, format,
    anyOf, oneOf, allOf, nullable.
    Strips keywords Gemini's responseSchema rejects:
      - title, default, $schema (metadata)
      - $defs, definitions (already inlined)
      - additionalProperties (not supported at all)
      - exclusiveMinimum/Maximum, multipleOf (limited subset)
      - pattern (complicated regex subset)
    """
    UNSUPPORTED = {
        "title", "default", "$schema", "$defs", "definitions",
        "additionalProperties", "exclusiveMinimum", "exclusiveMaximum",
        "multipleOf", "pattern", "patternProperties", "uniqueItems",
        "readOnly", "writeOnly", "examples", "const",
    }
    if isinstance(schema, dict):
        return {
            k: _strip_unsupported_keys(v)
            for k, v in schema.items()
            if k not in UNSUPPORTED
        }
    if isinstance(schema, list):
        return [_strip_unsupported_keys(v) for v in schema]
    return schema


class UnfillableSchema(Exception):
    """A field Gemini could never fill, detected before we ask it to.

    Gemini's responseSchema is a CONSTRAINED DECODER, not a hint. An object
    with no declared `properties` has no legal key, so the only output the
    grammar permits is `{}`. Stripping `additionalProperties` -- which we must,
    Gemini rejects it -- silently turns "any key is allowed" into "no key is
    allowed", and the caller gets empty objects with no error anywhere.

    That is exactly how, on 2026-09-16, a user got four turns of
    `params={}` in a row. Failing here is loud and immediate; the alternative
    is a system that quietly plans nothing.
    """


def _assert_fillable(schema, path="$"):
    """Raises if normalization left a field Gemini cannot produce a value for."""
    if isinstance(schema, dict):
        if schema.get("type") in ("object", "OBJECT"):
            has_shape = any(k in schema for k in
                            ("properties", "anyOf", "oneOf", "allOf", "enum"))
            if not has_shape:
                raise UnfillableSchema(
                    f"{path}: object with no 'properties'. Gemini decodes under "
                    f"the schema, so it can only return {{}} here. Declare the "
                    f"properties (see agent/tools/schema.param_union_schema) or "
                    f"do not send this schema to Gemini.")
        for k, v in schema.items():
            _assert_fillable(v, f"{path}.{k}")
    elif isinstance(schema, list):
        for i, v in enumerate(schema):
            _assert_fillable(v, f"{path}[{i}]")


def _normalize_schema_for_gemini(raw: dict) -> dict:
    """Transforms a Pydantic JSON schema into one Gemini accepts.

    Steps:
      1. Extract $defs (sub-model definitions).
      2. Replace every $ref with the content of the definition.
      3. Remove unsupported keywords (title, default, etc.).
    """
    defs = raw.get("$defs") or raw.get("definitions") or {}
    resolved = _resolve_refs(raw, defs)
    cleaned = _strip_unsupported_keys(resolved)
    # Fail here, not in production with empty results.
    _assert_fillable(cleaned)
    return cleaned


def _thinking_budget_for(model: str) -> Optional[int]:
    """The thinkingBudget to send for this model, or None to send none.

    Env wins. Otherwise 0 (thinking off) on flash, which supports it, and
    nothing at all on every other model, which may not.
    """
    env = os.environ.get("GEMINI_THINKING_BUDGET", "").strip()
    if env.lstrip("-").isdigit():
        return int(env)
    return 0 if "flash" in (model or "").lower() else None


class ChatGeminiHTTP(BaseChatModel):
    """LangChain-compatible chat model that talks to Gemini over direct HTTP."""

    model: str = "gemini-2.5-flash"
    api_key: str = ""
    temperature: float = 0.2
    max_tokens: int = 8192
    # Raised with max_tokens: a bigger answer takes longer to produce, and a
    # timeout here costs a full retry of a ~12k-token prompt. agent/llm.py
    # has used 120 all along.
    timeout: int = 120
    # If set, forces responseMimeType=application/json + responseSchema
    # → Gemini guarantees the output is valid JSON conforming to the schema.
    response_schema: Optional[dict] = Field(default=None)

    # Gemini 2.5 models think by DEFAULT and, per Google's own documentation,
    # "max_output_tokens sets the maximum number of tokens a response can
    # generate, INCLUDING thought tokens". The planner's prompt is ~12k tokens
    # of catalog, which is exactly the kind of input that makes a thinking
    # model think for a long time -- and every one of those tokens is taken
    # out of the same budget the JSON answer has to fit in. Two failures in
    # production match that shape: a document cut mid-string (2026-09-21) and
    # a schema-valid but EMPTY object (2026-09-22, twice).
    #
    # So thinking is turned OFF by default on the flash models, where the API
    # allows a budget of 0. It is NOT turned off on other models: 2.5-pro
    # rejects 0 (its minimum is 128), and a default that breaks the moment
    # someone changes GEMINI_MODEL is a trap, not a safety net.
    #
    # GEMINI_THINKING_BUDGET overrides both: 0 off, -1 dynamic, or a cap in
    # tokens (0..24576 on flash).
    thinking_budget: Optional[int] = None

    def model_post_init(self, __context) -> None:
        if self.thinking_budget is None:
            self.thinking_budget = _thinking_budget_for(self.model)

    model_config = {"arbitrary_types_allowed": True}

    @property
    def _llm_type(self) -> str:
        return "gemini-http"

    def _generate(
        self,
        messages: List[BaseMessage],
        stop: Optional[List[str]] = None,
        run_manager: Any = None,
        **kwargs: Any,
    ) -> ChatResult:
        # One line per Gemini call. Without this, a fallback from Anthropic to
        # Gemini is invisible: the 2026-09-16 empty-params turns had nothing in
        # the logs naming the provider that served them.
        try:
            from agent import obs
            obs.log("llm_call", provider="gemini", model=self.model,
                    structured=bool(self.response_schema))
        except Exception:
            pass
        system_text, contents = _messages_to_gemini(messages)

        body: dict = {
            "contents": contents,
            "generationConfig": {
                "temperature": self.temperature,
                "maxOutputTokens": self.max_tokens,
            },
        }
        if system_text:
            body["systemInstruction"] = {"parts": [{"text": system_text}]}
        if self.response_schema:
            body["generationConfig"]["responseMimeType"] = "application/json"
            body["generationConfig"]["responseSchema"] = self.response_schema
        if self.thinking_budget is not None:
            body["generationConfig"]["thinkingConfig"] = {
                "thinkingBudget": self.thinking_budget}

        if not self.api_key:
            raise RuntimeError("GEMINI_API_KEY is not configured")

        url = _GEMINI_URL.format(model=self.model)
        try:
            resp = requests.post(
                f"{url}?key={self.api_key}",
                json=body,
                timeout=self.timeout,
            )
        except requests.RequestException as e:
            raise RuntimeError(f"Gemini HTTP request failed: {e}") from e

        if resp.status_code != 200:
            # Error body for diagnostics (cap 500 chars to avoid spamming logs).
            raise RuntimeError(
                f"Gemini API {resp.status_code}: {resp.text[:500]}"
            )

        data = resp.json()
        # Defensive extraction: Gemini's response can have several shapes.
        try:
            candidates = data.get("candidates") or []
            if not candidates:
                # Blocked by safety or some other reason
                feedback = data.get("promptFeedback", {})
                raise RuntimeError(f"Gemini returned no candidates. Feedback: {feedback}")
            parts = candidates[0].get("content", {}).get("parts", [])
            text = "".join(p.get("text", "") for p in parts)
        except Exception as e:
            raise RuntimeError(
                f"Unexpected Gemini response format: {json.dumps(data)[:500]}"
            ) from e

        # WHY THIS IS LOGGED
        # Two failures were diagnosed blind because the response's own account
        # of what happened was discarded: a structured output cut mid-document
        # (2026-09-21, a real user) and a schema-valid EMPTY object (2026-09-22,
        # twice in a row). Both look like "the model did not understand" and
        # neither is: `finishReason` says whether the model finished or ran out
        # of room, and `thoughtsTokenCount` says how much of maxOutputTokens
        # the thinking consumed before the answer began. Neither was read.
        finish = (candidates[0] or {}).get("finishReason", "")
        usage = data.get("usageMetadata") or {}
        thoughts = usage.get("thoughtsTokenCount", 0)
        try:
            from agent import obs
            obs.log("llm_response", provider="gemini", model=self.model,
                    finish_reason=finish,
                    prompt_tokens=usage.get("promptTokenCount", 0),
                    output_tokens=usage.get("candidatesTokenCount", 0),
                    thoughts_tokens=thoughts,
                    max_output_tokens=self.max_tokens,
                    thinking_budget=self.thinking_budget,
                    chars=len(text))
        except Exception:
            pass

        budget_note = (
            f" The model spent {thoughts} of its {self.max_tokens} output tokens "
            f"on THINKING before answering; Gemini 2.5 thinks by default and "
            f"those tokens come out of maxOutputTokens. Set "
            f"GEMINI_THINKING_BUDGET=0 or raise max_tokens."
            if thoughts else "")

        if finish == "MAX_TOKENS":
            # A truncated structured answer is not an answer, even on the rare
            # occasion it still parses. Raising puts it in the caller's retry
            # loop with the reason named, instead of handing the graph a
            # half-decoded plan.
            #
            # PRINTED, not only raised: NodeLLM wraps this model in
            # .with_fallbacks([bedrock, anthropic]), and LangChain's fallback
            # swallows the exception without a word. On 2026-09-22 every
            # planner call ended in MAX_TOKENS and every plan was quietly
            # produced by the FALLBACK provider -- the logs showed a healthy
            # turn, and Gemini had failed 100% of the time. An error that only
            # exists inside an exception a fallback eats is not an error
            # anyone can see.
            print(f"   [GEMINI] TRUNCATED at MAX_TOKENS after {len(text)} chars "
                  f"(max_output_tokens={self.max_tokens}, "
                  f"thinking_budget={self.thinking_budget}, thoughts={thoughts}). "
                  f"A fallback provider will answer instead, silently.")
            print(f"   [GEMINI] head: {text[:400]!r}")
            print(f"   [GEMINI] tail: {text[-200:]!r}")
            try:
                from agent import obs
                obs.log("llm_truncated", provider="gemini", model=self.model,
                        chars=len(text), head=text[:200])
            except Exception:
                pass
            raise RuntimeError(
                f"Gemini stopped at MAX_TOKENS: the response is truncated."
                f"{budget_note} First 200 chars: {text[:200]!r}")

        if not text:
            raise RuntimeError(
                f"Gemini returned empty text (finishReason={finish!r})."
                f"{budget_note} Raw: {json.dumps(data)[:300]}")

        return ChatResult(generations=[ChatGeneration(message=AIMessage(content=text))])

    def with_structured_output(
        self,
        schema: Type[BaseModel],
        **kwargs: Any,
    ) -> Runnable:
        """Produces JSON output conforming to the Pydantic schema via responseSchema.

        Returns a Runnable that .invoke() returns an instance of the schema.
        Because it's a RunnableSequence, it supports .with_fallbacks() (which
        is what NodeLLM applies in llm_factory).
        """
        if not (isinstance(schema, type) and issubclass(schema, BaseModel)):
            raise ValueError(
                "ChatGeminiHTTP.with_structured_output only accepts Pydantic models"
            )

        raw_schema = schema.model_json_schema()
        cleaned = _normalize_schema_for_gemini(raw_schema)

        gemini_with_schema = ChatGeminiHTTP(
            model=self.model,
            api_key=self.api_key,
            temperature=self.temperature,
            max_tokens=self.max_tokens,
            timeout=self.timeout,
            response_schema=cleaned,
        )

        def _parse_json(message: AIMessage) -> BaseModel:
            try:
                data = json.loads(message.content)
            except json.JSONDecodeError as e:
                raise RuntimeError(
                    f"Gemini structured output is NOT valid JSON. "
                    f"Content (first 300 chars): {message.content[:300]!r}"
                ) from e
            try:
                return schema(**data)
            except Exception as e:
                raise RuntimeError(
                    f"JSON does not match schema {schema.__name__}: {e}"
                ) from e

        return gemini_with_schema | RunnableLambda(_parse_json)
