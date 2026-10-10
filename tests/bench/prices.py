"""US$ per 1M tokens (input, output), standard paid tier, checked 2026-10-08.

Sources: ai.google.dev/gemini-api/docs/pricing, ollama.com/pricing,
anthropic.com/pricing. Prices change: this table only turns token counts
into an ESTIMATE, so a report can compare models side by side. Update the
date above when you change a number. Keys are matched as substrings of the
model name, most specific first.
"""
PRICES = [
    # Gemini
    ("gemini-2.5-flash-lite", 0.10, 0.40),
    ("gemini-2.5-flash", 0.30, 2.50),
    ("gemini-2.5-pro", 1.25, 10.00),
    ("gemini-3.8-flash", 0.75, 3.75),
    # Anthropic
    ("claude-sonnet-4-5", 3.00, 15.00),
    ("claude-haiku-4-5", 1.00, 5.00),
    # Ollama Cloud
    ("deepseek-v4.1-flash", 0.30, 1.20),
    ("deepseek-v4-pro", 1.32, 3.96),
    ("gemma4", 0.14, 0.40),
    ("glm-5.3-flash", 0.15, 0.50),
    ("glm-5.3", 1.40, 4.40),
    ("glm-5.2", 1.40, 4.40),
    ("gpt-oss:120b", 0.15, 0.60),
    ("gpt-oss:20b", 0.07, 0.30),
    ("kimi-k3", 3.00, 15.00),
    ("kimi-k2.7", 0.95, 4.00),
    ("kimi-k2.6", 0.95, 4.00),
    ("minimax-m3", 0.60, 2.40),
    ("minimax-m2.7", 0.30, 1.20),
    ("mistral-large-4", 0.68, 2.09),
    ("mistral-large-3", 0.50, 1.50),
    ("nemotron-3-nano", 0.06, 0.24),
    ("nemotron-3-super", 0.015, 0.60),
    ("nemotron-3-ultra", 0.10, 3.00),
]


def price_for(model: str):
    m = (model or "").lower()
    for key, i, o in PRICES:
        if key in m:
            return i, o
    return None


def cost(model: str, prompt_tokens: int, output_tokens: int):
    p = price_for(model)
    if p is None:
        return None
    return (prompt_tokens * p[0] + output_tokens * p[1]) / 1_000_000
