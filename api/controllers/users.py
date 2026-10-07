"""User profile and preferences endpoints (timezone, language)."""
from fastapi import APIRouter, Header, HTTPException

from api.deps import require_uid
from api.schemas import UpdateUserSettingsRequest
from api.services import contacts as contacts_service
from api.services import users as users_service

router = APIRouter()


@router.get("/api/user/settings")
async def get_user_settings(x_user_id: str = Header(default="")):
    """The user's saved preferences. Unsaved fields come back as null, with
    the defaults alongside."""
    uid = require_uid(x_user_id)
    return users_service.get_settings(uid)


@router.put("/api/user/settings")
async def update_user_settings(req: UpdateUserSettingsRequest,
                               x_user_id: str = Header(default="")):
    """Partial update: only the fields sent are changed."""
    uid = require_uid(x_user_id)
    try:
        return users_service.update_settings(uid, req.timezone, req.language)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e))


@router.get("/api/user/contacts")
async def get_user_contacts(x_user_id: str = Header(default=""),
                            x_user_email: str = Header(default="")):
    """People from the user's projects, deduplicated, for autocomplete."""
    uid = require_uid(x_user_id)
    try:
        return contacts_service.list_contacts(uid, (x_user_email or "").lower())
    except Exception as e:
        print(f"[contacts] error: {e}")
        raise HTTPException(status_code=500, detail=str(e))
