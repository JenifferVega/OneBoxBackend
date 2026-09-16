"""Tasks internal logic: list, create, update (with block and assignment
notifications) and delete with subtasks."""
import threading
import uuid
from datetime import datetime

from boto3.dynamodb.conditions import Attr
from fastapi import HTTPException

from agent.tools import projects_table, tasks_table
from api.services.access import has_project_access


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


def create_task(uid: str, user_email: str, project_id: str, req) -> dict:
    """Create a task in a project. Accessible to owner AND invited users.
    The task is created with userId = project owner (not creator) so that
    EVERYONE with project access can edit it. createdBy is stored."""
    has, _is_owner, proj = has_project_access(uid, user_email, project_id)
    if not has:
        raise HTTPException(status_code=403, detail="No access to this project")
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

    # Immediately notify the person if their assignment changed (from empty
    # or from another person to THEM). If only something else is edited
    # (text, date) and assigned_to already pointed to the same person, do
    # NOT send again.
    old_assigned = (task.get('assignedTo', '') or '').strip()
    new_assigned = updates.get('assignedTo')
    if (
        new_assigned is not None
        and new_assigned.strip()                              # assigns to someone
        and new_assigned.strip().lower() != old_assigned.lower()  # and changed
    ):
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
    old_status = task.get('status', '')
    new_status = updates.get('status')
    if new_status == 'blocked' and old_status != 'blocked':
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

    return {"success": True, "taskId": task_id}


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
    return {"success": True, "taskId": task_id, "childrenAffected": len(children), "cascade": cascade}
