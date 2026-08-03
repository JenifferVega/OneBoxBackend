"""Narrador de notificaciones (WhatsApp/SMS, contactos del proyecto)."""

GUIDANCE = """## GUÍA PARA NOTIFICACIONES:
1. Si se enviaron notificaciones (status "sent"), confirma a quién (nombre y canal) y resume el contenido.
2. Si algún envío falló (error, sin teléfono, número o email inválido, _schedule_error), dilo
   claramente y explica el motivo; NO lo presentes como enviado.
3. Si quedó PROGRAMADO (status "scheduled_pending"/"scheduled"/"scheduled_recurring"), di que se
   programó para la fecha/hora del resultado (scheduled_at) al destinatario del resultado, aclarando
   que todavía NO se ha enviado (saldrá cuando llegue el momento). No digas "enviado".
4. Si se consultaron contactos, lista quién tiene teléfono/email y sus pendientes.

## EJEMPLO:
"📱 **Notificaciones enviadas:**
• WhatsApp a **María** (+34 612...): 2 tareas pendientes
• WhatsApp a **Juan** (+50 494...): 1 tarea bloqueada

⚠️ **Pedro** no tiene teléfono registrado — puedo avisarle por correo si quieres.\""""
