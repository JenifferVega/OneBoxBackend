# OneBox Backend — Contexto para Claude

## ¿Qué es este proyecto?

**OneBoxBackend** es un backend Python/FastAPI con un agente conversacional construido sobre LangGraph. El agente recibe mensajes de usuarios, planifica acciones usando herramientas (crear proyectos, tareas, enviar notificaciones, etc.) y responde en lenguaje natural.

**Proyecto separado — NO modificar:** `C:\Users\user\Desktop\AppDev\send\chatbot` es un proyecto distinto e independiente. Nunca toques archivos de esa carpeta.

---

## Arquitectura del agente

```
Usuario → context_resolver → planner → executor → validator → narrator → Respuesta
```

El flujo es un grafo LangGraph con estos nodos:

| Nodo | Archivo | Rol |
|---|---|---|
| context_resolver | `agent/graph/nodes/context_resolver/` | Detecta idioma, intención y contexto del historial |
| planner | `agent/graph/nodes/planner/` | Decide qué herramientas ejecutar y en qué orden |
| executor | `agent/graph/nodes/executor/` | Ejecuta el plan paso a paso (sin LLM) |
| validator | `agent/graph/nodes/validator/` | Evalúa si los resultados son correctos |
| narrator | `agent/graph/nodes/narrator/` | Genera la respuesta final en lenguaje natural |

---

## Archivos de comportamiento conversacional

Estos son los únicos archivos que definen **cómo conversa el agente**. Son el objetivo del flujo de debug y ajuste:

| Archivo | Qué controla |
|---|---|
| `agent/graph/nodes/planner/catalog.py` | Reglas del planner: cuándo usar qué herramienta, ejemplos ❌/✅, patrones multi-paso |
| `agent/graph/nodes/planner/prompts.py` | Prompt base del planner |
| `agent/graph/nodes/narrator/prompts.py` | Cómo el narrator presenta los resultados al usuario |
| `agent/graph/nodes/narrator/narrators/` | Narradores especializados por tipo de resultado |
| `agent/graph/nodes/validator/prompts.py` | Criterios que usa el validator para aprobar o rechazar resultados |

---

## Archivos de lógica de código (NO son objetivo del flujo conversacional)

Estos archivos son lógica de ejecución. Solo se modifican cuando hay un bug de código, no para ajustar comportamiento conversacional:

| Archivo | Rol |
|---|---|
| `agent/graph/nodes/executor/resolve.py` | Resolución de referencias `from_step` entre pasos del plan |
| `agent/graph/nodes/executor/validators.py` | Validaciones de parámetros antes de ejecutar herramientas |
| `agent/graph/nodes/executor/node.py` | Orquestación del executor, soporte de `foreach` |
| `agent/graph/builder.py` | Construcción del grafo LangGraph |
| `agent/tools.py` | Implementación de las herramientas (DynamoDB, Twilio, etc.) |

---

## Flujo de debug y ajuste conversacional (vibe coding)

Este es el proceso correcto para iterar sobre el comportamiento del agente:

```
1. Levantar el servidor:   uvicorn main:app --reload
2. Usar el MCP onebox-chat para conversar turno a turno con el agente
3. Al terminar la sesión: onebox_report() → ver análisis completo
4. Discutir con el usuario qué problema se detectó y por qué ocurre
5. El usuario aprueba el cambio propuesto
6. Claude edita el archivo de comportamiento conversacional correspondiente
7. Reiniciar el servidor y repetir desde el paso 2
```

### Regla fundamental

**Claude no modifica archivos de comportamiento conversacional sin que el usuario apruebe explícitamente el cambio.**

El reporte muestra el problema. La discusión define la solución. El usuario aprueba. Claude edita.

---

## Modo debug

El agente tiene un modo debug activable con `debug=True` en el request:
- Dry-run: no escribe en DynamoDB ni llama a Twilio
- Retorna `debug_info` con el plan, iteraciones del planner, decisiones y resultados simulados
- Los resultados simulados están en `executor/node.py` → `_DRY_RUN_RESULTS`

El MCP en `mcp/server.py` siempre usa `debug=True`.

---

## Señales de problema en el debug

Cuando el reporte muestra alguna de estas señales, hay algo que ajustar en los archivos de comportamiento:

| Señal | Causa probable | Archivo a revisar |
|---|---|---|
| `iteration > 1` | La regla del planner no es suficientemente clara | `catalog.py` — agregar ejemplo ❌/✅ |
| `_validation_error` en resultados | El planner generó un param inválido a pesar de la regla | `catalog.py` — reforzar la restricción |
| Planner creó un proyecto innecesario | Falta regla explícita de cuándo NO crear | `catalog.py` — PROHIBIDO |
| Narrator no menciona proyectos disponibles | El narrator no tiene guía para ese resultado | `narrator/narrators/` |
| Validator rechaza resultado correcto | Criterio de validación demasiado estricto | `validator/prompts.py` |

---

## Convención de ejemplos en catalog.py

El patrón más efectivo para enseñarle al planner es el contraste explícito:

```
❌ INCORRECTO — descripción del caso prohibido:
plan: [ ... el plan malo ... ]
→ Por qué está mal.

✅ CORRECTO:
plan: [ ... el plan correcto ... ]
```

Siempre que se agregue una nueva regla, incluir este contraste.

---

## Inicio de sesión de debug

Cuando el usuario diga "vamos a probar el agente", "iniciemos debug", "quiero testear el flujo" o similar, seguir este protocolo sin esperar más instrucciones:

**Paso 1 — Verificar prerequisitos**
Preguntar al usuario:
- ¿Está corriendo el servidor? (`uvicorn main:app --reload`)
- ¿Qué flujo quiere probar? (o proponer los escenarios conocidos)

**Paso 2 — Limpiar sesión**
Usar `onebox_reset()` antes de empezar para asegurar historial limpio.

**Paso 3 — Conducir la conversación turno a turno**
Usar `onebox_chat(message, session_id)` para cada mensaje. Mostrar al usuario la respuesta del agente y el plan ejecutado. No enviar el siguiente turno automáticamente — esperar confirmación del usuario para continuar.

**Paso 4 — Generar el reporte**
Al terminar (cuando el usuario lo indique o se agoten los turnos del escenario), ejecutar `onebox_report()` y leer el análisis completo.

**Paso 5 — Diagnosticar**
Identificar los problemas del reporte usando la tabla de señales. Explicar al usuario qué ocurrió y por qué, con referencia al archivo específico que lo causa.

**Paso 6 — Proponer el cambio**
Proponer el cambio concreto al archivo de comportamiento conversacional: qué línea o sección cambiar, qué agregar, mostrando el antes/después. **Esperar aprobación explícita del usuario.**

**Paso 7 — Editar y repetir**
Con aprobación, editar el archivo. Indicar al usuario que reinicie el servidor (`Ctrl+C` → `uvicorn main:app --reload`) y volver al Paso 2.

### Regla de oro de este loop
Solo se modifican los **archivos de comportamiento conversacional** (catalog.py, prompts, narrators). Nunca lógica de código sin un bug explícito. Nunca sin aprobación del usuario.

---

## Stack técnico

- **Python 3.11+** con FastAPI y Uvicorn
- **LangGraph** para el grafo de agente
- **LangChain** para mensajes y LLM
- **DynamoDB** como base de datos
- **Twilio** para WhatsApp/SMS
- **LLM:** configurable vía `agent/llm.py` (Bedrock / Anthropic / Gemini)
- **MCP de debug:** `mcp/server.py` (requiere `pip install mcp httpx`)
