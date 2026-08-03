"""Punto de entrada de OneBox Backend.

- `lambda_handler`: entrypoint AWS Lambda (modo agente-only, request/response JSON).
- `app`: aplicación FastAPI construida por api.app.create_app() — toda la
  implementación vive en api/ (schemas, controllers, services) y agent/.
- `python main.py`: levanta el servidor uvicorn en el puerto 8006 (Docker CMD).
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
                "body": json.dumps({"error": "El campo 'message' es requerido"})
            }

        # Contexto multi-tenant: SIN userId las tools fallan a propósito
        # (mismo patrón que /chat en api/controllers/chat.py). Antes este
        # handler corría sin contexto → riesgo de fuga entre usuarios.
        event_headers = {k.lower(): v for k, v in (event.get("headers") or {}).items()}
        user_id = event_headers.get("x-user-id") or body.get("userId") or body.get("uid") or ""
        user_email = (event_headers.get("x-user-email") or body.get("email") or "").lower()
        if not user_id:
            return {
                "statusCode": 401,
                "headers": headers,
                "body": json.dumps({"error": "Falta userId (header x-user-id o body.userId)"})
            }

        print(f"[Agent] Mensaje: {message}")
        print(f"[Agent] Historial: {len(history)} mensajes")

        set_current_user(user_id, user_email)
        try:
            result = run_agent(message, history)
        finally:
            clear_current_user()

        print(f"[Agent] Tools: {result.get('tools_used', [])}")
        print(f"[Agent] Respuesta: {result.get('response', '')[:100]}...")

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
                "error": "Error interno del agente",
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
                        help="Host de escucha (default: 0.0.0.0 o env HOST)")
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", 8006)),
                        help="Puerto de escucha (default: 8006 o env PORT)")
    parser.add_argument("--reload", action="store_true",
                        help="Recarga automática en desarrollo")
    args = parser.parse_args()

    print(f"\n🚀 Iniciando OneBox Agent en http://localhost:{args.port}")
    print(f"📖 Docs en http://localhost:{args.port}/docs")
    print("📡 REST API: /api/projects, /api/insights, /api/inbox, /api/notifications")
    print("📱 Twilio webhook: /api/twilio/webhook\n")

    # uvicorn exige el import string "main:app" cuando reload=True.
    uvicorn.run("main:app" if args.reload else app,
                host=args.host, port=args.port, reload=args.reload)
