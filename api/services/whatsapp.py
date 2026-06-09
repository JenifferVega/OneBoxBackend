"""Lógica interna del canal WhatsApp/Twilio: sesiones por número, respuestas
salientes y procesamiento del webhook entrante (wizard, media y agente IA)."""
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
    """Envía respuesta por WhatsApp usando Twilio API."""
    try:
        from twilio.rest import Client
        sid = os.environ.get('TWILIO_ACCOUNT_SID', '')
        token = os.environ.get('TWILIO_AUTH_TOKEN', '')
        wa_number = os.environ.get('TWILIO_WHATSAPP_NUMBER', 'whatsapp:+14155238886')
        client = Client(sid, token)
        client.messages.create(body=message, from_=wa_number, to=to_number)
        print(f"[Webhook] Respuesta enviada a {to_number}")
    except Exception as e:
        print(f"[Webhook] Error enviando respuesta: {e}")


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
        parts.append(f"[CONTEXTO: El usuario está hablando sobre el proyecto '{name}' (ID: {active}). "
                     f"Si el mensaje se refiere a este proyecto, úsalo. Si habla de algo nuevo, crea uno nuevo.]")
    parts.append(new_message)
    return "\n".join(parts)


def extract_project(response_text, tools_used):
    import re
    if any(t in tools_used for t in ['crear_proyecto', 'listar_proyectos', 'obtener_contactos_proyecto']):
        id_match = re.search(r'proj-[a-f0-9]+', response_text)
        name_match = re.search(r'\*\*(.+?)\*\*', response_text)
        return (id_match.group(0) if id_match else '', name_match.group(1) if name_match else '')
    return ('', '')


