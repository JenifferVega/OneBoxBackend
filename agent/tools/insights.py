"""Insight and analysis tools: SLA, auto-classification and executive summary.

Moved verbatim out of the old single-file agent/tools.py.
"""

from datetime import datetime
import uuid

from agent.tools.access import _accessible_project_ids, _has_project_access
from agent.tools.context import _current_uid
from agent.tools.db import conversations_table, insights_table, projects_table, tasks_table
from agent.tools.registry import register_tool


@register_tool("create_insight")
def create_insight(project_id: str, project_name: str, type: str, title: str, description: str = "", related_person: str = "", actions: list = None) -> dict:
    """Creates an insight in the intelligence table."""
    if not _has_project_access(project_id):
        return {"error": "No access to that project"}
    try:
        now = datetime.utcnow().isoformat()
        insight_id = f"{now}#{uuid.uuid4().hex[:8]}"
        item = {
            'userId': _current_uid(),
            'insightId': insight_id,
            'projectId': project_id,
            'projectName': project_name,
            'type': type,
            'title': title,
            'description': description,
            'actor': 'OneBox IA',
            'relatedPerson': related_person,
            'actionsTaken': actions or [],
            'status': 'new',
            'createdAt': now
        }
        insights_table.put_item(Item=item)
        return {"success": True, "insightId": insight_id, "type": type, "title": title}
    except Exception as e:
        return {"error": str(e)}


@register_tool("check_sla")
def check_sla() -> dict:
    """Scans tasks and projects for blocked, overdue or unanswered items."""
    try:
        from boto3.dynamodb.conditions import Attr
        today = datetime.utcnow().strftime("%Y-%m-%d")

        print(f"[Tool] check_sla → Scanning tasks and projects...")

        # Scope = ALL accessible projects (own + shared + invited), same as
        # list_projects. Tasks belong to the project OWNER's userId, so we
        # filter by accessible projectId, NOT by the current user's userId
        # (otherwise an invited user would see no tasks).
        accessible = list(_accessible_project_ids())
        if not accessible:
            return {"success": True, "total_alerts": 0, "blocked_tasks": 0,
                    "overdue_tasks": 0, "unassigned_inbox": 0, "alerts": []}

        blocked_result = tasks_table.scan(
            FilterExpression=Attr('projectId').is_in(accessible) & Attr('status').eq('blocked')
        )
        blocked_tasks = blocked_result.get('Items', [])

        all_tasks_result = tasks_table.scan(
            FilterExpression=Attr('projectId').is_in(accessible) & Attr('status').eq('pending')
        )
        overdue_tasks = []
        for task in all_tasks_result.get('Items', []):
            due = task.get('dueDate', '')
            if due and due < today:
                overdue_tasks.append(task)

        unassigned_result = conversations_table.scan(
            FilterExpression=Attr('projectId').eq('unassigned') & Attr('userId').eq(_current_uid())
        )
        unassigned_count = len(unassigned_result.get('Items', []))

        alerts = []
        for task in blocked_tasks:
            created = task.get('createdAt', '')
            alerts.append({
                "type": "blocked",
                "severity": "high",
                "task": task.get('text', ''),
                "project_id": task.get('projectId', ''),
                "assigned_to": task.get('assignedTo', ''),
                "blocked_since": created
            })

        for task in overdue_tasks:
            alerts.append({
                "type": "overdue",
                "severity": "high",
                "task": task.get('text', ''),
                "project_id": task.get('projectId', ''),
                "due_date": task.get('dueDate', ''),
                "assigned_to": task.get('assignedTo', '')
            })

        if unassigned_count > 0:
            alerts.append({
                "type": "unassigned_inbox",
                "severity": "medium",
                "count": unassigned_count,
                "message": f"There are {unassigned_count} unassigned messages in the inbox"
            })

        print(f"[Tool] check_sla ← {len(alerts)} alerts found")
        return {
            "success": True,
            "total_alerts": len(alerts),
            "blocked_tasks": len(blocked_tasks),
            "overdue_tasks": len(overdue_tasks),
            "unassigned_inbox": unassigned_count,
            "alerts": alerts[:20] 
        }
    except Exception as e:
        return {"error": f"Error checking SLA: {str(e)}"}


