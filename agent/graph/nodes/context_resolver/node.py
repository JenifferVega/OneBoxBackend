"""🔎 CONTEXT RESOLVER: reescribe seguimientos ambiguos como mensajes autocontenidos.

Optimización clave: con historial vacío NO llama al LLM (cero latencia añadida
en primeros turnos, el caso más común). Cualquier error → mensaje original.
"""
from langchain_core.messages import HumanMessage, SystemMessage

from agent.graph.history import format_history
from agent.graph.nodes.context_resolver.prompts import RESOLVER_PROMPT
from agent.graph.state import AgentState


def context_resolver_node(state: AgentState, llm) -> dict:
    print("\n" + "=" * 60)
    print("🔎 CONTEXT RESOLVER")
    print("=" * 60)

    history = state.get("history", [])
    message = state.get("user_message", "")

    if not history:
        print("   → Sin historial: se omite (sin LLM)")
        return {"resolved_message": None, "status": "planning"}

    try:
        result = llm.invoke([
            SystemMessage(content=RESOLVER_PROMPT),
            HumanMessage(content=(
                f"HISTORIAL:\n{format_history(history)}\n\nMENSAJE: {message}"
            )),
        ])
        resolved = (result.content if hasattr(result, "content") else str(result)).strip()
    except Exception as e:
        print(f"   ⚠️ Resolver falló ({e}), usando mensaje original")
        return {"resolved_message": None, "status": "planning"}

    if not resolved or resolved.lower() == message.strip().lower():
        print("   → Mensaje ya era claro, sin cambios")
        return {"resolved_message": None, "status": "planning"}

    print(f"   → Reescrito: {resolved[:120]}")
    return {"resolved_message": resolved, "status": "planning"}
