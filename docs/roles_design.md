# Design proposal — Role and permissions system (OneBox)

> Status: **design proposal** (not implemented). Agreed scope: **hybrid** model = global role per user + per-project override.
> Basis: review of `agent/tools.py`, `mcp/server.py` and `api/services/access.py`.

---

## 1. Current state (what exists today)

OneBox **has no role system**. The only access control is at the **project** level:

- `api/services/access.py → has_project_access(uid, email, project_id)` returns `(has_access, is_owner, project)`.
  A user can access a project if: (1) they are the `owner` (`proj.userId == uid`), (2) their email appears in `participants[]`, or (3) they have an `accepted` invitation.
- `agent/tools.py → _has_project_access(project_id)` is the "last-mile defense" write tools call before touching DynamoDB, so the LLM cannot inject someone else's `projectId`.
- The `participants[].rol` field ("Client", "Developer", "PM") is **descriptive text**, it does not control permissions.

Consequences:

1. There is already an implicit binary distinction: **owner** vs **participant**. The docstring for `has_project_access` itself says that administrative endpoints "must require `is_owner=True`", but this is not formalized as named roles nor applied consistently.
2. The conversational agent **applies no role check**. `execute_tool(tool_name, params)` runs any tool without looking at who the user is; only some write tools call `_has_project_access`. There is no way to say "this user can only read" or "this user cannot send external communication".
3. There is no separation between low-risk actions (read) and high-risk ones (send WhatsApp/SMS/email to the outside world).

This design builds on that base and formalizes it without breaking it.

---

## 2. Design principles

1. **Classify by capability and risk, not by individual tool.** Roles grant *capabilities*; each tool requires a capability. Adding a new tool then only requires labeling it, not editing every role.
2. **The most permissive role cannot bypass project scope.** Roles and project access are orthogonal: the role says *what kind of action* you can do; project access says *on what data*. Both filters must pass.
3. **Hybrid with a global ceiling.** The global role defines the **maximum** capabilities of a user. The per-project role can only **restrict** within that project (or, for administration, elevate to admin *of that project*) — never grant a capability the global role doesn't allow. This prevents privilege escalation.
4. **Single-point enforcement.** One gate (`can(...)`) in `execute_tool` for the agent, mirrored in the API controllers. No scattered ad-hoc checks.
5. **Fail-closed.** If the role can't be determined or a tool's capability isn't declared, access is denied.

---

## 3. Classified action catalog

The 16 tools registered in `agent/tools.py`, grouped by required **capability** and **scope**.

| # | Action | Scope | Required capability | Risk |
|---|--------|-------|---------------------|--------|
| 1 | `list_emails` | global | `read` | low |
| 2 | `inspect_email` | global | `read` | low |
| 3 | `analyze_inbox` | global | `read` | low |
| 4 | `list_projects` | global | `read` | low |
| 5 | `list_notifications` | project* | `read` | low |
| 6 | `get_project_contacts` | project | `read` | low (contact PII) |
| 7 | `check_sla` | global | `read` (analysis) | low |
| 8 | `proactive_summary` | global | `read` (analysis) | low |
| 9 | `auto_classify_messages` | global | `read` (suggest only) | low |
| 10 | `create_project` | global | `write_internal` | medium |
| 11 | `assign_email_to_project` | project | `write_internal` | medium |
| 12 | `create_insight` | project | `write_internal` | medium |
| 13 | `create_task` | project | `write_internal` | medium |
| 14 | `create_reminder` | project* | `write_internal` | medium |
| 15 | `send_email` | project* | `send_external` | **high** |
| 16 | `send_notification` | project* | `send_external` | **high** (WhatsApp/SMS/email, scheduling + recurrence) |

\* `project_id` is optional in these tools. Rule: if `project_id` is present → the **per-project** role applies; if it's absent → the **global** role applies (account-level action).

Administrative capabilities (not agent tools; they live in the project API: `update_participants`, `delete project`, invitations, role management):

| Capability | Actions |
|-----------|----------|
| `manage_project` | edit/delete project, add/remove participants, invite |
| `manage_roles` | assign other users' global role and per-project role |

### The four base capabilities

- **`read`** — query and analyze. No write or external side effects.
- **`write_internal`** — create/modify data inside OneBox (projects, tasks, insights, reminders, assignments). No outbound output.
- **`send_external`** — send communication that goes out to the world (email, WhatsApp, SMS). The risk red line.
- **`manage_project` / `manage_roles`** — administration.

---

## 4. Role model

### 4.1 Global roles (at the user / workspace level)

They set the **ceiling** on user capabilities across the whole system.

| Global role | `read` | `write_internal` | `send_external` | `manage_project` | `manage_roles` |
|------------|:---:|:---:|:---:|:---:|:---:|
| **Owner** (account owner) | ✅ | ✅ | ✅ | ✅ | ✅ |
| **Operator** | ✅ | ✅ | ✅ | ➖¹ | ❌ |
| **Collaborator** | ✅ | ✅ | ❌ | ❌ | ❌ |
| **Viewer / Analyst** | ✅ | ❌ | ❌ | ❌ | ❌ |

