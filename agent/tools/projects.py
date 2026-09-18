"""Project tools: create, list, contacts, edit, participants and invitations.

Moved verbatim out of the old single-file agent/tools.py.
"""

from datetime import datetime
import uuid

from agent.tools.access import _accessible_project_ids, _has_project_access
from agent.tools.context import _current_uid
from agent.tools.db import conversations_table, projects_table, tasks_table
from agent.tools.errors import _svc_error
from agent.tools.registry import register_tool


@register_tool("create_project")
def create_project(name: str, description: str = "", type: str = "Other", participants: list = None, channels: list = None) -> dict:
    """Creates a project in DynamoDB."""
    try:
        project_id = "proj-" + uuid.uuid4().hex[:8]
        now = datetime.utcnow().isoformat()
        item = {
            'projectId': project_id,
            'userId': _current_uid(),
            'name': name,
            'description': description,
            'type': type,
            'status': 'active',
            'participants': participants or [],
            'channels': channels or ['Gmail'],
            'createdAt': now,
            'lastActivity': now
        }
        projects_table.put_item(Item=item)
        return {"success": True, "projectId": project_id, "name": name}
    except Exception as e:
        return {"error": str(e)}


@register_tool("assign_email_to_project")
def assign_email_to_project(conversation_id: str, project_id: str, project_name: str = "") -> dict:
    """Moves an email from 'unassigned' to a project."""
    # SECURITY: block if the LLM tries to assign to a project the user
    # cannot see (not owner, not shared, not invited).
    if not _has_project_access(project_id):
        return {"error": "No access to that project"}
    try:
        result = conversations_table.get_item(
            Key={'projectId': 'unassigned', 'conversationId': conversation_id}
        )
        if 'Item' not in result:
            return {"error": "Email not found"}

        item = result['Item']

        item['projectId'] = project_id
        item['status'] = 'assigned'
        conversations_table.put_item(Item=item)
        
        conversations_table.delete_item(
            Key={'projectId': 'unassigned', 'conversationId': conversation_id}
        )
        
        return {"success": True, "conversationId": conversation_id, "projectId": project_id}
    except Exception as e:
        return {"error": str(e)}


@register_tool("list_projects")
def list_projects() -> dict:
    """Lists ALL projects accessible to the current user: own + shared + invited.

    IMPORTANT: uses the same access definition as the /api/projects endpoint
    so the chat is consistent with the dashboard. Does NOT leak other users'
    projects: only returns those where the uid is owner, the email appears
    in participants, or there is an accepted invitation.
    """
    try:
        accessible_ids = _accessible_project_ids()
        if not accessible_ids:
            return {"count": 0, "projects": []}
        # Hydrate each projectId with the full item. We call get_item one at
        # a time (there is no convenient batch_get here) — in practice there
        # are few per user and we want fresh data.
        items = []
        for pid in accessible_ids:
            r = projects_table.get_item(Key={'projectId': pid}).get('Item')
            if r:
                items.append(r)
        return {"count": len(items), "projects": items}
    except Exception as e:
        return {"error": str(e)}


