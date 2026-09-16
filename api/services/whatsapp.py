"""WhatsApp/Twilio channel internal logic: per-number sessions, outbound replies
and inbound webhook processing (wizard, media and AI agent)."""
import os
from datetime import datetime, timedelta
from urllib.parse import parse_qs

from agent.graph import run_agent
from agent.tools import conversations_table, set_current_user
from api.deps import sessions_table
from api.services.documents import save_attachment_record
from api.services.phones import auto_link_phone, lookup_user_by_phone

SESSION_TIMEOUT_HOURS = 2
MAX_HISTORY = 10


def send_whatsapp_reply(to_number: str, message: str):
    """Send a WhatsApp reply using the Twilio API."""
    try:
        from twilio.rest import Client
        sid = os.environ.get('TWILIO_ACCOUNT_SID', '')
        token = os.environ.get('TWILIO_AUTH_TOKEN', '')
        wa_number = os.environ.get('TWILIO_WHATSAPP_NUMBER', 'whatsapp:+14155238886')
        client = Client(sid, token)
        client.messages.create(body=message, from_=wa_number, to=to_number)
        print(f"[Webhook] Reply sent to {to_number}")
    except Exception as e:
        print(f"[Webhook] Error sending reply: {e}")


def get_session(phone_number: str) -> dict:
    try:
        result = sessions_table.get_item(Key={'phoneNumber': phone_number})
        session = result.get('Item')
        if session:
            last_activity = session.get('lastActivity', '')
            if last_activity:
                last_time = datetime.fromisoformat(last_activity)
                if datetime.utcnow() - last_time > timedelta(hours=SESSION_TIMEOUT_HOURS):
                    return create_session(phone_number)
            return session
        return create_session(phone_number)
    except Exception:
        return create_session(phone_number)


def create_session(phone_number: str) -> dict:
    session = {
        'phoneNumber': phone_number,
        'activeProjectId': '',
        'activeProjectName': '',
        'history': [],
        'lastActivity': datetime.utcnow().isoformat(),
        'createdAt': datetime.utcnow().isoformat()
    }
    sessions_table.put_item(Item=session)
    return session


def update_session(phone_number, message, response, project_id='', project_name=''):
    try:
        session = get_session(phone_number)
        history = session.get('history', [])
        history.append({'role': 'user', 'content': message})
        history.append({'role': 'assistant', 'content': response})
        if len(history) > MAX_HISTORY * 2:
            history = history[-(MAX_HISTORY * 2):]

        update_expr = "SET #h = :history, lastActivity = :now"
        expr_values = {':history': history, ':now': datetime.utcnow().isoformat()}
        expr_names = {'#h': 'history'}
        if project_id:
            update_expr += ", activeProjectId = :pid, activeProjectName = :pname"
            expr_values[':pid'] = project_id
            expr_values[':pname'] = project_name

        sessions_table.update_item(
            Key={'phoneNumber': phone_number},
            UpdateExpression=update_expr,
            ExpressionAttributeValues=expr_values,
            ExpressionAttributeNames=expr_names
        )
    except Exception as e:
        print(f"[Session] Error: {e}")


def build_context(session, new_message):
    parts = []
    active = session.get('activeProjectId', '')
    name = session.get('activeProjectName', '')
    if active:
        parts.append(f"[CONTEXT: the user is talking about project '{name}' (ID: {active}). "
                     f"If the message refers to this project, use it. If they are talking about something new, create a new one.]")
    parts.append(new_message)
    return "\n".join(parts)


def extract_project(response_text, tools_used):
    import re
    if any(t in tools_used for t in ['create_project', 'list_projects', 'get_project_contacts']):
        id_match = re.search(r'proj-[a-f0-9]+', response_text)
        name_match = re.search(r'\*\*(.+?)\*\*', response_text)
        return (id_match.group(0) if id_match else '', name_match.group(1) if name_match else '')
    return ('', '')


