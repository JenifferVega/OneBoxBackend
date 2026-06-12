"""⚡ EXECUTOR: ejecuta el plan paso a paso (sin LLM).

En modo debug (debug_mode=True) simula cada herramienta sin tocar DynamoDB.
Los contextvars multi-tenant (set_current_user) los fija el caller; el grafo
corre síncrono en el mismo stack, así que _current_uid() resuelve correctamente.
execute_tool nunca lanza (devuelve {"error": ...}); el try/except es un backstop.
"""
import json
import uuid

from agent.graph.nodes.executor.resolve import resolve_params, _get_path
from agent.graph.nodes.executor.validators import validate_tool_params
from agent.graph.state import AgentState
from agent.tools import execute_tool

# ── Cache in-memory para proyectos creados en dry-run ─────────────────────────
# Keyed por session_id. Persiste mientras el servidor esté corriendo.
# Se limpia con clear_dry_run_cache(session_id) cuando se hace onebox_reset.
_DRY_RUN_PROJECT_CACHE: dict[str, list] = {}
# Keyed por session_id → {projectId: [participants]}
_DRY_RUN_PARTICIPANTS_CACHE: dict[str, dict] = {}


def get_dry_run_projects(session_id: str) -> list:
    return _DRY_RUN_PROJECT_CACHE.get(session_id, [])


def add_dry_run_project(session_id: str, project: dict) -> None:
    if session_id not in _DRY_RUN_PROJECT_CACHE:
        _DRY_RUN_PROJECT_CACHE[session_id] = []
    _DRY_RUN_PROJECT_CACHE[session_id].append(project)


def add_dry_run_participants(session_id: str, project_id: str, participants: list) -> None:
    if session_id not in _DRY_RUN_PARTICIPANTS_CACHE:
        _DRY_RUN_PARTICIPANTS_CACHE[session_id] = {}
    _DRY_RUN_PARTICIPANTS_CACHE[session_id][project_id] = participants


def get_dry_run_participants(session_id: str, project_id: str) -> list:
    return _DRY_RUN_PARTICIPANTS_CACHE.get(session_id, {}).get(project_id, [])


def clear_dry_run_cache(session_id: str) -> None:
    _DRY_RUN_PROJECT_CACHE.pop(session_id, None)
    _DRY_RUN_PARTICIPANTS_CACHE.pop(session_id, None)


# Resultados simulados realistas por herramienta (dry-run)
_DRY_RUN_RESULTS = {
    "crear_proyecto":              lambda p: {"success": True, "projectId": f"proj-DRYRUN-{uuid.uuid4().hex[:8]}", "name": p.get("name", "?")},
    "crear_tarea":                 lambda p: {"success": True, "taskId": f"task-DRYRUN-{uuid.uuid4().hex[:8]}", "text": p.get("text", "?")},
    "crear_insight":               lambda p: {"success": True, "insightId": f"ins-DRYRUN-{uuid.uuid4().hex[:8]}", "title": p.get("title", "?")},
    "crear_recordatorio":          lambda p: {"success": True, "reminder_id": f"rem-DRYRUN-{uuid.uuid4().hex[:8]}", "titulo": p.get("titulo", "?")},
    "enviar_correo":               lambda p: {"success": True, "email_id": f"email-DRYRUN-{uuid.uuid4().hex[:8]}", "status": "simulated", "destinatario": p.get("destinatario_email", "?")},
    "enviar_notificacion":         lambda p: {"success": True, "sid": f"SM-DRYRUN-{uuid.uuid4().hex[:16]}", "status": "simulated", "canal": p.get("canal", "?"), "destinatario": p.get("destinatario", "?")},
    "asignar_correo_a_proyecto":   lambda p: {"success": True, "conversationId": p.get("conversation_id", "?"), "projectId": p.get("project_id", "?")},
    "listar_proyectos":            lambda p: {"count": 2, "projects": [
        {"projectId": "proj-DRYRUN-aaa111", "name": "Proyecto Demo A", "type": "Backend", "status": "active"},
        {"projectId": "proj-DRYRUN-bbb222", "name": "Proyecto Demo B", "type": "Marketing", "status": "active"},
    ], "_dry_run": True},
    "listar_correos":              lambda p: {"count": 2, "emails": [
        {"email_id": "email-DRYRUN-cc1", "subject": "Reunión mañana", "from": "equipo@empresa.com"},
        {"email_id": "email-DRYRUN-cc2", "subject": "Factura pendiente", "from": "proveedor@empresa.com"},
    ], "_dry_run": True},
    "inspeccionar_correo":         lambda p: {"email_id": p.get("email_id", "?"), "subject": "[DRY RUN]", "body": "Contenido simulado del correo.", "_dry_run": True},
    "analizar_inbox":              lambda p: {"count": 2, "emails": [
        {"email_id": "email-DRYRUN-cc1", "subject": "Reunión mañana", "suggested_project": "proj-DRYRUN-aaa111"},
    ], "_dry_run": True},
    "listar_notificaciones":       lambda p: {"count": 1, "notifications": [
        {"id": "notif-DRYRUN-001", "message": "Tarea pendiente", "status": "unread"},
    ], "_dry_run": True},
    "obtener_contactos_proyecto":  lambda p: {"total_contactos": 2, "contactos": [
        {"nombre": "Ana Torres", "telefono": "+50494622817", "rol": "Coordinadora", "tareas_pendientes": 2},
        {"nombre": "Carlos López", "telefono": "+50494622818", "rol": "Desarrollador", "tareas_pendientes": 1},
    ], "_dry_run": True},
    "verificar_sla":               lambda p: {"total_alerts": 0, "alerts": [], "_dry_run": True},
    "clasificar_mensajes_automatico": lambda p: {"suggestions": [], "_dry_run": True},
    "resumen_proactivo":           lambda p: {"projects_count": 0, "suggested_actions": [], "_dry_run": True},
}


