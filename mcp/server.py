#!/usr/bin/env python3
"""MCP server for testing the OneBox agent interactively (debug mode).

Available tools:
  onebox_chat     — send a message and keep multi-turn history
  onebox_reset    — reset a session
  onebox_history  — show the history of a session
  onebox_report   — generate a feedback report with analysis and catalog suggestions
  onebox_export   — save the report as a .md file

See mcp/README.md for installation and configuration instructions.
"""
import asyncio
import json
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

try:
    import httpx
    from mcp.server import Server
    from mcp.server.stdio import stdio_server
    from mcp import types
except ImportError as e:
    print(
        f"ERROR: missing dependency — {e}\n"
        "Install with:  pip install mcp httpx",
        file=sys.stderr,
    )
    sys.exit(1)

# ── Configuration ─────────────────────────────────────────────────────────────
BASE_URL   = os.getenv("ONEBOX_BASE_URL",   "http://localhost:8006")
USER_ID    = os.getenv("ONEBOX_USER_ID",    "debug-user-001")
USER_EMAIL = os.getenv("ONEBOX_USER_EMAIL", "debug@onebox.com")
REPORTS_DIR = Path(os.getenv("ONEBOX_REPORTS_DIR", Path(__file__).parent / "reports"))

DEFAULT_SESSION = "default"

# ── In-memory session state ───────────────────────────────────────────────────
# { session_id: { "history": [...], "turns_meta": [...] } }
_sessions: dict[str, dict] = {}

server = Server("onebox-chat")


# ══════════════════════════════════════════════════════════════════════════════
# TOOL DEFINITIONS
# ══════════════════════════════════════════════════════════════════════════════

