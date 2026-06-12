"""Adapter LangChain-compatible para Gemini usando HTTP directo.

Por qué existe este archivo:
  - El grafo del agente (llm_factory) consume modelos vía la interfaz de
    langchain (.invoke + .with_structured_output + .with_fallbacks).
  - El paquete oficial `langchain-google-genai` NO se puede instalar junto a
    `langgraph 1.x` porque exige `langchain-core<1.0` (su última release
    no se ha alineado con langchain-core 1.x). Es una decisión consciente
    documentada en requirements.txt.
  - Resultado: si LLM_PROVIDER=gemini, el llm_factory caía silenciosamente
    a Bedrock por el `_gemini_available()` que devolvía False. En throttling
    de Bedrock, el chatbot se rompía.

Solución: implementar ChatGeminiHTTP que habla con la API REST de Gemini
(generativelanguage.googleapis.com) cumpliendo la interfaz mínima que el
grafo usa. Es independiente del paquete oficial y compatible con langchain-core>=1.0.

Lo que sí soporta:
  - invoke([SystemMessage, HumanMessage]) → AIMessage con .content
  - with_structured_output(PydanticSchema) → Runnable que devuelve instancia
    (usando responseSchema nativo de Gemini para garantizar JSON válido)
  - with_fallbacks([...]) → heredado de BaseChatModel

Lo que NO soporta (no se usa en el grafo):
  - Tool calling
  - Streaming
  - Function calling
"""
from __future__ import annotations

import json
from typing import Any, List, Optional, Type

import requests
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.runnables import Runnable, RunnableLambda
from pydantic import BaseModel, Field

# Endpoint de la API v1beta (la stable para generateContent + structured output).
# El modelo va en la URL ({model}) y la API key en query string.
_GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"


def _messages_to_gemini(messages: List[BaseMessage]) -> tuple[str, list]:
    """Convierte mensajes LangChain al formato que espera Gemini.

    Gemini separa el system prompt (systemInstruction) del cuerpo del
    diálogo (contents). El cuerpo alterna roles 'user' y 'model'.
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
            # Mensaje desconocido: tratarlo como user para no perder contenido.
            contents.append({"role": "user", "parts": [{"text": str(msg.content)}]})
    return "\n\n".join(system_parts).strip(), contents


def _resolve_refs(schema: Any, defs: dict) -> Any:
    """Inline recursivamente todos los $ref del schema con su definición.

    Pydantic genera schemas con sub-modelos referenciados como
    `{"$ref": "#/$defs/PlannerStep"}` y las definiciones en `$defs`.
    Gemini NO acepta $ref → hay que sustituir cada $ref por el contenido
    de la definición referenciada (resolviendo refs anidados también).
    """
    if isinstance(schema, dict):
        if "$ref" in schema and isinstance(schema["$ref"], str):
            ref = schema["$ref"]
            # Formato típico: "#/$defs/NombreModelo" o "#/definitions/X"
            if ref.startswith("#/$defs/"):
                name = ref[len("#/$defs/"):]
            elif ref.startswith("#/definitions/"):
                name = ref[len("#/definitions/"):]
            else:
                # ref externo o desconocido: devolver tal cual para que falle ruidoso
                return schema
            referenced = defs.get(name)
            if referenced is None:
                return schema
            # Resolver recursivamente refs dentro del referenced también
            return _resolve_refs(referenced, defs)
        return {k: _resolve_refs(v, defs) for k, v in schema.items()}
    if isinstance(schema, list):
        return [_resolve_refs(v, defs) for v in schema]
    return schema


def _strip_unsupported_keys(schema: Any) -> Any:
    """Elimina keywords que Gemini no soporta.

    Mantiene: type, properties, items, required, description, enum, format,
    anyOf, oneOf, allOf, nullable.
    Quita keywords que Gemini's responseSchema rechaza:
      - title, default, $schema (metadata)
      - $defs, definitions (ya inlineados)
      - additionalProperties (no soportado en absoluto)
      - exclusiveMinimum/Maximum, multipleOf (subset limitado)
      - pattern (regex subset complicado)
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


def _normalize_schema_for_gemini(raw: dict) -> dict:
    """Transforma un JSON schema de Pydantic en uno aceptable por Gemini.

    Pasos:
      1. Extraer $defs (definiciones de sub-modelos).
      2. Sustituir todos los $ref por el contenido de la definición.
      3. Eliminar keywords no soportados (title, default, etc.).
    """
    defs = raw.get("$defs") or raw.get("definitions") or {}
    resolved = _resolve_refs(raw, defs)
    return _strip_unsupported_keys(resolved)


class ChatGeminiHTTP(BaseChatModel):
    """Chat model langchain-compatible que habla con Gemini vía HTTP directo."""

    model: str = "gemini-2.5-flash"
    api_key: str = ""
    temperature: float = 0.2
    max_tokens: int = 4096
    timeout: int = 60
    # Si está seteado, fuerza responseMimeType=application/json + responseSchema
    # → Gemini garantiza que la salida es JSON válido conforme al schema.
    response_schema: Optional[dict] = Field(default=None)

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

        if not self.api_key:
            raise RuntimeError("GEMINI_API_KEY no está configurada")

        url = _GEMINI_URL.format(model=self.model)
        try:
            resp = requests.post(
                f"{url}?key={self.api_key}",
                json=body,
                timeout=self.timeout,
            )
        except requests.RequestException as e:
            raise RuntimeError(f"Gemini HTTP request falló: {e}") from e

        if resp.status_code != 200:
            # Cuerpo del error para diagnóstico (cap 500 chars para no spamear logs).
            raise RuntimeError(
                f"Gemini API {resp.status_code}: {resp.text[:500]}"
            )

        data = resp.json()
        # Extracción defensiva: la respuesta de Gemini puede tener varias formas.
        try:
            candidates = data.get("candidates") or []
            if not candidates:
                # Bloqueado por safety u otro motivo
                feedback = data.get("promptFeedback", {})
                raise RuntimeError(f"Gemini no devolvió candidates. Feedback: {feedback}")
            parts = candidates[0].get("content", {}).get("parts", [])
            text = "".join(p.get("text", "") for p in parts)
        except Exception as e:
            raise RuntimeError(
                f"Respuesta Gemini con formato inesperado: {json.dumps(data)[:500]}"
            ) from e

        if not text:
            raise RuntimeError(f"Gemini devolvió texto vacío. Raw: {json.dumps(data)[:300]}")

        return ChatResult(generations=[ChatGeneration(message=AIMessage(content=text))])

    def with_structured_output(
        self,
        schema: Type[BaseModel],
        **kwargs: Any,
    ) -> Runnable:
        """Genera salida JSON conforme al schema Pydantic usando responseSchema.

        Devuelve un Runnable que al .invoke() retorna una instancia del schema.
        Como es un RunnableSequence, soporta .with_fallbacks() (que es lo que
        NodeLLM aplica en llm_factory).
        """
        if not (isinstance(schema, type) and issubclass(schema, BaseModel)):
            raise ValueError(
                "ChatGeminiHTTP.with_structured_output solo acepta Pydantic models"
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
                    f"Gemini structured output NO es JSON válido. "
                    f"Contenido (primeros 300 chars): {message.content[:300]!r}"
                ) from e
            try:
                return schema(**data)
            except Exception as e:
                raise RuntimeError(
                    f"JSON no encaja con schema {schema.__name__}: {e}"
                ) from e

        return gemini_with_schema | RunnableLambda(_parse_json)
