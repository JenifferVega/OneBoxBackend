"""System details (Settings > System details): which model each node uses.
See api/services/system_info.py. Never returns secrets."""
from fastapi import APIRouter, Header

from api.deps import require_uid
from api.services import model_check, system_info

router = APIRouter()


@router.get("/api/system/info")
async def get_system_info(x_user_id: str = Header(default=""), x_user_email: str = Header(default="")):
    require_uid(x_user_id)
    return system_info.info((x_user_email or "").lower())


@router.post("/api/system/check-models")
def check_models(x_user_id: str = Header(default=""), x_user_email: str = Header(default="")):
    """One tiny real call per model (api/services/model_check.py). Sync: runs in the threadpool."""
    require_uid(x_user_id)
    return model_check.run((x_user_email or "").lower())
