"""Resolución de referencias from_step en parámetros (portado del agente anterior).

Formatos soportados:
  {"from_step": N}
      → pasa el resultado completo del paso N.
  {"from_step": N, "path": "campo.subcampo"}
      → extrae un valor específico usando dot-notation.
      Ejemplos:
        "path": "projectId"              → resultado["projectId"]
        "path": "projects.0.projectId"  → resultado["projects"][0]["projectId"]
        "path": "contactos.0.telefono"  → resultado["contactos"][0]["telefono"]
  {"from_step": N, "match": {"key": "name", "value": "Alpha"}, "extract": "projectId"}
      → en una lista de objetos, encuentra el que tenga key==value y extrae el campo.
      Útil para listar_proyectos cuando se sabe el nombre del proyecto de antemano.
"""


def _get_path(obj, path: str):
    """Extrae un valor de obj usando dot-notation. Soporta índices numéricos."""
    parts = path.split(".")
    current = obj
    for part in parts:
        if current is None:
            return None
        if isinstance(current, list):
            try:
                current = current[int(part)]
            except (ValueError, IndexError):
                return None
        elif isinstance(current, dict):
            current = current.get(part)
        else:
            return None
    return current


def _match_and_extract(obj, key: str, value: str, extract: str):
    """En una lista de objetos, encuentra el que tenga obj[key]==value y devuelve obj[extract]."""
    if isinstance(obj, list):
        items = obj
    elif isinstance(obj, dict):
        # Intenta encontrar la primera lista en el dict (ej: {"projects": [...], "count": N})
        items = next((v for v in obj.values() if isinstance(v, list)), [])
    else:
        return None

    for item in items:
        if isinstance(item, dict) and str(item.get(key, "")).lower() == str(value).lower():
            return item.get(extract)
    # Si no encontró match exacto, devuelve el primero disponible
    if items and isinstance(items[0], dict):
        return items[0].get(extract)
    return None


def resolve_params(params: dict, results: dict) -> dict:
    """Resuelve referencias from_step en los parámetros."""
    if not params:
        return {}

    if "from_step" in params and len(params) == 1:
        step_ref = params["from_step"]
        return results.get(step_ref, {})

    resolved = {}
    for key, value in params.items():
        if isinstance(value, dict) and "from_step" in value:
            step_ref = value["from_step"]
            step_result = results.get(step_ref)

            if step_result is None:
                resolved[key] = value
                continue

            # Caso 1: extracción por match (busca por nombre/campo y extrae otro campo)
            if "match" in value and "extract" in value:
                match_cfg = value["match"]
                extracted = _match_and_extract(
                    step_result,
                    match_cfg.get("key", ""),
                    match_cfg.get("value", ""),
                    value["extract"],
                )
                resolved[key] = extracted if extracted is not None else step_result

            # Caso 2: extracción por path (dot-notation)
            elif "path" in value:
                extracted = _get_path(step_result, value["path"])
                resolved[key] = extracted if extracted is not None else step_result

            # Caso 3: extract directo — saca un campo del resultado sin match
            # Ej: {"from_step": 1, "extract": "projectId"} → step_result["projectId"]
            elif "extract" in value:
                field = value["extract"]
                if isinstance(step_result, dict):
                    extracted = step_result.get(field)
                    resolved[key] = extracted if extracted is not None else step_result
                else:
                    resolved[key] = step_result

            # Caso 4: resultado completo
            else:
                resolved[key] = step_result

        else:
            resolved[key] = value

    # Post-proceso: auto-extraer IDs conocidos cuando el LLM olvidó usar extract/path.
    # Si project_id / email_id / task_id siguen siendo dicts con el campo ID conocido,
    # extraer el valor escalar automáticamente para no pasarle un dict al tool real.
    _AUTO_EXTRACT = {
        "project_id":  "projectId",
        "email_id":    "email_id",
        "task_id":     "taskId",
        "reminder_id": "reminder_id",
    }
    for field, id_key in _AUTO_EXTRACT.items():
        if field in resolved and isinstance(resolved[field], dict):
            val = resolved[field].get(id_key)
            if val is not None:
                resolved[field] = val

    return resolved
