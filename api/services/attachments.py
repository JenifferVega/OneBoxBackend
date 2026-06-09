"""Lógica interna de adjuntos: subida, listado, descarga y borrado."""
import uuid
from datetime import datetime

from boto3.dynamodb.conditions import Key
from fastapi import HTTPException

from agent.tools import notifications_table
from api.deps import attachments_table
from api.services.access import has_project_access
from api.services.documents import save_attachment_record


def upload_attachment(uid: str, user_email: str, project_id: str,
                      file_bytes: bytes, file_name: str, content_type: str) -> dict:
    """Adjunta un documento a un proyecto. Owner Y invitados con acceso pueden subir.
    Se guarda uploadedBy (sub + email) para trazabilidad. El adjunto queda
    asociado al userId del owner del proyecto."""
    from agent.document_parser import extract_text, upload_to_s3, validate_file
    from agent.project_helpers import generate_insights_for_project

    has, _is_owner, existing = has_project_access(uid, user_email, project_id)
    if not existing:
        raise HTTPException(status_code=404, detail="Proyecto no encontrado")
    if not has:
        raise HTTPException(status_code=403, detail="Sin acceso a este proyecto")
    owner_uid = existing.get('userId', uid)

    valid, ext, error = validate_file(file_bytes, file_name or '', content_type or '')
    if not valid:
        raise HTTPException(status_code=400, detail=error)

    text = extract_text(file_bytes, ext)
    print(f"[attachment] {file_name}: {len(text)} caracteres extraídos")

    # Subir a S3
    s3_key = upload_to_s3(file_bytes, project_id, file_name or f'doc.{ext}', content_type or '')

    # Registrar adjunto (asociado al owner, con uploadedBy del que subió)
    att = save_attachment_record(
        project_id=project_id,
        user_id=owner_uid,
        file_name=file_name or f'doc.{ext}',
        file_size=len(file_bytes),
        content_type=content_type or '',
        ext=ext,
        s3_key=s3_key,
        extracted_text=text,
        source='web',
        uploaded_by=uid,
        uploaded_by_email=(user_email or '').strip().lower(),
    )

    # Si hay texto suficiente, generar insights adicionales (siempre en nombre del owner)
    insights_result = {"generated": False, "reason": "no_text"}
    if text and len(text.strip()) >= 100:
        insights_result = generate_insights_for_project(
            user_id=owner_uid,
            project_id=project_id,
            project_name=existing.get('name', 'Proyecto'),
            project_type=existing.get('type', 'Otro'),
            description=text[:5000],
            participants_count=len(existing.get('participants', []))
        )

        # Notificación in-app (queda en el feed del owner)
        if insights_result.get('generated') and insights_result.get('count', 0) > 0:
            try:
                notifications_table.put_item(Item={
                    'userId': owner_uid,
                    'notificationId': f"{datetime.utcnow().isoformat()}#{uuid.uuid4().hex[:8]}",
                    'projectId': project_id,
                    'projectName': existing.get('name', 'Proyecto'),
                    'type': 'document_analyzed',
                    'title': f'Documento analizado: {file_name}',
                    'mensaje': f'La IA generó {insights_result["count"]} nuevos insights desde "{file_name}"',
                    'canal': 'system',
                    'status': 'unread',
                    'createdAt': datetime.utcnow().isoformat(),
                })
            except Exception as e:
                print(f"[attachment] notif error: {e}")

    return {
        "success": True,
        "attachment": {
            'attachmentId': att['attachmentId'],
            'fileName': att['fileName'],
            'fileSize': att['fileSize'],
            'extractedTextLength': att['extractedTextLength'],
        },
        "insightsGenerated": insights_result
    }


def list_attachments(uid: str, user_email: str, project_id: str) -> list:
    """Lista los adjuntos de un proyecto. Owner Y invitados con acceso."""
    has, _is_owner, existing = has_project_access(uid, user_email, project_id)
    if not existing:
        raise HTTPException(status_code=404, detail="Proyecto no encontrado")
    if not has:
        raise HTTPException(status_code=403, detail="Sin acceso a este proyecto")

    result = attachments_table.query(
        KeyConditionExpression=Key('projectId').eq(project_id),
        ScanIndexForward=False
    )
    items = result.get('Items', [])
    return [{
        'attachmentId': i.get('attachmentId'),
        'fileName': i.get('fileName'),
        'fileSize': int(i.get('fileSize', 0)),
        'contentType': i.get('contentType', ''),
        'extension': i.get('extension', ''),
        'extractedTextPreview': i.get('extractedTextPreview', ''),
        'extractedTextLength': int(i.get('extractedTextLength', 0)),
        'source': i.get('source', 'web'),
        'createdAt': i.get('createdAt', ''),
    } for i in items]


def get_download_url(uid: str, user_email: str, project_id: str, attachment_id: str) -> dict:
    """Genera URL presignada de S3 para descargar el adjunto.
    Owner Y invitados con acceso al proyecto pueden descargar."""
    from agent.document_parser import generate_download_url

    item = attachments_table.get_item(
        Key={'projectId': project_id, 'attachmentId': attachment_id}
    ).get('Item')
    if not item:
        raise HTTPException(status_code=404, detail="Adjunto no encontrado")
    has, _is_owner, _proj = has_project_access(uid, user_email, project_id)
    if not has:
        raise HTTPException(status_code=403, detail="Sin acceso a este proyecto")

    url = generate_download_url(item['s3Key'], item.get('fileName', 'documento'))
    return {"url": url, "fileName": item.get('fileName'), "expiresIn": 600}


def delete_attachment(uid: str, user_email: str, project_id: str, attachment_id: str) -> dict:
    """Elimina un adjunto (S3 + registro DynamoDB). SOLO el owner del proyecto puede borrar."""
    from agent.document_parser import delete_from_s3

    item = attachments_table.get_item(
        Key={'projectId': project_id, 'attachmentId': attachment_id}
    ).get('Item')
    if not item:
        raise HTTPException(status_code=404, detail="Adjunto no encontrado")
    _has, is_owner, _proj = has_project_access(uid, user_email, project_id)
    if not is_owner:
        raise HTTPException(status_code=403, detail="Solo el dueño del proyecto puede borrar adjuntos")

    delete_from_s3(item.get('s3Key', ''))
    attachments_table.delete_item(Key={'projectId': project_id, 'attachmentId': attachment_id})
    return {"success": True}
