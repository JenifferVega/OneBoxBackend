# Plan de Pruebas del Agente OneBox — Evaluación del Sistema IA

**Fecha:** 2026-06-09  
**Objetivo:** Probar cada herramienta del agente turno a turno con `onebox_chat`, detectar problemas en el planner/narrator/validator y generar un diagnóstico del estado actual del sistema IA.

---

## Prerequisitos antes de empezar

1. Servidor corriendo: `uvicorn main:app --reload`
2. Verificar salud: `GET http://localhost:8000/health`
3. MCP conectado: herramientas `onebox_chat`, `onebox_reset`, `onebox_report`, `onebox_export` visibles en Claude
4. Limpiar sesión: `onebox_reset(session_id="plan-pruebas")`

---

## Herramientas a cubrir (14 en total)

| # | Herramienta | Categoría |
|---|---|---|
| 1 | `listar_correos` | Correo |
| 2 | `inspeccionar_correo` | Correo |
| 3 | `analizar_inbox` | Correo |
| 4 | `clasificar_mensajes_automatico` | Correo |
| 5 | `enviar_correo` | Correo |
| 6 | `listar_proyectos` | Proyectos |
| 7 | `crear_proyecto` | Proyectos |
| 8 | `crear_tarea` | Proyectos |
| 9 | `asignar_correo_a_proyecto` | Proyectos |
| 10 | `crear_insight` | Proyectos |
| 11 | `obtener_contactos_proyecto` | Notificaciones |
| 12 | `enviar_notificacion` | Notificaciones |
| 13 | `listar_notificaciones` | Notificaciones |
| 14 | `crear_recordatorio` | Utilidades |
| 15 | `verificar_sla` | Utilidades |
| 16 | `resumen_proactivo` | Utilidades |

---

## Escenarios de prueba

### SESIÓN 1 — Correo y clasificación
**session_id:** `"prueba-correo"`

| Turno | Mensaje | Herramienta esperada | Qué evaluar |
|---|---|---|---|
| 1 | `"muéstrame mis correos"` | `listar_correos` | ¿Lista sin query? ¿Narrator muestra asuntos/remitentes? |
| 2 | `"correos de LinkedIn"` | `listar_correos(query="from:linkedin")` | ¿Usa `from:` para remitente conocido? |
| 3 | `"inspecciona el primer correo"` | `inspeccionar_correo` | ¿Resuelve el `email_id` del resultado anterior con `from_step`? |
| 4 | `"analiza mi inbox"` | `analizar_inbox` | ¿Retorna correos sin asignar? ¿Narrator los presenta bien? |
| 5 | `"clasifica esos mensajes"` | `clasificar_mensajes_automatico` | ¿Sugiere proyectos? ¿Narrator explica las sugerencias? |
| 6 | `"envía un seguimiento a test@empresa.com sobre el tema del correo anterior"` | `enviar_correo` | ¿Pide datos faltantes si no hay asunto/cuerpo? ¿No inventa email? |

**Señales de alerta:**
- `iteration > 1` en cualquier turno → regla del planner poco clara
- Narrator que no menciona los correos encontrados o no propone acción siguiente
- `inspeccionar_correo` con `email_id` inventado en lugar de `from_step`

---

### SESIÓN 2 — Proyectos y tareas
**session_id:** `"prueba-proyectos"`

| Turno | Mensaje | Herramienta esperada | Qué evaluar |
|---|---|---|---|
| 1 | `"muéstrame mis proyectos"` | `listar_proyectos` | ¿Lista correctamente? ¿Narrator da nombres e IDs? |
| 2 | `"quiero crear un proyecto"` | `direct_response` (pide datos) | ¿Pregunta nombre, tipo y descripción antes de actuar? |
| 3 | `"se llama Alpha, es de backend. Liderado por Laura García, coordinado por Daniel Rojas. Tendrá 3 fases: Diseño, Desarrollo y Despliegue, durará 2 meses"` | `crear_proyecto + 3x crear_tarea` | ¿Extrae participantes? ¿Crea las 3 fases como tareas? ¿Fechas realistas? |
| 4 | `"crea una tarea urgente: revisar presupuesto Q3"` | `listar_proyectos` (solo) | ¿NO intenta crear_tarea sin que el usuario elija proyecto? |
| 5 | `"en el proyecto Alpha"` | `listar_proyectos + crear_tarea` | ¿Usa `match` por nombre para resolver project_id? |
| 6 | `"qué tareas están bloqueadas o vencidas?"` | `verificar_sla` | ¿Escanea todos los proyectos? ¿Narrator lista los bloqueados? |

**Señales de alerta:**
- Turno 2: el planner ejecuta `crear_proyecto` sin tener descripción → **bug crítico**
- Turno 3: solo crea el proyecto pero no las tareas → falta regla FULL_EXTRACTION
- Turno 4: incluye `crear_tarea` en el mismo plan → **prohibido por catalog.py**
- `project_id` inventado o en blanco en cualquier herramienta

---

### SESIÓN 3 — Notificaciones
**session_id:** `"prueba-notificaciones"`

| Turno | Mensaje | Herramienta esperada | Qué evaluar |
|---|---|---|---|
| 1 | `"muéstrame mis proyectos"` | `listar_proyectos` | Necesario para obtener project_id de Alpha |
| 2 | `"manda los pendientes por WhatsApp al equipo de Alpha"` | `obtener_contactos_proyecto + enviar_notificacion(foreach)` | ¿Usa `foreach` para enviar a todos? ¿No genera un paso por contacto? |
| 3 | `"muéstrame las notificaciones enviadas de Alpha"` | `listar_notificaciones(project_id)` | ¿Filtra por proyecto correctamente? |
| 4 | `"manda un WhatsApp a +50494622817 que el deploy fue exitoso"` | `enviar_notificacion` | ¿Usa el número dado directamente sin pedir confirmación innecesaria? |

