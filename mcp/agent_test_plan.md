# OneBox Agent Test Plan — AI System Evaluation

**Date:** 2026-06-09  
**Goal:** Test each agent tool turn by turn with `onebox_chat`, spot problems in the planner/narrator/validator, and produce a diagnostic of the current state of the AI system.

---

## Prerequisites before starting

1. Server running: `uvicorn main:app --reload`
2. Health check: `GET http://localhost:8000/health`
3. MCP connected: tools `onebox_chat`, `onebox_reset`, `onebox_report`, `onebox_export` visible in Claude
4. Clean the session: `onebox_reset(session_id="test-plan")`

---

## Tools to cover (14 total)

| # | Tool | Category |
|---|---|---|
| 1 | `list_emails` | Email |
| 2 | `inspect_email` | Email |
| 3 | `analyze_inbox` | Email |
| 4 | `auto_classify_messages` | Email |
| 5 | `send_email` | Email |
| 6 | `list_projects` | Projects |
| 7 | `create_project` | Projects |
| 8 | `create_task` | Projects |
| 9 | `assign_email_to_project` | Projects |
| 10 | `create_insight` | Projects |
| 11 | `get_project_contacts` | Notifications |
| 12 | `send_notification` | Notifications |
| 13 | `list_notifications` | Notifications |
| 14 | `create_reminder` | Utilities |
| 15 | `check_sla` | Utilities |
| 16 | `proactive_summary` | Utilities |

---

## Test scenarios

### SESSION 1 — Email and classification
**session_id:** `"test-email"`

| Turn | Message | Expected tool | What to check |
|---|---|---|---|
| 1 | `"show me my emails"` | `list_emails` | Lists with no query? Does the narrator show subjects/senders? |
| 2 | `"LinkedIn emails"` | `list_emails(query="from:linkedin")` | Does it use `from:` for a known sender? |
| 3 | `"inspect the first email"` | `inspect_email` | Does it resolve the previous result's `email_id` via `from_step`? |
| 4 | `"analyze my inbox"` | `analyze_inbox` | Does it return unassigned emails? Does the narrator present them well? |
| 5 | `"classify those messages"` | `auto_classify_messages` | Does it suggest projects? Does the narrator explain the suggestions? |
| 6 | `"send a follow-up to test@company.com about the topic of the previous email"` | `send_email` | Does it ask for missing data if there's no subject/body? Does it avoid inventing an email? |

**Warning signals:**
- `iteration > 1` in any turn → planner rule not clear enough
- Narrator that doesn't mention the emails found or doesn't propose a next action
- `inspect_email` with an invented `email_id` instead of `from_step`

---

### SESSION 2 — Projects and tasks
**session_id:** `"test-projects"`

| Turn | Message | Expected tool | What to check |
|---|---|---|---|
| 1 | `"show me my projects"` | `list_projects` | Correct listing? Does the narrator give names and IDs? |
| 2 | `"I want to create a project"` | `direct_response` (asks for data) | Does it ask for name, type and description before acting? |
| 3 | `"it's called Alpha, it's a backend project. Led by Laura García, coordinated by Daniel Rojas. It has 3 phases: Design, Development and Deployment, lasting 2 months"` | `create_project + 3x create_task` | Does it extract participants? Does it create the 3 phases as tasks? Realistic dates? |
| 4 | `"create an urgent task: review Q3 budget"` | `list_projects` (only) | Does it NOT try to create_task before the user picks a project? |
| 5 | `"in the Alpha project"` | `list_projects + create_task` | Does it use `match` by name to resolve project_id? |
| 6 | `"which tasks are blocked or overdue?"` | `check_sla` | Does it scan all projects? Does the narrator list the blocked ones? |

**Warning signals:**
- Turn 2: the planner runs `create_project` without a description → **critical bug**
- Turn 3: only creates the project but not the tasks → missing FULL_EXTRACTION rule
- Turn 4: includes `create_task` in the same plan → **forbidden by catalog.py**
- Invented or empty `project_id` in any tool

---

### SESSION 3 — Notifications
**session_id:** `"test-notifications"`

| Turn | Message | Expected tool | What to check |
|---|---|---|---|
| 1 | `"show me my projects"` | `list_projects` | Needed to get the project_id for Alpha |
| 2 | `"send the pending items via WhatsApp to the Alpha team"` | `get_project_contacts + send_notification(foreach)` | Does it use `foreach` to send to all? Does it avoid generating one step per contact? |
| 3 | `"show me the notifications sent for Alpha"` | `list_notifications(project_id)` | Does it filter by project correctly? |
| 4 | `"send a WhatsApp to +50494622817 that the deploy was successful"` | `send_notification` | Does it use the given number directly without asking unnecessary confirmation? |