def handle_twilio_webhook(body_raw: str) -> dict:
    """Process an inbound Twilio message (WhatsApp/SMS): wizard, media or AI agent."""
    import threading
    from agent.whatsapp_wizard import STEP_IDLE, get_flow_state, handle_wizard

    params = parse_qs(body_raw)

    from_number = params.get('From', [''])[0]
    message_body = params.get('Body', [''])[0]
    message_sid = params.get('MessageSid', [''])[0]
    num_media = int(params.get('NumMedia', ['0'])[0])

    channel = 'whatsapp' if from_number.startswith('whatsapp:') else 'sms'
    clean_number = from_number.replace('whatsapp:', '')

    if message_body.strip().lower().startswith('join'):
        print(f"[Webhook] sandbox join message from {clean_number}, ignoring")
        return {"status": "ok", "action": "join_ignored"}

    # Check whether the number is already linked to a user
    user_info = lookup_user_by_phone(clean_number)

    # =================================================================
    # Is there a file attached? Process directly
    # =================================================================
    if num_media > 0:
        media_url = params.get('MediaUrl0', [''])[0]
        media_ct = params.get('MediaContentType0', [''])[0]
        if not user_info:
            send_whatsapp_reply(
                from_number,
                "📎 I received your file, but your number is not linked to a OneBox account.\n\n"
                "To create projects from documents, first link your number:\n"
                "1️⃣ Sign in at oneboxmanager.com\n"
                "2️⃣ Go to your profile → link your number\n\n"
                "Or type *create project* to verify by email and create one from scratch."
            )
            return {"status": "ok", "action": "media_no_account"}

        # Process the file in background so the webhook is not blocked
        def _process_media():
            try:
                from agent.document_parser import (
                    analyze_document_for_project, download_from_twilio,
                    extract_text, upload_to_s3, validate_file
                )
                from agent.project_helpers import create_project_full

                sid = os.environ.get('TWILIO_ACCOUNT_SID', '')
                tok = os.environ.get('TWILIO_AUTH_TOKEN', '')
                file_bytes, ct, fname = download_from_twilio(media_url, sid, tok)
                if not file_bytes:
                    send_whatsapp_reply(from_number, "⚠️ I could not download the file. Try again or upload it from the web.")
                    return

                valid, ext, error = validate_file(file_bytes, fname, ct or media_ct)
                if not valid:
                    send_whatsapp_reply(from_number, f"⚠️ {error}")
                    return

                text = extract_text(file_bytes, ext)
                if not text or len(text.strip()) < 30:
                    send_whatsapp_reply(from_number, "⚠️ I could not extract enough text from the file. Make sure it is not scanned or protected.")
                    return

                send_whatsapp_reply(from_number, f"📄 Document received ({len(file_bytes)//1024} KB).\n🤖 Analyzing with AI...")

                analysis = analyze_document_for_project(text)
                description = analysis['description']
                if analysis.get('extractedNotes'):
                    description += "\n\nNotes: " + analysis['extractedNotes']

                result = create_project_full(
                    user_id=user_info['userId'],
                    name=analysis['name'],
                    description=description,
                    project_type=analysis['type'],
                    channels=['Gmail', 'WhatsApp'],
                    participants=[{
                        'name': user_info.get('name', ''),
                        'email': user_info.get('email', ''),
                        'phone': clean_number,
                        'role': 'Creator'
                    }]
                )
                project_id = result['projectId']

                s3_key = upload_to_s3(file_bytes, project_id, fname or f'doc.{ext}', ct or media_ct)
                save_attachment_record(
                    project_id=project_id,
                    user_id=user_info['userId'],
                    file_name=fname or f'doc.{ext}',
                    file_size=len(file_bytes),
                    content_type=ct or media_ct,
                    ext=ext,
                    s3_key=s3_key,
                    extracted_text=text,
                    source='whatsapp'
                )

                ig = result.get('insightsGenerated', {})
                count = ig.get('count', 0) if ig.get('generated') else 0
                msg = (
                    f"✅ *Project created: {analysis['name']}*\n"
                    f"📁 Type: {analysis['type']}\n\n"
                )
                if count > 0:
                    an = ig.get('analysis', {}) or {}
                    msg += (
                        f"🤖 The AI generated {count} insights:\n"
                        f"  • {len(an.get('tasks') or [])} tasks\n"
                        f"  • {len(an.get('risks') or [])} risks\n"
                        f"  • {len(an.get('decisions') or [])} decisions\n\n"
                    )
                msg += f"📎 Document attached to the project.\n📊 Review everything at https://www.oneboxmanager.com"
                send_whatsapp_reply(from_number, msg)
            except Exception as e:
                print(f"[Webhook media] Error: {e}")
                import traceback; traceback.print_exc()
                send_whatsapp_reply(from_number, f"⚠️ Error processing the document: {str(e)[:80]}")

        threading.Thread(target=_process_media).start()
        return {"status": "ok", "action": "media_processing"}

    # Load the wizard session (always, whether linked or not)
    session = get_session(clean_number)
    flow = get_flow_state(session)
    in_wizard = flow.get('step', STEP_IDLE) != STEP_IDLE

    # If NO linked number AND NOT in an active wizard: invite to the wizard or to sign up
    if not user_info and not in_wizard:
        print(f"[Webhook] Number {clean_number} not linked, offering wizard")
        # If the user wants to create a project, launch the wizard (it will verify the email)
        from agent.whatsapp_wizard import detect_intent
        intent = detect_intent(message_body)

        if intent in ('create_project', 'greeting', 'help'):
            # Allow entering the wizard even without prior linking
            pass
        else:
            send_whatsapp_reply(
                from_number,
                "👋 Hi! I'm *OneBox*.\n\n"
                "Your number is not linked to an account yet. But I can help you create your first project if you have a OneBox account with your email.\n\n"
                "Type *create project* to get started, or *help* for more options.\n\n"
                "If you don't have an account yet, sign up first at *oneboxmanager.com*."
            )
            return {"status": "ok", "action": "no_account_prompt"}

    # Process the wizard if applicable (or hand off to the agent if it returns None)
    wizard_response, new_flow = handle_wizard(
        session=session,
        phone_number=clean_number,
        message=message_body,
        auto_link_phone_func=auto_link_phone
    )

    if wizard_response is not None:
        # The wizard handled the message
        if new_flow is not None:
            try:
                sessions_table.update_item(
                    Key={'phoneNumber': clean_number},
                    UpdateExpression="SET creationFlow = :f, lastActivity = :now",
                    ExpressionAttributeValues={
                        ':f': new_flow,
                        ':now': datetime.utcnow().isoformat()
                    }
                )
            except Exception as e:
                print(f"[Webhook] Error updating flow: {e}")
        send_whatsapp_reply(from_number, wizard_response)
        return {"status": "ok", "action": "wizard_handled"}

    # If the wizard did not handle the message and there is no linked user, we cannot continue
    if not user_info:
        send_whatsapp_reply(
            from_number,
            "👋 To use the AI agent you need to link your number.\n\n"
            "Type *create project* to create one with your email, or link your number at *oneboxmanager.com*."
        )
        return {"status": "ok", "action": "unregistered_user"}

    resolved_user_id = user_info['userId']
    resolved_name = user_info.get('name', clean_number)

    print(f"[Webhook] {channel} from {clean_number} (user: {resolved_name}): {message_body[:100]}")

    now = datetime.utcnow().isoformat()
    try:
        conversations_table.put_item(
            Item={
                'projectId': 'unassigned',
                'conversationId': f"twilio#{message_sid}",
                'userId': resolved_user_id,
                'from': clean_number,
                'fromEmail': '',
                'subject': f'Inbound {channel.upper()} message',
                'body': message_body,
                'date': now,
                'channel': channel,
                'twilioMessageSid': message_sid,
                'hasAttachments': num_media > 0,
                'status': 'unassigned',
                'createdAt': now
            },
            ConditionExpression='attribute_not_exists(conversationId)'
        )
    except Exception:
        pass

    _resolved_uid = resolved_user_id

    def _process():
        try:
            # Inject the user context into the agent (multi-tenant safe).
            # Previously we did `_tools.USER_ID = _resolved_uid` (mutating a
            # global) — race condition: two concurrent webhooks trampled each other.
            # set_current_user uses contextvars, isolated per asyncio task.
            set_current_user(_resolved_uid, "")

            session = get_session(clean_number)
            history = session.get('history', [])
            context_message = build_context(session, message_body)

            result = run_agent(context_message, history[-6:])
            agent_response = result.get('response', 'I could not process your message.')
            tools_used = result.get('tools_used', [])

            if len(agent_response) > 1500:
                agent_response = agent_response[:1500] + "\n\n_...message truncated_"

            project_id, project_name = extract_project(agent_response, tools_used)
            update_session(clean_number, message_body, agent_response, project_id, project_name)
            send_whatsapp_reply(from_number, agent_response)
        except Exception as e:
            print(f"[Webhook] Error processing: {e}")
            import traceback; traceback.print_exc()
            send_whatsapp_reply(from_number, "⚠️ There was an error processing your message. Try again.")

    thread = threading.Thread(target=_process)
    thread.start()

    return {"status": "ok", "action": "processing"}
