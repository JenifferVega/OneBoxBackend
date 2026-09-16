"""Projects internal logic: enriched listing, detail, creation,
participants, invitations and cascade deletion."""
import os
import uuid
from datetime import datetime

import boto3
from boto3.dynamodb.conditions import Attr, Key
from fastapi import HTTPException

from agent.tools import (
    conversations_table, insights_table, invitations_table,
    notifications_table, projects_table, tasks_table,
)
from api.deps import scan_all_pages

TEAM_COLORS = [
    'from-violet-500 to-indigo-600',
    'from-blue-500 to-cyan-600',
    'from-pink-500 to-rose-600',
    'from-emerald-500 to-green-600',
    'from-amber-500 to-orange-600',
    'from-sky-500 to-blue-600',
    'from-rose-500 to-red-600',
]


def initials(name: str) -> str:
    if not name:
        return "?"
    return ''.join([w[0].upper() for w in name.split() if w])[:2]


# ── Participants: one shape, whatever the writer called the fields ──────────
# Participants are written by an LLM (create_project extracts them from the
# user's text), by the project wizard and by invite_user, and NOTHING enforced
# a shape. In a Spanish conversation the model emits {nombre, rol, telefono}
# instead of {name, role, phone}, and it was stored verbatim.
#
# Reading then did:  p.get('name', str(p))
# so a missing key fell back to THE WHOLE DICT, and the UI printed
# {'nombre': 'Kevin', 'rol': 'Desarrollador'} where a name should be. A
# default is meant to be a sane value, not the raw object it failed to read.
#
# Aliases are applied on READ as well as on write, so projects already stored
# with Spanish keys display correctly without a migration.
_PARTICIPANT_ALIASES = {
    'name':  ('name', 'nombre', 'nombre_completo', 'fullName', 'full_name',
              'displayName'),
    'role':  ('role', 'rol', 'cargo', 'puesto', 'role_inferred', 'roleInferred'),
    'email': ('email', 'correo', 'correo_electronico', 'mail', 'e_mail'),
    'phone': ('phone', 'telefono', 'teléfono', 'celular', 'movil', 'móvil',
              'phoneNumber', 'phone_number', 'whatsapp'),
}


def normalize_participant(p) -> dict:
    """A participant as {name, role, email, phone}, from any of the spellings
    seen in stored data. Unknown keys are dropped, never rendered."""
    if not isinstance(p, dict):
        # A bare string is a name -- that shape exists in older projects.
        text = str(p).strip() if p is not None else ''
        return {'name': text, 'role': '', 'email': '', 'phone': ''}

    lower = {str(k).lower(): v for k, v in p.items()}
    out = {}
    for field, keys in _PARTICIPANT_ALIASES.items():
        value = ''
        for k in keys:
            v = lower.get(k.lower())
            if isinstance(v, str) and v.strip():
                value = v.strip()
                break
        out[field] = value
    return out


def normalize_participants(items) -> list:
    """Normalize a list, dropping entries with nothing identifying in them.

    An entry with no name, email or phone cannot be shown, messaged or matched
    to a task -- keeping it only puts a blank row on the screen.
    """
    out = []
    for p in (items or []):
        n = normalize_participant(p)
        if n['name'] or n['email'] or n['phone']:
            out.append(n)
    return out


def channel_icon(name: str) -> str:
    m = {'gmail': 'email', 'email': 'email', 'whatsapp': 'whatsapp',
         'slack': 'slack', 'sms': 'sms', 'partners': 'partners'}
    return m.get(name.lower(), 'email')


