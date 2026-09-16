"""Narrator utilities: result truncation and human-prompt assembly."""
import json

MAX_RESULTS_CHARS = 4000


def truncate_results(results: dict, limit: int = MAX_RESULTS_CHARS) -> str:
    """Serialize results respecting the limit, truncating PER STEP
    (largest first) so that small results survive intact instead of
    cutting the whole blob at the end."""
    full = json.dumps(results, ensure_ascii=False, default=str, indent=2)
    if len(full) <= limit:
        return full

    # Proportional budget per step, trimming the largest ones first
    serialized = {
        k: json.dumps(v, ensure_ascii=False, default=str, indent=2)
        for k, v in results.items()
    }
    budget = limit - 50 * max(len(serialized), 1)  # margin for keys/notices
    per_step = max(budget // max(len(serialized), 1), 200)

    parts = []
    for k in sorted(serialized, key=lambda x: len(serialized[x])):
        text = serialized[k]
        if len(text) > per_step:
            text = text[:per_step] + "\n... (step truncated)"
        parts.append(f'"step_{k}": {text}')
    return "{\n" + ",\n".join(parts) + "\n}"


def build_user_prompt(user_message: str, results_text: str, guidance: str) -> str:
    """Build the narrator's human message with the intent-specific guidance."""
    return f"""## User message:
{user_message}

## Results obtained:
{results_text}

{guidance}

Present the results to the user."""
