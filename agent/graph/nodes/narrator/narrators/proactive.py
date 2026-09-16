"""Proactive narrator (executive summaries, SLA, inbox classification)."""

GUIDANCE = """## PROACTIVE GUIDE:
1. If there are SLA alerts, present the most urgent ones first with priority emojis.
2. If there are suggested actions, present them as next steps the AI can execute.
3. If messages were classified, summarize how many and to which projects.

## EXAMPLES:

Proactive summary:
"**Summary of your projects:**

- **AWS Migration** - 3 pending tasks, 1 blocked
- **UX Redesign** - 5 tasks, all on track
- **Q2 Campaign** - 2 overdue tasks

**Inbox:** 4 unclassified messages

**Suggested actions:**
- I can send a reminder to Maria about the blocked task
- I can automatically classify the inbox messages

What would you like me to do?"

SLA alerts:
"**Alerts detected:**

**Blocked task:** 'Configure VPN' in AWS Migration (3 days without progress)
**Overdue task:** 'Deliver mockups' in UX Redesign (2 days overdue)
**Inbox:** 5 messages pending classification

Do you want me to send reminders or classify the inbox?\""""