¹ The Operator can administer **only projects where they are project admin** (see 4.2), not all of them.

Reading the table:

- **Owner**: full control, including managing who has which role. Today this maps to the original `proj.userId` / account owner.
- **Operator**: runs the full day-to-day, including external communication, but doesn't manage other users' roles or delete things they don't administer.
- **Collaborator**: works inside OneBox (creates projects, tasks, insights) but **cannot send anything to the outside**. Useful for junior profiles, interns or integrations that should only organize information.
- **Viewer / Analyst**: queries and uses the summaries / SLA / classification (which only suggest), without modifying or sending anything. Useful for stakeholders, auditing, dashboards.

### 4.2 Project roles (override within a project)

Stored in `participants[].rol_permiso` on each project. They refine what the user can do **inside that specific project**.

| Project role | Meaning |
|-----------------|-------------|
| **Project Admin** | Administers the project: participants, invitations, deletion. (= today's `is_owner`) |
| **Project Editor** | Creates/edits inside the project: tasks, insights, assignments, reminders, and — if the global role allows it — external project communication. |
| **Project Viewer** | Read-only on the project. |

### 4.3 Combination rule (the "hybrid")

For an action with `project_id`, the **effective permission** is computed as:

```
allowed_capability =
    capabilities(global_role)             # ceiling: what the user can do in general
  ∩ capabilities(project_role)            # per-project floor: what is allowed here
  ∪ admin_extras_if(project_role == Admin)   # a project admin gets manage_project ONLY for this project
```

In words:

1. The **global role sets the ceiling**. A global Viewer will never be able to `send_external`, even if they are Editor in a project.
2. The **project role restricts** within that project. A global Operator who is Viewer in "Project X" only reads in that project.
3. **Administration exception**: being **Project Admin** grants `manage_project` *for that project* even if the global role is not Owner. It is the only elevation allowed, and it is scoped to that project.
4. If the user **is not a participant** of the project (no project role) but has access through another path, their effective permission is the **global role filtered to `read`** unless they are global Owner. (Configurable; conservative by default.)

For **global** actions (no `project_id`: `list_emails`, `create_project`, `proactive_summary`, etc.) only the **global role** applies.

---

## 5. Permissions matrix — global role × action

✅ allowed · ❌ denied · 🔒 allowed only if the project role also grants it (see §4.3)

| Action | Owner | Operator | Collaborator | Viewer |
|--------|:---:|:---:|:---:|:---:|
| `list_emails` | ✅ | ✅ | ✅ | ✅ |
| `inspect_email` | ✅ | ✅ | ✅ | ✅ |
| `analyze_inbox` | ✅ | ✅ | ✅ | ✅ |
| `list_projects` | ✅ | ✅ | ✅ | ✅ |
| `list_notifications` | ✅ | ✅ | ✅ | ✅ |
| `get_project_contacts` | ✅ | 🔒 | 🔒 | 🔒 |
| `check_sla` | ✅ | ✅ | ✅ | ✅ |
| `proactive_summary` | ✅ | ✅ | ✅ | ✅ |
| `auto_classify_messages` | ✅ | ✅ | ✅ | ✅ |
| `create_project` | ✅ | ✅ | ✅ | ❌ |
| `assign_email_to_project` | ✅ | 🔒 | 🔒 | ❌ |
| `create_insight` | ✅ | 🔒 | 🔒 | ❌ |
| `create_task` | ✅ | 🔒 | 🔒 | ❌ |
| `create_reminder` | ✅ | 🔒 | 🔒 | ❌ |
| `send_email` | ✅ | 🔒 | ❌ | ❌ |
| `send_notification` | ✅ | 🔒 | ❌ | ❌ |
| (admin) manage participants / invite / delete project | ✅ | 🔒² | ❌ | ❌ |
| (admin) assign roles | ✅ | ❌ | ❌ | ❌ |

² Only in projects where the Operator is Project Admin.

---

## 6. Code enforcement plan

Minimal changes, aligned with the current architecture. **A single gate**, mirrored between agent and API.

### 6.1 Propagate the user's role into the context

`agent/tools.py → set_current_user(uid, email)` already sets the current user. Extend it:

```python
# agent/tools.py
_CURRENT = {"uid": "", "email": "", "global_role": "viewer"}

def set_current_user(uid, email="", global_role="viewer"):
    _CURRENT.update(uid=uid, email=email, global_role=(global_role or "viewer"))
```

The global role is read from the users table and passed in from `api/controllers/chat.py` (which already calls `set_current_user(uid, user_email)`), and also via header in the REST controllers.

### 6.2 Declare capabilities and role map

Declarative table (single source of truth):

```python
# agent/permissions.py  (new)
TOOL_CAP = {
    "list_emails": "read", "inspect_email": "read", "analyze_inbox": "read",
    "list_projects": "read", "list_notifications": "read",
    "get_project_contacts": "read", "check_sla": "read",
    "proactive_summary": "read", "auto_classify_messages": "read",
    "create_project": "write_internal", "assign_email_to_project": "write_internal",
    "create_insight": "write_internal", "create_task": "write_internal",
    "create_reminder": "write_internal",
    "send_email": "send_external", "send_notification": "send_external",
}

ROLE_CAPS = {
    "owner":        {"read", "write_internal", "send_external", "manage_project", "manage_roles"},
    "operator":     {"read", "write_internal", "send_external"},
    "collaborator": {"read", "write_internal"},
    "viewer":       {"read"},
}

PROJECT_ROLE_CAPS = {
    "admin":  {"read", "write_internal", "send_external", "manage_project"},
    "editor": {"read", "write_internal", "send_external"},
    "viewer": {"read"},
}
```

### 6.3 Central gate

```python
def can(tool_name, project_id=""):
    cap = TOOL_CAP.get(tool_name)
    if cap is None:
        return False                      # fail-closed: unclassified tool
    global_caps = ROLE_CAPS.get(_CURRENT["global_role"], set())
    if cap not in global_caps:
        return False                      # global ceiling
    if project_id:                        # project-scoped action
        proj_role = _project_role_of_current_user(project_id)  # admin/editor/viewer
        eff = global_caps & PROJECT_ROLE_CAPS.get(proj_role, {"read"})
        if proj_role == "admin":
            eff |= {"manage_project"}
        return cap in eff
    return True                           # global action; the ceiling is enough
```

`_project_role_of_current_user` derives the project role by reading `participants[].rol_permiso` (or `admin` if `proj.userId == uid`). It reuses `has_project_access` so membership logic is not duplicated.

### 6.4 Apply the gate

```python
# agent/tools.py → execute_tool
def execute_tool(tool_name, params):
    if tool_name not in TOOL_MAP:
        return {"error": f"Unknown tool: {tool_name}"}
    if not can(tool_name, (params or {}).get("project_id", "")):
        return {"error": "permission_denied",
                "detail": f"Your role does not allow running {tool_name}."}
    ...
```

The narrator already knows how to present "no permission" errors nicely (`narrator/narrators/projects.py` already handles the "no permission" case). In the REST API, the same `can(...)` is invoked in each controller before calling the service, returning `403`.

### 6.5 Data schema

- **Users table**: add attribute `globalRole` (`owner | operator | collaborator | viewer`); default `viewer`.
- **`participants[]`** in each project: add `rol_permiso` (`admin | editor | viewer`); default `editor` for existing participants (see migration). The descriptive `rol` field is kept separately — the two are not mixed.

---

## 7. Migration and compatibility

To avoid breaking current users:

1. **Users without `globalRole`** → treat them as `owner` if they own at least one project, otherwise as `collaborator`. (Or an explicit backfill; team decision.)
2. **Project owner (`proj.userId`)** → always `Project Admin` of that project, without touching data: derived at runtime.
3. **Existing participants without `rol_permiso`** → default `editor` (current behavior: they could write). Anyone who wants to tighten this can drop them to `viewer` manually.
4. **Phased roll-out**: start in *log-only* mode (record what would have been denied, without blocking) for a few days to catch false positives before turning on real blocking.

---

## 8. Edge cases and risks

- **High-impact global action with no project** (`create_project`): controlled only by the global role; the Viewer cannot, the rest can. Correct.
- **Tools with optional `project_id`** (`send_notification`, `send_email`, `create_reminder`, `list_notifications`): if `project_id` isn't passed, they fall back to the global role. Watch that the planner doesn't omit `project_id` to sidestep the per-project filter — reinforce in `catalog.py` that external communication must always carry `project_id`.
- **The LLM does not decide permissions.** The gate lives in code (`execute_tool`), not in the prompt. The planner can *propose* a forbidden action; the executor *rejects* it. Same philosophy as `_has_project_access` today.
- **Contact PII** (`get_project_contacts` returns phone numbers/emails): that's why it is 🔒 (requires being at least Viewer of the project, not just having a generic read global role).
- **`manage_roles` concentrated on Owner**: prevents an Operator from self-promoting. If delegation is needed, add an intermediate global role in `ROLE_CAPS` without touching the rest of the design.
- **Fail-closed**: any new tool not added to `TOOL_CAP` stays blocked until it is classified. This is intentional.

---

## 9. Executive summary

- Today there is only project-level control (owner vs participant); the MCP agent applies no roles.
- The proposal introduces **4 capabilities** (`read`, `write_internal`, `send_external`, admin) and a **hybrid** model: 4 global roles (Owner, Operator, Collaborator, Viewer) that set the ceiling, plus 3 project roles (Admin, Editor, Viewer) that restrict within each project.
- The key risk line is `send_external` (sending WhatsApp/SMS/email): only Owner and Operator have it.
- Enforcement is concentrated in a single `can()` gate in `execute_tool`, reusing `has_project_access`, with a declarative capability table and a log-only roll-out.
