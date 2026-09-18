"""Pydantic output schemas for the planner (structured output)."""
from typing import List, Optional

from pydantic import BaseModel, Field


def _params_schema() -> dict:
    """The declared shape of `params`, generated from the tool signatures.

    A bare `dict` here becomes `{"type": "object", "additionalProperties":
    true}`, and Gemini's responseSchema does not support additionalProperties.
    The adapter strips it, leaving an object with NO declared properties --
    and Gemini decodes under the schema, so with no legal key to emit it
    returns `{}`. Every parameter of every tool, empty, every turn. That is
    what happened in production on 2026-09-16.

    Declaring the properties fixes it at the root: the union of the 43
    parameter names the tools accept, all optional, each with its real type.
    Generated, so it cannot drift from the signatures.

    Degrades to the previous open object if the tools cannot be imported:
    losing parameter names is bad, failing to build the planner is worse.
    """
    try:
        from agent.tools.schema import param_union_schema
        return param_union_schema()
    except Exception as e:                        # pragma: no cover
        print(f"[planner.schemas] could not derive params schema: "
              f"{type(e).__name__}: {e} -- falling back to an open object")
        return {"additionalProperties": True}


class PlanStep(BaseModel):
    step: int = Field(description="Step order, starting at 1")
    tool: str = Field(description="EXACT tool name from the catalog")
    params: dict = Field(
        default_factory=dict,
        description=(
            "Parameters for THIS tool. Only the ones its signature accepts. "
            "A value may be {'from_step': N} to chain the full result of a "
            "previous step."
        ),
        json_schema_extra=_params_schema(),
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
