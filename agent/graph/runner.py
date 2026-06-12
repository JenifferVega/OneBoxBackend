"""run_agent(): wrapper de compatibilidad sobre el grafo LangGraph.

Contrato público SIN CAMBIOS respecto al agente anterior:
    run_agent(message, history) -> {"response": str, "tools_used": List[str]}
Callers: api/controllers/chat.py, api/services/whatsapp.py, main.py (lambda).

El grafo compilado y los LLMs se construyen UNA vez por contenedor caliente
(singleton de módulo) — evita recrear clientes Bedrock por request en Lambda.
"""
from typing import Any, Dict, List

from agent.graph.state import AgentState

_GRAPH = None


def _graph():
    global _GRAPH
    if _GRAPH is None:
        from agent.graph.builder import build_graph
        from agent.graph.llm_factory import create_node_llms
        _GRAPH = build_graph(create_node_llms())
    return _GRAPH


def run_agent(user_message: str, history: List[dict] = None, debug_mode: bool = False, dry_run_projects: list = None, session_id: str = "") -> Dict[str, Any]:
    """Ejecuta el agente completo.

    Args:
        user_message: Mensaje del usuario
        history: Historial de conversación [{"role","content"}]

    Returns:
        dict: {"response": str, "tools_used": List[str]}
    """
    print("\n" + "=" * 70)
    print("🤖 ONEBOX AGENT - INICIO")
    print("=" * 70)
    print(f"Mensaje: {user_message}")

    state: AgentState = {
        "user_message": user_message,
        "history": history or [],
        "plan": [],
        "results": {},
        "tools_used": [],
        "validation_feedback": None,
        "iteration": 0,
        "status": "resolving",
        "response": "",
        "direct_response": None,
        "debug_mode": debug_mode,
        "session_id": session_id,
        "intent_draft": None,
        "debug_info": {},
    }

    try:
        final = _graph().invoke(state, config={"recursion_limit": 25})
    except Exception as e:
        print(f"[run_agent] Error en el grafo: {e}")
        import traceback
        traceback.print_exc()
        return {"response": "Lo siento, no pude procesar tu solicitud.", "tools_used": []}

    print("\n" + "=" * 70)
    print("🤖 ONEBOX AGENT - FIN")
    print("=" * 70)

    return {
        "response": final.get("response") or "Lo siento, no pude procesar tu solicitud.",
        "tools_used": final.get("tools_used", []),
        "debug_info": final.get("debug_info"),
        "results": final.get("results", {}),  # resultados crudos de cada paso (para callers internos)
    }
