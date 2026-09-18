"""Shared error translation for the api.services wrappers.

Moved verbatim out of the old single-file agent/tools.py.
"""



# ============================================================================
# PROJECT AND TASK MUTATIONS (update / delete / invite)
# ----------------------------------------------------------------------------
# These tools are thin wrappers over the api.services.* layer, so the agent
# respects EXACTLY the same RBAC and the same side effects (block/assign
# notifications, subtask deletion, invitation revocation) as the REST API.
# The tools NEVER raise: any HTTPException/Exception is converted into
# {"error": ...} so the executor/narrator can handle it gracefully. The
# api.services import is LAZY (inside each function) to avoid an import
# cycle with api.services.*, which in turn imports from agent.tools. In
# debug mode (dry-run) these functions are NOT invoked: the executor
# simulates the result, so neither DynamoDB nor Twilio are touched.
# ============================================================================

def _svc_error(e) -> str:
    """Turns an exception into a message for the agent AND records it.

    Every one of the tools ends the same way:

        except Exception as e:
            return {"error": _svc_error(e)}

    which is right for the conversation -- the agent gets something it can
    explain instead of a crash -- and was wrong for everything else: the user
    saw a message, and the server kept no trace of who it happened to, in
    which tool, or why. "The Trello integration does not work for this user"
    could not be answered from the logs because there was nothing in them.

    Logging HERE covers all of the tools at once, because all of them funnel
    through this one function. The tool name is taken from the calling frame,
    so no call site has to pass it.
    """
    import inspect

    detail = getattr(e, "detail", None)
    message = str(detail) if detail is not None else str(e)

    try:
        from agent import obs
        frame = inspect.currentframe()
        tool = frame.f_back.f_code.co_name if frame and frame.f_back else "?"
        status = getattr(e, "status_code", None)
        fields = {"tool": tool}
        if status:
            fields["status_code"] = status
        obs.error("tool_failed", exc=e, **fields)
    except Exception:
        # Logging must never be the reason a tool fails differently.
        pass

    return message
