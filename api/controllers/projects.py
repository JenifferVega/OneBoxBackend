"""Endpoints de proyectos: listado, detalle, creación, participantes,
invitaciones y borrado."""
from fastapi import APIRouter, Header, HTTPException

from api.deps import require_uid
from api.schemas import (
    CreateProjectRequest, InviteRequest, RemoveParticipantRequest,
    UpdateParticipantsRequest,
)
from api.services import projects as projects_service

router = APIRouter()


@router.get("/api/projects")
async def get_projects(user_id: str = Header(alias="x-user-id", default=""), x_user_email: str = Header(default="")):
    """Lista todos los proyectos con datos enriquecidos (task counts, insights, etc.)."""
    uid = require_uid(user_id)
    user_email = x_user_email.lower() if x_user_email else ""
    try:
        return projects_service.list_projects(uid, user_email)
    except Exception as e:
        print(f"[API] Error en get_projects: {e}")
        import traceback; traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/api/projects/{project_id}")
async def get_project(project_id: str, x_user_id: str = Header(default="")):
    """Obtiene un proyecto específico con todos sus datos."""
    uid = require_uid(x_user_id)
    try:
        return projects_service.get_project_detail(uid, project_id)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/api/projects")
async def create_project(req: CreateProjectRequest, x_user_id: str = Header(default="")):
    """Crea un nuevo proyecto con análisis IA, notificaciones e insights."""
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


@router.put("/api/projects/{project_id}/participants")
async def update_participants(project_id: str, req: UpdateParticipantsRequest, x_user_id: str = Header(default="")):
    """Actualiza los participantes de un proyecto (incluye teléfonos)."""
    uid = require_uid(x_user_id)
    try:
        return projects_service.update_participants(uid, project_id, req.participants)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/api/projects/{project_id}/invite")
async def invite_user_to_project(project_id: str, req: InviteRequest, x_user_id: str = Header(default="")):
    """Añade una persona al equipo del proyecto. Acepta email y/o teléfono.

    - Email + send_notification=True: crea usuario en Cognito (email con
      contraseña temporal) y guarda invitación pendiente.
    - Teléfono + send_notification=True: manda WhatsApp via Twilio.
    - En todos los casos registra el contacto en participants[] del proyecto.
    - send_notification=False: solo registra el contacto, sin notificar."""
    uid = require_uid(x_user_id)
    return projects_service.invite_user(
        uid, project_id,
        email=req.email, phone=req.phone, name=req.name,
        role=req.role, send_notification=req.send_notification,
    )


@router.delete("/api/projects/{project_id}/participants")
async def remove_participant(project_id: str, req: RemoveParticipantRequest, x_user_id: str = Header(default="")):
    """Elimina un participante del equipo: lo saca de participants[], vacía el
    assignedTo de sus tareas y revoca sus invitaciones. Solo el owner (RBAC)."""
    uid = require_uid(x_user_id)
    return projects_service.remove_participant(
        uid, project_id,
        email=req.email, phone=req.phone, name=req.name,
    )


@router.delete("/api/projects/{project_id}")
async def delete_project(project_id: str, x_user_id: str = Header(default="")):
    """Elimina un proyecto y sus datos relacionados (insights, notificaciones, tareas)."""
    uid = require_uid(x_user_id)
    try:
        return projects_service.delete_project(uid, project_id)
    except HTTPException:
        raise
    except Exception as e:
        print(f"[delete_project] Error: {e}")
        import traceback; traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))
