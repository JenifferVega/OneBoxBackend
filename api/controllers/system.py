"""System details (Settings > System details): which model each node uses.
See api/services/system_info.py. Never returns secrets."""
from fastapi import APIRouter, Header

from api.deps import require_uid
from api.services import system_info

router = APIRouter()


@router.get("/api/system/info")
async def get_system_info(x_user_id: str = Header(default=""), x_user_email: str = Header(default="")):
    require_uid(x_user_id)
    return system_info.info((x_user_email or "").lower())
