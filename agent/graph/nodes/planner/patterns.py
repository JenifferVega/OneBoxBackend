"""Fast-paths deterministas del planner (Tier 1, sin LLM).

Cubren los casos de "CUÁNDO NO USAR HERRAMIENTAS" del prompt anterior con
regex: saludos, ayuda/capacidades y agradecimientos. Todo lo demás pasa al LLM.
"""
import re
from typing import Optional

from agent.graph.personality import GREETING_RESPONSE, HELP_RESPONSE, THANKS_RESPONSE

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
    """Devuelve la respuesta predefinida si el mensaje es un fast-path; sino None."""
    msg = (message or "").strip()
    if not msg:
        return None
    if _GREETING_RE.match(msg):
        return GREETING_RESPONSE
    if _HELP_RE.search(msg):
        return HELP_RESPONSE
    if _THANKS_RE.match(msg):
        return THANKS_RESPONSE
    return None
