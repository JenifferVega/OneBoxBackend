"""Planner tool catalog and guidance (prompt sections).

Decomposes the previous monolithic PLANNER_PROMPT into named sections.
The authoritative reference for parameters is agent.tools.TOOLS_DESCRIPTION;
this file complements it with curated usage and combination guidance.
"""
from agent.tools import TOOLS_DESCRIPTION

# get_project_contacts is registered in TOOL_MAP but was missing from
# TOOLS_DESCRIPTION — it's documented here so the planner knows about it.
_EXTRA_TOOLS = """
- get_project_contacts(project_id): Returns the project's participants
  with their phones and pending tasks. ALWAYS use it before sending
  bulk notifications to know who has a phone and what pending work they have.

- prepare_trello_board(project_query): Read-only. Finds the project by name AND
  reports whether it already has a Trello board, in ONE call. Always the FIRST
  step when asked to create a board.
    - can_create: false -> it already has one; `already_linked` names it.
    - can_create: true  -> propose creating it and ask for confirmation.

- create_trello_board(name, project_id, lists): Creates a NEW board in the
  user's real Trello account. REQUIRES the user to agree first.
    - name: REQUIRED, the board name. Never omit it.
    - project_id: links the project to the new board so later pushes need no
      list. lists: column names in order; defaults to OneBox's own statuses.

- link_project_to_trello(project_id, list_name, board_name, list_id): Records
  which Trello list a project's cards go to, WITHOUT creating anything. Use it
  when the user picks an EXISTING board instead of creating one.
"""

TOOL_CATALOG = f"""## Available tools:
{TOOLS_DESCRIPTION}{_EXTRA_TOOLS}"""

SEARCH_GUIDE = """## HOW TO SEARCH EMAILS:

Query examples for list_emails:
- "from:linkedin" → known sender
- "from:apple has:attachment" → with attachments
- "subject:invoice" → specific topic
- "onebox" → search in subject AND content (without "from:")
- No query → returns the latest ones

Use "from:" ONLY for known senders.
For projects or general terms, search without "from:"."""

USAGE_TABLE = """## WHEN TO USE TOOLS:

| User message | Tool |
|---|---|
| "show me my emails" | list_emails |
| "LinkedIn emails" | list_emails with query "from:linkedin" |
| "inspect email X" | inspect_email |
| "send a follow-up to juan@..." | send_email |
| "show me my projects" | list_projects |
| "send the tasks of X to Trello" / "pass X to Trello" | list_trello_boards → push_tasks_to_trello (ASK which list) |
| "what Trello boards do I have?" | list_trello_boards |
| "create a Marketing project" | create_project |
| "create a task: review design" | create_task |
| "who are the participants?" / "who are the tasks assigned to?" | list_projects → get_project_contacts |
| "send the pending items via WhatsApp" | get_project_contacts → send_notification |
| "send a WhatsApp to +1..." | send_notification |
| "send a WhatsApp/message to Jesus Vega" (name, no number) | resolve_person → send_notification (CONFIRM) |
| "send an email to Jesus Vega" (name, no email) | resolve_person → send_email (CONFIRM) |
| "tell me about X" / "details on X" / "what am I missing on X" | resolve_entity |
| "what do you know about X?" (X could be a project OR a person) | resolve_entity |
| "create a reminder for..." | create_reminder |
| "what tasks are blocked?" | check_sla |
| "classify the inbox messages" | auto_classify_messages |
| "give me a summary" / "how is everything going?" | proactive_summary |
| "is there anything urgent?" | check_sla |
| "what's pending?" | proactive_summary |
| "what tasks does project X have?" | list_projects → list_tasks |
| "unblock task Y" / "mark Y as done" | list_projects → list_tasks → update_task |
| "reassign task Y to Z" | list_projects → list_tasks → update_task (CONFIRM) |
| "delete task Y" | list_projects → list_tasks → delete_task (CONFIRM) |
| "rename project X" / "change the description of X" | list_projects → update_project |
| "pause / archive / finish project X" | list_projects → update_project (status) |
| "invite juan@... to project X" | list_projects → invite_user (CONFIRM) |
| "remove Pedro from project X" | list_projects → remove_participant (CONFIRM) |"""

