# Sincronización bidireccional con Trello y Asana

Diseño propuesto. Nada implementado todavía.

## Lo que decide todo: la tabla de enlaces

Una integración bidireccional no se sostiene sobre las llamadas a la API,
sino sobre saber **qué objeto de aquí es qué objeto de allá** y **quién
escribió el último cambio**. Todo lo demás es plomería.

Tabla nueva `onebox-sync-links`:

| Campo | Ejemplo | Para qué |
|---|---|---|
| `linkId` (PK) | `asana#1201234567890` | clave por objeto externo |
| `provider` | `asana` \| `trello` | qué adaptador aplica |
| `externalId` | `1201234567890` | gid de Asana / id de card |
| `projectId` | `proj-b8f8cea7` | lado OneBox |
| `taskId` | `4120feba...` | vacío si el enlace es de proyecto↔tablero |
| `externalParentId` | gid del project / id del board | dónde vive allá |
| `syncHash` | `sha256(...)` | **supresión de eco** (ver abajo) |
| `lastSyncedAt` | ISO8601 | diagnóstico y conflictos |
| `lastDirection` | `out` \| `in` | diagnóstico |

GSI `taskId-index` para la búsqueda inversa (OneBox → externo).

## El problema difícil: el eco

OneBox escribe en Asana → Asana dispara su webhook → OneBox aplica el cambio
→ escribe en Asana → bucle infinito. Es el modo de fallo número uno de
cualquier sincronización bidireccional.

**Ventanas de tiempo no sirven.** Dependen del reloj, y con reintentos y
backoff los eventos llegan tarde y fuera de orden.

La solución robusta es por **contenido**:

1. Normalizar la tarea a una forma canónica (los campos que se sincronizan,
   ordenados, sin metadatos).
2. `syncHash = sha256(forma_canónica)`.
3. Al escribir hacia fuera, guardar el hash de lo que se escribió.
4. Al recibir un webhook, normalizar el estado entrante y calcular su hash.
   **Si es igual a `syncHash`, es nuestro propio eco: descartar.**

Es idempotente, no depende del reloj y sobrevive a reintentos y duplicados.

## Conflictos

Dos ediciones al mismo campo entre sincronizaciones. Hay que elegir política
y escribirla, porque el caso va a ocurrir.

Propuesta: **último en escribir gana, por campo**, comparando marcas de
tiempo; empate → gana OneBox.

Con una limitación real: **Trello no da marcas por campo**, solo
`dateLastActivity` de la tarjeta entera. Así que en Trello degrada a
último-gana por tarjeta completa. Hay que documentarlo, no esconderlo.

## Mapeo de estados: aquí Trello y Asana no se parecen

OneBox: `pending` | `done` | `blocked`.

**Asana** — `completed` es un booleano. `pending`/`done` mapean directo.
`blocked` **no existe**: hay que representarlo con un campo personalizado o
una etiqueta, y eso es configuración por workspace.

**Trello** — el estado *es la lista donde está la tarjeta*. No hay campo de
estado. Requiere que el usuario diga, por tablero, qué lista significa qué.
Y se rompe si alguien renombra o borra una lista.

Esa asimetría es el argumento más fuerte para la capa de adaptadores: no es
que las APIs sean distintas, es que **el modelo de datos es distinto**.

## Mapeo de responsables: hay un problema de raíz

`assignedTo` en OneBox es **texto libre con el nombre** (`"Jesus Vega"`).
Asana y Trello necesitan un id de usuario.

El puente es `participants[].email` → usuario del proveedor. Pero eso
significa que el responsable de una tarea solo se puede sincronizar si esa
persona está en `participants` **con email**, y ya vimos que se puede ser
participante solo con teléfono.

Un nombre no es un identificador estable. `resolve_person` existe justamente
por esto. Antes de sincronizar responsables conviene guardar en la tarea el
email además del nombre.

## Estructura, siguiendo el patrón que ya usas

