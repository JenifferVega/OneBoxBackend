"""Linked phones endpoints (WhatsApp ↔ user)."""
from fastapi import APIRouter, Header, HTTPException

from api.deps import require_uid
from api.schemas import LinkPhoneRequest
from api.services import phones as phones_service

router = APIRouter()


@router.post("/api/user/phone")
async def link_phone(req: LinkPhoneRequest, x_user_id: str = Header(default=""), x_user_email: str = Header(default=""), x_user_name: str = Header(default="")):
    """Link a WhatsApp number to the authenticated user."""
    uid = require_uid(x_user_id)
    try:
        return phones_service.link_phone(uid, req.phoneNumber, x_user_email, x_user_name)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/api/user/phone")
async def get_user_phone(x_user_id: str = Header(default="")):
    """Get the user's linked phone."""
    uid = require_uid(x_user_id)
    try:
        return phones_service.get_user_phone(uid)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/api/user/phones")
async def get_user_phones(x_user_id: str = Header(default="")):
    """Get all of the user's linked WhatsApp phones."""
    uid = require_uid(x_user_id)
    try:
        return phones_service.get_user_phones(uid)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.delete("/api/user/phone")
async def unlink_phone(x_user_id: str = Header(default="")):
    """Unlink the user's phone."""
    uid = require_uid(x_user_id)
    try:
        return phones_service.unlink_phone(uid)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
