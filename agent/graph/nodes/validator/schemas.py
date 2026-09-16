"""Pydantic output schema for the validator (structured output)."""
from typing import Literal

from pydantic import BaseModel, Field


class ValidatorOutput(BaseModel):
    decision: Literal["DONE", "CONTINUE", "ERROR"] = Field(
        description=(
            "DONE = the results satisfy the request (includes count=0 with no error). "
            "ERROR = technical failure (500, timeout, tool failed) → replan. "
            "CONTINUE = partial results, steps are missing."
        )
    )
    summary: str = Field(default="", description="Brief summary of what was found or not found")
    feedback: str = Field(
        default="",
        description="On ERROR/CONTINUE: what failed and what the planner should do differently",
    )
