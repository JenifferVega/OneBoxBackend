"""Endpoints de WhatsApp/Twilio: webhook entrante y test de envío."""
from fastapi import APIRouter, HTTPException, Request

from api.services import whatsapp as whatsapp_service

router = APIRouter()


@router.post("/api/twilio/webhook")
async def twilio_webhook(request: Request):
    """Webhook de Twilio para WhatsApp/SMS entrantes. Procesa con wizard o agente IA."""
    try:
        body_raw = (await request.body()).decode('utf-8')
        return whatsapp_service.handle_twilio_webhook(body_raw)
    except Exception as e:
        print(f"[Webhook] Error: {e}")
        return {"status": "error", "detail": str(e)}


@router.post("/api/test-whatsapp")
async def test_whatsapp(request: Request):
    """Test directo de envío de WhatsApp sin pasar por el agente."""
    from agent.tools import enviar_notificacion
    body = await request.json()
    phone = body.get("phone", "")
    message = body.get("message", "Prueba de OneBox")
    if not phone:
        raise HTTPException(status_code=400, detail="phone requerido")
    result = enviar_notificacion(
        destinatario=phone,
        mensaje=message,
        canal="whatsapp",
        project_id="test",
        project_name="Test"
    )
    return result
