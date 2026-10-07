# OneBox Backend — Context for Claude

## What is this project?

**OneBoxBackend** is a Python/FastAPI backend with a conversational agent built on LangGraph. The agent receives user messages, plans actions using tools (create projects, tasks, send notifications, etc.) and responds in natural language.

**Separate project — DO NOT modify:** `C:\Users\user\Desktop\AppDev\send\chatbot` is a distinct, independent project. Never touch files in that folder.

---

## Agent architecture

```
User → context_resolver → planner → executor → validator → narrator → Response
```

The flow is a LangGraph graph with these nodes:

| Node | File | Role |
|---|---|---|
| context_resolver | `agent/graph/nodes/context_resolver/` | Detects language, intent and context from history |
| planner | `agent/graph/nodes/planner/` | Decides which tools to run and in what order |
| executor | `agent/graph/nodes/executor/` | Runs the plan step by step (no LLM) |
| validator | `agent/graph/nodes/validator/` | Assesses whether the results are correct |
| narrator | `agent/graph/nodes/narrator/` | Produces the final natural-language response |

---

## Conversational behavior files

These are the only files that define **how the agent converses**. They are the target of the debug and tuning loop:

| File | What it controls |
|---|---|
| `agent/graph/nodes/planner/catalog.py` | Planner rules: when to use which tool, ❌/✅ examples, multi-step patterns |
| `agent/graph/nodes/planner/prompts.py` | Planner base prompt |
| `agent/graph/nodes/narrator/prompts.py` | How the narrator presents results to the user |
| `agent/graph/nodes/narrator/narrators/` | Specialized narrators per result type |
| `agent/graph/nodes/validator/prompts.py` | Criteria the validator uses to approve or reject results |

---

## Code-logic files (NOT the target of the conversational loop)

These files are execution logic. They are only modified when there is a code bug, not to tune conversational behavior:

| File | Role |
|---|---|
| `agent/graph/nodes/executor/resolve.py` | Resolution of `from_step` references between plan steps |
| `agent/graph/nodes/executor/validators.py` | Parameter validation before running tools |
| `agent/graph/nodes/executor/node.py` | Executor orchestration, `foreach` support |
| `agent/graph/builder.py` | LangGraph graph construction |
| `agent/tools.py` | Tool implementations (DynamoDB, Twilio, etc.) |

---

## Debug and conversational tuning loop (vibe coding)

This is the correct process to iterate on the agent's behavior:

```
1. Start the server:   uvicorn main:app --reload
2. Use the onebox-chat MCP to converse turn by turn with the agent
3. When the session ends: onebox_report() → see full analysis
4. Discuss with the user what problem was detected and why it happens
5. The user approves the proposed change
6. Claude edits the corresponding conversational behavior file
7. Restart the server and repeat from step 2
```

### Core rule

**Claude does not modify conversational behavior files without the user's explicit approval of the change.**

The report shows the problem. The discussion defines the solution. The user approves. Claude edits.

---

## Debug mode

The agent has a debug mode you can enable with `debug=True` in the request:
- Dry-run: does not write to DynamoDB or call Twilio
- Returns `debug_info` with the plan, planner iterations, decisions and simulated results
- The simulated results live in `executor/node.py` → `_DRY_RUN_RESULTS`

The MCP in `mcp/server.py` always uses `debug=True`.

---

## Debug signals to watch for

When the report shows one of these signals, something needs adjusting in the behavior files:

| Signal | Likely cause | File to review |
|---|---|---|
| `iteration > 1` | The planner rule isn't clear enough | `catalog.py` — add ❌/✅ example |
| `_validation_error` in results | The planner produced an invalid param despite the rule | `catalog.py` — reinforce the constraint |
| Planner created an unnecessary project | Missing explicit rule for when NOT to create | `catalog.py` — FORBIDDEN |
| Narrator does not mention available projects | The narrator lacks guidance for that result | `narrator/narrators/` |
| Validator rejects a correct result | Validation criterion is too strict | `validator/prompts.py` |

