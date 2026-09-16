"""Pydantic output schemas for the planner (structured output)."""
from typing import List, Optional

from pydantic import BaseModel, Field


class PlanStep(BaseModel):
    step: int = Field(description="Step order, starting at 1")
    tool: str = Field(description="EXACT tool name from the catalog")
    params: dict = Field(
        default_factory=dict,
        description=(
            "Tool parameters. A value may be {'from_step': N} "
            "to chain the full result of a previous step."
        ),
    )


class PlannerOutput(BaseModel):
    plan: List[PlanStep] = Field(
        default_factory=list,
        description="Steps to execute, in order. Empty if no tools are needed.",
    )
    direct_response: Optional[str] = Field(
        default=None,
        description=(
            "Direct reply to the user ONLY when tools are not needed "
            "(greeting, help, casual conversation, out-of-scope request)."
        ),
    )
    reasoning: Optional[str] = Field(
        default=None,
        description="Brief internal reasoning (not shown to the user).",
    )
