"""Endpoints de análisis de documentos/texto y creación de proyectos desde ellos."""
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
    """Analiza un texto pegado SIN crear proyecto. Devuelve draftId + sugerencia.
    Equivalente a /api/documents/analyze pero para texto. Reusa /api/projects/from-document-draft
    para confirmar."""
    uid = require_uid(x_user_id)
    try:
        return documents_service.analyze_text_preview(uid, req.text, req.source)
    except HTTPException:
        raise
    except Exception as e:
        print(f"[analyze_text_preview] Error: {e}")
        import traceback; traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/api/documents/analyze")
async def analyze_document_preview(
    file: UploadFile = File(...),
    x_user_id: str = Header(default="")
):
    """Analiza un documento (extrae texto + sugiere metadata) SIN crear el proyecto.
    El frontend muestra el preview, el usuario revisa/edita y luego confirma.
    Devuelve un draft_id que se usará después en /api/projects/from-document-draft."""
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
    """Crea el proyecto definitivo a partir de un draft analizado previamente.
    Mueve el archivo del draft a la carpeta del proyecto y registra el adjunto."""
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
    """Crea un proyecto a partir de un documento. La IA infiere nombre, tipo
    y descripción si no se proveen. El documento queda anexado al proyecto."""
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
    """Crea un proyecto desde un texto pegado (conversación WhatsApp, correo, notas).
    La IA infiere nombre, tipo, descripción y genera insights automáticamente."""
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
    """Analiza un texto pegado dentro de un proyecto existente.
    Genera nuevos insights (tareas, riesgos, decisiones) sin crear un proyecto nuevo.
    Permite a owner E invitados con acceso al proyecto."""
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
