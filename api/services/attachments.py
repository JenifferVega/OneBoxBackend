"""Attachments internal logic: upload, list, download and delete."""
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
    """Attach a document to a project. Owner AND invited users with access can upload.
    Stores uploadedBy (sub + email) for traceability. The attachment is
    associated with the project owner's userId."""
    from agent.document_parser import extract_text, upload_to_s3, validate_file
    from agent.project_helpers import generate_insights_for_project

    has, _is_owner, existing = has_project_access(uid, user_email, project_id)
    if not existing:
        raise HTTPException(status_code=404, detail="Project not found")
    if not has:
        raise HTTPException(status_code=403, detail="No access to this project")
    owner_uid = existing.get('userId', uid)

    valid, ext, error = validate_file(file_bytes, file_name or '', content_type or '')
    if not valid:
        raise HTTPException(status_code=400, detail=error)

    text = extract_text(file_bytes, ext)
    print(f"[attachment] {file_name}: {len(text)} characters extracted")

    # Upload to S3
    s3_key = upload_to_s3(file_bytes, project_id, file_name or f'doc.{ext}', content_type or '')

    # Record attachment (associated with owner, uploadedBy set to actual uploader)
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

    # If there is enough text, generate additional insights (always on behalf of the owner)
    insights_result = {"generated": False, "reason": "no_text"}
    if text and len(text.strip()) >= 100:
        insights_result = generate_insights_for_project(
            user_id=owner_uid,
            project_id=project_id,
            project_name=existing.get('name', 'Project'),
            project_type=existing.get('type', 'Other'),
            description=text[:5000],
            participants=existing.get('participants', []),
        )

        # In-app notification (kept in the owner's feed)
        if insights_result.get('generated') and insights_result.get('count', 0) > 0:
            try:
                notifications_table.put_item(Item={
                    'userId': owner_uid,
                    'notificationId': f"{datetime.utcnow().isoformat()}#{uuid.uuid4().hex[:8]}",
                    'projectId': project_id,
                    'projectName': existing.get('name', 'Project'),
                    'type': 'document_analyzed',
                    'title': f'Document analyzed: {file_name}',
                    'message': f'The AI generated {insights_result["count"]} updated insights from "{file_name}"',
                    'channel': 'system',
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
    """List a project's attachments. Owner AND invited users with access."""
    has, _is_owner, existing = has_project_access(uid, user_email, project_id)
    if not existing:
        raise HTTPException(status_code=404, detail="Project not found")
    if not has:
        raise HTTPException(status_code=403, detail="No access to this project")

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
    """Generate a presigned S3 URL to download the attachment.
    Owner AND invited users with access to the project can download."""
    from agent.document_parser import generate_download_url

    item = attachments_table.get_item(
        Key={'projectId': project_id, 'attachmentId': attachment_id}
    ).get('Item')
    if not item:
        raise HTTPException(status_code=404, detail="Attachment not found")
    has, _is_owner, _proj = has_project_access(uid, user_email, project_id)
    if not has:
        raise HTTPException(status_code=403, detail="No access to this project")

    url = generate_download_url(item['s3Key'], item.get('fileName', 'document'))
    return {"url": url, "fileName": item.get('fileName'), "expiresIn": 600}


def delete_attachment(uid: str, user_email: str, project_id: str, attachment_id: str) -> dict:
    """Delete an attachment (S3 + DynamoDB record). ONLY the project owner can delete."""
    from agent.document_parser import delete_from_s3

    item = attachments_table.get_item(
        Key={'projectId': project_id, 'attachmentId': attachment_id}
    ).get('Item')
    if not item:
        raise HTTPException(status_code=404, detail="Attachment not found")
    _has, is_owner, _proj = has_project_access(uid, user_email, project_id)
    if not is_owner:
        raise HTTPException(status_code=403, detail="Only the project owner can delete attachments")

    delete_from_s3(item.get('s3Key', ''))
    attachments_table.delete_item(Key={'projectId': project_id, 'attachmentId': attachment_id})
    return {"success": True}
