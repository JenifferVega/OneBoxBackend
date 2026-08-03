# OneBox — Catálogo de roles y funciones

> Documento de referencia para el sistema de permisos. Nombres formalizados + inventario completo de funciones (16 acciones del agente MCP + ~45 endpoints REST), clasificadas por capacidad.
> Fuente: `agent/tools.py`, `mcp/server.py`, `api/controllers/`, `api/services/access.py`.
> Estado: propuesta de diseño (no implementado). Modelo **híbrido**: rol global + override por proyecto.

---

## 1. Roles

### 1.1 Roles globales (a nivel usuario / cuenta)

Definen el **techo** de capacidades del usuario en todo el sistema.

| Código interno | Etiqueta UI | Descripción |
|---|---|---|
| `propietario` | Propietario | Dueño de la cuenta. Todo, incluido conectar/desconectar Gmail, facturación y gestionar los roles de otros. |
| `miembro` | Miembro | Equipo operativo. Día a día completo, **incluye enviar comunicación al exterior**. Rol por defecto al unirse. |
| `restringido` | Miembro restringido | Organiza dentro de OneBox (proyectos, tareas, insights) pero **no envía nada al exterior**. Para perfiles junior, en prueba o integraciones. |
| `lector` | Invitado (solo lectura) | **Aparcado para v2.** Cliente o stakeholder externo que solo ve el estado de su proyecto. No activar en v1. |

### 1.2 Roles por proyecto (override dentro de un proyecto)

Se guardan en `participants[].rol_permiso` de cada proyecto y refinan lo que el usuario puede hacer **dentro de ese proyecto concreto**.

| Código | Etiqueta | Significado |
|---|---|---|
| `admin` | Admin de proyecto | Administra el proyecto: participantes, invitaciones, borrado. (= el actual `is_owner`) |
| `editor` | Editor | Crea/edita dentro del proyecto y, si su rol global lo permite, envía comunicación del proyecto. |
| `viewer` | Visor | Solo lectura del proyecto. |

### 1.3 Regla de combinación (híbrido)

Para una acción **con `project_id`**:

```
capacidad_permitida =
    capacidades(rol_global)            # techo: qué puede hacer en general
  ∩ capacidades(rol_proyecto)          # piso: qué se le permite aquí
  ∪ {administrar_proyecto} si rol_proyecto == admin   # única elevación, acotada a este proyecto
```

- El **rol global pone el techo**: un `restringido` nunca enviará al exterior, aunque en un proyecto sea `editor`.
- El **rol de proyecto restringe**: un `miembro` que en el Proyecto X es `viewer`, ahí solo lee.
- Ser **`admin` de proyecto** otorga administración *de ese proyecto* aunque el rol global no sea `propietario`. Es la única elevación permitida.

Para acciones **globales** (sin `project_id`) solo aplica el **rol global**.

---

## 2. Capacidades

La unidad real que se chequea. Cada función exige una capacidad; cada rol concede un conjunto de capacidades.

| Capacidad | Qué habilita | Riesgo |
|---|---|---|
| `leer` | Consultar y analizar. Sin escritura ni salida externa. | bajo |
| `escribir_interno` | Crear/editar datos dentro de OneBox (proyectos, tareas, insights, recordatorios, asignaciones, adjuntos, perfil propio). | medio |
| `enviar_externo` | Enviar comunicación que sale al mundo: email, WhatsApp, SMS. **La línea roja.** | alto |
| `administrar_proyecto` | Participantes, invitaciones, borrado de proyecto/adjuntos. | alto |
| `administrar_cuenta` | Conectar/desconectar la fuente de datos (Gmail) — afecta a toda la cuenta. | alto |
| `administrar_roles` | Asignar el rol global de otros usuarios y los roles por proyecto. | crítico |

### Capacidades por rol global

| Capacidad | `propietario` | `miembro` | `restringido` | `lector` |
|---|:---:|:---:|:---:|:---:|
| `leer` | ✅ | ✅ | ✅ | ✅ |
| `escribir_interno` | ✅ | ✅ | ✅ | ❌ |
| `enviar_externo` | ✅ | ✅ | ❌ | ❌ |
| `administrar_proyecto` | ✅ | 🔒¹ | ❌ | ❌ |
| `administrar_cuenta` | ✅ | ❌ | ❌ | ❌ |
| `administrar_roles` | ✅ | ❌ | ❌ | ❌ |

¹ El `miembro` administra solo los proyectos donde es `admin` de proyecto.

---

## 3. Funciones gobernadas por roles

### 3.1 Acciones del agente (MCP) — 16

| Función | Capacidad | Scope |
|---|---|---|
| `listar_correos` | leer | global |
| `inspeccionar_correo` | leer | global |
| `analizar_inbox` | leer | global |
| `listar_proyectos` | leer | global |
| `listar_notificaciones` | leer | global / proyecto |
| `obtener_contactos_proyecto` | leer (PII) | proyecto |
| `verificar_sla` | leer (análisis) | global |
| `resumen_proactivo` | leer (análisis) | global |
| `clasificar_mensajes_automatico` | leer (solo sugiere) | global |
| `crear_proyecto` | escribir_interno | global |
| `asignar_correo_a_proyecto` | escribir_interno | proyecto |
| `crear_insight` | escribir_interno | proyecto |
| `crear_tarea` | escribir_interno | proyecto |
| `crear_recordatorio` | escribir_interno | proyecto |
| `enviar_correo` | **enviar_externo** | proyecto |
| `enviar_notificacion` | **enviar_externo** | proyecto |

