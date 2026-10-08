"""Tasks internal logic: list, create, update (with block and assignment
notifications) and delete with subtasks."""
import threading
import uuid
from datetime import datetime

from boto3.dynamodb.conditions import Attr
from fastapi import HTTPException

from agent.tools import projects_table, tasks_table
from api.services.access import has_project_access


def notification_effects(task_before: dict, assigned_to=None, status=None) -> list:
    """Which notifications an update would send, given the task as it is NOW.

    The single source of truth for "does this update reach a person?". Used
    here by update_task to decide what to send, and by the agent's executor to
    decide whether the change needs the user's explicit consent first. One
    rule in one place: if the two ever disagreed, the agent would either ask
    for nothing or send without asking.

      "assignment" -> the new assignee is notified (email/WhatsApp). Only when
                      the assignee actually CHANGES: re-saving the same person
                      sends nothing.
      "blocked"    -> EVERY participant of the project is notified. Only when
                      the task MOVES to blocked: it was not blocked before.
    """
    task_before = task_before or {}
    effects = []
    old_assigned = (task_before.get('assignedTo', '') or '').strip()
    if (assigned_to is not None
            and str(assigned_to).strip()
            and str(assigned_to).strip().lower() != old_assigned.lower()):
        effects.append('assignment')
    if status == 'blocked' and (task_before.get('status', '') or '') != 'blocked':
        effects.append('blocked')
    return effects


def _notify_assignment_async(project_id: str, task_text: str, assigned_to_name: str,
                              due_date: str = "", actor_uid: str = "",
                              actor_email: str = "") -> None:
    """Fire the notification to the newly assigned person in background.

    Finds the participant whose `name` (or `email`) matches assigned_to_name
    and sends email AND/OR WhatsApp depending on the channels they have.

    Why a thread: it must not block the endpoint response. If SES or Twilio
    are slow or fail, the user who created/edited the task should not have
    to wait.

    IMPORTANT — contextvars and threads:
      The agent tools (send_notification, _log_notification) read
      _current_uid() from a contextvar. Python threads do NOT inherit
      contextvars automatically, so inside the thread we must re-set the
      user context (actor_uid/actor_email) or the calls fail with
      "Agent tool invoked without user context".
    """
    def _worker():
        # Re-establish the user context in this thread (Python does not
        # propagate contextvars to threading.Thread). If not called,
        # _current_uid() raises RuntimeError and the notification silently fails.
        from agent.tools import set_current_user, clear_current_user
        if actor_uid:
            set_current_user(actor_uid, actor_email or '')
        try:
            from agent.tools import send_notification
            proj = projects_table.get_item(Key={'projectId': project_id}).get('Item') or {}
            if not proj:
                return
            proj_name = proj.get('name', '')
            target_lower = (assigned_to_name or '').strip().lower()
            if not target_lower:
                return

            # Find the participant: match by exact name or email.
            target_part = None
            for part in proj.get('participants', []) or []:
                if not isinstance(part, dict):
                    continue
                p_name = (part.get('name', '') or '').strip().lower()
                p_email = (part.get('email', '') or '').strip().lower()
                if p_name == target_lower or (p_email and p_email == target_lower):
                    target_part = part
                    break
            if not target_part:
                # If they are not a participant, we cannot send anything.
                # (In the future we might also look up in Cognito, but today
                # we only notify participants registered on the project.)
                print(f"[task assignment] '{assigned_to_name}' is not a participant of {project_id}, skip")
                return

            email = (target_part.get('email', '') or '').strip().lower()
            tel = (target_part.get('phone') or target_part.get('phone') or '').strip()

            # Concise message (same for email and WhatsApp).
            lines = [
                f"📌 *{proj_name}* — New task assigned to you",
                "",
                f"Hi {target_part.get('name', '')}, you have been assigned a task:",
                "",
                f"  • {task_text}",
            ]
            if due_date:
                lines.append(f"  📅 Due: {due_date}")
            lines.append("")
            lines.append("Open OneBox to see it: https://www.oneboxmanager.com")
            msg = "\n".join(lines)

            sent_count = 0
            if email:
                res = send_notification(email, msg, channel='email',
                                          project_id=project_id, project_name=proj_name)
                if res.get('success') or res.get('status') == 'skipped_unverified':
                    sent_count += 1
            if tel:
                res = send_notification(tel, msg, channel='whatsapp',
                                          project_id=project_id, project_name=proj_name)
                if res.get('success'):
                    sent_count += 1
            print(f"[task assignment] notif to '{assigned_to_name}' → "
                  f"{sent_count} channel(s) (email={bool(email)}, tel={bool(tel)})")
        except Exception as e:
            print(f"[task assignment] error notifying: {e}")
        finally:
            # Clear the thread's context for hygiene (thread contextvars
            # could persist if the thread pool reused it).
            try:
                clear_current_user()
            except Exception:
                pass

    threading.Thread(target=_worker, daemon=True).start()