@server.list_tools()
async def list_tools() -> list[types.Tool]:
    return [
        types.Tool(
            name="onebox_chat",
            description=(
                "Send a message to the OneBox agent in debug mode and receive its response. "
                "History is kept automatically per session, simulating real turns. "
                "Always use the same session_id within a conversation."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "message": {
                        "type": "string",
                        "description": "Message to send to the agent",
                    },
                    "session_id": {
                        "type": "string",
                        "description": "Session ID (use the same one across all turns of a conversation)",
                        "default": DEFAULT_SESSION,
                    },
                },
                "required": ["message"],
            },
        ),
        types.Tool(
            name="onebox_reset",
            description="Reset the history of a session to start a new conversation.",
            inputSchema={
                "type": "object",
                "properties": {
                    "session_id": {
                        "type": "string",
                        "description": "Session ID to reset",
                        "default": DEFAULT_SESSION,
                    },
                },
            },
        ),
        types.Tool(
            name="onebox_history",
            description="Show the message history of a session.",
            inputSchema={
                "type": "object",
                "properties": {
                    "session_id": {
                        "type": "string",
                        "description": "Session ID",
                        "default": DEFAULT_SESSION,
                    },
                },
            },
        ),
        types.Tool(
            name="onebox_report",
            description=(
                "Generate a full feedback report for the session: conversation, "
                "per-turn analysis (planner iterations, tools used, "
                "validation errors, replans) and concrete suggestions to improve catalog.py."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "session_id": {
                        "type": "string",
                        "description": "Session ID to analyze",
                        "default": DEFAULT_SESSION,
                    },
                },
            },
        ),
        types.Tool(
            name="onebox_export",
            description=(
                "Save the feedback report of a session as a .md file "
                "in the mcp/reports/ folder. Useful to share or review later."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "session_id": {
                        "type": "string",
                        "description": "Session ID to export",
                        "default": DEFAULT_SESSION,
                    },
                    "filename": {
                        "type": "string",
                        "description": "File name (without extension). Defaults to date and session.",
                    },
                },
            },
        ),
        types.Tool(
            name="onebox_from_text_preview",
            description=(
                "Calls POST /api/text/analyze with pasted text (WhatsApp conversation, email, notes). "
                "Returns the agent's preview in debug mode: detected participants, tasks with assigned_to "
                "and dates, without creating anything in the database. Useful to verify analysis quality."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "text": {
                        "type": "string",
                        "description": "Text or conversation to analyze",
                    },
                    "source": {
                        "type": "string",
                        "description": "Source of the text: 'whatsapp', 'email', 'notes', etc.",
                        "default": "whatsapp",
                    },
                },
                "required": ["text"],
            },
        ),
        types.Tool(
            name="onebox_from_text_create",
            description=(
                "Calls POST /api/projects/from-text with pasted text. "
                "Creates the REAL project in the database using the full agent: "
                "detects participants, creates tasks with assigned_to and dates. "
                "Returns the created projectId and the agent's response."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "text": {
                        "type": "string",
                        "description": "Text or conversation from which to create the project",
                    },
                    "name": {
                        "type": "string",
                        "description": "Project name (optional; the agent infers it if omitted)",
                    },
                },
                "required": ["text"],
            },
        ),
        types.Tool(
            name="onebox_notify_schedule",
            description=(
                "Test a scheduled ONE-TIME notification. "
                "Calls send_notification directly with scheduled_at. "
                "If TWILIO_MESSAGING_SERVICE_SID is configured, Twilio schedules it natively. "
                "Otherwise, it stays in DynamoDB with status=pending for the dispatcher to send. "
                "Use onebox_dispatch_pending to trigger it manually without waiting for EventBridge."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "recipient": {
                        "type": "string",
                        "description": "E.164 phone (e.g., +50494622817) or email if channel=email",
                    },
                    "message": {
                        "type": "string",
                        "description": "Message text",
                    },
                    "channel": {
                        "type": "string",
                        "description": "whatsapp | sms | email",
                        "default": "whatsapp",
                    },
                    "scheduled_at": {
                        "type": "string",
                        "description": "UTC ISO 8601 date/time. E.g.: '2026-06-19T09:00:00Z'. At least 15 min in the future for native Twilio.",
                    },
                    "project_id": {
                        "type": "string",
                        "description": "Related project ID (optional)",
                        "default": "",
                    },
                    "project_name": {
                        "type": "string",
                        "description": "Project name (optional)",
                        "default": "",
                    },
                },
                "required": ["recipient", "message", "scheduled_at"],
            },
        ),
        types.Tool(
            name="onebox_notify_recurring",
            description=(
                "Test a weekly RECURRING notification. "
                "Stores in DynamoDB with isRecurring=True and the configured days. "
                "Use onebox_dispatch_pending to simulate the EventBridge dispatcher running "
                "and verify the notification is sent on the correct days."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "recipient": {
                        "type": "string",
                        "description": "E.164 phone or email",
                    },
                    "message": {
                        "type": "string",
                        "description": "Recurring message text",
                    },
                    "channel": {
                        "type": "string",
                        "description": "whatsapp | sms | email",
                        "default": "whatsapp",
                    },
                    "recurring_days": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Days of the week: monday, tuesday, wednesday, thursday, friday, saturday, sunday",
                    },
                    "project_id": {
                        "type": "string",
                        "description": "Related project ID (optional)",
                        "default": "",
                    },
                    "project_name": {
                        "type": "string",
                        "description": "Project name (optional)",
                        "default": "",
                    },
                },
                "required": ["recipient", "message", "recurring_days"],
            },
        ),
        types.Tool(
            name="onebox_dispatch_pending",
            description=(
                "Manually trigger the scheduled notifications dispatcher. "
                "Equivalent to what EventBridge does every hour: calls POST /api/scheduled/dispatch-pending. "
                "Use it to verify that notifications with status=pending are sent correctly "
                "without having to wait for the AWS cron."
            ),
            inputSchema={
                "type": "object",
                "properties": {},
            },
        ),
        types.Tool(
            name="onebox_task",
            description=(
                "Test TASK actions in natural language: list, unblock, "
                "complete, reassign or delete. Exercises list_tasks, update_task and "
                "delete_task. Note: reassign and delete require CONFIRMATION → respond next "
                "with onebox_confirm in the same session."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "action":      {"type": "string", "enum": ["list", "unblock", "block", "complete", "reassign", "delete"]},
                    "project":    {"type": "string", "description": "Project name"},
                    "task":       {"type": "string", "description": "Task text (not required for 'list')"},
                    "assignee": {"type": "string", "description": "Only for 'reassign': new assignee"},
                    "session_id":  {"type": "string"},
                },
                "required": ["action", "project"],
            },
        ),
        types.Tool(
            name="onebox_project",
            description=(
                "Test project ADMINISTRATION in natural language: edit (name/description/"
                "status), invite someone, or remove a participant. Exercises update_project, "
                "invite_user and remove_participant. Invite and remove require CONFIRMATION → use "
                "onebox_confirm afterwards."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "action":     {"type": "string", "enum": ["edit", "invite", "remove"]},
                    "project":   {"type": "string", "description": "Project name"},
                    "detail":    {"type": "string", "description": "What to change / who to invite or remove (e.g., 'rename it to Alpha 2' or 'invite juan@x.com')"},
                    "session_id": {"type": "string"},
                },
                "required": ["action", "project", "detail"],
            },
        ),
        types.Tool(
            name="onebox_send",
            description=(
                "Test sending to a person by NAME or contact, with scheduling in NATURAL "
                "LANGUAGE. Exercises resolve_person + time calculation (schedule) + confirmation. "
                "E.g.: to='Jesus Vega', message='review the report', when='in two hours'. Sending requires "
                "CONFIRMATION → use onebox_confirm afterwards."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "to":         {"type": "string", "description": "Person name, or email/phone"},
                    "message":    {"type": "string"},
                    "channel":      {"type": "string", "enum": ["whatsapp", "sms", "email"], "default": "whatsapp"},
                    "when":       {"type": "string", "description": "Optional, natural language: 'in two hours', 'at 1pm', 'tomorrow at 9', 'every monday'. Empty = send now."},
                    "session_id": {"type": "string"},
                },
                "required": ["to", "message"],
            },
        ),
        types.Tool(
            name="onebox_confirm",
            description=(
                "Continue a flow that asked for CONFIRMATION by responding yes/no in the SAME session. "
                "Use it after onebox_task/onebox_project/onebox_send when the agent asked to confirm."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "session_id": {"type": "string", "description": "The same session as the previous step"},
                    "confirm":  {"type": "boolean", "default": True, "description": "true = yes; false = cancel"},
                },
                "required": ["session_id"],
            },
        ),
        types.Tool(
            name="onebox_onboarding",
            description=(
                "Test the REAL onboarding flow from text: runs the preview "
                "(/api/text/analyze) and then creates the project from the draft "
                "(/api/projects/from-document-draft), which now analyzes the FULL TEXT "
                "to generate deep insights. WARNING: creates REAL DATA (project + insights "
                "in DynamoDB), it is NOT dry-run. Returns the created project and its insights so you can "
                "inspect depth (summary, real type, profile, key insight, counts)."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "text":     {"type": "string", "description": "Text/transcript to analyze (conversation, brief, minutes)."},
                    "name":     {"type": "string", "description": "Optional: force the project name (otherwise uses the one suggested by the AI)."},
                    "channels": {"type": "array", "items": {"type": "string"}, "description": "Project channels. Default ['Gmail']."},
                },
                "required": ["text"],
            },
        ),
        types.Tool(
            name="onebox_onboarding_dryrun",
            description=(
                "DRY-RUN of the onboarding analysis: runs the REAL AI on the FULL TEXT "
                "to see the quality/depth of the insights, but does NOT create a project nor "
                "write ANYTHING to DynamoDB. Ideal to verify capabilities without polluting data. "
                "Returns summary, real type, client profile, key insight, and the lists of "
                "tasks/work done/blockers/risks/decisions/metrics/technical issues."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "text": {"type": "string", "description": "Text/transcript to analyze (conversation, brief, minutes)."},
                },
                "required": ["text"],
            },
        ),
    ]


