"""Schema Pydantic de salida del validator (structured output)."""
from typing import Literal

from pydantic import BaseModel, Field


class ValidatorOutput(BaseModel):
    decision: Literal["DONE", "CONTINUE", "ERROR"] = Field(
        description=(
            "DONE = los resultados satisfacen la solicitud (incluye count=0 sin error). "
            "ERROR = error técnico (500, timeout, herramienta falló) → replanear. "
            "CONTINUE = resultados parciales, faltan pasos."
        )
    )
    summary: str = Field(default="", description="Breve resumen de lo encontrado o no encontrado")
    feedback: str = Field(
        default="",
        description="En ERROR/CONTINUE: qué falló y qué debería hacer distinto el planner",
    )
