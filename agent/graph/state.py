"""Estado tipado del grafo del agente OneBox.

Un único TypedDict fluye por todos los nodos (patrón LangGraph). Cada nodo
devuelve SOLO las claves que modifica; LangGraph las fusiona en el estado.
"""
from typing import Dict, List, Optional, TypedDict

# Máximo de replaneos del planner (preserva el comportamiento del agente anterior).
MAX_PLANNER_ITERATIONS = 3


class AgentState(TypedDict, total=False):
    # ── Entrada (la fija el runner antes de invocar el grafo) ──
    user_message: str            # mensaje crudo (puede traer contexto de proyecto prepuesto por WhatsApp)
    history: List[dict]          # [{"role": "user"|"assistant", "content": str}]

    # ── context_resolver ──
    resolved_message: Optional[str]   # mensaje reescrito autocontenido; None si no cambió

    # ── planner ──
    plan: List[dict]             # [{"step": int, "tool": str, "params": dict}]
    direct_response: Optional[str]    # respuesta sin herramientas (saludo, ayuda, rechazo)
    intent: Optional[str]        # emails | projects | notifications | proactive | conversation | generic
    resolution_method: Optional[str]  # "regex" (fast-path) | "llm"

    # ── executor ──
    results: Dict[int, dict]     # resultados por número de paso (las referencias from_step dependen de esto)
    tools_used: List[str]

    # ── validator ──
    validation_feedback: Optional[str]   # contexto de error para el replaneo

    # ── narrator ──
    response: str

    # ── Debug / dry-run ──
    debug_mode: bool              # si True, el executor simula tools sin tocar DynamoDB
    session_id: Optional[str]     # ID de sesión MCP (para cache dry-run por sesión)
    intent_draft: Optional[dict]  # schema parcial del intent en construcción
    debug_info: Optional[dict]    # acumulado de info de debug para retornar al caller

    # ── Control de flujo ──
    iteration: int               # contador de replaneos del planner (tope MAX_PLANNER_ITERATIONS)
    status: str                  # resolving→planning→executing→validating→narrating→done
                                 # transitorios: "direct" (planner→narrator), "replan" (validator→planner)
