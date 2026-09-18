"""Documents and text internal logic: AI analysis, drafts and project
creation from documents/pasted text."""
import uuid
from datetime import datetime

from fastapi import HTTPException

from agent.tools import notifications_table
from api.deps import attachments_table


def save_attachment_record(project_id: str, user_id: str, file_name: str,
                           file_size: int, content_type: str, ext: str,
                           s3_key: str, extracted_text: str = "",
                           source: str = "web",
                           uploaded_by: str = "",
                           uploaded_by_email: str = "") -> dict:
    """Save attachment metadata in DynamoDB.
    user_id: ALWAYS the project owner's sub (for consistency with the rest
             of the items associated with the project).
    uploaded_by / uploaded_by_email: who uploaded the file (may be owner or
             invited user). For traceability."""
    now = datetime.utcnow().isoformat()
    attachment_id = f"{now}#{uuid.uuid4().hex[:8]}"
    item = {
        'projectId': project_id,
        'attachmentId': attachment_id,
        'userId': user_id,
        'fileName': file_name,
        'fileSize': file_size,
        'contentType': content_type,
        'extension': ext,
        's3Key': s3_key,
        'extractedTextPreview': (extracted_text or '')[:500],
        'extractedTextLength': len(extracted_text or ''),
        'source': source,
        'createdAt': now,
    }
    if uploaded_by:
        item['uploadedBy'] = uploaded_by
    if uploaded_by_email:
        item['uploadedByEmail'] = uploaded_by_email
    attachments_table.put_item(Item=item)
    return item


def analyze_text_preview(uid: str, text: str, source: str) -> dict:
    """Analyze pasted text WITHOUT creating a project.
    Uses only the agent's planner (no executor): zero DynamoDB writes,
    no dry-run. Returns draftId + suggestion with participants and assigned tasks."""
    from agent.document_parser import upload_to_s3
    from agent.graph.llm_factory import create_llm
    from agent.graph.nodes.planner.node import planner_node

    text = (text or '').strip()
    if len(text) < 30:
        raise HTTPException(status_code=400, detail="Text is too short. Paste at least a conversation or a paragraph.")

    # Invoke only the planner (no executor, no DynamoDB)
    planner_llm = create_llm("planner")
    planner_state = {
        "user_message": f"Create a project from this conversation or text:\n\n{text}",
        "resolved_message": None,
        "history": [],
        "plan": [],
        "results": {},
        "tools_used": [],
        "validation_feedback": None,
        "iteration": 0,
        "status": "planning",
        "response": "",
        "direct_response": None,
        "debug_mode": False,
        "session_id": f"preview-{uid}",
        "intent_draft": None,
        "debug_info": {},
    }
    planner_result = None
    for attempt in range(3):
        try:
            planner_result = planner_node(planner_state, planner_llm)
            break
        except Exception as e:
            print(f"[analyze_text_preview] Planner attempt {attempt + 1}/3 failed: {e}")
    if planner_result is None:
        raise HTTPException(status_code=500, detail="Could not analyze the text. Try again.")

    # Extract data from the plan
    project_name, project_type, description = "", "Other", ""
    detected_participants, tasks = [], []

    plan = (planner_result or {}).get("plan") or []
    for step in plan:
        tool = step.get("tool", "")
        params = step.get("params", {})
        if tool == "create_project":
            project_name = params.get("name", "")
            project_type = params.get("type", "Other")
            description = params.get("description", "")
            for p in (params.get("participants") or []):
                if isinstance(p, dict):
                    detected_participants.append({
                        "name":  p.get("name", ""),
                        "role":  p.get("role", ""),
                        "email": p.get("email", ""),
                        "phone": p.get("phone", ""),
                    })
        elif tool == "create_task":
            task_text = params.get("text", "")
            if task_text and isinstance(task_text, str):
                tasks.append({
                    "text":        task_text,
                    "assigned_to": params.get("assigned_to", ""),
                    "start_date":  params.get("start_date", ""),
                    "due_date":    params.get("due_date", ""),
                    "status":      params.get("status", "pending"),
                })

    # Save as .txt draft in S3 + DynamoDB
    draft_id = uuid.uuid4().hex
    source = source or 'paste'
    now = datetime.utcnow().isoformat()
    file_name = f"pasted-text-{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}.txt"
    text_bytes = text.encode('utf-8')
    try:
        s3_key = upload_to_s3(text_bytes, f"_drafts/{uid}", file_name, 'text/plain')
        attachments_table.put_item(Item={
            'projectId': f'_draft#{uid}',
            'attachmentId': draft_id,
            'userId': uid,
            'fileName': file_name,
            'fileSize': len(text_bytes),
            'contentType': 'text/plain',
            'extension': 'txt',
            's3Key': s3_key,
            'extractedTextPreview': text[:5000],
            'extractedTextLength': len(text),
            'source': f'web_draft_{source}',
            'createdAt': now,
        })
    except Exception as e:
        print(f"[analyze_text_preview] Error saving draft: {e}")

    return {
        "draftId": draft_id,
        "fileName": file_name,
        "fileSize": len(text_bytes),
        "extractedTextLength": len(text),
        "suggestion": {
            "name":                  project_name,
            "type":                  project_type,
            "description":           description,
            "extractedNotes":        "",
            "detected_participants": detected_participants,
            "tasks":                 tasks,
        },
    }