def list_projects(uid: str, user_email: str) -> list:
    """List all projects with enriched data (task counts, insights, etc.)."""
    # Diagnostic log: without email, the invitation auto-accept does NOT fire
    # (querying the email-index GSI requires the exact email). If we see
    # requests with an empty user_email, the frontend is not sending
    # `x-user-email` — typical bug from old sessions where the email never
    # made it into localStorage.
    print(f"[list_projects] uid={uid[:8]}... email={'<empty>' if not user_email else user_email}")
    # Fallback: if the x-user-email header came empty (frontend bug / old
    # session / cache), resolve the email from Cognito using the sub. Without
    # this, invited users are stranded and never see their linked projects.
    # The sub is a Cognito UUID, so admin_get_user by Username=sub returns
    # the authoritative UserAttributes.email. Silent failure: if Cognito
    # fails or the user does not exist, continue with an empty email as before.
    if not user_email and uid:
        try:
            pool_id = os.environ.get('COGNITO_USER_POOL_ID', 'us-east-1_b76prubhx')
            cip = boto3.client('cognito-idp', region_name='us-east-1')
            resp = cip.admin_get_user(UserPoolId=pool_id, Username=uid)
            for a in resp.get('UserAttributes', []):
                if a.get('Name') == 'email':
                    user_email = (a.get('Value', '') or '').strip().lower()
                    break
            print(f"[list_projects] fallback Cognito lookup → email={user_email or '<not-found>'}")
        except Exception as e:
            print(f"[list_projects] Cognito fallback lookup failed: {e}")
    # IMPORTANT: we use scan_all_pages to avoid losing items to pagination.
    # DynamoDB Scan has a 1MB per-page limit and applies the filter AFTER reading.
    own_projects = scan_all_pages(
        projects_table,
        FilterExpression=Attr('userId').eq(uid)
    )
    own_ids = {p['projectId'] for p in own_projects}

    # Shared projects: only if the user's email appears EXACTLY as a
    # participant. We removed name matching (too permissive and dangerous
    # — it could show other users' projects on casual name collisions).
    shared_projects = []
    if user_email:
        all_proj_items = scan_all_pages(projects_table)
        for p in all_proj_items:
            if p['projectId'] in own_ids:
                continue
            participants = p.get('participants', [])
            for part in participants:
                part_email = (part.get('email', '') or '').strip().lower()
                # Only match by EXACT EMAIL. Nothing else.
                if part_email and part_email == user_email:
                    p['_shared'] = True
                    shared_projects.append(p)
                    break

    # INVITATION projects: if the user has pending invitations for their
    # email, mark them accepted and add the projects.
    invited_projects = []
    if user_email:
        try:
            inv_resp = invitations_table.query(
                IndexName='email-index',
                KeyConditionExpression=Key('email').eq(user_email),
            )
            invs = inv_resp.get('Items', [])
            print(f"[list_projects] found {len(invs)} invitation(s) for {user_email}")
            inv_now = datetime.utcnow().isoformat()
            seen_proj_ids = own_ids | {p['projectId'] for p in shared_projects}
            for inv in invs:
                pid = inv.get('projectId')
                if not pid or pid in seen_proj_ids:
                    continue
                # Auto-accept if pending
                if inv.get('status') == 'pending':
                    try:
                        invitations_table.update_item(
                            Key={'invitationId': inv['invitationId']},
                            UpdateExpression="SET #s = :s, acceptedAt = :a, acceptedBy = :b",
                            ExpressionAttributeNames={'#s': 'status'},
                            ExpressionAttributeValues={':s': 'accepted', ':a': inv_now, ':b': uid},
                        )
                    except Exception as e:
                        print(f"[list_projects] Could not auto-accept invitation {inv.get('invitationId')}: {e}")
                # Load the project and add it
                proj_item = projects_table.get_item(Key={'projectId': pid}).get('Item')
                if proj_item:
                    proj_item['_invited'] = True
                    # Auto-add to the project's participants[] if not already
                    # present (idempotent: if already there by email, do not duplicate)
                    try:
                        current_parts = proj_item.get('participants', []) or []
                        inv_email_norm = (inv.get('email', '') or '').strip().lower()
                        already_in = any(
                            (p.get('email', '') or '').strip().lower() == inv_email_norm
                            for p in current_parts if isinstance(p, dict)
                        )
                        if inv_email_norm and not already_in:
                            # Name = local part of the email (kotomivega@gmail.com → kotomivega)
                            derived_name = inv_email_norm.split('@')[0] or inv_email_norm
                            new_part = {
                                'name': derived_name,
                                'email': inv_email_norm,
                                'phone': '',
                                'role': 'Invited',
                            }
                            updated_parts = current_parts + [new_part]
                            projects_table.update_item(
                                Key={'projectId': pid},
                                UpdateExpression="SET participants = :p",
                                ExpressionAttributeValues={':p': updated_parts},
                            )
                            proj_item['participants'] = updated_parts
                            print(f"[list_projects] Invited user {inv_email_norm} added to participants of {pid}")
                    except Exception as e:
                        print(f"[list_projects] Could not add invited user to participants of {pid}: {e}")
                    invited_projects.append(proj_item)
                    seen_proj_ids.add(pid)
        except Exception as e:
            print(f"[list_projects] Error querying invitations: {e}")

    projects = own_projects + shared_projects + invited_projects

    # FIX (#23): the previous filter Attr('userId').eq(uid) hid tasks and
    # insights for projects where the user is shared or invited.
    # Persisted tasks/insights carry userId = project owner (not the
    # querying user), so an invited user never matched and saw the correct
    # projects but empty of tasks/insights.
    #
    # Solution: full scan and client-side filter by projectId ∈ accessible
    # projects. Authorization already ran above (own + shared + invited):
    # that set defines exactly what they can see, and applying it here
    # guarantees NO tasks from inaccessible projects sneak through.
    # Cost: same full scan as before; DynamoDB was applying the filter
    # POST-scan internally, so in practice this change is not slower.
    accessible_pids = {p['projectId'] for p in projects}

    all_tasks_raw = scan_all_pages(tasks_table)
    all_tasks = [t for t in all_tasks_raw if t.get('projectId') in accessible_pids]

    all_insights_raw = scan_all_pages(insights_table)
    all_insights = sorted(
        [i for i in all_insights_raw if i.get('projectId') in accessible_pids],
        key=lambda x: x.get('createdAt', ''),
        reverse=True
    )

    today = datetime.utcnow().strftime("%Y-%m-%d")
    enriched = []

    for proj in projects:
        pid = proj['projectId']

        # Tasks for this project
        proj_tasks = [t for t in all_tasks if t.get('projectId') == pid]
        done = len([t for t in proj_tasks if t.get('status') == 'done'])
        pending = len([t for t in proj_tasks if t.get('status') == 'pending'])
        blocked = len([t for t in proj_tasks if t.get('status') == 'blocked'])
        total = len(proj_tasks)

        # =====================================================
        # PROGRESS % CALCULATION WITH CRITICAL BLOCKING LOGIC
        # =====================================================
        # Critical blockers that force progress=0:
        #   - No participants on the project
        #   - More blockers than done tasks (project stuck)
        # If there are blockers but also progress, the % is penalized.
        # If everything is OK, normal calculation.
        participants_list = proj.get('participants', [])
        no_participants = len(participants_list) == 0
        project_stuck = blocked > 0 and blocked >= done  # more blockers than actual progress
        progress_blocked_reason = ''

        if total == 0:
            progress = 0
        elif no_participants and total > 0:
            progress = 0
            progress_blocked_reason = 'No participants assigned'
        elif project_stuck:
            # Project stuck: compute progress but penalize it by half
            progress = max(0, round((done / total) * 100 * 0.5))
            progress_blocked_reason = f'{blocked} blocked task(s) are stopping progress'
        else:
            progress = round((done / total) * 100)

        proj_insights = [i for i in all_insights if i.get('projectId') == pid]

        overdue = [t for t in proj_tasks
                   if t.get('status') == 'pending' and t.get('dueDate', '') and t.get('dueDate', '') < today]
        # Improved SLA logic: also consider lack of participants
        if no_participants and total > 0:
            sla = 'sla_overdue'  # Critical: tasks but no team
        elif blocked >= 2 or len(overdue) >= 2:
            sla = 'sla_overdue'
        elif blocked > 0 or len(overdue) > 0:
            sla = 'at_risk'
        else:
            sla = 'on_track'

        participants = proj.get('participants', [])
        team = []
        for i, p in enumerate(normalize_participants(participants)):
            name = p['name']
            task_count = len([t for t in proj_tasks if t.get('assignedTo', '') == name])
            team.append({
                'name': name or p['email'] or p['phone'],
                'initials': initials(name),
                'role': p['role'] or 'Member',
                'email': p['email'],
                'phone': p['phone'],
                'color': TEAM_COLORS[i % len(TEAM_COLORS)],
                'tasks': task_count,
            })

        raw_channels = proj.get('channels', [])
        channels = []
        for ch in raw_channels:
            ch_name = ch if isinstance(ch, str) else ch.get('name', '')
            channels.append({
                'name': ch_name,
                'icon': channel_icon(ch_name),
                'lastActivity': '',
                'unread': 0,
            })

        last_action = {'detected': '', 'action': ''}
        if proj_insights:
            latest = proj_insights[0]
            actions_txt = ', '.join(latest.get('actionsTaken', []))
            last_action = {
                'detected': latest.get('title', ''),
                'action': latest.get('description', '') or actions_txt,
            }

        ai_actions = []
        for ins in proj_insights[:8]:
            created = ins.get('createdAt', '')
            time_str = created[11:16] if len(created) > 16 else created
            actions_taken = ins.get('actionsTaken', [])
            ai_actions.append({
                'id': ins.get('insightId', ''),
                'detected': ins.get('title', ''),
                'executed': ins.get('description', '') or ', '.join(actions_taken),
                'channel': 'IA',
                'channelIcon': 'email',
                'time': time_str,
            })

        tasks_list = []
        for t in proj_tasks:
            assigned = t.get('assignedTo', '') or 'Unassigned'
            # The task STATUS is its own field (status), NOT a tag.
            # We do not put it in tags to avoid the "Pending: Completed"
            # redundancy (the frontend already paints the state as a badge
            # from status). tags only carries real labels: reminder, overdue.
            is_overdue = bool(
                t.get('dueDate', '') and t.get('dueDate', '') < today
                and t.get('status') == 'pending'
            )
            tags = []
            if t.get('type') == 'reminder':
                tags.append('Reminder')
            if is_overdue:
                tags.append('Overdue')

            # Count child subtasks of this task (if any)
            children = [c for c in proj_tasks if c.get('parentTaskId', '') == t.get('taskId', '')]
            children_done = len([c for c in children if c.get('status') == 'done'])
            tasks_list.append({
                'id': t.get('taskId', ''),
                'text': t.get('text', ''),
                'status': t.get('status', 'pending'),
                'description': t.get('description', ''),
                'overdue': is_overdue,
                'blockedReason': t.get('blockedReason', ''),
                'startDate': t.get('startDate', ''),
                'dueDate': t.get('dueDate', ''),
                'parentTaskId': t.get('parentTaskId', ''),
                'subtasksCount': len(children),
                'subtasksDone': children_done,
                'assignedTo': {
                    'name': assigned,
                    'initials': initials(assigned),
                    'color': 'from-violet-500 to-indigo-600',
                },
                'tags': tags,
            })

        # Is the logged-in user the owner of this project?
        # Used so the frontend can hide owner-only actions
        # (delete, invite, add/remove participants) from invited users.
        is_owner = proj.get('userId') == uid
        # If NOT owner, how did they get in? (sharedByEmail or invited)
        role = 'owner' if is_owner else ('invitedByEmail' if proj.get('_shared') else ('invitedByLink' if proj.get('_invited') else 'collaborator'))

        enriched.append({
            'projectId': pid,
            'name': proj.get('name', ''),
            'client': proj.get('client', ''),
            'description': proj.get('description', ''),
            'status': proj.get('status', 'active'),
            'sla': sla,
            'type': proj.get('type', 'Other'),
            'deliveryDate': proj.get('deliveryDate', ''),
            'timing': proj.get('timing', ''),
            'daysLeft': 0,
            'progress': progress,
            'progressBlockedReason': progress_blocked_reason,
            'done': done,
            'pending': pending,
            'blocked': blocked,
            'aiMessages': len(proj_insights),
            'lastAction': last_action,
            'team': team,
            'channels': channels,
            'labels': [],
            'slaMetrics': {
                'clientResponse': '-',
                'unassignedTasks': 0,
                'partnerResponse': '-',
                'tasksBlocked24h': blocked,
            },
            'startDate': proj.get('createdAt', '')[:10],
            'tasks': tasks_list,
            'aiActions': ai_actions,
            'notifications': [],
            # === Permissions for the frontend ===
            'isOwner': is_owner,
            'role': role,
        })

    # Order: active first, then by name
    enriched.sort(key=lambda p: (0 if p['status'] == 'active' else 1, p['name']))
    return enriched


