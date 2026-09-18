"""
Shared functions for creating projects with AI analysis.
Used both by the web endpoint POST /api/projects and by the WhatsApp flow.
"""
import os
import json
import re
import uuid
from datetime import datetime, timedelta
from typing import Optional

import boto3

from agent.tools import (
    projects_table, insights_table, notifications_table, tasks_table
)
from agent.llm import call_llm, extract_json_from_response


COGNITO_USER_POOL_ID = os.environ.get("COGNITO_USER_POOL_ID", "us-east-1_b76prubhx")
COGNITO_REGION = os.environ.get("AWS_REGION", "us-east-1")

_cognito_client = None


def get_cognito_client():
    global _cognito_client
    if _cognito_client is None:
        _cognito_client = boto3.client("cognito-idp", region_name=COGNITO_REGION)
    return _cognito_client


def lookup_user_by_email(email: str) -> Optional[dict]:
    """
    Looks up a user in Cognito by email.
    Returns {userId, email, name} if found, None otherwise.
    """
    if not email or '@' not in email:
        return None
    try:
        client = get_cognito_client()
        # Look up by email (verified or not, but must exist)
        response = client.list_users(
            UserPoolId=COGNITO_USER_POOL_ID,
            Filter=f'email = "{email.strip().lower()}"',
            Limit=1
        )
        users = response.get('Users', [])
        if not users:
            return None

        user = users[0]
        attrs = {a['Name']: a['Value'] for a in user.get('Attributes', [])}
        return {
            'userId': attrs.get('sub', user.get('Username', '')),
            'email': attrs.get('email', email),
            'name': attrs.get('name') or attrs.get('given_name') or attrs.get('email', '').split('@')[0],
            'status': user.get('UserStatus', '')
        }
    except Exception as e:
        print(f"[Cognito] Error looking up user by email {email}: {e}")
        return None


