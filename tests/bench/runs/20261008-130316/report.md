# Model benchmark — 2026-10-08 13:03

Nodes measured: **context_resolver, planner, validator, narrator** · cases: ambiguous_project_must_ask (1) · repeat: 1 · fallbacks OFF

| Model | Score (checks) | Turns OK | Cases OK | Avg turn | Measured node(s): avg / p95 ms | LLM failures | Est. cost |
|---|---|---|---|---|---|---|---|
| `O1` Small steps on gpt-oss:120b | **100.0%** (5/5) | 1/1 | 1/1 | 8.8 s | planner 4584/4584 | 0/1 | n/a |

Score = share of the cases' checks that passed (tools used, tool results, what the reply says or must not say). Cost covers the measured node(s) only and is an estimate from tests/bench/prices.py; n/a when the provider does not report tokens (Anthropic, Bedrock).

## `O1` — Small steps on gpt-oss:120b

- **planner** (measured): anthropic:claude-sonnet-4-5-20250929 · 1 calls, 0 failed · avg 4584 ms, p95 4584 ms · tokens 0 in / 0 out

### ✅ ambiguous_project_must_ask
- T1 ✅ 8.8 s · tools: -
