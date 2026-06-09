"""Narrador de notificaciones (WhatsApp/SMS, contactos del proyecto)."""

GUIDANCE = """## GUÍA PARA NOTIFICACIONES:
1. Si se enviaron notificaciones, confirma a quién (nombre y canal), y resume el contenido.
2. Si algún envío falló (sin teléfono, número inválido), dilo claramente y sugiere
   actualizar el contacto en el proyecto.
3. Si se consultaron contactos, lista quién tiene teléfono y sus pendientes.

## EJEMPLO:
"📱 **Notificaciones enviadas:**
• WhatsApp a **María** (+34 612...): 2 tareas pendientes
• WhatsApp a **Juan** (+50 494...): 1 tarea bloqueada

⚠️ **Pedro** no tiene teléfono registrado — puedo avisarle por correo si quieres.\""""
