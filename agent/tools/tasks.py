"""Task and reminder tools.

Moved verbatim out of the old single-file agent/tools.py.
"""

from datetime import datetime
import uuid

from agent.tools.access import _has_project_access
from agent.tools.context import _current_email, _current_uid
from agent.tools.db import insights_table, tasks_table
from agent.tools.errors import _svc_error
from agent.tools.registry import register_tool


@register_tool("create_task")
def create_task(
    project_id: str,
    text: str,
    assigned_to: str = "",
    status: str = "pending",
    start_date: str = "",
    due_date: str = "",
    description: str = "",
) -> dict:
    """Creates a task associated with a project.

    start_date / due_date are OPTIONAL in YYYY-MM-DD format. The AI should
    always try to propose them by looking at the project's deliveryDate/timing
    so the Gantt works from day 1. If they are not passed, the task ends up
    without dates and does not appear on the timeline (still valid).

    Reuses api.services.tasks.create_task (same path as the REST API) so we
    have BEHAVIOR PARITY: the task belongs to the project owner, createdBy
    is stored, and if it is assigned to someone they are NOTIFIED
    (WhatsApp/email) just like when reassigning with update_task. Previously
    this tool wrote directly to DynamoDB and did NOT notify → inconsistency
    with update_task, now fixed.
    """
    if not _has_project_access(project_id):
        return {"error": "No access to that project"}
    try:
        from types import SimpleNamespace
        from api.services.tasks import create_task
        req = SimpleNamespace(
            text=text,
            description=description or "",
            status=status or "pending",
            assigned_to=assigned_to or "",
            start_date=(start_date or "").strip() or None,
            due_date=(due_date or "").strip() or None,
            parent_task_id=None,
        )
        return create_task(_current_uid(), _current_email(), project_id, req)
    except Exception as e:
        return {"error": _svc_error(e)}


@register_tool("create_reminder")
def create_reminder(title: str, descripcion: str = "", due_date: str = "", project_id: str = "", project_name: str = "", assigned_to: str = "") -> dict:
    """Creates a reminder/follow-up with a due date."""
    try:
        now = datetime.utcnow().isoformat()
        reminder_id = f"rem_{uuid.uuid4().hex[:8]}"

        print(f"[Tool] create_reminder → {title} | Due: {due_date}")

        item = {
            'projectId': project_id or 'general',
            'taskId': reminder_id,
            'userId': _current_uid(),
            'text': title,
            'description': descripcion,
            'status': 'pending',
            'type': 'reminder',
            'createdBy': 'OneBox IA',
            'assignedTo': assigned_to,
            'dueDate': due_date,
            'createdAt': now
        }
        tasks_table.put_item(Item=item)

        try:
            insight_id = f"{now}#{uuid.uuid4().hex[:8]}"
            insights_table.put_item(Item={
                'userId': _current_uid(),
                'insightId': insight_id,
                'projectId': project_id,
                'projectName': project_name,
                'type': 'followup',
                'title': f'Reminder created: {title}',
                'description': f'Due: {due_date}. {descripcion}',
                'actor': 'OneBox IA',
                'relatedPerson': assigned_to,
                'actionsTaken': [f'Created reminder: {title}'],
                'status': 'new',
                'createdAt': now
            })
        except Exception:
            pass

        print(f"[Tool] create_reminder ← OK (ID: {reminder_id})")
        return {
            "success": True,
            "reminder_id": reminder_id,
            "title": title,
            "due_date": due_date,
            "assigned_to": assigned_to
        }
    except Exception as e:
        return {"error": f"Error creating reminder: {str(e)}"}