# ══════════════════════════════════════════════════════════════════════════════
# DISPATCH
# ══════════════════════════════════════════════════════════════════════════════

@server.call_tool()
async def call_tool(name: str, arguments: dict[str, Any]) -> list[types.TextContent]:
    handlers = {
        "onebox_chat":              _handle_chat,
        "onebox_reset":             _handle_reset,
        "onebox_history":           _handle_history,
        "onebox_report":            _handle_report,
        "onebox_export":            _handle_export,
        "onebox_from_text_preview": _handle_from_text_preview,
        "onebox_from_text_create":  _handle_from_text_create,
        "onebox_notify_schedule":   _handle_notify_schedule,
        "onebox_notify_recurring":  _handle_notify_recurring,
        "onebox_dispatch_pending":  _handle_dispatch_pending,
        "onebox_task":              _handle_task,
        "onebox_project":           _handle_project,
        "onebox_send":              _handle_send,
        "onebox_confirm":           _handle_confirm,
        "onebox_onboarding":        _handle_onboarding,
        "onebox_onboarding_dryrun": _handle_onboarding_dryrun,
    }
    handler = handlers.get(name)
    if not handler:
        return [types.TextContent(type="text", text=f"Unknown tool: {name}")]
    if asyncio.iscoroutinefunction(handler):
        return await handler(arguments)
    return handler(arguments)


# ══════════════════════════════════════════════════════════════════════════════
# HANDLERS
# ══════════════════════════════════════════════════════════════════════════════

async def _handle_chat(args: dict) -> list[types.TextContent]:
    message    = (args.get("message") or "").strip()
    session_id = args.get("session_id") or DEFAULT_SESSION

    if not message:
        return [types.TextContent(type="text", text="⚠️ The message cannot be empty.")]

    session  = _sessions.setdefault(session_id, {"history": [], "turns_meta": []})
    history  = session["history"]

    payload = {"message": message, "history": history, "debug": True}
    headers = {
        "x-user-id":    USER_ID,
        "x-user-email": USER_EMAIL,
        "Content-Type": "application/json",
    }

    try:
        async with httpx.AsyncClient(timeout=90.0) as client:
            resp = await client.post(f"{BASE_URL}/chat", json=payload, headers=headers)
            resp.raise_for_status()
            data = resp.json()
    except httpx.ConnectError:
        return [types.TextContent(
            type="text",
            text=f"❌ Could not connect to {BASE_URL}\nIs the server running? → `uvicorn main:app --reload`",
        )]
    except httpx.HTTPStatusError as e:
        return [types.TextContent(
            type="text",
            text=f"❌ HTTP error {e.response.status_code}: {e.response.text[:400]}",
        )]
    except Exception as e:
        return [types.TextContent(type="text", text=f"❌ Unexpected error: {e}")]

    agent_response = data.get("response", "")
    tools_used     = data.get("toolsUsed", [])
    debug_info     = data.get("debug_info") or {}

    # Accumulate history
    history.append({"role": "user",      "content": message})
    history.append({"role": "assistant", "content": agent_response})

    # Save turn metadata for the report
    session["turns_meta"].append({
        "turn":       len(session["turns_meta"]) + 1,
        "message":    message,
        "response":   agent_response,
        "tools_used": tools_used,
        "debug_info": debug_info,
        "timestamp":  datetime.now().isoformat(),
    })

    # ── Format response ───────────────────────────────────────────────────
    lines = [f"🤖 **Agent:** {agent_response}"]

    if tools_used:
        lines.append(f"\n🔧 **Tools:** `{'`, `'.join(tools_used)}`")

    decision  = debug_info.get("planner_decision", "")
    iteration = debug_info.get("iteration", 1)
    plan      = debug_info.get("plan", [])

    if decision:
        iter_warn = " ⚠️" if iteration > 1 else ""
        lines.append(f"📊 **Planner:** `{decision}` | iterations: **{iteration}**{iter_warn}")

    if plan:
        steps = [f"  {s['step']}. `{s['tool']}`" for s in plan]
        lines.append("📋 **Plan:**\n" + "\n".join(steps))

    # Simulated steps (dry-run): debug JSON with resolved params and simulated
    # result for each step. Here you can see, e.g., the scheduled_at computed by
    # 'schedule', the contact resolved by resolve_person, or a validation/
    # scheduling error before touching the real database.
    sim_calls = debug_info.get("simulated_calls", [])
    if sim_calls:
        sim_lines = ["🧪 **Simulated steps (dry-run):**"]
        for c in sim_calls:
            params = json.dumps(c.get("params", {}), ensure_ascii=False, default=str)
            result = json.dumps(c.get("simulated_result", {}), ensure_ascii=False, default=str)
            sim_lines.append(f"  {c.get('step')}. `{c.get('tool')}`")
            sim_lines.append(f"     params: `{params[:400]}`")
            sim_lines.append(f"     → result: `{result[:300]}`")
        lines.append("\n".join(sim_lines))

    turn = len(history) // 2
    lines.append(f"\n*(turn {turn} · session `{session_id}` · debug=true)*")

    return [types.TextContent(type="text", text="\n".join(lines))]


def _handle_reset(args: dict) -> list[types.TextContent]:
    session_id = args.get("session_id") or DEFAULT_SESSION
    prev = _sessions.get(session_id, {})
    prev_turns = len(prev.get("turns_meta", []))
    _sessions[session_id] = {"history": [], "turns_meta": []}
    return [types.TextContent(
        type="text",
        text=f"✅ Session `{session_id}` reset. ({prev_turns} previous turns cleared)",
    )]


