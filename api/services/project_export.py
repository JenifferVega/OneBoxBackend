"""Full export of one project, for offline analysis.

Everything OneBox stores about a project, in one JSON document: the project
itself, its tasks, insights, attachments (with their full extracted text),
conversations, notifications and invitations.

Read-only. Nothing here writes to DynamoDB or S3.
"""
from datetime import datetime

from boto3.dynamodb.conditions import Attr, Key
from fastapi import HTTPException

from agent.tools import (
    conversations_table, insights_table, invitations_table,
    notifications_table, tasks_table,
)
from api.deps import attachments_table, scan_all_pages
from api.services.access import has_project_access

EXPORT_VERSION = 1

# An attachment's text is re-extracted from S3, because the attachments table
# only keeps the first 500 characters. Capped so one huge file cannot make the
# export unusable.
MAX_TEXT_CHARS = 200_000


def _query_all(table, **kwargs) -> list:
    """Paginated Query. query() stops at 1 MB just like scan()."""
    items, last_key = [], None
    while True:
        args = dict(kwargs)
        if last_key:
            args['ExclusiveStartKey'] = last_key
        res = table.query(**args)
        items.extend(res.get('Items', []))
        last_key = res.get('LastEvaluatedKey')
        if not last_key:
            return items


def _attachment_text(item: dict) -> dict:
    """Full text of one attachment, or the reason it could not be read."""
    from agent.document_parser import S3_ATTACHMENTS_BUCKET, extract_text, get_s3_client

    s3_key = item.get('s3Key', '')
    if not s3_key:
        return {'fullText': None, 'fullTextError': 'no s3Key'}
    try:
        obj = get_s3_client().get_object(Bucket=S3_ATTACHMENTS_BUCKET, Key=s3_key)
        text = extract_text(obj['Body'].read(), item.get('extension', '') or 'txt')
    except Exception as e:
        return {'fullText': None, 'fullTextError': str(e)[:300]}
    if not text:
        return {'fullText': None, 'fullTextError': 'no text could be extracted'}
    out = {'fullText': text[:MAX_TEXT_CHARS]}
    if len(text) > MAX_TEXT_CHARS:
        out['fullTextTruncatedAt'] = MAX_TEXT_CHARS
    return out


def _by(field: str):
    return lambda x: x.get(field, '') or ''


def export_project(uid: str, user_email: str, project_id: str,
                   include_text: bool = True) -> dict:
    """Everything stored about one project. Owner and participants with access."""
    has, _is_owner, proj = has_project_access(uid, user_email, project_id)
    if not proj:
        raise HTTPException(status_code=404, detail="Project not found")
    if not has:
        raise HTTPException(status_code=403, detail="No access to this project")

    # Tasks, insights and attachments are stored under the project OWNER's
    # userId, whoever is exporting.
    owner_uid = proj.get('userId', '')

    tasks = _query_all(tasks_table, KeyConditionExpression=Key('projectId').eq(project_id))
    tasks.sort(key=lambda t: (t.get('startDate') or t.get('dueDate') or '~',
                              t.get('createdAt', '')))

    insights = _query_all(
        insights_table,
        KeyConditionExpression=Key('userId').eq(owner_uid),
        FilterExpression=Attr('projectId').eq(project_id),
    ) if owner_uid else []
    insights.sort(key=_by('createdAt'))

    attachments = _query_all(attachments_table,
                             KeyConditionExpression=Key('projectId').eq(project_id))
    attachments.sort(key=_by('createdAt'))
    if include_text:
        for a in attachments:
            a.update(_attachment_text(a))

    conversations = _query_all(conversations_table,
                               KeyConditionExpression=Key('projectId').eq(project_id))
    conversations.sort(key=lambda c: c.get('date') or c.get('createdAt', ''))

    # Keyed by the user who sent or received them, so the only way to get every
    # one for this project is a filtered scan.
    notifications = scan_all_pages(notifications_table,
                                   FilterExpression=Attr('projectId').eq(project_id))
    notifications.sort(key=_by('createdAt'))

    invitations = scan_all_pages(invitations_table,
                                 FilterExpression=Attr('projectId').eq(project_id))
    invitations.sort(key=_by('createdAt'))

    status_counts: dict = {}
    for t in tasks:
        s = t.get('status', '') or 'unknown'
        status_counts[s] = status_counts.get(s, 0) + 1

    return {
        'exportVersion': EXPORT_VERSION,
        'exportedAt': datetime.utcnow().isoformat() + 'Z',
        'exportedBy': {'userId': uid, 'email': (user_email or '').lower()},
        'summary': {
            'tasks': len(tasks),
            'tasksByStatus': status_counts,
            'insights': len(insights),
            'attachments': len(attachments),
            'conversations': len(conversations),
            'notifications': len(notifications),
            'invitations': len(invitations),
        },
        'project': proj,
        'tasks': tasks,
        'insights': insights,
        'attachments': attachments,
        'conversations': conversations,
        'notifications': notifications,
        'invitations': invitations,
    }
