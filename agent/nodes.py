

import json
from typing import Dict, Any
from agent.state import AgentState
from agent.llm import call_llm, extract_json_from_response
from agent.tools import TOOLS_DESCRIPTION, execute_tool

MAX_ITERATIONS = 3



PLANNER_PROMPT = """Eres el planificador de OneBox, un asistente inteligente de gestión de proyectos y comunicaciones.

## Herramientas disponibles:
{tools_description}

## Historial de conversación:
{history}

## Feedback del validador (si hay):
{validator_feedback}

## INSTRUCCIONES:

1. Lee el mensaje del usuario cuidadosamente.
2. Decide si necesitas usar herramientas o si puedes responder directamente.
3. Si necesitas herramientas, crea un plan paso a paso.
4. PIENSA PROACTIVAMENTE: si detectas oportunidades de mejora, sugiere acciones.

## TUS CAPACIDADES:

### 📧 Correos (Gmail)
- listar_correos: Buscar correos con query de Gmail
- inspeccionar_correo: Ver detalle de un correo específico
- enviar_correo: Enviar correo de seguimiento

### 📋 Proyectos y Tareas
- listar_proyectos: Ver todos los proyectos
- crear_proyecto: Crear un nuevo proyecto
- crear_tarea: Crear tarea en un proyecto
- asignar_correo_a_proyecto: Mover correo a un proyecto

### 📱 Notificaciones (WhatsApp/SMS)
- obtener_contactos_proyecto: Obtener participantes de un proyecto con sus teléfonos y tareas pendientes
- enviar_notificacion: Enviar WhatsApp o SMS
- listar_notificaciones: Ver historial de notificaciones

### 🧠 Inteligencia Proactiva
- analizar_inbox: Leer correos sin asignar
- crear_insight: Registrar acción inteligente
- crear_recordatorio: Crear follow-up con fecha límite
- verificar_sla: Detectar tareas bloqueadas, vencidas o sin respuesta
- clasificar_mensajes_automatico: Auto-clasificar mensajes a proyectos
- resumen_proactivo: Resumen ejecutivo de todo el sistema

## CÓMO BUSCAR CORREOS:

Ejemplos de query para listar_correos:
- "from:linkedin" → remitente conocido
- "from:apple has:attachment" → con adjuntos
- "subject:factura" → tema específico
- "onebox" → busca en asunto Y contenido (sin "from:")
- Sin query → trae los últimos

Usa "from:" SOLO para remitentes conocidos.
Para proyectos o términos generales, busca sin "from:".

## CUÁNDO USAR HERRAMIENTAS:

| Mensaje del usuario | Herramienta |
|---|---|
| "muéstrame mis correos" | listar_correos |
| "correos de LinkedIn" | listar_correos con query "from:linkedin" |
| "inspecciona el correo X" | inspeccionar_correo |
| "envía un seguimiento a juan@..." | enviar_correo |
| "muéstrame mis proyectos" | listar_proyectos |
| "crea un proyecto de Marketing" | crear_proyecto |
| "crea una tarea: revisar diseño" | crear_tarea |
| "manda los pendientes por WhatsApp" | obtener_contactos_proyecto → enviar_notificacion |
| "manda un WhatsApp a +1..." | enviar_notificacion |
| "crea un recordatorio para..." | crear_recordatorio |
| "qué tareas están bloqueadas?" | verificar_sla |
| "clasifica los mensajes del inbox" | clasificar_mensajes_automatico |
| "dame un resumen" / "cómo va todo?" | resumen_proactivo |
| "hay algo urgente?" | verificar_sla |
| "qué hay pendiente?" | resumen_proactivo |

## ACCIONES PROACTIVAS (combinar herramientas):

Si el usuario pide algo complejo, usa MÚLTIPLES pasos:

- "manda los pendientes por WhatsApp al equipo" →
  Paso 1: listar_proyectos (para obtener el project_id)
  Paso 2: obtener_contactos_proyecto(project_id) → obtiene participantes con teléfonos y sus tareas pendientes
  Paso 3: enviar_notificacion a cada participante que tenga teléfono, con sus tareas pendientes

- "revisa qué hay pendiente y avísale a María" →
  Paso 1: obtener_contactos_proyecto(project_id) → busca a María y sus pendientes
  Paso 2: enviar_notificacion al teléfono de María con el resumen

- "clasifica el inbox y crea tareas" →
  Paso 1: clasificar_mensajes_automatico
  Paso 2+: asignar_correo_a_proyecto por cada uno

- "manda un follow-up sobre la factura" →
  Paso 1: listar_correos con query "factura"
  Paso 2: enviar_correo de seguimiento

## CUÁNDO NO USAR HERRAMIENTAS:
- Saludos: "hola", "buenos días"
- Preguntas sobre ti: "qué puedes hacer", "ayuda"
- Conversación casual

## FORMATO DE RESPUESTA:

Si necesitas herramientas, responde SOLO con este JSON:
{{
    "plan": [
        {{"step": 1, "tool": "nombre_herramienta", "params": {{...}}}}
    ]
}}

Si NO necesitas herramientas, responde SOLO con este JSON:
{{
    "direct_response": "Tu respuesta aquí"
}}

## PARÁMETROS DE HERRAMIENTAS CLAVE:

- crear_proyecto(name, description, type, participants, channels):
  - participants: Lista de dicts con nombre, email y rol. Ej: [{{"nombre": "Juan Pérez", "email": "juan@empresa.com", "rol": "Developer"}}]
  - IMPORTANTE: SIEMPRE incluye el email de cada participante. Extrae los emails de los correos (campos from, to, cc).
  - Si no conoces el email, pon "" pero siempre incluye el campo email en cada participante.

- obtener_contactos_proyecto(project_id): Devuelve los participantes del proyecto con sus teléfonos y tareas pendientes.
  Úsalo SIEMPRE antes de enviar notificaciones masivas para saber quién tiene teléfono y qué pendientes tiene.

- enviar_notificacion(destinatario, mensaje, canal, project_id, project_name):
  - destinatario: Número de teléfono con código país. Ej: "+50494622817"
  - canal: "whatsapp" o "sms"
  - IMPORTANTE: Obtén el teléfono de obtener_contactos_proyecto, NO le pidas al usuario que lo escriba.

## IMPORTANTE:
- SIEMPRE responde en JSON válido, nada más.
- Si el usuario pide un "resumen" o "cómo va todo", usa resumen_proactivo.
- Si el usuario pregunta por cosas "urgentes" o "bloqueadas", usa verificar_sla.
- Puedes combinar hasta 5 pasos en un plan."""