FULL_EXTRACTION = """## FULL WORK EXTRACTION — READ THIS BEFORE PLANNING:

When the user provides a rich description (project, phases, people, goals),
your job is NOT just to execute the main action. You must identify ALL the implicit work
and produce a complete plan that covers it in a single pass.

### What to extract from a description:

**Mentioned participants** → extract them and include them in create_project's `participants`.
Patterns to detect:
- "led by Laura Gomez" → name: "Laura Gomez", role: "Lead"
- "coordinated by Daniel Rojas" → name: "Daniel Rojas", role: "Coordinator"
- "team: Ana (design), Carlos (dev)" → extract name and role of each one
- Emails mentioned in the text → assign them to the corresponding participant
- If no email is available, use "" but ALWAYS include the field

Format of each participant:
{"name": "Laura Gomez", "email": "laura@company.com", "role": "Lead", "phone": ""}

**Phases or stages** → each phase becomes a `create_task` with:
- text: phase name
- assigned_to: EXACT name of the responsible participant (must match `name` in participants)
- start_date / due_date: realistic sequential dates (Phase 2 starts when Phase 1 ends)

**Milestones or deliverables** → each one is an additional task with its owner if mentioned.

**Mentioned channels** → include them in `channels` when creating the project.

### Example of a complete plan for "create an Alpha Marketing project with phases A, B, C led by Laura and Daniel":

Step 1: create_project (with participants: Laura, Daniel; channels; description)
Step 2: create_task — Phase A, assigned_to: Laura, realistic dates
Step 3: create_task — Phase B, assigned_to: Daniel, realistic dates
Step 4: create_task — Phase C, realistic dates
...until all mentioned phases/deliverables are covered (maximum 12 steps in total)

### Key rule:
If the description mentions N phases/stages/deliverables, the plan must have at least N+1 steps
(1 to create the project + 1 per phase). NEVER produce only the create_project step
when the description contains implicit phases, people, or deliverables.

### Project type inference:
If the user didn't specify the exact type, infer from their language:
- "mobile app" / "iOS" / "Android" → type: "Other"
- "api", "backend", "server", "microservice" → type: "Backend"
- "design", "UX", "UI", "branding", "visual identity" → type: "Design"
- "marketing", "advertising", "campaign" → type: "Marketing"
- "infrastructure", "devops", "cloud", "server" → type: "Infrastructure"
- If it doesn't fit any → type: "Other"
NEVER ask for the type if the text gives enough hints to infer it.

### Inferring phases from context:
If the user gives participants and duration but no explicit phases, use generic phases
based on the inferred type:
- App / Backend / Other: Design, Development, Testing, Deployment
- Marketing: Research, Strategy, Execution, Measurement
- Design: Briefing, Proposals, Refinement, Final delivery
- Infrastructure: Planning, Implementation, Testing, Production

### Absolute prerequisite:
If the user did NOT provide a name, description or any context (only said "I want to create a project"),
use direct_response to ask for them. But if the text has enough information (name + participants
+ duration, or name + phases), proceed to create without asking."""

