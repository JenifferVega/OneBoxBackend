"""Scheduled endpoints (EventBridge cron): Gmail sync and SLA notifications."""
from fastapi import APIRouter, Header, HTTPException

from api.deps import require_uid
from api.services import gmail as gmail_service
from api.services import notifications as notifications_service

router = APIRouter()


@router.post("/api/scheduled/gmail-sync")
async def scheduled_gmail_sync(x_user_id: str = Header(default="")):
    """
    Sync Gmail, fetch updated emails and analyze them with AI.
    Creates projects, tasks and insights automatically.
    Uses the user's refresh token stored in DynamoDB.
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
    Endpoint for automatic notifications.
    Reviews SLA (blocked/overdue tasks) and sends WhatsApp to those responsible.
    Designed to be invoked by an EventBridge cron every morning.
    """
    try:
        return notifications_service.send_scheduled_notifications()
    except Exception as e:
        print(f"[Scheduled] Error in notifications: {e}")
        import traceback; traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/api/scheduled/dispatch-pending")
async def dispatch_pending_notifications():
    """
    Dispatcher for notifications scheduled by the user from the chat.
    Sends notifications with status='pending' whose scheduledAt has passed,
    and recurring notifications if today is one of their configured days.
    Designed to be invoked by EventBridge every hour.
    """
    try:
        return notifications_service.dispatch_pending_notifications()
    except Exception as e:
        print(f"[Scheduled] Error in dispatch-pending: {e}")
        import traceback; traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))
