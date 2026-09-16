"""Resolution of from_step references in parameters (ported from the previous agent).

Supported formats:
  {"from_step": N}
      → pass the full result of step N.
  {"from_step": N, "path": "field.subfield"}
      → extract a specific value using dot-notation.
      Examples:
        "path": "projectId"              → result["projectId"]
        "path": "projects.0.projectId"  → result["projects"][0]["projectId"]
        "path": "contacts.0.phone"  → result["contacts"][0]["phone"]
  {"from_step": N, "match": {"key": "name", "value": "Alpha"}, "extract": "projectId"}
      → in a list of objects, find the one where key==value and extract the field.
      Useful for list_projects when the project name is known in advance.

FAILED EXTRACTION RESOLVES TO None, NEVER TO THE WHOLE RESULT.

Substituting the full step result when a field was missing used to look
forgiving, but it fed a JSON object into parameters that expect a string. The
tool then failed with "project_id is a JSON object", which says nothing about
the real problem -- that the field the plan asked for was not in the result.
None reaches the parameter validator, which names the field and the step.
"""


def _get_path(obj, path: str):
    """Extract a value from obj using dot-notation. Supports numeric indices."""
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
    """In a list of objects, find the one where obj[key]==value and return obj[extract]."""
    if isinstance(obj, list):
        items = obj
    elif isinstance(obj, dict):
        # Try to find the first list in the dict (e.g. {"projects": [...], "count": N})
        items = next((v for v in obj.values() if isinstance(v, list)), [])
    else:
        return None

    for item in items:
        if isinstance(item, dict) and str(item.get(key, "")).lower() == str(value).lower():
            return item.get(extract)
    # No match. We used to fall back to items[0] here, which is how a task
    # could silently land in whatever project happened to be listed first.
    # A wrong id is far worse than a missing one: return None and let the
    # parameter validator report it.
    return None


def resolve_params(params: dict, results: dict) -> dict:
    """Resolve from_step references in the parameters."""
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

            # Case 1: extract by match (find by name/field and extract another field)
            if "match" in value and "extract" in value:
                match_cfg = value["match"]
                extracted = _match_and_extract(
                    step_result,
                    match_cfg.get("key", ""),
                    match_cfg.get("value", ""),
                    value["extract"],
                )
                resolved[key] = extracted

            # Case 2: extract by path (dot-notation)
            elif "path" in value:
                extracted = _get_path(step_result, value["path"])
                resolved[key] = extracted

            # Case 3: direct extract — pull a field from the result without a match
            # E.g. {"from_step": 1, "extract": "projectId"} → step_result["projectId"]
            elif "extract" in value:
                field = value["extract"]
                if isinstance(step_result, dict):
                    resolved[key] = step_result.get(field)
                else:
                    resolved[key] = step_result

            # Case 4: full result
            else:
                resolved[key] = step_result

        else:
            resolved[key] = value

    # Post-processing: auto-extract known IDs when the LLM forgot to use extract/path.
    # If project_id / email_id / task_id are still dicts with the known ID field,
    # extract the scalar value automatically so we don't pass a dict to the actual tool.
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
