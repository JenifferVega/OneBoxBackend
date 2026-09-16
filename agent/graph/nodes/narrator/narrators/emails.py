"""Narrator for email results (list/inspect/send)."""

GUIDANCE = """## GUIDE FOR EMAILS:
1. If there are emails, mention: count, main senders, topics.
2. If there are NO emails (count: 0), explain that none were found and suggest alternatives
   (another query, no "from:", check the unassigned inbox).
3. If an email was sent, confirm the recipient and subject.

## EXAMPLE:
"**Action completed:**
- Sent a follow-up email to juan@company.com about the pending invoice

Anything else you need?\""""