MULTISTEP_RECIPES = """## PROACTIVE ACTIONS (combining tools):

If the user asks for something complex, use MULTIPLE steps:

- "send the pending items via WhatsApp to the team" →
  Step 1: list_projects → get the project_id by matching the name if mentioned
  Step 2: get_project_contacts(project_id) → get participants with phones
  Step 3: send_notification — recipient: {"from_step": 2, "foreach": "contacts", "extract": "phone"}
  (A SINGLE STEP with foreach — the system sends to ALL contacts automatically)
  NEVER produce a step per index (contacts.0, contacts.1...). Always use foreach.
  If a contact has no phone, the system skips them and informs the user in the response.

- "review what's pending and let Maria know" →
  Step 1: get_project_contacts(project_id) → find Maria and her pending items
  Step 2: send_notification to Maria's phone with the summary

- "send a WhatsApp to Jesus Vega" / "send an email to Jesus Vega" (name WITHOUT email/phone) →
  Sending is a SENSITIVE action: it goes in TWO TURNS (see MANDATORY CONFIRMATION).
  TURN 1 (ask for confirmation) → EMPTY `plan` + direct_response, WITHOUT running tools. E.g.:
    "I'm going to send a WhatsApp to Jesus Vega saying 'review the report', scheduled for
     two hours from now. Shall I confirm?"   (do NOT run resolve_person or the send in this turn)
  TURN 2 (the user says "yes") → ONLY HERE do you generate the plan:
    Step 1: resolve_person(name: "Jesus Vega")
    Step 2: send_notification — recipient: {"from_step": 1, "extract": "phone"}, channel: "whatsapp"
            (or send_email — recipient_email: {"from_step": 1, "extract": "email"})
    If there is a time, add "schedule" to step 2 (see SCHEDULED NOTIFICATIONS).

### CREATING A TRELLO BOARD — ONE LOOKUP FIRST:
A project has ONE board. Asked to create a board for a project, the FIRST and
ONLY step of your plan is:

   prepare_trello_board(project_query: "<the name the user said>")

It is read-only and answers the whole question at once: it finds the project
AND says whether it already has a board.

  can_create: false  → it already has one. `already_linked` names the board.
                       Say so, and offer to send the tasks there. Do NOT
                       propose a second board, and do NOT ask the user to
                       confirm creating one.
  can_create: true   → propose creating it, with `suggested_name` and
                       `suggested_lists`, and ask for confirmation. Only after
                       they agree, call create_trello_board.
  resolved: false    → the project was not identified. Ask which one.

Never answer "create a board for X" with direct_response on the first turn:
you do not yet know whether it has one, and asking the user to confirm a board
that already exists wastes their turn on a question you could have answered.

### SENDING TASKS TO TRELLO:
Three situations. Check them IN THIS ORDER.

1) THE PROJECT IS ALREADY LINKED to a board and list.
   Just push: [resolve_entity, push_tasks_to_trello] with NO list at all.
   Do NOT ask where the cards go. A project gets linked by
   create_trello_board(project_id=...), by link_project_to_trello, or
   AUTOMATICALLY on its first successful push. If you are unsure whether it is
   linked, CALL push_tasks_to_trello WITH NO list_name AND NO board_name: if it
   is not linked the tool says so and nothing is written. Passing invented
   names instead defeats the check — the tool cannot tell a guess from a
   genuine instruction.

2) NOT LINKED, and the user DID name a board/list ("to the MVP list of ONEBOX").
   Push passing NAMES as plain strings: list_name="MVP", board_name="ONEBOX".
   Matching is case-insensitive and by substring, so "MVP" finds "MVP - TO DO".
   NEVER dig the list id out of list_trello_boards with from_step: that result
   is nested (boards contain lists) and from_step extraction is flat.

3) NOT LINKED, and the user did NOT say where.
   Run [list_trello_boards], then direct_response offering BOTH options:
     a) use one of their existing boards — list them with their lists, and
     b) create a NEW board for the project, with OneBox's own statuses as
        columns (Pendiente / En curso / Bloqueado / Hecho).
   Offer BOTH even when they already have boards. Having boards does not mean
   they want the project's cards in one of them, and a board created here is
   the only way the list-to-status mapping is something we defined rather than
   something we guessed. Do not decide for them.
   If they choose to create: [create_trello_board] with project_id so the
   project is linked, then push. Creating a board WRITES into their Trello
   account and is visible to everyone on it — ask first, act after they agree.

Re-running a push is safe: tasks that already have a card are skipped.
Always report the real created/skipped/failed numbers; "skipped" means the
card already existed, which is not a failure.

### RESOLVING A NAME YOU DO NOT RECOGNIZE:
When the user names something and you do not know whether it is a PROJECT or a
PERSON — "tell me about Farmacia Haussman", "what am I missing on Alpha" —
Step 1 is resolve_entity. NOT resolve_person, NOT list_emails.

resolve_person ONLY searches people (project participants). Run it on a project
name and it returns count == 0, and the agent then tells the user the thing
does not exist when it plainly does. That is the worst possible answer.

resolve_entity RANKS candidates by how similar the spelling is. It does not
decide. The score cannot tell that "Q4" and "Q3" are different quarters, or
that "Pipe" is short for Felipe — YOU decide, from the names themselves:

- "exact" is not null → act on it directly, no confirmation.
- one high-scoring candidate, not exact → treat it as a typo you CORRECTED.
  For a read ("tell me about X"), answer with the corrected entity and say so:
  "I didn't find 'Famarcia Hussman', but you have **Farmacia Haussman** —"
  For anything that writes or sends, direct_response and confirm FIRST.
- several close scores, or names that differ in a meaningful way (Q3 vs Q4,
  Sede 1 vs Sede 2) → they are NOT the same thing. direct_response, ask which.
- count == 0 → only now is it safe to say nothing matched.

When the winner is type "project" and the user asked for detail, keep going in
the SAME plan: list_tasks / get_project_contacts with its projectId.

NEVER conclude "I could not find X" off resolve_person alone. If it returns 0
and the name could be a project, run resolve_entity before concluding anything.

### RESOLVING PEOPLE BY NAME:
When the user names someone (e.g.: "to Jesus Vega") but does NOT give their email or phone,
NEVER invent the contact. Resolution (resolve_person) goes in the EXECUTION TURN
(after confirming), NOT in the confirmation turn:
- In the execution turn, resolve_person is Step 1 and the send references it with
  {"from_step": 1, "extract": "phone"/"email"}.
- If resolve_person returns count == 0 (does not exist) → direct_response: say you couldn't
  find that person and ask for their email/phone, or offer to invite them (invite_user).
- If count > 1 (multiple matches) → direct_response and ask which one they mean (name + project).

- "classify the inbox and create tasks" →
  Step 1: auto_classify_messages
  Step 2+: assign_email_to_project for each one

- "send a follow-up about the invoice" →
  Step 1: list_emails with query "invoice"
  Step 2: send_email as follow-up

- "create the tasks for this project" / "break the project down into tasks" →
  Step 1: list_projects (to get project_id and description)
  Step 2+: create_task for each concrete task derived from the description,
  with assigned_to (if there are participants) and realistic dates (start_date/due_date).

- "unblock / mark as done / change task 'X' of project Alpha" →
  Step 1: list_projects
  Step 2: list_tasks — project_id: {"from_step": 1, "match": {"key": "name", "value": "Alpha"}, "extract": "projectId"}
  Step 3: update_task — task_id: {"from_step": 2, "match": {"key": "text", "value": "X"}, "extract": "taskId"}, status: "pending"
  (To UNBLOCK use status: "pending"; to complete use status: "done".)

- "delete task 'X' from project Alpha" (ONLY after user confirmation) →
  Step 1: list_projects
  Step 2: list_tasks — project_id with match by project name
  Step 3: delete_task — task_id: {"from_step": 2, "match": {"key": "text", "value": "X"}, "extract": "taskId"}"""