def planner_node(state: AgentState) -> AgentState:
    """
    🧠 PLANNER: Analiza el mensaje y genera un plan de acción.
    """
    print("\n" + "="*60)
    print("🧠 PLANNER")
    print("="*60)
    
    user_message = state.get("user_message", "")
    history = state.get("history", [])
    validator_feedback = state.get("validation_feedback", "Ninguno")
    iteration = state.get("iteration", 0)
    
    print(f"   Mensaje: {user_message[:100]}...")
    print(f"   Iteración: {iteration + 1}/{MAX_ITERATIONS}")
    
    if iteration >= MAX_ITERATIONS:
        print("   ⚠️ Límite de iteraciones alcanzado")
        return {
            **state,
            "status": "done",
            "direct_response": "No pude completar tu solicitud después de varios intentos. ¿Podrías reformular tu pregunta?"
        }
    
    history_text = "Sin historial previo."
    if history:
        history_lines = []
        for msg in history[-6:]:  
            role = "Usuario" if msg.get("role") == "user" else "Asistente"
            content = msg.get("content", "")[:200]
            history_lines.append(f"- {role}: {content}")
        history_text = "\n".join(history_lines)
    
    prompt = PLANNER_PROMPT.format(
        tools_description=TOOLS_DESCRIPTION,
        history=history_text,
        validator_feedback=validator_feedback
    )
    
    response = call_llm(
        system_prompt=prompt,
        user_message=user_message,
        temperature=0.2
    )
    
    print(f"   LLM Response: {response[:200]}...")
    
    parsed = extract_json_from_response(response)
    
    if not parsed:
        print("   ⚠️ No se pudo parsear JSON, respondiendo directo")
        return {
            **state,
            "status": "done",
            "direct_response": response,
            "iteration": iteration + 1
        }
    
    if "direct_response" in parsed:
        print(f"   → Respuesta directa (sin herramientas)")
        return {
            **state,
            "status": "done",
            "direct_response": parsed["direct_response"],
            "plan": [],
            "iteration": iteration + 1
        }
    
    if "plan" in parsed and parsed["plan"]:
        plan = parsed["plan"]
        print(f"   → Plan generado: {len(plan)} pasos")
        for step in plan:
            print(f"      Paso {step.get('step')}: {step.get('tool')} - {step.get('params', {})}")
        return {
            **state,
            "status": "executing",
            "plan": plan,
            "results": {},
            "tools_used": [],
            "iteration": iteration + 1
        }
    
    print("   ⚠️ JSON sin plan ni respuesta directa")
    return {
        **state,
        "status": "done",
        "direct_response": "No entendí bien tu solicitud. ¿Podrías ser más específico?",
        "iteration": iteration + 1
    }



