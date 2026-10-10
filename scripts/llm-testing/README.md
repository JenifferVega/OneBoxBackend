# LLM testing

Quick checks that every model the agent depends on is reachable and answers
in the shapes the graph needs. They use the same `.env` as the backend.

| Script | What it answers |
|---|---|
| `check_keys.py` | Are the Gemini, Anthropic and Ollama Cloud keys valid, and does each answer a tiny prompt? Straight to each API, no OneBox code. |
| `check_nodes.py` | Is each graph node's LLM up? Which provider/model answered, how fast, and why it failed. |

```bash
python scripts/llm-testing/check_keys.py                        # every key in .env
python scripts/llm-testing/check_keys.py --no-generate          # validate keys only (free)
python scripts/llm-testing/check_nodes.py                       # all four nodes, as configured
python scripts/llm-testing/check_nodes.py --node narrator --repeat 5
python scripts/llm-testing/check_nodes.py --model gemini:gemini-2.5-flash   # try a candidate everywhere
python scripts/llm-testing/check_nodes.py --planning            # also the replanning LLM
```

Model strings are `provider:model`, the same format as `NODE_LLM_<NODE>` in
`.env` (split on the first `:`, so `ollama-cloud:gpt-oss:120b` works).

Fallbacks are off unless `--with-fallbacks` is passed: with them on, a broken
provider looks healthy because another one answers for it.