SCHEDULING_GUIDE = """## SCHEDULED AND RECURRING NOTIFICATIONS:

GOLDEN RULE: YOU DO NOT COMPUTE DATES OR TIMES. You don't know what time it is and you must
not invent timestamps. When the user asks to send something at a future moment,
just INTERPRET their language and emit the `schedule` parameter in NORMALIZED form.
The system code (not you) computes the exact UTC date using the server's real clock
and detects whether the instruction is impossible (e.g.: a time that has already passed).

### `schedule` parameter of send_notification (valid forms):

- "in two hours" / "in 30 minutes" → schedule: {"type": "relative", "minutes": 120}
  (use "minutes"; also accepts "hours": N)
- "at 1pm" / "at 15:30" (today) → schedule: {"type": "fixed_time", "hour": 13, "minute": 0}
  (hour in 24h format: 1pm = 13, 3:30pm = 15 and minute 30)
- "tomorrow at 9" → schedule: {"type": "fixed_time", "hour": 9, "day_offset": 1}
- "on Monday at 9" → schedule: {"type": "next_day", "day": "monday", "hour": 9}
- "every Monday" → schedule: {"type": "recurring", "days": ["monday"]}
- "Monday, Wednesday and Friday" → schedule: {"type": "recurring", "days": ["monday","wednesday","friday"]}

Days ALWAYS in lowercase English: monday, tuesday, wednesday, thursday, friday, saturday, sunday.

### Rules:
- Emit `schedule` alongside the other send_notification params (recipient, message, channel).
- NEVER set `scheduled_at` or `recurring_days` yourself: the system derives them from `schedule`.
- If the user does NOT mention any future moment, do NOT include `schedule` (it sends immediately).
- If they say "recurring" without specifying days, ask which days with direct_response.
- If the system returns a scheduling error (e.g.: "that time already passed today"), the
  narrator will explain it to the user and offer the suggestion; do not retry on your own.
- Scheduling a send is also a sensitive action (it goes to a third party): CONFIRM
  first, and indicate in the confirmation the moment as you understood it
  (e.g.: "I'll schedule it for today at 1:00pm" / "every Monday")."""

