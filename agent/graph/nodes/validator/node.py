"""✅ VALIDATOR: verifica si los resultados cumplen la solicitud.

DONE → narrar. ERROR/CONTINUE con iteraciones disponibles → replanear con
feedback. Tope alcanzado → narrar igual (degradación elegante, igual que el
agente anterior). Fallo del LLM → DONE (nunca bloquea la respuesta).
"""
import json

from langchain_core.messages import HumanMessage, SystemMessage

from agent.graph.nodes.validator.prompts import VALIDATOR_PROMPT
from agent.graph.nodes.validator.schemas import ValidatorOutput
from agent.graph.state import MAX_PLANNER_ITERATIONS, AgentState


def _extract_validation_errors(results: dict) -> list[str]:
    """Detecta errores de validación del executor en los resultados."""
    errors = []
    for step_num, result in results.items():
        if isinstance(result, dict) and result.get("_validation_error"):
            errors.append(result.get("hint", result.get("error", "Error de validación desconocido")))
    return errors


def validator_node(state: AgentState, llm) -> dict:
    print("\n" + "=" * 60)
    print("✅ VALIDATOR")
    print("=" * 60)

    user_message = state.get("resolved_message") or state.get("user_message", "")
    plan = state.get("plan", [])
    results = state.get("results", {})
    iteration = state.get("iteration", 0)

    # ── Detección rápida de errores de validación (sin LLM) ──────────────
    validation_errors = _extract_validation_errors(results)
    if validation_errors:
        feedback = "ERRORES DE PARÁMETROS detectados por el sistema:\n" + "\n".join(
            f"  • {e}" for e in validation_errors
        )
        print(f"   ⚠️ Validación por código falló: {feedback[:200]}")
        if iteration >= MAX_PLANNER_ITERATIONS:
            print("   ⚠️ Límite alcanzado, continuando al narrator de todas formas")
            return {"status": "narrate"}
        return {"status": "replan", "validation_feedback": feedback}

    system = VALIDATOR_PROMPT.format(
        user_message=user_message,
        plan=json.dumps(plan, ensure_ascii=False, indent=2),
        results=json.dumps(results, ensure_ascii=False, default=str)[:2000],
    )

    try:
        output = llm.with_structured_output(ValidatorOutput).invoke([
            SystemMessage(content=system),
            HumanMessage(content="Evalúa los resultados."),
        ])
    except Exception as e:
        print(f"   ⚠️ Validator falló ({e}), asumiendo DONE")
        output = None

    if output is None or output.decision == "DONE":
        summary = (output.summary if output else "")[:100]
        print(f"   → Validación exitosa: {summary}")
        return {"status": "narrate"}

    print(f"   → {output.decision}: {output.feedback[:150]}")

    if iteration >= MAX_PLANNER_ITERATIONS:
        print("   ⚠️ Límite alcanzado, continuando al narrator")
        return {"status": "narrate"}

    return {"status": "replan", "validation_feedback": output.feedback or output.summary}