def analyze_text_insights_dryrun(uid: str, text: str) -> dict:
    """DRY-RUN of insight generation: runs the real LLM on the FULL text but
    does NOT write anything to DynamoDB (no project, insights or tasks).
    Useful to verify AI quality/depth without creating data.
    Returns the raw analysis output (summary, real type, profile, tasks, etc.)."""
    text = (text or '').strip()
    if len(text) < 30:
        raise HTTPException(status_code=400, detail="Text is too short. Paste at least a conversation or a paragraph.")

    from agent.project_helpers import generate_insights_for_project
    result = generate_insights_for_project(
        user_id=uid,
        project_id="_dryrun",
        project_name="(dry-run)",
        project_type="Other",
        description="",
        participants_count=0,
        analysis_text=text,
        dry_run=True,
    )
    analysis = result.get("analysis", {}) or {}
    return {
        "dryRun": True,
        "generated": result.get("generated", False),
        "reason": result.get("reason"),
        "insightCount": result.get("count", 0),
        "analysis": analysis,
    }


def analyze_document_preview(uid: str, file_bytes: bytes, file_name: str, content_type: str) -> dict:
    """Analyze a document (extract text + suggest metadata) WITHOUT creating the project.
    Uses the agent's planner to obtain participants with roles and assigned tasks.
    Returns a draft_id later used in /api/projects/from-document-draft."""
    from agent.document_parser import extract_text, upload_to_s3, validate_file
    from agent.graph.llm_factory import create_llm
    from agent.graph.nodes.planner.node import planner_node

    valid, ext, error = validate_file(file_bytes, file_name or '', content_type or '')
    if not valid:
        raise HTTPException(status_code=400, detail=error)

    text = extract_text(file_bytes, ext)
    if not text or len(text.strip()) < 20:
        raise HTTPException(status_code=400, detail="Could not extract text from the document or it is too brief.")

    # Invoke only the planner (no executor, no DynamoDB)
    planner_llm = create_llm("planner")
    planner_state = {
        "user_message": f"Create a project from this document:\n\n{text[:50000]}",
        "resolved_message": None,
        "history": [],
        "plan": [],
        "results": {},
        "tools_used": [],
        "validation_feedback": None,
        "iteration": 0,
        "status": "planning",
        "response": "",
        "direct_response": None,
        "debug_mode": False,
        "session_id": f"docpreview-{uid}",
        "intent_draft": None,
        "debug_info": {},
    }

    planner_result = None
    for attempt in range(3):
        try:
            planner_result = planner_node(planner_state, planner_llm)
            break
        except Exception as e:
            print(f"[analyze_document_preview] Planner attempt {attempt + 1}/3 failed: {e}")
    if planner_result is None:
        raise HTTPException(status_code=500, detail="Could not analyze the document. Try again.")

    # Extract data from the plan
    project_name, project_type, description = "", "Other", ""
    detected_participants, tasks = [], []

    for step in (planner_result.get("plan") or []):
        tool = step.get("tool", "")
        params = step.get("params", {})
        if tool == "create_project":
            project_name = params.get("name", "")
            project_type = params.get("type", "Other")
            description = params.get("description", "")
            for p in (params.get("participants") or []):
                if isinstance(p, dict):
                    detected_participants.append({
                        "name":  p.get("name", ""),
                        "role":  p.get("role", ""),
                        "email": p.get("email", ""),
                        "phone": p.get("phone", ""),
                    })
        elif tool == "create_task":
            task_text = params.get("text", "")
            if task_text and isinstance(task_text, str):
                tasks.append({
                    "text":        task_text,
                    "assigned_to": params.get("assigned_to", ""),
                    "start_date":  params.get("start_date", ""),
                    "due_date":    params.get("due_date", ""),
                    "status":      params.get("status", "pending"),
                })

    # Upload the file to a "draft" area in S3 for later confirmation
    draft_id = uuid.uuid4().hex
    s3_key = upload_to_s3(file_bytes, f"_drafts/{uid}", file_name or f'doc.{ext}', content_type or '')

    attachments_table.put_item(Item={
        'projectId': f'_draft#{uid}',
        'attachmentId': draft_id,
        'userId': uid,
        'fileName': file_name or f'doc.{ext}',
        'fileSize': len(file_bytes),
        'contentType': content_type or '',
        'extension': ext,
        's3Key': s3_key,
        'extractedTextPreview': text[:5000],
        'extractedTextLength': len(text),
        'source': 'web_draft',
        'createdAt': datetime.utcnow().isoformat(),
    })

    return {
        "draftId": draft_id,
        "fileName": file_name or f'doc.{ext}',
        "fileSize": len(file_bytes),
        "extractedTextLength": len(text),
        "suggestion": {
            "name":                  project_name,
            "type":                  project_type,
            "description":           description,
            "extractedNotes":        "",
            "detected_participants": detected_participants,
            "tasks":                 tasks,
        },
    }