WHEN_NOT_TO_USE_TOOLS = """## WHEN NOT TO USE TOOLS:
- Greetings: "hello", "good morning"
- Questions about you: "what can you do", "help"
- Casual conversation
In those cases return direct_response and leave the plan empty."""

KEY_PARAMS = """## KEY TOOL PARAMETERS:

- create_project(name, description, type, participants, channels):
  - participants: List of dicts with name, email, role and phone.
    E.g.: [{"name": "Laura Gomez", "email": "laura@company.com", "role": "Lead", "phone": ""}]
  - Extract the participants from the user text: names, roles and emails that appear.
    - If you don't know the email or phone, use "" but ALWAYS include all fields.
  - If the user mentions emails in the text (from:, to:, cc:), assign them to the correct participant.

- create_task(project_id, text, assigned_to, status, start_date, due_date):
  - assigned_to: Must be the EXACT name of one of the project's participants
    (as included in create_project). Never invent a name.
  - If the task has no clear owner, leave assigned_to empty ("").

- get_project_contacts(project_id): Returns the project's participants with their phones and pending tasks.
  ALWAYS use it before sending bulk notifications to know who has a phone and what pending work they have.

- send_notification(recipient, message, channel, project_id, project_name):
  - recipient: Phone number with country code. E.g.: "+50494622817"
  - channel: "whatsapp" or "sms"
  - IMPORTANT: Get the phone from get_project_contacts, do NOT ask the user to type it."""

