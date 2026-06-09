"""💬 NARRATOR: compone la respuesta final según la intención.

Dispatcher por intención (patrón de la referencia):
  - "conversation" → passthrough de direct_response, SIN LLM.
  - emails / projects / notifications / proactive / generic → LLM con la guía
    específica de la intención + personalidad compartida.
"""
from langchain_core.messages import HumanMessage, SystemMessage

from agent.graph.nodes.narrator.helpers import build_user_prompt, truncate_results
from agent.graph.nodes.narrator.narrators import GUIDANCE_BY_INTENT
from agent.graph.nodes.narrator.prompts import NARRATOR_SYSTEM
from agent.graph.state import AgentState


def narrator_node(state: AgentState, llm) -> dict:
    print("\n" + "=" * 60)
    print("💬 NARRATOR")
    print("=" * 60)

    direct_response = state.get("direct_response") or ""
    if direct_response:
        print("   → Usando respuesta directa del planner (sin LLM)")
        update = {"response": direct_response, "status": "done"}
        if state.get("debug_mode"):
            update["debug_info"] = {
                **(state.get("debug_info") or {}),
                "narrator": "direct_passthrough",
            }
        return update

    results = state.get("results", {})
    if not results:
        return {
            "response": "No obtuve resultados. ¿Podrías reformular tu pregunta?",
            "status": "done",
        }

    intent = state.get("intent") or "generic"
    guidance = GUIDANCE_BY_INTENT.get(intent, GUIDANCE_BY_INTENT["generic"])
    print(f"   Intención: {intent}")

    user_message = state.get("resolved_message") or state.get("user_message", "")
    prompt = build_user_prompt(user_message, truncate_results(results), guidance)

    try:
        result = llm.invoke([
            SystemMessage(content=NARRATOR_SYSTEM),
            HumanMessage(content=prompt),
        ])
        response = result.content if hasattr(result, "content") else str(result)
    except Exception as e:
        print(f"   ❌ Narrator falló: {e}")
        response = (
            "Ejecuté tu solicitud pero tuve un problema redactando el resumen. "
            "Los datos se procesaron correctamente; inténtalo de nuevo para ver el detalle."
        )

    print(f"   Respuesta: {str(response)[:150]}...")
    update = {"response": response, "status": "done"}
    if state.get("debug_mode"):
        update["debug_info"] = {
            **(state.get("debug_info") or {}),
            "narrator": "llm",
            "intent": intent,
            "results_count": len(results),
            "dry_run": True,
        }
    return update