---

## Example convention in catalog.py

The most effective pattern to teach the planner is explicit contrast:

```
❌ INCORRECT — description of the forbidden case:
plan: [ ... the bad plan ... ]
→ Why it's wrong.

✅ CORRECT:
plan: [ ... the correct plan ... ]
```

Whenever a new rule is added, include this contrast.

---

## Starting a debug session

When the user says "let's test the agent", "let's start debugging", "I want to test the flow" or similar, follow this protocol without waiting for further instructions:

**Step 1 — Verify prerequisites**
Ask the user:
- Is the server running? (`uvicorn main:app --reload`)
- Which flow do you want to test? (or propose the known scenarios)

**Step 2 — Clean up the session**
Use `onebox_reset()` before starting to ensure a clean history.

**Step 3 — Drive the conversation turn by turn**
Use `onebox_chat(message, session_id)` for each message. Show the user the agent's response and the plan that was executed. Do not send the next turn automatically — wait for the user's confirmation to continue.

**Step 4 — Generate the report**
When done (when the user says so or the scenario runs out of turns), run `onebox_report()` and read the full analysis.

**Step 5 — Diagnose**
Identify the problems in the report using the signals table. Explain to the user what happened and why, pointing to the specific file that causes it.

**Step 6 — Propose the change**
Propose the concrete change to the conversational behavior file: which line or section to change, what to add, showing before/after. **Wait for the user's explicit approval.**

**Step 7 — Edit and repeat**
With approval, edit the file. Tell the user to restart the server (`Ctrl+C` → `uvicorn main:app --reload`) and go back to Step 2.

### Golden rule of this loop
Only **conversational behavior files** are modified (catalog.py, prompts, narrators). Never code logic without an explicit bug. Never without the user's approval.

---

## Checks before pushing

```bash
./scripts/check.sh
```

ruff (undefined names) → mypy (calls that cannot bind) → contracts and smoke
tests. No LLM, no AWS, no network: about a minute, safe to run on every change.
Run it before proposing that anything is finished. `README.md` explains what
each pass catches and why each check exists.

Two rules that matter when touching the planner:

- The planner's output schema must be fillable by a CONSTRAINED decoder, not
  only by one that reads the schema as a hint. A field typed as a bare `dict`
  comes back empty on Gemini. `tests/test_tool_schemas.py` guards this; see
  "The 2026-09-16 incident" in `README.md`.
- Never hand-write a tool's parameter list in a prompt. Derive it from the
  signature (`agent/tools/schema.py`). Prose drifts; generated text cannot.

## Tech stack

- **Python 3.11+** with FastAPI and Uvicorn
- **LangGraph** for the agent graph
- **LangChain** for messages and LLM
- **DynamoDB** as the database
- **Twilio** for WhatsApp/SMS
- **LLM:** configurable via `agent/llm.py` (Bedrock / Anthropic / Gemini)
- **Debug MCP:** `mcp/server.py` (requires `pip install mcp httpx`)

---

## Planning graph (agent/planning) — separate from the chat graph

A second LangGraph graph keeps each project's task plan consistent with reality:
it reads documents (WhatsApp exports, transcripts, proposals), the leader's
planning chat, and schedule incongruences, and returns a PROPOSAL of operations
(close / update / create / delete / link / block). It never writes: code applies
a proposal after the leader approves it (`schedule.apply_proposal`).

| File | Role |
|---|---|
| `agent/planning/prompts.py` | Behaviour: what counts as done, duplicate, dependency. Tune here. |
| `agent/planning/nodes.py`, `builder.py` | Nodes and wiring (code logic) |
| `agent/planning/schedule.py`, `validate.py`, `sources.py` | Deterministic: dates, rules, deltas, redaction |
| `tests/planning/cases/` | Cases: before.json + input.json + expected.json |

