"""TOOLS_DESCRIPTION: the tool reference the planner prompt is built from.

Moved verbatim out of the old single-file agent/tools.py.
"""



TOOLS_DESCRIPTION = """
- list_emails(query, max_results): Search emails in Gmail.
  Parameters:
    - query: (optional) Gmail filter. E.g.: "from:linkedin", "has:attachment", "project alpha"
    - max_results: (optional) Max emails (default: 50)

- inspect_email(email_id): Inspect a specific email by its ID.

- analyze_inbox(): Reads ALL unassigned emails from the database. No parameters needed.

- list_projects(): Lists all existing projects for the user.

- create_project(name, description, type, participants, channels): Creates a new project.
  Parameters:
    - name: Project name (required)
    - description: Short description
    - type: Type (Infrastructure, Design, Backend, Marketing, Other)
    - participants: List of participants [{"name": "X", "role": "Y"}]
    - channels: List of channels ["Gmail", "Slack", "WhatsApp"]

- assign_email_to_project(conversation_id, project_id, project_name): Assigns an unassigned email to an existing project.
  Parameters:
    - conversation_id: Conversation ID (from analyze_inbox)
    - project_id: Target project ID
    - project_name: Project name (for reference)

- create_insight(project_id, project_name, type, title, description, related_person, actions): Records an intelligent AI action.
  Parameters:
    - project_id: Project ID
    - project_name: Project name
    - type: decision | blocker | task_created | followup | risk
    - title: Insight title
    - description: Detail
    - related_person: Person involved
    - actions: List of actions taken

- create_task(project_id, text, assigned_to, status, start_date, due_date): Creates a task in a project.
  Parameters:
    - project_id: Project ID
    - text: Task description
    - assigned_to: Owner (optional)
    - status: pending | done | blocked
    - start_date: (optional) Start date in YYYY-MM-DD format. ALWAYS try to propose a realistic date.
    - due_date: (optional) Deadline in YYYY-MM-DD format. ALWAYS try to propose a realistic date
      based on complexity: small tasks 2-3 days, medium 5-7 days, large 10-15 days.

- send_notification(recipient, message, channel, project_id, project_name, scheduled_at, recurring_days): Sends or schedules a notification via WhatsApp, SMS or email.
  Parameters:
    - recipient: E.164 number (WhatsApp/SMS) or email (email channel). E.g.: "+34612345678"
    - message: Text of the message to send
    - channel: "whatsapp", "sms" or "email"
    - project_id: (optional) Related project ID
    - project_name: (optional) Project name
    - scheduled_at: (optional) Send date and time in ISO 8601 UTC. E.g.: "2026-06-20T09:00:00Z"
      If provided, the notification is scheduled instead of sent immediately.
      For WhatsApp/SMS with Messaging Service configured: Twilio schedules it natively.
      For email or without Messaging Service: it is stored in DynamoDB and the dispatcher sends it.
    - recurring_days: (optional) List of days for weekly recurring send.
      Valid values: "monday","tuesday","wednesday","thursday","friday","saturday","sunday"
      E.g.: ["monday","wednesday","friday"]
      If provided, the notification repeats every week on those days.
      Requires the EventBridge dispatcher to be active.

- list_notifications(project_id): Lists notifications sent for a project.
  Parameters:
    - project_id: (optional) Filter by project. If omitted, lists all.

- send_email(recipient_email, subject, body, project_id, project_name): Sends a follow-up email.
  Parameters:
    - recipient_email: Recipient email (required)
    - subject: Email subject (required)
    - body: Email content (required)
    - project_id: (optional) Related project ID
    - project_name: (optional) Project name

- create_reminder(title, descripcion, due_date, project_id, project_name, assigned_to): Creates a reminder/follow-up.
  Parameters:
    - title: Reminder title (required)
    - descripcion: Reminder detail
    - due_date: Deadline in "YYYY-MM-DD" format (required)
    - project_id: (optional) Project ID
    - project_name: (optional) Project name
    - assigned_to: (optional) Owner

- check_sla(): Scans ALL projects and tasks looking for blocked, overdue or unanswered items. No parameters needed.

- auto_classify_messages(): Reads the unassigned inbox messages and suggests which project to assign them to based on content. No parameters needed.

- proactive_summary(): Generates an executive summary of the state of all projects, pending tasks, SLA and suggested actions. No parameters needed.

- list_tasks(project_id): Lists the tasks in a project (text, status, owner, dates, taskId).
  Use it to locate a task's taskId before updating or deleting it.

- update_task(task_id, text, status, assigned_to, due_date, start_date, description, blocked_reason): Updates an existing task.
  Parameters (all optional except task_id; only the fields you send are changed):
    - task_id: ID of the task to modify (required). Get it from list_tasks.
    - status: pending | done | blocked  (to UNBLOCK a task, use status="pending")
    - assigned_to: name of the new owner (REASSIGN — requires user confirmation)
    - text / description / start_date / due_date / blocked_reason: fields to edit.
  DO NOT create a new task to "change" an existing one: use this tool.

- delete_task(task_id, cascade): Deletes a task. Requires user confirmation.
    - task_id: Task ID (required). Get it from list_tasks.
    - cascade: (optional) if True, also deletes subtasks; if False, subtasks become root.

- update_project(project_id, name, description, type, status, delivery_date, timing): Edits fields of an existing project. Only the owner can edit.
    - project_id: Project ID (required). Get it from list_projects.
    - status: active | paused | finished. The rest of the fields are optional; only the ones you send are changed.

- invite_user(project_id, email, phone, name, role, send_notification): Adds a person to the project team. Requires user confirmation.
    - project_id: Project ID (required).
    - email and/or phone: at least one required. With email we create access and notify them; with phone we notify via WhatsApp.
    - name, role: optional. send_notification (optional, default True): if False, only records the contact without notifying.

- update_participants(project_id, participants): Replaces the FULL participants list of the project. Requires confirmation (it is a bulk replacement). Owner only.
    - participants: list of dicts [{"name","email","role","phone"}].

- remove_participant(project_id, email, phone, name): Removes a person from the project team. Requires user confirmation. Owner only.
    - Identifies the person by email > phone > name (first match). Their tasks become unassigned and they lose access if they were an invitee.

- resolve_person(name): Looks up a person by NAME among the participants of your projects and returns their contact info (email, phone, role, project).
    - Use it ALWAYS when the user mentions someone by name without giving email or phone, before sending or assigning something to them.
    - If count == 1, exposes email/phone/name/projectId at the top level so they can be referenced with {"from_step": N, "extract": "phone"} (or "email").
    - If count == 0 (does not exist) or count > 1 (multiple people with the same name), DO NOT send: ask the user (email/phone, which one of them, or whether they want to invite the person).
    - Tolerates typos: if the exact lookup finds nobody, it retries with fuzzy matching, and matches found that way are flagged "fuzzy": true. Confirm those with the user before sending anything.

## TRELLO INTEGRATION — WHAT IT CAN AND CANNOT DO (READ BEFORE OFFERING ANYTHING):
OneBox talks to Trello through EXACTLY TWO operations, listed below. The integration is ONE-WAY:
OneBox writes cards INTO Trello and never reads them back.

It CANNOT, and you must NEVER offer or imply any of these:
  - read, list or show the cards of a board          - move a card between lists
  - edit, rename, comment on, archive or delete a card - assign a Trello member
  - read a card's status back into OneBox             - detect changes made in Trello
  - add labels, or edit or delete an existing board
(It CAN create a NEW board with its lists — see create_trello_board below.)

Trello is a well-known product with many features. Do NOT describe what Trello can do in general;
describe only what these two tools do. Suggesting anything else sends the user to try something
that does not exist.

- create_trello_board(name, project_id, lists): Creates a NEW Trello board with its lists. REQUIRES USER CONFIRMATION.
    - name: board name (required). lists: column names in order; defaults to OneBox's own statuses
      (Pendiente / En curso / Bloqueado / Hecho), which is the POINT: when OneBox creates the board, the
      list-to-status mapping is something we defined instead of something we guessed from someone's naming.
    - project_id: (optional) links the project to the new board and its first list, so later pushes need no list.
    - This WRITES into the user's Trello account and the board is visible to everyone on it. NEVER call it inside a
      multi-step plan on your own initiative: ask first, act after the user says yes.

- link_project_to_trello(project_id, list_name, board_name, list_id): Remembers which Trello list a project pushes to.
    - Use it when the user picks an EXISTING board rather than creating one.
    - After this, push_tasks_to_trello needs no list: it uses the saved one. Ask WHERE once, not on every push.

- list_trello_boards(): Lists the user's Trello boards, each with its lists (columns). No parameters.
    - Run it BEFORE push_tasks_to_trello: in Trello the list IS the status, so a card cannot be created without a target list.
    - If the user has not connected Trello it returns an error saying so. Tell them to connect it; do not retry.

- push_tasks_to_trello(project_id, list_name, board_name, list_id, only_pending): Creates a Trello card for each task of a project.
    - project_id: OneBox project (required).
    - If the project is already linked, you may omit the list entirely: the saved one is used. Do NOT re-ask where the
      cards go when the project already has one. A project becomes linked by create_trello_board(project_id=...),
      by link_project_to_trello, OR AUTOMATICALLY on its first successful push — so after one push with an explicit
      list, "send the tasks of X to Trello" is enough on its own and needs no board or list.
    - If it is NOT linked and the user has not said where, ask: offer both creating a new board and using an existing one.
    - PASS THE LIST BY NAME: list_name="MVP", board_name="ONEBOX". The tool resolves the name against Trello itself,
      case-insensitively and by substring, so list_name="MVP" finds a list actually called "MVP - TO DO".
    - DO NOT try to dig the list id out of list_trello_boards with from_step. That result is NESTED (boards contain
      lists) and from_step extraction is FLAT: there is no two-level match. `then_match`, `then_extract`, `board_name`
      as a match key -- none of those exist. Pass the names as plain strings instead.
    - list_id: only when you already have the literal id. Give EITHER list_id OR list_name, never both.
    - only_pending: default True, skips tasks that are already done.
    - Tasks that already have a card are SKIPPED, so running it twice never duplicates cards.
    - NEVER invent a list_id. If the user did not say where, run list_trello_boards and ASK which list.
    - Returns "outcome" (complete | partial | failed | noop), a "summary" line, and created/skipped/failed naming each task.
    - REPORT ALL THREE NUMBERS, ALWAYS, in the SAME answer. "partial" means some cards WERE created: reporting only the failure
      makes the user believe nothing happened while those cards already exist in Trello. Say what was created, what was skipped
      and what failed, then what to do about the failures.
    - "skipped" is NOT a failure: those tasks already had a card and were not duplicated. Present it as such, not as a problem.

- resolve_entity(query): Ranks the projects AND people whose names resemble `query`, tolerating typos. Searches BOTH in one pass.
    - Use it whenever the user names something and you do not already know what it is: "tell me about X", "details on X", "what am I missing on X", "what do you know about X".
    - Prefer it over resolve_person whenever the name could be a project. resolve_person ONLY searches people, so on a project name it always returns 0.
    - IT RETURNS RANKED CANDIDATES, NOT AN ANSWER. The score only measures how similar the spelling is; it cannot tell that "Q4" and "Q3" are different quarters, that "Pipe" is Felipe, or that "la farmacia" means "Farmacia Haussman". YOU make that call from the names themselves.
    - How to read it:
      · "exact" is not null → the query appears verbatim in that name. A fact. Act on it, no confirmation needed.
      · one candidate scoring high but not exact → almost certainly a typo. Use it, but SAY you corrected it: "did you mean <name>?"
      · several close scores → real ambiguity. Ask which one.
      · scores are close but the names mean different things (Q3 vs Q4, Sede 1 vs Sede 2, v1 vs v2) → they are NOT the same thing. Do not merge them; ask.
      · count == 0 → nothing even resembles it. Only here is it safe to say it does not exist.
    - "dropped_below_floor" counts names too dissimilar to be worth showing. A high number just means a big workspace, not a failed search.
"""