def _simulate_tool(tool_name: str, params: dict, session_id: str = "") -> dict:
    """Simula la ejecución de una herramienta sin tocar ninguna base de datos."""
    if tool_name == "listar_proyectos":
        base = [
            {"projectId": "proj-DRYRUN-aaa111", "name": "Proyecto Demo A", "type": "Backend",  "status": "active"},
            {"projectId": "proj-DRYRUN-bbb222", "name": "Proyecto Demo B", "type": "Marketing", "status": "active"},
        ]
        extra = get_dry_run_projects(session_id) if session_id else []
        all_projects = base + extra
        return {"count": len(all_projects), "projects": all_projects, "_dry_run": True}

    if tool_name == "obtener_contactos_proyecto":
        project_id = params.get("project_id", "")
        # Intenta por project_id exacto, luego por fallback "latest" de la sesión
        cached = get_dry_run_participants(session_id, project_id) if session_id else []
        if not cached and session_id:
            cached = get_dry_run_participants(session_id, "_latest")
        if cached:
            return {"total_contactos": len(cached), "contactos": cached, "_dry_run": True}
        return {"total_contactos": 0, "contactos": [], "_dry_run": True,
                "_note": "No hay participantes registrados para este proyecto en dry-run"}

    sim = _DRY_RUN_RESULTS.get(tool_name)
    if sim:
        return sim(params)
    return {"_dry_run": True, "tool": tool_name, "params": params}


def _execute_foreach_step(
    tool_name: str,
    step_num: int,
    params: dict,
    foreach_key: str,
    foreach_spec: dict,
    results: dict,
    debug_mode: bool,
    session_id: str = "",
) -> dict:
    """Expande un paso con foreach: ejecuta el tool una vez por cada item de la lista.

    foreach_spec ejemplo:
      {"from_step": 2, "foreach": "contactos", "extract": "telefono"}

    Retorna un dict consolidado con:
      - sent: lista de {nombre, <extract_field>, resultado}
      - skipped: lista de {nombre, motivo}
      - sent_count / skipped_count
    """
    step_ref = foreach_spec["from_step"]
    source = results.get(step_ref, {})
    foreach_path = foreach_spec["foreach"]        # e.g. "contactos"
    extract_field = foreach_spec.get("extract", "telefono")
    name_field = foreach_spec.get("name_field", "nombre")

    # Obtiene la lista; soporta dot-notation ("a.b") o key directo
    items_list = _get_path(source, foreach_path) if "." in foreach_path else (
        source.get(foreach_path, []) if isinstance(source, dict) else []
    )

    if not items_list:
        print(f"   ⚠️ foreach: lista '{foreach_path}' vacía o no encontrada")
        return {"sent_count": 0, "skipped_count": 0, "sent": [], "skipped": [], "_foreach_result": True}

    # Resuelve el resto de parámetros (los que NO son foreach) una sola vez
    other_params = {k: v for k, v in params.items() if k != foreach_key}
    other_resolved = resolve_params(other_params, results)

    sent = []
    skipped = []

    for item in items_list:
        name = item.get(name_field, "Contacto desconocido") if isinstance(item, dict) else str(item)
        value = item.get(extract_field) if isinstance(item, dict) else None

        if not value or not isinstance(value, str) or not value.strip():
            motivo = f"No tiene '{extract_field}' registrado"
            skipped.append({"nombre": name, "motivo": motivo})
            print(f"   ⏭️ Omitiendo {name}: {motivo}")
            continue

        iter_params = {**other_resolved, foreach_key: value}
        print(f"   → {name} ({value})")

        if debug_mode:
            iter_result = _simulate_tool(tool_name, iter_params, session_id)
        else:
            iter_result = execute_tool(tool_name, iter_params)

        sent.append({"nombre": name, extract_field: value, "resultado": iter_result})
        print(f"   ✓ {json.dumps(iter_result, default=str)[:100]}")

    return {
        "sent_count": len(sent),
        "skipped_count": len(skipped),
        "sent": sent,
        "skipped": skipped,
        "_foreach_result": True,
    }


