"""Project attachment endpoints: upload, list, download and delete."""
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
    """Attach a document to a project. Owner AND invited users with access may upload."""
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
    """List a project's attachments. Owner AND invited users with access."""
    uid = require_uid(x_user_id)
    try:
        return attachments_service.list_attachments(uid, x_user_email, project_id)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/api/attachments/{project_id}/{attachment_id}/download")
async def download_attachment(project_id: str, attachment_id: str, x_user_id: str = Header(default=""), x_user_email: str = Header(default="")):
    """Generate a presigned S3 URL to download the attachment."""
    uid = require_uid(x_user_id)
    try:
        return attachments_service.get_download_url(uid, x_user_email, project_id, attachment_id)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.delete("/api/attachments/{project_id}/{attachment_id}")
async def delete_attachment(project_id: str, attachment_id: str, x_user_id: str = Header(default=""), x_user_email: str = Header(default="")):
    """Delete an attachment (S3 + DynamoDB record). ONLY the project owner can delete."""
    uid = require_uid(x_user_id)
    try:
        return attachments_service.delete_attachment(uid, x_user_email, project_id, attachment_id)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
