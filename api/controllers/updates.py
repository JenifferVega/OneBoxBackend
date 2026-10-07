"""Member updates (chat in the app) and their consolidation for the owner.
See api/services/member_updates.py and updates_consolidation.py."""
from typing import List, Optional

from fastapi import APIRouter, Header
from pydantic import BaseModel

from api.deps import require_uid
from api.services import member_updates, planning_chat, updates_consolidation

router = APIRouter()


class Turn(BaseModel):
    messages: List[str]                # one or more chat bubbles sent together
    name: Optional[str] = ""           # only for participants stored without email


@router.get("/api/updates/pending")
async def updates_pending(name: str = "", x_user_id: str = Header(default=""), x_user_email: str = Header(default="")):
    """Projects where the logged user owes an update (blocking chat on login)."""
    uid = require_uid(x_user_id)
    return member_updates.pending(uid, (x_user_email or "").lower(), name)


@router.get("/api/updates/{project_id}/session")
def updates_session(project_id: str, name: str = "", x_user_id: str = Header(default=""),
                    x_user_email: str = Header(default="")):
    """The member's open conversation, to resume it after a reload."""
    uid = require_uid(x_user_id)
    return member_updates.open_session(uid, (x_user_email or "").lower(), project_id, name)


@router.post("/api/updates/{project_id}/message")
def updates_message(project_id: str, req: Turn, x_user_id: str = Header(default=""),
                    x_user_email: str = Header(default="")):
    """One member turn -> the AI's reply (questions or the closing summary)."""
    uid = require_uid(x_user_id)
    return member_updates.converse(uid, (x_user_email or "").lower(), project_id, req.messages, req.name or "")


@router.post("/api/projects/{project_id}/updates/consolidate")
def updates_consolidate(project_id: str, x_user_id: str = Header(default=""), x_user_email: str = Header(default="")):
    """On demand ('Consolidar ahora'): same as the daily digest."""
    uid = require_uid(x_user_id)
    email = (x_user_email or "").lower()
    planning_chat._access(uid, email, project_id)
    return updates_consolidation.consolidate(project_id, actor=email or uid)


@router.get("/api/projects/{project_id}/updates")
def updates_list(project_id: str, all: bool = False, x_user_id: str = Header(default=""),
                 x_user_email: str = Header(default="")):
    """Closed member updates (default: not yet consolidated) for the owner."""
    uid = require_uid(x_user_id)
    planning_chat._access(uid, (x_user_email or "").lower(), project_id)
    rows = member_updates.sessions(project_id, only_unconsolidated=not all)
    return {"updates": planning_chat._plain([{k: r.get(k) for k in ("sessionId", "person", "email", "closedAt",
                                                                    "consolidatedAt", "items", "messages")}
                                             for r in rows])}
