# Propuesta de diseño — Sistema de roles y permisos (OneBox)

> Estado: **propuesta de diseño** (no implementado). Alcance acordado: modelo **híbrido** = rol global por usuario + override por proyecto.
> Base: revisión de `agent/tools.py`, `mcp/server.py` y `api/services/access.py`.

---

## 1. Estado actual (qué hay hoy)

OneBox **no tiene un sistema de roles**. El único control de acceso es a nivel de **proyecto**:

- `api/services/access.py → has_project_access(uid, email, project_id)` devuelve `(has_access, is_owner, project)`.
  Un usuario accede a un proyecto si: (1) es el `owner` (`proj.userId == uid`), (2) su email aparece en `participants[]`, o (3) tiene una invitación `accepted`.
- `agent/tools.py → _has_project_access(project_id)` es la "defensa de último kilómetro" que usan las tools de escritura antes de tocar DynamoDB, para que el LLM no inyecte `projectId` ajenos.
- El campo `participants[].rol` ("Cliente", "Desarrollador", "PM") es **texto descriptivo**, no controla permisos.

Consecuencias:

1. Existe ya una distinción binaria implícita: **owner** vs **participante**. El propio docstring de `has_project_access` dice que los endpoints administrativos "deben requerir `is_owner=True`", pero no está formalizado en roles nombrados ni aplicado de forma consistente.
2. El agente conversacional **no aplica ningún chequeo de rol**. `execute_tool(tool_name, params)` ejecuta cualquier tool sin mirar quién es el usuario; solo algunas tools de escritura llaman a `_has_project_access`. No hay forma de decir "este usuario solo puede leer" o "este usuario no puede enviar comunicación externa".
3. No hay separación entre acciones de bajo riesgo (leer) y alto riesgo (enviar WhatsApp/SMS/email al mundo exterior).

Este diseño parte de esa base y la formaliza sin romperla.

---

## 2. Principios de diseño

1. **Clasificar por capacidad y riesgo, no por tool individual.** Los roles otorgan *capacidades*; cada tool exige una capacidad. Así, añadir una tool nueva solo requiere etiquetarla, no editar cada rol.
2. **El rol más permisivo no puede saltarse el scope de proyecto.** Roles y acceso a proyecto son ortogonales: el rol dice *qué tipo de acción* puedes hacer; el acceso a proyecto dice *sobre qué datos*. Se exige pasar ambos filtros.
3. **Híbrido con techo global.** El rol global define el **máximo** de capacidades del usuario. El rol por proyecto solo puede **restringir** dentro de ese proyecto (o, para administración, elevar a admin *de ese proyecto*) — nunca otorgar una capacidad que el rol global no permite. Esto evita escaladas de privilegio.
4. **Enforcement en el punto único.** Un solo gate (`can(...)`) en `execute_tool` para el agente, espejado en los controladores de la API. No esparcir checks ad-hoc.
5. **Fail-closed.** Si no se puede determinar el rol o la capacidad de una tool no está declarada, se deniega.

---

## 3. Catálogo de acciones clasificado

Las 16 tools registradas en `agent/tools.py`, agrupadas por **capacidad** requerida y **scope**.

| # | Acción | Scope | Capacidad requerida | Riesgo |
|---|--------|-------|---------------------|--------|
| 1 | `listar_correos` | global | `read` | bajo |
| 2 | `inspeccionar_correo` | global | `read` | bajo |
| 3 | `analizar_inbox` | global | `read` | bajo |
| 4 | `listar_proyectos` | global | `read` | bajo |
| 5 | `listar_notificaciones` | proyecto* | `read` | bajo |
| 6 | `obtener_contactos_proyecto` | proyecto | `read` | bajo (PII de contacto) |
| 7 | `verificar_sla` | global | `read` (análisis) | bajo |
| 8 | `resumen_proactivo` | global | `read` (análisis) | bajo |
| 9 | `clasificar_mensajes_automatico` | global | `read` (solo sugiere) | bajo |
| 10 | `crear_proyecto` | global | `write_internal` | medio |
| 11 | `asignar_correo_a_proyecto` | proyecto | `write_internal` | medio |
| 12 | `crear_insight` | proyecto | `write_internal` | medio |
| 13 | `crear_tarea` | proyecto | `write_internal` | medio |
| 14 | `crear_recordatorio` | proyecto* | `write_internal` | medio |
| 15 | `enviar_correo` | proyecto* | `send_external` | **alto** |
| 16 | `enviar_notificacion` | proyecto* | `send_external` | **alto** (WhatsApp/SMS/email, scheduling + recurrencia) |

