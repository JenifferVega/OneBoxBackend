# Diagnóstico General del Sistema IA — OneBox Agent
**Fecha:** 2026-06-09 | **Sesiones ejecutadas:** 5 | **Turnos totales:** 23

---

## Resumen ejecutivo

El agente se comporta correctamente en la mayoría de los flujos simples y medios. Las reglas de `REQUIRED_PARAMS` funcionan, el flujo multi-paso de notificaciones es sólido, y la extracción de fases/participantes desde descripciones ricas es una de las fortalezas del sistema. Sin embargo, hay **2 problemas reales** que afectan la confiabilidad y **1 limitación de dry-run** que distorsiona los resultados en test.

---

## Scorecard por sesión

| Sesión | Turnos | Iteraciones > 1 | Errores | Score |
|---|---|---|---|---|
| Correo | 6 | 2 (T2, T3) | 0 | 🟡 Bueno |
| Proyectos | 6 | 1 (T5) | 0 | 🟡 Bueno |
| Notificaciones | 4 | 0 | 0 | 🟢 Perfecto |
| Utilidades | 4 | 0 | 0 | 🟢 Perfecto |
| Integración | 3 | 1 (T3) | 0 | 🟡 Bueno |

---

## Problemas detectados (ordenados por impacto)

---

### 🔴 PROBLEMA 1 — `match` por nombre falla con proyectos creados en la misma sesión
**Severidad:** Alta  
**Ocurrencia:** Sesión 2 T5, Sesión 5 T3  
**Síntoma:** El planner genera un plan correcto con `match: {key: "name", value: "Alpha"}`, pero el ejecutor no encuentra el proyecto porque en dry-run `listar_proyectos` siempre devuelve los mismos dos proyectos ficticios ("Proyecto Demo A", "Proyecto Demo B"), ignorando lo que se creó en el mismo turno.

**Ejemplo observado (Sesión 2 T5):**
- Plan generado: `listar_proyectos → crear_tarea` con `match: {key: "name", value: "Alpha"}`  
- Resultado: "no encontré un proyecto llamado Alpha" — la tarea se creó sin project_id válido  
- Iteraciones: 3

**Causa raíz:** Los `_DRY_RUN_RESULTS` de `listar_proyectos` en `executor/node.py` son estáticos y no reflejan proyectos creados durante la misma sesión de debug. El planner hace lo correcto; el problema está en la capa de simulación.

**Impacto en producción:** En producción con DynamoDB real este problema no existe — `listar_proyectos` devuelve el proyecto recién creado. **Es un bug de dry-run, no del planner.**

**Acción recomendada:** En `executor/node.py`, hacer que los resultados simulados de `crear_proyecto` se acumulen en memoria para que el siguiente `listar_proyectos` en dry-run los incluya:

```python
# En _DRY_RUN_RESULTS o en la lógica del executor:
# Si se ejecutó crear_proyecto en un paso anterior,
# agregar ese proyecto al resultado simulado de listar_proyectos.
```

---

### 🟡 PROBLEMA 2 — `listar_correos` necesita 2 iteraciones con remitentes conocidos
**Severidad:** Media  
**Ocurrencia:** Sesión 1 T2 ("correos de LinkedIn")  
**Síntoma:** El planner usa `query: "linkedin"` en el primer intento en lugar de `query: "from:linkedin"`. En la segunda iteración lo corrige.

**Causa raíz:** La regla `SEARCH_GUIDE` en `catalog.py` documenta el patrón `from:` pero no tiene un ejemplo de contraste ❌/✅ explícito para el caso de remitente conocido.

**Cambio propuesto en `catalog.py` — sección `SEARCH_GUIDE`:**

```
❌ INCORRECTO — "linkedin" como término genérico cuando el usuario nombra un remitente conocido:
  listar_correos(query="linkedin")
  → Busca en asunto/contenido, no filtra por remitente.

✅ CORRECTO — usar "from:" para cualquier servicio o empresa conocida:
  listar_correos(query="from:linkedin")
  listar_correos(query="from:apple")
  listar_correos(query="from:notion.so")
```

---

