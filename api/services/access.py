"""Control de acceso a proyectos: owner, participantes por email e invitaciones."""
from boto3.dynamodb.conditions import Attr, Key

from agent.tools import invitations_table, projects_table
from api.deps import scan_all_pages


def accessible_project_ids(uid: str, user_email: str) -> set:
    """Devuelve el set de projectIds a los que el usuario tiene acceso
    (own + shared por email + invited accepted). Usado por endpoints que
    listan items multi-proyecto (insights, notifications, etc.)."""
    accessible = set()
    # Propios
    own = scan_all_pages(projects_table, FilterExpression=Attr('userId').eq(uid))
    for p in own:
        accessible.add(p['projectId'])
    em = (user_email or '').strip().lower()
    if not em:
        return accessible
    # Por email exacto en participants
    for p in scan_all_pages(projects_table):
        if p['projectId'] in accessible:
            continue
        for part in (p.get('participants') or []):
            if (part.get('email', '') or '').strip().lower() == em:
                accessible.add(p['projectId'])
                break
    # Invitaciones aceptadas
    try:
        inv_resp = invitations_table.query(
            IndexName='email-index',
            KeyConditionExpression=Key('email').eq(em),
        )
        for inv in inv_resp.get('Items', []):
            if inv.get('status') == 'accepted' and inv.get('projectId'):
                accessible.add(inv['projectId'])
    except Exception as e:
        print(f"[accessible_project_ids] Error invitaciones: {e}")
    return accessible


def has_project_access(uid: str, user_email: str, project_id: str):
    """Devuelve (has_access, is_owner, project_dict).

    Un usuario tiene acceso a un proyecto si:
      1) Es el owner (proj.userId == uid), o
      2) Su email aparece como participante del proyecto, o
      3) Tiene una invitación accepted para ese proyecto.

    is_owner: True solo cuando el usuario es el dueño original. Los
    endpoints administrativos (delete proyecto, invitar, modificar
    participantes, borrar adjunto) deben requerir is_owner=True.
    """
    proj = projects_table.get_item(Key={'projectId': project_id}).get('Item')
    if not proj:
        return False, False, None
    # 1) Owner
    if proj.get('userId') == uid:
        return True, True, proj
    # 2) Participante por email exacto
    em = (user_email or '').strip().lower()
    if em:
        for part in (proj.get('participants') or []):
            part_email = (part.get('email', '') or '').strip().lower()
            if part_email and part_email == em:
                return True, False, proj
        # 3) Invitación accepted
        try:
            inv_resp = invitations_table.query(
                IndexName='email-index',
                KeyConditionExpression=Key('email').eq(em),
            )
            for inv in inv_resp.get('Items', []):
                if inv.get('projectId') == project_id and inv.get('status') == 'accepted':
                    return True, False, proj
        except Exception as e:
            print(f"[has_project_access] Error consultando invitaciones: {e}")
    return False, False, proj