def executor_node(state: AgentState) -> dict:
    print("\n" + "=" * 60)
    print("⚡ EXECUTOR")
    print("=" * 60)

    plan = state.get("plan", [])
    results = dict(state.get("results", {}))
    tools_used = list(state.get("tools_used", []))
    debug_mode = state.get("debug_mode", False)
    session_id = state.get("session_id") or ""
    simulated_calls = []

    if debug_mode:
        print("   🔍 MODO DEBUG — dry-run activo, no se modificará la base de datos")

    if not plan:
        print("   ⚠️ No hay plan que ejecutar")
        return {"status": "validating"}

    for step in plan:
        step_num = step.get("step", 0)
        tool_name = step.get("tool", "")
        params = step.get("params", {})

        print(f"\n   Paso {step_num}: {tool_name}")
        try:
            # ── Detecta foreach en cualquier param ─────────────────────────
            foreach_key = next(
                (k for k, v in params.items() if isinstance(v, dict) and "foreach" in v and "from_step" in v),
                None,
            )

            if foreach_key:
                # Ejecución en bucle: un tool call por cada item de la lista
                print(f"   [FOREACH] expandiendo por '{params[foreach_key]['foreach']}'")
                result = _execute_foreach_step(
                    tool_name, step_num, params,
                    foreach_key, params[foreach_key],
                    results, debug_mode,
                    session_id=session_id,
                )
                resolved_params = params  # para el log de simulated_calls
            else:
                resolved_params = resolve_params(params, results)
                print(f"   Params: {json.dumps(resolved_params, default=str)[:200]}")

                # ── Validación de parámetros antes de ejecutar ──────────────
                validation_error = validate_tool_params(tool_name, step_num, resolved_params)
                if validation_error:
                    print(f"   ⚠️ VALIDACIÓN FALLIDA: {validation_error['error'][:150]}")
                    result = validation_error
                elif debug_mode:
                    result = _simulate_tool(tool_name, resolved_params, session_id)
                    print(f"   [DRY RUN] Resultado simulado: {json.dumps(result, default=str)[:200]}")
                    # Guardar en cache in-memory para que listar_proyectos lo vea en turnos posteriores
                    if tool_name == "crear_proyecto" and result.get("success") and session_id:
                        project_id = result.get("projectId", f"proj-DRYRUN-{uuid.uuid4().hex[:8]}")
                        add_dry_run_project(session_id, {
                            "projectId": project_id,
                            "name":      resolved_params.get("name", "?"),
                            "type":      resolved_params.get("type", "Otro"),
                            "status":    "active",
                        })
                        # Guardar participants para que obtener_contactos_proyecto los devuelva
                        raw_participants = resolved_params.get("participants", [])
                        if raw_participants:
                            contactos = [
                                {
                                    "nombre":           p.get("nombre", "?"),
                                    "rol":              p.get("rol", ""),
                                    "telefono":         p.get("telefono", ""),
                                    "email":            p.get("email", ""),
                                    "tareas_pendientes": 0,
                                }
                                for p in raw_participants if isinstance(p, dict)
                            ]
                            add_dry_run_participants(session_id, project_id, contactos)
                            # Fallback "_latest" para recuperar aunque haya mismatch de project_id
                            add_dry_run_participants(session_id, "_latest", contactos)
                        print(f"   [DRY RUN] Proyecto guardado en cache: {resolved_params.get('name')}")
                else:
                    result = execute_tool(tool_name, resolved_params)

        except Exception as e:
            print(f"   ❌ Error inesperado en el paso: {e}")
            result = {"error": str(e)}
            resolved_params = params

        results[step_num] = result
        tools_used.append(tool_name)

        if debug_mode:
            simulated_calls.append({
                "step": step_num,
                "tool": tool_name,
                "params": resolved_params,
                "simulated_result": result,
            })

        preview = json.dumps(result, default=str, ensure_ascii=False)[:200]
        print(f"   Resultado: {preview}...")

    print(f"\n   ✅ Plan {'simulado' if debug_mode else 'ejecutado'}: {len(plan)} pasos")

    update = {"results": results, "tools_used": tools_used, "status": "validating"}
    if debug_mode and simulated_calls:
        existing = state.get("debug_info") or {}
        update["debug_info"] = {**existing, "simulated_calls": simulated_calls}
    return update