def list_tasks(uid: str, user_email: str, project_id: str) -> list:
    """List a project's tasks. Accessible to owner AND invited users."""
    has, _is_owner, _proj = has_project_access(uid, user_email, project_id)
    if not has:
        raise HTTPException(status_code=403, detail="No access to this project")
    # Project tasks (all of them, no userId filter — they belong to the project)
    result = tasks_table.scan(
        FilterExpression=Attr('projectId').eq(project_id)
    )
    return sorted(
        result.get('Items', []),
        key=lambda x: x.get('createdAt', ''),
        reverse=True
    )


# ── What a member (not the owner) may do ────────────────────────────────────
# A member does the work and reports on it; the owner decides the plan.
#   member:  status / blocked reason / comments, on tasks assigned to THEM,
#            and a status change always says why.
#   owner:   everything else -- what exists, who does it, and when. A member
#            who wants one of those asks for it (planning chat or check-in)
#            and it becomes a proposal the owner approves.
_MEMBER_FIELDS = ('status', 'blocked_reason', 'comment')
_PLAN_FIELDS = ('text', 'assigned_to', 'description', 'start_date', 'due_date',
                'parent_task_id')
_MIN_COMMENT = 3


def _is_assigned_to(proj: dict, task: dict, user_email: str) -> bool:
    """Is this task assigned to the person behind this email? Same matching
    the check-in uses: assignedTo is a free-text name, not an account."""
    from agent.planning.sources import match_person, normalize_participant
    email = (user_email or '').strip().lower()
    who = (task.get('assignedTo') or '').strip()
    if not email or not who:
        return False
    if who.lower() == email:
        return True
    parts = [normalize_participant(p) for p in (proj or {}).get('participants') or []]
    me = next((p for p in parts if (p.get('email') or '').lower() == email), None)
    if not me:
        return False
    hit = match_person(who, parts)
    return bool(hit) and (hit.get('email') or '').lower() == email


def _owner_only(what: str):
    raise HTTPException(
        status_code=403,
        detail=f"Only the project owner can {what}. Ask for it in the planning "
               f"chat or in your status update: it becomes a proposal the "
               f"owner approves.")


def create_task(uid: str, user_email: str, project_id: str, req) -> dict:
    """Create a task in a project. Accessible to owner AND invited users.
    The task is created with userId = project owner (not creator) so that
    EVERYONE with project access can edit it. createdBy is stored."""
    has, _is_owner, proj = has_project_access(uid, user_email, project_id)
    if not has:
        raise HTTPException(status_code=403, detail="No access to this project")
    if not _is_owner:
        _owner_only("create tasks")
    task_id = str(uuid.uuid4())
    now = datetime.utcnow().isoformat()
    owner_uid = proj.get('userId', uid)
    item = {
        'projectId': project_id,
        'taskId': task_id,
        'userId': owner_uid,           # task belongs to the project, not the creator
        'createdBy': uid,              # who created it (owner or invited user)
        'createdByEmail': (user_email or '').strip().lower(),
        'text': req.text,
        'description': req.description,
        'status': req.status,
        'assignedTo': req.assigned_to,
        'startDate': req.start_date or '',
        'dueDate': req.due_date or '',
        'parentTaskId': (req.parent_task_id or '').strip(),  # '' if root task
        'createdAt': now,
    }
    tasks_table.put_item(Item=item)

    # If the task was created WITH someone assigned, notify them immediately.
    # We pass the actor uid/email: the thread needs to re-set the user
    # contextvar for the agent tools to work.
    if (req.assigned_to or '').strip():
        _notify_assignment_async(
            project_id=project_id,
            task_text=req.text or '',
            assigned_to_name=req.assigned_to,
            due_date=req.due_date or '',
            actor_uid=uid,
            actor_email=user_email or '',
        )

    # The project's board follows, with the OWNER's Trello connection.
    from api.services import trello_sync
    trello_sync.sync_async(project_id, [task_id], created=True)

    return {"success": True, "taskId": task_id, "text": req.text}


