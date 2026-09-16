"""Structured output of the context resolver.

The resolver used to return a bare string, which is why a confirmation could
silently drop the request that motivated it: "yes" became "create a board" and
"send the tasks to Trello" existed nowhere in the state any more.

The standing goal is OPENED and CLOSED by code, from what the tools report --
those are facts, and a fact that can be checked should not become an opinion.
What genuinely needs judgement is whether the new message walks away from the
goal, and that is what this schema asks the model for.
"""
from typing import Literal

from pydantic import BaseModel, Field


class ResolverOutput(BaseModel):
    resolved_message: str = Field(
        description=(
            "The user's message, rewritten to stand on its own when it is an "
            "ambiguous follow-up, or returned unchanged when it is already "
            "clear. Never invent information that is not in the history."
        )
    )
    goal_verdict: Literal["keep", "abandon", "none"] = Field(
        default="none",
        description=(
            "Only about the PENDING GOAL you were given, if any. "
            "'keep': this message continues it (a confirmation, an answer to "
            "the assistant's question, a detail it asked for). "
            "'abandon': the user moved to something else and no longer wants "
            "it. "
            "'none': there was no pending goal."
        ),
    )
    goal_reason: str = Field(
        default="",
        description="One short sentence justifying the verdict. For telemetry.",
    )
