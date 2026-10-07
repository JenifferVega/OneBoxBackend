"""Planning chat endpoints (Intelligence > Replanificación). See
api/services/planning_chat.py for the flow."""
from typing import List, Optional

from fastapi import APIRouter, File, Form, Header, UploadFile
from pydantic import BaseModel

from api.deps import require_uid
from api.services import planning_chat

router = APIRouter()


class Decision(BaseModel):
    decision: str                      # approve_all | approve_some | reject_all
    opIndexes: Optional[List[int]] = None


@router.get("/api/projects/{project_id}/planning")
async def planning_state(project_id: str, x_user_id: str = Header(default=""), x_user_email: str = Header(default="")):
    uid = require_uid(x_user_id)
    return planning_chat.get_state(uid, (x_user_email or "").lower(), project_id)


@router.post("/api/projects/{project_id}/planning/messages")
async def planning_message(
    project_id: str,
    message: str = Form(""),
    context: str = Form(""),
    includeTrello: bool = Form(False),
    rereadAll: bool = Form(False),
    file: Optional[UploadFile] = File(None),
    x_user_id: str = Header(default=""),
    x_user_email: str = Header(default=""),
):
    uid = require_uid(x_user_id)
    f = None
    if file is not None and file.filename:
        f = (await file.read(), file.filename, file.content_type or "")
    return planning_chat.start_message(uid, (x_user_email or "").lower(), project_id, message, context, f,
                                       include_trello=includeTrello, reread_all=rereadAll)


@router.get("/api/projects/{project_id}/planning/jobs/{job_id}")
async def planning_job(project_id: str, job_id: str, x_user_id: str = Header(default=""),
                       x_user_email: str = Header(default="")):
    uid = require_uid(x_user_id)
    return planning_chat.job_status(uid, (x_user_email or "").lower(), project_id, job_id)


@router.post("/api/projects/{project_id}/planning/proposals/{proposal_id}/decision")
async def planning_decision(project_id: str, proposal_id: str, req: Decision,
                            x_user_id: str = Header(default=""), x_user_email: str = Header(default="")):
    uid = require_uid(x_user_id)
    if req.decision not in ("approve_all", "approve_some", "reject_all"):
        from fastapi import HTTPException
        raise HTTPException(status_code=400, detail="decision must be approve_all, approve_some or reject_all")
    return planning_chat.decide(uid, (x_user_email or "").lower(), project_id, proposal_id, req.decision,
                                req.opIndexes or [])


@router.post("/api/projects/{project_id}/planning/proposals/{proposal_id}/revert")
async def planning_revert(project_id: str, proposal_id: str, x_user_id: str = Header(default=""),
                          x_user_email: str = Header(default="")):
    uid = require_uid(x_user_id)
    return planning_chat.revert(uid, (x_user_email or "").lower(), project_id, proposal_id)
