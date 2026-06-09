"""Narrador proactivo (resúmenes ejecutivos, SLA, clasificación de inbox)."""

GUIDANCE = """## GUÍA PROACTIVA:
1. Si hay alertas SLA, presenta las más urgentes primero con emojis de prioridad.
2. Si hay acciones sugeridas, preséntalas como próximos pasos que la IA puede ejecutar.
3. Si se clasificaron mensajes, resume cuántos y a qué proyectos.

## EJEMPLOS:

Resumen proactivo:
"📊 **Resumen de tus proyectos:**

• **Migración AWS** - 3 tareas pendientes, 1 bloqueada 🔴
• **Rediseño UX** - 5 tareas, todo al día 🟢
• **Campaña Q2** - 2 tareas vencidas 🔴

📥 **Inbox:** 4 mensajes sin clasificar

🤖 **Acciones sugeridas:**
• Puedo enviar recordatorio a María sobre la tarea bloqueada
• Puedo clasificar automáticamente los mensajes del inbox

¿Qué quieres que haga?"

Alertas SLA:
"⚠️ **Alertas detectadas:**

🔴 **Tarea bloqueada:** 'Configurar VPN' en Migración AWS (3 días sin avance)
🔴 **Tarea vencida:** 'Entregar mockups' en Rediseño UX (venció hace 2 días)
🟡 **Inbox:** 5 mensajes pendientes de clasificar

¿Quieres que envíe recordatorios o clasifique el inbox?\""""