def _handle_history(args: dict) -> list[types.TextContent]:
    session_id = args.get("session_id") or DEFAULT_SESSION
    session    = _sessions.get(session_id, {})
    history    = session.get("history", [])

    if not history:
        return [types.TextContent(type="text", text=f"Session `{session_id}` has no history yet.")]

    lines = [f"📜 **History · session `{session_id}`** ({len(history) // 2} turns)\n"]
    for msg in history:
        icon    = "👤" if msg["role"] == "user" else "🤖"
        content = (msg.get("content") or "")[:500]
        lines.append(f"{icon} {content}\n")

    return [types.TextContent(type="text", text="\n".join(lines))]


def _handle_report(args: dict) -> list[types.TextContent]:
    session_id = args.get("session_id") or DEFAULT_SESSION
    report_md  = _build_report(session_id)
    return [types.TextContent(type="text", text=report_md)]


def _handle_export(args: dict) -> list[types.TextContent]:
    session_id = args.get("session_id") or DEFAULT_SESSION
    filename   = args.get("filename") or f"report_{session_id}_{datetime.now().strftime('%Y%m%d_%H%M%S')}"

    if not filename.endswith(".md"):
        filename += ".md"

    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    output_path = REPORTS_DIR / filename

    report_md = _build_report(session_id)
    output_path.write_text(report_md, encoding="utf-8")

    return [types.TextContent(
        type="text",
        text=f"✅ Report saved to:\n`{output_path}`",
    )]


# ══════════════════════════════════════════════════════════════════════════════
# REPORT GENERATION
# ══════════════════════════════════════════════════════════════════════════════

def _build_report(session_id: str) -> str:
    session    = _sessions.get(session_id, {})
    turns_meta = session.get("turns_meta", [])

    if not turns_meta:
        return f"⚠️ Session `{session_id}` has no recorded turns."

    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    lines = [
        f"# Feedback Report — OneBox Agent",
        f"**Session:** `{session_id}` · **Date:** {now} · **Turns:** {len(turns_meta)}",
        "",
    ]

    # ── 1. Full conversation ──────────────────────────────────────────────
    lines += ["## 1. Full conversation", ""]
    for meta in turns_meta:
        lines += [
            f"### Turn {meta['turn']}",
            f"**👤 User:** {meta['message']}",
            "",
            f"**🤖 Agent:** {meta['response']}",
            "",
        ]

    # ── 2. Per-turn analysis ──────────────────────────────────────────────
    lines += ["---", "## 2. Per-turn analysis", ""]
    issues_found = []

    for meta in turns_meta:
        t          = meta["turn"]
        di         = meta.get("debug_info") or {}
        tools      = meta.get("tools_used", [])
        iteration  = di.get("iteration", 1)
        decision   = di.get("planner_decision", "N/A")
        plan       = di.get("plan", [])
        sim_calls  = di.get("simulated_calls", [])

        lines.append(f"#### Turn {t}: _{meta['message'][:80]}_")
        lines.append(f"- **Planner decision:** `{decision}`")
        lines.append(f"- **Iterations:** {iteration}" + (" ⚠️ multiple iterations" if iteration > 1 else ""))
        lines.append(f"- **Tools executed:** {', '.join(f'`{t}`' for t in tools) if tools else 'none'}")

        if plan:
            plan_str = " → ".join(f"`{s['tool']}`" for s in plan)
            lines.append(f"- **Generated plan:** {plan_str}")

        # Detect validation errors in simulated_calls
        val_errors = [
            c for c in sim_calls
            if isinstance(c.get("simulated_result"), dict)
            and c["simulated_result"].get("_validation_error")
        ]
        if val_errors:
            for ve in val_errors:
                err_msg = ve["simulated_result"].get("error", "")[:200]
                lines.append(f"- **❌ Validation error at step {ve['step']} (`{ve['tool']}`):** {err_msg}")
                issues_found.append({
                    "turn": t,
                    "type": "validation_error",
                    "tool": ve["tool"],
                    "detail": err_msg,
                })

        # Detect high iterations
        if iteration > 1:
            issues_found.append({
                "turn": t,
                "type": "high_iterations",
                "detail": f"The planner needed {iteration} iterations for the message: '{meta['message'][:60]}'",
            })

        lines.append("")

    # ── 3. Issues detected ────────────────────────────────────────────────
    lines += ["---", "## 3. Issues detected", ""]

    if not issues_found:
        lines.append("✅ No issues detected in this session.")
    else:
        for issue in issues_found:
            if issue["type"] == "high_iterations":
                lines.append(f"- ⚠️ **Turn {issue['turn']} — Multiple iterations:** {issue['detail']}")
            elif issue["type"] == "validation_error":
                lines.append(f"- ❌ **Turn {issue['turn']} — Validation failed in `{issue['tool']}`:** {issue['detail']}")
    lines.append("")

    # ── 4. Catalog improvement suggestions ────────────────────────────────
    lines += ["---", "## 4. Suggestions to improve catalog.py", ""]

    suggestions = _generate_catalog_suggestions(issues_found, turns_meta)
    if not suggestions:
        lines.append("✅ No suggestions generated — the session was correct.")
    else:
        for s in suggestions:
            lines.append(f"### {s['title']}")
            lines.append(s["body"])
            lines.append("")

    # ── 5. Raw training data ──────────────────────────────────────────────
    lines += ["---", "## 5. Training data (JSON)", "", "```json"]
    training_data = [
        {
            "turn": m["turn"],
            "message": m["message"],
            "response": m["response"],
            "tools_used": m["tools_used"],
            "planner_decision": (m.get("debug_info") or {}).get("planner_decision"),
            "iterations": (m.get("debug_info") or {}).get("iteration", 1),
            "plan": (m.get("debug_info") or {}).get("plan", []),
        }
        for m in turns_meta
    ]
    lines.append(json.dumps(training_data, ensure_ascii=False, indent=2))
    lines.append("```")

    return "\n".join(lines)


