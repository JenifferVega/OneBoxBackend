# Model benchmark — 2026-10-08 18:02

Nodes measured: **context_resolver, planner, validator, narrator** · cases: ambiguous_project_must_ask (1) · repeat: 1 · fallbacks OFF

| Model | Score (checks) | Turns OK | Cases OK | Avg turn | Measured node(s): avg / p95 ms | LLM failures | Est. cost |
|---|---|---|---|---|---|---|---|
| `X1` All DeepSeek | **0.0%** (0/5) | 0/1 | 0/1 | 25.8 s | planner 2/5, narrator 2/2 | 4/4 | n/a |

Score = share of the cases' checks that passed (tools used, tool results, what the reply says or must not say). Cost covers the measured node(s) only and is an estimate from tests/bench/prices.py; n/a when the provider does not report tokens (Anthropic, Bedrock).

## `X1` — All DeepSeek

- **planner** (measured): ollama-cloud:deepseek-v4-pro:0813 · 3 calls, 3 failed · avg 2 ms, p95 5 ms · tokens 0 in / 0 out
  - error: `RuntimeError: Ollama HTTP request failed: HTTPSConnectionPool(host='ollama.com', port=443): Max retries exceeded with url: /api/chat (Caused by ProxyError('Unable to connect to proxy', OSError('Tunnel`
  - error: `RuntimeError: Ollama HTTP request failed: HTTPSConnectionPool(host='ollama.com', port=443): Max retries exceeded with url: /api/chat (Caused by ProxyError('Unable to connect to proxy', OSError('Tunnel`
  - error: `RuntimeError: Ollama HTTP request failed: HTTPSConnectionPool(host='ollama.com', port=443): Max retries exceeded with url: /api/chat (Caused by ProxyError('Unable to connect to proxy', OSError('Tunnel`
- **narrator** (measured): ollama-cloud:deepseek-v4.1-flash · 1 calls, 1 failed · avg 2 ms, p95 2 ms · tokens 0 in / 0 out
  - error: `RuntimeError: Ollama HTTP request failed: HTTPSConnectionPool(host='ollama.com', port=443): Max retries exceeded with url: /api/chat (Caused by ProxyError('Unable to connect to proxy', OSError('Tunnel`

### ❌ ambiguous_project_must_ask
- T1 ❌ 25.8 s · tools: -
  - ERROR: no LLM call succeeded in this turn: RuntimeError: Ollama HTTP request failed: HTTPSConnectionPool(host='ollama.com', port=443): Max retries exceeded with url: /api/chat (Caused by ProxyError('Unable to connect to proxy', OSError('Tunnel
  - failed: reply matches /(?i)(cual|cuál|que proyecto|qué proyecto|\?)/
  - reply: 'I ran your request but had a problem writing the summary. The data was processed correctly; try again to see the details.'
