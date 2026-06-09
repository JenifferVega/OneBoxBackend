"""Endpoints de adjuntos de proyecto: subir, listar, descargar y borrar."""
from fastapi import APIRouter, File, Header, HTTPException, UploadFile

from api.deps import require_uid
from api.services import attachments as attachments_service

router = APIRouter()


@router.post("/api/projects/{project_id}/attachments")
async def upload_attachment(
    project_id: str,
    file: UploadFile = File(...),
    x_user_id: str = Header(default=""),
    x_user_email: str = Header(default=""),
):
    """Adjunta un documento a un proyecto. Owner Y invitados con acceso pueden subir."""
    uid = require_uid(x_user_id)
    try:
        file_bytes = await file.read()
        return attachments_service.upload_attachment(
            uid, x_user_email, project_id, file_bytes, file.filename or '', file.content_type or ''
        )
    except HTTPException:
        raise
    except Exception as e:
        print(f"[attachment] Error: {e}")
        import traceback; traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/api/projects/{project_id}/attachments")
async def list_attachments(project_id: str, x_user_id: str = Header(default=""), x_user_email: str = Header(default="")):
    """Lista los adjuntos de un proyecto. Owner Y invitados con acceso."""
    uid = require_uid(x_user_id)
    try:
        return attachments_service.list_attachments(uid, x_user_email, project_id)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/api/attachments/{project_id}/{attachment_id}/download")
async def download_attachment(project_id: str, attachment_id: str, x_user_id: str = Header(default=""), x_user_email: str = Header(default="")):
    """Genera URL presignada de S3 para descargar el adjunto."""
    uid = require_uid(x_user_id)
    try:
        return attachments_service.get_download_url(uid, x_user_email, project_id, attachment_id)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.delete("/api/attachments/{project_id}/{attachment_id}")
async def delete_attachment(project_id: str, attachment_id: str, x_user_id: str = Header(default=""), x_user_email: str = Header(default="")):
    """Elimina un adjunto (S3 + registro DynamoDB). SOLO el owner del proyecto puede borrar."""
    uid = require_uid(x_user_id)
    try:
        return attachments_service.delete_attachment(uid, x_user_email, project_id, attachment_id)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
