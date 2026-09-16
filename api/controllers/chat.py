"""Conversational agent endpoints and health check."""
from fastapi import APIRouter, Header, HTTPException

from agent.graph import run_agent
from agent.graph.nodes.executor.node import clear_dry_run_cache
from agent.graph.runner import clear_last_results
from agent.tools import clear_current_user, set_current_user
from api.deps import require_uid
from api.schemas import ChatRequest, ChatResponse

router = APIRouter()


@router.post("/chat", response_model=ChatResponse)
async def chat(
    request: ChatRequest,
    x_user_id: str = Header(default=""),
    x_user_email: str = Header(default=""),
):
    """Main agent endpoint.

    SECURITY: requires x-user-id (validated by require_uid → 401 if missing).
    Sets the user context so tools filter by the requesting uid — NOT by a
    hardcoded global USER_ID (cross-tenant leak).
    """
    uid = require_uid(x_user_id)
    user_email = x_user_email.lower() if x_user_email else ""
    set_current_user(uid, user_email)
    try:
        result = run_agent(
            request.message,
            request.history,
            debug_mode=request.debug,
            session_id=request.session_id or "",
        )
        return ChatResponse(
            response=result["response"],
            toolsUsed=result.get("tools_used", []),
            debug_info=result.get("debug_info") if request.debug else None,
        )
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        # Clear the context so the uid does not persist across requests
        # (defense in depth — contextvars already isolates per task, but still).
        clear_current_user()


@router.delete("/debug/cache/{session_id}")
async def clear_session_cache(session_id: str):
    """Clear ALL per-session state. Called by onebox_reset.

    Both caches, not just the dry-run one. The previous-turn results were left
    behind, so a "reset" session still had the last turn's tool output injected
    into the planner prompt — debug runs did not start clean, and a stale
    fixture kept influencing conversations after it had been replaced.
    """
    clear_dry_run_cache(session_id)
    clear_last_results(session_id)
    return {"cleared": True, "session_id": session_id}


@router.get("/health")
async def health():
    return {"status": "ok", "agent": "OneBox Agent v1.0"}
