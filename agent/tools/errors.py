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
    """Extracts a readable message from an exception (HTTPException.detail or str)."""
    detail = getattr(e, "detail", None)
    return str(detail) if detail is not None else str(e)
