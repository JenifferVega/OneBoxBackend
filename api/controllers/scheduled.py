"""Scheduled endpoints (EventBridge cron): Gmail sync and SLA notifications."""
import hmac
import os

from fastapi import APIRouter, Header, HTTPException

from api.deps import require_uid
from api.services import gmail as gmail_service
from api.services import notifications as notifications_service

router = APIRouter()


def _check_cron_secret(secret: str) -> None:
    """These endpoints send WhatsApp and email to every participant: anyone
    who can reach the URL could trigger a mass send. EventBridge (API
    destination, connection of type API key) sends the header x-cron-secret.
    Without CRON_SECRET configured the check is skipped with a warning, so an
    environment that has not set it yet keeps working."""
    expected = os.environ.get("CRON_SECRET", "")
    if not expected:
        print("[Scheduled] WARNING: CRON_SECRET is not set; scheduled endpoints are unprotected")
        return
    if not hmac.compare_digest(secret or "", expected):
        raise HTTPException(status_code=401, detail="Invalid cron secret")


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
async def scheduled_notifications(x_cron_secret: str = Header(default="")):
    """
    Endpoint for automatic notifications.
    Reviews SLA (blocked/overdue tasks) and sends WhatsApp to those responsible.
    Designed to be invoked by an EventBridge cron every morning.
    """
    _check_cron_secret(x_cron_secret)
    try:
        return notifications_service.send_scheduled_notifications()
    except Exception as e:
        print(f"[Scheduled] Error in notifications: {e}")
        import traceback; traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/api/scheduled/dispatch-pending")
def dispatch_pending_notifications(force: bool = False, only: str = "", x_cron_secret: str = Header(default="")):
    """
    Dispatcher for notifications scheduled by the user from the chat.
    Sends notifications with status='pending' whose scheduledAt has passed,
    and recurring notifications if today is one of their configured days.
    Designed to be invoked by EventBridge every hour.

    ?force=true (testing, scripts/sim/update_flow.py): the hour-of-day gates of
    the update emails and the daily digest are skipped, and the project owner
    also gets the 'hora de actualizar' email. Everything else is unchanged:
    nobody gets the same email twice in a period.
    ?only=<email> (testing): ONLY that person is considered -- their update
    email, the owner alert about them, the digest of their projects; the
    pending user notifications are not dispatched. Nobody else gets anything.
    (A plain `def`: FastAPI runs it in a thread, the LLM calls do not block.)
    """
    _check_cron_secret(x_cron_secret)
    try:
        only = (only or "").strip().lower()
        result = {"only": only} if only else notifications_service.dispatch_pending_notifications()
        # Same hourly tick: tell owners who has gone quiet on their pending work.
        try:
            from api.services import checkin
            result = {**(result if isinstance(result, dict) else {"dispatch": result}),
                      "checkin_overdue": checkin.notify_overdue(only=only)}
        except Exception as e:
            print(f"[Scheduled] check-in overdue failed: {e}")
        # 'Hora de actualizar': prepared questions by email to whoever is due.
        try:
            from api.services import update_reminders
            result = {**result, "update_reminders": update_reminders.send_due(force=force, only=only)}
        except Exception as e:
            print(f"[Scheduled] update reminders failed: {e}")
        # ...and once a day the members' updates become a digest + proposal.
        try:
            from api.services import updates_consolidation
            result = {**result, "updates_digest": updates_consolidation.consolidate_due(force=force, only=only)}
        except Exception as e:
            print(f"[Scheduled] updates digest failed: {e}")
        return result
    except Exception as e:
        print(f"[Scheduled] Error in dispatch-pending: {e}")
        import traceback; traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))
