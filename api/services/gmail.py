"""Gmail internal logic: OAuth, email fetch, sync with AI analysis,
push notifications (Pub/Sub) and watch registration."""
import json
import os
from datetime import datetime

from fastapi import HTTPException

from agent.llm import call_llm, extract_json_from_response
from agent.tools import conversations_table
from api.deps import user_tokens_table

GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID")
GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET")
# NOTE: these two variables were referenced in the original code without being
# defined (NameError at runtime for /api/gmail/auth and /api/gmail/callback).
# They are now read from the environment; configure them at deploy time.
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
    """Sync Gmail, fetch updated emails and analyze them with AI.
    Creates projects, tasks and insights automatically.
    Uses the user's refresh token stored in DynamoDB."""
    from agent.tools import (
        analyze_inbox, assign_email_to_project, create_insight,
        create_project, create_task, list_projects
    )

    print(f"[Gmail Sync] Fetching emails for user {uid}...")
    gmail_emails = fetch_gmail_emails(uid, max_results=50)
    print(f"[Gmail Sync] {len(gmail_emails)} emails from Gmail")

    # Spam-domain and spam-subject keyword lists.
    # English keywords appear alongside Spanish ones so spam is filtered in
    # both languages (default app locale is English, but the user may still
    # have Spanish inboxes).
    SPAM_DOMAINS = [
        # ── Spanish/LatAm brands and senders ──────────────────────────────
        'bancolombia', 'homecenter', 'airbnb', 'puppis', 'dermosalud',
        'rappi', 'uber', 'samsung', 'adidas', 'temu', 'farmatodo',
        'linkedin', 'coursera', 'platzi', 'craftsy', 'medu.mx',
        'clickup', 'ngrok', 'livevoice', 'sura', 'coomeva',
        'loyal.ink', 'design.com', 'harumiglobal', 'npmjs',
        'worldoffice', 'exito.com', 'sodimac', 'nequi',
        'noreply', 'no-reply', 'no-responder', 'mailer-daemon',
        'notifications@', 'alertas@',
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
        'email.paiz', 'siman.com',
        # ── Common English/global senders ─────────────────────────────────
        'donotreply', 'do-not-reply', 'no.reply',
        'marketing@', 'newsletter@', 'promo@', 'promotions@',
        'updates@', 'offers@', 'deals@', 'sales@', 'digest@',
        'billing@', 'invoices@', 'receipts@', 'support@',
        'ebay', 'walmart', 'target.com', 'bestbuy', 'kohls',
        'macys', 'homedepot', 'costco', 'nike', 'adobe',
        'microsoft', 'google.com', 'yahoo', 'aol.com',
        'meta.com', 'facebookmail', 'instagram', 'twitter', 'x.com',
        'medium.com', 'substack', 'quora', 'pinterest',
        'doordash', 'grubhub', 'lyft', 'wayfair',
        'chase.com', 'wellsfargo', 'bankofamerica', 'citi.com',
        'venmo', 'zelle',
    ]
    SPAM_SUBJECT = [
        # ── English keywords ──────────────────────────────────────────────
        'newsletter', 'unsubscribe', 'marketing', 'promo', 'offer',
        'discount', 'verify', 'verification', 'survey',
        'off on', '% off', 'free shipping', 'your opinion',
        'security alert', 'alerts and notifications', 'invoice',
        'your order has been', 'reward points', 'flea',
        'two-factor', '2fa', 'hiring', 'jobs', 'vacation',
        'health plan', 'receipt', 'transaction', 'verification code',
        'passport', 'purchase order', 'payment confirmation',
        'discover', 'easter sale', 'black friday', 'cyber monday',
        'low prices', 'your order', 'you received a document',
        'signer of', 'welcome to',
        # ── Spanish keywords (kept for Spanish inboxes) ───────────────────
        'promocion', 'oferta', 'descuento', 'encuesta',
        'off en', 'envío gratis', 'tu opinión', 'alerta de seguridad',
        'alertas y notifications', 'factura electr', 'pedido se ha entregado',
        'cmr puntos', 'antipulgas',
        'busca personal', 'empleo', 'vacaciones', 'plan de salud',
        'comprobante', 'transacción', 'código de verificación',
        'pasaporte', 'orden de compra', 'confirmación de pago',
        'dcto', 'descubre',
        'precios bajos', 'tu pedido', 'has recibido un documento',
        'firmante de', 'pagaré',
    ]

    new_emails = 0
    spam_filtered = 0
    for email in gmail_emails:
        conv_id = email.get('id', email.get('messageId', ''))
        if not conv_id:
            continue

        # Filter spam before saving
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

    inbox_result = analyze_inbox()
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

    existing = list_projects()
    existing_names = [p.get('name', '') for p in existing.get('projects', [])]

    analysis_prompt = f"""You are OneBox's email classifier. Your job is to detect emails that are REAL work projects and create them.

EXISTING PROJECTS (do NOT create duplicates):
{json.dumps(existing_names, ensure_ascii=False)}

UNASSIGNED EMAILS:
{json.dumps(email_summaries, ensure_ascii=False, indent=2)}

CRITICAL RULES:
1. ONLY ignore emails that are CLEARLY newsletters, marketing, automatic system alerts, or spam.
2. If an email is from a REAL PERSON discussing work, a project, a request, a requirement, a task, a technical issue, a quote, or any professional topic → ALWAYS create it as a project with action "create_project".
3. If the email already belongs to an existing project → action "assign_to_existing".
4. WHEN IN DOUBT, create it as a project. Better to create one project too many than to miss an important email.
5. Group emails on the SAME topic into a single project.
6. Detect blockers, decisions, risks and tasks within each project.
7. In participants, ONLY include people with a verifiable email:
   - From the "from" field: the sender with their email
   - From the "to" field: all recipients with their emails
   - From the "cc" field: all CC recipients with their emails
   - Do NOT add people mentioned in the email body who do not have an email in the from/to/cc fields
   Format: {{"name": "Name", "email": "email@example.com", "role": "Detected role"}}
8. Read the FULL BODY of the email (the "body" field) to detect tasks, blockers, decisions and risks. Don't just look at the subject.

EXAMPLES of emails that ARE projects (do NOT ignore):
- "I need a web application for..." → create_project
- "I'm sending you the requirements for..." → create_project
- "There's a problem with the server..." → create_project
- "Can you quote...?" → create_project
- Any email from a colleague/client about work → create_project

EXAMPLES of emails to IGNORE:
- "Your Amazon order has been shipped" → ignore
- "50% off on..." → ignore
- "Google security alert" → ignore
- "New job offers" → ignore

RESPOND WITH JSON ONLY:
{{
  "analysis": [
    {{"action": "ignore", "conversation_id": "...", "reason": "..."}},
    {{"action": "create_project", "project_name": "...", "project_description": "...", "project_type": "...", "participants": [{{"name": "...", "email": "email@example.com", "role": "..."}}], "emails_to_assign": ["..."], "insights": [{{"type": "blocker|decision|followup|risk|task_created", "title": "...", "description": "...", "related_person": "..."}}], "tasks": [{{"text": "...", "assigned_to": "...", "status": "pending|blocked"}}]}},
    {{"action": "assign_to_existing", "project_name": "...", "emails_to_assign": ["..."]}}
  ]
}}"""

    print("[Gmail Sync] Analyzing with LLM...")
    print(f"[Gmail Sync] Email summaries: {json.dumps(email_summaries[:3], ensure_ascii=False)[:500]}")
    response = call_llm(
        system_prompt="You are OneBox's intelligent agent. You classify emails and create projects automatically. IMPORTANT: work emails, project requests, tasks or team communications MUST create projects. Only ignore automatic newsletters, spam, verification codes and marketing alerts.",
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
            result = create_project(
                name=item['project_name'],
                description=item.get('project_description', ''),
                type=item.get('project_type', 'Other'),
                participants=item.get('participants', []),
                channels=['Gmail']
            )
            if result.get('success'):
                pid = result['projectId']
                projects_created += 1

                for conv_id in item.get('emails_to_assign', []):
                    r = assign_email_to_project(conv_id, pid, item['project_name'])
                    if r.get('success'):
                        emails_assigned += 1

                for ins in item.get('insights', []):
                    r = create_insight(pid, item['project_name'], ins['type'], ins['title'],
                                      ins.get('description', ''), ins.get('related_person', ''))
                    if r.get('success'):
                        insights_count += 1

                for task in item.get('tasks', []):
                    r = create_task(pid, task['text'], task.get('assigned_to', ''), task.get('status', 'pending'))
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
                    r = assign_email_to_project(conv_id, target_pid, target_name)
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
    """Process a Google Pub/Sub notification when a new email arrives
    and trigger the Gmail sync in background."""
    import base64 as _base64
    import threading

    message = body.get('message', {})
    data = message.get('data', '')

    email_address = ''
    if data:
        decoded = json.loads(_base64.b64decode(data).decode('utf-8'))
        email_address = decoded.get('emailAddress', '')
        history_id = decoded.get('historyId', '')
        print(f"[Gmail Push] New email for {email_address} (historyId: {history_id})")
    else:
        print("[Gmail Push] Notification without data")

    # Look up the uid of the user this Gmail email belongs to.
    # If not found, do NOT process: using a hardcoded USER_ID as a fallback
    # caused notifications from one user to end up associated with another
    # (cross-tenant data leak).
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
        print(f"[Gmail Push] No user linked to {email_address}, ignoring notification.")
        return {"ok": True, "skipped": True, "reason": "no_user_linked"}

    def _sync():
        try:
            import urllib.request as _req
            payload = json.dumps({}).encode('utf-8')
            req = _req.Request(
                "http://localhost:8006/api/scheduled/gmail-sync",
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
    """Register the Gmail watch to receive push notifications via Pub/Sub."""
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
    """Generate a Google OAuth authorization URL to connect Gmail."""
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
    """Exchange the Google OAuth code for tokens and save them.
    Returns the connected Gmail email. Raises Exception on failure."""
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
        raise Exception("Token exchange returned without access_token")

    # If Google does not send a refresh_token (typical when you've authorized
    # before), reuse the existing one. If we don't have one stored either,
    # fail: without refresh_token the cron cannot sync.
    if not refresh_token:
        existing = user_tokens_table.get_item(Key={'userId': uid}).get('Item', {})
        refresh_token = existing.get('gmailRefreshToken', '')
        if not refresh_token:
            raise Exception(
                "Google did not return a refresh_token and none was stored. "
                "Revoke access at https://myaccount.google.com/permissions and retry."
            )

    # Request user info (email). WITHOUT email we cannot save: the cron
    # needs to know which account to sync. Previously we stored
    # gmailEmail='' and the result was "connected but invisible".
    user_info_resp = _requests.get(
        'https://www.googleapis.com/oauth2/v2/userinfo',
        headers={'Authorization': f'Bearer {access_token}'},
        timeout=15
    )
    print(f"[Gmail OAuth] userinfo status: {user_info_resp.status_code}")
    if user_info_resp.status_code != 200:
        raise Exception(
            f"Userinfo failed: {user_info_resp.status_code} {user_info_resp.text[:200]}"
        )
    user_info = user_info_resp.json()
    gmail_email = (user_info.get('email') or '').strip().lower()
    if not gmail_email:
        raise Exception(
            f"Userinfo did not return an email. Response: {json.dumps(user_info)[:200]}"
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
    """Check whether the user has Gmail connected."""
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
    """Disconnect the user's Gmail."""
    try:
        user_tokens_table.delete_item(Key={'userId': uid})
        return {"success": True}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
