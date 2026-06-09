"""System prompt base del narrator (personalidad compartida)."""
from agent.graph import personality

NARRATOR_SYSTEM = "\n\n".join([
    personality.IDENTITY,
    "Eres el narrador de OneBox. Tu trabajo es presentar los resultados al usuario de forma clara, útil y proactiva.",
    personality.RESPONSE_STYLE,
    personality.LANGUAGE,
])
