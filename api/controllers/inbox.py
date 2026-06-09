"""Endpoints del inbox, conversaciones por proyecto y notificaciones."""
from typing import Optional

from fastapi import APIRouter, Header, HTTPException, Query

from api.deps import require_uid
from api.schemas import AssignRequest
from api.services import inbox as inbox_service

router = APIRouter()


@router.get("/api/inbox")
async def get_inbox(x_user_id: str = Header(default="")):
    """Lista conversaciones sin asignar del inbox."""
    uid = require_uid(x_user_id)
    try:
        return inbox_service.get_inbox(uid)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/api/inbox/{conversation_id}/assign")
async def assign_to_project(conversation_id: str, req: AssignRequest):
    """Asigna una conversación del inbox a un proyecto."""
    try:
        return inbox_service.assign_conversation(conversation_id, req.projectId)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/api/projects/{project_id}/conversations")
async def get_conversations(project_id: str, x_user_id: str = Header(default=""), x_user_email: str = Header(default="")):
    """Lista conversaciones de un proyecto. Owner Y invitados con acceso."""
    uid = require_uid(x_user_id)
    try:
        return inbox_service.get_project_conversations(uid, x_user_email, project_id)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/api/notifications")
async def get_notifications(projectId: Optional[str] = Query(None), x_user_id: str = Header(default="")):
    """Lista notificaciones enviadas."""
    uid = require_uid(x_user_id)
    try:
        return inbox_service.list_notifications(uid, projectId)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