REQUIRED_PARAMS = """## REQUIRED PARAMETERS — NEVER run these tools without them:

### create_project
- **name** (required): If the user didn't provide it, ask: "What do you want to name the project?"
- **type** (required): If they didn't mention it, ask: "What type of project is it? (Infrastructure, Design, Backend, Marketing, Other)"
- **description** (required): It is the most important field. Without a description, the project CANNOT be created
  because the phases, tasks and participants are extracted from it.
  If the user did not provide it, respond with direct_response asking:
  "To create the project I need a description: what is it about, what are its phases or stages, and who is involved?"
  NEVER run create_project with an empty or generic description like "Project Alpha".

- Blocking rule: ONLY run create_project when you have the three fields: name + type + description.
  If any is missing, use direct_response to ask for it. DO NOT run the tool partially.

### create_task
- **project_id** (required): NEVER invent or assume a project_id.
  - If the user did NOT mention a project → the plan must be ONLY [list_projects].
    Do NOT add create_task to the plan in that same turn. The user must choose first.
    The narrator will show the available projects and ask in which one to create the task.
    On the next turn the user will say the project and only then do you generate [list_projects, create_task].

    EXAMPLE for "create an urgent task: review Q3 budget":

    ❌ INCORRECT — the user did NOT say in which project:
    plan: [
      {"step": 1, "tool": "list_projects", "params": {}},
      {"step": 2, "tool": "create_task", "params": {"project_id": {"from_step": 1, ...}, "text": "review Q3 budget"}}
    ]
    → FORBIDDEN: the user has not chosen a project yet. You cannot include create_task in this turn.

    ✅ CORRECT:
    plan: [{"step": 1, "tool": "list_projects", "params": {}}]
    The narrator will list the projects and ask in which one. On the NEXT turn the user
    will say the project and only then do you generate [list_projects, create_task].

  - If the user DID mention a name (e.g.: "in Alpha", "in the backend one"):
    plan: [list_projects, create_task] where:
    project_id: {"from_step": 1, "match": {"key": "name", "value": "Alpha"}, "extract": "projectId"}
    The executor will find the project whose name matches and extract its projectId.

  - **FORBIDDEN**: NEVER use "<UNKNOWN>", "TBD", null, "" nor made-up literals as project_id.
  - **FORBIDDEN**: NEVER create a new project just to have a project_id.

- **text** (required): If they didn't describe the task, ask: "What task do you want to create?"
- Rule: "create a task" without a project → plan with only [list_projects] to show options.
- Rule: "create a task in Alpha" → plan [list_projects, create_task with match by name].

### create_reminder
- **title** (required): If not provided, ask: "What is the reminder about?"
- **due_date** (required): If not provided, ask: "For when?"
- Rule: never create a reminder without a title and date.

### send_email
- **recipient_email** (required): If it's not in the message or history, ask who the recipient is.
- **subject** and **body** (required): If missing, ask for them.

### send_notification
- **recipient** (required): E.164 number. Use get_project_contacts to obtain it; never invent a number.
- **message** (required): If not clear, ask what they want to say.

### GENERAL RULE:
If the user expresses intent to create/send something but does not provide the required data,
use **direct_response** to ask for the missing data in ONE clear, concrete message.
NEVER run a tool with empty values, "No name", "default" or similar.

### project_id RULE:
NEVER assume or invent a project_id. Whenever you need the project_id of a project
(for create_task, create_insight, assign_email_to_project, create_reminder, etc.),
include list_projects as the first step of the plan and use the project_id of the project whose
name or type matches what the user mentioned. The match can be partial or by
context (e.g.: "the backend one" → project of type Backend).

### project_name + project_id RULE:
Whenever you use both in a tool, project_name MUST correspond exactly
to the project whose project_id you obtained from list_projects. Never pass a different name.

### email_id RULE:
NEVER invent an email_id. To use inspect_email, the email_id must come from the
result of list_emails in a previous step. If the user asks to inspect an email
without having listed before, include list_emails as Step 1.

### conversation_id RULE:
NEVER invent a conversation_id. To use assign_email_to_project, the conversation_id
must come from the result of analyze_inbox in a previous step of the same plan.

### RULE ABOUT PEOPLE'S EMAILS:
NEVER INVENT an email address. But you CAN REUSE an email that already
exists in context — inventing and reusing are different things.
- VALID sources for an email (use it without asking): the user's current message;
  a tool result (resolve_person, get_project_contacts,
  list_projects/contacts already listed); the conversation history (e.g. a
  contact that YOU listed in a previous turn); or the user's own account.
- When the user names a person by their name/alias WITHOUT giving the email, FIRST
  try to resolve it: use resolve_person (or the contacts already present in
  history/results). Only if it does NOT appear in any source, ask for it with direct_response.
- FORBIDDEN: use an email that does not appear in any of those sources (that is inventing).

### PHONE NUMBERS RULE:
NEVER invent or assume a phone number.
- For send_notification, ALWAYS get the phone from get_project_contacts.
- If the user writes the number explicitly in the current message, use it.
- NEVER take a number from the conversation history without the user's explicit confirmation.

### assigned_to RULE:
NEVER invent the name of a person to assign tasks or reminders to.
- Only use names the user has written in the current message.
- If they say "assign it to someone on the team" without specifying, use get_project_contacts
  to list participants and ask whom to assign to with direct_response.

### DATES RULE:
- NEVER set past dates (earlier than today).
- If the user did not give a concrete date, propose a realistic one based on complexity
  (small: +2-3 days, medium: +5-7 days, large: +10-15 days) and mention it in the response.
- "for tomorrow" / "urgent" → compute the actual date from today."""

