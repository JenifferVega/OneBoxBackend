"""What the logged user owes an update on. The update itself is a chat:
api/controllers/updates.py (the old per-task form was removed)."""
from fastapi import APIRouter, Header

from api.deps import require_uid
from api.services import checkin

router = APIRouter()


@router.get("/api/checkin/pending")
async def checkin_pending(name: str = "", x_user_id: str = Header(default=""), x_user_email: str = Header(default="")):
    """`name` (query) only matters for participants stored without email."""
    uid = require_uid(x_user_id)
    return checkin.pending_for_user(uid, (x_user_email or "").lower(), name)