def update_task(uid: str, user_email: str, task_id: str, req) -> dict:
    """Update a task. Accessible to owner AND invited users with access to the project."""
    # Look the task up by taskId only; then verify project access.
    result = tasks_table.scan(
        FilterExpression=Attr('taskId').eq(task_id)
    )
    items = result.get('Items', [])
    if not items:
        raise HTTPException(status_code=404, detail="Task not found")

    task = items[0]
    has, _is_owner, _proj = has_project_access(uid, user_email, task['projectId'])
    if not has:
        raise HTTPException(status_code=403, detail="No access to this project")
    comment = (getattr(req, 'comment', None) or '').strip()
    if not _is_owner:
        if not _is_assigned_to(_proj, task, user_email):
            _owner_only("change a task that is not assigned to you")
        if any(getattr(req, f, None) is not None for f in _PLAN_FIELDS):
            _owner_only("change a task's title, description, dates or assignee")
        new_status = 'done' if req.status == 'completed' else req.status
        if new_status is not None and new_status != task.get('status'):
            # Blocking already carries its reason; that IS the comment.
            comment = comment or (req.blocked_reason or '').strip()
            if len(comment) < _MIN_COMMENT:
                raise HTTPException(
                    status_code=400,
                    detail="A comment is required to change the status: say "
                           "briefly what happened or why.")
    updates = {}
    if req.text is not None:
        updates['text'] = req.text
    if req.status is not None:
        # Normalize 'completed' (legacy) → 'done' for consistency
        normalized_status = 'done' if req.status == 'completed' else req.status
        updates['status'] = normalized_status
    if req.assigned_to is not None:
        updates['assignedTo'] = req.assigned_to
    if req.description is not None:
        updates['description'] = req.description
    if req.blocked_reason is not None:
        updates['blockedReason'] = req.blocked_reason
    if req.start_date is not None:
        updates['startDate'] = req.start_date
    if req.due_date is not None:
        updates['dueDate'] = req.due_date
    if req.parent_task_id is not None:
        # '' = move to root; a value = make it a subtask of that task.
        # We do not allow a task to be a subtask of itself.
        if req.parent_task_id and req.parent_task_id == task_id:
            raise HTTPException(status_code=400, detail="A task cannot be a subtask of itself")
        updates['parentTaskId'] = req.parent_task_id.strip()

    if updates:
        expr_parts = []
        expr_values = {}
        expr_names = {}
        for i, (k, v) in enumerate(updates.items()):
            expr_parts.append(f"#k{i} = :v{i}")
            expr_values[f":v{i}"] = v
            expr_names[f"#k{i}"] = k

        tasks_table.update_item(
            Key={'projectId': task['projectId'], 'taskId': task_id},
            UpdateExpression="SET " + ", ".join(expr_parts),
            ExpressionAttributeValues=expr_values,
            ExpressionAttributeNames=expr_names,
        )

    # The comment is kept ON the task, with who said it and which status
    # change it explains: a status with no reason tells the owner nothing.
    note = ''
    if comment:
        entry = {
            'at': datetime.utcnow().isoformat(),
            'by': (user_email or '').strip().lower() or uid,
            'text': comment[:2000],
            'fromStatus': task.get('status', ''),
            'toStatus': updates.get('status', task.get('status', '')),
        }
        tasks_table.update_item(
            Key={'projectId': task['projectId'], 'taskId': task_id},
            UpdateExpression="SET comments = list_append(if_not_exists(comments, :e), :c)",
            ExpressionAttributeValues={':e': [], ':c': [entry]},
        )
        change = (f" ({entry['fromStatus']} -> {entry['toStatus']})"
                  if entry['fromStatus'] != entry['toStatus'] else '')
        note = f"{entry['by']}{change}: {entry['text']}"

    if updates or comment:
        # The card follows the task (owner's Trello connection), whoever
        # edited it. `task` is the state BEFORE this change.
        from api.services import trello_sync
        trello_sync.sync_async(task['projectId'], [task_id], before=[task], comment=note)

    # Immediately notify the person if their assignment changed (from empty
    # or from another person to THEM). If only something else is edited
    # (text, date) and assigned_to already pointed to the same person, do
    # NOT send again.
    effects = notification_effects(task, updates.get('assignedTo'), updates.get('status'))
    new_assigned = updates.get('assignedTo')
    if 'assignment' in effects:
        _notify_assignment_async(
            project_id=task['projectId'],
            task_text=updates.get('text', task.get('text', '')),
            assigned_to_name=new_assigned,
            due_date=updates.get('dueDate', task.get('dueDate', '')),
            actor_uid=uid,
            actor_email=user_email or '',
        )

    # If the task JUST moved to 'blocked', notify via WhatsApp the
    # participants with a phone (in a thread, so we do not block the response).
    if 'blocked' in effects:
        proj_id = task['projectId']  # threading already imported above
        task_text = updates.get('text', task.get('text', ''))
        reason = (updates.get('blockedReason') or task.get('blockedReason') or '').strip()
        # Capture context for the thread (contextvars do not propagate
        # automatically — same fix as in _notify_assignment_async).
        thread_uid = uid
        thread_email = user_email or ''

        def _notify_blocked():
            from agent.tools import send_notification, set_current_user, clear_current_user
            if thread_uid:
                set_current_user(thread_uid, thread_email)
            try:
                proj = projects_table.get_item(Key={'projectId': proj_id}).get('Item') or {}
                if not proj:
                    return
                # Access was already validated before reaching here; the
                # invited user can also trigger the block notification.
                # Notify via BOTH channels: WhatsApp and email.
                proj_name = proj.get('name', '')
                msg = (f"🔴 OneBox: task \"{task_text}\" in project "
                       f"\"{proj_name}\" is BLOCKED and needs attention.")
                if reason:
                    msg += f"\nReason: {reason}"
                sent = 0
                for part in proj.get('participants', []):
                    tel = (part.get('phone') or part.get('phone') or '').strip()
                    email = (part.get('email', '') or '').strip().lower()
                    if tel:
                        res = send_notification(tel, msg, channel='whatsapp',
                                                  project_id=proj_id, project_name=proj_name)
                        if res.get('success'):
                            sent += 1
                    if email:
                        res = send_notification(email, msg, channel='email',
                                                  project_id=proj_id, project_name=proj_name)
                        if res.get('success'):
                            sent += 1
                print(f"[update_task] Task blocked → {sent} notification(s) sent")
            except Exception as e:
                print(f"[update_task] Error notifying block: {e}")
            finally:
                try:
                    clear_current_user()
                except Exception:
                    pass
        threading.Thread(target=_notify_blocked, daemon=True).start()

    return {"success": True, "taskId": task_id,
            # The board follows on its own; nobody has to export again.
            **({"trelloSync": "automatic"} if (_proj or {}).get("trelloBoardId") else {})}