def _generate_catalog_suggestions(issues: list[dict], turns_meta: list[dict]) -> list[dict]:
    suggestions = []
    seen = set()

    for issue in issues:
        key = (issue["type"], issue.get("tool", ""))
        if key in seen:
            continue
        seen.add(key)

        if issue["type"] == "high_iterations":
            # Look up the turn's message for context
            turn_data = next((m for m in turns_meta if m["turn"] == issue["turn"]), {})
            msg = turn_data.get("message", "")
            suggestions.append({
                "title": f"⚠️ Turn {issue['turn']}: Planner took multiple iterations",
                "body": (
                    f"**Message:** _{msg}_\n\n"
                    "**Possible cause:** The rule in `REQUIRED_PARAMS` is not explicit enough "
                    "or is missing an ❌/✅ contrast example.\n\n"
                    "**Suggested action:** Add an INCORRECT vs CORRECT example in the section "
                    "of `catalog.py` corresponding to the action type of this turn."
                ),
            })

        elif issue["type"] == "validation_error":
            tool = issue.get("tool", "")
            suggestions.append({
                "title": f"❌ Validation failed in `{tool}` — reinforce example in catalog",
                "body": (
                    f"**Detected error:** {issue['detail'][:200]}\n\n"
                    f"**Suggested action:** In `MULTISTEP_RECIPES` or `REQUIRED_PARAMS`, "
                    f"add an explicit example of how the invalid parameter must be resolved "
                    f"in `{tool}`. Use the ❌ INCORRECT / ✅ CORRECT pattern."
                ),
            })

    return suggestions


async def _handle_from_text_preview(args: dict) -> list[types.TextContent]:
    text   = (args.get("text") or "").strip()
    source = args.get("source") or "whatsapp"

    if not text:
        return [types.TextContent(type="text", text="⚠️ The text cannot be empty.")]

    payload = {"text": text, "source": source}
    headers = {"x-user-id": USER_ID, "x-user-email": USER_EMAIL, "Content-Type": "application/json"}

    try:
        async with httpx.AsyncClient(timeout=120.0) as client:
            resp = await client.post(f"{BASE_URL}/api/text/analyze", json=payload, headers=headers)
            resp.raise_for_status()
            data = resp.json()
    except httpx.ConnectError:
        return [types.TextContent(type="text", text=f"❌ Could not connect to {BASE_URL}. Is the server running?")]
    except httpx.HTTPStatusError as e:
        return [types.TextContent(type="text", text=f"❌ HTTP error {e.response.status_code}: {e.response.text[:400]}")]
    except Exception as e:
        return [types.TextContent(type="text", text=f"❌ Error: {e}")]

    suggestion   = data.get("suggestion", {})
    participants = suggestion.get("detected_participants", [])
    tasks        = suggestion.get("tasks", [])

    lines = [
        f"## 📋 Project preview (not saved)",
        f"**Name:** {suggestion.get('name', '—')}",
        f"**Type:** {suggestion.get('type', '—')}",
        f"**Description:** {suggestion.get('description', '—')[:300]}",
        f"**Draft ID:** `{data.get('draftId', '—')}`",
        "",
    ]

    if participants:
        lines.append(f"### 👥 Detected participants ({len(participants)}):")
        for p in participants:
            lines.append(f"  • **{p.get('name', '?')}** — {p.get('role', '')} {('📧 ' + p.get('email','')) if p.get('email') else ''}")
    else:
        lines.append("👥 No participants detected.")

    lines.append("")

    if tasks:
        lines.append(f"### ✅ Detected tasks ({len(tasks)}):")
        for t in tasks:
            assigned = f" → **{t['assigned_to']}**" if t.get('assigned_to') else ""
            due      = f" (by {t['due_date']})" if t.get('due_date') else ""
            lines.append(f"  • {t.get('text', '?')}{assigned}{due}")
    else:
        lines.append("✅ No tasks detected.")

    if data.get("agentResponse"):
        lines += ["", f"🤖 **Agent response:** {data['agentResponse'][:400]}"]

    return [types.TextContent(type="text", text="\n".join(lines))]


async def _handle_from_text_create(args: dict) -> list[types.TextContent]:
    text = (args.get("text") or "").strip()
    name = (args.get("name") or "").strip()

    if not text:
        return [types.TextContent(type="text", text="⚠️ The text cannot be empty.")]

    payload = {"text": text, "name": name or None, "channels": ["WhatsApp"], "source": "whatsapp"}
    headers = {"x-user-id": USER_ID, "x-user-email": USER_EMAIL, "Content-Type": "application/json"}

    try:
        async with httpx.AsyncClient(timeout=120.0) as client:
            resp = await client.post(f"{BASE_URL}/api/projects/from-text", json=payload, headers=headers)
            resp.raise_for_status()
            data = resp.json()
    except httpx.ConnectError:
        return [types.TextContent(type="text", text=f"❌ Could not connect to {BASE_URL}. Is the server running?")]
    except httpx.HTTPStatusError as e:
        return [types.TextContent(type="text", text=f"❌ HTTP error {e.response.status_code}: {e.response.text[:400]}")]
    except Exception as e:
        return [types.TextContent(type="text", text=f"❌ Error: {e}")]

    lines = [
        f"## ✅ Project created",
        f"**Name:** {data.get('name', '—')}",
        f"**Project ID:** `{data.get('projectId', '—')}`",
        f"**Tools used:** {', '.join(data.get('tools_used', []))}",
        "",
        f"🤖 **Agent response:**",
        data.get("response", "—"),
    ]

    return [types.TextContent(type="text", text="\n".join(lines))]


# ══════════════════════════════════════════════════════════════════════════════
# HANDLERS — SCHEDULED NOTIFICATIONS
# ══════════════════════════════════════════════════════════════════════════════