@register_tool("auto_classify_messages")
def auto_classify_messages() -> dict:
    """Reads unassigned messages and suggests classification based on existing projects."""
    try:
        from boto3.dynamodb.conditions import Attr

        print(f"[Tool] auto_classify_messages → Analyzing inbox...")

        unassigned_result = conversations_table.scan(
            FilterExpression=Attr('projectId').eq('unassigned') & Attr('userId').eq(_current_uid())
        )
        unassigned = unassigned_result.get('Items', [])

        if not unassigned:
            return {"success": True, "message": "No unassigned messages", "suggestions": []}

        # Accessible projects (own + shared + invited), not only own.
        projects = []
        for pid in _accessible_project_ids():
            it = projects_table.get_item(Key={'projectId': pid}).get('Item')
            if it:
                projects.append(it)

        if not projects:
            return {
                "success": True,
                "message": f"There are {len(unassigned)} unassigned messages but no projects created",
                "unassigned_count": len(unassigned),
                "suggestions": []
            }

        suggestions = []
        for msg in unassigned[:10]:  
            subject = (msg.get('subject', '') or '').lower()
            body = (msg.get('body', '') or '')[:300].lower()
            sender = msg.get('from', '') or msg.get('fromEmail', '')
            content = f"{subject} {body}"

            best_match = None
            best_score = 0

            for proj in projects:
                proj_name = (proj.get('name', '') or '').lower()
                proj_desc = (proj.get('description', '') or '').lower()
                proj_keywords = proj_name.split() + proj_desc.split()

                score = 0
                for keyword in proj_keywords:
                    if len(keyword) > 3 and keyword in content:
                        score += 1

                if score > best_score:
                    best_score = score
                    best_match = proj

            suggestions.append({
                "conversation_id": msg.get('conversationId', ''),
                "from": sender,
                "subject": msg.get('subject', ''),
                "channel": msg.get('channel', 'gmail'),
                "suggested_project": best_match.get('name', 'No suggestion') if best_match and best_score > 0 else None,
                "suggested_project_id": best_match.get('projectId', '') if best_match and best_score > 0 else None,
                "confidence": "high" if best_score >= 2 else "medium" if best_score == 1 else "no_match"
            })

        classified = len([s for s in suggestions if s.get('suggested_project')])
        print(f"[Tool] auto_classify_messages ← {len(suggestions)} messages, {classified} with suggestion")
        return {
            "success": True,
            "total_unassigned": len(unassigned),
            "analyzed": len(suggestions),
            "with_suggestion": classified,
            "suggestions": suggestions
        }
    except Exception as e:
        return {"error": f"Error classifying messages: {str(e)}"}