**Warning signals:**
- Turn 2: generates `N` `send_notification` steps instead of 1 with `foreach` → **catalog bug**
- Planner asks the user for the phone number when it should get it from `get_project_contacts`

---

### SESSION 4 — Reminders and summary
**session_id:** `"test-utils"`

| Turn | Message | Expected tool | What to check |
|---|---|---|---|
| 1 | `"set me a reminder"` | `direct_response` (asks for title and date) | Does it avoid creating the reminder without data? |
| 2 | `"review contract with client X, for 2026-06-20"` | `create_reminder` | Does it pick up title and date from context? |
| 3 | `"give me a summary of how everything is going"` | `proactive_summary` | Does it cover projects, tasks, SLA? Does the narrator give a useful executive summary? |
| 4 | `"is anything urgent?"` | `check_sla` | Does it avoid unnecessarily re-invoking `proactive_summary`? |

**Warning signals:**
- Turn 1: creates a reminder with a generic title → **violates REQUIRED_PARAMS**
- Turn 3: narrator gives a vague response without structuring the projects found

---

### SESSION 5 — Complex multi-step flow (integration)
**session_id:** `"test-integration"`

| Turn | Message | Expected tool | What to check |
|---|---|---|---|
| 1 | `"create a Marketing project for the summer campaign, with Ana López (ana@company.com) as lead and Marcos Ruiz as designer. Duration: 1 month"` | `create_project + 4x create_task` (inferred phases) | Does it infer Marketing phases: Research, Strategy, Execution, Measurement? |
| 2 | `"send an email to ana@company.com telling her the project has started"` | `send_email` | Does it take the email from the previous turn's context? |
| 3 | `"classify the inbox and assign the relevant emails to the campaign project"` | `auto_classify_messages + assign_email_to_project` | Does it chain both tools correctly? |

---

## AI system evaluation criteria

### Planner
| Criterion | Weight | Indicator |
|---|---|---|
| Does not invent parameters (`project_id`, `email_id`, phones) | High | No `_validation_error` in debug_info |
| Resolves data via `from_step` correctly | High | `iteration == 1` in 90% of turns |
| Honors REQUIRED_PARAMS (asks for data before acting) | High | `direct_response` on turns without enough data |
| Extracts participants and phases from rich descriptions | Medium | Plan with N+1 steps when there are N phases |
| Uses `foreach` for mass notifications | Medium | A single `send_notification` step with foreach |
| Doesn't create unnecessary projects to resolve `project_id` | High | Never `create_project` when only the ID is needed |

### Narrator
| Criterion | Weight | Indicator |
|---|---|---|
| Mentions concrete entities (names, dates, emails) | High | No vague responses like "done successfully" |
| Proposes a natural next action | Medium | Each response suggests what the user can do |
| Reports when a contact has no phone | Medium | Doesn't silence `foreach` omissions |
| Structured executive summary in `proactive_summary` | High | Covers projects, tasks, blocked SLA |

### Validator
| Criterion | Weight | Indicator |
|---|---|---|
| Doesn't reject correct results | High | No unjustified `replan` in debug_info |
| Detects `_validation_error` correctly | High | Reroutes to the planner on real errors |

---

## How to run this plan

```
# Before each session
onebox_reset(session_id="<session-id>")

# Each turn
onebox_chat("<message>", session_id="<session-id>")
→ Check: agent response, plan executed, iterations, tools used

# When each session is done
onebox_report(session_id="<session-id>")
onebox_export(session_id="<session-id>", filename="report-<session-id>")
```

After each session, compare the results against the criteria table and note:
- ✅ Pass / ❌ Fail / ⚠️ Partial
- If there is ❌ or ⚠️: identify the file to tweak (`catalog.py`, `prompts.py`, `narrators/`) and propose the change.

---

## Per-session report template

```
## Session: <name>
Date: YYYY-MM-DD

### Per-turn results
| Turn | Message | Actual tool | Iterations | Errors | Status |
|---|---|---|---|---|---|
| 1 | ... | ... | 1 | none | ✅ |

### Detected issues
1. [Issue description] — Likely cause: [catalog.py / prompts.py / narrators/]

### Proposed changes
- catalog.py line X: [before] → [after]
```
