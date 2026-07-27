"""Fast-paths deterministas del planner (Tier 1, sin LLM).

Cubren los casos de "CUÁNDO NO USAR HERRAMIENTAS" del prompt anterior con
regex: saludos, ayuda/capacidades y agradecimientos. Todo lo demás pasa al LLM.
"""
import re
from typing import Optional

from agent.graph.personality import (
    GREETING_RESPONSE, HELP_RESPONSE, THANKS_RESPONSE,
    GREETING_RESPONSE_EN, HELP_RESPONSE_EN, THANKS_RESPONSE_EN,
)

# Detectores de inglés (tokens que solo aparecen en inglés) para responder los
# fast-paths en el idioma del usuario. Si no es claramente inglés, se asume español.
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
    r"^\s*(thank\s+you|thank\s+u|thanks|great|perfect|awesome|got\s+it|okay)[\s!.,🙂😊👍]*$",
    re.IGNORECASE,
)

_GREETING_RE = re.compile(
    r"^\s*(hola+|holi+|buenas|buenos\s+d[ií]as|buenas\s+tardes|buenas\s+noches|"
    r"hey|hello|hi|qu[eé]\s+tal|saludos)[\s!.,?¡¿]*$",
    re.IGNORECASE,
)

_HELP_RE = re.compile(
    r"(qu[eé]\s+puedes\s+hacer|c[oó]mo\s+funcionas|para\s+qu[eé]\s+sirves|"
    r"qui[eé]n\s+eres|^\s*ayuda[\s!.?]*$|^\s*help[\s!.?]*$)",
    re.IGNORECASE,
)

_THANKS_RE = re.compile(
    r"^\s*(muchas\s+gracias|gracias|perfecto|genial|listo|ok+|vale|de\s+acuerdo|"
    r"excelente)[\s!.,🙂😊👍]*$",
    re.IGNORECASE,
)


def match_fast_path(message: str) -> Optional[str]:
    """Devuelve la respuesta predefinida si el mensaje es un fast-path; sino None.

    Responde en el idioma del usuario: si el saludo/agradecimiento/ayuda está en
    inglés, devuelve la versión en inglés; en cualquier otro caso, la de español.
    """
    msg = (message or "").strip()
    if not msg:
        return None
    # Inglés primero (tokens específicos); si no coincide, se asume español.
    if _EN_GREETING_RE.match(msg):
        return GREETING_RESPONSE_EN
    if _EN_HELP_RE.search(msg):
        return HELP_RESPONSE_EN
    if _EN_THANKS_RE.match(msg):
        return THANKS_RESPONSE_EN
    if _GREETING_RE.match(msg):
        return GREETING_RESPONSE
    if _HELP_RE.search(msg):
        return HELP_RESPONSE
    if _THANKS_RE.match(msg):
        return THANKS_RESPONSE
    return None
