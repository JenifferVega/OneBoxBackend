"""Project access rules -- the same definition the /api/projects endpoint uses.

Moved verbatim out of the old single-file agent/tools.py.
"""

from agent.tools.context import _current_email, _current_uid
from agent.tools.db import invitations_table, projects_table


# ============================================================================
# PROJECT ACCESS — SAME LOGIC AS THE /api/projects ENDPOINT
# ----------------------------------------------------------------------------
# The agent must respect exactly the same permissions as the UI:
#   - own:     projects the user created (userId == uid)
#   - shared:  projects where their email appears in participants[]
#   - invited: projects with an accepted invitation for their email
#
# Without this logic, agent tools either (a) hide legitimate projects (chat
# says "you have 2" while the UI shows 3), or (b) leak other users' projects
# if they only filter by participants without checking uid.
# ============================================================================

def _accessible_project_ids(uid: str = "", email: str = "") -> set:
    """Returns the set of projectIds the user can see.
    If uid/email are not passed, uses the current context."""
    from boto3.dynamodb.conditions import Key
    uid = uid or _current_uid()
    email = (email or _current_email() or "").strip().lower()

    accessible = set()

    # 1) Own
    try:
        own = projects_table.query(
            IndexName='userId-index',
            KeyConditionExpression=Key('userId').eq(uid),
            ProjectionExpression='projectId',
        )
        accessible.update(p['projectId'] for p in own.get('Items', []))
    except Exception as e:
        print(f"[_accessible_project_ids] own query error: {e}")

    if not email:
        return accessible

    # 2) Shared by email (scan + client-side filter because participants is a nested list)
    # PAGINATED: one scan() call returns at most 1 MB. Without the loop, a
    # project stored past the first page was invisible to the people it was
    # shared with -- in the project list, in the agent's access checks and in
    # the contact list -- and it got worse as the table grew.
    try:
        items, last_key = [], None
        while True:
            kwargs = {'ProjectionExpression': 'projectId, participants'}
            if last_key:
                kwargs['ExclusiveStartKey'] = last_key
            page = projects_table.scan(**kwargs)
            items.extend(page.get('Items', []))
            last_key = page.get('LastEvaluatedKey')
            if not last_key:
                break
        for p in items:
            pid = p.get('projectId')
            if not pid or pid in accessible:
                continue
            for part in (p.get('participants') or []):
                if not isinstance(part, dict):
                    continue
                if (part.get('email', '') or '').strip().lower() == email:
                    accessible.add(pid)
                    break
    except Exception as e:
        print(f"[_accessible_project_ids] shared scan error: {e}")

    # 3) Invited (accepted invitations for this email)
    try:
        inv = invitations_table.query(
            IndexName='email-index',
            KeyConditionExpression=Key('email').eq(email),
        )
        for i in inv.get('Items', []):
            if i.get('status') == 'accepted' and i.get('projectId'):
                accessible.add(i['projectId'])
    except Exception as e:
        print(f"[_accessible_project_ids] invitations query error: {e}")

    return accessible


def _has_project_access(project_id: str) -> bool:
    """Last-mile defense: can the context user touch this projectId?
    Any write tool (create_task, create_insight, etc.) must call this
    before touching DynamoDB so the LLM cannot inject other users' projectIds."""
    if not project_id:
        return False
    return project_id in _accessible_project_ids()
