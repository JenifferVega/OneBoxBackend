"""Task endpoints: list/create by project and edit/delete by taskId."""
from fastapi import APIRouter, Header, HTTPException, Query

from api.deps import require_uid
from api.schemas import CreateTaskRequest, UpdateTaskRequest
from api.services import tasks as tasks_service

router = APIRouter()


@router.get("/api/projects/{project_id}/tasks")
async def get_tasks(project_id: str, x_user_id: str = Header(default=""), x_user_email: str = Header(default="")):
    """List a project's tasks. Accessible to owner AND invited users."""
    uid = require_uid(x_user_id)
    try:
        return tasks_service.list_tasks(uid, x_user_email, project_id)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/api/projects/{project_id}/tasks")
async def create_task(project_id: str, req: CreateTaskRequest, x_user_id: str = Header(default=""), x_user_email: str = Header(default="")):
    """Create a task in a project. Accessible to owner AND invited users."""
    uid = require_uid(x_user_id)
    try:
        return tasks_service.create_task(uid, x_user_email, project_id, req)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.put("/api/tasks/{task_id}")
async def update_task(task_id: str, req: UpdateTaskRequest, x_user_id: str = Header(default=""), x_user_email: str = Header(default="")):
    """Update a task. Accessible to owner AND invited users with access to the project."""
    uid = require_uid(x_user_id)
    try:
        return tasks_service.update_task(uid, x_user_email, task_id, req)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.delete("/api/tasks/{task_id}")
async def delete_task(task_id: str, cascade: bool = Query(False), x_user_id: str = Header(default=""), x_user_email: str = Header(default="")):
    """Delete a task. Owner AND invited users with access to the project can delete."""
    uid = require_uid(x_user_id)
    try:
        return tasks_service.delete_task(uid, x_user_email, task_id, cascade)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
