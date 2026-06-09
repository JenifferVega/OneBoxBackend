"""Schemas Pydantic de salida del planner (structured output)."""
from typing import List, Optional

from pydantic import BaseModel, Field


class PlanStep(BaseModel):
    step: int = Field(description="Orden del paso, empezando en 1")
    tool: str = Field(description="Nombre EXACTO de la herramienta del catálogo")
    params: dict = Field(
        default_factory=dict,
        description=(
            "Parámetros de la herramienta. Un valor puede ser {'from_step': N} "
            "para encadenar el resultado completo de un paso anterior."
        ),
    )


class PlannerOutput(BaseModel):
    plan: List[PlanStep] = Field(
        default_factory=list,
        description="Pasos a ejecutar, en orden. Vacío si no se necesitan herramientas.",
    )
    direct_response: Optional[str] = Field(
        default=None,
        description=(
            "Respuesta directa al usuario SOLO cuando no se necesitan herramientas "
            "(saludo, ayuda, conversación casual, solicitud fuera de alcance)."
        ),
    )
    reasoning: Optional[str] = Field(
        default=None,
        description="Razonamiento interno breve (no se muestra al usuario).",
    )