def executor_node(state: AgentState) -> AgentState:
    """
    ⚡ EXECUTOR: Ejecuta el plan paso a paso.
    """
    print("\n" + "="*60)
    print("⚡ EXECUTOR")
    print("="*60)
    
    plan = state.get("plan", [])
    results = state.get("results", {})
    tools_used = state.get("tools_used", [])
    
    if not plan:
        print("   ⚠️ No hay plan que ejecutar")
        return {**state, "status": "validating"}
    
    for step in plan:
        step_num = step.get("step", 0)
        tool_name = step.get("tool", "")
        params = step.get("params", {})
        
        print(f"\n   Paso {step_num}: {tool_name}")
        
        resolved_params = resolve_params(params, results)
        print(f"   Params: {json.dumps(resolved_params, default=str)[:200]}")
        
        result = execute_tool(tool_name, resolved_params)
        
        results[step_num] = result
        tools_used.append(tool_name)
        
        result_preview = json.dumps(result, default=str, ensure_ascii=False)[:200]
        print(f"   Resultado: {result_preview}...")
    
    print(f"\n   ✅ Plan ejecutado: {len(plan)} pasos")
    
    return {
        **state,
        "results": results,
        "tools_used": tools_used,
        "status": "validating"
    }


def resolve_params(params: dict, results: dict) -> dict:
    """
    Resuelve referencias from_step en los parámetros.
    """
    if not params:
        return {}
    
    if "from_step" in params and len(params) == 1:
        step_ref = params["from_step"]
        if step_ref in results:
            return results[step_ref]
        return {}
    
    resolved = {}
    for key, value in params.items():
        if isinstance(value, dict) and "from_step" in value:
            step_ref = value["from_step"]
            if step_ref in results:
                resolved[key] = results[step_ref]
            else:
                resolved[key] = value
        else:
            resolved[key] = value
    
    return resolved


VALIDATOR_PROMPT = """Eres el validador de OneBox. Tu trabajo es verificar si los resultados cumplen lo que pidió el usuario.

## Mensaje del usuario:
{user_message}

## Plan ejecutado:
{plan}

## Resultados obtenidos:
{results}

## INSTRUCCIONES:
1. Compara lo que pidió el usuario con los resultados obtenidos.
2. Verifica si se cumplió el objetivo.

## CRITERIOS DE VALIDACIÓN:
- Si el usuario pidió correos y hay correos en los resultados → COMPLETE
- Si el usuario pidió correos de X y NO hay correos de X → Eso significa que NO EXISTEN, es COMPLETE (no "incomplete")
- Si hay un error técnico (500, timeout, etc.) → INCOMPLETE, reintentar
- Si el count es 0 pero no hay error → COMPLETE (simplemente no hay correos que coincidan)

## RESPONDE SOLO CON ESTE JSON:

Si los resultados son suficientes (incluyendo cuando count=0 sin errores):
{{
    "status": "complete",
    "summary": "Breve resumen de lo que se encontró o no se encontró"
}}

Si hay errores técnicos que requieren reintento:
{{
    "status": "incomplete",
    "feedback": "Qué error ocurrió",
    "suggestion": "Qué debería hacer el planner"
}}

## IMPORTANTE:
- "count: 0" NO es un error, significa que no hay correos que coincidan
- Solo marca "incomplete" si hay errores técnicos reales
- Responde SOLO con JSON válido"""


def validator_node(state: AgentState) -> AgentState:
    """
    ✅ VALIDATOR: Verifica si los resultados cumplen la solicitud.
    """
    print("\n" + "="*60)
    print("✅ VALIDATOR")
    print("="*60)
    
    user_message = state.get("user_message", "")
    plan = state.get("plan", [])
    results = state.get("results", {})
    iteration = state.get("iteration", 0)
    
    plan_text = json.dumps(plan, ensure_ascii=False, indent=2)
    results_text = json.dumps(results, ensure_ascii=False, default=str)[:2000] 
    
    prompt = VALIDATOR_PROMPT.format(
        user_message=user_message,
        plan=plan_text,
        results=results_text
    )
    
    response = call_llm(
        system_prompt=prompt,
        user_message="Evalúa los resultados.",
        temperature=0.1
    )
    
    print(f"   LLM Response: {response[:200]}...")
    
    parsed = extract_json_from_response(response)
    
    if not parsed:
        print("   ⚠️ No se pudo parsear, asumiendo completo")
        return {**state, "status": "done"}
    
    status = parsed.get("status", "complete")
    
    if status == "complete":
        print(f"   → Validación exitosa: {parsed.get('summary', '')[:100]}")
        return {**state, "status": "done"}
    
    feedback = parsed.get("feedback", "Resultados incompletos")
    print(f"   → Incompleto: {feedback}")
    
    if iteration >= MAX_ITERATIONS:
        print("   ⚠️ Límite alcanzado, continuando al narrator")
        return {**state, "status": "done"}
    
    return {
        **state,
        "status": "continue", 
        "validation_feedback": feedback
    }



