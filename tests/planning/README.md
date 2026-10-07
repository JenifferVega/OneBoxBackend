# Planning graph — test cases

The planning graph (`agent/planning/`) reads a project's material and proposes
changes to its task plan. It is tested with **cases**: a project as it was
(*before*), something that happens (*input*), and what a careful human would
have proposed (*expected*). A run produces the proposal and the project as it
would be after approving it (*after*), and scores the proposal.

## Folder of a case

```
tests/planning/cases/<case>/
  before.json     project export, exactly what the "Download" button gives
                  (GET /api/projects/{id}/export). Includes the documents' text.
  input.json      what happens: {"trigger", "mode", "today", "message", "document"}
  expected.json   the rules the proposal is scored against (format below)
  reference.json  optional: the full human reading the rules were derived from
  sources/        the original files (PDF, txt, docx...). An attachment in
                  before.json with "sourceFile": "sources/x.pdf" gets its text
                  extracted at run time by agent.document_parser.extract_text,
                  the same code production runs on upload. Never paste
                  extracted text into a fixture by hand.
  runs/<ts>/      generated, git-ignored: proposal.json, after.json, report.md, run.json
  runs/latest/    copy of the last run
```

`input.json`

| field | values |
|---|---|
| `trigger` | `reanalyze` (read every document again), `document` (one new document), `cron`, `task_event`, `chat` |
| `mode` | `full` or `delta` (only what a WhatsApp export adds over earlier exports) |
| `today` | `YYYY-MM-DD`, fixed so the case is reproducible |
| `document` | file name, for `document` |
| `message` | leader's message, for `chat` / note for `cron` |

## expected.json

```json
{
  "existing": {"<taskId>": {"accept": ["close", "question"], "note": "why"}},
  "pairs": [["<taskId that stays>", "<duplicate>"]],
  "plan_items": [{"id": "t4 AS-IS", "required": true, "any_of": [["as-is"], ["proceso", "actual"]]}],
  "limits": {"max_creates": 15, "max_questions": 5},
  "thresholds": {"existing_accuracy": 0.75, "plan_items_recall": 0.7}
}
```

- **existing**: what may happen to each current task. Actions: `keep`, `update`,
  `close`, `delete`, `block`, `reopen`, `question` (only asked about). A task
  not listed must be `keep`.
- **pairs**: duplicates where it does not matter which one goes, as long as
  exactly one is deleted.
- **plan_items**: work that must appear in the new plan, as text of a created
  or updated task. A group matches when ALL its words appear (accents and case
  ignored); an item matches when ANY group does.
- **violations** fail the case regardless of scores: more creates/questions
  than the limits, deleting work a person created, a "committed" date with no
  evidence, validator errors left unresolved.

## Running

```bash
python tests/test_planning_core.py                 # no LLM, part of scripts/check.sh
python tests/run_planning_case.py farmacia_reanalyze
python tests/run_planning_case.py --all
PLANNING_LLM=anthropic:claude-sonnet-4-5-20250929 python tests/run_planning_case.py --all
LLM_FALLBACKS=off ...                              # measure one provider honestly
```

From Claude via MCP (`mcp/server.js`, the one the desktop config runs):
`planning_list_cases`, `planning_run_case`, `planning_report`, `planning_chat`
(a dry-run planning chat whose state lives in `tests/planning/sessions/`).
They call the backend over HTTP (`/debug/planning/*`, in
`api/controllers/planning_debug.py`), so the backend must be running. Those
endpoints only answer requests from the same machine.

## Adding a case

1. Download the project with the **Download** button → `before.json`.
2. Write `input.json`.
3. Read the material yourself and write `expected.json` (and, ideally,
   `reference.json` with the full reading and the quotes behind each rule).
4. Run it. A failing case is a prompt to tune (`agent/planning/prompts.py`) or a
   rule to reconsider — decide which before changing anything.

## Current cases

| case | what it checks | status |
|---|---|---|
| `farmacia_reanalyze` | July tasks replaced by the Fase 1 proposal; the out-of-scope task deleted; no invented dates | ready |
| `bion_reanalyze` | 41 tasks after a 5,000-char analysis: close what the full chat shows done, delete duplicates, add the client deliverables | ready (before.json rebuilt by hand from the real export; see its `_mock` field) |
