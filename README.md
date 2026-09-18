# OneBox Backend

Python/FastAPI backend with a conversational agent built on LangGraph.

```
User → context_resolver → planner → executor → validator → narrator → Response
```

`CLAUDE.md` and `AGENTS.md` describe the architecture and the debugging loop.
This file is about running it and about checking it before you push.

## Running locally

```bash
pip install -r requirements.txt
cp .env.example .env          # then fill it in; .env is gitignored and is NOT
                              # copied into the image (see .dockerignore)
python main.py                # http://localhost:8006, docs at /docs
```

In production the container gets its environment from the ECS task definition,
not from `.env`. A variable that only exists in your `.env` does not exist in
production — which is how the planner ended up running on a different LLM
provider there than it does on your machine.

---

## Before pushing: `./scripts/check.sh`

```bash
./scripts/check.sh
```

Three passes, cheapest first, about a minute in total. Each catches a class the
others cannot, and each one is in there because that class of bug reached a
user.

### 1. `ruff` — names that do not exist

Undefined names, unreachable code, unused imports. This is what catches the
shape of

```python
UnboundLocalError: local variable 'participants' referenced before assignment
```

which returned a 500 on every project created from a document, for weeks. It is
invisible to a type checker and to any test that does not execute that exact
line; `ruff` reports it as `F821` without running anything.

### 2. `mypy` — calls that cannot bind

Filtered down to `call-arg` and `attr-defined`, the two codes that mean a real
crash. A function called with an argument it does not accept, or with a
required one missing, is a fact about two signatures that nobody was comparing.
The rest of mypy's output is noise on a codebase without full annotations, so
it is deliberately not shown.

### 3. Contracts and smoke tests

| Test | What it protects |
|---|---|
| `test_contracts.py` | The planner is told how to call a tool by prose in `catalog.py` and `descriptions.py`. Prose drifts from the signatures it describes. This compares them: a documented parameter that does not exist, a required parameter that is documented nowhere, a tool with no dry-run fixture, a tool classified as neither read nor write. |
| `test_tool_schemas.py` | The schema the planner is decoded under must be one it can actually fill. See *The 2026-09-16 incident* below. |
| `test_smoke_tools.py` | Runs all 30 tools twice each — minimum call and full call — with the database and the network faked. A contract check cannot tell you a function is broken inside; running it can. A tool that returns an error or raises `HTTPException` passes: it ran and refused politely, which is working code. |
| `test_smoke_services.py` | The same, for the REST/wizard side. Every test we had drove the chat agent, and the 500 above was on the other surface. |
| `test_participants.py` | Participant normalisation, where that 500 lived. |
| `test_trello_diff.py` | The three-way merge between a task and its Trello card. |
| `test_pending_goal.py` | A goal is opened and closed from tool evidence, not from what the model claims. |
| `test_resolve_chaining.py` | `from_step` extraction, including the nested cases. |
| `test_chain_ref.py` | A chaining reference is recognised whether the provider wrote it as an object or serialised it into a string. |
| `test_executor_ledger.py` | A replan must not run a write twice. |

No LLM, no AWS, no network: everything is faked, so the whole thing is safe to
run on every change and costs nothing.

A test that reports `SKIP` checked nothing. That is neither a pass nor a
failure — it is there so a harness cannot certify behaviour nobody verified.

---

## Diagnostic probes

Not part of `check.sh`. These call real APIs and cost a few cents; run them when
you are chasing something specific.

```bash
python scripts/probe_empty_params.py [provider]
```

Runs the real planner prompt against a real provider and prints the parameters
of every step, flagging any step missing a required one. With no argument it
uses whatever `.env` says; pass `gemini`, `anthropic` or `bedrock` to force one,
which also disables the fallbacks so a failure surfaces instead of being
silently served by another provider.

```bash
python scripts/probe_as_deployed.py [provider]
```

The same probe, against the files as they were at the last deployed commit.
Swaps them in, runs, and restores them in a `finally` — no git writes, because
a stale `.git/index.lock` makes `git stash` fail silently.

```bash
python scripts/probe_gemini_schema.py
```

Asks the Gemini API directly what it does with each schema shape.

---

## The 2026-09-16 incident

Worth reading before changing the planner's output schema, because the failure
was invisible in every way a failure can be.

A user reported that the Trello integration did not work. The logs showed, for
several turns in a row:

```
step=2 tool=create_trello_board   params={}
step=2 tool=push_tasks_to_trello  params={}
step=? tool=resolve_entity        params={}
```

Every parameter of every tool, empty, identical across four replans. Trello
itself was fine — the board listing in the same session returned two real
boards. It worked locally and could not be reproduced.

`PlanStep.params` was declared as a bare `dict`, which Pydantic renders as
`{"type": "object", "additionalProperties": true}`. Gemini's `responseSchema`
does not support `additionalProperties`, so the adapter strips it — correctly,
Gemini rejects it — and what reaches the model is `{"type": "object"}`: an
object with no declared properties.

Gemini's structured output is a **constrained decoder**, not a hint. With no
property declared and no `additionalProperties` allowed, there is no legal key
to emit and `{}` is the only output the grammar permits. Anthropic and Bedrock
read the same schema as a description and fill it from the catalog, which is
why it worked on every developer machine: `.env` sets `LLM_PROVIDER=anthropic`,
and production does not get `.env`.

Three things came out of it, and they are the reason the current code looks the
way it does:

- **`agent/tools/schema.py`** derives the parameter schema from the real
  signatures, so `params` has declared properties and no hand-written copy of
  the tool list can drift from the code.
- **`_assert_fillable`** in the Gemini adapter raises if normalisation leaves a
  field the decoder could never fill. Failing at construction beats planning
  nothing in production.
- **`obs.log("llm_configured", ...)`** and one line per Gemini call record which
  provider served a turn. Nothing in the original logs named the provider,
  which is what turned a one-line cause into a night of deduction.

The general lesson, which applies well beyond this file: **a lossy translation
that fails silently is worse than one that fails loudly.** The sanitiser had
everything it needed to notice that it had just produced an unfillable field,
and it carried on.
