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
BASE_URL   = os.getenv("ONEBOX_BASE_URL",   "http://localhost:8000")
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
    ]


# ══════════════════════════════════════════════════════════════════════════════
# DISPATCH
# ══════════════════════════════════════════════════════════════════════════════

@server.call_tool()
async def call_tool(name: str, arguments: dict[str, Any]) -> list[types.TextContent]:
    handlers = {
        "onebox_chat":    _handle_chat,
        "onebox_reset":   _handle_reset,
        "onebox_history": _handle_history,
        "onebox_report":  _handle_report,
        "onebox_export":  _handle_export,
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


# ══════════════════════════════════════════════════════════════════════════════
# ENTRY POINT
# ══════════════════════════════════════════════════════════════════════════════

async def main():
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