# What the Gantt can and cannot draw.
#
# The timeline is not a view of "the tasks": it is a view of the tasks that
# can be placed on a calendar. ProjectGantt.tsx keeps only those with BOTH
# dates and with end >= start; everything else is dropped silently.
#
# On 2026-09-22 a client reported "the dashboard says 8 blocked and the gantt
# shows 5". Asked which task was missing, the agent answered "I have no way to
# access your Gantt chart" -- and it was wrong: it had every field it needed.
# What it lacked was the RULE, which lived only in a React component.
#
# Teaching the rule to the prompt would have been the wrong fix: prose is not
# enforced, it drifts, and every model would have to learn it again. The fact
# belongs in the data, next to the task it describes -- the same reason
# get_project_contacts reports `unreachable` instead of a phone count.
#
# NOTE: this rule is a second copy of the one in
# OneBoxFrontEnd/OneBox/src/components/ProjectGantt.tsx (the `scheduledAll`
# filter). If one changes, the other must. tests/test_gantt_visibility.py
# pins both.
def _timeline_issue(task: dict) -> str:
    """Why this task cannot be drawn on the timeline, or '' if it can."""
    start = (task.get("startDate") or "").strip()
    due = (task.get("dueDate") or "").strip()
    if not start and not due:
        return "no start date and no due date"
    if not start:
        return "no start date"
    if not due:
        return "no due date"
    if due < start:                      # ISO dates compare as strings
        return f"due date {due} is before the start date {start}"
    return ""


@register_tool("list_tasks")
def list_tasks(project_id: str) -> dict:
    """Lists a project's tasks. Useful for locating the real taskId."""
    if not _has_project_access(project_id):
        return {"error": "No access to that project"}
    try:
        from api.services.tasks import list_tasks
        items = list_tasks(_current_uid(), _current_email(), project_id)
        tasks = []
        for t in items:
            issue = _timeline_issue(t)
            tasks.append({
                "taskId":       t.get("taskId", ""),
                "text":         t.get("text", ""),
                "status":       t.get("status", ""),
                "assignedTo":   t.get("assignedTo", ""),
                "startDate":    t.get("startDate", ""),
                "dueDate":      t.get("dueDate", ""),
                "parentTaskId": t.get("parentTaskId", ""),
                "on_timeline":  not issue,
                "timeline_issue": issue,
            })

        off = [{"taskId": t["taskId"], "text": t["text"],
                "why": t["timeline_issue"]}
               for t in tasks if not t["on_timeline"]]
        out = {"count": len(tasks), "tasks": tasks}
        if off:
            # Named, not counted: a count cannot be acted on, and "3 tasks are
            # missing from your Gantt" is not an answer to "which one?".
            out["not_on_timeline"] = off
            out["instruction"] = (
                f"{len(off)} of these {len(tasks)} tasks do NOT appear on the "
                f"Gantt/timeline, listed under 'not_on_timeline' with the reason. "
                f"A task is on the timeline only if it has BOTH a start date and "
                f"a due date, and the due date is not earlier than the start. "
                f"If the user asks what is missing from the Gantt, answer with "
                f"those names and their reason; do not say you cannot see their "
                f"Gantt.")
        return out
    except Exception as e:
        return {"error": _svc_error(e)}


@register_tool("update_task")
def update_task(task_id: str, text: str = None, status: str = None,
                     assigned_to: str = None, due_date: str = None,
                     start_date: str = None, description: str = None,
                     blocked_reason: str = None, comment: str = None) -> dict:
    """Updates an existing task. Only changes the fields that are sent.
    Reuses api.services.tasks.update_task → preserves the block and
    reassignment notifications."""
    if not task_id or not isinstance(task_id, str):
        return {"error": "Missing task_id. Use list_tasks to get the real task ID."}
    try:
        from types import SimpleNamespace
        from api.services.tasks import update_task
        req = SimpleNamespace(
            text=text, status=status, assigned_to=assigned_to,
            description=description, blocked_reason=blocked_reason,
            start_date=start_date, due_date=due_date, parent_task_id=None,
            comment=comment,
        )
        return update_task(_current_uid(), _current_email(), task_id, req)
    except Exception as e:
        return {"error": _svc_error(e)}


@register_tool("delete_task")
def delete_task(task_id: str, cascade: bool = False) -> dict:
    """Deletes a task (and optionally its subtasks)."""
    if not task_id or not isinstance(task_id, str):
        return {"error": "Missing task_id. Use list_tasks to get the real task ID."}
    try:
        from api.services.tasks import delete_task
        return delete_task(_current_uid(), _current_email(), task_id, bool(cascade))
    except Exception as e:
        return {"error": _svc_error(e)}