**Señales de alerta:**
- Turno 2: genera `N` pasos de `enviar_notificacion` en lugar de 1 con `foreach` → **bug de catalog**
- Planner pregunta el teléfono al usuario cuando debería obtenerlo de `obtener_contactos_proyecto`

---

### SESIÓN 4 — Recordatorios y resumen
**session_id:** `"prueba-utils"`

| Turno | Mensaje | Herramienta esperada | Qué evaluar |
|---|---|---|---|
| 1 | `"ponme un recordatorio"` | `direct_response` (pide título y fecha) | ¿No crea el recordatorio sin datos? |
| 2 | `"revisar contrato con cliente X, para el 2026-06-20"` | `crear_recordatorio` | ¿Recoge titulo y fecha del contexto? |
| 3 | `"dame un resumen de cómo va todo"` | `resumen_proactivo` | ¿Cubre proyectos, tareas, SLA? ¿Narrator da un resumen ejecutivo útil? |
| 4 | `"hay algo urgente?"` | `verificar_sla` | ¿No re-invoca `resumen_proactivo` innecesariamente? |

**Señales de alerta:**
- Turno 1: crea recordatorio con título genérico → **viola REQUIRED_PARAMS**
- Turno 3: narrator da respuesta vaga sin estructurar los proyectos encontrados

---

### SESIÓN 5 — Flujo multi-paso complejo (integración)
**session_id:** `"prueba-integracion"`

| Turno | Mensaje | Herramienta esperada | Qué evaluar |
|---|---|---|---|
| 1 | `"crea un proyecto de Marketing para la campaña de verano, con Ana López (ana@empresa.com) como líder y Marcos Ruiz como diseñador. Duración: 1 mes"` | `crear_proyecto + 4x crear_tarea` (fases inferidas) | ¿Infiere fases Marketing: Investigación, Estrategia, Ejecución, Medición? |
| 2 | `"manda un correo a ana@empresa.com diciéndole que el proyecto arrancó"` | `enviar_correo` | ¿Toma el email del contexto del turno anterior? |
| 3 | `"clasifica el inbox y asigna los correos relevantes al proyecto de campaña"` | `clasificar_mensajes_automatico + asignar_correo_a_proyecto` | ¿Encadena ambas herramientas correctamente? |

---

## Criterios de evaluación del sistema IA

### Planner
| Criterio | Peso | Indicador |
|---|---|---|
| No inventa parámetros (`project_id`, `email_id`, teléfonos) | Alto | `_validation_error` ausente en debug_info |
| Resuelve datos con `from_step` correctamente | Alto | `iteration == 1` en el 90% de los turnos |
| Respeta REQUIRED_PARAMS (pide datos antes de actuar) | Alto | `direct_response` en turnos sin datos suficientes |
| Extrae participantes y fases de descripciones ricas | Medio | Plan con N+1 pasos cuando hay N fases |
| Usa `foreach` para notificaciones masivas | Medio | 1 solo paso de `enviar_notificacion` con foreach |
| No crea proyectos innecesarios para resolver `project_id` | Alto | Nunca `crear_proyecto` cuando se necesita solo el ID |

### Narrator
| Criterio | Peso | Indicador |
|---|---|---|
| Menciona entidades concretas (nombres, fechas, emails) | Alto | Sin respuestas vagas tipo "se hizo con éxito" |
| Propone siguiente acción natural | Medio | Cada respuesta sugiere qué puede hacer el usuario |
| Informa cuando un contacto no tiene teléfono | Medio | No silencia omisiones de `foreach` |
| Resumen ejecutivo estructurado en `resumen_proactivo` | Alto | Cubre proyectos, tareas, SLA bloqueados |

### Validator
| Criterio | Peso | Indicador |
|---|---|---|
| No rechaza resultados correctos | Alto | Sin `replan` injustificado en debug_info |
| Detecta `_validation_error` correctamente | Alto | Reroutea al planner cuando hay error real |

---

## Cómo ejecutar este plan

```
# Antes de cada sesión
onebox_reset(session_id="<id-sesion>")

# Cada turno
onebox_chat("<mensaje>", session_id="<id-sesion>")
→ Ver: respuesta del agente, plan ejecutado, iteraciones, herramientas usadas

# Al terminar cada sesión
onebox_report(session_id="<id-sesion>")
onebox_export(session_id="<id-sesion>", filename="reporte-<id-sesion>")
```

Después de cada sesión, comparar los resultados contra la tabla de criterios y anotar:
- ✅ Pasa / ❌ Falla / ⚠️ Parcial
- Si hay ❌ o ⚠️: identificar el archivo a ajustar (`catalog.py`, `prompts.py`, `narrators/`) y proponer el cambio.

---

## Plantilla de reporte por sesión

```
## Sesión: <nombre>
Fecha: YYYY-MM-DD

### Resultados por turno
| Turno | Mensaje | Herramienta real | Iteraciones | Errores | Estado |
|---|---|---|---|---|---|
| 1 | ... | ... | 1 | ninguno | ✅ |

### Problemas detectados
1. [Descripción del problema] — Causa probable: [catalog.py / prompts.py / narrators/]

### Cambios propuestos
- catalog.py línea X: [antes] → [después]
```