def delete_task(uid: str, user_email: str, task_id: str, cascade: bool) -> dict:
    """Delete a task. Owner AND invited users with access to the project can delete.

    If the task has subtasks:
      - cascade=true  → also delete all children.
      - cascade=false → children become root tasks (parentTaskId = '').
    """
    # Locate the task by taskId
    result = tasks_table.scan(FilterExpression=Attr('taskId').eq(task_id))
    items = result.get('Items', [])
    if not items:
        raise HTTPException(status_code=404, detail="Task not found")
    task = items[0]
    project_id = task['projectId']
    has, _is_owner, _proj = has_project_access(uid, user_email, project_id)
    if not has:
        raise HTTPException(status_code=403, detail="No access to this project")
    if not _is_owner:
        _owner_only("delete tasks")

    # Look up subtasks
    children_resp = tasks_table.scan(
        FilterExpression=Attr('projectId').eq(project_id) & Attr('parentTaskId').eq(task_id)
    )
    children = children_resp.get('Items', [])

    if children:
        if cascade:
            # Delete all children too.
            for child in children:
                tasks_table.delete_item(Key={'projectId': project_id, 'taskId': child['taskId']})
        else:
            # Promote children to root tasks.
            for child in children:
                tasks_table.update_item(
                    Key={'projectId': project_id, 'taskId': child['taskId']},
                    UpdateExpression="SET parentTaskId = :p",
                    ExpressionAttributeValues={':p': ''},
                )

    tasks_table.delete_item(Key={'projectId': project_id, 'taskId': task_id})
    # Archive the cards of what was deleted (restorable in Trello).
    gone = [task] + (list(children) if cascade else [])
    from api.services import trello_sync
    trello_sync.sync_async(project_id, [t['taskId'] for t in gone], before=gone)
    return {"success": True, "taskId": task_id, "childrenAffected": len(children), "cascade": cascade}
