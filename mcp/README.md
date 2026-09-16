# OneBox Chat MCP

MCP server for testing the OneBox agent interactively in **debug mode** (dry-run, no database changes). It lets you simulate multi-turn conversations, watch the planner's behavior, and produce feedback reports to improve the catalog.

---

## What is it for?

- **Test the agent in real time** — send messages one at a time, the way a real user would, and see how the agent responds each turn.
- **Debug the planner** — see what plan it generated, how many iterations it needed, and whether there were validation errors.
- **Generate feedback** — when a session ends, produce a report with automatic analysis and concrete suggestions to improve `catalog.py`.
- **Vibe coding** — iterate fast: try → spot the problem → tweak the catalog → try again.

---

## Installation

### 1. Install the Python dependencies

```bash
pip install mcp httpx
```

### 2. Register the MCP in Claude

Copy the contents of `mcp_config.json` into **one** of these files (whichever fits your setup):

**Claude Code (recommended)** — create or edit `.mcp.json` in the project root:
```bash
cp mcp/mcp_config.json .mcp.json
```

**Claude Desktop** — edit `claude_desktop_config.json`:
- Windows: `%APPDATA%\Claude\claude_desktop_config.json`
- Mac: `~/Library/Application Support/Claude/claude_desktop_config.json`

Add the `onebox-chat` block inside `"mcpServers": { ... }`.

> **Adjust the path** in `mcp_config.json` if your project lives somewhere else.

### 3. Start the FastAPI server

```bash
cd OneBoxBackend
uvicorn main:app --reload
```

Verify it's running: [http://localhost:8000/health](http://localhost:8000/health)

### 4. Restart Claude

After restarting, Claude will see the tools `onebox_chat`, `onebox_reset`, `onebox_history`, `onebox_report` and `onebox_export`.

---

## Available tools

### `onebox_chat` — Send a message to the agent

```
onebox_chat(message, session_id?)
```

- Always uses **debug=True** (dry-run, doesn't touch the DB).
- History accumulates automatically within the session.
- Use the same `session_id` across all turns of a conversation.
- The response includes: agent message, tools used, planner decision, plan and iterations.

### `onebox_reset` — Reset a session

```
onebox_reset(session_id?)
```

Clears the session history so you can start a new conversation.

### `onebox_history` — See the history

```
onebox_history(session_id?)
```

Shows every message in the session in order.

### `onebox_report` — Generate a feedback report

```
onebox_report(session_id?)
```

Produces a Markdown report with:
1. **Full conversation** — every turn
2. **Per-turn analysis** — planner decision, iterations, tools, validation errors detected
3. **Detected issues** — high iterations, failed validations, replans
4. **Suggestions for catalog.py** — concrete actions using the ❌/✅ pattern
5. **Training-data JSON** — format ready for fine-tuning or analysis

### `onebox_export` — Save the report to disk

```
onebox_export(session_id?, filename?)
```

Saves the report at `mcp/reports/<filename>.md`. If no filename is given, it uses the date and session id.

---

## Typical workflow

```
1. Start the server:  uvicorn main:app --reload
2. In Claude: onebox_reset(session_id="test-1")
3. onebox_chat("I want to create a project", session_id="test-1")
4. onebox_chat("it's called Alpha", session_id="test-1")
5. onebox_chat("it's a backend project", session_id="test-1")
6. onebox_chat("show me my emails", session_id="test-1")   ← topic change
7. onebox_chat("Ana and Carlos are participants...", session_id="test-1")
8. onebox_report(session_id="test-1")    ← full analysis
9. onebox_export(session_id="test-1")    ← saved to mcp/reports/
10. Tune catalog.py based on the suggestions
11. Restart the server and repeat
```

---

## Environment variables

| Variable | Default | Description |
|---|---|---|
| `ONEBOX_BASE_URL` | `http://localhost:8000` | FastAPI server URL |
| `ONEBOX_USER_ID` | `debug-user-001` | User ID used in the calls |
| `ONEBOX_USER_EMAIL` | `debug@onebox.com` | User email used in the calls |
| `ONEBOX_REPORTS_DIR` | `mcp/reports/` | Folder where exported reports are saved |

---

## Folder structure

```
mcp/
  server.py          ← MCP server (only file to run)
  mcp_config.json    ← sample config to copy into .mcp.json
  README.md          ← this file
  reports/           ← exported reports (created automatically)
```

---

## Notes

- The MCP always uses `debug=True` — **never writes to DynamoDB or calls Twilio**.
- History lives in memory; when the MCP server restarts, sessions are wiped.
- If the FastAPI server isn't running, `onebox_chat` returns a clear error with instructions.
- To test against real data (production), hit the endpoint directly with `curl` or Postman.
