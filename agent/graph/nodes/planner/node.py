"""🧠 PLANNER: clasifica el mensaje y genera un plan de herramientas.

Dos niveles (patrón de la arquitectura de referencia):
  Tier 1 — fast-paths regex (saludos/ayuda/gracias) sin LLM.
  Tier 2 — LLM con structured output (PlannerOutput) + reintentos + sanitización.
"""
from langchain_core.messages import HumanMessage, SystemMessage

from agent.graph.history import format_history
from agent.graph.nodes.planner.patterns import match_fast_path
from agent.graph.nodes.planner.prompts import PLANNER_PROMPT
from agent.graph.nodes.planner.schemas import PlannerOutput
from agent.graph.state import MAX_PLANNER_ITERATIONS, AgentState
from agent.tools import TOOL_MAP

LLM_MAX_RETRIES = 3
MAX_PLAN_STEPS = 12

# Mapa herramienta → intención (para el dispatch del narrator)
_TOOL_INTENTS = {
    "listar_correos": "emails",
    "inspeccionar_correo": "emails",
    "enviar_correo": "emails",
    "listar_proyectos": "projects",
    "crear_proyecto": "projects",
    "crear_tarea": "projects",
    "asignar_correo_a_proyecto": "projects",
    "crear_insight": "projects",
    "crear_recordatorio": "projects",
    "enviar_notificacion": "notifications",
    "listar_notificaciones": "notifications",
    "obtener_contactos_proyecto": "notifications",
    "analizar_inbox": "proactive",
    "verificar_sla": "proactive",
    "clasificar_mensajes_automatico": "proactive",
    "resumen_proactivo": "proactive",
}


def _derive_intent(plan: list) -> str:
    """Deriva la intención del plan: una sola categoría → esa; mezcla → generic."""
    intents = {_TOOL_INTENTS.get(s["tool"], "generic") for s in plan}
    return intents.pop() if len(intents) == 1 else "generic"


def planner_node(state: AgentState, llm) -> dict:
    print("\n" + "=" * 60)
    print("🧠 PLANNER")
    print("=" * 60)

    message = state.get("resolved_message") or state.get("user_message", "")
    iteration = state.get("iteration", 0)
    feedback = state.get("validation_feedback") or "Ninguno"

    print(f"   Mensaje: {message[:100]}...")
    print(f"   Iteración: {iteration + 1}/{MAX_PLANNER_ITERATIONS}")

    if iteration >= MAX_PLANNER_ITERATIONS:
        print("   ⚠️ Límite de iteraciones alcanzado")
        return {
            "status": "direct",
            "intent": "conversation",
            "direct_response": (
                "No pude completar tu solicitud después de varios intentos. "
                "¿Podrías reformular tu pregunta?"
            ),
            "iteration": iteration + 1,
        }

    # ── Tier 1: fast-paths regex (solo en el primer intento, sin feedback) ──
    if iteration == 0 and not state.get("validation_feedback"):
        canned = match_fast_path(message)
        if canned:
            print("   ⚡ Fast-path regex (sin LLM)")
            return {
                "status": "direct",
                "intent": "conversation",
                "direct_response": canned,
                "resolution_method": "regex",
                "iteration": iteration + 1,
            }

    # ── Tier 2: LLM con structured output ──
    # .replace() en vez de .format(): el catálogo contiene JSON literal con llaves
    system = (
        PLANNER_PROMPT
        .replace("{history}", format_history(state.get("history", [])))
        .replace("{validator_feedback}", feedback)
    )
    structured = llm.with_structured_output(PlannerOutput)

    output = None
    for attempt in range(1, LLM_MAX_RETRIES + 1):
        try:
            output = structured.invoke([
                SystemMessage(content=system),
                HumanMessage(content=f"Mensaje del usuario: {message}"),
            ])
            if output is not None:
                break
        except Exception as e:
            print(f"   ⚠️ Intento {attempt}/{LLM_MAX_RETRIES} falló: {e}")

    if output is None:
        print("   ❌ Planner sin salida válida tras reintentos")
        return {
            "status": "direct",
            "intent": "conversation",
            "direct_response": (
                "Lo siento, tuve un problema procesando tu solicitud. "
                "¿Puedes intentarlo de nuevo?"
            ),
            "iteration": iteration + 1,
        }

    # ── DEBUG: mostrar salida completa del LLM ──
    print(f"   [DEBUG] direct_response: {repr(output.direct_response)}")
    print(f"   [DEBUG] plan raw ({len(output.plan)} pasos):")
    for s in output.plan:
        print(f"      step={s.step} tool={s.tool} params={s.params}")

    # ── Sanitización (guarda contra alucinaciones del LLM) ──
    plan = []
    for step in output.plan[:MAX_PLAN_STEPS]:
        if step.tool not in TOOL_MAP:
            print(f"   ⚠️ Herramienta desconocida descartada: {step.tool}")
            continue
        plan.append({"step": step.step, "tool": step.tool, "params": step.params or {}})

    if plan:
        print(f"   → Plan generado: {len(plan)} pasos")
        for s in plan:
            print(f"      Paso {s['step']}: {s['tool']} - {s['params']}")
        update = {
            "status": "executing",
            "plan": plan,
            "intent": _derive_intent(plan),
            "results": {},
            "tools_used": [],
            "resolution_method": "llm",
            "validation_feedback": None,
            "iteration": iteration + 1,
        }
        if state.get("debug_mode"):
            update["debug_info"] = {
                **(state.get("debug_info") or {}),
                "planner_decision": "execute_plan",
                "plan": plan,
                "intent": _derive_intent(plan),
                "iteration": iteration + 1,
                "resolution_method": "llm",
            }
        return update

    if output.direct_response:
        print("   → Respuesta directa (sin herramientas)")
        update = {
            "status": "direct",
            "intent": "conversation",
            "direct_response": output.direct_response,
            "plan": [],
            "resolution_method": "llm",
            "iteration": iteration + 1,
        }
        if state.get("debug_mode"):
            update["debug_info"] = {
                **(state.get("debug_info") or {}),
                "planner_decision": "direct_response",
                "direct_response": output.direct_response,
                "iteration": iteration + 1,
                "resolution_method": "llm",
            }
        return update

    print("   ⚠️ Salida sin plan ni respuesta directa")
    return {
        "status": "direct",
        "intent": "conversation",
        "direct_response": "No entendí bien tu solicitud. ¿Podrías ser más específico?",
        "iteration": iteration + 1,
    }
