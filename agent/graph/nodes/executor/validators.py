"""Parameter validation before executing tools.

Each validator returns None if everything is fine, or a dict with:
  {"_validation_error": True, "tool": str, "step": int, "error": str, "hint": str}

The "hint" is written in language the planner LLM can read and act on.
"""

# Fields that MUST be scalar strings (never dicts or lists)
_SCALAR_ID_FIELDS = {
    "project_id": "projectId",
    "email_id":   "email_id",
    "task_id":    "taskId",
}

# Values considered "empty / invented"
_INVALID_SCALAR_VALUES = {
    "", "none", "null", "unknown", "<unknown>", "tbd", "pending",
    "user_selection", "undefined", "n/a", "sin_proyecto",
}


def _is_invalid_scalar(value) -> bool:
    """True if the value is None, a dict, a list, or one of the invalid markers.

    None matters: a from_step extraction that finds nothing now resolves to
    None instead of silently substituting the whole step result. If None were
    treated as valid it would sail past this check and reach the tool, which
    is exactly the silent failure the change was meant to remove.
    """
    if value is None:
        return True
    if isinstance(value, (dict, list)):
        return True
    if isinstance(value, str) and value.strip().lower() in _INVALID_SCALAR_VALUES:
        return True
    return False


def validate_tool_params(tool_name: str, step_num: int, params: dict) -> dict | None:
    """Validate a tool's parameters before executing it.

    Returns None if valid, or an error dict if not.
    """
    errors = []

    # ── Scalar ID validations ──────────────────────────────────────
    for field, id_key in _SCALAR_ID_FIELDS.items():
        if field not in params:
            continue
        val = params[field]
        if _is_invalid_scalar(val):
            if val is None:
                hint = (
                    f"Parameter '{field}' came back empty: the from_step reference "
                    f"pointed at a field that was NOT in that step's result. Check "
                    f"which field you asked for. resolve_entity exposes '{id_key}' "
                    f"at the top level only when it returns exactly ONE candidate; "
                    f"with several candidates you must pick one and pass its "
                    f"'{id_key}' literally, or ask the user which one they meant."
                )
            elif isinstance(val, dict):
                hint = (
                    f"Parameter '{field}' is a JSON object instead of a string. "
                    f"Reference the ID itself with "
                    f'{{\"from_step\": N, \"extract\": \"{id_key}\"}} '
                    f"or {{\"from_step\": N, \"match\": {{\"key\": \"name\", \"value\": \"NAME\"}}, "
                    f'\"extract\": \"{id_key}\"}}. '
                    f"Get the ID from resolve_entity (when the user named the project) "
                    f"or from list_projects (when they did not)."
                )
            else:
                hint = (
                    f"Parameter '{field}' has an invalid value: '{val}'. "
                    f"Obtain the real ID with resolve_entity or list_projects "
                    f"before running {tool_name}."
                )
            errors.append(hint)

    # ── Tool-specific validations ───────────────────────────

    if tool_name == "create_project":
        name = params.get("name", "")
        desc = params.get("description", "")
        ptype = params.get("type", "")
        if not name or not isinstance(name, str) or not name.strip():
            errors.append("create_project requires 'name' (project name). Ask the user what to call it.")
        if not desc or not isinstance(desc, str) or len(desc.strip()) < 10:
            errors.append(
                "create_project requires 'description' with at least a short project description. "
                "Ask the user what the project is about before creating it."
            )
        if not ptype or not isinstance(ptype, str):
            errors.append("create_project requires 'type'. Infer the type from the message or ask the user.")

    elif tool_name == "create_task":
        text = params.get("text", "")
        if not text or not isinstance(text, str) or not text.strip():
            errors.append("create_task requires 'text' (task description). Ask the user what task they want to create.")

    elif tool_name == "send_notification":
        dest = params.get("recipient", "")
        if _is_invalid_scalar(dest):
            errors.append(
                "send_notification requires 'recipient' as an E.164 phone number (e.g. '+50494622817'). "
                "Use get_project_contacts to get the team's phone numbers before sending."
            )
        msg = params.get("message", "")
        if not msg or not isinstance(msg, str) or not msg.strip():
            errors.append("send_notification requires 'message'. Ask the user what they want to send.")

    elif tool_name == "send_email":
        dest_email = params.get("recipient_email", "")
        if not dest_email or not isinstance(dest_email, str) or "@" not in dest_email:
            errors.append(
                "send_email requires 'recipient_email' with a valid email. "
                "Never invent an email; ask the user if they didn't mention it."
            )

    elif tool_name in ("update_task", "delete_task"):
        task_id = params.get("task_id", "")
        if _is_invalid_scalar(task_id) or not str(task_id).strip():
            errors.append(
                f"{tool_name} requires a real 'task_id'. Use list_tasks (after list_projects) "
                f'and reference the ID with {{"from_step": N, "extract": "taskId"}} or with '
                f'{{"from_step": N, "match": {{"key": "text", "value": "TASK TEXT"}}, "extract": "taskId"}}. '
                f"Never invent a task_id."
            )
        if tool_name == "update_task":
            fields = ("text", "status", "assigned_to", "due_date", "start_date", "description", "blocked_reason")
            if not any(params.get(c) not in (None, "") for c in fields):
                errors.append("update_task needs at least one field to change (e.g. status='pending' to unblock).")

    elif tool_name == "update_project":
        fields = ("name", "description", "type", "status", "delivery_date", "timing")
        if not any(params.get(c) not in (None, "") for c in fields):
            errors.append("update_project needs at least one field to change (name, description, type, status...).")

    elif tool_name == "invite_user":
        email = params.get("email", "")
        phone = params.get("phone", "")
        has_email = isinstance(email, str) and "@" in email
        has_phone = isinstance(phone, str) and phone.strip() != ""
        if not has_email and not has_phone:
            errors.append(
                "invite_user requires 'email' (valid) or 'phone' (E.164). "
                "Never invent contact data; ask the user if they didn't provide it."
            )

    elif tool_name == "remove_participant":
        if not any(str(params.get(k, "")).strip() for k in ("email", "phone", "name")):
            errors.append("remove_participant requires email, phone or name of the person to remove.")

    elif tool_name == "update_participants":
        parts = params.get("participants")
        if not isinstance(parts, list):
            errors.append("update_participants requires 'participants' as a list of {name, email, role, phone}.")

    elif tool_name == "create_reminder":
        title = params.get("title", "")
        date = params.get("due_date", "")
        if not title or not isinstance(title, str) or not title.strip():
            errors.append("create_reminder requires 'title'. Ask the user what the reminder is about.")
        if not date or not isinstance(date, str) or not date.strip():
            errors.append("create_reminder requires 'due_date'. Ask the user for when.")

    if not errors:
        return None

    return {
        "_validation_error": True,
        "tool": tool_name,
        "step": step_num,
        "error": "; ".join(errors),
        "hint": (
            f"CORRECTION REQUIRED at step {step_num} ({tool_name}): "
            + " | ".join(errors)
        ),
    }
