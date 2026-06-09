"""Fábrica de LLMs por nodo del grafo (y reutilizable fuera del grafo).

Configuración por variables de entorno, formato "provider:model":
  - NODE_LLM_CONTEXT_RESOLVER, NODE_LLM_PLANNER, NODE_LLM_VALIDATOR, NODE_LLM_NARRATOR
  - providers soportados: bedrock | anthropic | gemini
  - Ej: NODE_LLM_NARRATOR=bedrock:us.anthropic.claude-haiku-4-5-20251001-v1:0
        (se separa en el PRIMER ':' — los IDs de Bedrock contienen ':')

Sin override, todos los nodos usan el provider por defecto (misma lógica que
agent/llm.py: gemini si hay GEMINI_API_KEY, sino anthropic si hay key, sino
bedrock) con su modelo por defecto. Defaulteamos TODOS los nodos al mismo
modelo Sonnet ya habilitado en prod; los modelos ligeros por nodo se activan
vía env cuando se validen en la cuenta.

Los imports de providers son perezosos: solo se importa el paquete del
provider seleccionado (controla el tamaño de la imagen Lambda).

IMPORTANTE: este módulo no importa nada del grafo — api/services puede usarlo
directamente (p. ej. generación de tareas con IA con structured output).
"""
import os
from typing import List, Optional

# Reutiliza las mismas constantes/semántica de agent/llm.py para que la
# configuración del sistema sea una sola.
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

# Temperaturas por nodo (preservan las del agente anterior)
_NODE_TEMPERATURE = {
    "context_resolver": 0.0,
    "planner": 0.2,
    "validator": 0.1,
    "narrator": 0.4,
}


class NodeLLM:
    """Envuelve un LLM primario + fallbacks manteniendo la interfaz de chat.

    `.with_fallbacks()` de LangChain devuelve un Runnable genérico SIN
    `.with_structured_output()`, así que componemos el schema en cada provider
    ANTES de encadenar los fallbacks (riesgo de composición documentado en el plan).
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
    """Parsea 'provider:model' separando en el PRIMER ':' (IDs Bedrock llevan ':')."""
    raw = (value or "").strip()
    if not raw:
        return None, None
    if ":" in raw:
        provider, model = raw.split(":", 1)
        provider = provider.strip().lower()
        if provider in PROVIDERS:
            return provider, model.strip()
    # Solo provider, sin modelo explícito
    if raw.lower() in PROVIDERS:
        return raw.lower(), None
    raise ValueError(
        f"Config LLM inválida: '{value}'. Usa 'provider:model' con provider en {PROVIDERS}"
    )


def _create_llm(provider: str, model: str, temperature: float, max_tokens: int = 4096):
    """Crea el chat model del provider (import perezoso por provider)."""
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
        try:
            from langchain_google_genai import ChatGoogleGenerativeAI
        except ImportError as e:
            raise RuntimeError(
                "Provider 'gemini' seleccionado pero langchain-google-genai no está "
                "instalado (conflicta con langchain-core>=1.0; ver requirements.txt). "
                "Usa bedrock/anthropic o instala el paquete en un entorno compatible."
            ) from e
        return ChatGoogleGenerativeAI(
            model=model,
            google_api_key=GEMINI_API_KEY,
            temperature=temperature,
            max_output_tokens=max_tokens,
        )
    raise ValueError(f"Provider desconocido: {provider}")


def _gemini_available() -> bool:
    try:
        import langchain_google_genai  # noqa: F401
        return True
    except ImportError:
        return False


def _default_provider() -> str:
    """Provider por defecto, degradando si el auto-seleccionado no es usable.

    LLM_PROVIDER hereda la auto-selección de agent/llm.py (gemini si hay key),
    pero langchain-google-genai no es instalable junto a langgraph 1.x: si el
    default resuelve a gemini sin paquete, degradamos a anthropic/bedrock con
    aviso. Un NODE_LLM_*=gemini:... EXPLÍCITO sí falla fuerte en _create_llm.
    """
    provider = LLM_PROVIDER if LLM_PROVIDER in PROVIDERS else "bedrock"
    if provider == "gemini" and not _gemini_available():
        fallback = "anthropic" if ANTHROPIC_API_KEY else "bedrock"
        print(f"[llm_factory] ⚠️ default 'gemini' sin paquete instalado → usando '{fallback}'")
        return fallback
    return provider


def _available_fallback_providers(primary: str) -> List[str]:
    """Providers alternativos utilizables (con credenciales/paquete disponibles)."""
    candidates = []
    for p in PROVIDERS:
        if p == primary:
            continue
        if p == "anthropic" and not ANTHROPIC_API_KEY:
            continue
        if p == "gemini":
            if not GEMINI_API_KEY:
                continue
            try:
                import langchain_google_genai  # noqa: F401
            except ImportError:
                continue
        # bedrock: usa credenciales IAM del entorno; lo consideramos disponible
        candidates.append(p)
    return candidates


def create_llm(node: str = "narrator", env_value: str = "", temperature: float = None,
               max_tokens: int = 4096, with_fallbacks: bool = True) -> NodeLLM:
    """Crea el NodeLLM de un nodo (o de un consumidor externo del factory)."""
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
                print(f"[llm_factory] fallback {p} no disponible: {e}")
    return NodeLLM(primary, fallbacks)


def create_node_llms() -> dict:
    """Crea los LLMs de los 4 nodos del grafo (claves = nombres de nodo)."""
    llms = {}
    for node in NODE_NAMES:
        llms[node] = create_llm(node)
    return llms