def get_project_detail(uid: str, project_id: str) -> dict:
    """Get a specific project with all its data."""
    proj_result = projects_table.get_item(Key={'projectId': project_id})
    if 'Item' not in proj_result:
        raise HTTPException(status_code=404, detail="Project not found")
    proj = proj_result['Item']

    tasks_result = tasks_table.scan(
        FilterExpression=Attr('projectId').eq(project_id) & Attr('userId').eq(uid)
    )
    tasks = tasks_result.get('Items', [])

    convs_result = conversations_table.query(
        KeyConditionExpression=Key('projectId').eq(project_id)
    )
    conversations = convs_result.get('Items', [])

    insights_result = insights_table.scan(
        FilterExpression=Attr('userId').eq(uid) & Attr('projectId').eq(project_id)
    )
    insights = sorted(
        insights_result.get('Items', []),
        key=lambda x: x.get('createdAt', ''),
        reverse=True
    )

    notif_result = notifications_table.scan(
        FilterExpression=Attr('userId').eq(uid) & Attr('projectId').eq(project_id)
    )
    notifications = sorted(
        notif_result.get('Items', []),
        key=lambda x: x.get('createdAt', ''),
        reverse=True
    )

    return {
        "project": proj,
        "tasks": tasks,
        "conversations": conversations,
        "insights": insights,
        "notifications": notifications,
    }


