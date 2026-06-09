"""Lógica interna del inbox: conversaciones sin asignar, asignación a proyectos,
conversaciones por proyecto y notificaciones enviadas."""
from typing import Optional

from boto3.dynamodb.conditions import Attr, Key
from fastapi import HTTPException

from agent.tools import conversations_table, notifications_table
from api.deps import scan_all_pages
from api.services.access import has_project_access


def get_inbox(uid: str) -> list:
    """Lista conversaciones sin asignar del inbox."""
    items = scan_all_pages(
        conversations_table,
        FilterExpression=Attr('projectId').eq('unassigned') & Attr('userId').eq(uid)
    )
    for item in items:
        if item.get('body'):
            item['body'] = item['body'][:500]
    items.sort(key=lambda x: x.get('date', x.get('createdAt', '')), reverse=True)
    return items


def assign_conversation(conversation_id: str, project_id: str) -> dict:
    """Asigna una conversación del inbox a un proyecto."""
    result = conversations_table.get_item(
        Key={'projectId': 'unassigned', 'conversationId': conversation_id}
    )
    if 'Item' not in result:
        raise HTTPException(status_code=404, detail="Conversación no encontrada")

    item = result['Item']
    item['projectId'] = project_id
    item['status'] = 'assigned'
    conversations_table.put_item(Item=item)

    conversations_table.delete_item(
        Key={'projectId': 'unassigned', 'conversationId': conversation_id}
    )

    return {"success": True, "conversationId": conversation_id, "projectId": project_id}


def get_project_conversations(uid: str, user_email: str, project_id: str) -> list:
    """Lista conversaciones de un proyecto. Owner Y invitados con acceso."""
    has, _is_owner, _proj = has_project_access(uid, user_email, project_id)
    if not has:
        raise HTTPException(status_code=403, detail="Sin acceso a este proyecto")
    result = conversations_table.query(
        KeyConditionExpression=Key('projectId').eq(project_id)
    )
    convs = sorted(
        result.get('Items', []),
        key=lambda x: x.get('date', x.get('createdAt', '')),
        reverse=True
    )
    for c in convs:
        if c.get('body'):
            c['body'] = c['body'][:500]
    return convs


def list_notifications(uid: str, project_id: Optional[str] = None) -> list:
    """Lista notificaciones enviadas."""
    filter_expr = Attr('userId').eq(uid)
    if project_id:
        filter_expr = filter_expr & Attr('projectId').eq(project_id)

    items = sorted(
        scan_all_pages(notifications_table, FilterExpression=filter_expr),
        key=lambda x: x.get('createdAt', ''),
        reverse=True
    )
    return items[:50]
