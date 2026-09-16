"""Email tools: inbox listing/inspection and outbound SES sends.

Moved verbatim out of the old single-file agent/tools.py.
"""

from datetime import datetime
from urllib.parse import urlencode
import requests
import uuid

from agent.tools.channels import (
    SES_FROM_EMAIL,
    SES_SANDBOX,
    _get_ses_client,
    _ses_check_verified,
)
from agent.tools.context import _current_uid
from agent.tools.db import (
    GMAIL_INSPECT_API,
    GMAIL_LIST_API,
    conversations_table,
    insights_table,
)
from agent.tools.registry import register_tool


@register_tool("list_emails")
def list_emails(query: str = "", max_results: int = 50) -> dict:
    """
    Searches and lists Gmail emails.
    """
    try:
        params = {
            'max_results': min(max_results, 100)
        }
        if query:
            params['query'] = query

        url = GMAIL_LIST_API + "?" + urlencode(params)

        print(f"[Tool] list_emails → {url}")
        response = requests.get(url, timeout=60)
        response.raise_for_status()
        data = response.json()
        print(f"[Tool] list_emails ← {data.get('count', 0)} emails")
        return data
    except requests.RequestException as e:
        return {"error": f"Error listing emails: {str(e)}", "emails": []}


@register_tool("inspect_email")
def inspect_email(email_id: str) -> dict:
    """
    Inspects a specific email; downloads content and attachments.
    """
    if not email_id:
        return {"error": "email_id is required"}

    try:
        url = f"{GMAIL_INSPECT_API}?email_id={email_id}"
        print(f"[Tool] inspect_email → {url}")
        response = requests.get(url, timeout=60)
        response.raise_for_status()
        data = response.json()
        print(f"[Tool] inspect_email ← OK")
        return data
    except requests.RequestException as e:
        return {"error": f"Error inspecting email: {str(e)}"}



@register_tool("analyze_inbox")
def analyze_inbox() -> dict:
    """Reads all unassigned emails from DynamoDB."""
    try:
        from boto3.dynamodb.conditions import Key, Attr
        result = conversations_table.scan(
            FilterExpression=Attr('projectId').eq('unassigned') & Attr('userId').eq(_current_uid())
        )
        emails = result.get('Items', [])
        for email in emails:
            if email.get('body'):
                email['body'] = email['body'][:500]
        return {"count": len(emails), "emails": emails}
    except Exception as e:
        return {"error": str(e)}


@register_tool("send_email")
def send_email(recipient_email: str, subject: str, body: str, project_id: str = "", project_name: str = "") -> dict:
    """Sends a REAL follow-up email via Amazon SES and logs it in DynamoDB.

    Sandbox gate (same pattern as the 'email' channel in send_notification):
    while SES is in sandbox, we can only send to verified addresses. If the
    recipient is NOT verified, we do NOT try to send: it is logged as
    'skipped_unverified' and that status is returned (without an ugly error).
    Once AWS approves production access, _ses_check_verified stops blocking
    and everyone goes through.
    """
    try:
        now = datetime.utcnow().isoformat()
        email_sid = f"email_{uuid.uuid4().hex[:12]}"
        target = (recipient_email or "").strip().lower()

        print(f"[Tool] send_email → {target} | Subject: {subject}")

        # ── Basic validation ───────────────────────────────────────────────
        if not target or "@" not in target:
            return {"error": f"Invalid email: '{recipient_email}'", "status": "invalid_email"}

        # ── Real send via SES ──────────────────────────────────────────────
        # In production (SES_SANDBOX=false, default) we send to anyone.
        # Only in sandbox do we require the recipient to be verified.
        if SES_SANDBOX and not _ses_check_verified(target):
            status = "skipped_unverified"
            print(f"[SES] {target} NOT verified in sandbox → not sending (skipped_unverified)")
        else:
            try:
                client = _get_ses_client()
                response = client.send_email(
                    Source=SES_FROM_EMAIL,
                    Destination={'ToAddresses': [target]},
                    Message={
                        'Subject': {'Data': subject or '', 'Charset': 'UTF-8'},
                        'Body': {'Text': {'Data': body or '', 'Charset': 'UTF-8'}},
                    },
                )
                email_sid = response.get('MessageId', email_sid)
                status = "sent"
                print(f"[SES] send_email sent to={target} messageId={email_sid}")
            except Exception as ses_err:
                err_msg = f"SES error sending to {target}: {ses_err}"
                print(f"[SES] {err_msg}")
                # Log the failed attempt for traceability and return an error.
                try:
                    conversations_table.put_item(Item={
                        'projectId': project_id or 'unassigned',
                        'conversationId': f"email_out#{email_sid}",
                        'userId': _current_uid(),
                        'from': 'OneBox IA', 'fromEmail': SES_FROM_EMAIL,
                        'to': target, 'subject': subject, 'body': body,
                        'date': now, 'channel': 'gmail', 'status': 'ses_error',
                        'type': 'outbound', 'createdAt': now,
                    })
                except Exception:
                    pass
                return {"error": err_msg, "status": "ses_error", "recipient": target}

        conv_id = f"email_out#{email_sid}"
        try:
            conversations_table.put_item(Item={
                'projectId': project_id or 'unassigned',
                'conversationId': conv_id,
                'userId': _current_uid(),
                'from': 'OneBox IA',
                'fromEmail': SES_FROM_EMAIL,
                'to': target,
                'subject': subject,
                'body': body,
                'date': now,
                'channel': 'gmail',
                'status': status,
                'type': 'outbound',
                'createdAt': now
            })
        except Exception:
            pass

        try:
            insight_id = f"{now}#{uuid.uuid4().hex[:8]}"
            insights_table.put_item(Item={
                'userId': _current_uid(),
                'insightId': insight_id,
                'projectId': project_id,
                'projectName': project_name,
                'type': 'followup',
                'title': f'Follow-up email sent to {target}',
                'description': f'Subject: {subject}',
                'actor': 'OneBox IA',
                'relatedPerson': target,
                'actionsTaken': [f'Sent email: {subject}'],
                'status': 'executed' if status == 'sent' else 'skipped',
                'createdAt': now
            })
        except Exception:
            pass

        print(f"[Tool] send_email ← {status} (ID: {email_sid})")
        if status == "skipped_unverified":
            return {
                "success": False,
                "email_id": email_sid,
                "status": status,
                "recipient": target,
                "subject": subject,
                "message": (
                    f"Not sent: '{target}' is not verified in SES (sandbox). "
                    f"Verify that address in SES or request production access to send to anyone."
                ),
            }
        return {
            "success": True,
            "email_id": email_sid,
            "status": status,
            "recipient": target,
            "subject": subject,
            "mode": "SES",
        }
    except Exception as e:
        return {"error": f"Error sending email: {str(e)}"}
