"""Conversión del historial OneBox ({"role","content"}) a formatos del grafo."""
from typing import List

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage


def to_lc_messages(history: List[dict]) -> List[BaseMessage]:
    """Convierte el historial OneBox a mensajes LangChain."""
    msgs: List[BaseMessage] = []
    for m in history or []:
        content = m.get("content", "")
        if m.get("role") == "assistant":
            msgs.append(AIMessage(content))
        else:
            msgs.append(HumanMessage(content))
    return msgs


def format_history(history: List[dict], max_messages: int = 10, max_chars: int = 350) -> str:
    """Formatea el historial como texto plano para inyectar en prompts.

    Últimos 10 mensajes, 350 caracteres por mensaje — preserva IDs, nombres
    y datos referenciables que aparecen en turnos anteriores.
    """
    if not history:
        return "Sin historial previo."
    lines = []
    for msg in history[-max_messages:]:
        role = "Usuario" if msg.get("role") == "user" else "Asistente"
        content = (msg.get("content", "") or "")[:max_chars]
        lines.append(f"- {role}: {content}")
    return "\n".join(lines)
