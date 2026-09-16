# OneBox — Role and function catalog

> Reference document for the permissions system. Formalized names + full inventory of functions (16 MCP agent actions + ~45 REST endpoints), classified by capability.
> Sources: `agent/tools.py`, `mcp/server.py`, `api/controllers/`, `api/services/access.py`.
> Status: design proposal (not implemented). **Hybrid** model: global role + per-project override.

---

## 1. Roles

### 1.1 Global roles (at the user / account level)

They set the **ceiling** on what the user can do across the whole system.

| Internal code | UI label | Description |
|---|---|---|
| `owner` | Owner | Account owner. Everything, including connecting/disconnecting Gmail, billing, and managing other users' roles. |
| `member` | Member | Operating team. Full day-to-day, **including sending external communication**. Default role on joining. |
| `restricted` | Restricted member | Organizes inside OneBox (projects, tasks, insights) but **cannot send anything to the outside**. For junior profiles, trial users or integrations. |
| `viewer` | Guest (read-only) | **Deferred to v2.** External client or stakeholder who only sees the state of their project. Do not enable in v1. |

### 1.2 Project roles (override within a project)

Stored in `participants[].rol_permiso` on each project. They refine what the user can do **inside that specific project**.

| Code | Label | Meaning |
|---|---|---|
| `admin` | Project admin | Administers the project: participants, invitations, deletion. (= today's `is_owner`) |
| `editor` | Editor | Creates/edits inside the project and, if their global role allows it, sends project communication. |
| `viewer` | Viewer | Read-only on the project. |

### 1.3 Combination rule (hybrid)

For an action **with `project_id`**:

```
allowed_capability =
    capabilities(global_role)          # ceiling: what they can do in general
  ∩ capabilities(project_role)         # floor: what is allowed here
  ∪ {manage_project} if project_role == admin   # only elevation, scoped to this project
```

- The **global role sets the ceiling**: a `restricted` user will never send externally, even if they are an `editor` in a project.
- The **project role restricts**: a `member` who is a `viewer` in Project X only reads there.
- Being **`admin` of a project** grants administration *of that project* even if the global role isn't `owner`. This is the only elevation allowed.

For **global** actions (no `project_id`), only the **global role** applies.

---

## 2. Capabilities

The actual unit that gets checked. Each function requires a capability; each role grants a set of capabilities.

| Capability | What it enables | Risk |
|---|---|---|
| `read` | Query and analyze. No writes or external output. | low |
| `write_internal` | Create/edit data inside OneBox (projects, tasks, insights, reminders, assignments, attachments, own profile). | medium |
| `send_external` | Send communication that goes out to the world: email, WhatsApp, SMS. **The red line.** | high |
| `manage_project` | Participants, invitations, project/attachment deletion. | high |
| `manage_account` | Connect/disconnect the data source (Gmail) — affects the whole account. | high |
| `manage_roles` | Assign the global role of other users and the per-project roles. | critical |

### Capabilities per global role

| Capability | `owner` | `member` | `restricted` | `viewer` |
|---|:---:|:---:|:---:|:---:|
| `read` | ✅ | ✅ | ✅ | ✅ |
| `write_internal` | ✅ | ✅ | ✅ | ❌ |
| `send_external` | ✅ | ✅ | ❌ | ❌ |
| `manage_project` | ✅ | 🔒¹ | ❌ | ❌ |
| `manage_account` | ✅ | ❌ | ❌ | ❌ |
| `manage_roles` | ✅ | ❌ | ❌ | ❌ |

¹ The `member` administers only projects where they are project `admin`.

---

## 3. Functions governed by roles

### 3.1 Agent actions (MCP) — 16

| Function | Capability | Scope |
|---|---|---|
| `list_emails` | read | global |
| `inspect_email` | read | global |
| `analyze_inbox` | read | global |
| `list_projects` | read | global |
| `list_notifications` | read | global / project |
| `get_project_contacts` | read (PII) | project |
| `check_sla` | read (analysis) | global |
| `proactive_summary` | read (analysis) | global |
| `auto_classify_messages` | read (suggest only) | global |
| `create_project` | write_internal | global |
| `assign_email_to_project` | write_internal | project |
| `create_insight` | write_internal | project |
| `create_task` | write_internal | project |
| `create_reminder` | write_internal | project |
| `send_email` | **send_external** | project |
| `send_notification` | **send_external** | project |

### 3.2 User REST endpoints

| Endpoint | Capability |
|---|---|
| `GET /api/projects` | read |
| `GET /api/projects/{id}` | read |
| `GET /api/projects/{id}/tasks` | read |
| `GET /api/projects/{id}/conversations` | read |
| `GET /api/insights` | read |
| `GET /api/inbox` | read |
| `GET /api/notifications` | read |
| `GET /api/projects/{id}/attachments` | read |
| `GET /api/attachments/{id}/download` | read |
| `GET /api/gmail/status` | read |
| `POST /api/text/analyze` | read (analysis) |
| `POST /api/documents/analyze` | read (analysis) |
| `POST /api/projects/from-document-draft` | read (preview, does not persist) |
| `POST /api/projects/{id}/analyze-text` | read (analysis) — *confirm it does not persist* |
| `POST /api/projects` | write_internal |
| `POST /api/projects/from-document` | write_internal |
| `POST /api/projects/from-text` | write_internal |
| `POST /api/projects/{id}/tasks` | write_internal |
| `PUT /api/tasks/{id}` | write_internal |
| `DELETE /api/tasks/{id}` | write_internal |
| `POST /api/inbox/{conversation_id}/assign` | write_internal |
| `POST /api/projects/{id}/attachments` | write_internal |
| `POST /api/user/phone` | write_internal (own profile) |
| `GET /api/user/phone` | read (own profile) |
| `GET /api/user/phones` | read (own profile) |
| `DELETE /api/user/phone` | write_internal (own profile) |
| `PUT /api/projects/{id}/participants` | manage_project |
| `POST /api/projects/{id}/invite` | manage_project |
| `DELETE /api/projects/{id}/participants` | manage_project |
| `DELETE /api/attachments/{id}` | manage_project |
| `DELETE /api/projects/{id}` | manage_project (destructive) |
| `GET /api/gmail/auth` | **manage_account** |
| `DELETE /api/gmail/disconnect` | **manage_account** |
| `POST /chat` | any authenticated user (the real filter is applied per-tool inside the agent) |

---

## 4. Functions NOT governed by roles (system / infrastructure)

These are called by machines (Google, Twilio, EventBridge), not by users. They are protected with secret/signature/origin verification, **not** with a user role.

| Endpoint | Who calls it |
|---|---|
| `GET /api/gmail/callback` | Google (OAuth redirect) |
| `POST /api/gmail/push-notification` | Google Pub/Sub |
| `POST /api/gmail/register-watch` | System (Gmail watch registration) |
| `POST /api/twilio/webhook` | Twilio (inbound message) |
| `POST /api/scheduled/gmail-sync` | EventBridge (cron) |
| `POST /api/scheduled/notifications` | EventBridge (cron) |
| `POST /api/scheduled/dispatch-pending` | EventBridge (cron) |
| `GET /health` | Load balancer / monitoring (public) |
| `DELETE /debug/cache/{session_id}` | Development |
| `POST /api/test-whatsapp` | Development / testing |

---

## 5. Summary matrix: global role × action

✅ allowed · ❌ denied · 🔒 allowed only if the project role also grants it (§1.3)

| Action / group | `owner` | `member` | `restricted` | `viewer` |
|---|:---:|:---:|:---:|:---:|
| Reads and analysis (all `read` ones) | ✅ | ✅ | ✅ | ✅ |
| `get_project_contacts` (PII) | ✅ | 🔒 | 🔒 | 🔒 |
| `create_project` / create projects via REST | ✅ | ✅ | ✅ | ❌ |
| Writes inside a project (tasks, insights, assign, reminders, attachments) | ✅ | 🔒 | 🔒 | ❌ |
| `send_email` / `send_notification` | ✅ | 🔒 | ❌ | ❌ |
| Administer project (participants, invite, delete) | ✅ | 🔒² | ❌ | ❌ |
| Connect/disconnect Gmail | ✅ | ❌ | ❌ | ❌ |
| Assign roles | ✅ | ❌ | ❌ | ❌ |

² Only in projects where the `member` is a project `admin`.

---

## 6. Notes and open questions

1. **`analyze-text` / `analyze`**: marked as `read` assuming they only return analysis without persisting. Confirm in the service; if they write into the project, reclassify to `write_internal`.
2. **Connecting Gmail = `manage_account`**: it affects the data source of the whole account, not of a single project; that's why it is reserved for the `owner`.
3. **`/chat` is not governed as a single capability**: any authenticated user can chat; the real control happens per-tool inside the agent (`execute_tool`). It is the single enforcement point on the agent side.
4. **Tools with optional `project_id`** (`send_notification`, `send_email`, `create_reminder`, `list_notifications`): if `project_id` is missing they fall back to the global role. Reinforce in `catalog.py` that external communication must always carry `project_id` so the per-project filter cannot be sidestepped.
5. **Fail-closed**: any new function that isn't classified here stays blocked until it is labeled.