### 🟡 PROBLEMA 3 — Narrator desincronizado en `clasificar_mensajes_automatico` tras `analizar_inbox`
**Severidad:** Media  
**Ocurrencia:** Sesión 1 T5  
**Síntoma:** El turno anterior (`analizar_inbox`) encontró 2 mensajes. En el turno siguiente, `clasificar_mensajes_automatico` retorna "inbox vacío". El narrator lo presenta como estado real, sin mencionar los mensajes del turno anterior.

**Causa raíz:** En dry-run, `clasificar_mensajes_automatico` devuelve resultado vacío independientemente del estado anterior. El narrator no tiene guía para contrastar con el historial de la conversación.

**Cambio propuesto en `narrator/narrators/`:** Agregar un narrator especializado (o regla en el narrator general) que, cuando `clasificar_mensajes_automatico` devuelva vacío pero el historial tenga resultados de `analizar_inbox`, explique el estado de forma coherente en lugar de contradecir el turno anterior.

---

## Lo que funciona correctamente ✅

**Planner — fortalezas confirmadas:**

- `REQUIRED_PARAMS` funciona: el planner no ejecuta `crear_proyecto` ni `crear_recordatorio` sin datos. En todos los casos donde faltaban datos usó `direct_response` correctamente.
- `FULL_EXTRACTION` funciona: descripción rica → plan completo en 1 iteración. Sesión 2 T3 (Alpha, 3 fases) y Sesión 5 T1 (Marketing, 4 fases inferidas) son los casos más exigentes y ambos pasaron en 1 iteración.
- `foreach` en notificaciones: el flujo `listar_proyectos → obtener_contactos_proyecto → enviar_notificacion(foreach)` funcionó perfecto en 1 iteración, enviando a múltiples destinatarios con un solo paso.
- Inferencia de tipo de proyecto: "campaña de verano" → Marketing → fases correctas (Investigación, Estrategia, Ejecución, Medición).
- Resolución de emails desde contexto: `enviar_correo` tomó `ana@empresa.com` del turno anterior sin pedirlo al usuario.
- `verificar_sla` vs `resumen_proactivo`: el agente no confunde ambas herramientas. "hay algo urgente?" → `verificar_sla`, no `resumen_proactivo`.
- Protección de tarea sin proyecto: "crea una tarea urgente" sin contexto → `direct_response` preguntando el proyecto, nunca intenta inventar un `project_id`.

---

## Tabla de herramientas probadas

| Herramienta | Probada | Resultado | Iteraciones promedio |
|---|---|---|---|
| `listar_correos` | ✅ | Funciona, 2 iter en remitentes conocidos | 1.5 |
| `inspeccionar_correo` | ✅ | Funciona con `from_step` | 2 |
| `analizar_inbox` | ✅ | Correcto | 1 |
| `clasificar_mensajes_automatico` | ✅ | Correcto (narrator inconsistente) | 1 |
| `enviar_correo` | ✅ | Correcto, toma email del contexto | 1 |
| `listar_proyectos` | ✅ | Correcto | 1 |
| `crear_proyecto` | ✅ | Correcto con extracción completa | 1 |
| `crear_tarea` | ✅ | Correcto cuando hay proyecto en contexto | 1–3 |
| `asignar_correo_a_proyecto` | ✅ | Funciona (3 iter en multi-paso) | 3 |
| `crear_insight` | ⬜ | No probada en este ciclo | — |
| `obtener_contactos_proyecto` | ✅ | Correcto | 1 |
| `enviar_notificacion` | ✅ | Correcto con `foreach` y número directo | 1 |
| `listar_notificaciones` | ✅ | Correcto | 1 |
| `crear_recordatorio` | ✅ | Correcto con y sin datos | 1 |
| `verificar_sla` | ✅ | Correcto | 1 |
| `resumen_proactivo` | ✅ | Correcto | 1 |

---

## Prioridad de cambios

| Prioridad | Cambio | Archivo |
|---|---|---|
| 🔴 Alta | Dry-run acumula proyectos creados para que `match` funcione entre turnos | `executor/node.py` |
| 🟡 Media | Agregar ejemplo ❌/✅ para `from:` en remitentes conocidos | `catalog.py` → `SEARCH_GUIDE` |
| 🟡 Media | Narrator coherente cuando `clasificar` devuelve vacío tras `analizar_inbox` | `narrator/narrators/` |
| 🟢 Baja | Probar `crear_insight` (única herramienta sin cobertura) | Próximo ciclo |
