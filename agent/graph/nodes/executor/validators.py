"""Validaciones de parámetros antes de ejecutar herramientas.

Cada validador devuelve None si todo está bien, o un dict con:
  {"_validation_error": True, "tool": str, "step": int, "error": str, "hint": str}

El "hint" está redactado en lenguaje que el LLM planner puede leer y actuar sobre él.
"""

# Campos que DEBEN ser strings escalares (nunca dicts ni listas)
_SCALAR_ID_FIELDS = {
    "project_id": "projectId",
    "email_id":   "email_id",
    "task_id":    "taskId",
}

# Valores que se consideran "vacíos / inventados"
_INVALID_SCALAR_VALUES = {
    "", "none", "null", "unknown", "<unknown>", "tbd", "pending",
    "user_selection", "undefined", "n/a", "sin_proyecto",
}


def _is_invalid_scalar(value) -> bool:
    """True si el valor es un dict, lista, o uno de los marcadores inválidos."""
    if isinstance(value, (dict, list)):
        return True
    if isinstance(value, str) and value.strip().lower() in _INVALID_SCALAR_VALUES:
        return True
    return False


def validate_tool_params(tool_name: str, step_num: int, params: dict) -> dict | None:
    """Valida los parámetros de una herramienta antes de ejecutarla.

    Retorna None si es válido, o un dict de error si no lo es.
    """
    errors = []

    # ── Validaciones de IDs escalares ──────────────────────────────────────
    for field, id_key in _SCALAR_ID_FIELDS.items():
        if field not in params:
            continue
        val = params[field]
        if _is_invalid_scalar(val):
            if isinstance(val, dict):
                hint = (
                    f"El parámetro '{field}' es un objeto JSON en vez de un string. "
                    f"Debes usar listar_proyectos primero y luego referenciar el ID con "
                    f'{{\"from_step\": N, \"extract\": \"{id_key}\"}} '
                    f"o {{\"from_step\": N, \"match\": {{\"key\": \"name\", \"value\": \"NOMBRE\"}}, "
                    f'\"extract\": \"{id_key}\"}}.'
                )
            else:
                hint = (
                    f"El parámetro '{field}' tiene un valor inválido: '{val}'. "
                    f"Debes obtener el ID real usando listar_proyectos antes de ejecutar {tool_name}."
                )
            errors.append(hint)

    # ── Validaciones específicas por herramienta ───────────────────────────

    if tool_name == "crear_proyecto":
        name = params.get("name", "")
        desc = params.get("description", "")
        ptype = params.get("type", "")
        if not name or not isinstance(name, str) or not name.strip():
            errors.append("crear_proyecto requiere 'name' (nombre del proyecto). Pregunta al usuario cómo se llama.")
        if not desc or not isinstance(desc, str) or len(desc.strip()) < 10:
            errors.append(
                "crear_proyecto requiere 'description' con al menos una descripción corta del proyecto. "
                "Pregunta al usuario de qué trata el proyecto antes de crearlo."
            )
        if not ptype or not isinstance(ptype, str):
            errors.append("crear_proyecto requiere 'type'. Infiere el tipo del mensaje o pregunta al usuario.")

    elif tool_name == "crear_tarea":
        text = params.get("text", "")
        if not text or not isinstance(text, str) or not text.strip():
            errors.append("crear_tarea requiere 'text' (descripción de la tarea). Pregunta al usuario qué tarea quiere crear.")

    elif tool_name == "enviar_notificacion":
        dest = params.get("destinatario", "")
        if _is_invalid_scalar(dest):
            errors.append(
                "enviar_notificacion requiere 'destinatario' como número de teléfono E.164 (ej: '+50494622817'). "
                "Usa obtener_contactos_proyecto para obtener los teléfonos del equipo antes de enviar."
            )
        msg = params.get("mensaje", "")
        if not msg or not isinstance(msg, str) or not msg.strip():
            errors.append("enviar_notificacion requiere 'mensaje'. Pregunta al usuario qué quiere enviar.")

    elif tool_name == "enviar_correo":
        dest_email = params.get("destinatario_email", "")
        if not dest_email or not isinstance(dest_email, str) or "@" not in dest_email:
            errors.append(
                "enviar_correo requiere 'destinatario_email' con un email válido. "
                "Nunca inventes un email; pídelo al usuario si no lo mencionó."
            )

    elif tool_name == "crear_recordatorio":
        titulo = params.get("titulo", "")
        fecha = params.get("fecha_vencimiento", "")
        if not titulo or not isinstance(titulo, str) or not titulo.strip():
            errors.append("crear_recordatorio requiere 'titulo'. Pregunta al usuario de qué es el recordatorio.")
        if not fecha or not isinstance(fecha, str) or not fecha.strip():
            errors.append("crear_recordatorio requiere 'fecha_vencimiento'. Pregunta al usuario para cuándo.")

    if not errors:
        return None

    return {
        "_validation_error": True,
        "tool": tool_name,
        "step": step_num,
        "error": "; ".join(errors),
        "hint": (
            f"CORRECCIÓN REQUERIDA en el paso {step_num} ({tool_name}): "
            + " | ".join(errors)
        ),
    }