@register_tool("proactive_summary")
def proactive_summary() -> dict:
    """Generates an executive summary of the state of all projects and suggested actions."""
    try:
        from boto3.dynamodb.conditions import Attr

        print(f"[Tool] proactive_summary → Generating summary...")

        # Accessible projects (own + shared + invited). Tasks and insights
        # belong to the project OWNER's userId, so we filter by accessible
        # projectId, NOT by the current user's userId (same as list_projects).
        accessible = _accessible_project_ids()
        projects = []
        for pid in accessible:
            it = projects_table.get_item(Key={'projectId': pid}).get('Item')
            if it:
                projects.append(it)

        acc_list = list(accessible)
        all_tasks, insights = [], []
        if acc_list:
            tasks_result = tasks_table.scan(FilterExpression=Attr('projectId').is_in(acc_list))
            all_tasks = tasks_result.get('Items', [])
            insights_result = insights_table.scan(FilterExpression=Attr('projectId').is_in(acc_list))
            insights = sorted(insights_result.get('Items', []),
                              key=lambda x: x.get('createdAt', ''), reverse=True)[:5]

        # The unassigned inbox IS per-user (received emails belong to the current user).
        inbox_result = conversations_table.scan(
            FilterExpression=Attr('projectId').eq('unassigned') & Attr('userId').eq(_current_uid())
        )
        unassigned = inbox_result.get('Items', [])

        project_summaries = []
        for proj in projects:
            pid = proj.get('projectId', '')
            proj_tasks = [t for t in all_tasks if t.get('projectId') == pid]
            pending = len([t for t in proj_tasks if t.get('status') == 'pending'])
            blocked = len([t for t in proj_tasks if t.get('status') == 'blocked'])
            done = len([t for t in proj_tasks if t.get('status') == 'done'])

            project_summaries.append({
                "name": proj.get('name', ''),
                "project_id": pid,
                "status": proj.get('status', ''),
                "tasks_total": len(proj_tasks),
                "tasks_pending": pending,
                "tasks_blocked": blocked,
                "tasks_done": done,
                "channels": proj.get('channels', []),
                "last_activity": proj.get('lastActivity', '')
            })

        suggested_actions = []
        today = datetime.utcnow().strftime("%Y-%m-%d")

        for summary in project_summaries:
            if summary['tasks_blocked'] > 0:
                suggested_actions.append({
                    "action": "send_reminder",
                    "reason": f"Project '{summary['name']}' has {summary['tasks_blocked']} blocked task(s)",
                    "priority": "high"
                })
            if summary['tasks_pending'] > 5:
                suggested_actions.append({
                    "action": "review_tasks",
                    "reason": f"Project '{summary['name']}' has {summary['tasks_pending']} pending tasks piled up",
                    "priority": "medium"
                })

        if len(unassigned) > 3:
            suggested_actions.append({
                "action": "classify_inbox",
                "reason": f"There are {len(unassigned)} unclassified messages in the inbox",
                "priority": "high"
            })

        overdue = [t for t in all_tasks if t.get('dueDate', '') and t.get('dueDate', '') < today and t.get('status') == 'pending']
        if overdue:
            suggested_actions.append({
                "action": "escalate_overdue",
                "reason": f"There are {len(overdue)} task(s) with an overdue date",
                "priority": "high"
            })

        print(f"[Tool] proactive_summary ← {len(projects)} projects, {len(all_tasks)} tasks, {len(suggested_actions)} suggested actions")
        return {
            "success": True,
            "projects_count": len(projects),
            "total_tasks": len(all_tasks),
            "unassigned_inbox": len(unassigned),
            "projects": project_summaries,
            "recent_insights": [{
                "title": i.get('title', ''),
                "type": i.get('type', ''),
                "date": i.get('createdAt', '')
            } for i in insights],
            "suggested_actions": suggested_actions
        }
    except Exception as e:
        return {"error": f"Error generating summary: {str(e)}"}


# ============================================================================
# NAME CANDIDATE RANKING
# ----------------------------------------------------------------------------
# Users mistype names constantly ("Famarcia Hussman" -> "Farmacia Haussman").
# Substring matching cannot absorb a typo, so the lookup returns nothing and
# the narrator reports "that does not exist" -- the worst possible answer.
#
# DESIGN: this module RANKS, it does not DECIDE.
#
# A character-distance metric has no idea that "Q3" and "Q4" are different
# quarters, that "Pipe" is short for Felipe, or that "la farmacia" refers to
# "Farmacia Haussman". Encoding those judgements as thresholds means a dumb
# function silently discards candidates before anything with semantic
# understanding ever sees them -- and a false negative here is invisible,
# because nobody can tell the difference between "no match" and "the gate ate
# the right answer".
#
# So _similarity() returns a continuous score and NOTHING else. resolve_entity
# hands the ranked candidates to the LLM, which makes the actual call: it can
# read "Q4 vs Q3 -- different projects" and "Hussman vs Haussman -- typo" from
# the names themselves. Cheap and deterministic for ordering, semantic for the
# verdict.
#
# RECALL_FLOOR exists only to keep a large workspace from dumping every name
# into the prompt. It is deliberately far below anything that could be a real
# match, and the count of what it dropped is reported back.
# ============================================================================
