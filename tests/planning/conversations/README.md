# Conversation tests: the update loop

The core of the product is the UPDATE loop:

    member gives an update in the app chat (AI asks, records items, changes nothing)
      -> daily consolidation (guard rules) -> digest + ONE proposal for the owner
      -> owner approves with the buttons -> app + Trello

The owner's planning chat (Intelligence > Replanificación) TALKS by default
(questions about the plan, about why the AI decided something, about what the
documents say) and replans only on an explicit order, attached context or the
"prepare a proposal with this" button. These scripted conversations test both, with the real
LLM, AWS faked in memory and Trello answered from raw REST fixtures. People
write like people: short bursts, nicknames ("Jenny", "Ale", "Clau"), "casi",
"creo que", "la otra semana", "si Rafa me pasa las urls", things remembered late.

| Folder | Kind | What it exercises |
|---|---|---|
| `member_jenny` | member | "casi", a blocker that depends on the owner (urgent), vague date, an idea, a late "ah y..." |
| `member_ale` | member | vague start, half report, no dates, a condition, hearsay about someone else's task |
| `member_eloi` | member | no tasks of his own: ideas and process proposals for the owner |
| `member_espera_lider` | member + follow-up | stopped by the LEAD (Azure account Santi must create) and by a third party (Meta), plus one task that is hers: who is asked now, and the "waiting on you" notice to the lead |
| `member_proyecto_cancelado` | member + follow-up | "the project was cancelled": one blocker on the lead (no task), nothing left to ask the member, the lead is told |
| `member_claudia` | member | BION: abbreviations, a date conditioned on another person, an unanswered question |
| `consolidate_onebox` | consolidation | Jenny + Ale + Eloi + the Trello board -> urgent alert, contradiction, guard rules, questions, digest; approval written to Trello |
| `bion_replan_mensaje` | replan | the owner replans with a message only (no attachment) |
| `bion_preguntas` | replan + talk | after a replan the owner ASKS: why each decision, what to confirm with whom, a complaint ("no te pedí replanear"), approves, asks about the applied plan and the sources, reports a fact and presses "prepare a proposal" |
| `bion_trello` | replan | replan from a Trello board the team moved by hand |
| `farmacia_acta` | replan | replan from a meeting record (.docx, table at the end) |
| `onebox_semana` | replan | replan from a weekly update (.md) + a hand-made Trello board that disagrees |

`_archive/` keeps the old leader chats (bion_chat, farmacia_chat, onebox_lider):
the planning chat no longer answers questions or approves by chatting.

Run: `python tests/planning/run_conversation.py <name> [--dry]` or the MCP tool
`planning_run_conversation`. Report in `<name>/runs/latest/report.md`.

## member (script.json)

```json
{"kind": "member", "today": "2026-06-05", "member": {"name": "Jeniffer", "email": "..."},
 "turns": [{"messages": ["hola! pues bien", "lo de microsoft casi listo"], "expect": {...}}],
 "expect_report": {"items": [...], "items_absent": [...], "plan_unchanged": true}}
```

`expect_followup` (optional, after the update): `waiting_tasks` (the member is NOT asked
about them: someone else must act), `responsible_tasks` (still the member's), `on_lead_min`,
`lead_notice_mentions` / `lead_notice_absent` (each entry = alternative words), `lead_notice: false`.
`languages` (top level): `{"<email>" | "lead": "en"}` = the app language each person chose.

Per turn: `ai_asks` (+`ai_asks_min`) keyword groups the AI must ask about,
`ai_questions_max`, `ai_closes`, `ai_summary_mentions` (+`ai_summary_min`).
Report items: `{type, task, mentions, owner, certainty, due_date (list of
accepted), depends_on, about_person, urgent, decision_for_owner}`; every key
is optional. `owner` (optional, top level) = the project owner; default: the
first participant with an email.

## consolidation (script.json)

`inputs`: `{"member_conversation": "member_jenny"}` (run in order on ONE
shared project) and `{"trello_api": "../x/fixtures/trello_api.json"}`.
`project_patch` / `trello_links` (taskId -> cardId) simulate a linked board.
`expect`: `urgent_to_owner` [{mentions, after}], `contradictions` [{task,
mentions}], `proposal` {ops, ops_absent, depends, decisions_for_owner},
`digest` {mentions, mentions_min, not_reported}, `plan_unchanged_until_approved`.
`approve: true` then approves everything and grades `expect_trello`:
`writes` [{method, card, params, param_keys}], `writes_absent`, `status`.

## replan

The format below (`turns` with `context`), one turn: the owner's instruction
with its context.

## script.json

```json
{"today": "2026-09-25", "leader": {"name": "Jesus", "email": "..."},
 "turns": [{"from": "leader", "messages": ["...", "..."], "context": {"trello": true, "file": "fixtures/x.docx"},
            "why": "what a human means here", "expect": {...}}]}
```

`messages` with several entries = a burst sent within seconds (one turn).
`"replan": true` = the leader pressed "prepare a plan proposal with this" (no
classification). `"button": "approve_all" | "reject_all"` (no messages) = the
leader pressed a button of the pending proposal.
Task references use the stable ids of before.json; a prefix is enough for
hex ids. Keyword groups follow the reanalysis evaluator: `[["a", "b"], ["c"]]`
= (a AND b) OR c, matched at word start, accents ignored.

## expect

| Key | Meaning |
|---|---|
| `intent` | question / change / feedback / decide / revert / action / clarify (string or list of accepted) |
| `proposal` | after the turn: none, new, edited (same proposal changed), unchanged |
| `ops` / `ops_absent` | operations that must / must not be in the open proposal: `{op, task, fields}`; a field value may be a list of accepted values |
| `ops_any` | at least one of these |
| `creates` / `creates_absent` | new tasks by keywords in their text, with optional fields |
| `creates_or_notes`, `creates_or_updates` | the item may come as a task or as a note / update |
| `notes_mentions`, `asks`, `answer_mentions` (+ `_min`) | keyword groups in notes, questions back, or the answer text |
| `decision` | `{kind: approve_all / approve_some / reject_all, exclude: {op: [...]} or {task: [...]}}` |
| `applied` | whether the plan changed in this turn |
| `plan_state` | fields of tasks in the APPLIED plan after the turn |
| `handoff` | external action passed to the assistant chat: `to` (any of), `to_also`, `channel`, `mentions`, `tool_hint` |
| `depends_on_min`, `depends_absent` | dependency checks on the open proposal |
| `answer_absent` | keyword groups the answer must NOT contain (e.g. "no encontré cambios") |
| `nodes_absent` | graph nodes that must not run (a question must not reach `extract_facts` / `reconcile`) |
| `sources_added` | context files saved in the turn (a question saves 0) |
| `offers_replan` | the reply shows the "prepare a proposal with this" button |
| `max_seconds` | the turn must finish within this time |
