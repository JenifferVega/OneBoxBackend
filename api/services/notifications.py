"""Internal logic for scheduled notifications: review pending items
and send WhatsApp AND email summaries. Designed for a daily EventBridge cron.

Design:
  - For each project with pending tasks, build ONE summary.
  - For each participant:
      · if they have an email   → send_notification(email, message, channel='email')
      · if they have a phone    → send_notification(phone, message, channel='whatsapp')
  - send_notification already handles the SES sandbox-check and records into
    the onebox-notifications table. If the participant has no channel, we simply skip.

dispatch_pending_notifications():
  - Scans onebox-notifications for status='pending'.
  - One-time notifications (scheduledAt <= now): sends and marks as 'sent'.
  - Recurring notifications (isRecurring=True): checks whether today is one
    of the recurringDays; if so, sends but keeps status='pending' (so it runs
    again next week). EventBridge fires this endpoint every hour.
"""
from boto3.dynamodb.conditions import Attr


def send_scheduled_notifications() -> dict:
    """Daily cron: builds a summary of pending tasks per project and sends it
    to each participant via email AND/OR WhatsApp depending on the channels they have.

    "Pending" = status pending, in_progress or blocked (anything not yet done).
    This is the information the user needs to see about their team.
    """
    from agent.tools import (
        send_notification, projects_table, tasks_table, set_current_user,
        clear_current_user,
    )

    # Single scan of projects and tasks — more efficient than calling
    # get_project_contacts per project (which internally scans again).
    try:
        proj_scan = projects_table.scan()
        all_projects = proj_scan.get('Items', [])
    except Exception as e:
        return {"success": False, "error": f"scan projects: {e}"}

    try:
        # Only non-completed tasks. status=done or completed → excluded.
        task_scan = tasks_table.scan(
            FilterExpression=Attr('status').ne('done') & Attr('status').ne('completed')
        )
        all_open_tasks = task_scan.get('Items', [])
    except Exception as e:
        return {"success": False, "error": f"scan tasks: {e}"}

    # Index tasks by projectId
    tasks_by_pid: dict = {}
    for t in all_open_tasks:
        pid = t.get('projectId', '')
        if pid:
            tasks_by_pid.setdefault(pid, []).append(t)

    notifications_sent = 0
    notifications_skipped = 0
    errors: list = []
    projects_processed = 0

    for proj in all_projects:
        pid = proj.get('projectId', '')
        if not pid:
            continue
        proj_tasks = tasks_by_pid.get(pid, [])
        if not proj_tasks:
            # Project without pending tasks → nothing to notify
            continue

        project_name = proj.get('name', 'Project')
        owner_uid = proj.get('userId', '')
        if not owner_uid:
            continue

        # The agent's tools require a user context (multi-tenant).
        # We set the project owner: they are who "sends" from the system.
        set_current_user(owner_uid, '')
        try:
            participants = proj.get('participants', []) or []
            if not participants:
                continue

            # FIX: only notify participants that have tasks ASSIGNED to them.
            # Previously we sent to everyone even if tasks were unassigned →
            # noise on projects with unassigned tasks.
            #
            # Match: task.assignedTo == participant.name (or email). Tasks
            # without assignedTo are not counted for anyone, so no one gets
            # email/WhatsApp for those. The owner can still see them by
            # opening the app.
            project_processed_at_least_one = False
            for part in participants:
                if not isinstance(part, dict):
                    continue
                name = (part.get('name', '') or '').strip().lower()
                email = (part.get('email', '') or '').strip().lower()
                tel = (part.get('phone', '') or '').strip()

                # Filter the tasks assigned specifically to this participant.
                # Match by name OR by email (either works).
                his_tasks = []
                for t in proj_tasks:
                    assigned = (t.get('assignedTo', '') or '').strip().lower()
                    if not assigned:
                        continue
                    if assigned == name or (email and assigned == email):
                        his_tasks.append(t)

                if not his_tasks:
                    # SKIP: this participant has nothing assigned in this
                    # project → they receive nothing. Product decision.
                    continue

                # Count by status only from their own tasks
                pending = [t for t in his_tasks if t.get('status') == 'pending']
                in_progress = [t for t in his_tasks if t.get('status') == 'in_progress']
                blocked = [t for t in his_tasks if t.get('status') == 'blocked']

                # Personalized message for this person (includes their name)
                lines = [
                    f"📋 *{project_name}* — Your pending items",
                    "",
                    f"Hi {part.get('name', '')}, you have {len(his_tasks)} pending task(s) in this project.",
                    "",
                ]
                if blocked:
                    lines.append(f"🚫 Blocked: {len(blocked)}")
                if in_progress:
                    lines.append(f"🔄 In progress: {len(in_progress)}")
                if pending:
                    lines.append(f"⏳ To do: {len(pending)}")
                lines.append("")
                lines.append("📝 Details:")

                # Prioritize blocked, then in progress, then pending.
                preview = blocked + in_progress + pending
                for t in preview[:10]:
                    status = t.get('status', '')
                    icon = {'pending': '⏳', 'in_progress': '🔄', 'blocked': '🚫'}.get(status, '•')
                    text = (t.get('text', '') or '')[:80]
                    due = (t.get('dueDate', '') or '').strip()
                    line = f"  {icon} {text}"
                    if due:
                        line += f" (due {due})"
                    lines.append(line)
                if len(preview) > 10:
                    lines.append(f"  … and {len(preview) - 10} more")
                lines.append("")
                lines.append("Open OneBox to see them: https://www.oneboxmanager.com")
                message = "\n".join(lines)

                if email:
                    res = send_notification(
                        recipient=email,
                        message=message,
                        channel='email',
                        project_id=pid,
                        project_name=project_name,
                    )
                    if res.get('success'):
                        notifications_sent += 1
                        project_processed_at_least_one = True
                    elif res.get('status') == 'skipped_unverified':
                        notifications_skipped += 1
                    else:
                        errors.append(f"email {email}: {res.get('error', '')[:120]}")

                if tel:
                    res = send_notification(
                        recipient=tel,
                        message=message,
                        channel='whatsapp',
                        project_id=pid,
                        project_name=project_name,
                    )
                    if res.get('success'):
                        notifications_sent += 1
                        project_processed_at_least_one = True
                    else:
                        errors.append(f"whatsapp {tel}: {res.get('error', '')[:120]}")

            if project_processed_at_least_one:
                projects_processed += 1
        finally:
            clear_current_user()

    return {
        "success": True,
        "projects_processed": projects_processed,
        "notifications_sent": notifications_sent,
        "notifications_skipped": notifications_skipped,  # unverified email in SES sandbox
        "errors": errors if errors else None,
    }


