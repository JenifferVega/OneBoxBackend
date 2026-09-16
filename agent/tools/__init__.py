"""OneBox agent tools.

This used to be one 1,886-line module. It is now a package, split by subject,
but the public surface is UNCHANGED: every `from agent.tools import X` in the
codebase keeps working exactly as before.

Two things this file must never stop doing:

1. IMPORT EVERY SUBMODULE. Tools register themselves through the
   @register_tool decorator, which only runs on import. A submodule that is
   not imported here has its tools silently missing from TOOL_MAP, and the
   failure shows up at runtime as "Unknown tool: x" -- never at import time.
   The guard at the bottom turns that into a loud startup error instead.

2. RE-EXPORT THE NAMES BELOW. api/services/*, api/controllers/*, main.py and
   agent/project_helpers.py import tables and functions straight from
   `agent.tools`. Removing a name here breaks them at import time.

Layout:
    db            tables and endpoint constants
    context       per-request user context (multi-tenant isolation)
    access        project access rules
    channels      Twilio/SES config, phone normalization, notification log
    errors        shared error translation
    descriptions  TOOLS_DESCRIPTION for the planner prompt
    registry      TOOL_MAP, register_tool, execute_tool
    emails        inbox + outbound email tools
    projects      project tools
    tasks         task and reminder tools
    insights      SLA, classification, executive summary
    notifications send_notification and the notification log
    resolve       name -> project/person candidate ranking
"""


from agent.tools.db import (
    GMAIL_LIST_API,
    GMAIL_INSPECT_API,
    dynamodb,
    projects_table,
    conversations_table,
    tasks_table,
    insights_table,
    notifications_table,
    invitations_table,
)

from agent.tools.context import (
    set_current_user,
    clear_current_user,
    _current_uid,
    _current_email,
)

from agent.tools.access import (
    _accessible_project_ids,
    _has_project_access,
)

from agent.tools.channels import (
    TWILIO_ACCOUNT_SID,
    TWILIO_AUTH_TOKEN,
    TWILIO_PHONE_NUMBER,
    TWILIO_WHATSAPP_NUMBER,
    TWILIO_TEST_MODE,
    TWILIO_MESSAGING_SERVICE_SID,
    SES_FROM_EMAIL,
    SES_REGION,
    SES_SANDBOX,
    _ses_client,
    _get_ses_client,
    _ses_check_verified,
    E164_REGEX,
    _normalize_e164,
    _log_notification,
)

from agent.tools.errors import (
    _svc_error,
)

from agent.tools.descriptions import (
    TOOLS_DESCRIPTION,
)

from agent.tools.registry import (
    TOOL_MAP,
    register_tool,
    execute_tool,
)

from agent.tools.emails import (
    list_emails,
    inspect_email,
    analyze_inbox,
    send_email,
)

from agent.tools.projects import (
    create_project,
    assign_email_to_project,
    list_projects,
    get_project_contacts,
    update_project,
    invite_user,
    update_participants,
    remove_participant,
)

from agent.tools.tasks import (
    create_task,
    create_reminder,
    list_tasks,
    update_task,
    delete_task,
)

from agent.tools.insights import (
    create_insight,
    check_sla,
    auto_classify_messages,
    proactive_summary,
)

from agent.tools.notifications import (
    send_notification,
    list_notifications,
)

from agent.tools.trello import (
    create_trello_board,
    link_project_to_trello,
    list_trello_boards,
    prepare_trello_board,
    push_tasks_to_trello,
)

from agent.tools.resolve import (
    RECALL_FLOOR,
    MAX_CANDIDATES,
    _norm_name,
    _edits,
    _similarity,
    RESOLVE_ENTITY_DESC,
    resolve_entity,
    resolve_person,
)

__all__ = [
    "GMAIL_LIST_API",
    "GMAIL_INSPECT_API",
    "dynamodb",
    "projects_table",
    "conversations_table",
    "tasks_table",
    "insights_table",
    "notifications_table",
    "invitations_table",
    "set_current_user",
    "clear_current_user",
    "_current_uid",
    "_current_email",
    "_accessible_project_ids",
    "_has_project_access",
    "TWILIO_ACCOUNT_SID",
    "TWILIO_AUTH_TOKEN",
    "TWILIO_PHONE_NUMBER",
    "TWILIO_WHATSAPP_NUMBER",
    "TWILIO_TEST_MODE",
    "TWILIO_MESSAGING_SERVICE_SID",
    "SES_FROM_EMAIL",
    "SES_REGION",
    "SES_SANDBOX",
    "_ses_client",
    "_get_ses_client",
    "_ses_check_verified",
    "E164_REGEX",
    "_normalize_e164",
    "_log_notification",
    "_svc_error",
    "TOOLS_DESCRIPTION",
    "TOOL_MAP",
    "register_tool",
    "execute_tool",
    "list_emails",
    "inspect_email",
    "analyze_inbox",
    "send_email",
    "create_project",
    "assign_email_to_project",
    "list_projects",
    "get_project_contacts",
    "update_project",
    "invite_user",
    "update_participants",
    "remove_participant",
    "create_task",
    "create_reminder",
    "list_tasks",
    "update_task",
    "delete_task",
    "create_insight",
    "check_sla",
    "auto_classify_messages",
    "proactive_summary",
    "send_notification",
    "list_notifications",
    "RECALL_FLOOR",
    "MAX_CANDIDATES",
    "_norm_name",
    "_edits",
    "_similarity",
    "RESOLVE_ENTITY_DESC",
    "resolve_entity",
    "resolve_person",
    "list_trello_boards",
    "push_tasks_to_trello",
    "create_trello_board",
    "link_project_to_trello",
    "prepare_trello_board",
]


# --- registration guard -----------------------------------------------------
# Every tool the planner is told about must actually be callable. Without this,
# a submodule that stops being imported fails silently at request time with
# "Unknown tool", which is miserable to debug. Failing at import is better.
_EXPECTED_TOOLS = {
    "list_emails", "inspect_email", "analyze_inbox", "send_email",
    "create_project", "assign_email_to_project", "list_projects",
    "get_project_contacts", "update_project", "invite_user",
    "update_participants", "remove_participant",
    "create_task", "create_reminder", "list_tasks", "update_task", "delete_task",
    "create_insight", "check_sla", "auto_classify_messages", "proactive_summary",
    "send_notification", "list_notifications",
    "resolve_entity", "resolve_person",
    "list_trello_boards", "push_tasks_to_trello",
    "create_trello_board", "link_project_to_trello",
    "prepare_trello_board",
}
_missing = _EXPECTED_TOOLS - set(TOOL_MAP)
if _missing:
    raise ImportError(
        "agent.tools: these tools did not register: %s. A submodule is most "
        "likely missing from the imports above." % sorted(_missing)
    )