```
api/services/sync/
    models.py          # forma canónica + syncHash
    links.py           # tabla de enlaces: buscar / upsert / ¿es eco?
    engine.py          # aplicar cambios, política de conflictos
    providers/
        base.py        # el contrato que cumplen ambos
        asana.py
        trello.py
api/controllers/sync.py   # OAuth callbacks + endpoints de webhook
```

Tokens: reusar `onebox-user-tokens` con atributos nuevos
(`asanaRefreshToken`, `trelloToken`), exactamente como `gmailRefreshToken`.

Herramientas del agente en `agent/tools/sync.py` con `@register_tool`:
`list_external_boards`, `link_project_to_board`, `sync_project_now`.
Recordar el guard de `agent/tools/__init__.py`: hay que importar el módulo
nuevo o las herramientas no se registran.

## Mecánica de webhooks (verificado contra la doc actual)

### Asana
- **Handshake**: al crear el webhook, Asana hace POST con cabecera
  `X-Hook-Secret`. Hay que **devolver la misma cabecera** y `200`/`204`.
  Recién ahí la creación devuelve `201`.
- **Firma**: HMAC-SHA256 del cuerpo completo, con el `X-Hook-Secret` del
  handshake, en `X-Hook-Signature`. Hay que guardar ese secreto por webhook.
- **Timeout: 10 segundos.** Reintentos con backoff hasta 24 h.
- **Se auto-borra** si pasa 24 h sin una entrega exitosa. Un endpoint caído
  un día entero destruye la suscripción.
- **El payload NO trae los datos.** Solo referencias al recurso cambiado;
  hay que hacer un GET aparte para leer el estado actual.

### Trello
- **Validación**: al crear el webhook, Trello hace un `HEAD` a la
  `callbackURL`. Si no responde `200`, no se crea.
- **Firma**: HMAC-SHA1 en base64 de `cuerpo + callbackURL`, con el *secret*
  de la aplicación, en `X-Trello-Webhook`.
- Reintentos: 3, con 30s / 60s / 120s.
- Se desactiva tras fallos consecutivos durante 30 días.

**Los dos exigen una URL pública HTTPS.** Tu backend corre en `localhost:8000`,
así que en desarrollo hace falta un túnel (ngrok o equivalente), y la URL
cambia en cada reinicio — lo que en Asana implica recrear el webhook y con
él el `X-Hook-Secret`.

El timeout de 10 s de Asana obliga a **responder primero y procesar después**:
verificar firma, encolar, devolver 200. Procesar dentro del request es cómo
se pierden webhooks bajo carga.

## Orden propuesto

Cada paso es útil y verificable por sí solo.

1. **Tabla de enlaces + forma canónica + Asana solo de salida.**
   Prueba el mapeo con el modelo que mejor encaja. Sin webhooks.
2. **Entrada por sondeo** (`modified_since` de Asana), todavía sin webhooks.
   Ejercita la supresión de eco donde se puede reproducir y depurar.
3. **Cambiar sondeo por webhooks.** El código de aplicación ya está probado;
   solo cambia el disparador.
4. **Añadir Trello** como segundo adaptador. Para entonces `base.py` estará
   moldeado por necesidades reales y no por adivinanzas.

Empezar por webhooks es tentador y es la peor opción: un fallo de webhook es
asíncrono e invisible, no se reproduce a voluntad. El sondeo se corre en un
bucle y se diffea.

## Cosas que hay que decidir antes de escribir código

- ¿Borrar en un lado borra en el otro, o archiva?
- ¿Qué pasa con tareas creadas en Trello en una lista sin mapear?
- ¿Las subtareas (`parentTaskId`) se sincronizan? Trello no tiene subtareas
  reales, solo checklists — y una checklist **no** es una tarjeta.
- ¿Se sincroniza por proyecto o hay proyectos que no se sincronizan?
- Límites: Asana ~150 req/min por token; Trello 300 req/10s por key y
  100 req/10s por token. Un proyecto con 200 tareas los toca.
