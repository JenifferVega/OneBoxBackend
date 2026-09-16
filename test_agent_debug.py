"""
Self-play test of the OneBox agent in debug mode.
Runs predefined conversations and prints the debug_info for each turn.

Usage:
    python test_agent_debug.py

Requires the backend to be running (python main.py) and ngrok active.
"""
import json
import time
import urllib.request
import urllib.error

#BASE    = "https://sumaikun.ngrok.app"
BASE = "http://localhost:8000"
UID     = "test-debug-user-001"
EMAIL   = "debug@onebox.test"
HEADERS = {
    "Content-Type": "application/json",
    "x-user-id": UID,
    "x-user-email": EMAIL,
    "ngrok-skip-browser-warning": "true",
}


# ─────────────────────────────────────────────
def chat(message: str, history: list = None, debug: bool = True) -> dict:
    payload = json.dumps({
        "message": message,
        "history": history or [],
        "debug": debug,
    }).encode()
    req = urllib.request.Request(
        f"{BASE}/chat", data=payload, headers=HEADERS, method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=90) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return {"error": e.read().decode(), "http_status": e.code}
    except Exception as e:
        return {"error": str(e)}


def print_turn(turn_num: int, message: str, result: dict):
    print(f"\n{'─'*60}")
    print(f"  Turn {turn_num} → \"{message}\"")
    print(f"{'─'*60}")
    print(f"  RESPONSE : {result.get('response', 'N/A')}")
    print(f"  TOOLS    : {result.get('toolsUsed', [])}")
    di = result.get("debug_info")
    if di:
        print(f"  DEBUG_INFO:\n{json.dumps(di, indent=4, ensure_ascii=False)}")
    else:
        print("  DEBUG_INFO: (not available)")
    if result.get("error"):
        print(f"  ⚠️  ERROR: {result['error']}")


def run_scenario(title: str, turns: list):
    print(f"\n{'='*60}")
    print(f"  SCENARIO: {title}")
    print(f"{'='*60}")
    history = []
    for i, message in enumerate(turns, 1):
        result = chat(message, history, debug=True)
        print_turn(i, message, result)
        # accumulate history for the next turn
        if "response" in result:
            history.append({"role": "user",      "content": message})
            history.append({"role": "assistant",  "content": result["response"]})
        time.sleep(1)  # avoid rate-limit
    return history


# ─────────────────────────────────────────────
#  SCENARIOS
# ─────────────────────────────────────────────

if __name__ == "__main__":

    # ── 1. Intent without data → should ask for a name ─────────────────
    run_scenario(
        "Project creation without data (should ask for name)",
        ["I want to create a project"],
    )

    # ── 2. Everything in one message → should extract without asking ─────────
    run_scenario(
        "Full project in one message (should not ask anything)",
        [
            "create a Marketing project called Nova. "
            "Laura Gomez (coordinator) and Daniel Rojas (dev) will be in charge. "
            "Phases: market research, brand identity, digital launch.",
        ],
    )

    # ── 3. Step-by-step flow (multi-turn) ──────────────────────────
    run_scenario(
        "Step-by-step creation with a topic change in the middle",
        [
            "I want to create a project",         # turn 1: asks for name
            "it's called Alpha",                   # turn 2: gives name
            "it's a backend project",              # turn 3: gives type
            "show me my emails",                   # turn 4: TOPIC CHANGE — should not create a project
            "Alpha is a hotel reservations app, "
            "coordinated by Ana Torres. Phases: "
            "UX design, API development, QA testing, deployment.",  # turn 5: resumes with description
        ],
    )

    # ── 4. Type inferred from natural language ─────────────────────
    run_scenario(
        "Type inference from natural language",
        [
            "create a project for a mobile delivery app called QuickBite, "
            "it will last 2 months, Carlos (iOS) and Marta (backend) are involved.",
        ],
    )

    # ── 5. Task without a specified project ─────────────────────────
    run_scenario(
        "Create task without specifying a project (should list projects first)",
        ["create an urgent task: review the Q3 budget"],
    )

    # ── 6. Team notification (should fetch contacts first) ───
    run_scenario(
        "Send a WhatsApp to the team without an explicit phone number",
        ["send a WhatsApp to the Alpha project team saying there's a meeting tomorrow at 10am"],
    )

    print(f"\n{'='*60}")
    print("  END OF TESTS")
    print(f"{'='*60}\n")
