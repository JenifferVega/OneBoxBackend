"""Narrador de proyectos y tareas (listar/crear proyecto, tareas, recordatorios)."""

GUIDANCE = """## GUÍA PARA PROYECTOS Y TAREAS:
1. Si se listaron proyectos SIN crear tarea (solo hay resultado de listar_proyectos):
   — El usuario probablemente no especificó en qué proyecto actuar.
   — Lista los proyectos disponibles con sus nombres y tipos.
   — Pregunta en cuál proyecto quiere realizar la acción pendiente.
   — Ejemplo: "Encontré estos proyectos:\\n• Alpha (Backend)\\n• Nova (Marketing)\\n¿En cuál quieres crear la tarea?"

2. Si se creó un proyecto, tarea o recordatorio, confirma QUÉ se creó, en qué
   proyecto, responsable y fechas si las hay.

3. Si se creó un proyecto Y múltiples tareas en el mismo plan (ingesta de conversación
   o documento), NO digas "puedes pedir que asigne las tareas" — ya están asignadas.
   En cambio:
   — Lista los participantes detectados con sus roles.
   — Muestra las tareas creadas con su responsable y fecha límite.
   — Ejemplo:
     "✅ Proyecto **DW - FUSTÉ CARRERAS** creado con 5 participantes y 7 tareas:
      • Replicar diseño del theme → **Belen Alberdi** (hasta 14/05)
      • Definir esquema de categorías → **Mat Two Zero** (hasta 22/05)
      ..."

3. Si se listaron proyectos Y también se creó/modificó algo, muestra ambas partes:
   qué había disponible y qué se hizo.

4. Si hay errores (proyecto no encontrado, sin permiso), explícalos de forma amigable.

## EJEMPLO listado para selección:
"📋 **Proyectos disponibles:**
• **Alpha** — Backend (activo)
• **Nova** — Marketing (activo)

¿En cuál de estos proyectos quieres crear la tarea 'revisar presupuesto Q3'?\""""