def evaluate_description(name: str, description: str) -> dict:
    """
    Uses the AI to evaluate whether the project description is sufficient
    to generate useful insights.
    Returns {sufficient: bool, missing: str, score: int}
    """
    if not description or len(description.strip()) < 20:
        return {
            "sufficient": False,
            "missing": "a more detailed description (goals, deadlines, team)",
            "score": 0
        }

    prompt = f"""Evaluate whether this project description is enough to produce a useful analysis with tasks, risks and key decisions.

PROJECT: {name}
DESCRIPTION: {description}

A good description should mention at least 2 of: goal, deadlines, team, scope/features, budget, risks, dependencies.

Respond ONLY with JSON in this exact format:
{{
  "sufficient": true|false,
  "missing": "what is missing to improve it (1 short sentence) or empty if sufficient",
  "score": 1-10
}}"""

    try:
        response = call_llm(
            system_prompt="You are an evaluator of project descriptions. Respond ONLY with valid JSON.",
            user_message=prompt,
            temperature=0.1,
            max_tokens=200
        )
        analysis = extract_json_from_response(response)
        if not analysis:
            # Fallback: if the description has >80 characters, assume it's OK
            return {
                "sufficient": len(description) >= 80,
                "missing": "more detail if you want a better analysis" if len(description) < 80 else "",
                "score": min(10, len(description) // 20)
            }
        return {
            "sufficient": bool(analysis.get('sufficient', False)),
            "missing": str(analysis.get('missing', ''))[:200],
            "score": int(analysis.get('score', 0))
        }
    except Exception as e:
        print(f"[evaluate_description] Error: {e}")
        # Simple fallback
        return {
            "sufficient": len(description) >= 80,
            "missing": "" if len(description) >= 80 else "more detail about goals and deadlines",
            "score": min(10, len(description) // 20)
        }


def generate_insights_for_project(
    user_id: str,
    project_id: str,
    project_name: str,
    project_type: str,
    description: str,
    # The LIST, not only how many. The body has always used it -- it builds a
    # richer prompt naming each person and their role -- but the signature had
    # lost it, so line "participants = participants or []" read a name that was
    # never bound: UnboundLocalError, and a 500 on every project created from a
    # document. The other two call sites were broken the same way, one of them
    # passing `participants=` to a function that did not accept it (TypeError).
    participants: list = None,
    participants_count: int = 0,
    analysis_text: str = "",
    dry_run: bool = False
) -> dict:
    """
    Calls the AI to analyze the content and create insights in DynamoDB.
    Returns {generated: bool, count: int, analysis: dict}

    dry_run: if True, runs the analysis with the real LLM but DOES NOT write
    anything to DynamoDB (neither insights nor tasks). Useful for checking
    the AI quality without creating data. The `analysis` field of the return
    holds the raw LLM output.

    analysis_text: original FULL TEXT (transcript/document). If provided, THIS
    is analyzed instead of the short description. Previously only the
    planner's short description (a few sentences) was analyzed → almost all
    the context of long texts was lost. Now the deep analysis sees the real
    material.
    """
    content = (analysis_text or description or "").strip()
    if not content:
        return {"generated": False, "reason": "no_description"}

    # Normalized here too: participants are stored with whatever keys the
    # writer used ({nombre, rol} from a Spanish conversation, for one), and
    # this prompt reads name and role. The leftover `p.get('name', p.get('name'))`
    # below was a half-finished attempt at the same thing -- a default that
    # re-read the key it had just failed to find.
    try:
        from api.services.projects import normalize_participants
        participants = normalize_participants(participants)
    except Exception:
        participants = participants or []
    if not participants_count and participants:
        participants_count = len(participants)

    # Today's date so the LLM can stagger tasks starting from here
    today_str = datetime.utcnow().strftime('%Y-%m-%d')

    # Build the participants section for the prompt
    if participants:
        parts_lines = "\n".join(
            f"  - {p.get('name', '')} ({p.get('role') or 'Participant'})"
            for p in participants if p.get('name')
        )
        participants_section = f"PARTICIPANTS ({participants_count}):\n{parts_lines}"
    else:
        participants_section = f"PARTICIPANTS: {participants_count} people (no detail)"

    analysis_prompt = f"""You are a senior project analyst. Your job is to produce a FACTUAL summary of the PROJECT itself (what it is, what it has, what stack it uses, what deliverables), NOT of the conversation between the authors.

PROJECT: {project_name}
DECLARED TYPE: {project_type}
{participants_section}

CONTENT TO ANALYZE (conversation, brief, minutes, email, etc.):
{content}

CRITICAL RULES FOR "summary":
1. The summary must describe THE PROJECT, not the conversation.
   - ❌ INCORRECT: "Kevin asked Mateo for a project..." / "Santi passed the credentials to Belen..."
   - ✅ CORRECT: "GhostLink, a messaging application..." / "Update of the WordPress site les-sp.org..."
2. DO NOT narrate "X said to Y", "X proposed to Y", "X requested from Y". Describe the project as a technical/executive brief.
3. FORBIDDEN to use generic language like "initiative to optimize", "a series of improvements", "an important challenge".
4. MANDATORY to mention specific FACTS about THE PROJECT:
   - Name of the product/project (e.g.: "GhostLink", "les-sp.org")
   - Specific features
   - Exact tech stack if present (React, Node.js, MongoDB, etc.)
   - URLs / domains / platforms
   - Concrete tasks with detail
   - Exact metrics (hours, prices, dates)
   - Specific decisions about the product
5. People who only chat do NOT go in the summary.
   - Exception: if a person is the end client, owner or key role of the project, then include them.
6. 8-14 sentences, dense with concrete information ABOUT THE PROJECT. If the text is long and covers several systems/processes, use the upper range and don't leave important processes out.

STRICT RULES FOR "work_done" vs "tasks" (VERY IMPORTANT):
- "work_done" = ONLY things EXPLICITLY already done/completed in the text.
- "tasks" = pending/proposed/required things that have NOT been done yet.

Indicators of WORK DONE (past-tense verbs describing completed actions):
  - "we already did X", "we reorganized Y", "we implemented Z", "W is complete"
  - "we redirected the news", "we removed obsolete content"
  - "was performed", "was completed", "was delivered"

Indicators that are NOT work done (go to "tasks", NOT "work_done"):
  - "would have", "would include", "the stack will be", "would be like", "will be done with"
  - "we need", "we have to", "we're going to do", "is pending"
  - Definitions of future features
  - Tech stack decided but NOT implemented
  - Proposals in an initial conversation

EXAMPLE 1 — proposal conversation (all future):
Text: "Kevin: I need an app. Mateo: it would have real-time chats. The stack would be React+Node."
- work_done: [] (NOTHING is done, just proposed)
- tasks: ["Implement real-time chats", "Set up React + Node stack"]
- decisions: ["Tech stack: React + Node"]

EXAMPLE 2 — chat with real work already done:
Text: "Santi: we reorganized the home and removed obsolete content. ~10 hours."
- work_done: ["Home page reorganization", "Removal of obsolete content"]
- tasks: [] (no new pending items mentioned)
- metrics: ["~10 hours worked"]

GOLDEN RULE: if the whole text is a planning/proposal conversation with no completed work, "work_done" must be EMPTY. DO NOT invent work done.

RULES FOR THE OTHER FIELDS:
- Distinguish WORK PERFORMED (already done per the text) from PENDING TASKS (what's left).
- Distinguish general RISKS from client-specific BLOCKERS (missing content, credentials, external dependencies).
- Identify specific TECHNICAL ISSUES (plugins, hosting, access, integrations).
- Capture exact METRICS if they appear (hours, €, dates).
- Characterize the REAL PROJECT (not just the category).
- Characterize the CLIENT if it can be inferred.
- DO NOT invent data not in the text.

EXAMPLE OF A GOOD summary (reference, do not copy):
"Update of the WordPress site les-sp.org for client LES España y Portugal. Santi passed the credentials to Belen, who confirmed access. Pending tasks: update the board, review member benefits (no changes in 9 years), add publications (the last ones are from 2022), fill the video library and add testimonials. Blocker: the plugins are outdated and hosting does not allow updating them fully. Work already done: home page reorganization, removal of obsolete content, redirection of news to LinkedIn and creation of 2 versions of /comites-les-espana-y-portugal. Estimate: ~10 hours at 14.5€/hour. Decided not to add a security plugin because they do daily backups."

EXAMPLE OF A BAD summary (DO NOT DO THIS):
"The project is an initiative to optimize and update the institutional website. There are a series of improvements needed and a critical technical blocker that represents an important challenge. Institutional client with an outdated website."

DATES AND TASK ASSIGNMENT — IMPORTANT FOR THE GANTT:
For "tasks", "work_done" and "blockers" return OBJECTS with start_date, due_date and assigned_to:
- TODAY is {today_str}. Distribute the tasks sequentially starting tomorrow.
- If the text mentions explicit deadlines ("before June 15", "by Friday"), use them.
- If there are no deadlines, estimate by complexity:
  · Small tasks (configure, tweak, review): 2-3 days
  · Medium tasks (implement, integrate, design): 5-7 days
  · Large tasks (full module, release): 10-15 days
- For "work_done" put dates in the past if there's a clue; otherwise leave them blank.
- Stagger with 1-2 days of margin between tasks so the Gantt looks clean.
- "assigned_to": exact name of the participant responsible for the task (from the PARTICIPANTS list).
  If there is no clear participant for that task, leave empty.

RESPOND ONLY WITH JSON, no extra text or code fences:
{{
  "summary": "FACTUAL summary with names, URLs, numbers, tasks and concrete decisions mentioned in the text (8-14 sentences)",
  "project_type_real": "Concrete characterization in one sentence (e.g.: 'Update of institutional WordPress with hosting blocker')",
  "client_profile": "Client characterization with data from the text (e.g.: 'LES España y Portugal association with an outdated institutional website'). Empty if there is not enough data.",
  "key_insight": "The most important strategic observation in one concrete sentence",
  "work_done": [{{"text": "Task already done", "assigned_to": "Name or empty", "start_date": "YYYY-MM-DD or empty", "due_date": "YYYY-MM-DD or empty"}}],
  "tasks": [{{"text": "Specific pending task", "assigned_to": "Owner name or empty", "start_date": "YYYY-MM-DD", "due_date": "YYYY-MM-DD"}}],
  "risks": ["General risk 1", "Risk 2"],
  "blockers": [{{"text": "Specific blocker with detail", "assigned_to": "Name or empty", "start_date": "YYYY-MM-DD", "due_date": "YYYY-MM-DD"}}],
  "decisions": ["Concrete decision made or pending", "Another decision"],
  "metrics": ["Exact metric from the text (e.g.: '~10 hours estimated', '14.5€/hour')"],
  "tech_issues": ["Specific technical issue mentioned"]
}}"""

    try:
        print(f"[insights] Calling LLM for {project_name}")
        response = call_llm(
            system_prompt="You are a project analyst who extracts CONCRETE and FACTUAL data from a text. Your priority is being specific: proper names, URLs, numbers, exact dates, concrete tasks. Generic language is forbidden. You ALWAYS return valid JSON in the same language as the source text (default English) with no markdown code fences.",
            user_message=analysis_prompt,
            temperature=0.15,
            max_tokens=8192
        )
        print(f"[insights] LLM response ({len(response)} chars)")

        # Try to parse JSON
        analysis = extract_json_from_response(response)
        if not analysis:
            cleaned = response.strip()
            if cleaned.startswith('```'):
                cleaned = re.sub(r'^```(?:json)?\s*', '', cleaned)
                cleaned = re.sub(r'\s*```$', '', cleaned)
            try:
                analysis = json.loads(cleaned)
            except json.JSONDecodeError:
                # Repair truncated JSON
                last_brace = cleaned.rfind('}')
                if last_brace > 0:
                    truncated = cleaned[:last_brace + 1]
                    open_brackets = truncated.count('[') - truncated.count(']')
                    if open_brackets > 0:
                        last_comma = truncated.rfind(',')
                        if last_comma > 0:
                            truncated = truncated[:last_comma] + (']' * open_brackets) + '}'
                    analysis = json.loads(truncated)
                    print("[insights] JSON repaired after truncation")
                else:
                    raise

        created = 0

        def _save_insight(itype, title, description=''):
            """Helper to create an insight. In dry_run does NOT write to DynamoDB."""
            nonlocal created
            if not dry_run:
                insights_table.put_item(Item={
                    'userId': user_id,
                    'insightId': f"{datetime.utcnow().isoformat()}#{uuid.uuid4().hex[:8]}",
                    'projectId': project_id,
                    'projectName': project_name,
                    'type': itype,
                    'title': title,
                    'description': description or '',
                    'status': 'created',
                    'createdAt': datetime.utcnow().isoformat(),
                })
            created += 1

        def _save_task(item_or_text, task_status, source_label=''):
            """Helper to create a real operational task.

            Accepts both a string (old format) and a dict {text, start_date, due_date}
            (new format with AI-estimated dates). Saving dates makes the task
            appear on the project's Gantt view from day 1.
            """
            try:
                # Support both formats so we don't break if the LLM returns the old one
                if isinstance(item_or_text, dict):
                    text = str(item_or_text.get('text', ''))[:500]
                    start_date = (item_or_text.get('start_date') or '').strip()
                    due_date = (item_or_text.get('due_date') or '').strip()
                    assigned_to = (item_or_text.get('assigned_to') or '').strip()
                else:
                    text = str(item_or_text)[:500]
                    start_date = ''
                    due_date = ''
                    assigned_to = ''
                if not text:
                    return
                if dry_run:
                    return
                tasks_table.put_item(Item={
                    'projectId': project_id,
                    'taskId': uuid.uuid4().hex,
                    'userId': user_id,
                    'text': text,
                    'status': task_status,  # pending, in_progress, completed, blocked
                    'createdBy': 'IA',
                    'assignedTo': assigned_to,
                    'startDate': start_date,
                    'dueDate': due_date,
                    'sourceLabel': source_label,
                    'createdAt': datetime.utcnow().isoformat(),
                })
            except Exception as e:
                print(f"[insights] Could not create operational task: {e}")

        # 1. Narrative summary (enriched description)
        if analysis.get('summary'):
            _save_insight('summary', 'Project Summary', analysis['summary'])

        # 2. Real project characterization
        if analysis.get('project_type_real'):
            _save_insight('project_characterization', analysis['project_type_real'],
                          f'Real project type detected by AI')

        # 3. Client profile
        if analysis.get('client_profile'):
            _save_insight('client_profile', analysis['client_profile'],
                          f'Client profile inferred by AI')

        # 4. Key insight (highlighted callout)
        if analysis.get('key_insight'):
            _save_insight('key_insight', analysis['key_insight'],
                          f'Strategic observation for the project')

        # Helper to extract the text from an item that can be a string or dict
        # (the AI now returns dicts with start_date/due_date for work_done/tasks/blockers)
        def _text_of(item):
            if isinstance(item, dict):
                return str(item.get('text', ''))
            return str(item)

        # 5. Work already done → also as COMPLETED tasks (status='done')
        for item in (analysis.get('work_done') or [])[:20]:
            _save_insight('work_done', _text_of(item), f'Work performed on {project_name}')
            _save_task(item, 'done', 'work_done')

        # 6. Pending tasks → also as PENDING tasks
        for task in (analysis.get('tasks') or [])[:30]:
            _save_insight('task_created', _text_of(task), f'Task auto-detected in {project_name}')
            _save_task(task, 'pending', 'task_created')

        # 7. General risks
        for risk in (analysis.get('risks') or [])[:15]:
            _save_insight('risk', str(risk), f'Risk identified in {project_name}')

        # 8. Client-specific blockers → also as BLOCKED tasks
        for blocker in (analysis.get('blockers') or [])[:15]:
            _save_insight('blocker', _text_of(blocker), f'Client blocker or dependency in {project_name}')
            _save_task(blocker, 'blocked', 'blocker')

        # 9. Decisions (made or pending)
        for decision in (analysis.get('decisions') or [])[:15]:
            _save_insight('decision', str(decision), f'Key decision for {project_name}')

        # 10. Metrics (hours, costs, deadlines)
        for metric in (analysis.get('metrics') or [])[:15]:
            _save_insight('metric', str(metric), f'Metric detected in {project_name}')

        # 11. Technical issues
        for issue in (analysis.get('tech_issues') or [])[:15]:
            _save_insight('tech_issue', str(issue), f'Technical issue identified in {project_name}')

        print(f"[insights] {created} insights created for {project_name} (with operational tasks synced)")
        return {"generated": True, "count": created, "analysis": analysis}

    except Exception as e:
        print(f"[insights] Error: {e}")
        import traceback; traceback.print_exc()
        return {"generated": False, "reason": str(e)}


def create_project_full(
    user_id: str,
    name: str,
    description: str = "",
    project_type: str = "Other",
    channels: list = None,
    participants: list = None,
    timing: str = "",
    delivery_date: str = "",
    analysis_text: str = ""
) -> dict:
    """
    Creates a full project with:
    - DynamoDB record
    - In-app notification "project created"
    - AI analysis → insights (summary, tasks, risks, decisions)
    - In-app notification "AI analyzed your project"

    Reusable function for the web flow (POST /api/projects) and the WhatsApp flow (agent tool).
    Returns {success, projectId, name, insightsGenerated}.
    """
    project_id = "proj-" + uuid.uuid4().hex[:8]
    now = datetime.utcnow().isoformat()
    channels = channels or ['Gmail']
    participants = participants or []

    # 0. Defensive dedupe — if the same user already created a project with
    # the same (normalized) name in the last 5 minutes, return that one
    # instead of creating another. Covers wizard double-clicks, retries after
    # network timeouts, duplicate tabs, etc. 5 min is a wide window without
    # blocking a legitimate user creating two similar projects back to back.
    try:
        from boto3.dynamodb.conditions import Attr
        name_norm = (name or '').strip().lower()
        cutoff = (datetime.utcnow() - timedelta(minutes=5)).isoformat()
        dupe_scan = projects_table.scan(
            FilterExpression=Attr('userId').eq(user_id) & Attr('createdAt').gt(cutoff),
            ProjectionExpression='projectId, #n, createdAt',
            ExpressionAttributeNames={'#n': 'name'},
        )
        for it in dupe_scan.get('Items', []):
            if (it.get('name') or '').strip().lower() == name_norm and name_norm:
                print(f"[create_project_full] Dedupe: {name!r} already created <5min ago as {it['projectId']} → returning existing")
                return {
                    "success": True,
                    "projectId": it['projectId'],
                    "name": name,
                    "description": description,
                    "insightsGenerated": {"generated": False, "count": 0, "deduped": True},
                }
    except Exception as e:
        print(f"[create_project_full] Dedupe check failed (non-blocking): {e}")

    # 1. Create project
    item = {
        'projectId': project_id,
        'userId': user_id,
        'name': name,
        'description': description,
        'type': project_type,
        'status': 'active',
        'participants': participants,
        'channels': channels,
        'timing': timing or '',
        'deliveryDate': delivery_date or '',
        'createdAt': now,
        'lastActivity': now,
    }
    projects_table.put_item(Item=item)
    print(f"[create_project_full] Project {project_id} created: {name}")

    # 2. Project-created notification
    try:
        notifications_table.put_item(Item={
            'userId': user_id,
            'notificationId': f"{now}#{uuid.uuid4().hex[:8]}",
            'projectId': project_id,
            'projectName': name,
            'type': 'project_created',
            'title': f'Project created: {name}',
            'message': f'Your project "{name}" was created with channels: {", ".join(channels) if channels else "none"}',
            'channel': 'system',
            'status': 'unread',
            'createdAt': now,
        })
    except Exception as e:
        print(f"[create_project_full] Error in project notification: {e}")

    # 3. Generate insights with AI
    insights_result = generate_insights_for_project(
        user_id=user_id,
        project_id=project_id,
        project_name=name,
        project_type=project_type,
        description=description,
        participants=participants,
        participants_count=len(participants),
        analysis_text=analysis_text,  # original full text if the caller has it
    )

    # 3.5 If the original description is long (looks like chat / raw text) and
    # the AI produced an executive summary, we replace the project description
    # with ONLY the summary (the other categories —Characterization, Client
    # profile, Key insight— live in the sidebar as separate insights, so we
    # avoid redundancy). We keep the original as `originalDescription` for
    # audit if needed.
    final_description = description
    try:
        analysis = (insights_result or {}).get('analysis') or {}
        summary = analysis.get('summary', '').strip() if isinstance(analysis, dict) else ''
        # Heuristic to detect raw / long text:
        # - more than 250 characters
        # - OR contains WhatsApp-style timestamp patterns ("12/11/25, 13:03 -")
        is_long = len(description or '') > 250
        looks_like_chat = bool(re.search(r'\d{1,2}[/-]\d{1,2}[/-]\d{2,4}.{0,12}\d{1,2}:\d{2}.{0,5}-', description or ''))
        if summary and (is_long or looks_like_chat):
            # ONLY the pure summary as description. Clean and non-redundant.
            try:
                projects_table.update_item(
                    Key={'projectId': project_id},
                    UpdateExpression='SET description = :d, originalDescription = :o',
                    ExpressionAttributeValues={
                        ':d': summary,
                        ':o': description,
                    }
                )
                final_description = summary
                print(f"[create_project_full] Description replaced with AI summary ({len(summary)} chars). Original stored in originalDescription.")
            except Exception as e:
                print(f"[create_project_full] Error updating description: {e}")
    except Exception as e:
        print(f"[create_project_full] Error in description post-processing: {e}")

    # 4. Insights-generated notification
    if insights_result.get('generated') and insights_result.get('count', 0) > 0:
        try:
            notifications_table.put_item(Item={
                'userId': user_id,
                'notificationId': f"{datetime.utcnow().isoformat()}#{uuid.uuid4().hex[:8]}",
                'projectId': project_id,
                'projectName': name,
                'type': 'insights_generated',
                'title': f'AI analyzed your project: {insights_result["count"]} insights',
                'message': f'We automatically generated {insights_result["count"]} insights for "{name}". Check the Intelligence panel.',
                'channel': 'system',
                'status': 'unread',
                'createdAt': datetime.utcnow().isoformat(),
            })
        except Exception as e:
            print(f"[create_project_full] Error in insights notification: {e}")

    # 5. Notify via WhatsApp to all participants with a phone + to the owner if they have a linked number
    try:
        import threading
        threading.Thread(
            target=_notify_whatsapp_async,
            args=(user_id, project_id, name, project_type, final_description, participants, insights_result, channels),
            daemon=True
        ).start()
    except Exception as e:
        print(f"[create_project_full] Could not start WhatsApp notification: {e}")

    # 6. Email invitation to each participant with an email. Without this,
    # emails detected by the AI (or added manually in the wizard) do NOT
    # receive the link to join and remain only as a record in participants[].
    # We use the _invite_by_email helper which silently creates the Cognito
    # account + saves the invitation in onebox-invitations + sends the custom
    # email via SES. Runs in the background so we don't block the create response.
    try:
        import threading
        emails_to_invite = []
        seen_inv_emails = set()
        for p in (participants or []):
            if not isinstance(p, dict):
                continue
            em = (p.get('email') or '').strip().lower()
            if em and '@' in em and em not in seen_inv_emails:
                emails_to_invite.append(em)
                seen_inv_emails.add(em)
        if emails_to_invite:
            threading.Thread(
                target=_invite_by_email_async,
                args=(user_id, project_id, name, emails_to_invite),
                daemon=True,
            ).start()
    except Exception as e:
        print(f"[create_project_full] Could not start email invitation: {e}")

    return {
        "success": True,
        "projectId": project_id,
        "name": name,
        "description": final_description,
        "insightsGenerated": insights_result
    }


def _invite_by_email_async(owner_uid: str, project_id: str, project_name: str, emails: list):
    """Runs in the background: for each email, invokes _invite_by_email to
    silently create the Cognito account + save the pending invitation + send
    the custom email with the project link. If one fails, we continue with
    the rest (log and continue)."""
    try:
        # Deferred import to avoid circular dependencies in the module.
        from api.services.projects import _invite_by_email
        for em in emails:
            try:
                _invite_by_email(email=em, uid=owner_uid, project_id=project_id, project_name=project_name)
            except Exception as e:
                print(f"[_invite_by_email_async] failed for {em}: {e}")
    except Exception as e:
        print(f"[_invite_by_email_async] global failure: {e}")


def _notify_whatsapp_async(
    owner_user_id: str,
    project_id: str,
    project_name: str,
    project_type: str,
    description: str,
    participants: list,
    insights_result: dict,
    channels: list
):
    """Sends WhatsApp messages in the background to participants and the owner when a project is created.
    Only sends if the WhatsApp channel is included in the project, or if the participant
    is explicitly marked as a WhatsApp contact (has a phone number).
    """
    try:
        from agent.tools import send_notification
        import boto3 as _boto3_local

        # Determine unique recipients by phone number
        recipients = []  # list of {name, phone, isOwner}
        seen_phones = set()

        # Participants with a phone number
        for p in (participants or []):
            phone = (p.get('phone') or '').strip()
            if not phone or phone in seen_phones:
                continue
            seen_phones.add(phone)
            recipients.append({
                'name': p.get('name') or 'Team',
                'phone': phone if phone.startswith('+') else '+' + phone,
                'isOwner': False,
            })

        # Look up the owner's linked phone (onebox-user-phones table)
        try:
            dynamodb_local = _boto3_local.resource('dynamodb', region_name=os.environ.get('AWS_REGION', 'us-east-1'))
            phones_table = dynamodb_local.Table('onebox-user-phones')
            scan_res = phones_table.scan(
                FilterExpression='userId = :u',
                ExpressionAttributeValues={':u': owner_user_id}
            )
            for item in scan_res.get('Items', []):
                p = item.get('phoneNumber', '')
                if p and p not in seen_phones:
                    seen_phones.add(p)
                    recipients.append({
                        'name': item.get('name') or 'You',
                        'phone': p if p.startswith('+') else '+' + p,
                        'isOwner': True,
                    })
        except Exception as e:
            print(f"[_notify_whatsapp] error fetching owner phone: {e}")

        if not recipients:
            print(f"[_notify_whatsapp] {project_id}: no recipients with a phone number, skipping")
            return

        # Compose message
        ig_count = 0
        analysis = {}
        if insights_result and insights_result.get('generated'):
            ig_count = insights_result.get('count', 0)
            analysis = insights_result.get('analysis', {}) or {}

        n_tasks = len(analysis.get('tasks') or [])
        n_risks = len(analysis.get('risks') or [])
        n_decisions = len(analysis.get('decisions') or [])

        # Trim description for the message
        desc_short = (description or '').strip()
        if len(desc_short) > 200:
            desc_short = desc_short[:200] + '...'

        for r in recipients:
            try:
                if r['isOwner']:
                    header = f"✅ *Project created:* {project_name}"
                    role_line = "You're the project owner."
                else:
                    header = f"📋 *You've been added to a new project:* {project_name}"
                    role_line = f"Hi {r['name']}, you've been added to the team."

                lines = [header, '', role_line]
                if project_type and project_type not in ('Other', 'Otro'):
                    lines.append(f"📁 Type: *{project_type}*")
                if desc_short:
                    lines.append('')
                    lines.append(f"_{desc_short}_")

                if ig_count > 0:
                    lines.append('')
                    lines.append(f"🤖 AI analyzed the project and generated *{ig_count} insights*:")
                    if n_tasks: lines.append(f"  • {n_tasks} tasks detected")
                    if n_risks: lines.append(f"  • {n_risks} risks identified")
                    if n_decisions: lines.append(f"  • {n_decisions} key decisions")

                if channels:
                    lines.append('')
                    lines.append(f"📡 Channels: {', '.join(channels)}")

                lines.append('')
                lines.append("📊 See it at https://www.oneboxmanager.com")
                lines.append('')
                lines.append("_Automated message from OneBox_")

                message = "\n".join(lines)

                send = send_notification(
                    recipient=r['phone'],
                    message=message,
                    channel='whatsapp',
                    project_id=project_id,
                    project_name=project_name
                )
                if send.get('success'):
                    print(f"[_notify_whatsapp] sent to {r['phone']} ({'owner' if r['isOwner'] else r['name']})")
                else:
                    print(f"[_notify_whatsapp] failed sending to {r['phone']}: {send.get('error', 'unknown')}")
            except Exception as e:
                print(f"[_notify_whatsapp] exception sending to {r['phone']}: {e}")

        print(f"[_notify_whatsapp] {project_id}: {len(recipients)} recipient(s) processed")

    except Exception as e:
        print(f"[_notify_whatsapp_async] General error: {e}")
        import traceback; traceback.print_exc()