def handle_twilio_webhook(body_raw: str) -> dict:
    """Procesa un mensaje entrante de Twilio (WhatsApp/SMS): wizard, media o agente IA."""
    import threading
    from agent.whatsapp_wizard import STEP_IDLE, get_flow_state, handle_wizard

    params = parse_qs(body_raw)

    from_number = params.get('From', [''])[0]
    message_body = params.get('Body', [''])[0]
    message_sid = params.get('MessageSid', [''])[0]
    num_media = int(params.get('NumMedia', ['0'])[0])

    canal = 'whatsapp' if from_number.startswith('whatsapp:') else 'sms'
    clean_number = from_number.replace('whatsapp:', '')

    if message_body.strip().lower().startswith('join'):
        print(f"[Webhook] Mensaje de join sandbox de {clean_number}, ignorando")
        return {"status": "ok", "action": "join_ignored"}

    # Buscar si el número ya está vinculado a un usuario
    user_info = lookup_user_by_phone(clean_number)

    # =================================================================
    # ¿Hay un archivo adjunto? Procesar directamente
    # =================================================================
    if num_media > 0:
        media_url = params.get('MediaUrl0', [''])[0]
        media_ct = params.get('MediaContentType0', [''])[0]
        if not user_info:
            send_whatsapp_reply(
                from_number,
                "📎 Recibí tu archivo, pero tu número no está vinculado a una cuenta de OneBox.\n\n"
                "Para crear proyectos desde documentos, primero vincula tu número:\n"
                "1️⃣ Inicia sesión en oneboxmanager.com\n"
                "2️⃣ Ve a tu perfil → vincula tu número\n\n"
                "O escribe *crear proyecto* para validarte por correo y crear uno desde cero."
            )
            return {"status": "ok", "action": "media_no_account"}

        # Procesar el archivo en background para no bloquear el webhook
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
                    send_whatsapp_reply(from_number, "⚠️ No pude descargar el archivo. Intenta de nuevo o súbelo desde la web.")
                    return

                valid, ext, error = validate_file(file_bytes, fname, ct or media_ct)
                if not valid:
                    send_whatsapp_reply(from_number, f"⚠️ {error}")
                    return

                text = extract_text(file_bytes, ext)
                if not text or len(text.strip()) < 30:
                    send_whatsapp_reply(from_number, "⚠️ No pude extraer suficiente texto del archivo. Asegúrate de que no esté escaneado o protegido.")
                    return

                send_whatsapp_reply(from_number, f"📄 Documento recibido ({len(file_bytes)//1024} KB).\n🤖 Analizando con IA...")

                analysis = analyze_document_for_project(text)
                description = analysis['description']
                if analysis.get('extractedNotes'):
                    description += "\n\nNotas: " + analysis['extractedNotes']

                result = create_project_full(
                    user_id=user_info['userId'],
                    name=analysis['name'],
                    description=description,
                    project_type=analysis['type'],
                    channels=['Gmail', 'WhatsApp'],
                    participants=[{
                        'nombre': user_info.get('name', ''),
                        'email': user_info.get('email', ''),
                        'telefono': clean_number,
                        'rol': 'Creador'
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
                    f"✅ *Proyecto creado: {analysis['name']}*\n"
                    f"📁 Tipo: {analysis['type']}\n\n"
                )
                if count > 0:
                    an = ig.get('analysis', {}) or {}
                    msg += (
                        f"🤖 La IA generó {count} insights:\n"
                        f"  • {len(an.get('tasks') or [])} tareas\n"
                        f"  • {len(an.get('risks') or [])} riesgos\n"
                        f"  • {len(an.get('decisions') or [])} decisiones\n\n"
                    )
                msg += f"📎 Documento adjuntado al proyecto.\n📊 Revisa todo en https://www.oneboxmanager.com"
                send_whatsapp_reply(from_number, msg)
            except Exception as e:
                print(f"[Webhook media] Error: {e}")
                import traceback; traceback.print_exc()
                send_whatsapp_reply(from_number, f"⚠️ Error procesando el documento: {str(e)[:80]}")

        threading.Thread(target=_process_media).start()
        return {"status": "ok", "action": "media_processing"}

    # Cargar sesión del wizard (siempre, esté vinculado o no)
    session = get_session(clean_number)
    flow = get_flow_state(session)
    in_wizard = flow.get('step', STEP_IDLE) != STEP_IDLE

    # Si NO hay número vinculado Y NO está en wizard activo: invitar a wizard o registrarse
    if not user_info and not in_wizard:
        print(f"[Webhook] Número {clean_number} no vinculado, ofreciendo wizard")
        # Si el usuario quiere crear un proyecto, lanzamos el wizard (validará el email)
        from agent.whatsapp_wizard import detect_intent
        intent = detect_intent(message_body)

        if intent in ('create_project', 'greeting', 'help'):
            # Permitir entrar al wizard incluso sin vinculación previa
            pass
        else:
            send_whatsapp_reply(
                from_number,
                "👋 ¡Hola! Soy *OneBox*.\n\n"
                "Tu número aún no está vinculado a una cuenta. Pero puedo ayudarte a crear tu primer proyecto si tienes una cuenta de OneBox con tu correo.\n\n"
                "Escribe *crear proyecto* para empezar, o *ayuda* para más opciones.\n\n"
                "Si aún no tienes cuenta, regístrate primero en *oneboxmanager.com*."
            )
            return {"status": "ok", "action": "no_account_prompt"}

    # Procesar el wizard si aplica (o pasar al agente si retorna None)
    wizard_response, new_flow = handle_wizard(
        session=session,
        phone_number=clean_number,
        message=message_body,
        auto_link_phone_func=auto_link_phone
    )

    if wizard_response is not None:
        # El wizard manejó el mensaje
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
                print(f"[Webhook] Error actualizando flow: {e}")
        send_whatsapp_reply(from_number, wizard_response)
        return {"status": "ok", "action": "wizard_handled"}

    # Si el wizard no manejó el mensaje y no hay usuario vinculado, no podemos continuar
    if not user_info:
        send_whatsapp_reply(
            from_number,
            "👋 Para usar el agente IA necesitas vincular tu número.\n\n"
            "Escribe *crear proyecto* para crear uno con tu correo, o vincula tu número en *oneboxmanager.com*."
        )
        return {"status": "ok", "action": "unregistered_user"}

    resolved_user_id = user_info['userId']
    resolved_name = user_info.get('name', clean_number)

    print(f"[Webhook] {canal} de {clean_number} (user: {resolved_name}): {message_body[:100]}")

    now = datetime.utcnow().isoformat()
    try:
        conversations_table.put_item(
            Item={
                'projectId': 'unassigned',
                'conversationId': f"twilio#{message_sid}",
                'userId': resolved_user_id,
                'from': clean_number,
                'fromEmail': '',
                'subject': f'Mensaje {canal.upper()} entrante',
                'body': message_body,
                'date': now,
                'channel': canal,
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
            # Inyectar contexto de usuario al agente (multi-tenant seguro).
            # Antes hacíamos `_tools.USER_ID = _resolved_uid` (mutar global)
            # — race-condition: dos webhooks concurrentes se pisaban.
            # set_current_user usa contextvars, aislado por task asyncio.
            set_current_user(_resolved_uid, "")

            session = get_session(clean_number)
            history = session.get('history', [])
            context_message = build_context(session, message_body)

            result = run_agent(context_message, history[-6:])
            agent_response = result.get('response', 'No pude procesar tu mensaje.')
            tools_used = result.get('tools_used', [])

            if len(agent_response) > 1500:
                agent_response = agent_response[:1500] + "\n\n_...mensaje truncado_"

            project_id, project_name = extract_project(agent_response, tools_used)
            update_session(clean_number, message_body, agent_response, project_id, project_name)
            send_whatsapp_reply(from_number, agent_response)
        except Exception as e:
            print(f"[Webhook] Error procesando: {e}")
            import traceback; traceback.print_exc()
            send_whatsapp_reply(from_number, "⚠️ Hubo un error procesando tu mensaje. Intenta de nuevo.")

    thread = threading.Thread(target=_process)
    thread.start()

    return {"status": "ok", "action": "processing"}