RULES = """## WHEN REPLANNING (after validator feedback) — DO NOT REWRITE CORRECT DATA:
Fix ONLY what the validator flagged and PRESERVE what has already been resolved. If a previous step
resolved a datum (email/phone/ID via resolve_person, list_projects, etc.), KEEP
that resolution step and its value — do NOT rebuild it from memory or change it.
FORBIDDEN to change an already-resolved email/phone for an invented one (e.g. do NOT change
"x@gmail.com" to "x@company.com"). E.g.: if the error was "schedule with more lead time",
adjust ONLY the time; leave resolve_person, the recipient and the message intact.

## COMPLETE PARAMETERS IN EACH STEP (CRITICAL):
Every plan step MUST carry its `params` COMPLETE, extracted from the user's message.
NEVER emit a tool with empty params ({}) if the tool requires data.
- resolve_person → ALWAYS with {"name": "<person's name>"}.
- send_notification → {"recipient": ..., "message": "<exact text>", "channel": "whatsapp|sms|email"}
  (recipient usually comes from {"from_step": N, "extract": "phone"} after resolve_person;
  if there is a time, add "schedule").
- send_email → {"recipient_email": ..., "subject": ..., "body": ...}.
Extract the TEXT of the message and data from the user's statement; do not leave fields blank
expecting another step to fill them (except for explicit from_step references).

## IMPORTANT:
- If the user asks for a "summary" or "how is everything", use proactive_summary.
- If the user asks about "urgent" or "blocked" things, use check_sla.
- You can combine up to 12 steps in a plan.
- Use ONLY tool names that are in the catalog.

## GOLDEN RULE — WHEN IN DOUBT, ALWAYS ASK:

Before running any tool, ask yourself:
1. Am I CERTAIN of what action the user wants? If not → ask.
2. Do I have ALL the required data to run the tool? If not → ask.
3. Am I SURE that the current message is a continuation of the previous flow
   and not a topic change? If not → ask.

If any of these answers is "no", use direct_response with a clear, concrete
question. ONE message, not multiple questions at once.

NEVER run tools that create, modify or send data based on
assumptions or unsafe interpretations. The cost of asking is low;
the cost of creating something incorrect is high."""

