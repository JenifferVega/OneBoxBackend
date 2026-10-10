"""LangChain-compatible adapter for Ollama Cloud over direct HTTP.

Same contract as gemini_adapter.ChatGeminiHTTP -- the only interface the graph
uses: .invoke(messages) -> AIMessage, .with_structured_output(PydanticModel),
.with_fallbacks() (inherited). No extra package: Ollama's /api/chat is one
POST, and its `format` field takes a JSON schema and constrains decoding to it.

Configuration (.env):
    OLLAMA_API_KEY      required for ollama.com
    OLLAMA_BASE_URL     default https://ollama.com (a local `ollama serve` works too)
    OLLAMA_THINK        optional: true | false | low | medium | high. Unset =
                        the model's default. Reasoning models think before
                        answering: slower, and those tokens are billed.

Model strings: NODE_LLM_<NODE>=ollama-cloud:<model>, e.g.
    NODE_LLM_NARRATOR=ollama-cloud:gpt-oss:120b
"""
from __future__ import annotations

import json
import os
from typing import Any, List, Optional, Type

import requests
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, SystemMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.runnables import Runnable, RunnableLambda
from pydantic import BaseModel, Field

OLLAMA_BASE_URL = os.environ.get("OLLAMA_BASE_URL", "https://ollama.com").rstrip("/")


def _messages(messages: List[BaseMessage]) -> list:
    out = []
    for m in messages:
        role = ("system" if isinstance(m, SystemMessage) else
                "assistant" if isinstance(m, AIMessage) else "user")
        out.append({"role": role, "content": str(m.content)})
    return out


def _schema_for_ollama(raw: dict) -> dict:
    """Inline $refs (the grammar compiler needs one self-contained schema) and
    drop pure metadata. Everything else is standard JSON schema, which
    Ollama's `format` accepts as is."""
    from agent.graph.gemini_adapter import _resolve_refs
    defs = raw.get("$defs") or raw.get("definitions") or {}
    resolved = _resolve_refs(raw, defs)

    def strip(s):
        if isinstance(s, dict):
            return {k: strip(v) for k, v in s.items() if k not in ("title", "$defs", "definitions")}
        if isinstance(s, list):
            return [strip(v) for v in s]
        return s
    return strip(resolved)


def _think_setting() -> Any:
    v = os.environ.get("OLLAMA_THINK", "").strip().lower()
    if not v:
        return None
    if v in ("true", "1", "yes", "on"):
        return True
    if v in ("false", "0", "no", "off"):
        return False
    return v                     # low | medium | high (gpt-oss)


class ChatOllamaCloud(BaseChatModel):
    """Chat model that talks to Ollama Cloud's /api/chat."""

    model: str = "gpt-oss:120b"
    api_key: str = ""
    base_url: str = OLLAMA_BASE_URL
    temperature: float = 0.2
    max_tokens: int = 8192
    timeout: int = 120
    response_schema: Optional[dict] = Field(default=None)

    model_config = {"arbitrary_types_allowed": True}

    @property
    def _llm_type(self) -> str:
        return "ollama-cloud"

    def _generate(self, messages: List[BaseMessage], stop: Optional[List[str]] = None,
                  run_manager: Any = None, **kwargs: Any) -> ChatResult:
        from agent import obs
        try:
            obs.log("llm_call", provider="ollama-cloud", model=self.model,
                    structured=bool(self.response_schema))
        except Exception:
            pass
        if not self.api_key and "ollama.com" in self.base_url:
            raise RuntimeError("OLLAMA_API_KEY is not configured")

        body: dict = {
            "model": self.model,
            "messages": _messages(messages),
            "stream": False,
            "options": {"temperature": self.temperature, "num_predict": self.max_tokens},
        }
        if self.response_schema:
            body["format"] = self.response_schema
        think = _think_setting()
        if think is not None:
            body["think"] = think

        headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
        try:
            resp = requests.post(f"{self.base_url}/api/chat", headers=headers, json=body,
                                 timeout=self.timeout)
        except requests.RequestException as e:
            raise RuntimeError(f"Ollama HTTP request failed: {e}") from e
        if resp.status_code != 200:
            raise RuntimeError(f"Ollama API {resp.status_code}: {resp.text[:500]}")

        data = resp.json()
        msg = data.get("message") or {}
        text = msg.get("content") or ""
        finish = data.get("done_reason", "")
        try:
            obs.log("llm_response", provider="ollama-cloud", model=self.model,
                    finish_reason=finish,
                    prompt_tokens=data.get("prompt_eval_count", 0),
                    output_tokens=data.get("eval_count", 0),
                    thinking_chars=len(msg.get("thinking") or ""),
                    server_ms=int((data.get("total_duration") or 0) / 1e6),
                    chars=len(text))
        except Exception:
            pass

        if finish == "length":
            # Same rule as Gemini: a truncated structured answer is not an
            # answer. Printed as well as raised, because a fallback swallows
            # the exception without a word.
            print(f"   [OLLAMA] TRUNCATED (done_reason=length) after {len(text)} chars, "
                  f"num_predict={self.max_tokens}, model={self.model}")
            raise RuntimeError(f"Ollama stopped at the token limit ({self.max_tokens}): "
                               f"the response is truncated. First 200 chars: {text[:200]!r}")
        if not text.strip():
            raise RuntimeError(f"Ollama returned empty content (done_reason={finish!r}). "
                               f"Thinking chars: {len(msg.get('thinking') or '')}")
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content=text))])

    def with_structured_output(self, schema: Type[BaseModel], **kwargs: Any) -> Runnable:
        if not (isinstance(schema, type) and issubclass(schema, BaseModel)):
            raise ValueError("ChatOllamaCloud.with_structured_output only accepts Pydantic models")
        constrained = ChatOllamaCloud(
            model=self.model, api_key=self.api_key, base_url=self.base_url,
            temperature=self.temperature, max_tokens=self.max_tokens, timeout=self.timeout,
            response_schema=_schema_for_ollama(schema.model_json_schema()))

        def _parse(message: AIMessage) -> BaseModel:
            text = str(message.content).strip()
            if text.startswith("```"):
                text = text.strip("`")
                text = text[text.find("{"):] if "{" in text else text
            try:
                return schema(**json.loads(text))
            except Exception as e:
                raise RuntimeError(f"Ollama structured output did not match the schema: "
                                   f"{type(e).__name__}: {str(e)[:200]}. Head: {text[:200]!r}") from e
        return constrained | RunnableLambda(_parse)
