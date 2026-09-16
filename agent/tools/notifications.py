"""send_notification (WhatsApp/SMS/email) and the notification log.

Moved verbatim out of the old single-file agent/tools.py.
"""

from datetime import datetime
import uuid

from agent.tools.access import _has_project_access
from agent.tools.channels import (
    SES_FROM_EMAIL,
    SES_SANDBOX,
    TWILIO_ACCOUNT_SID,
    TWILIO_AUTH_TOKEN,
    TWILIO_MESSAGING_SERVICE_SID,
    TWILIO_PHONE_NUMBER,
    TWILIO_TEST_MODE,
    TWILIO_WHATSAPP_NUMBER,
    _get_ses_client,
    _log_notification,
    _normalize_e164,
    _ses_check_verified,
)
from agent.tools.context import _current_uid
from agent.tools.db import conversations_table, notifications_table
from agent.tools.registry import register_tool


@register_tool("send_notification")
def send_notification(
    recipient: str,
    message: str,
    channel: str = "whatsapp",
    project_id: str = "",
    project_name: str = "",
    scheduled_at: str = "",
    recurring_days: list = None,
) -> dict:
    """Sends or schedules a notification via WhatsApp, SMS or email.
    - Without scheduled_at or recurring_days: immediate send (previous behavior).
    - With scheduled_at: schedules for that UTC date/time.
      · WhatsApp/SMS + TWILIO_MESSAGING_SERVICE_SID → native Twilio scheduling.
      · Email or without Messaging Service → stored in DynamoDB (EventBridge dispatcher sends).
    - With recurring_days: stored in DynamoDB with isRecurring=True; the
      EventBridge dispatcher executes the send every week on the given days.
    Validates the phone to E.164 before sending and logs every attempt (success or failure)."""
    # SECURITY: if a project_id is provided, it must be accessible to the user.
    if project_id and not _has_project_access(project_id):
        return {"error": "No access to that project"}
    now = datetime.utcnow().isoformat()

    # ──────────────────────────────────────────────────────────────────────
    # RECURRING NOTIFICATION — always goes to DynamoDB regardless of channel.
    # The EventBridge dispatcher decides when to send it based on recurring_days.
    # ──────────────────────────────────────────────────────────────────────
    if recurring_days:
        valid_days = {"monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"}
        normalized = [d.strip().lower() for d in recurring_days if d.strip().lower() in valid_days]
        if not normalized:
            return {"error": "invalid recurring_days. Use: monday,tuesday,wednesday,thursday,friday,saturday,sunday"}
        _log_notification(
            now, project_id, project_name, channel, recipient, message,
            status='pending', scheduled_at=scheduled_at or '',
            is_recurring=True, recurring_days=normalized,
        )
        print(f"[Tool] send_notification ← RECURRING stored (days: {normalized})")
        return {
            "success": True,
            "status": "scheduled_recurring",
            "channel": channel,
            "recipient": recipient,
            "recurring_days": normalized,
        }

    # ──────────────────────────────────────────────────────────────────────
    # EMAIL CHANNEL (SES) — independent branch, does not enter the Twilio flow.
    # SES does not support native scheduling: if scheduled_at is set, it
    # always goes to DynamoDB and the EventBridge dispatcher sends it at
    # the correct time.
    # ──────────────────────────────────────────────────────────────────────
    if channel == "email":
        # SCHEDULED EMAIL → DynamoDB (SES has no native scheduling)
        if scheduled_at:
            target = (recipient or '').strip().lower()
            _log_notification(
                now, project_id, project_name, channel, target, message,
                status='pending', scheduled_at=scheduled_at,
            )
            print(f"[Tool] send_notification ← SCHEDULED EMAIL stored for {scheduled_at}")
            return {
                "success": True,
                "status": "scheduled_pending",
                "channel": channel,
                "recipient": target,
                "scheduled_at": scheduled_at,
            }
        target = (recipient or '').strip().lower()
        if not target or '@' not in target:
            _log_notification(now, project_id, project_name, channel, recipient, message,
                              status='invalid_email', error=f"Invalid email: '{recipient}'")
            return {"error": f"Invalid email: '{recipient}'", "status": "invalid_email"}

        # In SES SANDBOX we can only send to verified addresses → explicit gate.
        # Once AWS approves production access the gate becomes transparent
        # (all emails will be "verified" from the API's point of view).
        # ── SANDBOX LOCK ────────────────────────────────────────────────────
        # The SES account is already in PRODUCTION → we send to any recipient.
        # The gate only kicks in if SES_SANDBOX=true (test environment); in
        # production (default) this condition is False and never blocks.
        if SES_SANDBOX and not _ses_check_verified(target):
            _log_notification(now, project_id, project_name, channel, target, message,
                              status='skipped_unverified',
                              error=f"Email not verified in SES (sandbox): {target}")
            print(f"[SES] SKIP: {target} is not verified in SES sandbox")
            return {"status": "skipped_unverified", "recipient": target}

        try:
            subject = f"[OneBox] {project_name}" if project_name else "[OneBox] Notification"
            # Basic HTML: wrap the message in a minimal structure. The current
            # cron generates the message in plain text with \n; here we
            # convert it to <br> so it looks decent in the inbox.
            safe_html = (
                message
                .replace('&', '&amp;')
                .replace('<', '&lt;')
                .replace('>', '&gt;')
                .replace('\n', '<br>')
            )
            html_body = (
                "<html><body style=\"font-family:Arial,sans-serif;color:#222;\">"
                f"<div style=\"max-width:600px;margin:0 auto;padding:20px;\">"
                f"<p style=\"white-space:pre-wrap;\">{safe_html}</p>"
                f"<hr style=\"border:none;border-top:1px solid #eee;margin:24px 0 12px;\">"
                f"<p style=\"font-size:12px;color:#888;\">Sent by OneBox · "
                f"<a href=\"https://www.oneboxmanager.com\" style=\"color:#7c3aed;text-decoration:none;\">"
                f"oneboxmanager.com</a></p>"
                f"</div></body></html>"
            )
            client = _get_ses_client()
            response = client.send_email(
                Source=SES_FROM_EMAIL,
                Destination={'ToAddresses': [target]},
                Message={
                    'Subject': {'Data': subject, 'Charset': 'UTF-8'},
                    'Body': {
                        'Text': {'Data': message, 'Charset': 'UTF-8'},
                        'Html': {'Data': html_body, 'Charset': 'UTF-8'},
                    }
                }
            )
            ses_message_id = response.get('MessageId', '')
            print(f"[SES] sent to={target} messageId={ses_message_id}")
            _log_notification(now, project_id, project_name, channel, target, message,
                              sid=ses_message_id, status='sent')
            return {"success": True, "messageId": ses_message_id, "status": "sent"}
        except Exception as e:
            err_msg = str(e)[:300]
            print(f"[SES] ERROR sending to {target}: {err_msg}")
            _log_notification(now, project_id, project_name, channel, target, message,
                              status='ses_error', error=err_msg)
            return {"error": err_msg, "status": "ses_error"}

    # ──────────────────────────────────────────────────────────────────────
    # TWILIO CHANNELS (whatsapp, sms)
    # ──────────────────────────────────────────────────────────────────────
    # 1) Validate/normalize the phone number BEFORE touching Twilio.
    clean = _normalize_e164(recipient)
    if not clean:
        _log_notification(now, project_id, project_name, channel, recipient, message,
                          status='invalid_phone', error=f"Phone not E.164: '{recipient}'")
        return {"error": f"Invalid phone: '{recipient}' (expected E.164, e.g.: +34600123456)",
                "status": "invalid_phone"}

    if channel == "whatsapp":
        from_number = TWILIO_WHATSAPP_NUMBER or "whatsapp:+14155238886"
        to_number = f"whatsapp:{clean}"
    else:
        from_number = TWILIO_PHONE_NUMBER or "+15005550006"
        to_number = clean

    print(f"[Tool] send_notification → {channel} to {to_number} (test_mode={TWILIO_TEST_MODE})")

    # 2) NATIVE TWILIO SCHEDULING (WhatsApp/SMS with scheduled_at)
    # Requires MessagingServiceSid. If not configured, falls back to DynamoDB.
    if scheduled_at and not TWILIO_TEST_MODE:
        if TWILIO_MESSAGING_SERVICE_SID:
            if not TWILIO_ACCOUNT_SID or not TWILIO_AUTH_TOKEN:
                _log_notification(now, project_id, project_name, channel, clean, message,
                                  status='not_configured', error='Twilio without credentials',
                                  scheduled_at=scheduled_at)
                return {"error": "Twilio not configured (missing credentials).", "status": "not_configured"}
            try:
                from twilio.rest import Client
                from datetime import timezone
                client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
                # Twilio requires UTC ISO 8601 with an explicit timezone.
                send_time = scheduled_at if scheduled_at.endswith('Z') or '+' in scheduled_at[10:] else scheduled_at + 'Z'
                tw_message = client.messages.create(
                    body=message,
                    messaging_service_sid=TWILIO_MESSAGING_SERVICE_SID,
                    to=to_number,
                    schedule_type='fixed',
                    send_time=send_time,
                )
                sid = tw_message.sid
                tw_status = tw_message.status  # should be 'scheduled'
                print(f"[Tool] send_notification ← TWILIO SCHEDULED (SID: {sid}, send_time: {send_time})")
                _log_notification(now, project_id, project_name, channel, clean, message,
                                  sid=sid, status=tw_status, scheduled_at=scheduled_at)
                return {
                    "success": True,
                    "sid": sid,
                    "status": tw_status,
                    "channel": channel,
                    "recipient": clean,
                    "scheduled_at": scheduled_at,
                    "mode": "TWILIO_SCHEDULED",
                }
            except Exception as e:
                print(f"[Tool] Twilio scheduling error, falling back to DynamoDB: {e}")
                # If Twilio fails (e.g. invalid date format), we fall back to DynamoDB.
        # Without Messaging Service SID or if Twilio failed → store in DynamoDB
        _log_notification(now, project_id, project_name, channel, clean, message,
                          status='pending', scheduled_at=scheduled_at)
        print(f"[Tool] send_notification ← SCHEDULED in DynamoDB for {scheduled_at}")
        return {
            "success": True,
            "status": "scheduled_pending",
            "channel": channel,
            "recipient": clean,
            "scheduled_at": scheduled_at,
        }

    # 3) Send immediately (or simulate in test_mode).
    if TWILIO_TEST_MODE:
        sid = f"SM_TEST_{uuid.uuid4().hex[:16]}"
        tw_status = "test_simulated"
        print(f"[Tool] send_notification ← SIMULATED (SID: {sid})")
    else:
        if not TWILIO_ACCOUNT_SID or not TWILIO_AUTH_TOKEN or "xxx" in TWILIO_ACCOUNT_SID:
            _log_notification(now, project_id, project_name, channel, clean, message,
                              status='not_configured', error='Twilio without credentials')
            return {"error": "Twilio not configured (missing credentials).", "status": "not_configured"}
        try:
            from twilio.rest import Client
            client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
            tw_message = client.messages.create(body=message, from_=from_number, to=to_number)
            sid = tw_message.sid
            tw_status = tw_message.status
            print(f"[Tool] send_notification ← OK (SID: {sid}, status: {tw_status})")
        except Exception as e:
            # Twilio failed: log the actual error (previously swallowed silently).
            _log_notification(now, project_id, project_name, channel, clean, message,
                              status='twilio_error', error=str(e))
            return {"error": f"Twilio error: {str(e)}", "status": "twilio_error"}

    # 4) Log the send (with its real status) and the project conversation.
    _log_notification(now, project_id, project_name, channel, clean, message, sid=sid, status=tw_status)

    if project_id:
        try:
            conversations_table.put_item(Item={
                'projectId': project_id,
                'conversationId': f"twilio#{sid}",
                'userId': _current_uid(),
                'from': 'OneBox IA',
                'fromEmail': '',
                'subject': f'{channel.upper()} notification sent',
                'body': message,
                'date': now,
                'channel': channel,
                'status': 'sent',
                'createdAt': now,
            })
        except Exception:
            pass

    return {
        "success": True,
        "sid": sid,
        "status": tw_status,
        "channel": channel,
        "recipient": clean,
        "mode": "TEST" if TWILIO_TEST_MODE else "REAL",
    }


@register_tool("list_notifications")
def list_notifications(project_id: str = "") -> dict:
    """Lists sent notifications, optionally filtered by project.

    - Without project_id: only notifications where the uid is the owner (own ones).
    - With project_id: requires the user to have access to the project. If
      they do, returns ALL of the project's notifications (not only theirs) —
      so invitees also see the project's notifications.
    """
    try:
        from boto3.dynamodb.conditions import Key, Attr
        if project_id:
            if not _has_project_access(project_id):
                return {"error": "No access to that project"}
            result = notifications_table.scan(
                FilterExpression=Attr('projectId').eq(project_id)
            )
        else:
            result = notifications_table.scan(
                FilterExpression=Attr('userId').eq(_current_uid())
            )
        items = sorted(result.get('Items', []), key=lambda x: x.get('createdAt', ''), reverse=True)
        return {"count": len(items), "notifications": items[:50]}
    except Exception as e:
        return {"error": str(e)}