NARRATOR_PROMPT = """Eres el narrador de OneBox. Tu trabajo es presentar los resultados al usuario de forma clara, útil y proactiva.

## Mensaje del usuario:
{user_message}

## Resultados obtenidos:
{results}

## INSTRUCCIONES:
1. Resume los resultados de forma clara y natural en español.
2. Si hay correos, menciona: cantidad, remitentes principales, temas.
3. Si NO hay correos (count: 0), explica que no se encontraron y sugiere alternativas.
4. Si hay alertas SLA, presenta las más urgentes primero con emojis de prioridad.
5. Si hay acciones sugeridas, preséntalas como próximos pasos que la IA puede ejecutar.
6. Si se ejecutaron acciones proactivas (envío correo, recordatorio, clasificación), confirma qué se hizo.
7. Si hay errores, explícalos de forma amigable.

## FORMATO:
- Usa **negritas** para resaltar información importante
- Usa listas con • para enumerar items
- Usa 🔴 para alertas altas, 🟡 para medias, 🟢 para info
- Sé conciso pero informativo
- SIEMPRE responde en español
- NO uses JSON, responde en texto natural
- Al final, sugiere acciones que el usuario puede pedir

## EJEMPLOS DE BUENAS RESPUESTAS:

Cuando hay resumen proactivo:
"📊 **Resumen de tus proyectos:**

• **Migración AWS** - 3 tareas pendientes, 1 bloqueada 🔴
• **Rediseño UX** - 5 tareas, todo al día 🟢
• **Campaña Q2** - 2 tareas vencidas 🔴

📥 **Inbox:** 4 mensajes sin clasificar

🤖 **Acciones sugeridas:**
• Puedo enviar recordatorio a María sobre la tarea bloqueada
• Puedo clasificar automáticamente los mensajes del inbox
• Puedo crear follow-ups para las tareas vencidas

¿Qué quieres que haga?"

Cuando hay alertas SLA:
"⚠️ **Alertas detectadas:**

🔴 **Tarea bloqueada:** 'Configurar VPN' en Migración AWS (3 días sin avance)
🔴 **Tarea vencida:** 'Entregar mockups' en Rediseño UX (venció hace 2 días)
🟡 **Inbox:** 5 mensajes pendientes de clasificar

¿Quieres que envíe recordatorios o clasifique el inbox?"

Cuando se ejecutó una acción:
"✅ **Acción completada:**
• Envié correo de seguimiento a juan@empresa.com sobre la factura pendiente
• Creé recordatorio para el 15 de marzo: 'Revisar entrega de mockups'

¿Necesitas algo más?"
"""


def narrator_node(state: AgentState) -> AgentState:
    """
    💬 NARRATOR: Formatea los resultados como respuesta final.
    """
    print("\n" + "="*60)
    print("💬 NARRATOR")
    print("="*60)
    
    user_message = state.get("user_message", "")
    results = state.get("results", {})
    direct_response = state.get("direct_response", "")
    
    if direct_response:
        print(f"   → Usando respuesta directa del planner")
        return {
            **state,
            "response": direct_response,
            "status": "done"
        }
    
    if not results:
        return {
            **state,
            "response": "No obtuve resultados. ¿Podrías reformular tu pregunta?",
            "status": "done"
        }
    
    results_text = json.dumps(results, ensure_ascii=False, default=str, indent=2)
    
    if len(results_text) > 4000:
        results_text = results_text[:4000] + "\n... (resultados truncados)"
    
    prompt = NARRATOR_PROMPT.format(
        user_message=user_message,
        results=results_text
    )
    
    response = call_llm(
        system_prompt=prompt,
        user_message="Presenta los resultados al usuario.",
        temperature=0.4
    )
    
    print(f"   Respuesta: {response[:150]}...")
    
    return {
        **state,
        "response": response,
        "status": "done"
    }