\* `project_id` es opcional en estas tools. Regla: si llega `project_id` → se aplica el rol **por proyecto**; si no llega → se aplica el rol **global** (acción a nivel cuenta).

Capacidades administrativas (no son tools del agente, viven en la API de proyectos: `update_participants`, `delete project`, invitaciones, gestión de roles):

| Capacidad | Acciones |
|-----------|----------|
| `manage_project` | editar/borrar proyecto, añadir/quitar participantes, invitar |
| `manage_roles` | asignar el rol global de otros usuarios y el rol por proyecto |

### Las cuatro capacidades base

- **`read`** — consultar y analizar. Sin efectos de escritura ni externos.
- **`write_internal`** — crear/modificar datos dentro de OneBox (proyectos, tareas, insights, recordatorios, asignaciones). Sin salida al exterior.
- **`send_external`** — enviar comunicación que sale al mundo (email, WhatsApp, SMS). La línea roja de riesgo.
- **`manage_project` / `manage_roles`** — administración.

---

## 4. Modelo de roles

### 4.1 Roles globales (a nivel usuario / workspace)

Definen el **techo** de capacidades del usuario en todo el sistema.

| Rol global | `read` | `write_internal` | `send_external` | `manage_project` | `manage_roles` |
|------------|:---:|:---:|:---:|:---:|:---:|
| **Owner** (dueño de la cuenta) | ✅ | ✅ | ✅ | ✅ | ✅ |
| **Operador** | ✅ | ✅ | ✅ | ➖¹ | ❌ |
| **Colaborador** | ✅ | ✅ | ❌ | ❌ | ❌ |
| **Lector / Analista** | ✅ | ❌ | ❌ | ❌ | ❌ |

¹ El Operador puede administrar **solo los proyectos donde es admin de proyecto** (ver 4.2), no todos.

Lectura del cuadro:

- **Owner**: control total, incluida la gestión de quién tiene qué rol. Hoy equivale al `proj.userId` original / dueño de la cuenta.
- **Operador**: opera el día a día completo, incluida la comunicación externa, pero no gestiona roles de otros ni borra cosas que no administra.
- **Colaborador**: trabaja dentro de OneBox (crea proyectos, tareas, insights) pero **no puede enviar nada al exterior**. Útil para perfiles junior, becarios o integraciones que solo deben organizar información.
- **Lector / Analista**: consulta y usa los resúmenes/SLA/clasificación (que solo sugieren), sin modificar ni enviar nada. Útil para stakeholders, auditoría, dashboards.

### 4.2 Roles por proyecto (override dentro de un proyecto)

Se guardan en `participants[].rol_permiso` de cada proyecto. Refinan lo que el usuario puede hacer **dentro de ese proyecto concreto**.

| Rol de proyecto | Significado |
|-----------------|-------------|
| **Project Admin** | Administra el proyecto: participantes, invitaciones, borrado. (= el actual `is_owner`) |
| **Project Editor** | Crea/edita dentro del proyecto: tareas, insights, asignaciones, recordatorios, y — si su rol global lo permite — comunicación externa del proyecto. |
| **Project Viewer** | Solo lectura del proyecto. |

### 4.3 Regla de combinación (el "híbrido")

Para una acción con `project_id`, el **permiso efectivo** se calcula así:

```
capacidad_permitida =
    capacidades(rol_global)              # techo: qué puede hacer el usuario en general
  ∩ capacidades(rol_proyecto)            # piso por proyecto: qué se le permite aquí
  ∪ extras_admin_si(rol_proyecto == Admin)   # un admin de proyecto gana manage_project SOLO de este proyecto
```

En palabras:

1. El **rol global pone el techo**. Un Lector global nunca podrá `send_external`, aunque en un proyecto sea Editor.
2. El **rol de proyecto restringe** dentro de ese proyecto. Un Operador global que en el "Proyecto X" es Viewer, en ese proyecto solo lee.
3. **Excepción de administración**: ser **Project Admin** otorga `manage_project` *de ese proyecto* aunque el rol global no sea Owner. Es la única elevación permitida, y está acotada a ese proyecto.
4. Si el usuario **no es participante** del proyecto (no tiene rol de proyecto) pero tiene acceso por otra vía, su permiso efectivo es el del **rol global filtrado a `read`** salvo que sea Owner global. (Configurable; por defecto conservador.)

