# Model benchmark — 2026-10-08 18:01

Nodes measured: **context_resolver, planner, validator, narrator** · cases: ambiguous_project_must_ask (1) · repeat: 1 · fallbacks OFF

| Model | Score (checks) | Turns OK | Cases OK | Avg turn | Measured node(s): avg / p95 ms | LLM failures | Est. cost |
|---|---|---|---|---|---|---|---|
| `A` Baseline (current .env) | **80.0%** (4/5) | 0/1 | 0/1 | 0.0 s | - | 0/0 | n/a |
| `X1` All DeepSeek | **80.0%** (4/5) | 0/1 | 0/1 | 25.8 s | planner 2/5, narrator 1/1 | 4/4 | n/a |

### Speed vs baseline `A`

| Config | Avg turn | vs baseline | planner avg | resolver avg | validator avg | narrator avg |
|---|---|---|---|---|---|---|
| `A` | 0.0 s | — | 0 ms | 0 ms | 0 ms | 0 ms |
| `X1` | 25.8 s | — | 2 ms (—) | 0 ms (—) | 0 ms (—) | 1 ms (—) |

Score = share of the cases' checks that passed (tools used, tool results, what the reply says or must not say). Cost covers the measured node(s) only and is an estimate from tests/bench/prices.py; n/a when the provider does not report tokens (Anthropic, Bedrock).

## `A` — Baseline (current .env)


### ❌ ambiguous_project_must_ask
- T1 ❌ 0.0 s · tools: -
  - failed: reply matches /(?i)(cual|cuál|que proyecto|qué proyecto|\?)/
  - reply: "Sorry, I couldn't process your request."

## `X1` — All DeepSeek

- **planner** (measured): ollama-cloud:deepseek-v4-pro:0813 · 3 calls, 3 failed · avg 2 ms, p95 5 ms · tokens 0 in / 0 out
  - error: `RuntimeError: Ollama HTTP request failed: HTTPSConnectionPool(host='ollama.com', port=443): Max retries exceeded with url: /api/chat (Caused by ProxyError('Unable to connect to proxy', OSError('Tunnel`
  - error: `RuntimeError: Ollama HTTP request failed: HTTPSConnectionPool(host='ollama.com', port=443): Max retries exceeded with url: /api/chat (Caused by ProxyError('Unable to connect to proxy', OSError('Tunnel`
  - error: `RuntimeError: Ollama HTTP request failed: HTTPSConnectionPool(host='ollama.com', port=443): Max retries exceeded with url: /api/chat (Caused by ProxyError('Unable to connect to proxy', OSError('Tunnel`
- **narrator** (measured): ollama-cloud:deepseek-v4.1-flash · 1 calls, 1 failed · avg 1 ms, p95 1 ms · tokens 0 in / 0 out
  - error: `RuntimeError: Ollama HTTP request failed: HTTPSConnectionPool(host='ollama.com', port=443): Max retries exceeded with url: /api/chat (Caused by ProxyError('Unable to connect to proxy', OSError('Tunnel`

### ❌ ambiguous_project_must_ask
- T1 ❌ 25.8 s · tools: -
  - failed: reply matches /(?i)(cual|cuál|que proyecto|qué proyecto|\?)/
  - reply: 'I ran your request but had a problem writing the summary. The data was processed correctly; try again to see the details.'
