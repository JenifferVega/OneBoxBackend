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
import json


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


def _normalize(text) -> str:
    """Casefold, strip accents, collapse whitespace — for comparing names.

    "Sistema de Gestión de Inventarios" and "sistema de gestion de
    inventarios" are the same project to a person, and were two different
    strings to this function.
    """
    import re
    import unicodedata
    s = unicodedata.normalize("NFKD", str(text or ""))
    s = "".join(c for c in s if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", s).strip().lower()


def _match_and_extract(obj, key: str, value: str, extract: str):
    """In a list of objects, find the one where obj[key]==value and return obj[extract]."""
    if isinstance(obj, list):
        items = obj
    elif isinstance(obj, dict):
        # Try to find the first list in the dict (e.g. {"projects": [...], "count": N})
        items = next((v for v in obj.values() if isinstance(v, list)), [])
    else:
        return None

    # Three passes, each looser than the last, and each requiring the match to
    # be UNIQUE.
    #
    # Exact-only was too strict for how people talk: on 2026-09-23 a user asked
    # about "el proyecto del sistema de gestión de inventarios", the planner
    # matched on that phrase, no project was named exactly that, the reference
    # resolved to None and the turn burned an iteration before the planner
    # gave up and pasted a literal id.
    #
    # Loose is dangerous in the other direction, which is why ambiguity is
    # NOT resolved: if two records match, this returns None. Picking the first
    # of several is precisely the "wrong record" failure the write guards
    # exist to prevent, and a missing id costs a question while a wrong one
    # costs somebody's data.
    want = _normalize(value)
    candidates = [i for i in items if isinstance(i, dict)]

    for test in (
        lambda have: have == want,                       # same text
        lambda have: want and (want in have or have in want),  # one contains the other
    ):
        hits = [i for i in candidates if test(_normalize(i.get(key, "")))]
        if len(hits) == 1:
            return hits[0].get(extract)
        if len(hits) > 1:
            print(f"   [MATCH] '{value}' matches {len(hits)} records on '{key}' "
                  f"({', '.join(str(h.get(key)) for h in hits[:4])}). Ambiguous "
                  f"is not a match: returning nothing so the user is asked.")
            return None

    # No match. We used to fall back to items[0] here, which is how a task
    # could silently land in whatever project happened to be listed first.
    # A wrong id is far worse than a missing one: return None and let the
    # parameter validator report it.
    return None


def _as_chain_ref(value):
    """A chaining reference, whatever shape the provider wrote it in.

    The planner is supposed to write {"from_step": 1, "extract": "projectId"}
    as an OBJECT. Gemini, decoding under a schema that types the parameter as
    a string, writes the same thing SERIALISED:

        'project_id': '{"from_step":1,"extract":"projectId"}'

    Observed on 2026-09-18 with gemini-2.5-flash. Left alone it is worse than
    an empty parameter: the reference is never resolved and the literal text
    is passed to the tool as if it were an id, so the failure is silent and
    the tool looks up something that cannot exist.

    So: accept both. A provider that cannot express an object in a string
    field should not be able to break chaining.
    """
    if isinstance(value, dict):
        return value if "from_step" in value else None
    if isinstance(value, str):
        text = value.strip()
        if not (text.startswith("{") and "from_step" in text):
            return None
        try:
            parsed = json.loads(text)
        except (ValueError, TypeError):
            return None
        return parsed if isinstance(parsed, dict) and "from_step" in parsed else None
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
        ref = _as_chain_ref(value)
        if ref is not None:
            value = ref                      # normalised: the rest is unchanged
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