def create_project(uid: str, name: str, description: str, project_type: str,
                   channels, participants, timing: str, delivery_date: str) -> dict:
    """Create a new project with AI analysis, notifications and insights.
    Uses the shared `create_project_full` function (also used by the WhatsApp flow)."""
    from agent.project_helpers import create_project_full
    return create_project_full(
        user_id=uid,
        name=name,
        description=description,
        project_type=project_type,
        channels=channels,
        # Normalized at the WRITE boundary too, not only on read: the catalog
        # documents {name, email, role, phone} but nothing enforced it, and
        # what the model happens to emit became the stored schema.
        participants=normalize_participants(participants),
        timing=timing or '',
        delivery_date=delivery_date or ''
    )


def update_project(uid: str, project_id: str, updates: dict) -> dict:
    """Edit allowed fields on a project. Only the owner can edit.

    Valid fields: name, description, type, status, deliveryDate, timing.
    Anything else is ignored (silently, without raising) so we do not break
    the frontend if the schema evolves.
    """
    ALLOWED = {'name', 'description', 'type', 'status', 'deliveryDate', 'timing'}
    filtered = {k: v for k, v in (updates or {}).items() if k in ALLOWED}
    if not filtered:
        raise HTTPException(status_code=400, detail="No valid fields to update")

    # Validate status against the allowed set (avoid junk in DDB).
    if 'status' in filtered and filtered['status'] not in ('active', 'paused', 'finished'):
        raise HTTPException(status_code=400, detail=f"invalid status: {filtered['status']}")

    # Check ownership + existence before trying the update
    proj = projects_table.get_item(Key={'projectId': project_id}).get('Item')
    if not proj:
        raise HTTPException(status_code=404, detail="Project not found")
    if proj.get('userId') != uid:
        raise HTTPException(status_code=403, detail="Only the owner can edit this project")

    # Build UpdateExpression dynamically. Use ExpressionAttributeNames because
    # `name`, `status`, `type` are reserved words in DDB.
    set_parts = []
    ean = {}
    eav = {}
    for i, (k, v) in enumerate(filtered.items()):
        ph = f":v{i}"
        nm = f"#k{i}"
        set_parts.append(f"{nm} = {ph}")
        ean[nm] = k
        eav[ph] = v
    projects_table.update_item(
        Key={'projectId': project_id},
        UpdateExpression="SET " + ", ".join(set_parts),
        ExpressionAttributeNames=ean,
        ExpressionAttributeValues=eav,
    )
    return {"success": True, "projectId": project_id, "updated": list(filtered.keys())}


