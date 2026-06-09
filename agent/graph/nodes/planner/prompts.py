"""System prompt del planner, compuesto desde personality + catálogo.

La sección "FORMATO DE RESPUESTA JSON" del prompt anterior desaparece:
el structured output (PlannerOutput) garantiza la forma de la salida.
"""
from agent.graph import personality
from agent.graph.nodes.planner.catalog import (
    FULL_EXTRACTION, KEY_PARAMS, MULTISTEP_RECIPES, REQUIRED_PARAMS, RULES,
    SEARCH_GUIDE, TOOL_CATALOG, USAGE_TABLE, WHEN_NOT_TO_USE_TOOLS,
)

PLANNER_PROMPT = "\n\n".join([
    "Eres el planificador de OneBox, un asistente inteligente de gestión de proyectos y comunicaciones.",
    personality.CAPABILITIES,
    TOOL_CATALOG,
    SEARCH_GUIDE,
    USAGE_TABLE,
    FULL_EXTRACTION,
    MULTISTEP_RECIPES,
    WHEN_NOT_TO_USE_TOOLS,
    KEY_PARAMS,
    REQUIRED_PARAMS,
    RULES,
    personality.LANGUAGE,
    """## Historial de conversación:
{history}

## Feedback del validador (si hay):
{validator_feedback}

## INSTRUCCIONES:
1. Lee el mensaje del usuario cuidadosamente.
2. Decide si necesitas usar herramientas o si puedes responder directamente.
3. Si necesitas herramientas, crea un plan paso a paso (campo `plan`).
4. Si NO necesitas herramientas, responde en `direct_response` y deja `plan` vacío.
5. PIENSA PROACTIVAMENTE: si detectas oportunidades de mejora, sugiere acciones.""",
])
