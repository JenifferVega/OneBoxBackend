# Model benchmark — 2026-10-08 17:34

Nodes measured: **narrator, validator, context_resolver, planner** · cases: member_cannot_change_plan (1) · repeat: 1 · fallbacks OFF

| Model | Score (checks) | Turns OK | Cases OK | Avg turn | Measured node(s): avg / p95 ms | LLM failures | Est. cost |
|---|---|---|---|---|---|---|---|
| `ollama-cloud:gpt-oss:120b` | **50.0%** (2/4) | 0/2 | 0/1 | 25.8 s | narrator 1/2, context_resolver 2/2, planner 2/4 | 9/9 | $0.0000 |

Score = share of the cases' checks that passed (tools used, tool results, what the reply says or must not say). Cost covers the measured node(s) only and is an estimate from tests/bench/prices.py; n/a when the provider does not report tokens (Anthropic, Bedrock).

## `ollama-cloud:gpt-oss:120b`

- **planner** (measured): ollama-cloud:gpt-oss:120b · 6 calls, 6 failed · avg 2 ms, p95 4 ms · tokens 0 in / 0 out
  - error: `RuntimeError: Ollama HTTP request failed: HTTPSConnectionPool(host='ollama.com', port=443): Max retries exceeded with url: /api/chat (Caused by ProxyError('Unable to connect to proxy', OSError('Tunnel`
  - error: `RuntimeError: Ollama HTTP request failed: HTTPSConnectionPool(host='ollama.com', port=443): Max retries exceeded with url: /api/chat (Caused by ProxyError('Unable to connect to proxy', OSError('Tunnel`
  - error: `RuntimeError: Ollama HTTP request failed: HTTPSConnectionPool(host='ollama.com', port=443): Max retries exceeded with url: /api/chat (Caused by ProxyError('Unable to connect to proxy', OSError('Tunnel`
- **narrator** (measured): ollama-cloud:gpt-oss:120b · 2 calls, 2 failed · avg 1 ms, p95 2 ms · tokens 0 in / 0 out
  - error: `RuntimeError: Ollama HTTP request failed: HTTPSConnectionPool(host='ollama.com', port=443): Max retries exceeded with url: /api/chat (Caused by ProxyError('Unable to connect to proxy', OSError('Tunnel`
  - error: `RuntimeError: Ollama HTTP request failed: HTTPSConnectionPool(host='ollama.com', port=443): Max retries exceeded with url: /api/chat (Caused by ProxyError('Unable to connect to proxy', OSError('Tunnel`
- **context_resolver** (measured): ollama-cloud:gpt-oss:120b · 1 calls, 1 failed · avg 2 ms, p95 2 ms · tokens 0 in / 0 out
  - error: `RuntimeError: Ollama HTTP request failed: HTTPSConnectionPool(host='ollama.com', port=443): Max retries exceeded with url: /api/chat (Caused by ProxyError('Unable to connect to proxy', OSError('Tunnel`

### ❌ member_cannot_change_plan
- T1 ❌ 25.8 s · tools: -
  - failed: reply matches /(?i)(due[ñn]o|propietari|owner|responsable del proyecto|l[ií]der)/
  - reply: 'I ran your request but had a problem writing the summary. The data was processed correctly; try again to see the details.'
- T2 ❌ 25.7 s · tools: -
  - failed: reply matches /(?i)(due[ñn]o|propietari|owner|responsable del proyecto|l[ií]der)/
  - reply: 'I ran your request but had a problem writing the summary. The data was processed correctly; try again to see the details.'