Para acciones **globales** (sin `project_id`: `listar_correos`, `crear_proyecto`, `resumen_proactivo`, etc.) solo aplica el **rol global**.

---

## 5. Matriz de permisos rol global × acción

✅ permitido · ❌ denegado · 🔒 permitido solo si además el rol de proyecto lo habilita (ver §4.3)

| Acción | Owner | Operador | Colaborador | Lector |
|--------|:---:|:---:|:---:|:---:|
| `listar_correos` | ✅ | ✅ | ✅ | ✅ |
| `inspeccionar_correo` | ✅ | ✅ | ✅ | ✅ |
| `analizar_inbox` | ✅ | ✅ | ✅ | ✅ |
| `listar_proyectos` | ✅ | ✅ | ✅ | ✅ |
| `listar_notificaciones` | ✅ | ✅ | ✅ | ✅ |
| `obtener_contactos_proyecto` | ✅ | 🔒 | 🔒 | 🔒 |
| `verificar_sla` | ✅ | ✅ | ✅ | ✅ |
| `resumen_proactivo` | ✅ | ✅ | ✅ | ✅ |
| `clasificar_mensajes_automatico` | ✅ | ✅ | ✅ | ✅ |
| `crear_proyecto` | ✅ | ✅ | ✅ | ❌ |
| `asignar_correo_a_proyecto` | ✅ | 🔒 | 🔒 | ❌ |
| `crear_insight` | ✅ | 🔒 | 🔒 | ❌ |
| `crear_tarea` | ✅ | 🔒 | 🔒 | ❌ |
| `crear_recordatorio` | ✅ | 🔒 | 🔒 | ❌ |
| `enviar_correo` | ✅ | 🔒 | ❌ | ❌ |
| `enviar_notificacion` | ✅ | 🔒 | ❌ | ❌ |
| (admin) gestionar participantes / invitar / borrar proyecto | ✅ | 🔒² | ❌ | ❌ |
| (admin) asignar roles | ✅ | ❌ | ❌ | ❌ |

² Solo en proyectos donde el Operador es Project Admin.

---

## 6. Plan de enforcement en el código

Cambios mínimos, alineados con la arquitectura actual. **Un solo gate**, espejado entre agente y API.

### 6.1 Propagar el rol del usuario al contexto

`agent/tools.py → set_current_user(uid, email)` ya fija el usuario actual. Extenderlo:

```python
# agent/tools.py
_CURRENT = {"uid": "", "email": "", "global_role": "lector"}

def set_current_user(uid, email="", global_role="lector"):
    _CURRENT.update(uid=uid, email=email, global_role=(global_role or "lector"))
```

El rol global se lee de la tabla de usuarios y se pasa desde `api/controllers/chat.py` (donde hoy ya se llama `set_current_user(uid, user_email)`), y también vía header en los controladores REST.

### 6.2 Declarar capacidades y mapa de roles

Tabla declarativa (única fuente de verdad):

```python
# agent/permissions.py  (nuevo)
TOOL_CAP = {
    "listar_correos": "read", "inspeccionar_correo": "read", "analizar_inbox": "read",
    "listar_proyectos": "read", "listar_notificaciones": "read",
    "obtener_contactos_proyecto": "read", "verificar_sla": "read",
    "resumen_proactivo": "read", "clasificar_mensajes_automatico": "read",
    "crear_proyecto": "write_internal", "asignar_correo_a_proyecto": "write_internal",
    "crear_insight": "write_internal", "crear_tarea": "write_internal",
    "crear_recordatorio": "write_internal",
    "enviar_correo": "send_external", "enviar_notificacion": "send_external",
}

ROLE_CAPS = {
    "owner":       {"read", "write_internal", "send_external", "manage_project", "manage_roles"},
    "operador":    {"read", "write_internal", "send_external"},
    "colaborador": {"read", "write_internal"},
    "lector":      {"read"},
}

PROJECT_ROLE_CAPS = {
    "admin":  {"read", "write_internal", "send_external", "manage_project"},
    "editor": {"read", "write_internal", "send_external"},
    "viewer": {"read"},
}
```

### 6.3 Gate central

