"""Twilio and SES configuration, phone normalization and notification logging.

Moved verbatim out of the old single-file agent/tools.py.
"""

from typing import Optional
import boto3
import os
import re
import uuid

from agent.tools.context import _current_uid
from agent.tools.db import notifications_table



TWILIO_ACCOUNT_SID = os.environ.get("TWILIO_ACCOUNT_SID", "")
TWILIO_AUTH_TOKEN = os.environ.get("TWILIO_AUTH_TOKEN", "")
TWILIO_PHONE_NUMBER = os.environ.get("TWILIO_PHONE_NUMBER", "+15005550006")
TWILIO_WHATSAPP_NUMBER = os.environ.get("TWILIO_WHATSAPP_NUMBER", "whatsapp:+14155238886")
TWILIO_TEST_MODE = os.environ.get("TWILIO_TEST_MODE", "false").lower() == "true"
# Messaging Service SID — required for native Twilio scheduling.
# Without this SID, scheduled notifications (WhatsApp/SMS) are stored in
# DynamoDB and the EventBridge dispatcher sends them at the correct time.
TWILIO_MESSAGING_SERVICE_SID = os.environ.get("TWILIO_MESSAGING_SERVICE_SID", "")

# ============================================================================
# SES (Amazon Simple Email Service) — for the "email" channel
# ----------------------------------------------------------------------------
# Today we are in SANDBOX: you can only send to verified emails in SES.
# That is why _ses_check_verified gates before sending — if it isn't
# verified, we log it as 'skipped_unverified' and DO NOT try to send.
# Once AWS approves production access, the gate becomes a no-op (all pass).
#
# The FROM is jenifferf.funezp@gmail.com (verified in SES). Can be changed
# via env var SES_FROM_EMAIL with no redeploy if we get our own domain later.
# ============================================================================
SES_FROM_EMAIL = os.environ.get("SES_FROM_EMAIL", "jenifferf.funezp@gmail.com")
SES_REGION = os.environ.get("SES_REGION", os.environ.get("AWS_REGION", "us-east-1"))
# The SES account is already in PRODUCTION → we can send to any recipient
# without verifying them. The verification gate only applies when
# SES_SANDBOX=true (useful for test environments with sandbox access).
# Default: production (no gate).
SES_SANDBOX = os.environ.get("SES_SANDBOX", "false").lower() == "true"
_ses_client = None
# Local verification cache — avoids calling SES on every email.
# Resets when the container restarts (which is OK because adding a new
# verified address in SES is not very frequent).
_ses_verified_cache: dict = {}


def _get_ses_client():
    """Lazy init of the SES client. Uses the ECS task IAM role (no API keys)."""
    global _ses_client
    if _ses_client is None:
        _ses_client = boto3.client('ses', region_name=SES_REGION)
    return _ses_client


def _ses_check_verified(email: str) -> bool:
    """Checks whether an email is verified in SES (required in sandbox).
    Caches the result so we don't query on every send."""
    email = (email or '').strip().lower()
    if not email:
        return False
    if email in _ses_verified_cache:
        return _ses_verified_cache[email]
    try:
        client = _get_ses_client()
        result = client.get_identity_verification_attributes(Identities=[email])
        attrs = result.get('VerificationAttributes', {}).get(email, {})
        verified = attrs.get('VerificationStatus') == 'Success'
        _ses_verified_cache[email] = verified
        return verified
    except Exception as e:
        print(f"[SES] Error checking verification for {email}: {e}")
        return False



# E.164 validation: + followed by 8 to 15 digits (the first is not 0).
E164_REGEX = re.compile(r'^\+[1-9]\d{7,14}$')


def _normalize_e164(phone: str) -> Optional[str]:
    """Normalizes a phone number to E.164 format. Returns None if invalid.
    Keeps Twilio from silently failing on malformed numbers."""
    if not phone:
        return None
    p = str(phone).replace('whatsapp:', '').strip()
    if E164_REGEX.match(p):
        return p
    digits = re.sub(r'\D', '', p)
    if not digits:
        return None
    candidate = '+' + digits
    return candidate if E164_REGEX.match(candidate) else None


def _log_notification(now, project_id, project_name, channel, recipient, message,
                      sid='', status='unknown', error='',
                      scheduled_at='', is_recurring=False, recurring_days=None):
    """Logs EVERY send attempt in onebox-notifications, successful or not.
    status: sent/queued/failed/invalid_phone/twilio_error/not_configured/test_simulated/pending."""
    try:
        item = {
            'userId': _current_uid(),
            'notificationId': f"{now}#{uuid.uuid4().hex[:8]}",
            'projectId': project_id,
            'projectName': project_name,
            'channel': channel,
            'recipient': recipient,
            'message': message,
            'twilioSid': sid,
            'status': status,
            'errorMessage': error,
            'createdAt': now,
        }
        if scheduled_at:
            item['scheduledAt'] = scheduled_at
        if is_recurring:
            item['isRecurring'] = True
            item['recurringDays'] = recurring_days or []
        notifications_table.put_item(Item=item)
    except Exception as e:
        print(f"[Tool] could not log notification: {e}")