Pipeline: extract_facts (only state-changing facts, chunks in parallel) ->
consolidate (facts -> topics with their state at the END; <20 facts: no LLM) ->
reconcile. Code-enforced in reconcile: topics open with no news for 30 days of
the material and no deadline are "stale" (never created, one question to the
leader); near-identical new tasks are merged (word Jaccard >= 0.6).
reconcile = first one `cover` call that walks the topics one by one (covered /
create / note / ignore), then batches of 15 existing tasks (near-duplicates grouped
in the same batch; each task needs a review whose verdict matches its operation)
that see the new tasks too, so a generic task replaced by specific ones is deleted
pointing at them. Notes are
things that happened (email, meeting, call, decision, idea): never tasks; on
approval they are written to `onebox-insights` as type `note` (taskId, noteKind,
eventDate, relatedPerson, evidence).

MEMBER UPDATES — the core loop of the product (keep it separate from the rest):
  member chat -> classified items -> daily consolidation -> digest + ONE proposal
  -> owner approves -> app + Trello.
* `api/services/member_updates.py` + `api/controllers/updates.py`, frontend
  `UpdateChatModal.tsx` mounted in Layout (replaces the old CheckinModal form).
  On login, a member whose update is due (same clock as the check-in:
  CHECKIN_DAYS=3, or a task overdue / due within 2 days) gets a BLOCKING chat.
  Prompt `MEMBER_UPDATE`, schema `UpdateTurn` (reply, items, finished): at most
  2 short questions per turn, items typed done / progress / not_started /
  blocker / date_change / plan / idea / question / hearsay with certainty
  (sure / estimated / conditional / low), owner, depends_on, urgent,
  decision_for_owner. NOTHING in the plan changes here. Members cannot close
  the chat; the project owner (when they also have tasks) can postpone it
  ("Ahora no", `isOwner` in `/api/updates/pending`). People without an account
  are updated by the owner in Replanificación ("Pedro dice que..."); an
  approved change to someone's tasks counts as their report (clock restarts,
  `planning_chat._count_as_reports`). Session item
  `upd#<iso>#<person>` in `onebox-planning`. On close: check-in clock reset;
  an urgent item notifies the owner at once (`update_urgent`, app + email/WA).
* `api/services/update_reminders.py` ("hora de actualizar"): hourly, from
  UPDATE_REMINDER_HOUR_UTC (12) on, once per due period, each member WITH email
  whose update is due (not the owner) gets an email: prompt `UPDATE_PREP`
  (schema `UpdatePrep`) prepares 2-3 concrete questions from their open tasks
  and their last update (follow-ups like "¿ya llegó la cuenta de Azure?"; no
  task ids; deterministic fallback without LLM). The same questions open the
  chat in the app (`opening_for`, stored as `prep` in `checkin#<person>`).
  Replies to the email are not read: the button goes to the app (APP_URL).
* Simulation on REAL data: `scripts/sim/update_flow.py` (status / make-due /
  eventbridge / notifications / owner-late). `eventbridge` POSTs
  `/api/scheduled/dispatch-pending?force=true` (no hour gates, owner included).
  `EMAIL_OUTBOX=<folder>` in the backend writes emails to files instead of SES.
  `tasks` lists every participant with their open tasks and the tasks nobody
  is asked about; `ses` checks the SES account. `scripts/sim/fix_mock_data.py`
  makes MOCK data consistent (one name per email, assignees added as
  participants, unassigned open tasks spread over participants with email);
  dry run by default, `--apply` writes with a backup, `--restore` undoes.
  The daily digest also lists open tasks without assignee and assignees who
  are not participants ("Nadie da update de esto").