def dispatch_pending_notifications() -> dict:
    """Dispatcher for scheduled notifications. Designed to run every hour
    via EventBridge → POST /api/scheduled/dispatch-pending.

    Logic:
    - Scans onebox-notifications for status='pending'.
    - One-time (scheduledAt present, isRecurring absent or False):
        If scheduledAt <= now UTC → sends and updates status='sent'.
    - Recurring (isRecurring=True, recurringDays present):
        If today (UTC) is in recurringDays → sends.
        Keeps status='pending' so it runs again next week.
        Records the last run in 'lastSentAt' to avoid double sends
        within the same hourly window.
    """
    from datetime import datetime, timezone
    from agent.tools import (
        send_notification, notifications_table, set_current_user, clear_current_user,
    )

    now_dt = datetime.now(timezone.utc)
    now_iso = now_dt.isoformat()
    today_name = now_dt.strftime('%A').lower()  # 'monday', 'tuesday', etc.

    try:
        result = notifications_table.scan(
            FilterExpression=Attr('status').eq('pending')
        )
        pending = result.get('Items', [])
    except Exception as e:
        return {"success": False, "error": f"scan error: {e}"}

    sent = 0
    skipped = 0
    errors = []

    for notif in pending:
        uid = notif.get('userId', '')
        if not uid:
            continue

        channel = notif.get('channel', '')
        recipient = notif.get('recipient', '')
        message = notif.get('message', '')
        project_id = notif.get('projectId', '')
        project_name = notif.get('projectName', '')
        notif_id = notif.get('notificationId', '')
        scheduled_at = notif.get('scheduledAt', '')
        is_recurring = notif.get('isRecurring', False)
        recurring_days = notif.get('recurringDays', [])

        should_send = False

        if is_recurring:
            # Recurring: check whether today is one of the configured days.
            if today_name not in (recurring_days or []):
                skipped += 1
                continue
            # Prevent double sends if it already ran today (lastSentAt matches today's date).
            last_sent = notif.get('lastSentAt', '')
            if last_sent and last_sent[:10] == now_dt.strftime('%Y-%m-%d'):
                skipped += 1
                continue
            should_send = True
        elif scheduled_at:
            # One-time: only send if scheduled_at <= now.
            try:
                # Normalize to aware datetime
                sat = scheduled_at.rstrip('Z')
                sched_dt = datetime.fromisoformat(sat).replace(tzinfo=timezone.utc)
                if sched_dt <= now_dt:
                    should_send = True
                else:
                    skipped += 1
                    continue
            except ValueError:
                errors.append(f"{notif_id}: invalid scheduledAt '{scheduled_at}'")
                continue
        else:
            # No scheduledAt and no isRecurring → should not be in pending, skip.
            skipped += 1
            continue

        if not should_send:
            skipped += 1
            continue

        # Send using the context of the notification's owner user.
        set_current_user(uid, '')
        try:
            res = send_notification(
                recipient=recipient,
                message=message,
                channel=channel,
                project_id=project_id,
                project_name=project_name,
                # No scheduled_at or recurring_days: immediate send.
            )
            if res.get('success') or res.get('status') in ('sent', 'queued', 'test_simulated', 'skipped_unverified'):
                sent += 1
                if is_recurring:
                    # Update lastSentAt but keep status=pending.
                    try:
                        notifications_table.update_item(
                            Key={'userId': uid, 'notificationId': notif_id},
                            UpdateExpression='SET lastSentAt = :ts',
                            ExpressionAttributeValues={':ts': now_iso},
                        )
                    except Exception as ue:
                        errors.append(f"{notif_id}: update lastSentAt error: {ue}")
                else:
                    # One-time: mark as sent.
                    try:
                        notifications_table.update_item(
                            Key={'userId': uid, 'notificationId': notif_id},
                            UpdateExpression='SET #s = :sent, sentAt = :ts',
                            ExpressionAttributeNames={'#s': 'status'},
                            ExpressionAttributeValues={':sent': 'sent', ':ts': now_iso},
                        )
                    except Exception as ue:
                        errors.append(f"{notif_id}: update status error: {ue}")
            else:
                errors.append(f"{notif_id} ({channel} → {recipient}): {res.get('error', 'unknown')[:120]}")
        except Exception as e:
            errors.append(f"{notif_id}: exception: {str(e)[:120]}")
        finally:
            clear_current_user()

    return {
        "success": True,
        "evaluated": len(pending),
        "sent": sent,
        "skipped": skipped,
        "errors": errors if errors else None,
    }
