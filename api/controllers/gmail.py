"""Endpoints de Gmail: OAuth, estado, desconexión, push notifications y watch."""
from fastapi import APIRouter, Header, Query, Request
from fastapi.responses import RedirectResponse

from api.deps import require_uid
from api.services import gmail as gmail_service

router = APIRouter()


@router.get("/api/gmail/auth")
async def gmail_auth(x_user_id: str = Header(default="")):
    """Genera URL de autorización de Google OAuth para conectar Gmail."""
    uid = require_uid(x_user_id)
    return gmail_service.build_auth_url(uid)


@router.get("/api/gmail/callback")
async def gmail_callback(code: str = Query(...), state: str = Query("")):
    """Callback de Google OAuth. Intercambia code por tokens y guarda."""
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
    """Verifica si el usuario tiene Gmail conectado."""
    uid = require_uid(x_user_id)
    return gmail_service.get_status(uid)


@router.delete("/api/gmail/disconnect")
async def gmail_disconnect(x_user_id: str = Header(default="")):
    """Desconecta Gmail del usuario."""
    uid = require_uid(x_user_id)
    return gmail_service.disconnect(uid)


@router.post("/api/gmail/push-notification")
async def gmail_push_notification(request: Request):
    """
    Webhook que recibe notificaciones de Google Pub/Sub cuando llega un correo nuevo.
    Dispara el sync de Gmail automáticamente.
    """
    try:
        body = await request.json()
        return gmail_service.handle_push_notification(body)
    except Exception as e:
        print(f"[Gmail Push] Error: {e}")
        return {"status": "ok"}


@router.post("/api/gmail/register-watch")
async def gmail_register_watch(x_user_id: str = Header(default="")):
    """Registra el watch de Gmail para recibir notificaciones push via Pub/Sub."""
    uid = require_uid(x_user_id)
    return gmail_service.register_watch(uid)
