"""Project endpoints: list, detail, creation, participants,
invitations and deletion."""
from fastapi import APIRouter, Header, HTTPException

from api.deps import require_uid
from api.schemas import (
    CreateProjectRequest, InviteRequest, RemoveParticipantRequest,
    UpdateParticipantsRequest, UpdateProjectRequest,
)
from api.services import projects as projects_service

router = APIRouter()


@router.get("/api/projects")
async def get_projects(user_id: str = Header(alias="x-user-id", default=""), x_user_email: str = Header(default="")):
    """List all projects with enriched data (task counts, insights, etc.)."""
    uid = require_uid(user_id)
    user_email = x_user_email.lower() if x_user_email else ""
    try:
        return projects_service.list_projects(uid, user_email)
    except Exception as e:
        print(f"[API] Error in get_projects: {e}")
        import traceback; traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/api/projects/{project_id}")
async def get_project(project_id: str, x_user_id: str = Header(default="")):
    """Get a specific project with all its data."""
    uid = require_uid(x_user_id)
    try:
        return projects_service.get_project_detail(uid, project_id)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/api/projects")
async def create_project(req: CreateProjectRequest, x_user_id: str = Header(default="")):
    """Create a new project with AI analysis, notifications and insights."""
    uid = require_uid(x_user_id)
    try:
        return projects_service.create_project(
            uid,
            name=req.name,
            description=req.description,
            project_type=req.type,
            channels=req.channels,
            participants=req.participants,
            timing=req.timing or '',
            delivery_date=req.deliveryDate or ''
        )
    except Exception as e:
        print(f"[create_project] Error: {e}")
        import traceback; traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))


@router.put("/api/projects/{project_id}")
async def update_project(project_id: str, req: UpdateProjectRequest, x_user_id: str = Header(default="")):
    """Edit fields of an existing project. Owner only (RBAC in the service).
    Only fields present in the body are updated (non-null)."""
    uid = require_uid(x_user_id)
    try:
        # Keep only the fields that were set (avoid overwriting existing DDB
        # values with None).
        updates = {k: v for k, v in req.model_dump().items() if v is not None}
        return projects_service.update_project(uid, project_id, updates)
    except HTTPException:
        raise
    except Exception as e:
        print(f"[update_project] Error: {e}")
        import traceback; traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))


@router.put("/api/projects/{project_id}/participants")
async def update_participants(project_id: str, req: UpdateParticipantsRequest, x_user_id: str = Header(default="")):
    """Update a project's participants (includes phones)."""
    uid = require_uid(x_user_id)
    try:
        return projects_service.update_participants(uid, project_id, req.participants)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/api/projects/{project_id}/invite")
async def invite_user_to_project(project_id: str, req: InviteRequest, x_user_id: str = Header(default="")):
    """Add a person to the project team. Accepts email and/or phone.

    - Email + send_notification=True: creates a user in Cognito (email with
      temporary password) and stores a pending invitation.
    - Phone + send_notification=True: sends a WhatsApp via Twilio.
    - In every case records the contact in the project's participants[].
    - send_notification=False: only records the contact, without notifying."""
    uid = require_uid(x_user_id)
    return projects_service.invite_user(
        uid, project_id,
        email=req.email, phone=req.phone, name=req.name,
        role=req.role, send_notification=req.send_notification,
    )


@router.delete("/api/projects/{project_id}/participants")
async def remove_participant(project_id: str, req: RemoveParticipantRequest, x_user_id: str = Header(default="")):
    """Remove a participant from the team: takes them out of participants[],
    clears the assignedTo on their tasks and revokes their invitations. Owner only (RBAC)."""
    uid = require_uid(x_user_id)
    return projects_service.remove_participant(
        uid, project_id,
        email=req.email, phone=req.phone, name=req.name,
    )


@router.delete("/api/projects/{project_id}")
async def delete_project(project_id: str, x_user_id: str = Header(default="")):
    """Delete a project and its related data (insights, notifications, tasks)."""
    uid = require_uid(x_user_id)
    try:
        return projects_service.delete_project(uid, project_id)
    except HTTPException:
        raise
    except Exception as e:
        print(f"[delete_project] Error: {e}")
        import traceback; traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))
