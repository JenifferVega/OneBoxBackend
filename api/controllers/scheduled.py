"""Endpoints programados (EventBridge cron): sync de Gmail y notificaciones SLA."""
from fastapi import APIRouter, Header, HTTPException

from api.deps import require_uid
from api.services import gmail as gmail_service
from api.services import notifications as notifications_service

router = APIRouter()


@router.post("/api/scheduled/gmail-sync")
async def scheduled_gmail_sync(x_user_id: str = Header(default="")):
    """
    Sincroniza Gmail, trae correos nuevos y los analiza con IA.
    Crea proyectos, tareas e insights automáticamente.
    Usa el refresh token del usuario almacenado en DynamoDB.
    """
    uid = require_uid(x_user_id)
    try:
        return gmail_service.sync_gmail(uid)
    except Exception as e:
        print(f"[Gmail Sync] Error: {e}")
        import traceback; traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/api/scheduled/notifications")
async def scheduled_notifications():
    """
    Endpoint para notificaciones automáticas.
    Revisa SLA (tareas bloqueadas/vencidas) y envía WhatsApp a los responsables.
    Diseñado para ser invocado por EventBridge cron cada mañana.
    """
    try:
        return notifications_service.send_scheduled_notifications()
    except Exception as e:
        print(f"[Scheduled] Error en notificaciones: {e}")
        import traceback; traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))
