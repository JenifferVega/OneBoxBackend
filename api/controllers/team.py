"""Members page (owner): follow-up status per person and their recent updates.
See api/services/team.py."""
from fastapi import APIRouter, Header

from api.deps import require_uid
from api.services import team

router = APIRouter()


@router.get("/api/team/status")
def team_status(x_user_id: str = Header(default=""), x_user_email: str = Header(default="")):
    uid = require_uid(x_user_id)
    return team.status(uid, (x_user_email or "").lower())


@router.get("/api/team/updates")
def team_updates(projectId: str, person: str, x_user_id: str = Header(default="")):
    uid = require_uid(x_user_id)
    return team.updates(uid, projectId, person)