def create_project_from_draft(uid: str, req) -> dict:
    """Create the final project from a previously analyzed draft.
    Moves the file from the draft folder to the project folder and records the attachment."""
    from agent.document_parser import S3_ATTACHMENTS_BUCKET, get_s3_client
    from agent.project_helpers import create_project_full

    # Retrieve the draft
    draft = attachments_table.get_item(
        Key={'projectId': f'_draft#{uid}', 'attachmentId': req.draftId}
    ).get('Item')
    if not draft:
        raise HTTPException(status_code=404, detail="Draft not found or expired")

    # Minimum validations
    name = (req.name or '').strip()
    if not name:
        raise HTTPException(status_code=400, detail="Project name is required")
    if not req.channels or len(req.channels) == 0:
        raise HTTPException(status_code=400, detail="Select at least one channel")

    # Build participants
    participants = []
    seen_emails = set()
    seen_phones = set()

    # 1. AI-detected participants (preserve the original name: Kevin, Mateo...)
    for p in (req.detectedParticipants or []):
        if not isinstance(p, dict):
            continue
        pname = (p.get('name') or '').strip()[:80]
        pemail = (p.get('email') or '').strip().lower()
        pphone_raw = (p.get('phone') or '').strip()
        prole = (p.get('role') or 'Participant').strip()[:80]
        pphone = ''
        if pphone_raw:
            pphone = pphone_raw if pphone_raw.startswith('+') else '+' + pphone_raw.replace(' ', '').replace('-', '')
        # Only add if there is a name and at least one contact channel (or just a name as reference)
        if pname or pemail or pphone:
            participants.append({
                'name': pname or (pemail.split('@')[0] if pemail else pphone),
                'email': pemail if '@' in pemail else '',
                'phone': pphone,
                'role': prole or 'Participant'
            })
            if pemail and '@' in pemail:
                seen_emails.add(pemail)
            if pphone:
                seen_phones.add(pphone)

    # 2. Loose emails added manually (not coming from detected list)
    for email in (req.emails or []):
        e = (email or '').strip().lower()
        if e and '@' in e and e not in seen_emails:
            participants.append({
                'name': e.split('@')[0],
                'email': e,
                'phone': '',
                'role': 'Email Contact'
            })
            seen_emails.add(e)

    # 3. Loose phones added manually
    for phone in (req.phones or []):
        pclean = (phone or '').strip()
        if pclean:
            formatted = pclean if pclean.startswith('+') else '+' + pclean
            if formatted not in seen_phones:
                participants.append({
                    'name': formatted,
                    'email': '',
                    'phone': formatted,
                    'role': 'WhatsApp Contact'
                })
                seen_phones.add(formatted)

    # Retrieve the FULL ORIGINAL TEXT of the draft for the insights analysis,
    # so the deep analysis sees the real material and not the short description.
    # Priority: sourceText from the request (if the frontend forwards it) → the
    # draft text saved in S3 by analyze_text_preview → (fallback) the description.
    full_text = (req.sourceText or '').strip()
    if not full_text and req.draftId:
        try:
            from agent.document_parser import get_s3_client, S3_ATTACHMENTS_BUCKET
            draft_item = attachments_table.get_item(
                Key={'projectId': f'_draft#{uid}', 'attachmentId': req.draftId}
            ).get('Item') or {}
            s3_key = draft_item.get('s3Key')
            if s3_key:
                obj = get_s3_client().get_object(Bucket=S3_ATTACHMENTS_BUCKET, Key=s3_key)
                full_text = obj['Body'].read().decode('utf-8', errors='replace')
        except Exception as e:
            print(f"[from_draft] Could not retrieve full text of draft {req.draftId}: {e}")

    # Create the project with the info reviewed by the user.
    result = create_project_full(
        user_id=uid,
        name=name,
        description=req.description or '',
        project_type=req.type or 'Other',
        channels=req.channels,
        participants=participants,
        timing=req.timing or '',
        delivery_date=req.deliveryDate or '',
        analysis_text=full_text,
    )
    project_id = result['projectId']

    # Move the draft file to the final project folder
    try:
        s3 = get_s3_client()
        old_key = draft['s3Key']
        # New key with the normal project structure
        new_key = old_key.replace(f'projects/_drafts/{uid}', f'projects/{project_id}/{datetime.utcnow().strftime("%Y%m%d")}')
        s3.copy_object(
            Bucket=S3_ATTACHMENTS_BUCKET,
            CopySource={'Bucket': S3_ATTACHMENTS_BUCKET, 'Key': old_key},
            Key=new_key,
            ServerSideEncryption='AES256'
        )
        s3.delete_object(Bucket=S3_ATTACHMENTS_BUCKET, Key=old_key)

        # Record the final attachment
        save_attachment_record(
            project_id=project_id,
            user_id=uid,
            file_name=draft.get('fileName', 'document'),
            file_size=int(draft.get('fileSize', 0)),
            content_type=draft.get('contentType', ''),
            ext=draft.get('extension', ''),
            s3_key=new_key,
            extracted_text=draft.get('extractedTextPreview', ''),
            source='web'
        )

        # Delete the draft record
        attachments_table.delete_item(
            Key={'projectId': f'_draft#{uid}', 'attachmentId': req.draftId}
        )
    except Exception as e:
        print(f"[from_draft] Error moving draft: {e}")
        import traceback; traceback.print_exc()
        # Don't fail creation if the move fails

    return result


