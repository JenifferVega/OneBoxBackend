#!/usr/bin/env python3
"""MCP server para probar el agente OneBox de forma interactiva (modo debug).

Herramientas disponibles:
  onebox_chat     — envía un mensaje y mantiene historial multi-turno
  onebox_reset    — reinicia una sesión
  onebox_history  — muestra el historial de una sesión
  onebox_report   — genera reporte de feedback con análisis y sugerencias de catalog
  onebox_export   — guarda el reporte en un archivo .md

Ver mcp/README.md para instrucciones de instalación y configuración.
"""
import asyncio
import json
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

try:
    import httpx
    from mcp.server import Server
    from mcp.server.stdio import stdio_server
    from mcp import types
except ImportError as e:
    print(
        f"ERROR: dependencia faltante — {e}\n"
        "Instala con:  pip install mcp httpx",
        file=sys.stderr,
    )
    sys.exit(1)

# ── Configuración ─────────────────────────────────────────────────────────────
BASE_URL   = os.getenv("ONEBOX_BASE_URL",   "http://localhost:8006")
USER_ID    = os.getenv("ONEBOX_USER_ID",    "debug-user-001")
USER_EMAIL = os.getenv("ONEBOX_USER_EMAIL", "debug@onebox.com")
REPORTS_DIR = Path(os.getenv("ONEBOX_REPORTS_DIR", Path(__file__).parent / "reports"))

DEFAULT_SESSION = "default"

# ── Estado de sesiones en memoria ─────────────────────────────────────────────
# { session_id: { "history": [...], "turns_meta": [...] } }
_sessions: dict[str, dict] = {}

server = Server("onebox-chat")


# ══════════════════════════════════════════════════════════════════════════════
# DEFINICIÓN DE HERRAMIENTAS
# ══════════════════════════════════════════════════════════════════════════════

