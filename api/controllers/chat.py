"""Endpoints del agente conversacional y health check."""
from fastapi import APIRouter, Header, HTTPException

from agent.graph import run_agent
from agent.graph.nodes.executor.node import clear_dry_run_cache
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
    """Endpoint principal del agente.

    SEGURIDAD: exige x-user-id (validado por require_uid → 401 si falta).
    Setea el contexto de usuario para que las tools filtren por el uid
    del que pregunta — NO por un USER_ID global hardcoded (cross-tenant leak).
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
        # Limpiar contexto para no dejar el uid pegado entre requests
        # (defensa en profundidad — contextvars ya aísla por task, pero igual).
        clear_current_user()


@router.delete("/debug/cache/{session_id}")
async def clear_session_cache(session_id: str):
    """Limpia el cache dry-run de una sesión MCP. Llamado por onebox_reset."""
    clear_dry_run_cache(session_id)
    return {"cleared": True, "session_id": session_id}


@router.get("/health")
async def health():
    return {"status": "ok", "agent": "OneBox Agent v1.0"}
