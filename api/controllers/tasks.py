"""Endpoints de tareas: listado/creación por proyecto y edición/borrado por taskId."""
from fastapi import APIRouter, Header, HTTPException, Query

from api.deps import require_uid
from api.schemas import CreateTaskRequest, UpdateTaskRequest
from api.services import tasks as tasks_service

router = APIRouter()


@router.get("/api/projects/{project_id}/tasks")
async def get_tasks(project_id: str, x_user_id: str = Header(default=""), x_user_email: str = Header(default="")):
    """Lista tareas de un proyecto. Accesible para owner Y invitados."""
    uid = require_uid(x_user_id)
    try:
        return tasks_service.list_tasks(uid, x_user_email, project_id)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/api/projects/{project_id}/tasks")
async def create_task(project_id: str, req: CreateTaskRequest, x_user_id: str = Header(default=""), x_user_email: str = Header(default="")):
    """Crea una tarea en un proyecto. Accesible para owner Y invitados."""
    uid = require_uid(x_user_id)
    try:
        return tasks_service.create_task(uid, x_user_email, project_id, req)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.put("/api/tasks/{task_id}")
async def update_task(task_id: str, req: UpdateTaskRequest, x_user_id: str = Header(default=""), x_user_email: str = Header(default="")):
    """Actualiza una tarea. Accesible para owner Y invitados con acceso al proyecto."""
    uid = require_uid(x_user_id)
    try:
        return tasks_service.update_task(uid, x_user_email, task_id, req)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.delete("/api/tasks/{task_id}")
async def delete_task(task_id: str, cascade: bool = Query(False), x_user_id: str = Header(default=""), x_user_email: str = Header(default="")):
    """Elimina una tarea. Owner Y invitados con acceso al proyecto pueden borrar."""
    uid = require_uid(x_user_id)
    try:
        return tasks_service.delete_task(uid, x_user_email, task_id, cascade)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