@server.list_tools()
async def list_tools() -> list[types.Tool]:
    return [
        types.Tool(
            name="onebox_chat",
            description=(
                "Envía un mensaje al agente OneBox en modo debug y recibe su respuesta. "
                "El historial se mantiene automáticamente por sesión, simulando turnos reales. "
                "Usa siempre el mismo session_id en una misma conversación."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "message": {
                        "type": "string",
                        "description": "Mensaje a enviar al agente",
                    },
                    "session_id": {
                        "type": "string",
                        "description": "ID de sesión (usa el mismo en todos los turnos de la conversación)",
                        "default": DEFAULT_SESSION,
                    },
                },
                "required": ["message"],
            },
        ),
        types.Tool(
            name="onebox_reset",
            description="Reinicia el historial de una sesión para empezar una conversación nueva.",
            inputSchema={
                "type": "object",
                "properties": {
                    "session_id": {
                        "type": "string",
                        "description": "ID de sesión a reiniciar",
                        "default": DEFAULT_SESSION,
                    },
                },
            },
        ),
        types.Tool(
            name="onebox_history",
            description="Muestra el historial de mensajes de una sesión.",
            inputSchema={
                "type": "object",
                "properties": {
                    "session_id": {
                        "type": "string",
                        "description": "ID de sesión",
                        "default": DEFAULT_SESSION,
                    },
                },
            },
        ),
        types.Tool(
            name="onebox_report",
            description=(
                "Genera un reporte completo de feedback de la sesión: conversación, "
                "análisis por turno (iteraciones del planner, herramientas usadas, "
                "errores de validación, replans) y sugerencias concretas para mejorar catalog.py."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "session_id": {
                        "type": "string",
                        "description": "ID de sesión a analizar",
                        "default": DEFAULT_SESSION,
                    },
                },
            },
        ),
        types.Tool(
            name="onebox_export",
            description=(
                "Guarda el reporte de feedback de una sesión como archivo .md "
                "en la carpeta mcp/reports/. Útil para compartir o revisar después."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "session_id": {
                        "type": "string",
                        "description": "ID de sesión a exportar",
                        "default": DEFAULT_SESSION,
                    },
                    "filename": {
                        "type": "string",
                        "description": "Nombre del archivo (sin extensión). Por defecto usa la fecha y sesión.",
                    },
                },
            },
        ),
        types.Tool(
            name="onebox_from_text_preview",
            description=(
                "Llama a POST /api/text/analyze con un texto pegado (conversación WhatsApp, correo, notas). "
                "Devuelve el preview del agente en modo debug: participantes detectados, tareas con assigned_to "
                "y fechas, sin crear nada en la base de datos. Útil para verificar la calidad del análisis."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "text": {
                        "type": "string",
                        "description": "Texto o conversación a analizar",
                    },
                    "source": {
                        "type": "string",
                        "description": "Origen del texto: 'whatsapp', 'email', 'notes', etc.",
                        "default": "whatsapp",
                    },
                },
                "required": ["text"],
            },
        ),
        types.Tool(
            name="onebox_from_text_create",
            description=(
                "Llama a POST /api/projects/from-text con un texto pegado. "
                "Crea el proyecto REAL en la base de datos usando el agente completo: "
                "detecta participantes, crea tareas con assigned_to y fechas. "
                "Retorna el projectId creado y la respuesta del agente."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "text": {
                        "type": "string",
                        "description": "Texto o conversación desde la que crear el proyecto",
                    },
                    "name": {
                        "type": "string",
                        "description": "Nombre del proyecto (opcional, el agente lo infiere si no se da)",
                    },
                },
                "required": ["text"],
            },
        ),
        types.Tool(
            name="onebox_notify_schedule",
            description=(
                "Prueba una notificación ONE-TIME programada. "
                "Llama directamente a enviar_notificacion con scheduled_at. "
                "Si hay TWILIO_MESSAGING_SERVICE_SID configurado, Twilio la agenda nativamente. "
                "Si no, queda en DynamoDB con status=pending para que el dispatcher la envíe. "
                "Usa onebox_dispatch_pending para dispararla manualmente sin esperar EventBridge."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "destinatario": {
                        "type": "string",
                        "description": "Teléfono E.164 (ej: +50494622817) o email si canal=email",
                    },
                    "mensaje": {
                        "type": "string",
                        "description": "Texto del mensaje",
                    },
                    "canal": {
                        "type": "string",
                        "description": "whatsapp | sms | email",
                        "default": "whatsapp",
                    },
                    "scheduled_at": {
                        "type": "string",
                        "description": "Fecha/hora UTC ISO 8601. Ej: '2026-06-19T09:00:00Z'. Mínimo 15 min en el futuro para Twilio nativo.",
                    },
                    "project_id": {
                        "type": "string",
                        "description": "ID del proyecto relacionado (opcional)",
                        "default": "",
                    },
                    "project_name": {
                        "type": "string",
                        "description": "Nombre del proyecto (opcional)",
                        "default": "",
                    },
                },
                "required": ["destinatario", "mensaje", "scheduled_at"],
            },
        ),
        types.Tool(
            name="onebox_notify_recurring",
            description=(
                "Prueba una notificación RECURRENTE semanal. "
                "Guarda en DynamoDB con isRecurring=True y los días configurados. "
                "Usa onebox_dispatch_pending para simular que el dispatcher de EventBridge corre "
                "y verificar que la notificación se envía en los días correctos."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "destinatario": {
                        "type": "string",
                        "description": "Teléfono E.164 o email",
                    },
                    "mensaje": {
                        "type": "string",
                        "description": "Texto del mensaje recurrente",
                    },
                    "canal": {
                        "type": "string",
                        "description": "whatsapp | sms | email",
                        "default": "whatsapp",
                    },
                    "recurring_days": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Días de la semana: monday, tuesday, wednesday, thursday, friday, saturday, sunday",
                    },
                    "project_id": {
                        "type": "string",
                        "description": "ID del proyecto relacionado (opcional)",
                        "default": "",
                    },
                    "project_name": {
                        "type": "string",
                        "description": "Nombre del proyecto (opcional)",
                        "default": "",
                    },
                },
                "required": ["destinatario", "mensaje", "recurring_days"],
            },
        ),
        types.Tool(
            name="onebox_dispatch_pending",
            description=(
                "Dispara manualmente el dispatcher de notificaciones programadas. "
                "Equivale a lo que hace EventBridge cada hora: llama POST /api/scheduled/dispatch-pending. "
                "Úsalo para probar que las notificaciones con status=pending se envían correctamente "
                "sin tener que esperar el cron de AWS."
            ),
            inputSchema={
                "type": "object",
                "properties": {},
            },
        ),
        types.Tool(
            name="onebox_task",
            description=(
                "Prueba acciones sobre TAREAS en lenguaje natural: listar, desbloquear, "
                "completar, reasignar o eliminar. Ejercita listar_tareas, actualizar_tarea y "
                "eliminar_tarea. Ojo: reasignar y eliminar piden CONFIRMACIÓN → responde luego "
                "con onebox_confirm en la misma sesión."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "accion":      {"type": "string", "enum": ["listar", "desbloquear", "bloquear", "completar", "reasignar", "eliminar"]},
                    "proyecto":    {"type": "string", "description": "Nombre del proyecto"},
                    "tarea":       {"type": "string", "description": "Texto de la tarea (no requerido para 'listar')"},
                    "responsable": {"type": "string", "description": "Solo para 'reasignar': nuevo responsable"},
                    "session_id":  {"type": "string"},
                },
                "required": ["accion", "proyecto"],
            },
        ),
        types.Tool(
            name="onebox_project",
            description=(
                "Prueba ADMINISTRACIÓN de proyecto en lenguaje natural: editar (nombre/descripción/"
                "estado), invitar a alguien, o quitar a un participante. Ejercita actualizar_proyecto, "
                "invitar_usuario y quitar_participante. Invitar y quitar piden CONFIRMACIÓN → usa "
                "onebox_confirm después."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "accion":     {"type": "string", "enum": ["editar", "invitar", "quitar"]},
                    "proyecto":   {"type": "string", "description": "Nombre del proyecto"},
                    "detalle":    {"type": "string", "description": "Qué cambiar / a quién invitar o quitar (ej: 'renómbralo a Alpha 2' o 'invita a juan@x.com')"},
                    "session_id": {"type": "string"},
                },
                "required": ["accion", "proyecto", "detalle"],
            },
        ),
        types.Tool(
            name="onebox_send",
            description=(
                "Prueba el envío a una persona por NOMBRE o contacto, con programación en LENGUAJE "
                "NATURAL. Ejercita resolver_persona + el cálculo de tiempo (programar) + confirmación. "
                "Ej: a='Jesus Vega', mensaje='revisa el informe', cuando='en dos horas'. Enviar pide "
                "CONFIRMACIÓN → usa onebox_confirm después."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "a":          {"type": "string", "description": "Nombre de la persona, o su email/teléfono"},
                    "mensaje":    {"type": "string"},
                    "canal":      {"type": "string", "enum": ["whatsapp", "sms", "email"], "default": "whatsapp"},
                    "cuando":     {"type": "string", "description": "Opcional, lenguaje natural: 'en dos horas', 'a la 1pm', 'mañana a las 9', 'cada lunes'. Vacío = enviar ya."},
                    "session_id": {"type": "string"},
                },
                "required": ["a", "mensaje"],
            },
        ),
        types.Tool(
            name="onebox_confirm",
            description=(
                "Continúa un flujo que pidió CONFIRMACIÓN respondiendo sí/no en la MISMA sesión. "
                "Úsalo después de onebox_task/onebox_project/onebox_send cuando el agente pidió confirmar."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "session_id": {"type": "string", "description": "La misma sesión del paso anterior"},
                    "confirmar":  {"type": "boolean", "default": True, "description": "true = sí; false = cancelar"},
                },
                "required": ["session_id"],
            },
        ),
        types.Tool(
            name="onebox_onboarding",
            description=(
                "Prueba el flujo REAL de onboarding desde texto: hace el preview "
                "(/api/text/analyze) y luego crea el proyecto desde el draft "
                "(/api/projects/from-document-draft), que ahora analiza el TEXTO COMPLETO "
                "para generar insights profundos. OJO: crea DATOS REALES (proyecto + insights "
                "en DynamoDB), NO es dry-run. Devuelve el proyecto creado y sus insights para "
                "inspeccionar la profundidad (resumen, tipo real, perfil, insight clave, conteos)."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "text":     {"type": "string", "description": "Texto/transcript a analizar (conversación, brief, acta)."},
                    "name":     {"type": "string", "description": "Opcional: forzar el nombre del proyecto (si no, usa el sugerido por la IA)."},
                    "channels": {"type": "array", "items": {"type": "string"}, "description": "Canales del proyecto. Default ['Gmail']."},
                },
                "required": ["text"],
            },
        ),
        types.Tool(
            name="onebox_onboarding_dryrun",
            description=(
                "DRY-RUN del análisis de onboarding: corre la IA REAL sobre el TEXTO COMPLETO "
                "para ver la calidad/profundidad de los insights, pero NO crea proyecto ni "
                "escribe NADA en DynamoDB. Ideal para verificar capacidades sin ensuciar datos. "
                "Devuelve summary, tipo real, perfil del cliente, insight clave y las listas de "
                "tareas/trabajo hecho/bloqueos/riesgos/decisiones/métricas/problemas técnicos."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "text": {"type": "string", "description": "Texto/transcript a analizar (conversación, brief, acta)."},
                },
                "required": ["text"],
            },
        ),
    ]