async def _handle_notify_schedule(args: dict) -> list[types.TextContent]:
    """Sends a one-time notification with scheduled_at directly to the agent."""
    recipient = (args.get("recipient") or "").strip()
    message      = (args.get("message") or "").strip()
    channel        = (args.get("channel") or "whatsapp").strip()
    scheduled_at = (args.get("scheduled_at") or "").strip()
    project_id   = (args.get("project_id") or "").strip()
    project_name = (args.get("project_name") or "").strip()

    if not recipient or not message or not scheduled_at:
        return [types.TextContent(type="text", text="⚠️ recipient, message and scheduled_at are required.")]

    # We call the agent's chat with a structured message so it uses send_notification
    prompt = (
        f"Schedule a notification for {scheduled_at} UTC. "
        f"Channel: {channel}. "
        f"Recipient: {recipient}. "
        f"Message: {message}."
        + (f" Project ID: {project_id}, name: {project_name}." if project_id else "")
    )

    session_id = f"notify_schedule_{datetime.now().strftime('%H%M%S')}"
    session = _sessions.setdefault(session_id, {"history": [], "turns_meta": []})

    payload = {"message": prompt, "history": [], "debug": True}
    headers = {
        "x-user-id":    USER_ID,
        "x-user-email": USER_EMAIL,
        "Content-Type": "application/json",
    }

    try:
        async with httpx.AsyncClient(timeout=90.0) as client:
            resp = await client.post(f"{BASE_URL}/chat", json=payload, headers=headers)
            resp.raise_for_status()
            data = resp.json()
    except httpx.ConnectError:
        return [types.TextContent(type="text", text=f"❌ Could not connect to {BASE_URL}")]
    except httpx.HTTPStatusError as e:
        return [types.TextContent(type="text", text=f"❌ HTTP error {e.response.status_code}: {e.response.text[:400]}")]
    except Exception as e:
        return [types.TextContent(type="text", text=f"❌ Error: {e}")]

    debug_info = data.get("debug_info") or {}
    plan       = debug_info.get("plan", [])
    tools_used = data.get("toolsUsed", [])

    lines = [
        "## 📅 Scheduled notification (one-time)",
        f"**Recipient:** `{recipient}`",
        f"**Channel:** `{channel}`",
        f"**Scheduled at:** `{scheduled_at}`",
        f"**Message:** {message}",
        "",
        f"🤖 **Agent response:** {data.get('response', '')}",
    ]
    if tools_used:
        lines.append(f"🔧 **Tools:** `{'`, `'.join(tools_used)}`")
    if plan:
        steps = " → ".join(f"`{s['tool']}`" for s in plan)
        lines.append(f"📋 **Executed plan:** {steps}")
    lines += [
        "",
        "💡 **Tip:** Use `onebox_dispatch_pending` to verify that the dispatcher processes it correctly.",
    ]

    return [types.TextContent(type="text", text="\n".join(lines))]


async def _handle_notify_recurring(args: dict) -> list[types.TextContent]:
    """Creates a recurring notification with recurring_days."""
    recipient   = (args.get("recipient") or "").strip()
    message        = (args.get("message") or "").strip()
    channel          = (args.get("channel") or "whatsapp").strip()
    recurring_days = args.get("recurring_days") or []
    project_id     = (args.get("project_id") or "").strip()
    project_name   = (args.get("project_name") or "").strip()

    if not recipient or not message or not recurring_days:
        return [types.TextContent(type="text", text="⚠️ recipient, message and recurring_days are required.")]

    days_str = ", ".join(recurring_days)
    prompt = (
        f"Set up a weekly recurring notification. "
        f"Channel: {channel}. "
        f"Recipient: {recipient}. "
        f"Days: {days_str}. "
        f"Message: {message}."
        + (f" Project ID: {project_id}, name: {project_name}." if project_id else "")
    )

    payload = {"message": prompt, "history": [], "debug": True}
    headers = {
        "x-user-id":    USER_ID,
        "x-user-email": USER_EMAIL,
        "Content-Type": "application/json",
    }

    try:
        async with httpx.AsyncClient(timeout=90.0) as client:
            resp = await client.post(f"{BASE_URL}/chat", json=payload, headers=headers)
            resp.raise_for_status()
            data = resp.json()
    except httpx.ConnectError:
        return [types.TextContent(type="text", text=f"❌ Could not connect to {BASE_URL}")]
    except httpx.HTTPStatusError as e:
        return [types.TextContent(type="text", text=f"❌ HTTP error {e.response.status_code}: {e.response.text[:400]}")]
    except Exception as e:
        return [types.TextContent(type="text", text=f"❌ Error: {e}")]

    debug_info = data.get("debug_info") or {}
    tools_used = data.get("toolsUsed", [])
    plan       = debug_info.get("plan", [])

    lines = [
        "## 🔁 Recurring notification created",
        f"**Recipient:** `{recipient}`",
        f"**Channel:** `{channel}`",
        f"**Days:** {days_str}",
        f"**Message:** {message}",
        "",
        f"🤖 **Agent response:** {data.get('response', '')}",
    ]
    if tools_used:
        lines.append(f"🔧 **Tools:** `{'`, `'.join(tools_used)}`")
    if plan:
        steps = " → ".join(f"`{s['tool']}`" for s in plan)
        lines.append(f"📋 **Executed plan:** {steps}")
    lines += [
        "",
        "💡 **Tip:** Use `onebox_dispatch_pending` to simulate the EventBridge cron running right now.",
        f"   If today is one of [{days_str}], the notification will be sent immediately.",
    ]

    return [types.TextContent(type="text", text="\n".join(lines))]


