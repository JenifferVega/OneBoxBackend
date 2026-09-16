"""Notifications narrator (WhatsApp/SMS, project contacts)."""

GUIDANCE = """## GUIDE FOR NOTIFICATIONS:
1. If notifications were sent (status "sent"), confirm to whom (name and channel) and summarize the content.
2. If any send failed (error, no phone, invalid number or email, _schedule_error), say so
   clearly and explain the reason; do NOT present it as sent.
3. If it was SCHEDULED (status "scheduled_pending"/"scheduled"/"scheduled_recurring"), say it was
   scheduled for the date/time from the result (scheduled_at) to the recipient from the result, clarifying
   that it has NOT been sent yet (it will go out at the scheduled moment). Do not say "sent".
4. If contacts were queried, list who has a phone/email and what they have pending.

## EXAMPLE:
"**Notifications sent:**
- WhatsApp to **Maria** (+34 612...): 2 pending tasks
- WhatsApp to **Juan** (+50 494...): 1 blocked task

**Pedro** has no phone on record — I can email him if you want.\""""