# ══════════════════════════════════════════════════════════════════════════════
# DISPATCH
# ══════════════════════════════════════════════════════════════════════════════

@server.call_tool()
async def call_tool(name: str, arguments: dict[str, Any]) -> list[types.TextContent]:
    handlers = {
        "onebox_chat":              _handle_chat,
        "onebox_reset":             _handle_reset,
        "onebox_history":           _handle_history,
        "onebox_report":            _handle_report,
        "onebox_export":            _handle_export,
        "onebox_from_text_preview": _handle_from_text_preview,
        "onebox_from_text_create":  _handle_from_text_create,
        "onebox_notify_schedule":   _handle_notify_schedule,
        "onebox_notify_recurring":  _handle_notify_recurring,
        "onebox_dispatch_pending":  _handle_dispatch_pending,
        "onebox_task":              _handle_task,
        "onebox_project":           _handle_project,
        "onebox_send":              _handle_send,
        "onebox_confirm":           _handle_confirm,
        "onebox_onboarding":        _handle_onboarding,
        "onebox_onboarding_dryrun": _handle_onboarding_dryrun,
    }
    handler = handlers.get(name)
    if not handler:
        return [types.TextContent(type="text", text=f"Herramienta desconocida: {name}")]
    if asyncio.iscoroutinefunction(handler):
        return await handler(arguments)
    return handler(arguments)


# ══════════════════════════════════════════════════════════════════════════════
# HANDLERS
# ══════════════════════════════════════════════════════════════════════════════

async def _handle_chat(args: dict) -> list[types.TextContent]:
    message    = (args.get("message") or "").strip()
    session_id = args.get("session_id") or DEFAULT_SESSION

    if not message:
        return [types.TextContent(type="text", text="⚠️ El mensaje no puede estar vacío.")]

    session  = _sessions.setdefault(session_id, {"history": [], "turns_meta": []})
    history  = session["history"]

    payload = {"message": message, "history": history, "debug": True}
    headers = {
        "x-user-id":    USER_ID,
        "x-user-email": USER_EMAIL,
        "Content-Type": "application/json",
    }

    try:
        async with httpx.AsyncClient(timeout=90.0) as client:
            resp = await client.post(f"{BASE_URL}/chat", json=payload, headers=headers)
            resp.raise_for_status()
            data = resp.json()
    except httpx.ConnectError:
        return [types.TextContent(
            type="text",
            text=f"❌ No se pudo conectar a {BASE_URL}\n¿Está corriendo el servidor? → `uvicorn main:app --reload`",
        )]
    except httpx.HTTPStatusError as e:
        return [types.TextContent(
            type="text",
            text=f"❌ Error HTTP {e.response.status_code}: {e.response.text[:400]}",
        )]
    except Exception as e:
        return [types.TextContent(type="text", text=f"❌ Error inesperado: {e}")]

    agent_response = data.get("response", "")
    tools_used     = data.get("toolsUsed", [])
    debug_info     = data.get("debug_info") or {}

    # Acumular historial
    history.append({"role": "user",      "content": message})
    history.append({"role": "assistant", "content": agent_response})

    # Guardar metadata del turno para el reporte
    session["turns_meta"].append({
        "turn":       len(session["turns_meta"]) + 1,
        "message":    message,
        "response":   agent_response,
        "tools_used": tools_used,
        "debug_info": debug_info,
        "timestamp":  datetime.now().isoformat(),
    })

    # ── Formatear respuesta ───────────────────────────────────────────────────
    lines = [f"🤖 **Agente:** {agent_response}"]

    if tools_used:
        lines.append(f"\n🔧 **Herramientas:** `{'`, `'.join(tools_used)}`")

    decision  = debug_info.get("planner_decision", "")
    iteration = debug_info.get("iteration", 1)
    plan      = debug_info.get("plan", [])

    if decision:
        iter_warn = " ⚠️" if iteration > 1 else ""
        lines.append(f"📊 **Planner:** `{decision}` | iteraciones: **{iteration}**{iter_warn}")

    if plan:
        steps = [f"  {s['step']}. `{s['tool']}`" for s in plan]
        lines.append("📋 **Plan:**\n" + "\n".join(steps))

    # Pasos simulados (dry-run): el JSON de debug con params resueltos y resultado
    # simulado de cada paso. Aquí se ve, p.ej., el scheduled_at calculado por
    # 'programar', el contacto resuelto por resolver_persona, o un error de
    # validación/programación antes de tocar la base de datos real.
    sim_calls = debug_info.get("simulated_calls", [])
    if sim_calls:
        sim_lines = ["🧪 **Pasos simulados (dry-run):**"]
        for c in sim_calls:
            params = json.dumps(c.get("params", {}), ensure_ascii=False, default=str)
            result = json.dumps(c.get("simulated_result", {}), ensure_ascii=False, default=str)
            sim_lines.append(f"  {c.get('step')}. `{c.get('tool')}`")
            sim_lines.append(f"     params: `{params[:400]}`")
            sim_lines.append(f"     → resultado: `{result[:300]}`")
        lines.append("\n".join(sim_lines))

    turn = len(history) // 2
    lines.append(f"\n*(turno {turn} · sesión `{session_id}` · debug=true)*")

    return [types.TextContent(type="text", text="\n".join(lines))]