async def _handle_dispatch_pending(args: dict) -> list[types.TextContent]:
    """Manually triggers the /api/scheduled/dispatch-pending endpoint."""
    headers = {
        "x-user-id":    USER_ID,
        "x-user-email": USER_EMAIL,
        "Content-Type": "application/json",
    }

    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.post(f"{BASE_URL}/api/scheduled/dispatch-pending", headers=headers)
            resp.raise_for_status()
            data = resp.json()
    except httpx.ConnectError:
        return [types.TextContent(type="text", text=f"❌ Could not connect to {BASE_URL}")]
    except httpx.HTTPStatusError as e:
        return [types.TextContent(type="text", text=f"❌ HTTP error {e.response.status_code}: {e.response.text[:400]}")]
    except Exception as e:
        return [types.TextContent(type="text", text=f"❌ Error: {e}")]

    evaluated = data.get("evaluated", 0)
    sent      = data.get("sent", 0)
    skipped   = data.get("skipped", 0)
    errors    = data.get("errors") or []

    status_icon = "✅" if not errors else "⚠️"
    lines = [
        f"## {status_icon} Dispatcher executed",
        f"**Notifications evaluated:** {evaluated}",
        f"**Sent:** {sent}",
        f"**Skipped** (not its hour/day, or already sent today): {skipped}",
    ]

    if errors:
        lines.append(f"\n**❌ Errors ({len(errors)}):**")
        for err in errors[:10]:
            lines.append(f"  - {err}")

    if sent == 0 and evaluated > 0:
        lines += [
            "",
            "ℹ️ All notifications were skipped. Possible reasons:",
            "  - Recurring notifications don't apply today",
            "  - One-time notifications have not yet reached their scheduled_at",
            "  - Recurring ones were already sent today (lastSentAt is today)",
        ]
    elif sent == 0 and evaluated == 0:
        lines.append("\nℹ️ No pending notifications in DynamoDB.")

    return [types.TextContent(type="text", text="\n".join(lines))]


# ══════════════════════════════════════════════════════════════════════════════
# NEW FLOW HANDLERS (delegate to _handle_chat → reuse session,
# history, plan and simulated steps). They build a natural-language prompt
# to force the flow that we want to test.
# ══════════════════════════════════════════════════════════════════════════════

async def _handle_task(args: dict) -> list[types.TextContent]:
    action      = (args.get("action") or "").strip().lower()
    project    = (args.get("project") or "").strip()
    task       = (args.get("task") or "").strip()
    assignee = (args.get("assignee") or "").strip()
    session_id  = args.get("session_id") or f"task_{datetime.now().strftime('%H%M%S')}"

    if action in ("unblock", "block", "complete", "reassign", "delete") and not task:
        return [types.TextContent(type="text", text="⚠️ 'task' is required for this action.")]
    if action == "reassign" and not assignee:
        return [types.TextContent(type="text", text="⚠️ 'assignee' is required to reassign.")]

    prompts = {
        "list":      f"show me the tasks of the {project} project",
        "unblock": f"the task '{task}' of the {project} project is no longer blocked",
        "block":    f"mark the task '{task}' of the {project} project as blocked",
        "complete":   f"mark the task '{task}' of the {project} project as done",
        "reassign":   f"reassign the task '{task}' of the {project} project to {assignee}",
        "delete":    f"delete the task '{task}' of the {project} project",
    }
    prompt = prompts.get(action)
    if not prompt:
        return [types.TextContent(type="text", text=f"⚠️ unknown action: '{action}'")]
    return await _handle_chat({"message": prompt, "session_id": session_id})


async def _handle_project(args: dict) -> list[types.TextContent]:
    action     = (args.get("action") or "").strip().lower()
    project   = (args.get("project") or "").strip()
    detail    = (args.get("detail") or "").strip()
    session_id = args.get("session_id") or f"project_{datetime.now().strftime('%H%M%S')}"

    prompts = {
        "edit":  f"in the {project} project: {detail}",
        "invite": f"invite {detail} to the {project} project",
        "remove":  f"remove {detail} from the {project} project",
    }
    prompt = prompts.get(action)
    if not prompt:
        return [types.TextContent(type="text", text=f"⚠️ unknown action: '{action}'")]
    return await _handle_chat({"message": prompt, "session_id": session_id})


async def _handle_send(args: dict) -> list[types.TextContent]:
    to         = (args.get("to") or "").strip()
    message    = (args.get("message") or "").strip()
    channel      = (args.get("channel") or "whatsapp").strip().lower()
    when     = (args.get("when") or "").strip()
    session_id = args.get("session_id") or f"send_{datetime.now().strftime('%H%M%S')}"

    if not to or not message:
        return [types.TextContent(type="text", text="⚠️ 'to' and 'message' are required.")]

    base = f"send an email to {to}" if channel == "email" else f"send a {channel} to {to}"
    prompt = f"{base} saying: \"{message}\""
    if when:
        prompt += f", {when}"
    return await _handle_chat({"message": prompt, "session_id": session_id})


async def _handle_confirm(args: dict) -> list[types.TextContent]:
    session_id = args.get("session_id")
    if not session_id:
        return [types.TextContent(type="text", text="⚠️ 'session_id' is required (the same session as the previous step).")]
    confirm = args.get("confirm", True)
    msg = "yes, confirm it" if confirm else "no, cancel"
    return await _handle_chat({"message": msg, "session_id": session_id})