def create_project_from_document(uid: str, file_bytes: bytes, file_name: str,
                                 content_type: str, name: str, channels: str) -> dict:
    """Create a project from a document. The AI infers name, type
    and description if not provided. The document is attached to the project."""
    from agent.document_parser import (
        analyze_document_for_project, extract_text, upload_to_s3, validate_file
    )
    from agent.project_helpers import create_project_full

    valid, ext, error = validate_file(file_bytes, file_name or '', content_type or '')
    if not valid:
        raise HTTPException(status_code=400, detail=error)

    # Extract text
    print(f"[from_document] Extracting text from {file_name} ({len(file_bytes)} bytes, ext={ext})")
    text = extract_text(file_bytes, ext)
    if not text or len(text.strip()) < 20:
        raise HTTPException(status_code=400, detail="Could not extract text from the document or it is too brief.")
    print(f"[from_document] Extracted text: {len(text)} characters")

    # Analyze with AI if no name/type was provided
    analysis = analyze_document_for_project(text, fallback_name=name or '')
    project_name = (name or analysis['name']).strip()[:80]
    project_type = analysis['type']
    description = analysis['description']
    if analysis.get('extractedNotes'):
        description += "\n\nNotes: " + analysis['extractedNotes']

    # Parse channels
    channel_list = []
    if channels:
        channel_list = [c.strip() for c in channels.split(',') if c.strip()]
    if not channel_list:
        channel_list = ['Gmail']

    # Create project + insights. We pass the FULL document text so the insights
    # analysis sees the real material and not just the short description.
    result = create_project_full(
        user_id=uid,
        name=project_name,
        description=description,
        project_type=project_type,
        channels=channel_list,
        participants=[],
        analysis_text=text,
    )
    project_id = result['projectId']

    # Upload file to S3
    s3_key = upload_to_s3(file_bytes, project_id, file_name or f'doc.{ext}', content_type or '')

    # Record attachment in DynamoDB
    att = save_attachment_record(
        project_id=project_id,
        user_id=uid,
        file_name=file_name or f'doc.{ext}',
        file_size=len(file_bytes),
        content_type=content_type or '',
        ext=ext,
        s3_key=s3_key,
        extracted_text=text,
        source='web'
    )

    result['attachment'] = {
        'attachmentId': att['attachmentId'],
        'fileName': att['fileName'],
        'fileSize': att['fileSize'],
    }
    result['analysis'] = analysis
    return result


