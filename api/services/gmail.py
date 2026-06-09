"""Lógica interna de Gmail: OAuth, fetch de correos, sync con análisis IA,
push notifications (Pub/Sub) y registro de watch."""
import json
import os
from datetime import datetime

from fastapi import HTTPException

from agent.llm import call_llm, extract_json_from_response
from agent.tools import conversations_table
from api.deps import user_tokens_table

GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID")
GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET")
# NOTA: estas dos variables se referenciaban en el código original sin estar
# definidas (NameError en runtime para /api/gmail/auth y /api/gmail/callback).
# Ahora se leen de entorno; configúralas en el despliegue.
GOOGLE_REDIRECT_URI = os.getenv("GOOGLE_REDIRECT_URI", "")
GOOGLE_SCOPES = os.getenv(
    "GOOGLE_SCOPES",
    "https://www.googleapis.com/auth/gmail.readonly https://www.googleapis.com/auth/userinfo.email"
).split()

GOOGLE_CLOUD_PROJECT = os.environ.get('GOOGLE_CLOUD_PROJECT', 'gmail-lambda-project')
GMAIL_PUBSUB_TOPIC = f"projects/{GOOGLE_CLOUD_PROJECT}/topics/gmail-notifications"


def fetch_gmail_emails(user_id: str, max_results: int = 20) -> list:
    """Fetches emails from Gmail API using the user's stored refresh token."""
    import requests as _req

    token_item = user_tokens_table.get_item(Key={'userId': user_id}).get('Item', {})
    refresh_token = token_item.get('gmailRefreshToken', '')
    if not refresh_token:
        print(f"[Gmail] No refresh token for user {user_id}")
        return []

    token_resp = _req.post('https://oauth2.googleapis.com/token', data={
        'client_id': GOOGLE_CLIENT_ID,
        'client_secret': GOOGLE_CLIENT_SECRET,
        'refresh_token': refresh_token,
        'grant_type': 'refresh_token'
    }, timeout=15)
    if token_resp.status_code != 200:
        print(f"[Gmail] Token refresh failed: {token_resp.text}")
        return []
    access_token = token_resp.json().get('access_token', '')

    list_resp = _req.get(
        'https://gmail.googleapis.com/gmail/v1/users/me/messages',
        headers={'Authorization': f'Bearer {access_token}'},
        params={'maxResults': max_results, 'q': 'is:inbox -category:promotions -category:social -category:updates -category:forums'},
        timeout=15
    )
    if list_resp.status_code != 200:
        print(f"[Gmail] List messages failed: {list_resp.text}")
        return []

    messages = list_resp.json().get('messages', [])
    emails = []

    for msg in messages[:max_results]:
        msg_resp = _req.get(
            f'https://gmail.googleapis.com/gmail/v1/users/me/messages/{msg["id"]}',
            headers={'Authorization': f'Bearer {access_token}'},
            params={'format': 'full'},
            timeout=15
        )
        if msg_resp.status_code != 200:
            continue

        msg_data = msg_resp.json()
        hdrs = {h['name']: h['value'] for h in msg_data.get('payload', {}).get('headers', [])}

        payload = msg_data.get('payload', {})

        def _extract_body(part):
            if part.get('mimeType') == 'text/plain' and part.get('body', {}).get('data'):
                import base64
                return base64.urlsafe_b64decode(part['body']['data']).decode('utf-8', errors='ignore')
            for sub in part.get('parts', []):
                result = _extract_body(sub)
                if result:
                    return result
            return ''
        full_body = _extract_body(payload)
        if not full_body:
            full_body = msg_data.get('snippet', '')

        to_field = hdrs.get('To', '')
        cc_field = hdrs.get('Cc', '')

        emails.append({
            'id': msg['id'],
            'from': hdrs.get('From', ''),
            'fromEmail': hdrs.get('From', '').split('<')[-1].rstrip('>') if '<' in hdrs.get('From', '') else hdrs.get('From', ''),
            'to': to_field,
            'cc': cc_field,
            'subject': hdrs.get('Subject', ''),
            'snippet': msg_data.get('snippet', ''),
            'body': full_body[:2000],
            'date': hdrs.get('Date', '')
        })

    print(f"[Gmail] Fetched {len(emails)} emails for user {user_id}")
    return emails


