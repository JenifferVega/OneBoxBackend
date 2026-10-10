# Model benchmark — 2026-10-08 19:10

Nodes measured: **context_resolver, planner, validator, narrator** · cases: member_cannot_change_plan,ambiguous_project_must_ask (2) · repeat: 1 · fallbacks OFF

| Model | Score (checks) | Turns OK | Cases OK | Avg turn | Measured node(s): avg / p95 ms | LLM failures | Est. cost |
|---|---|---|---|---|---|---|---|
| `O1` Small steps on gpt-oss:120b | **0.0%** (0/9) | 0/3 | 0/2 | 0.0 s | - | 0/0 | n/a |
| `X1` All DeepSeek ⚠️ too slow, stopped | **0.0%** (0/0) | 0/2 | 0/2 | 3.0 s | - | 0/0 | n/a |

Score = share of the cases' checks that passed (tools used, tool results, what the reply says or must not say). Cost covers the measured node(s) only and is an estimate from tests/bench/prices.py; n/a when the provider does not report tokens (Anthropic, Bedrock).

## `O1` — Small steps on gpt-oss:120b


### ❌ member_cannot_change_plan
- T1 ❌ 0.0 s · tools: -
  - ERROR: no LLM call succeeded in this turn (the graph failed before calling a model)
  - failed: reply matches /(?i)(due[ñn]o|propietari|owner|responsable del proyecto|l[ií]der)/
  - reply: "Sorry, I couldn't process your request."
- T2 ❌ 0.0 s · tools: -
  - ERROR: no LLM call succeeded in this turn (the graph failed before calling a model)
  - failed: reply matches /(?i)(due[ñn]o|propietari|owner|responsable del proyecto|l[ií]der)/
  - reply: "Sorry, I couldn't process your request."

### ❌ ambiguous_project_must_ask
- T1 ❌ 0.0 s · tools: -
  - ERROR: no LLM call succeeded in this turn (the graph failed before calling a model)
  - failed: reply matches /(?i)(cual|cuál|que proyecto|qué proyecto|\?)/
  - reply: "Sorry, I couldn't process your request."

## `X1` — All DeepSeek


### ❌ member_cannot_change_plan
- T1 ❌ 3.0 s · tools: -
  - ERROR: TIMEOUT: no answer after 3 s (turn abandoned)
  - reply: ''

### ❌ ambiguous_project_must_ask
- T1 ❌ 3.0 s · tools: -
  - ERROR: TIMEOUT: no answer after 3 s (turn abandoned)
  - reply: ''