async def _handle_onboarding(args: dict) -> list[types.TextContent]:
    """REAL onboarding flow: preview → create-from-draft (analyzes the full text).
    Creates real data and returns the insights so you can inspect their depth."""
    text     = (args.get("text") or "").strip()
    channels = args.get("channels") or ["Gmail"]
    if not text:
        return [types.TextContent(type="text", text="⚠️ 'text' cannot be empty.")]

    headers = {"x-user-id": USER_ID, "x-user-email": USER_EMAIL, "Content-Type": "application/json"}

    try:
        async with httpx.AsyncClient(timeout=180.0) as client:
            # 1) Preview → draftId + suggestion
            pr = await client.post(f"{BASE_URL}/api/text/analyze",
                                   json={"text": text, "source": "paste"}, headers=headers)
            pr.raise_for_status()
            preview = pr.json()
            sug = preview.get("suggestion", {}) or {}
            draft_id = preview.get("draftId", "")
            if not draft_id:
                return [types.TextContent(type="text", text=f"❌ The preview did not return a draftId. {str(preview)[:400]}")]

            det = [
                {"name": p.get("name", ""), "email": p.get("email", ""),
                 "phone": p.get("phone", ""), "role": p.get("role", "")}
                for p in (sug.get("detected_participants") or [])
            ]
            draft_payload = {
                "draftId": draft_id,
                "name": args.get("name") or sug.get("name") or "Project without name",
                "type": sug.get("type", "Other"),
                "description": sug.get("description", ""),
                "sourceText": text,            # the backend also retrieves it from S3
                "channels": channels,
                "detectedParticipants": det,
            }
            # 2) Create from draft → route with FULL-TEXT analysis
            cr = await client.post(f"{BASE_URL}/api/projects/from-document-draft",
                                   json=draft_payload, headers=headers)
            cr.raise_for_status()
            created = cr.json()
            project_id = created.get("projectId", "")

            # 3) Fetch the project's insights to inspect depth
            insights = []
            try:
                ir = await client.get(f"{BASE_URL}/api/insights", headers=headers)
                if ir.status_code == 200:
                    insights = [i for i in ir.json() if i.get("projectId") == project_id]
            except Exception:
                pass
    except httpx.ConnectError:
        return [types.TextContent(type="text", text=f"❌ Could not connect to {BASE_URL}. Is the server running?")]
    except httpx.HTTPStatusError as e:
        return [types.TextContent(type="text", text=f"❌ HTTP error {e.response.status_code}: {e.response.text[:500]}")]
    except Exception as e:
        return [types.TextContent(type="text", text=f"❌ Error: {e}")]

    by_type: dict = {}
    for i in insights:
        by_type.setdefault(i.get("type", "?"), []).append(i)

    def _titles(t, n=6):
        return [(x.get("title") or x.get("detected") or "")[:200] for x in by_type.get(t, [])[:n]]

    lines = [
        "## 🚀 Onboarding (REAL flow — creates data)",
        f"**Project created:** {created.get('name', '—')}  (`{project_id}`)",
        f"**Insights generated:** {(created.get('insightsGenerated') or {}).get('count', '—')}  ·  retrieved from the project: {len(insights)}",
        "",
    ]
    summ = by_type.get("summary", [])
    if summ:
        lines += ["### 📊 Summary", (summ[0].get("description") or summ[0].get("action") or "")[:900], ""]
    for t, label in [("project_characterization", "🎯 Real type"),
                     ("client_profile", "👤 Client profile"),
                     ("key_insight", "💡 Key insight")]:
        vals = _titles(t)
        if vals:
            lines.append(f"**{label}:** " + " | ".join(vals))
    if insights:
        counts = ", ".join(f"{t}={len(v)}" for t, v in sorted(by_type.items()))
        lines += ["", f"### 📈 Count by type: {counts}"]
    return [types.TextContent(type="text", text="\n".join(lines))]


async def _handle_onboarding_dryrun(args: dict) -> list[types.TextContent]:
    """DRY-RUN: runs the AI analysis on the full text WITHOUT creating anything."""
    text = (args.get("text") or "").strip()
    if not text:
        return [types.TextContent(type="text", text="⚠️ 'text' cannot be empty.")]

    headers = {"x-user-id": USER_ID, "x-user-email": USER_EMAIL, "Content-Type": "application/json"}
    try:
        async with httpx.AsyncClient(timeout=180.0) as client:
            r = await client.post(f"{BASE_URL}/api/text/analyze-insights-dryrun",
                                  json={"text": text}, headers=headers)
            r.raise_for_status()
            data = r.json()
    except httpx.ConnectError:
        return [types.TextContent(type="text", text=f"❌ Could not connect to {BASE_URL}. Is the server running?")]
    except httpx.HTTPStatusError as e:
        return [types.TextContent(type="text", text=f"❌ HTTP error {e.response.status_code}: {e.response.text[:500]}")]
    except Exception as e:
        return [types.TextContent(type="text", text=f"❌ Error: {e}")]

    a = data.get("analysis", {}) or {}

    def _txt(item):
        return item.get("text", "") if isinstance(item, dict) else str(item)

    def _list(key, n=40):
        return [_txt(x) for x in (a.get(key) or [])[:n]]

    lines = [
        "## 🧪 Onboarding DRY-RUN (real AI, WITHOUT creating data)",
        f"**generated:** {data.get('generated')}  ·  **insights (simulated):** {data.get('insightCount')}",
        "",
        "### 📊 Summary",
        (a.get("summary") or "—"),
        "",
        f"**🎯 Real type:** {a.get('project_type_real') or '—'}",
        f"**👤 Client profile:** {a.get('client_profile') or '—'}",
        f"**💡 Key insight:** {a.get('key_insight') or '—'}",
        "",
    ]
    for key, label in [("tasks", "✅ Pending tasks"), ("work_done", "🏁 Work done"),
                       ("blockers", "🚧 Blockers"), ("risks", "⚠️ Risks"),
                       ("decisions", "🧭 Decisions"), ("metrics", "📈 Metrics"),
                       ("tech_issues", "🔧 Technical issues")]:
        vals = _list(key)
        lines.append(f"### {label} ({len(vals)})")
        for v in vals:
            if v:
                lines.append(f"  • {v[:220]}")
        lines.append("")
    if data.get("reason"):
        lines.append(f"⚠️ reason: {data['reason']}")
    return [types.TextContent(type="text", text="\n".join(lines))]


# ══════════════════════════════════════════════════════════════════════════════
# ENTRY POINT
# ══════════════════════════════════════════════════════════════════════════════

async def main():
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
