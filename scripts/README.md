# scripts/ — what each one does

Run everything from the `OneBoxBackend` folder. Scripts that touch data use the
same `.env` as the backend (same AWS account, same API keys) and print which
AWS identity they use before doing anything.

**Safety convention:** scripts that write are a **dry run by default** and only
change data with `--apply`. Read-only scripts are marked as such.

---

## Everyday

| Script | What it does | Writes? |
|---|---|---|
| `scripts/check.sh` | Run before pushing: ruff (undefined names, dead code), mypy (calls that cannot bind), then every unit/contract test with the database, network and LLM faked. | No |

## `scripts/dynamo/` — data maintenance (DynamoDB)

| Script | What it does | Writes? |
|---|---|---|
| `backfill_translations.py` | Gives every stored insight card both languages (es + en): detects the language of `title` / `description` and writes the other one into an `i18n` field (original untouched). `--sample N` prints N translations without writing; `--project` limits it; resumable. Model: `TRANSLATE_LLM`. | `--apply` |
| `refresh_pending_markers.py` | Sets the "proposal waiting" marker (`pendingProposal`) on projects that already had a pending plan proposal. One-time; new proposals keep it updated by themselves. | `--apply` |
| `reset_trello_links.py` | Forgets every OneBox↔Trello link (board/list on projects, card ids on tasks) to start the integration clean. `--project` for one project; `--tokens` also disconnects every user's Trello account. Does not touch Trello itself. | `--apply` |
| `create_planning_table.py` | Creates the `onebox-planning` table (planning chat messages, proposals, follow-up state). One-off; safe to run twice. | Creates table |
| `create_users_table.py` | Creates the `onebox-users` table (per-user timezone, language). One-off; safe to run twice. | Creates table |

## `scripts/llm-testing/` — are the AI models working?

See also `scripts/llm-testing/README.md`.

| Script | What it does | Cost |
|---|---|---|
| `check_keys.py` | Are the Gemini / Anthropic / Ollama Cloud keys valid and answering? Lists models (free) and makes one tiny call per model your `.env` uses. `--only ollama`, `--no-generate` (free). | A few tokens |
| `check_nodes.py` | Is every agent node's model up, and how fast? Two tiny calls per node (plain text + structured output) as configured in `.env`. `--node planner`, `--model provider:model` to try a candidate, `--repeat 3` for latency, `--planning` to include the replanning model, `--translator` the es/en translator (a real translation), `--all` everything. | A few tokens |
| `check_gemini_request.py` | Which part of our Gemini request a model rejects (thinkingBudget, thinkingLevel, JSON schema, temperature). Made for the 400 error on Gemini 3; pass a model name to test another one. | < 1 cent |

## `scripts/sim/` — simulate flows on the real data

| Script | What it does | Writes? |
|---|---|---|
| `update_flow.py` | Walks the member update loop on the real data, step by step: can SES send, who has open tasks, make someone due, fire the hourly job (`eventbridge`), see the digest/proposal and notifications, simulate an owner who goes late. Subcommands: `ses`, `tasks`, `status`, `make-due`, `eventbridge`, `notifications`, `owner-late`. | Some subcommands (state for testing) |
| `fix_mock_data.py` | Makes the mock/test data consistent so the update loop can be tested (canonical names per email, enough open tasks per person). Saves a backup first; `--restore <backup>` undoes it. | `--apply` |

## `scripts/dev/` — development only (remove before full production)

| Script | What it does | Writes? |
|---|---|---|
| `list_view_as_users.py` | Lists every project owner and participant with a ready "view as" link for the dev-only impersonation feature (`?as=…`). `--project` filters, `--base-url` for another frontend URL. | No (read only) |

## One-off investigations (`scripts/probe_*.py`)

Kept for reference from the 2026-09-16 "empty params" incident; not part of
normal work.

| Script | What it does |
|---|---|
| `probe_empty_params.py` | Runs the real planner prompt against the real provider to see whether it emits empty params for the Trello flow. Read only. |
| `probe_gemini_schema.py` | Asks the real Gemini API what it does with each schema shape (4 small calls). |
| `probe_as_deployed.py` | Runs the probe against the code as deployed on 2026-09-16 by swapping two files temporarily (restores them in a `finally`). |

---

## Related tools outside `scripts/`

| Tool | What it does |
|---|---|
| `tests/planning/run_conversation.py <name>` | Runs a scripted conversation (replan / member update / consolidation) with the REAL model and grades it turn by turn. `--list`, `--dry` (no AI). Same as the MCP tool `planning_run_conversation`. Conversations live in `tests/planning/conversations/` (see its README). |
| `tests/run_case.py` | Runs the agent chat test cases in `tests/cases/` (multi-turn, optional "as member" turns). |
| `tests/bench/run_bench.py` | Benchmarks model configurations (`tests/bench/configs.json`) on the hard cases; speed vs. baseline. Also via the MCP tools `bench_run` / `bench_report`. |
| `mcp/server.js` | The MCP server: chat with the agent, run planning cases/conversations, switch models and benchmark, from Claude. Calls the backend on `localhost:8000`. |