```python
def can(tool_name, project_id=""):
    cap = TOOL_CAP.get(tool_name)
    if cap is None:
        return False                      # fail-closed: tool sin clasificar
    global_caps = ROLE_CAPS.get(_CURRENT["global_role"], set())
    if cap not in global_caps:
        return False                      # techo global
    if project_id:                        # acción con scope de proyecto
        proj_role = _project_role_of_current_user(project_id)  # admin/editor/viewer
        eff = global_caps & PROJECT_ROLE_CAPS.get(proj_role, {"read"})
        if proj_role == "admin":
            eff |= {"manage_project"}
        return cap in eff
    return True                           # acción global, basta el techo
```

`_project_role_of_current_user` deriva el rol de proyecto leyendo `participants[].rol_permiso` (o `admin` si `proj.userId == uid`). Reutiliza `has_project_access` para no duplicar la lógica de membresía.

### 6.4 Aplicar el gate

```python
# agent/tools.py → execute_tool
def execute_tool(tool_name, params):
    if tool_name not in TOOL_MAP:
        return {"error": f"Herramienta desconocida: {tool_name}"}
    if not can(tool_name, (params or {}).get("project_id", "")):
        return {"error": "permiso_denegado",
                "detail": f"Tu rol no permite ejecutar {tool_name}."}
    ...
```

El narrator ya sabe presentar errores de "sin permiso" de forma amigable (`narrator/narrators/projects.py` ya contempla el caso "sin permiso"). En la API REST, el mismo `can(...)` se invoca en cada controlador antes de llamar al service, devolviendo `403`.

### 6.5 Esquema de datos

- **Tabla de usuarios**: añadir atributo `globalRole` (`owner | operador | colaborador | lector`); default `lector`.
- **`participants[]`** de cada proyecto: añadir `rol_permiso` (`admin | editor | viewer`); default `editor` para participantes existentes (ver migración). Se mantiene `rol` descriptivo aparte — no se mezclan.

---

## 7. Migración y compatibilidad

Para no romper a los usuarios actuales:

1. **Usuarios sin `globalRole`** → tratarlos como `owner` si son dueños de al menos un proyecto, si no como `colaborador`. (O un backfill explícito; decisión del equipo.)
2. **Dueño de proyecto (`proj.userId`)** → siempre `Project Admin` de ese proyecto, sin tocar datos: se deriva en runtime.
3. **Participantes existentes sin `rol_permiso`** → default `editor` (comportamiento actual: podían escribir). Quien quiera endurecer, baja a `viewer` manualmente.
4. **Roll-out por fases**: empezar en modo *log-only* (registrar qué se habría denegado, sin bloquear) durante unos días para detectar falsos positivos antes de activar el bloqueo real.

---

## 8. Casos límite y riesgos

- **Acción global de alto impacto sin proyecto** (`crear_proyecto`): controlada solo por rol global; el Lector no puede, el resto sí. Correcto.
- **Tools con `project_id` opcional** (`enviar_notificacion`, `enviar_correo`, `crear_recordatorio`, `listar_notificaciones`): si no se pasa `project_id`, caen al rol global. Vigilar que el planner no omita `project_id` para esquivar el filtro por proyecto — reforzar en `catalog.py` que la comunicación externa siempre lleve `project_id`.
- **El LLM no decide permisos.** El gate vive en código (`execute_tool`), no en el prompt. El planner puede *proponer* una acción prohibida; el executor la *rechaza*. Es la misma filosofía que `_has_project_access` hoy.
- **PII de contactos** (`obtener_contactos_proyecto` devuelve teléfonos/emails): por eso se marca 🔒 (requiere ser al menos Viewer del proyecto, no solo tener rol global de lectura genérica).
- **`manage_roles` concentrado en Owner**: evita que un Operador se autopromueva. Si se necesita delegar, crear un rol global intermedio en `ROLE_CAPS` sin tocar el resto del diseño.
- **Fail-closed**: cualquier tool nueva que no se añada a `TOOL_CAP` queda bloqueada hasta clasificarla. Es intencional.

---

## 9. Resumen ejecutivo

- Hoy solo hay control por proyecto (owner vs participante); el agente MCP no aplica roles.
- Se propone **4 capacidades** (`read`, `write_internal`, `send_external`, admin) y un modelo **híbrido**: 4 roles globales (Owner, Operador, Colaborador, Lector) que fijan el techo, más 3 roles por proyecto (Admin, Editor, Viewer) que restringen dentro de cada proyecto.
- La línea de riesgo clave es `send_external` (enviar WhatsApp/SMS/email): solo Owner y Operador la tienen.
- El enforcement se concentra en un único gate `can()` en `execute_tool`, reutilizando `has_project_access`, con tabla declarativa de capacidades y roll-out en modo log-only.