@register_tool("get_project_contacts")
def get_project_contacts(project_id: str) -> dict:
    """Gets a project's participants with their phone numbers and pending tasks."""
    if not _has_project_access(project_id):
        return {"error": "No access to that project"}
    try:
        from boto3.dynamodb.conditions import Attr

        result = projects_table.get_item(Key={'projectId': project_id})
        project = result.get('Item')
        if not project:
            return {"error": f"Project {project_id} not found"}

        # Tasks are filtered ONLY by projectId. Previously we also filtered
        # by the logged-in user's userId — but tasks have userId=project
        # owner, so invitees didn't see tasks of shared projects. We already
        # validated project access above, so this is safe.
        tasks_result = tasks_table.scan(
            FilterExpression=Attr('projectId').eq(project_id)
        )
        all_tasks = tasks_result.get('Items', [])

        # Same normalization the API uses. This one is not cosmetic: these are
        # the phone numbers the agent sends WhatsApp to. A participant stored
        # as {nombre, telefono} used to come back with an empty phone and the
        # whole dict as the name -- so the person could not be messaged at all,
        # and the agent reported them as having no number.
        from api.services.projects import normalize_participants

        contacts = []
        for p in normalize_participants(project.get('participants', [])):
            name = p['name'] or p['email'] or p['phone']
            phone = p['phone']
            role = p['role'] or 'Member'
            email = p['email']

            pending = [t for t in all_tasks if t.get('assignedTo', '') == name and t.get('status') in ('pending', 'in_progress', 'blocked')]

            contacts.append({
                'name': name,
                'phone': phone,
                'email': email,
                'role': role,
                'has_whatsapp': bool(phone),
                'pending_tasks': [{'text': t.get('text', ''), 'status': t.get('status', '')} for t in pending],
                'total_pending': len(pending)
            })

        return {
            "project_name": project.get('name', ''),
            "project_id": project_id,
            "total_contacts": len(contacts),
            "contacts_with_phone": len([c for c in contacts if c['has_whatsapp']]),
            "contacts": contacts
        }
    except Exception as e:
        return {"error": str(e)}


@register_tool("update_project")
def update_project(project_id: str, name: str = None, description: str = None,
                        type: str = None, status: str = None,
                        delivery_date: str = None, timing: str = None) -> dict:
    """Edits allowed fields of a project. Only the owner can edit."""
    if not _has_project_access(project_id):
        return {"error": "No access to that project"}
    updates = {}
    if name is not None:
        updates["name"] = name
    if description is not None:
        updates["description"] = description
    if type is not None:
        updates["type"] = type
    if status is not None:
        updates["status"] = status
    if delivery_date is not None:
        updates["deliveryDate"] = delivery_date
    if timing is not None:
        updates["timing"] = timing
    if not updates:
        return {"error": "You did not specify any field to update."}
    try:
        from api.services.projects import update_project
        return update_project(_current_uid(), project_id, updates)
    except Exception as e:
        return {"error": _svc_error(e)}


@register_tool("invite_user")
def invite_user(project_id: str, email: str = "", phone: str = "",
                    name: str = "", role: str = "", send_notification: bool = True) -> dict:
    """Adds a person to the project team (email and/or phone)."""
    if not _has_project_access(project_id):
        return {"error": "No access to that project"}
    if not (email or "").strip() and not (phone or "").strip():
        return {"error": "Email or phone is required to invite someone."}
    try:
        from api.services.projects import invite_user
        return invite_user(
            _current_uid(), project_id,
            email=email or "", phone=phone or "",
            name=name or "", role=role or "",
            send_notification=send_notification,
        )
    except Exception as e:
        return {"error": _svc_error(e)}


@register_tool("update_participants")
def update_participants(project_id: str, participants: list = None) -> dict:
    """Replaces the full participants list of the project. Owner only."""
    if not _has_project_access(project_id):
        return {"error": "No access to that project"}
    if not isinstance(participants, list):
        return {"error": "participants must be a list of {name, email, role, phone}."}
    try:
        from api.services.projects import update_participants
        return update_participants(_current_uid(), project_id, participants)
    except Exception as e:
        return {"error": _svc_error(e)}


@register_tool("remove_participant")
def remove_participant(project_id: str, email: str = "", phone: str = "", name: str = "") -> dict:
    """Removes a person from the project team. Owner only."""
    if not _has_project_access(project_id):
        return {"error": "No access to that project"}
    if not (email or "").strip() and not (phone or "").strip() and not (name or "").strip():
        return {"error": "Provide email, phone or name of the person to remove."}
    try:
        from api.services.projects import remove_participant
        return remove_participant(
            _current_uid(), project_id,
            email=email or "", phone=phone or "", name=name or "",
        )
    except Exception as e:
        return {"error": _svc_error(e)}
