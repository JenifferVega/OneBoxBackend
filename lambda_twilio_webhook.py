# ==============================================================================
# lambda_twilio_webhook.py
# ==============================================================================
# Lambda que recibe mensajes entrantes de WhatsApp/SMS via Twilio webhook.
# Mantiene sesiones por número de teléfono para contexto conversacional.
#

# ==============================================================================

import json
import os
import boto3
from datetime import datetime, timedelta
from urllib.parse import parse_qs
from decimal import Decimal

dynamodb = boto3.resource('dynamodb')
conversations_table = dynamodb.Table('onebox-conversations')
sessions_table = dynamodb.Table('onebox-whatsapp-sessions')

DEFAULT_USER_ID = "7458a478-e071-70ff-d1af-8d513f275621"

AGENT_API_URL = os.environ.get('AGENT_API_URL', 'https://api.oneboxmanager.com')

TWILIO_ACCOUNT_SID = os.environ.get('TWILIO_ACCOUNT_SID', '')
TWILIO_AUTH_TOKEN = os.environ.get('TWILIO_AUTH_TOKEN', '')
TWILIO_WHATSAPP_NUMBER = os.environ.get('TWILIO_WHATSAPP_NUMBER', 'whatsapp:+14155238886')

SESSION_TIMEOUT_HOURS = 2
MAX_HISTORY = 10  


def send_whatsapp_reply(to_number: str, message: str):
    """Envía respuesta por WhatsApp usando Twilio API."""
    try:
        from twilio.rest import Client
        client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
        client.messages.create(
            body=message,
            from_=TWILIO_WHATSAPP_NUMBER,
            to=to_number
        )
        print(f"[Webhook] Respuesta enviada a {to_number}")
    except Exception as e:
        print(f"[Webhook] Error enviando respuesta: {e}")


def get_session(phone_number: str) -> dict:
    """Obtiene la sesión activa de un número o crea una nueva."""
    try:
        result = sessions_table.get_item(Key={'phoneNumber': phone_number})
        session = result.get('Item')

        if session:
            # Verificar si la sesión expiró
            last_activity = session.get('lastActivity', '')
            if last_activity:
                last_time = datetime.fromisoformat(last_activity)
                if datetime.utcnow() - last_time > timedelta(hours=SESSION_TIMEOUT_HOURS):
                    print(f"[Session] Sesión expirada para {phone_number}, creando nueva")
                    return create_new_session(phone_number)

            return session

        return create_new_session(phone_number)
    except Exception as e:
        print(f"[Session] Error obteniendo sesión: {e}")
        return create_new_session(phone_number)


def create_new_session(phone_number: str) -> dict:
    """Crea una sesión nueva para un número."""
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


def update_session(phone_number: str, message: str, response: str,
                   project_id: str = '', project_name: str = ''):
    """Actualiza la sesión con el nuevo mensaje y respuesta."""
    try:
        session = get_session(phone_number)
        history = session.get('history', [])

        history.append({'role': 'user', 'content': message})
        history.append({'role': 'assistant', 'content': response})

        if len(history) > MAX_HISTORY * 2:
            history = history[-(MAX_HISTORY * 2):]

        update_expr = "SET #h = :history, lastActivity = :now"
        expr_values = {
            ':history': history,
            ':now': datetime.utcnow().isoformat()
        }
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
        print(f"[Session] Error actualizando sesión: {e}")


def build_context_message(session: dict, new_message: str) -> str:
    """Construye el mensaje con contexto de sesión para el agente."""
    parts = []

    # Agregar contexto del proyecto activo
    active_project = session.get('activeProjectId', '')
    active_name = session.get('activeProjectName', '')
    if active_project:
        parts.append(f"[CONTEXTO: El usuario está hablando sobre el proyecto '{active_name}' (ID: {active_project}). "
                     f"Si el mensaje se refiere a este proyecto, úsalo. Si habla de algo nuevo, crea uno nuevo.]")

    parts.append(new_message)

    return "\n".join(parts)


