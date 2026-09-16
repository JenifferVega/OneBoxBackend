"""Deterministic planner fast-paths (Tier 1, no LLM).

Covers the "WHEN NOT TO USE TOOLS" cases from the previous prompt via
regex: greetings, help/capabilities and thanks. Everything else goes to the LLM.

The default responses are English; Spanish fallbacks are available when the
user clearly writes in Spanish.
"""
import re
from typing import Optional

from agent.graph.personality import (
    GREETING_RESPONSE, HELP_RESPONSE, THANKS_RESPONSE,
    GREETING_RESPONSE_ES, HELP_RESPONSE_ES, THANKS_RESPONSE_ES,
)

# Spanish detectors (tokens that only appear in Spanish) so fast-paths can
# reply in Spanish when the user clearly writes in Spanish. Default is English.
_ES_GREETING_RE = re.compile(
    r"^\s*(hola+|holi+|buenas|buenos\s+d[ií]as|buenas\s+tardes|buenas\s+noches|"
    r"qu[eé]\s+tal|saludos)[\s!.,?¡¿]*$",
    re.IGNORECASE,
)
_ES_HELP_RE = re.compile(
    r"(qu[eé]\s+puedes\s+hacer|c[oó]mo\s+funcionas|para\s+qu[eé]\s+sirves|"
    r"qui[eé]n\s+eres|^\s*ayuda[\s!.?]*$)",
    re.IGNORECASE,
)
_ES_THANKS_RE = re.compile(
    r"^\s*(muchas\s+gracias|gracias|perfecto|genial|listo|vale|de\s+acuerdo|"
    r"excelente)[\s!.,🙂😊👍]*$",
    re.IGNORECASE,
)

# English detectors (default language of the app).
_EN_GREETING_RE = re.compile(
    r"^\s*(hello+|hi+|hey+|good\s+(morning|afternoon|evening)|greetings)[\s!.,?]*$",
    re.IGNORECASE,
)
_EN_HELP_RE = re.compile(
    r"(what\s+can\s+you\s+do|how\s+do\s+you\s+work|what\s+are\s+you|who\s+are\s+you|"
    r"^\s*help[\s!.?]*$)",
    re.IGNORECASE,
)
_EN_THANKS_RE = re.compile(
    r"^\s*(thank\s+you|thank\s+u|thanks|great|perfect|awesome|got\s+it|okay|ok+)[\s!.,🙂😊👍]*$",
    re.IGNORECASE,
)


def match_fast_path(message: str) -> Optional[str]:
    """Return the predefined response if the message is a fast-path; otherwise None.

    Replies in the user's language: if the greeting/thanks/help is clearly in
    Spanish, returns the Spanish version; otherwise defaults to English.
    """
    msg = (message or "").strip()
    if not msg:
        return None
    # Spanish first (specific tokens); if none match, English is assumed.
    if _ES_GREETING_RE.match(msg):
        return GREETING_RESPONSE_ES
    if _ES_HELP_RE.search(msg):
        return HELP_RESPONSE_ES
    if _ES_THANKS_RE.match(msg):
        return THANKS_RESPONSE_ES
    if _EN_GREETING_RE.match(msg):
        return GREETING_RESPONSE
    if _EN_HELP_RE.search(msg):
        return HELP_RESPONSE
    if _EN_THANKS_RE.match(msg):
        return THANKS_RESPONSE
    return None