def _handle_reset(args: dict) -> list[types.TextContent]:
    session_id = args.get("session_id") or DEFAULT_SESSION
    prev = _sessions.get(session_id, {})
    prev_turns = len(prev.get("turns_meta", []))
    _sessions[session_id] = {"history": [], "turns_meta": []}
    return [types.TextContent(
        type="text",
        text=f"✅ Sesión `{session_id}` reiniciada. ({prev_turns} turnos anteriores borrados)",
    )]


def _handle_history(args: dict) -> list[types.TextContent]:
    session_id = args.get("session_id") or DEFAULT_SESSION
    session    = _sessions.get(session_id, {})
    history    = session.get("history", [])

    if not history:
        return [types.TextContent(type="text", text=f"La sesión `{session_id}` no tiene historial aún.")]

    lines = [f"📜 **Historial · sesión `{session_id}`** ({len(history) // 2} turnos)\n"]
    for msg in history:
        icon    = "👤" if msg["role"] == "user" else "🤖"
        content = (msg.get("content") or "")[:500]
        lines.append(f"{icon} {content}\n")

    return [types.TextContent(type="text", text="\n".join(lines))]


def _handle_report(args: dict) -> list[types.TextContent]:
    session_id = args.get("session_id") or DEFAULT_SESSION
    report_md  = _build_report(session_id)
    return [types.TextContent(type="text", text=report_md)]


def _handle_export(args: dict) -> list[types.TextContent]:
    session_id = args.get("session_id") or DEFAULT_SESSION
    filename   = args.get("filename") or f"report_{session_id}_{datetime.now().strftime('%Y%m%d_%H%M%S')}"

    if not filename.endswith(".md"):
        filename += ".md"

    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    output_path = REPORTS_DIR / filename

    report_md = _build_report(session_id)
    output_path.write_text(report_md, encoding="utf-8")

    return [types.TextContent(
        type="text",
        text=f"✅ Reporte guardado en:\n`{output_path}`",
    )]


# ══════════════════════════════════════════════════════════════════════════════
# GENERACIÓN DE REPORTE
# ══════════════════════════════════════════════════════════════════════════════

def _build_report(session_id: str) -> str:
    session    = _sessions.get(session_id, {})
    turns_meta = session.get("turns_meta", [])

    if not turns_meta:
        return f"⚠️ La sesión `{session_id}` no tiene turnos registrados."

    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    lines = [
        f"# Reporte de Feedback — OneBox Agent",
        f"**Sesión:** `{session_id}` · **Fecha:** {now} · **Turnos:** {len(turns_meta)}",
        "",
    ]

    # ── 1. Conversación completa ──────────────────────────────────────────────
    lines += ["## 1. Conversación completa", ""]
    for meta in turns_meta:
        lines += [
            f"### Turno {meta['turn']}",
            f"**👤 Usuario:** {meta['message']}",
            "",
            f"**🤖 Agente:** {meta['response']}",
            "",
        ]

    # ── 2. Análisis por turno ─────────────────────────────────────────────────
    lines += ["---", "## 2. Análisis por turno", ""]
    issues_found = []

    for meta in turns_meta:
        t          = meta["turn"]
        di         = meta.get("debug_info") or {}
        tools      = meta.get("tools_used", [])
        iteration  = di.get("iteration", 1)
        decision   = di.get("planner_decision", "N/A")
        plan       = di.get("plan", [])
        sim_calls  = di.get("simulated_calls", [])

        lines.append(f"#### Turno {t}: _{meta['message'][:80]}_")
        lines.append(f"- **Decisión del planner:** `{decision}`")
        lines.append(f"- **Iteraciones:** {iteration}" + (" ⚠️ múltiples iteraciones" if iteration > 1 else ""))
        lines.append(f"- **Herramientas ejecutadas:** {', '.join(f'`{t}`' for t in tools) if tools else 'ninguna'}")

        if plan:
            plan_str = " → ".join(f"`{s['tool']}`" for s in plan)
            lines.append(f"- **Plan generado:** {plan_str}")

        # Detectar errores de validación en simulated_calls
        val_errors = [
            c for c in sim_calls
            if isinstance(c.get("simulated_result"), dict)
            and c["simulated_result"].get("_validation_error")
        ]
        if val_errors:
            for ve in val_errors:
                err_msg = ve["simulated_result"].get("error", "")[:200]
                lines.append(f"- **❌ Error de validación paso {ve['step']} (`{ve['tool']}`):** {err_msg}")
                issues_found.append({
                    "turn": t,
                    "type": "validation_error",
                    "tool": ve["tool"],
                    "detail": err_msg,
                })

        # Detectar iteraciones altas
        if iteration > 1:
            issues_found.append({
                "turn": t,
                "type": "high_iterations",
                "detail": f"El planner necesitó {iteration} iteraciones para el mensaje: '{meta['message'][:60]}'",
            })

        lines.append("")

    # ── 3. Problemas detectados ───────────────────────────────────────────────
    lines += ["---", "## 3. Problemas detectados", ""]

    if not issues_found:
        lines.append("✅ No se detectaron problemas en esta sesión.")
    else:
        for issue in issues_found:
            if issue["type"] == "high_iterations":
                lines.append(f"- ⚠️ **Turno {issue['turn']} — Múltiples iteraciones:** {issue['detail']}")
            elif issue["type"] == "validation_error":
                lines.append(f"- ❌ **Turno {issue['turn']} — Validación fallida en `{issue['tool']}`:** {issue['detail']}")
    lines.append("")

    # ── 4. Sugerencias de mejora al catalog ───────────────────────────────────
    lines += ["---", "## 4. Sugerencias de mejora al catalog.py", ""]

    suggestions = _generate_catalog_suggestions(issues_found, turns_meta)
    if not suggestions:
        lines.append("✅ No se generaron sugerencias — la sesión fue correcta.")
    else:
        for s in suggestions:
            lines.append(f"### {s['title']}")
            lines.append(s["body"])
            lines.append("")

    # ── 5. Datos raw para entrenamiento ──────────────────────────────────────
    lines += ["---", "## 5. Datos para entrenamiento (JSON)", "", "```json"]
    training_data = [
        {
            "turn": m["turn"],
            "message": m["message"],
            "response": m["response"],
            "tools_used": m["tools_used"],
            "planner_decision": (m.get("debug_info") or {}).get("planner_decision"),
            "iterations": (m.get("debug_info") or {}).get("iteration", 1),
            "plan": (m.get("debug_info") or {}).get("plan", []),
        }
        for m in turns_meta
    ]
    lines.append(json.dumps(training_data, ensure_ascii=False, indent=2))
    lines.append("```")

    return "\n".join(lines)


