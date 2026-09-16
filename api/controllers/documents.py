"""Document/text analysis endpoints and project creation from them."""
from typing import Optional

from fastapi import APIRouter, File, Form, Header, HTTPException, UploadFile

from api.deps import require_uid
from api.schemas import (
    AnalyzeTextPreviewRequest, AnalyzeTextRequest,
    CreateProjectFromDraftRequest, CreateProjectFromTextRequest,
)
from api.services import documents as documents_service

router = APIRouter()


@router.post("/api/text/analyze")
async def analyze_text_preview(req: AnalyzeTextPreviewRequest, x_user_id: str = Header(default="")):
    """Analyze pasted text WITHOUT creating a project. Returns draftId + suggestion.
    Equivalent to /api/documents/analyze but for text. Reuses /api/projects/from-document-draft
    for confirmation."""
    uid = require_uid(x_user_id)
    try:
        return documents_service.analyze_text_preview(uid, req.text, req.source)
    except HTTPException:
        raise
    except Exception as e:
        print(f"[analyze_text_preview] Error: {e}")
        import traceback; traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/api/text/analyze-insights-dryrun")
async def analyze_text_insights_dryrun(req: AnalyzeTextRequest, x_user_id: str = Header(default="")):
    """DRY-RUN: run the AI insights analysis on the full text WITHOUT
    creating a project or writing to DynamoDB. Returns the raw LLM output so
    you can verify the AI quality/depth. Persists nothing."""
    uid = require_uid(x_user_id)
    try:
        return documents_service.analyze_text_insights_dryrun(uid, req.text)
    except HTTPException:
        raise
    except Exception as e:
        print(f"[analyze_text_insights_dryrun] Error: {e}")
        import traceback; traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/api/documents/analyze")
async def analyze_document_preview(
    file: UploadFile = File(...),
    x_user_id: str = Header(default="")
):
    """Analyze a document (extract text + suggest metadata) WITHOUT creating the project.
    The frontend shows the preview, the user reviews/edits and then confirms.
    Returns a draft_id later used in /api/projects/from-document-draft."""
    uid = require_uid(x_user_id)
    try:
        file_bytes = await file.read()
        return documents_service.analyze_document_preview(
            uid, file_bytes, file.filename or '', file.content_type or ''
        )
    except HTTPException:
        raise
    except Exception as e:
        print(f"[analyze_document] Error: {e}")
        import traceback; traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/api/projects/from-document-draft")
async def create_project_from_draft(req: CreateProjectFromDraftRequest, x_user_id: str = Header(default="")):
    """Create the final project from a previously analyzed draft.
    Moves the file from the draft folder to the project folder and records the attachment."""
    uid = require_uid(x_user_id)
    try:
        return documents_service.create_project_from_draft(uid, req)
    except HTTPException:
        raise
    except Exception as e:
        print(f"[from_draft] Error: {e}")
        import traceback; traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/api/projects/from-document")
async def create_project_from_document(
    file: UploadFile = File(...),
    name: Optional[str] = Form(None),
    channels: Optional[str] = Form(None),  # CSV: "Gmail,WhatsApp"
    x_user_id: str = Header(default="")
):
    """Create a project from a document. The AI infers name, type and
    description if they are not provided. The document is attached to the project."""
    uid = require_uid(x_user_id)
    try:
        file_bytes = await file.read()
        return documents_service.create_project_from_document(
            uid, file_bytes, file.filename or '', file.content_type or '', name, channels
        )
    except HTTPException:
        raise
    except Exception as e:
        print(f"[from_document] Error: {e}")
        import traceback; traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/api/projects/from-text")
async def create_project_from_text(req: CreateProjectFromTextRequest, x_user_id: str = Header(default="")):
    """Create a project from pasted text (WhatsApp conversation, email, notes).
    The AI infers name, type, description and automatically generates insights."""
    uid = require_uid(x_user_id)
    try:
        return documents_service.create_project_from_text(uid, req.text, req.name, req.channels, req.source)
    except HTTPException:
        raise
    except Exception as e:
        print(f"[from_text] Error: {e}")
        import traceback; traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/api/projects/{project_id}/analyze-text")
async def analyze_text_for_project(
    project_id: str,
    req: AnalyzeTextRequest,
    x_user_id: str = Header(default=""),
    x_user_email: str = Header(default=""),
):
    """Analyze pasted text within an existing project.
    Generates updated insights (tasks, risks, decisions) without creating a new project.
    Allowed for the owner AND invited users with access to the project."""
    uid = require_uid(x_user_id)
    try:
        return documents_service.analyze_text_for_project(
            uid, project_id, req.text, req.source, user_email=x_user_email
        )
    except HTTPException:
        raise
    except Exception as e:
        print(f"[analyze_text] Error: {e}")
        import traceback; traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))