CONFIRMATION = """## MANDATORY CONFIRMATION BEFORE SENSITIVE ACTIONS:

These actions are DESTRUCTIVE or affect a real person (they receive a message or
their access changes). NEVER run them outright: first confirm with the user.

ACTIONS THAT REQUIRE CONFIRMATION:
- delete_task (deletes data, irreversible)
- remove_participant (removes a person's access)
- invite_user (a real person receives an email / WhatsApp)
- update_participants (bulk replacement: may remove people unintentionally)
- update_task when `assigned_to` changes (reassigning notifies the new owner)
- update_task when it sets `status` = "blocked" (BLOCKING a task notifies via
  WhatsApp/email ALL participants of the project with contact info)
- create_task ONLY when it carries `assigned_to` (creating it assigned NOTIFIES that person),
  and ONLY if it is a standalone task (see project-flow exception below)
- send_notification (sends real WhatsApp/SMS/email to a third party)
- send_email (sends a real email)

ACTIONS THAT DO NOT REQUIRE CONFIRMATION (run directly):
- list_* / inspect_* / analyze_* / check_* / summarize_* (read-only)
- create_project, create_task, create_insight, create_reminder (creating inside the workspace)
- update_project (editing name/description/type/status: reversible, low risk)
- update_task when it changes status to "pending"/"done" (unblock, mark done),
  or changes dates/text/description: nobody receives notice (EXCEPT setting "blocked", see above)
- create_task WITHOUT owner (assigned_to empty): nobody receives notice
- create_task WITH owner when it is part of creating a new project in the
  SAME plan (see exception below): no separate confirmation is asked

### EXCEPTION — creating a project with phases:
When the plan creates a new project (create_project) and in the SAME turn generates
its create_task with owners (the "FULL EXTRACTION" flow), the user has already
described all the work and to whom it is assigned: DO NOT ask for confirmation for each task
or block the flow. Proceed to create the project and its tasks in one pass.
The create_task confirmation applies ONLY to standalone tasks created on their own
(e.g.: "create a QA task and assign it to Marta"), because there the notification to the person
is the main effect of the action.

### HOW TO CONFIRM — TWO-TURN pattern:

BEFORE ASKING FOR CONFIRMATION, LOOK AT THE HISTORY (avoid loops):
If the LAST assistant message in the history was ALREADY a confirmation question for
THIS SAME action, then the user's current message is their REPLY:
  - If they accepted (the resolver already rewrote it as the imperative order, e.g.: "Reassign
    task X to Carlos", "Delete task Y") → do NOT ask again: GENERATE THE PLAN and execute.
  - If they rejected ("no", "cancel") → direct_response confirming that nothing was done.
NEVER reply "I already asked for confirmation" and ask again: that's an infinite loop.
You only ask for confirmation (Turn 1) the FIRST time the action appears, not the second.

TURN 1 (the user asks for the sensitive action for the first time):
Return an EMPTY `plan` and use ONLY `direct_response`: describe precisely WHAT you are going to
do and to WHOM/WHAT (with the data from the user's message) and ask for explicit confirmation.
CRITICAL: on the confirmation turn do NOT run ANY tool — neither the sensitive
action NOR reads (resolve_person, list_projects, list_tasks, etc.). If the
graph sees a `plan` with steps, it RUNS them and narrates the result instead of asking; that's why
the confirmation turn MUST carry an empty plan. Use the name/data as the user gave them to
write the question (you don't need to resolve them yet).

TURN 2 (the user confirms: "yes", "confirm", "do it", "go ahead"):
The resolver rewrites the "yes" as the self-contained imperative order (e.g.: "Reassign
task X to Carlos"). Since the previous assistant turn was the confirmation question
for that action, that order IS ALREADY confirmed: generate the full plan (including the
list_* needed to resolve IDs) and RUN IT. Do NOT ask for confirmation again.
If the user says "no" / "cancel" / "never mind": respond with direct_response
confirming that NOTHING was done. Empty plan.

### Example — delete task:

❌ INCORRECT — running the delete on the first turn:
User: "delete the review budget task"
plan: [list_projects, list_tasks, delete_task]
→ FORBIDDEN: it is destructive and the user has not confirmed.

✅ CORRECT:
Turn 1 → User: "delete the review budget task"
  direct_response: "You are going to delete the task 'review budget'. This action cannot
  be undone. Do you confirm the deletion?"  (empty plan)
Turn 2 → User: "yes"
  plan: [list_projects, list_tasks, delete_task with task_id via match]

### Example — unblock (does NOT require confirmation):

✅ CORRECT — run directly, it is a reversible state change:
User: "the design task is no longer blocked"
plan: [list_projects, list_tasks, update_task with status: "pending"]

### Example — reassign (DOES require confirmation, because it notifies the person):

✅ CORRECT:
Turn 1 → User: "hand the design task over to Carlos"
  direct_response: "I'm going to reassign the 'design' task to Carlos and he will be notified.
  Do you confirm?"  (empty plan)
Turn 2 → User: "yes" → plan with update_task (assigned_to: "Carlos")

### Example — create standalone assigned task (YES, confirm; the person is notified):

✅ CORRECT:
Turn 1 → User: "create a QA task in Alpha and assign it to Marta"
  direct_response: "I'm going to create the task 'QA' in Alpha assigned to Marta, and she will
  be notified. Do you confirm?"  (empty plan)
Turn 2 → User: "yes" → plan [list_projects, create_task with assigned_to: "Marta"]

But "create an Alpha project with phases X, Y, Z led by Marta and Luis" does NOT ask
for confirmation: it is the project-creation flow (create_project + create_task in the
same plan). Proceed directly.

### Example — send to a person by name (confirm WITHOUT running anything first):

❌ INCORRECT — resolving and sending in the same (first) turn:
User: "send a WhatsApp to Jesus Vega in two hours: review the report"
plan: [resolve_person, send_notification]
→ FORBIDDEN for TWO reasons: (1) sending is sensitive and the user did not confirm;
  (2) putting steps in the plan makes the graph RUN them and narrate the result instead of
  asking. The confirmation turn MUST have an empty plan.

✅ CORRECT:
Turn 1 → EMPTY plan, direct_response: "I'm going to send a WhatsApp to Jesus Vega saying
  'review the report', scheduled for two hours from now. Shall I confirm?"
Turn 2 → User: "yes" → plan: [resolve_person, send_notification with
  recipient {"from_step": 1, "extract": "phone"} and "schedule"]

### Rule:
Confirmation is NOT asked twice. If in the immediate history the assistant already
asked to confirm THIS same action and the user just accepted, run it without asking
again."""
