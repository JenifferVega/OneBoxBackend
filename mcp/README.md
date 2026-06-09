# OneBox Chat MCP

Servidor MCP para probar el agente OneBox de forma interactiva en **modo debug** (dry-run, sin modificar la base de datos). Permite simular conversaciones multi-turno, observar el comportamiento del planner y generar reportes de feedback para mejorar el catálogo.

---

## ¿Para qué sirve?

- **Probar el agente en tiempo real** — enviar mensajes uno a uno, como lo haría un usuario real, y ver cómo responde el agente en cada turno.
- **Depurar el planner** — ver qué plan generó, cuántas iteraciones necesitó y si hubo errores de validación.
- **Generar feedback** — al terminar una sesión, producir un reporte con análisis automático y sugerencias concretas para mejorar `catalog.py`.
- **Vibe coding** — iterar rápido: prueba → detecta problema → ajusta catalog → reprueba.

---

## Instalación

### 1. Instala las dependencias Python

```bash
pip install mcp httpx
```

### 2. Registra el MCP en Claude

Copia el contenido de `mcp_config.json` a **uno** de estos archivos (el que aplique a tu setup):

**Claude Code (recomendado)** — crea o edita `.mcp.json` en la raíz del proyecto:
```bash
cp mcp/mcp_config.json .mcp.json
```

**Claude Desktop** — edita `claude_desktop_config.json`:
- Windows: `%APPDATA%\Claude\claude_desktop_config.json`
- Mac: `~/Library/Application Support/Claude/claude_desktop_config.json`

Agrega el bloque `onebox-chat` dentro de `"mcpServers": { ... }`.

> **Ajusta la ruta** en `mcp_config.json` si tu proyecto está en una ubicación diferente.

### 3. Levanta el servidor FastAPI

```bash
cd OneBoxBackend
uvicorn main:app --reload
```

Verifica que esté corriendo: [http://localhost:8000/health](http://localhost:8000/health)

### 4. Reinicia Claude

Al reiniciar, Claude verá las herramientas `onebox_chat`, `onebox_reset`, `onebox_history`, `onebox_report` y `onebox_export`.

---

## Herramientas disponibles

### `onebox_chat` — Enviar un mensaje al agente

```
onebox_chat(message, session_id?)
```

- Siempre usa **debug=True** (dry-run, no modifica la BD).
- El historial se acumula automáticamente dentro de la sesión.
- Usa el mismo `session_id` en todos los turnos de una conversación.
- La respuesta incluye: mensaje del agente, herramientas usadas, decisión del planner, plan e iteraciones.

### `onebox_reset` — Reiniciar una sesión

```
onebox_reset(session_id?)
```

Borra el historial de la sesión para empezar una conversación nueva.

### `onebox_history` — Ver el historial

```
onebox_history(session_id?)
```

Muestra todos los mensajes de la sesión en orden.

### `onebox_report` — Generar reporte de feedback

```
onebox_report(session_id?)
```

Genera un reporte Markdown con:
1. **Conversación completa** — todos los turnos
2. **Análisis por turno** — decisión del planner, iteraciones, herramientas, errores de validación detectados
3. **Problemas detectados** — iteraciones altas, validaciones fallidas, replans
4. **Sugerencias para catalog.py** — acciones concretas usando el patrón ❌/✅
5. **Datos JSON para entrenamiento** — formato listo para fine-tuning o análisis

### `onebox_export` — Guardar el reporte en disco

```
onebox_export(session_id?, filename?)
```

Guarda el reporte en `mcp/reports/<filename>.md`. Si no se especifica nombre, usa la fecha y sesión.

---

## Flujo de trabajo típico

```
1. Levanta el servidor:  uvicorn main:app --reload
2. En Claude: onebox_reset(session_id="prueba-1")
3. onebox_chat("quiero crear un proyecto", session_id="prueba-1")
4. onebox_chat("se llama Alpha", session_id="prueba-1")
5. onebox_chat("es de backend", session_id="prueba-1")
6. onebox_chat("muéstrame mis correos", session_id="prueba-1")   ← cambio de tema
7. onebox_chat("participan Ana y Carlos...", session_id="prueba-1")
8. onebox_report(session_id="prueba-1")    ← análisis completo
9. onebox_export(session_id="prueba-1")    ← guarda en mcp/reports/
10. Ajusta catalog.py según las sugerencias
11. Reinicia el servidor y repite
```

---

## Variables de entorno

| Variable | Default | Descripción |
|---|---|---|
| `ONEBOX_BASE_URL` | `http://localhost:8000` | URL del servidor FastAPI |
| `ONEBOX_USER_ID` | `debug-user-001` | ID de usuario para las llamadas |
| `ONEBOX_USER_EMAIL` | `debug@onebox.com` | Email del usuario para las llamadas |
| `ONEBOX_REPORTS_DIR` | `mcp/reports/` | Carpeta donde se guardan los reportes exportados |

---

## Estructura de la carpeta

```
mcp/
  server.py          ← servidor MCP (único archivo a ejecutar)
  mcp_config.json    ← configuración de ejemplo para copiar a .mcp.json
  README.md          ← este archivo
  reports/           ← reportes exportados (se crea automáticamente)
```

---

## Notas

- El MCP siempre usa `debug=True` — **nunca escribe en DynamoDB ni llama a Twilio**.
- El historial vive en memoria; al reiniciar el servidor MCP, las sesiones se borran.
- Si el servidor FastAPI no está corriendo, `onebox_chat` retorna un error claro con instrucciones.
- Para probar con datos reales (producción), usa el endpoint directamente con `curl` o Postman.