def _generate_catalog_suggestions(issues: list[dict], turns_meta: list[dict]) -> list[dict]:
    suggestions = []
    seen = set()

    for issue in issues:
        key = (issue["type"], issue.get("tool", ""))
        if key in seen:
            continue
        seen.add(key)

        if issue["type"] == "high_iterations":
            # Busca el mensaje del turno para contexto
            turn_data = next((m for m in turns_meta if m["turn"] == issue["turn"]), {})
            msg = turn_data.get("message", "")
            suggestions.append({
                "title": f"⚠️ Turno {issue['turn']}: El planner tardó múltiples iteraciones",
                "body": (
                    f"**Mensaje:** _{msg}_\n\n"
                    "**Posible causa:** La regla en `REQUIRED_PARAMS` no es suficientemente explícita "
                    "o le falta un ejemplo de contraste ❌/✅.\n\n"
                    "**Acción sugerida:** Agregar un ejemplo INCORRECTO vs CORRECTO en la sección "
                    "correspondiente de `catalog.py` para el tipo de acción de este turno."
                ),
            })

        elif issue["type"] == "validation_error":
            tool = issue.get("tool", "")
            suggestions.append({
                "title": f"❌ Validación fallida en `{tool}` — reforzar ejemplo en catalog",
                "body": (
                    f"**Error detectado:** {issue['detail'][:200]}\n\n"
                    f"**Acción sugerida:** En `MULTISTEP_RECIPES` o `REQUIRED_PARAMS`, "
                    f"agregar un ejemplo explícito de cómo debe resolverse el parámetro "
                    f"inválido en `{tool}`. Usar el patrón ❌ INCORRECTO / ✅ CORRECTO."
                ),
            })

    return suggestions


async def _handle_from_text_preview(args: dict) -> list[types.TextContent]:
    text   = (args.get("text") or "").strip()
    source = args.get("source") or "whatsapp"

    if not text:
        return [types.TextContent(type="text", text="⚠️ El texto no puede estar vacío.")]

    payload = {"text": text, "source": source}
    headers = {"x-user-id": USER_ID, "x-user-email": USER_EMAIL, "Content-Type": "application/json"}

    try:
        async with httpx.AsyncClient(timeout=120.0) as client:
            resp = await client.post(f"{BASE_URL}/api/text/analyze", json=payload, headers=headers)
            resp.raise_for_status()
            data = resp.json()
    except httpx.ConnectError:
        return [types.TextContent(type="text", text=f"❌ No se pudo conectar a {BASE_URL}. ¿Está corriendo el servidor?")]
    except httpx.HTTPStatusError as e:
        return [types.TextContent(type="text", text=f"❌ Error HTTP {e.response.status_code}: {e.response.text[:400]}")]
    except Exception as e:
        return [types.TextContent(type="text", text=f"❌ Error: {e}")]

    suggestion   = data.get("suggestion", {})
    participants = suggestion.get("detected_participants", [])
    tasks        = suggestion.get("tasks", [])

    lines = [
        f"## 📋 Preview del proyecto (sin guardar)",
        f"**Nombre:** {suggestion.get('name', '—')}",
        f"**Tipo:** {suggestion.get('type', '—')}",
        f"**Descripción:** {suggestion.get('description', '—')[:300]}",
        f"**Draft ID:** `{data.get('draftId', '—')}`",
        "",
    ]

    if participants:
        lines.append(f"### 👥 Participantes detectados ({len(participants)}):")
        for p in participants:
            lines.append(f"  • **{p.get('name', '?')}** — {p.get('role', '')} {('📧 ' + p.get('email','')) if p.get('email') else ''}")
    else:
        lines.append("👥 No se detectaron participantes.")

    lines.append("")

    if tasks:
        lines.append(f"### ✅ Tareas detectadas ({len(tasks)}):")
        for t in tasks:
            assigned = f" → **{t['assigned_to']}**" if t.get('assigned_to') else ""
            due      = f" (hasta {t['due_date']})" if t.get('due_date') else ""
            lines.append(f"  • {t.get('text', '?')}{assigned}{due}")
    else:
        lines.append("✅ No se detectaron tareas.")

    if data.get("agentResponse"):
        lines += ["", f"🤖 **Respuesta del agente:** {data['agentResponse'][:400]}"]

    return [types.TextContent(type="text", text="\n".join(lines))]


async def _handle_from_text_create(args: dict) -> list[types.TextContent]:
    text = (args.get("text") or "").strip()
    name = (args.get("name") or "").strip()

    if not text:
        return [types.TextContent(type="text", text="⚠️ El texto no puede estar vacío.")]

    payload = {"text": text, "name": name or None, "channels": ["WhatsApp"], "source": "whatsapp"}
    headers = {"x-user-id": USER_ID, "x-user-email": USER_EMAIL, "Content-Type": "application/json"}

    try:
        async with httpx.AsyncClient(timeout=120.0) as client:
            resp = await client.post(f"{BASE_URL}/api/projects/from-text", json=payload, headers=headers)
            resp.raise_for_status()
            data = resp.json()
    except httpx.ConnectError:
        return [types.TextContent(type="text", text=f"❌ No se pudo conectar a {BASE_URL}. ¿Está corriendo el servidor?")]
    except httpx.HTTPStatusError as e:
        return [types.TextContent(type="text", text=f"❌ Error HTTP {e.response.status_code}: {e.response.text[:400]}")]
    except Exception as e:
        return [types.TextContent(type="text", text=f"❌ Error: {e}")]

    lines = [
        f"## ✅ Proyecto creado",
        f"**Nombre:** {data.get('name', '—')}",
        f"**Project ID:** `{data.get('projectId', '—')}`",
        f"**Herramientas usadas:** {', '.join(data.get('tools_used', []))}",
        "",
        f"🤖 **Respuesta del agente:**",
        data.get("response", "—"),
    ]

    return [types.TextContent(type="text", text="\n".join(lines))]


# ══════════════════════════════════════════════════════════════════════════════
# HANDLERS — NOTIFICACIONES PROGRAMADAS
# ══════════════════════════════════════════════════════════════════════════════

