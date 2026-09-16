"""Per-intent guidance for the narrator dispatcher."""
from agent.graph.nodes.narrator.narrators import (  # noqa: F401
    emails, generic, notifications, proactive, projects,
)

GUIDANCE_BY_INTENT = {
    "emails": emails.GUIDANCE,
    "projects": projects.GUIDANCE,
    "notifications": notifications.GUIDANCE,
    "proactive": proactive.GUIDANCE,
    "generic": generic.GUIDANCE,
}