def sync_gmail(uid: str) -> dict:
    """Sincroniza Gmail, trae correos nuevos y los analiza con IA.
    Crea proyectos, tareas e insights automáticamente.
    Usa el refresh token del usuario almacenado en DynamoDB."""
    from agent.tools import (
        analizar_inbox, asignar_correo_a_proyecto, crear_insight,
        crear_proyecto, crear_tarea, listar_proyectos
    )

    print(f"[Gmail Sync] Fetching emails for user {uid}...")
    gmail_emails = fetch_gmail_emails(uid, max_results=50)
    print(f"[Gmail Sync] {len(gmail_emails)} emails from Gmail")

    SPAM_DOMAINS = [
        'bancolombia', 'homecenter', 'airbnb', 'puppis', 'dermosalud',
        'rappi', 'uber', 'samsung', 'adidas', 'temu', 'farmatodo',
        'linkedin', 'coursera', 'platzi', 'craftsy', 'medu.mx',
        'clickup', 'ngrok', 'livevoice', 'sura', 'coomeva',
        'loyal.ink', 'design.com', 'harumiglobal', 'npmjs',
        'worldoffice', 'exito.com', 'sodimac', 'nequi',
        'noreply', 'no-reply', 'no-responder', 'mailer-daemon',
        'notifications@', 'alertas@', 'notificaciones@',
        'news@', 'info@', 'express@', 'team@m.', 'informacion@',
        'alert@', 'editor@',
        'hello.platzi', 'hello.rappi', 'hello.design',
        'mail.clickup', 'mail.coursera',
        'email.samsung', 'e.exito',
        'farmaciasiman', 'glam', 'paiz.com',
        'accounts.google', 'pse', 'firmaelectronica',
        'amazon.com', 'apple.com', 'netflix', 'spotify',
        'mercadolibre', 'mercadopago', 'paypal', 'stripe',
        'cibergestion', 'pagares.bvc', 'jobalerts',
        'correo.paiz', 'siman.com',
    ]
    SPAM_SUBJECT = [
        'newsletter', 'unsubscribe', 'marketing', 'promocion', 'oferta',
        'descuento', 'verify', 'verification', 'encuesta', 'survey',
        'off en', '% off', 'envío gratis', 'tu opinión', 'alerta de seguridad',
        'alertas y notificaciones', 'factura electr', 'pedido se ha entregado',
        'cmr puntos', 'antipulgas', 'two-factor', '2fa',
        'busca personal', 'empleo', 'vacaciones', 'plan de salud',
        'comprobante', 'transacción', 'código de verificación',
        'pasaporte', 'orden de compra', 'confirmación de pago',
        'dcto', 'descubre', 'easter sale', 'black friday',
        'precios bajos', 'tu pedido', 'has recibido un documento',
        'firmante de', 'pagaré', 'welcome to',
    ]

    new_emails = 0
    spam_filtered = 0
    for email in gmail_emails:
        conv_id = email.get('id', email.get('messageId', ''))
        if not conv_id:
            continue

        # Filtrar spam antes de guardar
        from_field = (email.get('from', '') + ' ' + email.get('fromEmail', '')).lower()
        subject = (email.get('subject', '') or '').lower()

        is_spam = (
            any(kw in from_field for kw in SPAM_DOMAINS) or
            any(kw in subject for kw in SPAM_SUBJECT)
        )
        if is_spam:
            spam_filtered += 1
            continue

        try:
            conversations_table.put_item(
                Item={
                    'projectId': 'unassigned',
                    'conversationId': f"gmail#{conv_id}",
                    'userId': uid,
                    'from': email.get('from', ''),
                    'fromEmail': email.get('fromEmail', email.get('from', '')),
                    'to': email.get('to', ''),
                    'cc': email.get('cc', ''),
                    'subject': email.get('subject', ''),
                    'body': email.get('body', email.get('snippet', ''))[:2000],
                    'date': email.get('date', datetime.utcnow().isoformat()),
                    'channel': 'gmail',
                    'status': 'unassigned',
                    'createdAt': datetime.utcnow().isoformat()
                },
                ConditionExpression='attribute_not_exists(conversationId)'
            )
            new_emails += 1
        except Exception:
            pass

    print(f"[Gmail Sync] {new_emails} new emails saved, {spam_filtered} spam filtered out")

    inbox_result = analizar_inbox()
    all_unassigned = inbox_result.get('emails', [])
    unassigned = sorted(all_unassigned, key=lambda x: x.get('date', x.get('createdAt', '')), reverse=True)[:20]
    print(f"[Gmail Sync] {len(unassigned)} unassigned emails to analyze")

    if not unassigned:
        return {
            "success": True,
            "new_emails": new_emails,
            "projects_created": 0
        }

    email_summaries = []
    for e in unassigned:
        email_summaries.append({
            'conversation_id': e.get('conversationId', ''),
            'from': e.get('from', ''),
            'fromEmail': e.get('fromEmail', ''),
            'to': e.get('to', ''),
            'cc': e.get('cc', ''),
            'subject': e.get('subject', ''),
            'body': e.get('body', '')[:1500],
            'date': e.get('date', '')
        })

    existing = listar_proyectos()
    existing_names = [p.get('name', '') for p in existing.get('projects', [])]

    analysis_prompt = f"""Eres el clasificador de correos de OneBox. Tu trabajo es detectar correos que son proyectos de trabajo REALES y crearlos.

PROYECTOS EXISTENTES (NO crear duplicados):
{json.dumps(existing_names, ensure_ascii=False)}

CORREOS SIN ASIGNAR:
{json.dumps(email_summaries, ensure_ascii=False, indent=2)}

REGLAS CRÍTICAS:
1. SOLO ignora correos que sean CLARAMENTE newsletters, marketing, alertas automáticas de sistemas o spam.
2. Si un correo es de una PERSONA REAL hablando de trabajo, un proyecto, una solicitud, un requerimiento, una tarea, un problema técnico, una cotización, o cualquier tema profesional → SIEMPRE créalo como proyecto con action "create_project".
3. Si el correo ya pertenece a un proyecto existente → action "assign_to_existing"
4. EN CASO DE DUDA, créalo como proyecto. Es mejor crear un proyecto de más que perder un correo importante.
5. Agrupa correos del MISMO tema en un solo proyecto.
6. Detecta bloqueos, decisiones, riesgos y tareas dentro de cada proyecto.
7. En participants SOLO incluye personas con email verificable:
   - Del campo "from": el remitente con su email
   - Del campo "to": todos los destinatarios con sus emails
   - Del campo "cc": todos los CC con sus emails
   - NO agregues personas mencionadas en el cuerpo del correo que no tengan email en los campos from/to/cc
   Formato: {{"nombre": "Nombre", "email": "correo@ejemplo.com", "rol": "Rol detectado"}}
8. Lee el CUERPO COMPLETO del correo (campo "body") para detectar tareas, bloqueos, decisiones y riesgos. No te limites al subject.

EJEMPLOS de correos que SÍ son proyectos (NO ignorar):
- "Necesito una aplicación web para..." → create_project
- "Te envío los requerimientos de..." → create_project
- "Hay un problema con el servidor..." → create_project
- "¿Puedes cotizar...?" → create_project
- Cualquier correo de un colega/cliente sobre trabajo → create_project

EJEMPLOS de correos para IGNORAR:
- "Tu pedido de Amazon ha sido enviado" → ignore
- "50% de descuento en..." → ignore
- "Alerta de seguridad de Google" → ignore
- "Nuevas ofertas de empleo" → ignore

RESPONDE SOLO JSON:
{{
  "analysis": [
    {{"action": "ignore", "conversation_id": "...", "reason": "..."}},
    {{"action": "create_project", "project_name": "...", "project_description": "...", "project_type": "...", "participants": [{{"nombre": "...", "email": "correo@ejemplo.com", "rol": "..."}}], "emails_to_assign": ["..."], "insights": [{{"type": "blocker|decision|followup|risk|task_created", "title": "...", "description": "...", "related_person": "..."}}], "tasks": [{{"text": "...", "assigned_to": "...", "status": "pending|blocked"}}]}},
    {{"action": "assign_to_existing", "project_name": "...", "emails_to_assign": ["..."]}}
  ]
}}"""

    print("[Gmail Sync] Analyzing with LLM...")
    print(f"[Gmail Sync] Email summaries: {json.dumps(email_summaries[:3], ensure_ascii=False)[:500]}")
    response = call_llm(
        system_prompt="Eres el agente inteligente de OneBox. Clasificas correos y creas proyectos automáticamente. IMPORTANTE: Los correos de trabajo, solicitudes de proyectos, tareas, o comunicaciones de equipo DEBEN crear proyectos. Solo ignora newsletters automáticos, spam, códigos de verificación y alertas de marketing.",
        user_message=analysis_prompt,
        temperature=0.2,
        max_tokens=8192
    )

    print(f"[Gmail Sync] LLM raw response length: {len(response)}")
    print(f"[Gmail Sync] LLM raw response preview: {response[:1000]}")

    plan = extract_json_from_response(response)
    if not plan or 'analysis' not in plan:
        import re
        cleaned = response.strip()
        if cleaned.startswith('```'):
            cleaned = re.sub(r'^```(?:json)?\s*', '', cleaned)
            cleaned = re.sub(r'\s*```$', '', cleaned)
        try:
            plan = json.loads(cleaned)
        except Exception:
            pass

    if not plan or 'analysis' not in plan:
        print(f"[Gmail Sync] Could not parse LLM response: {response[:500]}")
        return {
            "success": False,
            "error": "Could not parse LLM analysis",
            "new_emails": new_emails
        }

    print(f"[Gmail Sync] Plan: {json.dumps(plan, ensure_ascii=False)[:500]}")

    projects_created = 0
    emails_assigned = 0
    insights_count = 0
    tasks_count = 0
    ignored = 0

    for item in plan['analysis']:
        action = item.get('action', '')

        if action == 'ignore':
            ignored += 1
            print(f"[Gmail Sync] IGNORED: {item.get('conversation_id','')} - {item.get('reason','')}")
            continue

        if action == 'create_project':
            result = crear_proyecto(
                name=item['project_name'],
                description=item.get('project_description', ''),
                type=item.get('project_type', 'Otro'),
                participants=item.get('participants', []),
                channels=['Gmail']
            )
            if result.get('success'):
                pid = result['projectId']
                projects_created += 1

                for conv_id in item.get('emails_to_assign', []):
                    r = asignar_correo_a_proyecto(conv_id, pid, item['project_name'])
                    if r.get('success'):
                        emails_assigned += 1

                for ins in item.get('insights', []):
                    r = crear_insight(pid, item['project_name'], ins['type'], ins['title'],
                                      ins.get('description', ''), ins.get('related_person', ''))
                    if r.get('success'):
                        insights_count += 1

                for task in item.get('tasks', []):
                    r = crear_tarea(pid, task['text'], task.get('assigned_to', ''), task.get('status', 'pending'))
                    if r.get('success'):
                        tasks_count += 1

        elif action == 'assign_to_existing':
            target_name = item.get('project_name', '')
            target_pid = ''
            for p in existing.get('projects', []):
                if p.get('name', '').lower() == target_name.lower():
                    target_pid = p.get('projectId', '')
                    break
            if target_pid:
                for conv_id in item.get('emails_to_assign', []):
                    r = asignar_correo_a_proyecto(conv_id, target_pid, target_name)
                    if r.get('success'):
                        emails_assigned += 1

    result = {
        "success": True,
        "new_emails": new_emails,
        "ignored": ignored,
        "projects_created": projects_created,
        "emails_assigned": emails_assigned,
        "insights_created": insights_count,
        "tasks_created": tasks_count
    }
    print(f"[Gmail Sync] Done: {result}")
    return result