async def _handle_notify_schedule(args: dict) -> list[types.TextContent]:
    """Envía una notificación one-time con scheduled_at directamente al agente."""
    destinatario = (args.get("destinatario") or "").strip()
    mensaje      = (args.get("mensaje") or "").strip()
    canal        = (args.get("canal") or "whatsapp").strip()
    scheduled_at = (args.get("scheduled_at") or "").strip()
    project_id   = (args.get("project_id") or "").strip()
    project_name = (args.get("project_name") or "").strip()

    if not destinatario or not mensaje or not scheduled_at:
        return [types.TextContent(type="text", text="⚠️ destinatario, mensaje y scheduled_at son requeridos.")]

    # Llamamos al chat del agente con un mensaje estructurado para que use enviar_notificacion
    prompt = (
        f"Agenda una notificación para el {scheduled_at} UTC. "
        f"Canal: {canal}. "
        f"Destinatario: {destinatario}. "
        f"Mensaje: {mensaje}."
        + (f" Proyecto ID: {project_id}, nombre: {project_name}." if project_id else "")
    )

    session_id = f"notify_schedule_{datetime.now().strftime('%H%M%S')}"
    session = _sessions.setdefault(session_id, {"history": [], "turns_meta": []})

    payload = {"message": prompt, "history": [], "debug": True}
    headers = {
        "x-user-id":    USER_ID,
        "x-user-email": USER_EMAIL,
        "Content-Type": "application/json",
    }

    try:
        async with httpx.AsyncClient(timeout=90.0) as client:
            resp = await client.post(f"{BASE_URL}/chat", json=payload, headers=headers)
            resp.raise_for_status()
            data = resp.json()
    except httpx.ConnectError:
        return [types.TextContent(type="text", text=f"❌ No se pudo conectar a {BASE_URL}")]
    except httpx.HTTPStatusError as e:
        return [types.TextContent(type="text", text=f"❌ Error HTTP {e.response.status_code}: {e.response.text[:400]}")]
    except Exception as e:
        return [types.TextContent(type="text", text=f"❌ Error: {e}")]

    debug_info = data.get("debug_info") or {}
    plan       = debug_info.get("plan", [])
    tools_used = data.get("toolsUsed", [])

    lines = [
        "## 📅 Notificación programada (one-time)",
        f"**Destinatario:** `{destinatario}`",
        f"**Canal:** `{canal}`",
        f"**Scheduled at:** `{scheduled_at}`",
        f"**Mensaje:** {mensaje}",
        "",
        f"🤖 **Respuesta del agente:** {data.get('response', '')}",
    ]
    if tools_used:
        lines.append(f"🔧 **Herramientas:** `{'`, `'.join(tools_used)}`")
    if plan:
        steps = " → ".join(f"`{s['tool']}`" for s in plan)
        lines.append(f"📋 **Plan ejecutado:** {steps}")
    lines += [
        "",
        "💡 **Tip:** Usa `onebox_dispatch_pending` para verificar que el dispatcher la procesa correctamente.",
    ]

    return [types.TextContent(type="text", text="\n".join(lines))]


async def _handle_notify_recurring(args: dict) -> list[types.TextContent]:
    """Crea una notificación recurrente con recurring_days."""
    destinatario   = (args.get("destinatario") or "").strip()
    mensaje        = (args.get("mensaje") or "").strip()
    canal          = (args.get("canal") or "whatsapp").strip()
    recurring_days = args.get("recurring_days") or []
    project_id     = (args.get("project_id") or "").strip()
    project_name   = (args.get("project_name") or "").strip()

    if not destinatario or not mensaje or not recurring_days:
        return [types.TextContent(type="text", text="⚠️ destinatario, mensaje y recurring_days son requeridos.")]

    days_str = ", ".join(recurring_days)
    prompt = (
        f"Configura una notificación recurrente semanal. "
        f"Canal: {canal}. "
        f"Destinatario: {destinatario}. "
        f"Días: {days_str}. "
        f"Mensaje: {mensaje}."
        + (f" Proyecto ID: {project_id}, nombre: {project_name}." if project_id else "")
    )

    payload = {"message": prompt, "history": [], "debug": True}
    headers = {
        "x-user-id":    USER_ID,
        "x-user-email": USER_EMAIL,
        "Content-Type": "application/json",
    }

    try:
        async with httpx.AsyncClient(timeout=90.0) as client:
            resp = await client.post(f"{BASE_URL}/chat", json=payload, headers=headers)
            resp.raise_for_status()
            data = resp.json()
    except httpx.ConnectError:
        return [types.TextContent(type="text", text=f"❌ No se pudo conectar a {BASE_URL}")]
    except httpx.HTTPStatusError as e:
        return [types.TextContent(type="text", text=f"❌ Error HTTP {e.response.status_code}: {e.response.text[:400]}")]
    except Exception as e:
        return [types.TextContent(type="text", text=f"❌ Error: {e}")]

    debug_info = data.get("debug_info") or {}
    tools_used = data.get("toolsUsed", [])
    plan       = debug_info.get("plan", [])

    lines = [
        "## 🔁 Notificación recurrente creada",
        f"**Destinatario:** `{destinatario}`",
        f"**Canal:** `{canal}`",
        f"**Días:** {days_str}",
        f"**Mensaje:** {mensaje}",
        "",
        f"🤖 **Respuesta del agente:** {data.get('response', '')}",
    ]
    if tools_used:
        lines.append(f"🔧 **Herramientas:** `{'`, `'.join(tools_used)}`")
    if plan:
        steps = " → ".join(f"`{s['tool']}`" for s in plan)
        lines.append(f"📋 **Plan ejecutado:** {steps}")
    lines += [
        "",
        "💡 **Tip:** Usa `onebox_dispatch_pending` para simular que el cron de EventBridge corre ahora mismo.",
        f"   Si hoy es uno de [{days_str}], la notificación se enviará inmediatamente.",
    ]

    return [types.TextContent(type="text", text="\n".join(lines))]