### 3.2 Endpoints REST de usuario

| Endpoint | Capacidad |
|---|---|
| `GET /api/projects` | leer |
| `GET /api/projects/{id}` | leer |
| `GET /api/projects/{id}/tasks` | leer |
| `GET /api/projects/{id}/conversations` | leer |
| `GET /api/insights` | leer |
| `GET /api/inbox` | leer |
| `GET /api/notifications` | leer |
| `GET /api/projects/{id}/attachments` | leer |
| `GET /api/attachments/{id}/download` | leer |
| `GET /api/gmail/status` | leer |
| `POST /api/text/analyze` | leer (análisis) |
| `POST /api/documents/analyze` | leer (análisis) |
| `POST /api/projects/from-document-draft` | leer (preview, no persiste) |
| `POST /api/projects/{id}/analyze-text` | leer (análisis) — *confirmar que no persiste* |
| `POST /api/projects` | escribir_interno |
| `POST /api/projects/from-document` | escribir_interno |
| `POST /api/projects/from-text` | escribir_interno |
| `POST /api/projects/{id}/tasks` | escribir_interno |
| `PUT /api/tasks/{id}` | escribir_interno |
| `DELETE /api/tasks/{id}` | escribir_interno |
| `POST /api/inbox/{conversation_id}/assign` | escribir_interno |
| `POST /api/projects/{id}/attachments` | escribir_interno |
| `POST /api/user/phone` | escribir_interno (perfil propio) |
| `GET /api/user/phone` | leer (perfil propio) |
| `GET /api/user/phones` | leer (perfil propio) |
| `DELETE /api/user/phone` | escribir_interno (perfil propio) |
| `PUT /api/projects/{id}/participants` | administrar_proyecto |
| `POST /api/projects/{id}/invite` | administrar_proyecto |
| `DELETE /api/projects/{id}/participants` | administrar_proyecto |
| `DELETE /api/attachments/{id}` | administrar_proyecto |
| `DELETE /api/projects/{id}` | administrar_proyecto (destructivo) |
| `GET /api/gmail/auth` | **administrar_cuenta** |
| `DELETE /api/gmail/disconnect` | **administrar_cuenta** |
| `POST /chat` | cualquier usuario autenticado (el filtro real se aplica por-tool dentro del agente) |

---

## 4. Funciones NO gobernadas por roles (sistema / infraestructura)

Las invocan máquinas (Google, Twilio, EventBridge), no usuarios. Se protegen con secreto/firma/verificación de origen, **no** con rol de usuario.

| Endpoint | Quién lo llama |
|---|---|
| `GET /api/gmail/callback` | Google (redirect OAuth) |
| `POST /api/gmail/push-notification` | Google Pub/Sub |
| `POST /api/gmail/register-watch` | Sistema (registro de watch de Gmail) |
| `POST /api/twilio/webhook` | Twilio (mensaje entrante) |
| `POST /api/scheduled/gmail-sync` | EventBridge (cron) |
| `POST /api/scheduled/notifications` | EventBridge (cron) |
| `POST /api/scheduled/dispatch-pending` | EventBridge (cron) |
| `GET /health` | Load balancer / monitoreo (público) |
| `DELETE /debug/cache/{session_id}` | Desarrollo |
| `POST /api/test-whatsapp` | Desarrollo / pruebas |

---

## 5. Matriz resumen: rol global × acción

✅ permitido · ❌ denegado · 🔒 permitido solo si el rol de proyecto también lo habilita (§1.3)

| Acción / grupo | `propietario` | `miembro` | `restringido` | `lector` |
|---|:---:|:---:|:---:|:---:|
| Lectura y análisis (todas las de `leer`) | ✅ | ✅ | ✅ | ✅ |
| `obtener_contactos_proyecto` (PII) | ✅ | 🔒 | 🔒 | 🔒 |
| `crear_proyecto` / crear proyectos REST | ✅ | ✅ | ✅ | ❌ |
| Escritura en proyecto (tareas, insights, asignar, recordatorios, adjuntos) | ✅ | 🔒 | 🔒 | ❌ |
| `enviar_correo` / `enviar_notificacion` | ✅ | 🔒 | ❌ | ❌ |
| Administrar proyecto (participantes, invitar, borrar) | ✅ | 🔒² | ❌ | ❌ |
| Conectar/desconectar Gmail | ✅ | ❌ | ❌ | ❌ |
| Asignar roles | ✅ | ❌ | ❌ | ❌ |

² Solo en proyectos donde el `miembro` es `admin` de proyecto.

---

## 6. Notas y pendientes de confirmar

1. **`analyze-text` / `analyze`**: marcados como `leer` asumiendo que solo devuelven análisis sin persistir. Confirmar en el service; si escriben en el proyecto, reclasificar a `escribir_interno`.
2. **Conectar Gmail = `administrar_cuenta`**: afecta la fuente de datos de toda la cuenta, no de un proyecto; por eso se reserva al `propietario`.
3. **`/chat` no se gobierna como una sola capacidad**: cualquier usuario autenticado puede conversar; el control real ocurre por-tool dentro del agente (`execute_tool`). Es el punto único de enforcement del lado del agente.
4. **Tools con `project_id` opcional** (`enviar_notificacion`, `enviar_correo`, `crear_recordatorio`, `listar_notificaciones`): si no llega `project_id` caen al rol global. Reforzar en `catalog.py` que la comunicación externa siempre lleve `project_id` para que no se esquive el filtro por proyecto.
5. **Fail-closed**: cualquier función nueva que no se clasifique aquí queda bloqueada hasta etiquetarla.