def handle_push_notification(body: dict) -> dict:
    """Procesa una notificación de Google Pub/Sub cuando llega un correo nuevo
    y dispara el sync de Gmail en background."""
    import base64 as _base64
    import threading

    message = body.get('message', {})
    data = message.get('data', '')

    email_address = ''
    if data:
        decoded = json.loads(_base64.b64decode(data).decode('utf-8'))
        email_address = decoded.get('emailAddress', '')
        history_id = decoded.get('historyId', '')
        print(f"[Gmail Push] Correo nuevo para {email_address} (historyId: {history_id})")
    else:
        print("[Gmail Push] Notificación sin data")

    # Buscar el uid del usuario al que pertenece este email de Gmail.
    # Si no se encuentra, NO procesamos: usar un USER_ID hardcoded como
    # fallback estaba causando que las notificaciones de un usuario
    # quedaran asociadas a otro (data leak cross-tenant).
    uid = None
    try:
        result = user_tokens_table.scan()
        for item in result.get('Items', []):
            if item.get('gmailEmail', '').lower() == (email_address or '').lower():
                uid = item.get('userId')
                break
    except Exception:
        pass

    if not uid:
        print(f"[Gmail Push] No hay usuario vinculado a {email_address}, ignorando notificación.")
        return {"ok": True, "skipped": True, "reason": "no_user_linked"}

    def _sync():
        try:
            import urllib.request as _req
            payload = json.dumps({}).encode('utf-8')
            req = _req.Request(
                "http://localhost:8000/api/scheduled/gmail-sync",
                data=payload,
                headers={'Content-Type': 'application/json', 'x-user-id': uid},
                method='POST'
            )
            with _req.urlopen(req, timeout=120) as resp:
                result = json.loads(resp.read().decode('utf-8'))
                print(f"[Gmail Push] Sync result: {result}")
        except Exception as e:
            print(f"[Gmail Push] Sync error: {e}")

    thread = threading.Thread(target=_sync)
    thread.start()

    return {"status": "ok"}


