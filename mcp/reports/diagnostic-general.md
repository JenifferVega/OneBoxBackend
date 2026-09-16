# General Diagnostic of the AI System — OneBox Agent
**Date:** 2026-06-09 | **Sessions run:** 5 | **Total turns:** 23

---

## Executive summary

The agent behaves correctly in most simple and medium flows. The `REQUIRED_PARAMS` rules work, the multi-step notification flow is solid, and extracting phases/participants from rich descriptions is one of the system's strengths. That said, there are **2 real problems** that affect reliability and **1 dry-run limitation** that skews the test results.

---

## Scorecard by session

| Session | Turns | Iterations > 1 | Errors | Score |
|---|---|---|---|---|
| Email | 6 | 2 (T2, T3) | 0 | 🟡 Good |
| Projects | 6 | 1 (T5) | 0 | 🟡 Good |
| Notifications | 4 | 0 | 0 | 🟢 Perfect |
| Utilities | 4 | 0 | 0 | 🟢 Perfect |
| Integration | 3 | 1 (T3) | 0 | 🟡 Good |

---

## Detected issues (ordered by impact)

---

### 🔴 ISSUE 1 — `match` by name fails with projects created in the same session
**Severity:** High  
**Occurrences:** Session 2 T5, Session 5 T3  
**Symptom:** The planner generates a correct plan with `match: {key: "name", value: "Alpha"}`, but the executor can't find the project because in dry-run `list_projects` always returns the same two fictional projects ("Demo Project A", "Demo Project B"), ignoring anything created in the same turn.

**Observed example (Session 2 T5):**
- Generated plan: `list_projects → create_task` with `match: {key: "name", value: "Alpha"}`  
- Result: "I couldn't find a project called Alpha" — the task was created without a valid project_id  
- Iterations: 3

**Root cause:** The `_DRY_RUN_RESULTS` for `list_projects` in `executor/node.py` are static and don't reflect projects created during the same debug session. The planner is doing the right thing; the problem is in the simulation layer.

**Production impact:** In production against real DynamoDB this problem doesn't exist — `list_projects` returns the freshly created project. **This is a dry-run bug, not a planner bug.**

**Recommended action:** In `executor/node.py`, make the simulated results of `create_project` accumulate in memory so the next `list_projects` in dry-run includes them:

```python
# In _DRY_RUN_RESULTS or in the executor logic:
# If create_project ran in a previous step,
# add that project to the simulated list_projects result.
```

---

### 🟡 ISSUE 2 — `list_emails` needs 2 iterations with known senders
**Severity:** Medium  
**Occurrence:** Session 1 T2 ("LinkedIn emails")  
**Symptom:** The planner uses `query: "linkedin"` on the first attempt instead of `query: "from:linkedin"`. It corrects itself on the second iteration.

**Root cause:** The `SEARCH_GUIDE` rule in `catalog.py` documents the `from:` pattern but has no explicit ❌/✅ contrast example for the known-sender case.

**Proposed change in `catalog.py` — `SEARCH_GUIDE` section:**

```
❌ INCORRECT — "linkedin" as a generic term when the user names a known sender:
  list_emails(query="linkedin")
  → Searches subject/body, doesn't filter by sender.

✅ CORRECT — use "from:" for any known service or company:
  list_emails(query="from:linkedin")
  list_emails(query="from:apple")
  list_emails(query="from:notion.so")
```

---

### 🟡 ISSUE 3 — Narrator out of sync on `auto_classify_messages` after `analyze_inbox`
**Severity:** Medium  
**Occurrence:** Session 1 T5  
**Symptom:** The previous turn (`analyze_inbox`) found 2 messages. On the next turn, `auto_classify_messages` returns "empty inbox". The narrator presents that as the real state, without mentioning the previous turn's messages.

**Root cause:** In dry-run, `auto_classify_messages` returns an empty result regardless of prior state. The narrator has no guidance to contrast with the conversation history.

**Proposed change in `narrator/narrators/`:** Add a specialized narrator (or a rule in the general narrator) so that when `auto_classify_messages` returns empty but the history contains `analyze_inbox` results, the state is explained coherently instead of contradicting the previous turn.

---

## What works correctly ✅

**Planner — confirmed strengths:**

- `REQUIRED_PARAMS` works: the planner does not run `create_project` or `create_reminder` without data. In every case where data was missing, it used `direct_response` correctly.
- `FULL_EXTRACTION` works: rich description → complete plan in 1 iteration. Session 2 T3 (Alpha, 3 phases) and Session 5 T1 (Marketing, 4 inferred phases) are the most demanding cases and both passed in 1 iteration.
- `foreach` in notifications: the `list_projects → get_project_contacts → send_notification(foreach)` flow worked perfectly in 1 iteration, sending to multiple recipients in a single step.
- Project type inference: "summer campaign" → Marketing → correct phases (Research, Strategy, Execution, Measurement).
- Resolving emails from context: `send_email` picked up `ana@company.com` from the previous turn without asking the user.
- `check_sla` vs `proactive_summary`: the agent doesn't confuse the two tools. "is anything urgent?" → `check_sla`, not `proactive_summary`.
- Task-without-project protection: "create an urgent task" with no context → `direct_response` asking for the project, never tries to invent a `project_id`.

---

## Tools tested table

| Tool | Tested | Result | Average iterations |
|---|---|---|---|
| `list_emails` | ✅ | Works, 2 iter on known senders | 1.5 |
| `inspect_email` | ✅ | Works with `from_step` | 2 |
| `analyze_inbox` | ✅ | Correct | 1 |
| `auto_classify_messages` | ✅ | Correct (narrator inconsistent) | 1 |
| `send_email` | ✅ | Correct, picks email from context | 1 |
| `list_projects` | ✅ | Correct | 1 |
| `create_project` | ✅ | Correct with full extraction | 1 |
| `create_task` | ✅ | Correct when a project is in context | 1–3 |
| `assign_email_to_project` | ✅ | Works (3 iter in multi-step) | 3 |
| `create_insight` | ⬜ | Not tested this cycle | — |
| `get_project_contacts` | ✅ | Correct | 1 |
| `send_notification` | ✅ | Correct with `foreach` and direct number | 1 |
| `list_notifications` | ✅ | Correct | 1 |
| `create_reminder` | ✅ | Correct with and without data | 1 |
| `check_sla` | ✅ | Correct | 1 |
| `proactive_summary` | ✅ | Correct | 1 |

---

## Change priority

| Priority | Change | File |
|---|---|---|
| 🔴 High | Dry-run accumulates created projects so `match` works across turns | `executor/node.py` |
| 🟡 Medium | Add ❌/✅ example for `from:` with known senders | `catalog.py` → `SEARCH_GUIDE` |
| 🟡 Medium | Coherent narrator when `classify` returns empty after `analyze_inbox` | `narrator/narrators/` |
| 🟢 Low | Test `create_insight` (only tool without coverage) | Next cycle |
