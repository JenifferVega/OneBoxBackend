"""WhatsApp/Twilio endpoints: inbound webhook and send test."""
from fastapi import APIRouter, HTTPException, Request

from api.services import whatsapp as whatsapp_service

router = APIRouter()


@router.post("/api/twilio/webhook")
async def twilio_webhook(request: Request):
    """Twilio webhook for inbound WhatsApp/SMS. Handled by wizard or AI agent."""
    try:
        body_raw = (await request.body()).decode('utf-8')
        return whatsapp_service.handle_twilio_webhook(body_raw)
    except Exception as e:
        print(f"[Webhook] Error: {e}")
        return {"status": "error", "detail": str(e)}


@router.post("/api/test-whatsapp")
async def test_whatsapp(request: Request):
    """Send a WhatsApp directly without going through the agent."""
    from agent.tools import send_notification
    body = await request.json()
    phone = body.get("phone", "")
    message = body.get("message", "OneBox test")
    if not phone:
        raise HTTPException(status_code=400, detail="phone required")
    result = send_notification(
        recipient=phone,
        message=message,
        channel="whatsapp",
        project_id="test",
        project_name="Test"
    )
    return result