def register_watch(uid: str) -> dict:
    """Registra el watch de Gmail para recibir notificaciones push via Pub/Sub."""
    import requests

    try:
        token_item = user_tokens_table.get_item(Key={'userId': uid}).get('Item', {})
        refresh_token = token_item.get('gmailRefreshToken', '')
        if not refresh_token:
            return {"error": "No Gmail token found"}

        resp = requests.post('https://oauth2.googleapis.com/token', data={
            'client_id': GOOGLE_CLIENT_ID,
            'client_secret': GOOGLE_CLIENT_SECRET,
            'refresh_token': refresh_token,
            'grant_type': 'refresh_token'
        }, timeout=15)
        if resp.status_code != 200:
            return {"error": f"Token refresh failed: {resp.text}"}

        access_token = resp.json()['access_token']

        watch_resp = requests.post(
            'https://www.googleapis.com/gmail/v1/users/me/watch',
            headers={'Authorization': f'Bearer {access_token}', 'Content-Type': 'application/json'},
            json={
                'topicName': GMAIL_PUBSUB_TOPIC,
                'labelIds': ['INBOX']
            },
            timeout=15
        )

        if watch_resp.status_code == 200:
            watch_data = watch_resp.json()
            print(f"[Gmail Watch] Registered for user {uid}: {watch_data}")
            return {"success": True, "expiration": watch_data.get('expiration', ''), "historyId": watch_data.get('historyId', '')}
        else:
            return {"error": f"Watch failed: {watch_resp.text}"}

    except Exception as e:
        return {"error": str(e)}


