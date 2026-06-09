"""Narrador de resultados de correos (listar/inspeccionar/enviar)."""

GUIDANCE = """## GUÍA PARA CORREOS:
1. Si hay correos, menciona: cantidad, remitentes principales, temas.
2. Si NO hay correos (count: 0), explica que no se encontraron y sugiere alternativas
   (otra query, sin "from:", revisar el inbox sin asignar).
3. Si se envió un correo, confirma destinatario y asunto.

## EJEMPLO:
"✅ **Acción completada:**
• Envié correo de seguimiento a juan@empresa.com sobre la factura pendiente

¿Necesitas algo más?\""""
