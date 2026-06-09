"""Utilidades del narrator: truncado de resultados y armado del prompt humano."""
import json

MAX_RESULTS_CHARS = 4000


def truncate_results(results: dict, limit: int = MAX_RESULTS_CHARS) -> str:
    """Serializa los resultados respetando el límite, truncando POR PASO
    (los más grandes primero) para que los resultados pequeños sobrevivan
    completos en lugar de cortar el blob entero al final."""
    full = json.dumps(results, ensure_ascii=False, default=str, indent=2)
    if len(full) <= limit:
        return full

    # Presupuesto por paso proporcional, recortando primero los más grandes
    serialized = {
        k: json.dumps(v, ensure_ascii=False, default=str, indent=2)
        for k, v in results.items()
    }
    budget = limit - 50 * max(len(serialized), 1)  # margen para claves/avisos
    per_step = max(budget // max(len(serialized), 1), 200)

    parts = []
    for k in sorted(serialized, key=lambda x: len(serialized[x])):
        text = serialized[k]
        if len(text) > per_step:
            text = text[:per_step] + "\n... (paso truncado)"
        parts.append(f'"paso_{k}": {text}')
    return "{\n" + ",\n".join(parts) + "\n}"


def build_user_prompt(user_message: str, results_text: str, guidance: str) -> str:
    """Arma el mensaje humano del narrator con la guía específica de la intención."""
    return f"""## Mensaje del usuario:
{user_message}

## Resultados obtenidos:
{results_text}

{guidance}

Presenta los resultados al usuario."""