def build_auth_url(uid: str) -> dict:
    """Genera URL de autorización de Google OAuth para conectar Gmail."""
    from urllib.parse import urlencode as _urlencode

    params = {
        'client_id': GOOGLE_CLIENT_ID,
        'redirect_uri': GOOGLE_REDIRECT_URI,
        'response_type': 'code',
        'scope': ' '.join(GOOGLE_SCOPES),
        'access_type': 'offline',
        'prompt': 'consent',
        'state': uid
    }
    auth_url = f"https://accounts.google.com/o/oauth2/v2/auth?{_urlencode(params)}"
    return {"auth_url": auth_url}


def exchange_oauth_code(uid: str, code: str) -> str:
    """Intercambia el code de Google OAuth por tokens y los guarda.
    Devuelve el email de Gmail conectado. Lanza Exception si algo falla."""
    import requests as _requests

    print(f"[Gmail OAuth] Exchanging code for user {uid}")
    token_resp = _requests.post('https://oauth2.googleapis.com/token', data={
        'code': code,
        'client_id': GOOGLE_CLIENT_ID,
        'client_secret': GOOGLE_CLIENT_SECRET,
        'redirect_uri': GOOGLE_REDIRECT_URI,
        'grant_type': 'authorization_code'
    }, timeout=30)

    print(f"[Gmail OAuth] Token response status: {token_resp.status_code}")
    print(f"[Gmail OAuth] Token response body: {token_resp.text[:500]}")

    if token_resp.status_code != 200:
        raise Exception(f"Token exchange failed: {token_resp.status_code} - {token_resp.text}")

    tokens = token_resp.json()
    refresh_token = tokens.get('refresh_token', '')
    access_token = tokens.get('access_token', '')

    if not access_token:
        raise Exception("Token exchange devolvió sin access_token")

    # Si Google no manda refresh_token (típico cuando ya autorizaste antes),
    # reusamos el existente. Pero si tampoco lo tenemos guardado, falla:
    # sin refresh_token el cron no podrá sincronizar.
    if not refresh_token:
        existing = user_tokens_table.get_item(Key={'userId': uid}).get('Item', {})
        refresh_token = existing.get('gmailRefreshToken', '')
        if not refresh_token:
            raise Exception(
                "Google no devolvió refresh_token y no había uno previo. "
                "Revoca el acceso en https://myaccount.google.com/permissions y reintenta."
            )

    # Pedir info del usuario (email). SIN email no podemos guardar:
    # el cron necesita saber qué cuenta sincronizar. Antes guardábamos
    # gmailEmail='' y el resultado era "conectado pero invisible".
    user_info_resp = _requests.get(
        'https://www.googleapis.com/oauth2/v2/userinfo',
        headers={'Authorization': f'Bearer {access_token}'},
        timeout=15
    )
    print(f"[Gmail OAuth] userinfo status: {user_info_resp.status_code}")
    if user_info_resp.status_code != 200:
        raise Exception(
            f"Userinfo falló: {user_info_resp.status_code} {user_info_resp.text[:200]}"
        )
    user_info = user_info_resp.json()
    gmail_email = (user_info.get('email') or '').strip().lower()
    if not gmail_email:
        raise Exception(
            f"Userinfo no devolvió email. Respuesta: {json.dumps(user_info)[:200]}"
        )

    user_tokens_table.put_item(Item={
        'userId': uid,
        'gmailRefreshToken': refresh_token,
        'gmailEmail': gmail_email,
        'gmailConnected': True,
        'connectedAt': datetime.utcnow().isoformat()
    })

    print(f"[Gmail OAuth] OK uid={uid} email={gmail_email}")
    return gmail_email


def get_status(uid: str) -> dict:
    """Verifica si el usuario tiene Gmail conectado."""
    try:
        result = user_tokens_table.get_item(Key={'userId': uid})
        item = result.get('Item')
        if item and item.get('gmailConnected'):
            return {
                "connected": True,
                "email": item.get('gmailEmail', ''),
                "connectedAt": item.get('connectedAt', '')
            }
        return {"connected": False}
    except Exception:
        return {"connected": False}


def disconnect(uid: str) -> dict:
    """Desconecta Gmail del usuario."""
    try:
        user_tokens_table.delete_item(Key={'userId': uid})
        return {"success": True}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
