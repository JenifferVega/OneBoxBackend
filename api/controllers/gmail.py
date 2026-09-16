"""Gmail endpoints: OAuth, status, disconnect, push notifications and watch."""
from fastapi import APIRouter, Header, Query, Request
from fastapi.responses import RedirectResponse

from api.deps import require_uid
from api.services import gmail as gmail_service

router = APIRouter()


@router.get("/api/gmail/auth")
async def gmail_auth(x_user_id: str = Header(default="")):
    """Generate a Google OAuth authorization URL to connect Gmail."""
    uid = require_uid(x_user_id)
    return gmail_service.build_auth_url(uid)


@router.get("/api/gmail/callback")
async def gmail_callback(code: str = Query(...), state: str = Query("")):
    """Google OAuth callback. Exchanges the code for tokens and saves them."""
    uid = require_uid(state)
    try:
        gmail_service.exchange_oauth_code(uid, code)
        return RedirectResponse(url="https://www.oneboxmanager.com/?gmail=connected")
    except Exception as e:
        print(f"[Gmail OAuth] Error: {e}")
        import traceback; traceback.print_exc()
        return RedirectResponse(url=f"https://www.oneboxmanager.com/?gmail=error&detail={str(e)[:100]}")


@router.get("/api/gmail/status")
async def gmail_status(x_user_id: str = Header(default="")):
    """Check whether the user has Gmail connected."""
    uid = require_uid(x_user_id)
    return gmail_service.get_status(uid)


@router.delete("/api/gmail/disconnect")
async def gmail_disconnect(x_user_id: str = Header(default="")):
    """Disconnect the user's Gmail."""
    uid = require_uid(x_user_id)
    return gmail_service.disconnect(uid)


@router.post("/api/gmail/push-notification")
async def gmail_push_notification(request: Request):
    """
    Webhook receiving Google Pub/Sub notifications when a new email arrives.
    Triggers Gmail sync automatically.
    """
    try:
        body = await request.json()
        return gmail_service.handle_push_notification(body)
    except Exception as e:
        print(f"[Gmail Push] Error: {e}")
        return {"status": "ok"}


@router.post("/api/gmail/register-watch")
async def gmail_register_watch(x_user_id: str = Header(default="")):
    """Register the Gmail watch to receive push notifications via Pub/Sub."""
    uid = require_uid(x_user_id)
    return gmail_service.register_watch(uid)