def create_project_from_text(uid: str, text: str, name: str, channels, source: str) -> dict:
    """Create a project from pasted text by delegating to the full agent.
    Same depth as the chat: real participants, tasks with assigned_to and dates."""
    from agent.graph import run_agent
    from agent.tools import clear_current_user, set_current_user

    text = (text or '').strip()
    if len(text) < 30:
        raise HTTPException(status_code=400, detail="Text is too short. Paste at least a complete conversation or a descriptive paragraph.")

    message = f"Create a project from this conversation or text:\n\n{text}"
    if name:
        message = f"Create a project called '{name}' from this conversation or text:\n\n{text}"

    set_current_user(uid)
    try:
        agent_result = run_agent(message, history=[], debug_mode=False, session_id=f"fromtext-{uid}")
    finally:
        clear_current_user()

    # Extract real projectId from the agent results
    project_id = None
    project_name = name or ""
    for step_result in (agent_result.get("results") or {}).values():
        if isinstance(step_result, dict) and step_result.get("projectId"):
            project_id = step_result["projectId"]
            project_name = step_result.get("name", project_name)
            break

    # Save the text as a .txt attachment in the created project
    if project_id:
        try:
            from agent.document_parser import upload_to_s3
            fname = f"pasted-text-{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}.txt"
            s3_key = upload_to_s3(text.encode('utf-8'), project_id, fname, 'text/plain')
            save_attachment_record(
                project_id=project_id,
                user_id=uid,
                file_name=fname,
                file_size=len(text.encode('utf-8')),
                content_type='text/plain',
                ext='txt',
                s3_key=s3_key,
                extracted_text=text,
                source=source or 'paste'
            )
        except Exception as e:
            print(f"[from_text] Error saving attachment: {e}")

    return {
        "success": True,
        "projectId": project_id,
        "name": project_name,
        "response": agent_result.get("response", ""),
        "tools_used": agent_result.get("tools_used", []),
    }


def analyze_text_for_project(uid: str, project_id: str, text: str, source: str,
                              user_email: str = "") -> dict:
    """Analyze pasted text within an existing project.
    Generates updated insights (tasks, risks, decisions) without creating a new project.

    Allows both the owner and invited users with access to the project to
    paste text and generate insights. Previously only the owner could, which
    was inconsistent with upload_attachment (which does allow invited users).
    """
    from agent.document_parser import upload_to_s3
    from agent.project_helpers import generate_insights_for_project
    from api.services.access import has_project_access

    has, _is_owner, existing = has_project_access(uid, user_email, project_id)
    if not existing:
        raise HTTPException(status_code=404, detail="Project not found")
    if not has:
        raise HTTPException(status_code=403, detail="No access to this project")

    # Insights and the attachment are saved under the owner (not the invited
    # user who pasted the text) so that traceability works with the filter by
    # projectId in /api/projects.
    owner_uid = existing.get('userId', uid)

    text = (text or '').strip()
    if len(text) < 30:
        raise HTTPException(status_code=400, detail="Text is too short.")

    # Save the text as a .txt "attachment"
    fname = f"pasted-text-{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}.txt"
    try:
        s3_key = upload_to_s3(text.encode('utf-8'), project_id, fname, 'text/plain')
        save_attachment_record(
            project_id=project_id,
            user_id=owner_uid,
            file_name=fname,
            file_size=len(text.encode('utf-8')),
            content_type='text/plain',
            ext='txt',
            s3_key=s3_key,
            extracted_text=text,
            source=source or 'paste'
        )
    except Exception as e:
        print(f"[analyze_text] Error saving text: {e}")

    # Generate insights with the AI — the insights are associated with the
    # owner so they appear in the owner's view AND the invited users' view
    # (the projectId filter in /api/projects shows them to everyone with access).
    insights_result = generate_insights_for_project(
        user_id=owner_uid,
        project_id=project_id,
        project_name=existing.get('name', 'Project'),
        project_type=existing.get('type', 'Other'),
        description=text[:5000],
        participants=existing.get('participants', []),
    )

    # In-app notification (kept in the owner's feed)
    if insights_result.get('generated') and insights_result.get('count', 0) > 0:
        try:
            notifications_table.put_item(Item={
                'userId': owner_uid,
                'notificationId': f"{datetime.utcnow().isoformat()}#{uuid.uuid4().hex[:8]}",
                'projectId': project_id,
                'projectName': existing.get('name', 'Project'),
                'type': 'text_analyzed',
                'title': f'Text analyzed: {insights_result["count"]} insights',
                'message': f'The AI analyzed the pasted text and generated {insights_result["count"]} updated insights.',
                'channel': 'system',
                'status': 'unread',
                'createdAt': datetime.utcnow().isoformat(),
            })
        except Exception as e:
            print(f"[analyze_text] notif error: {e}")

    return {
        "success": True,
        "insightsGenerated": insights_result,
        "savedAs": fname
    }