def update_participants(uid: str, project_id: str, participants: list) -> dict:
    """Update a project's participants (includes phones)."""
    projects_table.update_item(
        Key={'projectId': project_id},
        UpdateExpression="SET participants = :p",
        ExpressionAttributeValues={':p': normalize_participants(participants)},
        ConditionExpression=Attr('userId').eq(uid)
    )
    return {"success": True, "projectId": project_id}


def invite_user(uid: str, project_id: str, email: str = '', phone: str = '',
                name: str = '', role: str = '', send_notification: bool = True) -> dict:
    """Add a person to the project team. Accepts email and/or phone.

    - If email + send_notification=True: creates a user in Cognito (sends
      email with temporary password) and stores a pending invitation.
    - If phone + send_notification=True: sends a WhatsApp message via
      Twilio notifying that they were added to the project.
    - In every case: records the contact in the project's participants[].
    - If send_notification=False: only records the contact, without notifying.

    Rules: requires at least email OR phone. If only a phone is provided, does
    NOT create a Cognito account (we cannot authenticate someone with just a number).
    """
    from agent.tools import send_notification, set_current_user, _normalize_e164

    email = (email or '').strip().lower()
    phone_raw = (phone or '').strip()
    name = (name or '').strip()
    role = (role or 'Invited').strip()
    send_notification = send_notification if send_notification is not None else True

    # Require at least one channel
    if not email and not phone_raw:
        raise HTTPException(status_code=400, detail="Email or phone required")
    if email and '@' not in email:
        raise HTTPException(status_code=400, detail="Invalid email")

    # Validate phone if provided
    phone = ''
    if phone_raw:
        phone = _normalize_e164(phone_raw) or ''
        if not phone:
            raise HTTPException(status_code=400, detail=f"Invalid phone: '{phone_raw}'. Use E.164 format (+34600000000)")

    # Check that the project belongs to the user
    proj = projects_table.get_item(Key={'projectId': project_id}).get('Item')
    if not proj:
        raise HTTPException(status_code=404, detail="Project not found")
    if proj.get('userId') != uid:
        raise HTTPException(status_code=403, detail="You do not have permission on this project")

    # 1) ADD/UPDATE THE PROJECT'S participants[]
    # ────────────────────────────────────────────────────
    # Match by email if provided, otherwise by phone. If it already exists, update.
    # If not, add it.
    current_parts = proj.get('participants', []) or []
    existing_idx = None
    for i, p in enumerate(current_parts):
        if not isinstance(p, dict):
            continue
        p_email = (p.get('email', '') or '').strip().lower()
        p_phone = (p.get('phone', '') or '').strip()
        if email and p_email == email:
            existing_idx = i
            break
        if phone and p_phone == phone:
            existing_idx = i
            break
    derived_name = name or (email.split('@')[0] if email else phone)
    new_part = {
        'name': derived_name,
        'email': email,
        'phone': phone,
        'role': role,
    }
    if existing_idx is not None:
        # Merge: do not overwrite non-empty fields with empty ones
        current = current_parts[existing_idx]
        for k, v in new_part.items():
            if v:
                current[k] = v
        current_parts[existing_idx] = current
    else:
        current_parts.append(new_part)
    try:
        projects_table.update_item(
            Key={'projectId': project_id},
            UpdateExpression="SET participants = :p",
            ExpressionAttributeValues={':p': current_parts},
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error saving participant: {str(e)}")

    # If we should NOT notify, we finish here (only the contact was recorded)
    if not send_notification:
        return {
            "success": True,
            "saved": True,
            "notified": False,
            "message": "Contact added to the project without notification.",
        }

    # 2) SEND NOTIFICATIONS (email and/or WhatsApp)
    # ───────────────────────────────────────────────
    notify_email_result = None
    notify_whatsapp_result = None

    # 2a) Email: create user in Cognito (if not present) and store the invitation
    if email:
        notify_email_result = _invite_by_email(
            email=email, uid=uid, project_id=project_id,
            project_name=proj.get('name', ''),
        )

    # 2b) WhatsApp: notification only, does NOT create an account.
    # The message changes depending on whether an email was also sent:
    #  - With email: tell them to check their email for the access link.
    #  - Without email: just an FYI notification; without email they cannot enter the web.
    if phone:
        # We need a user context so the tool can record the notif
        set_current_user(uid, "")
        project_name = proj.get('name', 'your project')
        email_was_sent = bool(
            email
            and notify_email_result
            and notify_email_result.get('success') is not False
        )
        if email_was_sent:
            message = (
                f"Hi {derived_name}, you were added to the *{project_name}* project on OneBox. "
                f"We sent an email to {email} with a link to sign up and access the platform. "
                f"Once you're in we'll keep you posted here about blocked tasks and important updates."
            )
        else:
            message = (
                f"Hi {derived_name}, you were added to the *{project_name}* project on OneBox. "
                f"We'll notify you here about blocked tasks and important project updates."
            )
        notify_whatsapp_result = send_notification(
            recipient=phone,
            message=message,
            channel='whatsapp',
            project_id=project_id,
            project_name=project_name,
        )

    return {
        "success": True,
        "saved": True,
        "notified": True,
        "email": notify_email_result,
        "whatsapp": notify_whatsapp_result,
    }


def _invite_by_email(email: str, uid: str, project_id: str, project_name: str) -> dict:
    """Helper: email invitation logic (silent Cognito + custom email via SES).

    Design:
      - Cognito is used ONLY to pre-create the identity (so the pre-signup
        trigger can auto-link when the person logs in via Google).
        The email with the temporary password is NOT sent — most invited
        users enter via Google and that password confuses them.
      - The only email the person receives is a custom one via SES with
        the link to "Sign in with Google" into the app.

    Returns a dict instead of raising so the endpoint can combine email +
    WhatsApp in a single response.
    """
    from agent.tools import send_notification, set_current_user

    cognito_client = boto3.client('cognito-idp', region_name='us-east-1')
    pool_id = os.environ.get('COGNITO_USER_POOL_ID', 'us-east-1_b76prubhx')
    cognito_user_existed = False
    user_status = None
    try:
        # SUPPRESS: creates the user in Cognito but sends NO email.
        # Without this, Cognito sends the temporary password to everyone even
        # if they will sign in via Google.
        cognito_client.admin_create_user(
            UserPoolId=pool_id,
            Username=email,
            UserAttributes=[
                {'Name': 'email', 'Value': email},
                {'Name': 'email_verified', 'Value': 'true'},
            ],
            MessageAction='SUPPRESS',
        )
    except cognito_client.exceptions.UsernameExistsException:
        cognito_user_existed = True
        try:
            u = cognito_client.admin_get_user(UserPoolId=pool_id, Username=email)
            user_status = u.get('UserStatus')
        except Exception as inner:
            print(f"[invite] admin_get_user failed for {email}: {inner}")
    except Exception as e:
        return {"success": False, "error": f"Cognito error: {str(e)}"}

    # Save the invitation (pending). If already accepted, do not duplicate.
    now = datetime.utcnow().isoformat()
    invitation_id = str(uuid.uuid4())
    try:
        invitations_table.put_item(Item={
            'invitationId': invitation_id,
            'email': email,
            'projectId': project_id,
            'projectName': project_name,
            'invitedBy': uid,
            'status': 'pending',
            'createdAt': now,
        })
    except Exception as e:
        return {"success": False, "error": f"DynamoDB error: {str(e)}"}

    # Send the custom email with the link — same personal tone for every
    # case (new, existing, Google, etc.). On login, the /api/projects
    # endpoint auto-accepts the invitation by email.
    share_url = f"https://www.oneboxmanager.com/?project={project_id}"
    message = (
        f"Hi,\n\n"
        f"You were added to the \"{project_name}\" project on OneBox.\n\n"
        f"To get in, sign in with Google (or with your email if you already had an account) "
        f"from this link:\n{share_url}\n\n"
        f"Once you're in, the project shows up automatically in your list.\n\n"
        f"— OneBox"
    )
    try:
        # send_notification records the notif in the table and calls SES.
        # Requires the current user to be set (for multi-tenant logs).
        set_current_user(uid, "")
        send_notification(
            email, message, channel='email',
            project_id=project_id, project_name=project_name,
        )
        print(f"[invite] custom email sent to {email} for {project_id}")
    except Exception as e:
        print(f"[invite] failed to send custom email to {email}: {e}")

    if not cognito_user_existed:
        msg = "Invitation sent. The person will receive an email with the link to enter."
    elif user_status == 'FORCE_CHANGE_PASSWORD':
        msg = "The person already had a pending account. We resent the link."
    elif user_status == 'CONFIRMED':
        msg = "The person already has an active account. We sent them the project link."
    else:
        msg = "The person is already registered. We sent them the project link."

    return {
        "success": True,
        "invitationId": invitation_id,
        "email": email,
        "cognitoUserExisted": cognito_user_existed,
        "emailResent": False,
        "userStatus": user_status,
        "message": msg,
        # share_url: direct link to the project. The backend always sends its
        # own email with this link, but the frontend returns it in case the
        # owner wants to copy it and share via another channel.
        "share_url": share_url,
        "needs_manual_share": False,
    }


def remove_participant(uid: str, project_id: str, email: str = '', phone: str = '', name: str = '') -> dict:
    """Remove a participant from the project team.

    Behavior (product decisions):
      1. Takes them out of the project's participants[].
      2. Clears assignedTo on ALL project tasks assigned to that person
         → they become 'Unassigned' (they are not deleted).
      3. If there was an invitation in onebox-invitations for their email
         on this project, delete it → they lose access if they were invited.
      4. Does NOT send a notification to the removed person.

    Only the project owner can remove participants (RBAC).
    """
    proj = projects_table.get_item(Key={'projectId': project_id}).get('Item')
    if not proj:
        raise HTTPException(status_code=404, detail="Project not found")
    if proj.get('userId') != uid:
        raise HTTPException(status_code=403, detail="Only the owner can remove participants")

    # Look up the participant by email > phone > name
    target_email = (email or '').strip().lower()
    target_phone = (phone or '').strip()
    target_name = (name or '').strip()
    if not (target_email or target_phone or target_name):
        raise HTTPException(status_code=400, detail="Provide email, phone or name of the participant to remove")

    parts = proj.get('participants', []) or []
    target_idx = None
    for i, p in enumerate(parts):
        if not isinstance(p, dict):
            continue
        p_email = (p.get('email', '') or '').strip().lower()
        p_phone = (p.get('phone', '') or '').strip()
        p_name = (p.get('name', '') or '').strip()
        if target_email and p_email == target_email:
            target_idx = i
            break
        if target_phone and p_phone == target_phone:
            target_idx = i
            break
        if target_name and p_name == target_name:
            target_idx = i
            break

    if target_idx is None:
        raise HTTPException(status_code=404, detail="Participant not found")

    # 1) Remove from participants[]
    removed = parts.pop(target_idx)
    try:
        projects_table.update_item(
            Key={'projectId': project_id},
            UpdateExpression="SET participants = :p",
            ExpressionAttributeValues={':p': parts},
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error updating project: {str(e)}")

    # 2) Clear assignedTo on their tasks in this project
    tasks_orphaned = 0
    removed_name = (removed.get('name', '') or '').strip()
    if removed_name:
        try:
            tres = tasks_table.scan(
                FilterExpression=Attr('projectId').eq(project_id) & Attr('assignedTo').eq(removed_name)
            )
            for t in tres.get('Items', []):
                tasks_table.update_item(
                    Key={'projectId': t['projectId'], 'taskId': t['taskId']},
                    UpdateExpression="SET assignedTo = :empty",
                    ExpressionAttributeValues={':empty': ''},
                )
                tasks_orphaned += 1
        except Exception as e:
            print(f"[remove_participant] error clearing assignedTo on tasks: {e}")

    # 3) Delete the removed user's invitations for this project (loses access)
    invitations_revoked = 0
    removed_email = (removed.get('email', '') or '').strip().lower()
    if removed_email:
        try:
            inv_resp = invitations_table.query(
                IndexName='email-index',
                KeyConditionExpression=Key('email').eq(removed_email),
            )
            for inv in inv_resp.get('Items', []):
                if inv.get('projectId') == project_id:
                    invitations_table.delete_item(Key={'invitationId': inv['invitationId']})
                    invitations_revoked += 1
        except Exception as e:
            print(f"[remove_participant] error deleting invitations: {e}")

    return {
        "success": True,
        "removed": {
            "name": removed.get('name', ''),
            "email": removed_email,
            "phone": removed.get('phone', ''),
        },
        "tasksOrphaned": tasks_orphaned,
        "invitationsRevoked": invitations_revoked,
    }


def delete_project(uid: str, project_id: str) -> dict:
    """Delete a project and its related data (insights, notifications, tasks)."""
    # Check that the project belongs to the user
    existing = projects_table.get_item(Key={'projectId': project_id}).get('Item')
    if not existing:
        raise HTTPException(status_code=404, detail="Project not found")
    if existing.get('userId') != uid:
        raise HTTPException(status_code=403, detail="You do not have permission to delete this project")

    project_name = existing.get('name', 'Project')

    # Delete the project
    projects_table.delete_item(Key={'projectId': project_id})
    print(f"[delete_project] Project {project_id} deleted")

    # Delete associated insights
    try:
        insights_to_delete = insights_table.scan(
            FilterExpression=Attr('userId').eq(uid) & Attr('projectId').eq(project_id),
            ProjectionExpression='userId,insightId'
        ).get('Items', [])
        for ins in insights_to_delete:
            insights_table.delete_item(Key={'userId': ins['userId'], 'insightId': ins['insightId']})
        print(f"[delete_project] {len(insights_to_delete)} insights deleted")
    except Exception as e:
        print(f"[delete_project] Error deleting insights: {e}")

    # Delete associated notifications
    try:
        notifs_to_delete = notifications_table.query(
            KeyConditionExpression=Key('userId').eq(uid),
            FilterExpression=Attr('projectId').eq(project_id),
            ProjectionExpression='userId,notificationId'
        ).get('Items', [])
        for n in notifs_to_delete:
            notifications_table.delete_item(Key={'userId': n['userId'], 'notificationId': n['notificationId']})
        print(f"[delete_project] {len(notifs_to_delete)} notifications deleted")
    except Exception as e:
        print(f"[delete_project] Error deleting notifications: {e}")

    # Delete associated tasks
    try:
        tasks_to_delete = tasks_table.query(
            KeyConditionExpression=Key('projectId').eq(project_id),
            ProjectionExpression='projectId,taskId'
        ).get('Items', [])
        for t in tasks_to_delete:
            tasks_table.delete_item(Key={'projectId': t['projectId'], 'taskId': t['taskId']})
        print(f"[delete_project] {len(tasks_to_delete)} tasks deleted")
    except Exception as e:
        print(f"[delete_project] Error deleting tasks: {e}")

    return {"success": True, "projectId": project_id, "name": project_name}