async def _handle_dispatch_pending(args: dict) -> list[types.TextContent]:
    """Dispara manualmente el endpoint /api/scheduled/dispatch-pending."""
    headers = {
        "x-user-id":    USER_ID,
        "x-user-email": USER_EMAIL,
        "Content-Type": "application/json",
    }

    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.post(f"{BASE_URL}/api/scheduled/dispatch-pending", headers=headers)
            resp.raise_for_status()
            data = resp.json()
    except httpx.ConnectError:
        return [types.TextContent(type="text", text=f"❌ No se pudo conectar a {BASE_URL}")]
    except httpx.HTTPStatusError as e:
        return [types.TextContent(type="text", text=f"❌ Error HTTP {e.response.status_code}: {e.response.text[:400]}")]
    except Exception as e:
        return [types.TextContent(type="text", text=f"❌ Error: {e}")]

    evaluated = data.get("evaluated", 0)
    sent      = data.get("sent", 0)
    skipped   = data.get("skipped", 0)
    errors    = data.get("errors") or []

    status_icon = "✅" if not errors else "⚠️"
    lines = [
        f"## {status_icon} Dispatcher ejecutado",
        f"**Notificaciones evaluadas:** {evaluated}",
        f"**Enviadas:** {sent}",
        f"**Omitidas** (no era su hora/día, o ya enviadas hoy): {skipped}",
    ]

    if errors:
        lines.append(f"\n**❌ Errores ({len(errors)}):**")
        for err in errors[:10]:
            lines.append(f"  - {err}")

    if sent == 0 and evaluated > 0:
        lines += [
            "",
            "ℹ️ Todas las notificaciones fueron omitidas. Posibles razones:",
            "  - Las notificaciones recurrentes no aplican para hoy",
            "  - Las one-time aún no han llegado a su scheduled_at",
            "  - Las recurrentes ya fueron enviadas hoy (lastSentAt de hoy)",
        ]
    elif sent == 0 and evaluated == 0:
        lines.append("\nℹ️ No hay notificaciones pendientes en DynamoDB.")

    return [types.TextContent(type="text", text="\n".join(lines))]


# ══════════════════════════════════════════════════════════════════════════════
# HANDLERS DE FLUJOS NUEVOS (delegan en _handle_chat → reutilizan sesión,
# historial, plan y pasos simulados). Construyen un prompt en lenguaje natural
# para forzar el flujo que se quiere probar.
# ══════════════════════════════════════════════════════════════════════════════

async def _handle_task(args: dict) -> list[types.TextContent]:
    accion      = (args.get("accion") or "").strip().lower()
    proyecto    = (args.get("proyecto") or "").strip()
    tarea       = (args.get("tarea") or "").strip()
    responsable = (args.get("responsable") or "").strip()
    session_id  = args.get("session_id") or f"task_{datetime.now().strftime('%H%M%S')}"

    if accion in ("desbloquear", "bloquear", "completar", "reasignar", "eliminar") and not tarea:
        return [types.TextContent(type="text", text="⚠️ 'tarea' es requerida para esta acción.")]
    if accion == "reasignar" and not responsable:
        return [types.TextContent(type="text", text="⚠️ 'responsable' es requerido para reasignar.")]

    prompts = {
        "listar":      f"muéstrame las tareas del proyecto {proyecto}",
        "desbloquear": f"la tarea '{tarea}' del proyecto {proyecto} ya no está bloqueada",
        "bloquear":    f"marca como bloqueada la tarea '{tarea}' del proyecto {proyecto}",
        "completar":   f"marca como hecha la tarea '{tarea}' del proyecto {proyecto}",
        "reasignar":   f"reasigna la tarea '{tarea}' del proyecto {proyecto} a {responsable}",
        "eliminar":    f"elimina la tarea '{tarea}' del proyecto {proyecto}",
    }
    prompt = prompts.get(accion)
    if not prompt:
        return [types.TextContent(type="text", text=f"⚠️ acción no reconocida: '{accion}'")]
    return await _handle_chat({"message": prompt, "session_id": session_id})


async def _handle_project(args: dict) -> list[types.TextContent]:
    accion     = (args.get("accion") or "").strip().lower()
    proyecto   = (args.get("proyecto") or "").strip()
    detalle    = (args.get("detalle") or "").strip()
    session_id = args.get("session_id") or f"project_{datetime.now().strftime('%H%M%S')}"

    prompts = {
        "editar":  f"en el proyecto {proyecto}: {detalle}",
        "invitar": f"invita al proyecto {proyecto} a {detalle}",
        "quitar":  f"quita del proyecto {proyecto} a {detalle}",
    }
    prompt = prompts.get(accion)
    if not prompt:
        return [types.TextContent(type="text", text=f"⚠️ acción no reconocida: '{accion}'")]
    return await _handle_chat({"message": prompt, "session_id": session_id})


async def _handle_send(args: dict) -> list[types.TextContent]:
    a          = (args.get("a") or "").strip()
    mensaje    = (args.get("mensaje") or "").strip()
    canal      = (args.get("canal") or "whatsapp").strip().lower()
    cuando     = (args.get("cuando") or "").strip()
    session_id = args.get("session_id") or f"send_{datetime.now().strftime('%H%M%S')}"

    if not a or not mensaje:
        return [types.TextContent(type="text", text="⚠️ 'a' y 'mensaje' son requeridos.")]

    base = f"envía un correo a {a}" if canal == "email" else f"manda un {canal} a {a}"
    prompt = f"{base} que diga: \"{mensaje}\""
    if cuando:
        prompt += f", {cuando}"
    return await _handle_chat({"message": prompt, "session_id": session_id})


async def _handle_confirm(args: dict) -> list[types.TextContent]:
    session_id = args.get("session_id")
    if not session_id:
        return [types.TextContent(type="text", text="⚠️ 'session_id' es requerido (la misma sesión del paso anterior).")]
    confirmar = args.get("confirmar", True)
    msg = "sí, confírmalo" if confirmar else "no, cancela"
    return await _handle_chat({"message": msg, "session_id": session_id})


