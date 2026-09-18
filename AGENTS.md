# OneBox Backend — Context for Codex

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
6. Codex edits the corresponding conversational behavior file
7. Restart the server and repeat from step 2
```

### Core rule

**Codex does not modify conversational behavior files without the user's explicit approval of the change.**

The report shows the problem. The discussion defines the solution. The user approves. Codex edits.

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

## Imported Claude Cowork project instructions