def extract_project_from_response(response_text: str, tools_used: list) -> tuple:
    """Intenta extraer el proyecto mencionado en la respuesta del agente."""
    if 'crear_proyecto' in tools_used:
        import re
        id_match = re.search(r'proj-[a-f0-9]+', response_text)
        name_match = re.search(r'\*\*(.+?)\*\*', response_text)
        return (
            id_match.group(0) if id_match else '',
            name_match.group(1) if name_match else ''
        )

    if any(t in tools_used for t in ['listar_proyectos', 'obtener_contactos_proyecto']):
        import re
        id_match = re.search(r'proj-[a-f0-9]+', response_text)
        name_match = re.search(r'\*\*(.+?)\*\*', response_text)
        return (
            id_match.group(0) if id_match else '',
            name_match.group(1) if name_match else ''
        )

    return ('', '')


def process_with_agent(message: str, from_number: str, clean_number: str):
    """Envía el mensaje al agente IA con contexto de sesión y responde por WhatsApp."""
    import urllib.request

    try:
        # Obtener sesión
        session = get_session(clean_number)
        history = session.get('history', [])

        context_message = build_context_message(session, message)

        payload = json.dumps({
            "message": context_message,
            "history": history[-6:]  
        }).encode('utf-8')

        req = urllib.request.Request(
            f"{AGENT_API_URL}/chat",
            data=payload,
            headers={'Content-Type': 'application/json'},
            method='POST'
        )

        with urllib.request.urlopen(req, timeout=90) as resp:
            result = json.loads(resp.read().decode('utf-8'))
            agent_response = result.get('response', 'No pude procesar tu mensaje.')
            tools_used = result.get('toolsUsed', [])

        if len(agent_response) > 1500:
            agent_response = agent_response[:1500] + "\n\n_...mensaje truncado_"

        project_id, project_name = extract_project_from_response(agent_response, tools_used)

        update_session(
            clean_number, message, agent_response,
            project_id=project_id, project_name=project_name
        )

        send_whatsapp_reply(from_number, agent_response)

    except Exception as e:
        print(f"[Webhook] Error procesando con agente: {e}")
        import traceback
        traceback.print_exc()
        send_whatsapp_reply(from_number, "⚠️ Hubo un error procesando tu mensaje. Intenta de nuevo.")


def lambda_handler(event, context):
    """
    Twilio envía un POST con form-urlencoded body.
    """
    try:
        body_raw = event.get('body', '')
        if event.get('isBase64Encoded'):
            import base64
            body_raw = base64.b64decode(body_raw).decode('utf-8')

        params = parse_qs(body_raw)

        from_number = params.get('From', [''])[0]
        to_number = params.get('To', [''])[0]
        message_body = params.get('Body', [''])[0]
        message_sid = params.get('MessageSid', [''])[0]
        num_media = int(params.get('NumMedia', ['0'])[0])

        canal = 'whatsapp' if from_number.startswith('whatsapp:') else 'sms'
        clean_number = from_number.replace('whatsapp:', '')

        print(f"[Webhook] {canal} de {clean_number}: {message_body[:100]}")

        now = datetime.utcnow().isoformat()
        conversation_id = f"twilio#{message_sid}"

        conversations_table.put_item(
            Item={
                'projectId': 'unassigned',
                'conversationId': conversation_id,
                'userId': DEFAULT_USER_ID,
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

        if message_body.strip().lower().startswith('join'):
            print(f"[Webhook] Mensaje de join sandbox, no procesar")
            return {
                'statusCode': 200,
                'headers': {'Content-Type': 'text/xml'},
                'body': '<?xml version="1.0" encoding="UTF-8"?><Response></Response>'
            }

        process_with_agent(message_body, from_number, clean_number)

        return {
            'statusCode': 200,
            'headers': {'Content-Type': 'text/xml'},
            'body': '<?xml version="1.0" encoding="UTF-8"?><Response></Response>'
        }

    except dynamodb.meta.client.exceptions.ConditionalCheckFailedException:
        return {
            'statusCode': 200,
            'headers': {'Content-Type': 'text/xml'},
            'body': '<?xml version="1.0" encoding="UTF-8"?><Response></Response>'
        }
    except Exception as e:
        print(f"[Webhook] Error: {str(e)}")
        import traceback
        traceback.print_exc()
        return {
            'statusCode': 200,
            'headers': {'Content-Type': 'text/xml'},
            'body': '<?xml version="1.0" encoding="UTF-8"?><Response></Response>'
        }
