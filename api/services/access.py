"""Project access control: owner, participants by email and invitations."""
from boto3.dynamodb.conditions import Attr, Key

from agent.tools import invitations_table, projects_table
from api.deps import scan_all_pages


def accessible_project_ids(uid: str, user_email: str) -> set:
    """Return the set of projectIds the user has access to
    (own + shared by email + invited accepted). Used by endpoints listing
    multi-project items (insights, notifications, etc.)."""
    accessible = set()
    # Own
    own = scan_all_pages(projects_table, FilterExpression=Attr('userId').eq(uid))
    for p in own:
        accessible.add(p['projectId'])
    em = (user_email or '').strip().lower()
    if not em:
        return accessible
    # By exact email in participants
    for p in scan_all_pages(projects_table):
        if p['projectId'] in accessible:
            continue
        for part in (p.get('participants') or []):
            if (part.get('email', '') or '').strip().lower() == em:
                accessible.add(p['projectId'])
                break
    # Accepted invitations
    try:
        inv_resp = invitations_table.query(
            IndexName='email-index',
            KeyConditionExpression=Key('email').eq(em),
        )
        for inv in inv_resp.get('Items', []):
            if inv.get('status') == 'accepted' and inv.get('projectId'):
                accessible.add(inv['projectId'])
    except Exception as e:
        print(f"[accessible_project_ids] Invitations error: {e}")
    return accessible


def has_project_access(uid: str, user_email: str, project_id: str):
    """Return (has_access, is_owner, project_dict).

    A user has access to a project if:
      1) They are the owner (proj.userId == uid), or
      2) Their email appears as a project participant, or
      3) They have an accepted invitation for that project.

    is_owner: True only when the user is the original owner. Administrative
    endpoints (delete project, invite, modify participants, delete
    attachment) must require is_owner=True.
    """
    proj = projects_table.get_item(Key={'projectId': project_id}).get('Item')
    if not proj:
        return False, False, None
    # 1) Owner
    if proj.get('userId') == uid:
        return True, True, proj
    # 2) Participant by exact email
    em = (user_email or '').strip().lower()
    if em:
        for part in (proj.get('participants') or []):
            part_email = (part.get('email', '') or '').strip().lower()
            if part_email and part_email == em:
                return True, False, proj
        # 3) Accepted invitation
        try:
            inv_resp = invitations_table.query(
                IndexName='email-index',
                KeyConditionExpression=Key('email').eq(em),
            )
            for inv in inv_resp.get('Items', []):
                if inv.get('projectId') == project_id and inv.get('status') == 'accepted':
                    return True, False, proj
        except Exception as e:
            print(f"[has_project_access] Error querying invitations: {e}")
    return False, False, proj
