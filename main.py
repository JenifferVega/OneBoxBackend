"""OneBox Backend entry point.

- `lambda_handler`: AWS Lambda entrypoint (agent-only mode, request/response JSON).
- `app`: FastAPI application built by api.app.create_app() — the full
  implementation lives in api/ (schemas, controllers, services) and agent/.
- `python main.py`: starts the uvicorn server on port 8006 (Docker CMD).
"""
from dotenv import load_dotenv
load_dotenv()


import json
from agent.graph import run_agent
from agent.tools import set_current_user, clear_current_user


def lambda_handler(event, context):
    """AWS Lambda handler."""

    headers = {
        "Content-Type": "application/json",
        "Access-Control-Allow-Origin": "*",
        "Access-Control-Allow-Headers": "Content-Type",
        "Access-Control-Allow-Methods": "POST, OPTIONS"
    }

    method = event.get("requestContext", {}).get("http", {}).get("method", "")
    if method == "OPTIONS" or event.get("httpMethod") == "OPTIONS":
        return {"statusCode": 200, "headers": headers, "body": ""}

    try:
        body = json.loads(event.get("body", "{}"))
        message = body.get("message", "")
        history = body.get("history", [])

        if not message:
            return {
                "statusCode": 400,
                "headers": headers,
                "body": json.dumps({"error": "The 'message' field is required"})
            }

        # Multi-tenant context: WITHOUT userId, tools deliberately fail
        # (same pattern as /chat in api/controllers/chat.py). Previously this
        # handler ran without context → risk of leaking data between users.
        event_headers = {k.lower(): v for k, v in (event.get("headers") or {}).items()}
        user_id = event_headers.get("x-user-id") or body.get("userId") or body.get("uid") or ""
        user_email = (event_headers.get("x-user-email") or body.get("email") or "").lower()
        if not user_id:
            return {
                "statusCode": 401,
                "headers": headers,
                "body": json.dumps({"error": "Missing userId (header x-user-id or body.userId)"})
            }

        print(f"[Agent] Message: {message}")
        print(f"[Agent] History: {len(history)} messages")

        set_current_user(user_id, user_email)
        try:
            result = run_agent(message, history)
        finally:
            clear_current_user()

        print(f"[Agent] Tools: {result.get('tools_used', [])}")
        print(f"[Agent] Response: {result.get('response', '')[:100]}...")

        return {
            "statusCode": 200,
            "headers": headers,
            "body": json.dumps({
                "response": result["response"],
                "toolsUsed": result.get("tools_used", [])
            }, ensure_ascii=False)
        }

    except Exception as e:
        print(f"[Agent] Error: {str(e)}")
        import traceback
        traceback.print_exc()

        return {
            "statusCode": 500,
            "headers": headers,
            "body": json.dumps({
                "error": "Internal agent error",
                "details": str(e)
            })
        }


from api.app import create_app

app = create_app()


if __name__ == "__main__":
    import argparse
    import os

    import uvicorn

    parser = argparse.ArgumentParser(description="OneBox Agent server")
    parser.add_argument("--host", default=os.environ.get("HOST", "0.0.0.0"),
                        help="Listen host (default: 0.0.0.0 or env HOST)")
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", 8006)),
                        help="Listen port (default: 8006 or env PORT)")
    parser.add_argument("--reload", action="store_true",
                        help="Auto-reload in development")
    args = parser.parse_args()

    print(f"\n🚀 Starting OneBox Agent at http://localhost:{args.port}")
    print(f"📖 Docs at http://localhost:{args.port}/docs")
    print("📡 REST API: /api/projects, /api/insights, /api/inbox, /api/notifications")
    print("📱 Twilio webhook: /api/twilio/webhook\n")

    # uvicorn requires the "main:app" import string when reload=True.
    uvicorn.run("main:app" if args.reload else app,
                host=args.host, port=args.port, reload=args.reload)