async def _handle_onboarding(args: dict) -> list[types.TextContent]:
    """Flujo REAL de onboarding: preview → crear-desde-draft (analiza texto completo).
    Crea datos reales y devuelve los insights para inspeccionar la profundidad."""
    text     = (args.get("text") or "").strip()
    channels = args.get("channels") or ["Gmail"]
    if not text:
        return [types.TextContent(type="text", text="⚠️ 'text' no puede estar vacío.")]

    headers = {"x-user-id": USER_ID, "x-user-email": USER_EMAIL, "Content-Type": "application/json"}

    try:
        async with httpx.AsyncClient(timeout=180.0) as client:
            # 1) Preview → draftId + sugerencia
            pr = await client.post(f"{BASE_URL}/api/text/analyze",
                                   json={"text": text, "source": "paste"}, headers=headers)
            pr.raise_for_status()
            preview = pr.json()
            sug = preview.get("suggestion", {}) or {}
            draft_id = preview.get("draftId", "")
            if not draft_id:
                return [types.TextContent(type="text", text=f"❌ El preview no devolvió draftId. {str(preview)[:400]}")]

            det = [
                {"name": p.get("name", ""), "email": p.get("email", ""),
                 "phone": p.get("phone", ""), "role": p.get("role", "")}
                for p in (sug.get("detected_participants") or [])
            ]
            draft_payload = {
                "draftId": draft_id,
                "name": args.get("name") or sug.get("name") or "Proyecto sin nombre",
                "type": sug.get("type", "Otro"),
                "description": sug.get("description", ""),
                "sourceText": text,            # el backend también lo recupera de S3
                "channels": channels,
                "detectedParticipants": det,
            }
            # 2) Crear desde draft → ruta con análisis de TEXTO COMPLETO
            cr = await client.post(f"{BASE_URL}/api/projects/from-document-draft",
                                   json=draft_payload, headers=headers)
            cr.raise_for_status()
            created = cr.json()
            project_id = created.get("projectId", "")

            # 3) Traer los insights del proyecto para ver la profundidad
            insights = []
            try:
                ir = await client.get(f"{BASE_URL}/api/insights", headers=headers)
                if ir.status_code == 200:
                    insights = [i for i in ir.json() if i.get("projectId") == project_id]
            except Exception:
                pass
    except httpx.ConnectError:
        return [types.TextContent(type="text", text=f"❌ No se pudo conectar a {BASE_URL}. ¿Está corriendo el servidor?")]
    except httpx.HTTPStatusError as e:
        return [types.TextContent(type="text", text=f"❌ Error HTTP {e.response.status_code}: {e.response.text[:500]}")]
    except Exception as e:
        return [types.TextContent(type="text", text=f"❌ Error: {e}")]

    by_type: dict = {}
    for i in insights:
        by_type.setdefault(i.get("type", "?"), []).append(i)

    def _titles(t, n=6):
        return [(x.get("title") or x.get("detected") or "")[:200] for x in by_type.get(t, [])[:n]]

    lines = [
        "## 🚀 Onboarding (flujo REAL — crea datos)",
        f"**Proyecto creado:** {created.get('name', '—')}  (`{project_id}`)",
        f"**Insights generados:** {(created.get('insightsGenerated') or {}).get('count', '—')}  ·  recuperados del proyecto: {len(insights)}",
        "",
    ]
    summ = by_type.get("summary", [])
    if summ:
        lines += ["### 📊 Resumen", (summ[0].get("description") or summ[0].get("action") or "")[:900], ""]
    for t, label in [("project_characterization", "🎯 Tipo real"),
                     ("client_profile", "👤 Perfil cliente"),
                     ("key_insight", "💡 Insight clave")]:
        vals = _titles(t)
        if vals:
            lines.append(f"**{label}:** " + " | ".join(vals))
    if insights:
        counts = ", ".join(f"{t}={len(v)}" for t, v in sorted(by_type.items()))
        lines += ["", f"### 📈 Conteo por tipo: {counts}"]
    return [types.TextContent(type="text", text="\n".join(lines))]


async def _handle_onboarding_dryrun(args: dict) -> list[types.TextContent]:
    """DRY-RUN: corre el análisis de la IA sobre el texto completo SIN crear nada."""
    text = (args.get("text") or "").strip()
    if not text:
        return [types.TextContent(type="text", text="⚠️ 'text' no puede estar vacío.")]

    headers = {"x-user-id": USER_ID, "x-user-email": USER_EMAIL, "Content-Type": "application/json"}
    try:
        async with httpx.AsyncClient(timeout=180.0) as client:
            r = await client.post(f"{BASE_URL}/api/text/analyze-insights-dryrun",
                                  json={"text": text}, headers=headers)
            r.raise_for_status()
            data = r.json()
    except httpx.ConnectError:
        return [types.TextContent(type="text", text=f"❌ No se pudo conectar a {BASE_URL}. ¿Está corriendo el servidor?")]
    except httpx.HTTPStatusError as e:
        return [types.TextContent(type="text", text=f"❌ Error HTTP {e.response.status_code}: {e.response.text[:500]}")]
    except Exception as e:
        return [types.TextContent(type="text", text=f"❌ Error: {e}")]

    a = data.get("analysis", {}) or {}

    def _txt(item):
        return item.get("text", "") if isinstance(item, dict) else str(item)

    def _list(key, n=40):
        return [_txt(x) for x in (a.get(key) or [])[:n]]

    lines = [
        "## 🧪 Onboarding DRY-RUN (IA real, SIN crear datos)",
        f"**generated:** {data.get('generated')}  ·  **insights (simulados):** {data.get('insightCount')}",
        "",
        "### 📊 Resumen",
        (a.get("summary") or "—"),
        "",
        f"**🎯 Tipo real:** {a.get('project_type_real') or '—'}",
        f"**👤 Perfil cliente:** {a.get('client_profile') or '—'}",
        f"**💡 Insight clave:** {a.get('key_insight') or '—'}",
        "",
    ]
    for key, label in [("tasks", "✅ Tareas pendientes"), ("work_done", "🏁 Trabajo hecho"),
                       ("blockers", "🚧 Bloqueos"), ("risks", "⚠️ Riesgos"),
                       ("decisions", "🧭 Decisiones"), ("metrics", "📈 Métricas"),
                       ("tech_issues", "🔧 Problemas técnicos")]:
        vals = _list(key)
        lines.append(f"### {label} ({len(vals)})")
        for v in vals:
            if v:
                lines.append(f"  • {v[:220]}")
        lines.append("")
    if data.get("reason"):
        lines.append(f"⚠️ reason: {data['reason']}")
    return [types.TextContent(type="text", text="\n".join(lines))]


# ══════════════════════════════════════════════════════════════════════════════
# ENTRY POINT
# ══════════════════════════════════════════════════════════════════════════════

async def main():
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