* `api/services/updates_consolidation.py`: DETERMINISTIC guard rules over the
  items (no LLM): no close on hearsay / low certainty; two people disagreeing
  on a task -> question, no change; conditional dates -> dependency + note, the
  date does not move; owner-only ideas -> questions; the assignee's word wins;
  Trello only for cards linked to tasks (done column -> close, due change).
  Saves ONE proposal (source `updates`, merged into a pending one on the same
  plan version), posts the digest in the planning chat (`kind:
  updates_digest`) and notifies the owner (`updates_digest`). Runs daily from
  `/api/scheduled/dispatch-pending` after UPDATES_DIGEST_HOUR_UTC (22) and on
  demand (`POST /api/projects/{id}/updates/consolidate`, button "Consolidar
  updates").
* `api/services/trello_reflect.py`: on approval (`planning_chat.decide`) the
  linked board is brought in line: creates -> cards in the project list,
  updates -> name/desc/due, done/blocked -> moved to the 'Hecho'/'Bloqueado'
  column if the board has one, deletes -> card archived. A card edited in
  Trello since the last sync is not overwritten; a card linked by hand and
  never synced keeps its title (only date and column are written). Trello
  errors never undo the approval; the result goes in the chat message.

PLANNING CHAT (Intelligence > Replanificación): `api/services/planning_chat.py`
+ `api/controllers/planning.py`, table `onebox-planning` (create it with
`python scripts/dynamo/create_planning_table.py`). The owner asks ONE thing
here: replan with context (the message itself, pasted text, a document, the
Trello board, "re-read all documents"); each context is saved as a project
attachment; the graph runs in a background job (the page polls
`/planning/jobs/{id}` for the step) and leaves ONE pending proposal (older ones
-> superseded). Approve all / some / reject with the buttons (not by chatting);
an applied proposal stores what it touched and can be reverted while the plan
has not changed since. The graph's `chat` trigger still exists but the chat no
longer routes to it.

CHECK-IN form (`api/services/checkin.py`, `POST /api/checkin`): the older
form version of the update; its clock (`checkin#<person>`), `pending_for_user`
and `notify_overdue` are reused by the member updates. The hourly
`/api/scheduled/dispatch-pending` runs `checkin.notify_overdue`: the owner is
told (app + email/WhatsApp) once per due period about each person
CHECKIN_GRACE_DAYS=1 past due, including people without an account.

LIVE: every path that analyses material (upload attachment, paste text, create
project from document / text / WhatsApp / chat) goes through
`generate_insights_for_project`. That function now writes only OBSERVATION
insights (summary, client, risks, decisions, metrics, tech issues) and hands the
tasks to `api/services/planning_live.py`, which runs the graph in a background
thread. A project's FIRST plan (no tasks yet) is applied directly; otherwise the
proposal is left pending in the planning chat (notification `plan_proposal`;
`PLANNING_UPLOADS=apply` applies directly instead). Proposal+events+trace are
logged to S3 `planning/<projectId>/<ts>.json`. `PLANNING_ENGINE=off` restores
the old task writer.

CONVERSATIONS (`tests/planning/conversations/`, README there), three kinds:
`member` (a member gives the update in the app chat: graded per turn -- asks
what matters, max questions, closes with a summary -- and on the recorded
items; the plan must not change), `consolidation` (several member chats + the
Trello board -> digest + proposal: urgent alerts, contradictions, guard rules,
questions for the owner; then approves and checks the Trello writes) and
`replan` (the owner replans with context, single turn). Old leader chats are in
`_archive/`. Run with `python tests/planning/run_conversation.py <name>
[--upto N] [--dry]` or the MCP tools planning_list_conversations /
planning_run_conversation / planning_conversation_report. AWS is faked in
memory, Trello answers from raw REST fixtures; the LLM is real (except --dry).
Unit test of the loop with a fake LLM: `tests/test_member_updates.py`.

Loop: run a case (`python tests/run_planning_case.py <case>` or the MCP tool
`planning_run_case`), read `runs/latest/report.md`, discuss the failure with the
user, change `prompts.py` with approval, run again. Rules in `tests/planning/README.md`.
