
import os
import re
import json
import uuid
import requests
import boto3
from datetime import datetime
from typing import Optional, List
from urllib.parse import urlencode

GMAIL_LIST_API = os.environ.get(
    "GMAIL_LIST_API", 
    "https://twjvhacirvnjr2aqp5j55ws5ri0ckgtc.lambda-url.us-east-1.on.aws/API"
)
GMAIL_INSPECT_API = os.environ.get(
    "GMAIL_INSPECT_API",
    "https://va6ackelj53zk566fbpvd4riim0hsnwn.lambda-url.us-east-1.on.aws/"
)

dynamodb = boto3.resource('dynamodb', region_name='us-east-1')
projects_table = dynamodb.Table('onebox-projects')
conversations_table = dynamodb.Table('onebox-conversations')
tasks_table = dynamodb.Table('onebox-tasks')
insights_table = dynamodb.Table('onebox-insights')

# ============================================================================
# CONTEXTO DE USUARIO POR REQUEST (CRÍTICO PARA AISLAMIENTO MULTI-TENANT)
# ----------------------------------------------------------------------------
# El agente NO debe tener un USER_ID global. Antes había una constante hardcoded
# que provocaba que CUALQUIER persona que chatease viera siempre los datos del
# mismo usuario (cross-tenant data leak grave).
#
# Ahora el endpoint /chat valida la auth y setea estos contextvars por request,
# y todas las tools leen _current_uid() / _current_email() en su lugar.
#
# Si una tool corre sin contexto seteado, _current_uid() lanza RuntimeError
# para FALLAR RUIDOSAMENTE y no leakear datos por accidente.
# ============================================================================
import contextvars

_CURRENT_UID: contextvars.ContextVar = contextvars.ContextVar('onebox_uid', default=None)
_CURRENT_EMAIL: contextvars.ContextVar = contextvars.ContextVar('onebox_email', default=None)


def set_current_user(uid: str, email: str = "") -> None:
    """Setea el contexto de usuario para esta request. Llamado por /chat."""
    _CURRENT_UID.set(uid)
    _CURRENT_EMAIL.set((email or "").lower())


def clear_current_user() -> None:
    """Limpia el contexto. Se llama en finally del endpoint."""
    _CURRENT_UID.set(None)
    _CURRENT_EMAIL.set(None)


def _current_uid() -> str:
    """Devuelve el uid del usuario que pregunta. Falla si no hay contexto."""
    uid = _CURRENT_UID.get()
    if not uid:
        raise RuntimeError(
            "Tool del agente invocada sin contexto de usuario. "
            "Esto es un bug de seguridad: el endpoint debe llamar set_current_user() antes."
        )
    return uid


def _current_email() -> str:
    return _CURRENT_EMAIL.get() or ""


# ============================================================================
# ACCESO A PROYECTOS — MISMA LÓGICA QUE EL ENDPOINT /api/projects
# ----------------------------------------------------------------------------
# El agente debe respetar exactamente los mismos permisos que la UI:
#   - own:     proyectos que el usuario creó (userId == uid)
#   - shared:  proyectos donde su email aparece en participants[]
#   - invited: proyectos con invitación aceptada para su email
#
# Sin esta lógica, las tools del agente o (a) ocultan proyectos legítimos
# (chat dice "tienes 2" cuando la UI muestra 3), o (b) leakean proyectos
# ajenos si solo filtran por participants sin checar uid.
# ============================================================================

def _accessible_project_ids(uid: str = "", email: str = "") -> set:
    """Devuelve el set de projectIds que el usuario puede ver.
    Si no se pasan uid/email, usa el contexto actual."""
    from boto3.dynamodb.conditions import Key, Attr
    uid = uid or _current_uid()
    email = (email or _current_email() or "").strip().lower()

    accessible = set()

    # 1) Own
    try:
        own = projects_table.query(
            IndexName='userId-index',
            KeyConditionExpression=Key('userId').eq(uid),
            ProjectionExpression='projectId',
        )
        accessible.update(p['projectId'] for p in own.get('Items', []))
    except Exception as e:
        print(f"[_accessible_project_ids] own query error: {e}")

    if not email:
        return accessible

    # 2) Shared por email (scan + filtro client-side porque participants es lista anidada)
    try:
        scan = projects_table.scan(
            ProjectionExpression='projectId, participants',
        )
        for p in scan.get('Items', []):
            pid = p.get('projectId')
            if not pid or pid in accessible:
                continue
            for part in (p.get('participants') or []):
                if not isinstance(part, dict):
                    continue
                if (part.get('email', '') or '').strip().lower() == email:
                    accessible.add(pid)
                    break
    except Exception as e:
        print(f"[_accessible_project_ids] shared scan error: {e}")

    # 3) Invited (invitaciones aceptadas para este email)
    try:
        inv = invitations_table.query(
            IndexName='email-index',
            KeyConditionExpression=Key('email').eq(email),
        )
        for i in inv.get('Items', []):
            if i.get('status') == 'accepted' and i.get('projectId'):
                accessible.add(i['projectId'])
    except Exception as e:
        print(f"[_accessible_project_ids] invitations query error: {e}")

    return accessible


def _has_project_access(project_id: str) -> bool:
    """Defensa en último kilómetro: ¿el usuario del contexto puede tocar este projectId?
    Cualquier tool que escriba (crear_tarea, crear_insight, etc.) debe llamarlo
    antes de tocar DynamoDB para que el LLM no pueda inyectar projectIds ajenos."""
    if not project_id:
        return False
    return project_id in _accessible_project_ids()

TWILIO_ACCOUNT_SID = os.environ.get("TWILIO_ACCOUNT_SID", "")
TWILIO_AUTH_TOKEN = os.environ.get("TWILIO_AUTH_TOKEN", "")
TWILIO_PHONE_NUMBER = os.environ.get("TWILIO_PHONE_NUMBER", "+15005550006")
TWILIO_WHATSAPP_NUMBER = os.environ.get("TWILIO_WHATSAPP_NUMBER", "whatsapp:+14155238886")
TWILIO_TEST_MODE = os.environ.get("TWILIO_TEST_MODE", "false").lower() == "true"

notifications_table = dynamodb.Table('onebox-notifications')
invitations_table = dynamodb.Table('onebox-invitations')

# Validación E.164: + seguido de 8 a 15 dígitos (el primero no es 0).
E164_REGEX = re.compile(r'^\+[1-9]\d{7,14}$')


def _normalize_e164(phone: str) -> Optional[str]:
    """Normaliza un teléfono a formato E.164. Devuelve None si no es válido.
    Evita que Twilio falle en silencio por números mal formados."""
    if not phone:
        return None
    p = str(phone).replace('whatsapp:', '').strip()
    if E164_REGEX.match(p):
        return p
    digits = re.sub(r'\D', '', p)
    if not digits:
        return None
    candidate = '+' + digits
    return candidate if E164_REGEX.match(candidate) else None


def _log_notification(now, project_id, project_name, canal, destinatario, mensaje,
                      sid='', status='unknown', error=''):
    """Registra CADA intento de envío en onebox-notifications, exitoso o no.
    status: sent/queued/failed/invalid_phone/twilio_error/not_configured/test_simulated."""
    try:
        notifications_table.put_item(Item={
            'userId': _current_uid(),
            'notificationId': f"{now}#{uuid.uuid4().hex[:8]}",
            'projectId': project_id,
            'projectName': project_name,
            'canal': canal,
            'destinatario': destinatario,
            'mensaje': mensaje,
            'twilioSid': sid,
            'status': status,
            'errorMessage': error,
            'createdAt': now,
        })
    except Exception as e:
        print(f"[Tool] no se pudo registrar notificación: {e}")


TOOLS_DESCRIPTION = """
- listar_correos(query, max_results): Busca correos en Gmail.
  Parámetros:
    - query: (opcional) Filtro de Gmail. Ej: "from:linkedin", "has:attachment", "proyecto alpha"
    - max_results: (opcional) Máximo de correos (default: 50)

- inspeccionar_correo(email_id): Inspecciona un correo específico por su ID.

- analizar_inbox(): Lee TODOS los correos sin asignar de la base de datos. No necesita parámetros.

- listar_proyectos(): Lista todos los proyectos existentes del usuario.

- crear_proyecto(name, description, type, participants, channels): Crea un nuevo proyecto.
  Parámetros:
    - name: Nombre del proyecto (requerido)
    - description: Descripción breve
    - type: Tipo (Infraestructura, Diseño, Backend, Marketing, Otro)
    - participants: Lista de participantes [{"nombre": "X", "rol": "Y"}]
    - channels: Lista de canales ["Gmail", "Slack", "WhatsApp"]

- asignar_correo_a_proyecto(conversation_id, project_id, project_name): Asigna un correo sin asignar a un proyecto existente.
  Parámetros:
    - conversation_id: ID de la conversación (de analizar_inbox)
    - project_id: ID del proyecto destino
    - project_name: Nombre del proyecto (para referencia)

- crear_insight(project_id, project_name, type, title, description, related_person, actions): Registra una acción inteligente de la IA.
  Parámetros:
    - project_id: ID del proyecto
    - project_name: Nombre del proyecto
    - type: decision | blocker | task_created | followup | risk
    - title: Título del insight
    - description: Detalle
    - related_person: Persona involucrada
    - actions: Lista de acciones tomadas

- crear_tarea(project_id, text, assigned_to, status): Crea una tarea en un proyecto.
  Parámetros:
    - project_id: ID del proyecto
    - text: Descripción de la tarea
    - assigned_to: Responsable (opcional)
    - status: pending | done | blocked

- enviar_notificacion(destinatario, mensaje, canal, project_id, project_name): Envía una notificación por WhatsApp o SMS.
  Parámetros:
    - destinatario: Número de teléfono con código país. Ej: "+34612345678"
    - mensaje: Texto del mensaje a enviar
    - canal: "whatsapp" o "sms"
    - project_id: (opcional) ID del proyecto relacionado
    - project_name: (opcional) Nombre del proyecto

- listar_notificaciones(project_id): Lista las notificaciones enviadas de un proyecto.
  Parámetros:
    - project_id: (opcional) Filtrar por proyecto. Si no se pasa, lista todas.

- enviar_correo(destinatario_email, asunto, cuerpo, project_id, project_name): Envía un correo electrónico de seguimiento.
  Parámetros:
    - destinatario_email: Email del destinatario (requerido)
    - asunto: Asunto del correo (requerido)
    - cuerpo: Contenido del correo (requerido)
    - project_id: (opcional) ID del proyecto relacionado
    - project_name: (opcional) Nombre del proyecto

- crear_recordatorio(titulo, descripcion, fecha_vencimiento, project_id, project_name, asignado_a): Crea un recordatorio/follow-up.
  Parámetros:
    - titulo: Título del recordatorio (requerido)
    - descripcion: Detalle del recordatorio
    - fecha_vencimiento: Fecha límite en formato "YYYY-MM-DD" (requerido)
    - project_id: (opcional) ID del proyecto
    - project_name: (opcional) Nombre del proyecto
    - asignado_a: (opcional) Persona responsable

- verificar_sla(): Escanea TODOS los proyectos y tareas buscando elementos bloqueados, vencidos o sin respuesta. No necesita parámetros.

- clasificar_mensajes_automatico(): Lee los mensajes sin asignar del inbox y sugiere a qué proyecto asignarlos basándose en el contenido. No necesita parámetros.

- resumen_proactivo(): Genera un resumen ejecutivo del estado de todos los proyectos, tareas pendientes, SLA y acciones sugeridas. No necesita parámetros.
"""

TOOL_MAP = {}

def register_tool(name):
    """Decorador para registrar herramientas."""
    def decorator(func):
        TOOL_MAP[name] = func
        return func
    return decorator



@register_tool("listar_correos")
def listar_correos(query: str = "", max_results: int = 50) -> dict:
    """
    Busca y lista correos de Gmail.
    """
    try:
        params = {
            'max_results': min(max_results, 100)
        }
        if query:
            params['query'] = query
        
        url = GMAIL_LIST_API + "?" + urlencode(params)
        
        print(f"[Tool] listar_correos → {url}")
        response = requests.get(url, timeout=60)
        response.raise_for_status()
        data = response.json()
        print(f"[Tool] listar_correos ← {data.get('count', 0)} correos")
        return data
    except requests.RequestException as e:
        return {"error": f"Error al listar correos: {str(e)}", "emails": []}


@register_tool("inspeccionar_correo")
def inspeccionar_correo(email_id: str) -> dict:
    """
    Inspecciona un correo específico, descarga contenido y adjuntos.
    """
    if not email_id:
        return {"error": "email_id es requerido"}
    
    try:
        url = f"{GMAIL_INSPECT_API}?email_id={email_id}"
        print(f"[Tool] inspeccionar_correo → {url}")
        response = requests.get(url, timeout=60)
        response.raise_for_status()
        data = response.json()
        print(f"[Tool] inspeccionar_correo ← OK")
        return data
    except requests.RequestException as e:
        return {"error": f"Error al inspeccionar correo: {str(e)}"}



@register_tool("analizar_inbox")
def analizar_inbox() -> dict:
    """Lee todos los correos sin asignar de DynamoDB."""
    try:
        from boto3.dynamodb.conditions import Key, Attr
        result = conversations_table.scan(
            FilterExpression=Attr('projectId').eq('unassigned') & Attr('userId').eq(_current_uid())
        )
        emails = result.get('Items', [])
        for email in emails:
            if email.get('body'):
                email['body'] = email['body'][:500]
        return {"count": len(emails), "emails": emails}
    except Exception as e:
        return {"error": str(e)}


@register_tool("crear_proyecto")
def crear_proyecto(name: str, description: str = "", type: str = "Otro", participants: list = None, channels: list = None) -> dict:
    """Crea un proyecto en DynamoDB."""
    try:
        project_id = "proj-" + uuid.uuid4().hex[:8]
        now = datetime.utcnow().isoformat()
        item = {
            'projectId': project_id,
            'userId': _current_uid(),
            'name': name,
            'description': description,
            'type': type,
            'status': 'active',
            'participants': participants or [],
            'channels': channels or ['Gmail'],
            'createdAt': now,
            'lastActivity': now
        }
        projects_table.put_item(Item=item)
        return {"success": True, "projectId": project_id, "name": name}
    except Exception as e:
        return {"error": str(e)}


@register_tool("asignar_correo_a_proyecto")
def asignar_correo_a_proyecto(conversation_id: str, project_id: str, project_name: str = "") -> dict:
    """Mueve un correo de 'unassigned' a un proyecto."""
    # SEGURIDAD: bloquear si el LLM intenta asignar a un proyecto que el
    # usuario no puede ver (no es owner, ni shared, ni invited).
    if not _has_project_access(project_id):
        return {"error": "Sin acceso a ese proyecto"}
    try:
        result = conversations_table.get_item(
            Key={'projectId': 'unassigned', 'conversationId': conversation_id}
        )
        if 'Item' not in result:
            return {"error": "Correo no encontrado"}

        item = result['Item']

        item['projectId'] = project_id
        item['status'] = 'assigned'
        conversations_table.put_item(Item=item)
        
        conversations_table.delete_item(
            Key={'projectId': 'unassigned', 'conversationId': conversation_id}
        )
        
        return {"success": True, "conversationId": conversation_id, "projectId": project_id}
    except Exception as e:
        return {"error": str(e)}


@register_tool("crear_insight")
def crear_insight(project_id: str, project_name: str, type: str, title: str, description: str = "", related_person: str = "", actions: list = None) -> dict:
    """Crea un insight en la tabla de inteligencia."""
    if not _has_project_access(project_id):
        return {"error": "Sin acceso a ese proyecto"}
    try:
        now = datetime.utcnow().isoformat()
        insight_id = f"{now}#{uuid.uuid4().hex[:8]}"
        item = {
            'userId': _current_uid(),
            'insightId': insight_id,
            'projectId': project_id,
            'projectName': project_name,
            'type': type,
            'title': title,
            'description': description,
            'actor': 'OneBox IA',
            'relatedPerson': related_person,
            'actionsTaken': actions or [],
            'status': 'new',
            'createdAt': now
        }
        insights_table.put_item(Item=item)
        return {"success": True, "insightId": insight_id, "type": type, "title": title}
    except Exception as e:
        return {"error": str(e)}


@register_tool("crear_tarea")
def crear_tarea(project_id: str, text: str, assigned_to: str = "", status: str = "pending") -> dict:
    """Crea una tarea asociada a un proyecto."""
    if not _has_project_access(project_id):
        return {"error": "Sin acceso a ese proyecto"}
    try:
        task_id = str(uuid.uuid4())
        now = datetime.utcnow().isoformat()
        item = {
            'projectId': project_id,
            'taskId': task_id,
            'userId': _current_uid(),
            'text': text,
            'status': status,
            'createdBy': 'OneBox IA',
            'assignedTo': assigned_to,
            'createdAt': now
        }
        tasks_table.put_item(Item=item)
        return {"success": True, "taskId": task_id, "text": text}
    except Exception as e:
        return {"error": str(e)}


@register_tool("listar_proyectos")
def listar_proyectos() -> dict:
    """Lista TODOS los proyectos accesibles para el usuario actual: own + shared + invited.

    IMPORTANTE: usa la misma definición de acceso que el endpoint /api/projects
    para que el chat sea consistente con el dashboard. NO leakea proyectos
    ajenos: solo devuelve aquellos donde el uid es owner, o el email aparece
    en participants, o existe invitación aceptada.
    """
    try:
        accessible_ids = _accessible_project_ids()
        if not accessible_ids:
            return {"count": 0, "projects": []}
        # Hidratar cada projectId con el item completo. Hacemos get_item uno
        # a uno (no hay batch_get cómodo aquí) — son pocos por usuario en la
        # práctica y queremos el dato fresco.
        items = []
        for pid in accessible_ids:
            r = projects_table.get_item(Key={'projectId': pid}).get('Item')
            if r:
                items.append(r)
        return {"count": len(items), "projects": items}
    except Exception as e:
        return {"error": str(e)}



@register_tool("enviar_notificacion")
def enviar_notificacion(destinatario: str, mensaje: str, canal: str = "whatsapp", project_id: str = "", project_name: str = "") -> dict:
    """Envía una notificación por WhatsApp o SMS via Twilio (o simula en modo test).
    Valida el teléfono a E.164 antes de enviar y registra cada intento (éxito o fallo)."""
    # SEGURIDAD: si viene un project_id, debe ser accesible para el usuario.
    # Si no viene (canal libre del agente), permitir.
    if project_id and not _has_project_access(project_id):
        return {"error": "Sin acceso a ese proyecto"}
    now = datetime.utcnow().isoformat()

    # 1) Validar/normalizar el teléfono ANTES de tocar Twilio (evita fallos silenciosos).
    clean = _normalize_e164(destinatario)
    if not clean:
        _log_notification(now, project_id, project_name, canal, destinatario, mensaje,
                          status='invalid_phone', error=f"Teléfono no E.164: '{destinatario}'")
        return {"error": f"Teléfono no válido: '{destinatario}' (esperado E.164, ej: +34600123456)",
                "status": "invalid_phone"}

    if canal == "whatsapp":
        from_number = TWILIO_WHATSAPP_NUMBER or "whatsapp:+14155238886"
        to_number = f"whatsapp:{clean}"
    else:
        from_number = TWILIO_PHONE_NUMBER or "+15005550006"
        to_number = clean

    print(f"[Tool] enviar_notificacion → {canal} a {to_number} (test_mode={TWILIO_TEST_MODE})")

    # 2) Enviar (o simular).
    if TWILIO_TEST_MODE:
        sid = f"SM_TEST_{uuid.uuid4().hex[:16]}"
        tw_status = "test_simulated"
        print(f"[Tool] enviar_notificacion ← SIMULADO (SID: {sid})")
    else:
        if not TWILIO_ACCOUNT_SID or not TWILIO_AUTH_TOKEN or "xxx" in TWILIO_ACCOUNT_SID:
            _log_notification(now, project_id, project_name, canal, clean, mensaje,
                              status='not_configured', error='Twilio sin credenciales')
            return {"error": "Twilio no configurado (faltan credenciales).", "status": "not_configured"}
        try:
            from twilio.rest import Client
            client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
            tw_message = client.messages.create(body=mensaje, from_=from_number, to=to_number)
            sid = tw_message.sid
            tw_status = tw_message.status
            print(f"[Tool] enviar_notificacion ← OK (SID: {sid}, status: {tw_status})")
        except Exception as e:
            # Twilio falló: lo registramos con el error real (antes se tragaba en silencio).
            _log_notification(now, project_id, project_name, canal, clean, mensaje,
                              status='twilio_error', error=str(e))
            return {"error": f"Error de Twilio: {str(e)}", "status": "twilio_error"}

    # 3) Registrar el envío (con su status real) y la conversación del proyecto.
    _log_notification(now, project_id, project_name, canal, clean, mensaje, sid=sid, status=tw_status)

    if project_id:
        try:
            conversations_table.put_item(Item={
                'projectId': project_id,
                'conversationId': f"twilio#{sid}",
                'userId': _current_uid(),
                'from': 'OneBox IA',
                'fromEmail': '',
                'subject': f'Notificación {canal.upper()} enviada',
                'body': mensaje,
                'date': now,
                'channel': canal,
                'status': 'sent',
                'createdAt': now,
            })
        except Exception:
            pass

    return {
        "success": True,
        "sid": sid,
        "status": tw_status,
        "canal": canal,
        "destinatario": clean,
        "mode": "TEST" if TWILIO_TEST_MODE else "REAL",
    }


@register_tool("listar_notificaciones")
def listar_notificaciones(project_id: str = "") -> dict:
    """Lista notificaciones enviadas, opcionalmente filtradas por proyecto.

    - Sin project_id: solo notificaciones donde el uid es dueño (las propias).
    - Con project_id: requiere que el usuario tenga acceso al proyecto. Si
      tiene acceso, devuelve TODAS las del proyecto (no solo las suyas) —
      así los invitados también ven las notificaciones del proyecto.
    """
    try:
        from boto3.dynamodb.conditions import Key, Attr
        if project_id:
            if not _has_project_access(project_id):
                return {"error": "Sin acceso a ese proyecto"}
            result = notifications_table.scan(
                FilterExpression=Attr('projectId').eq(project_id)
            )
        else:
            result = notifications_table.scan(
                FilterExpression=Attr('userId').eq(_current_uid())
            )
        items = sorted(result.get('Items', []), key=lambda x: x.get('createdAt', ''), reverse=True)
        return {"count": len(items), "notifications": items[:50]}
    except Exception as e:
        return {"error": str(e)}


@register_tool("obtener_contactos_proyecto")
def obtener_contactos_proyecto(project_id: str) -> dict:
    """Obtiene los participantes de un proyecto con sus teléfonos y tareas pendientes."""
    if not _has_project_access(project_id):
        return {"error": "Sin acceso a ese proyecto"}
    try:
        from boto3.dynamodb.conditions import Key, Attr

        result = projects_table.get_item(Key={'projectId': project_id})
        project = result.get('Item')
        if not project:
            return {"error": f"Proyecto {project_id} no encontrado"}

        # Las tareas se filtran SOLO por projectId. Antes filtrábamos también
        # por userId del usuario logueado — pero las tareas tienen userId=owner
        # del proyecto, por lo que invitados no veían las tareas de proyectos
        # compartidos. Ya validamos acceso al proyecto arriba, así que es seguro.
        tasks_result = tasks_table.scan(
            FilterExpression=Attr('projectId').eq(project_id)
        )
        all_tasks = tasks_result.get('Items', [])

        participants = project.get('participants', [])
        contactos = []
        for p in participants:
            nombre = p.get('nombre', str(p)) if isinstance(p, dict) else str(p)
            telefono = p.get('telefono', '') if isinstance(p, dict) else ''
            rol = p.get('rol', 'Miembro') if isinstance(p, dict) else 'Miembro'
            email = p.get('email', '') if isinstance(p, dict) else ''

            pending = [t for t in all_tasks if t.get('assignedTo', '') == nombre and t.get('status') in ('pending', 'in_progress', 'blocked')]

            contactos.append({
                'nombre': nombre,
                'telefono': telefono,
                'email': email,
                'rol': rol,
                'tiene_whatsapp': bool(telefono),
                'tareas_pendientes': [{'text': t.get('text', ''), 'status': t.get('status', '')} for t in pending],
                'total_pendientes': len(pending)
            })

        return {
            "project_name": project.get('name', ''),
            "project_id": project_id,
            "total_contactos": len(contactos),
            "contactos_con_telefono": len([c for c in contactos if c['tiene_whatsapp']]),
            "contactos": contactos
        }
    except Exception as e:
        return {"error": str(e)}


@register_tool("enviar_correo")
def enviar_correo(destinatario_email: str, asunto: str, cuerpo: str, project_id: str = "", project_name: str = "") -> dict:
    """Envía un correo de seguimiento (simulado en modo demo, registra en DynamoDB)."""
    try:
        now = datetime.utcnow().isoformat()
        email_sid = f"email_{uuid.uuid4().hex[:12]}"

        print(f"[Tool] enviar_correo → {destinatario_email} | Asunto: {asunto}")

        status = "sent_simulated"

        conv_id = f"email_out#{email_sid}"
        try:
            conversations_table.put_item(Item={
                'projectId': project_id or 'unassigned',
                'conversationId': conv_id,
                'userId': _current_uid(),
                'from': 'OneBox IA',
                'fromEmail': 'onebox-ia@onebox.app',
                'to': destinatario_email,
                'subject': asunto,
                'body': cuerpo,
                'date': now,
                'channel': 'gmail',
                'status': status,
                'type': 'outbound',
                'createdAt': now
            })
        except Exception:
            pass

        try:
            insight_id = f"{now}#{uuid.uuid4().hex[:8]}"
            insights_table.put_item(Item={
                'userId': _current_uid(),
                'insightId': insight_id,
                'projectId': project_id,
                'projectName': project_name,
                'type': 'followup',
                'title': f'Correo de seguimiento enviado a {destinatario_email}',
                'description': f'Asunto: {asunto}',
                'actor': 'OneBox IA',
                'relatedPerson': destinatario_email,
                'actionsTaken': [f'Envió correo: {asunto}'],
                'status': 'executed',
                'createdAt': now
            })
        except Exception:
            pass

        print(f"[Tool] enviar_correo ← OK (ID: {email_sid})")
        return {
            "success": True,
            "email_id": email_sid,
            "status": status,
            "destinatario": destinatario_email,
            "asunto": asunto,
            "mode": "SIMULATED"
        }
    except Exception as e:
        return {"error": f"Error enviando correo: {str(e)}"}


@register_tool("crear_recordatorio")
def crear_recordatorio(titulo: str, descripcion: str = "", fecha_vencimiento: str = "", project_id: str = "", project_name: str = "", asignado_a: str = "") -> dict:
    """Crea un recordatorio/follow-up con fecha de vencimiento."""
    try:
        now = datetime.utcnow().isoformat()
        reminder_id = f"rem_{uuid.uuid4().hex[:8]}"

        print(f"[Tool] crear_recordatorio → {titulo} | Vence: {fecha_vencimiento}")

        item = {
            'projectId': project_id or 'general',
            'taskId': reminder_id,
            'userId': _current_uid(),
            'text': titulo,
            'description': descripcion,
            'status': 'pending',
            'type': 'reminder',
            'createdBy': 'OneBox IA',
            'assignedTo': asignado_a,
            'dueDate': fecha_vencimiento,
            'createdAt': now
        }
        tasks_table.put_item(Item=item)

        try:
            insight_id = f"{now}#{uuid.uuid4().hex[:8]}"
            insights_table.put_item(Item={
                'userId': _current_uid(),
                'insightId': insight_id,
                'projectId': project_id,
                'projectName': project_name,
                'type': 'followup',
                'title': f'Recordatorio creado: {titulo}',
                'description': f'Vence: {fecha_vencimiento}. {descripcion}',
                'actor': 'OneBox IA',
                'relatedPerson': asignado_a,
                'actionsTaken': [f'Creó recordatorio: {titulo}'],
                'status': 'new',
                'createdAt': now
            })
        except Exception:
            pass

        print(f"[Tool] crear_recordatorio ← OK (ID: {reminder_id})")
        return {
            "success": True,
            "reminder_id": reminder_id,
            "titulo": titulo,
            "fecha_vencimiento": fecha_vencimiento,
            "asignado_a": asignado_a
        }
    except Exception as e:
        return {"error": f"Error creando recordatorio: {str(e)}"}


@register_tool("verificar_sla")
def verificar_sla() -> dict:
    """Escanea tareas y proyectos buscando elementos bloqueados, vencidos o sin respuesta."""
    try:
        from boto3.dynamodb.conditions import Attr
        now = datetime.utcnow().isoformat()
        today = datetime.utcnow().strftime("%Y-%m-%d")

        print(f"[Tool] verificar_sla → Escaneando tareas y proyectos...")

        blocked_result = tasks_table.scan(
            FilterExpression=Attr('userId').eq(_current_uid()) & Attr('status').eq('blocked')
        )
        blocked_tasks = blocked_result.get('Items', [])

        all_tasks_result = tasks_table.scan(
            FilterExpression=Attr('userId').eq(_current_uid()) & Attr('status').eq('pending')
        )
        overdue_tasks = []
        for task in all_tasks_result.get('Items', []):
            due = task.get('dueDate', '')
            if due and due < today:
                overdue_tasks.append(task)

        unassigned_result = conversations_table.scan(
            FilterExpression=Attr('projectId').eq('unassigned') & Attr('userId').eq(_current_uid())
        )
        unassigned_count = len(unassigned_result.get('Items', []))

        alerts = []
        for task in blocked_tasks:
            created = task.get('createdAt', '')
            alerts.append({
                "type": "blocked",
                "severity": "high",
                "task": task.get('text', ''),
                "project_id": task.get('projectId', ''),
                "assigned_to": task.get('assignedTo', ''),
                "blocked_since": created
            })

        for task in overdue_tasks:
            alerts.append({
                "type": "overdue",
                "severity": "high",
                "task": task.get('text', ''),
                "project_id": task.get('projectId', ''),
                "due_date": task.get('dueDate', ''),
                "assigned_to": task.get('assignedTo', '')
            })

        if unassigned_count > 0:
            alerts.append({
                "type": "unassigned_inbox",
                "severity": "medium",
                "count": unassigned_count,
                "message": f"Hay {unassigned_count} mensajes sin asignar en el inbox"
            })

        print(f"[Tool] verificar_sla ← {len(alerts)} alertas encontradas")
        return {
            "success": True,
            "total_alerts": len(alerts),
            "blocked_tasks": len(blocked_tasks),
            "overdue_tasks": len(overdue_tasks),
            "unassigned_inbox": unassigned_count,
            "alerts": alerts[:20] 
        }
    except Exception as e:
        return {"error": f"Error verificando SLA: {str(e)}"}


@register_tool("clasificar_mensajes_automatico")
def clasificar_mensajes_automatico() -> dict:
    """Lee mensajes sin asignar y sugiere clasificación basada en proyectos existentes."""
    try:
        from boto3.dynamodb.conditions import Key, Attr

        print(f"[Tool] clasificar_mensajes_automatico → Analizando inbox...")

        unassigned_result = conversations_table.scan(
            FilterExpression=Attr('projectId').eq('unassigned') & Attr('userId').eq(_current_uid())
        )
        unassigned = unassigned_result.get('Items', [])

        if not unassigned:
            return {"success": True, "message": "No hay mensajes sin asignar", "suggestions": []}

        projects_result = projects_table.scan(
            FilterExpression=Attr('userId').eq(_current_uid())
        )
        projects = projects_result.get('Items', [])

        if not projects:
            return {
                "success": True,
                "message": f"Hay {len(unassigned)} mensajes sin asignar pero no hay proyectos creados",
                "unassigned_count": len(unassigned),
                "suggestions": []
            }

        suggestions = []
        for msg in unassigned[:10]:  
            subject = (msg.get('subject', '') or '').lower()
            body = (msg.get('body', '') or '')[:300].lower()
            sender = msg.get('from', '') or msg.get('fromEmail', '')
            content = f"{subject} {body}"

            best_match = None
            best_score = 0

            for proj in projects:
                proj_name = (proj.get('name', '') or '').lower()
                proj_desc = (proj.get('description', '') or '').lower()
                proj_keywords = proj_name.split() + proj_desc.split()

                score = 0
                for keyword in proj_keywords:
                    if len(keyword) > 3 and keyword in content:
                        score += 1

                if score > best_score:
                    best_score = score
                    best_match = proj

            suggestions.append({
                "conversation_id": msg.get('conversationId', ''),
                "from": sender,
                "subject": msg.get('subject', ''),
                "channel": msg.get('channel', 'gmail'),
                "suggested_project": best_match.get('name', 'Sin sugerencia') if best_match and best_score > 0 else None,
                "suggested_project_id": best_match.get('projectId', '') if best_match and best_score > 0 else None,
                "confidence": "alta" if best_score >= 2 else "media" if best_score == 1 else "sin_match"
            })

        classified = len([s for s in suggestions if s.get('suggested_project')])
        print(f"[Tool] clasificar_mensajes_automatico ← {len(suggestions)} mensajes, {classified} con sugerencia")
        return {
            "success": True,
            "total_unassigned": len(unassigned),
            "analyzed": len(suggestions),
            "with_suggestion": classified,
            "suggestions": suggestions
        }
    except Exception as e:
        return {"error": f"Error clasificando mensajes: {str(e)}"}


@register_tool("resumen_proactivo")
def resumen_proactivo() -> dict:
    """Genera un resumen ejecutivo del estado de todos los proyectos y acciones sugeridas."""
    try:
        from boto3.dynamodb.conditions import Key, Attr

        print(f"[Tool] resumen_proactivo → Generando resumen...")

        proj_result = projects_table.scan(
            FilterExpression=Attr('userId').eq(_current_uid())
        )
        projects = proj_result.get('Items', [])

        tasks_result = tasks_table.scan(
            FilterExpression=Attr('userId').eq(_current_uid())
        )
        all_tasks = tasks_result.get('Items', [])

        inbox_result = conversations_table.scan(
            FilterExpression=Attr('projectId').eq('unassigned') & Attr('userId').eq(_current_uid())
        )
        unassigned = inbox_result.get('Items', [])

        insights_result = insights_table.scan(
            FilterExpression=Attr('userId').eq(_current_uid())
        )
        insights = sorted(insights_result.get('Items', []), key=lambda x: x.get('createdAt', ''), reverse=True)[:5]

        project_summaries = []
        for proj in projects:
            pid = proj.get('projectId', '')
            proj_tasks = [t for t in all_tasks if t.get('projectId') == pid]
            pending = len([t for t in proj_tasks if t.get('status') == 'pending'])
            blocked = len([t for t in proj_tasks if t.get('status') == 'blocked'])
            done = len([t for t in proj_tasks if t.get('status') == 'done'])

            project_summaries.append({
                "name": proj.get('name', ''),
                "project_id": pid,
                "status": proj.get('status', ''),
                "tasks_total": len(proj_tasks),
                "tasks_pending": pending,
                "tasks_blocked": blocked,
                "tasks_done": done,
                "channels": proj.get('channels', []),
                "last_activity": proj.get('lastActivity', '')
            })

        suggested_actions = []
        today = datetime.utcnow().strftime("%Y-%m-%d")

        for summary in project_summaries:
            if summary['tasks_blocked'] > 0:
                suggested_actions.append({
                    "action": "send_reminder",
                    "reason": f"Proyecto '{summary['name']}' tiene {summary['tasks_blocked']} tarea(s) bloqueada(s)",
                    "priority": "alta"
                })
            if summary['tasks_pending'] > 5:
                suggested_actions.append({
                    "action": "review_tasks",
                    "reason": f"Proyecto '{summary['name']}' tiene {summary['tasks_pending']} tareas pendientes acumuladas",
                    "priority": "media"
                })

        if len(unassigned) > 3:
            suggested_actions.append({
                "action": "classify_inbox",
                "reason": f"Hay {len(unassigned)} mensajes sin clasificar en el inbox",
                "priority": "alta"
            })

        overdue = [t for t in all_tasks if t.get('dueDate', '') and t.get('dueDate', '') < today and t.get('status') == 'pending']
        if overdue:
            suggested_actions.append({
                "action": "escalate_overdue",
                "reason": f"Hay {len(overdue)} tarea(s) con fecha vencida",
                "priority": "alta"
            })

        print(f"[Tool] resumen_proactivo ← {len(projects)} proyectos, {len(all_tasks)} tareas, {len(suggested_actions)} acciones sugeridas")
        return {
            "success": True,
            "projects_count": len(projects),
            "total_tasks": len(all_tasks),
            "unassigned_inbox": len(unassigned),
            "projects": project_summaries,
            "recent_insights": [{
                "title": i.get('title', ''),
                "type": i.get('type', ''),
                "date": i.get('createdAt', '')
            } for i in insights],
            "suggested_actions": suggested_actions
        }
    except Exception as e:
        return {"error": f"Error generando resumen: {str(e)}"}


def execute_tool(tool_name: str, params: dict) -> dict:
    """
    Ejecuta una herramienta por nombre.
    """
    if tool_name not in TOOL_MAP:
        return {"error": f"Herramienta desconocida: {tool_name}"}
    
    try:
        tool_func = TOOL_MAP[tool_name]
        
        if params:
            result = tool_func(**params)
        else:
            result = tool_func()
        
        return result
    
    except TypeError as e:
        return {"error": f"Parámetros inválidos para {tool_name}: {str(e)}"}
    except Exception as e:
        return {"error": f"Error ejecutando {tool_name}: {str(e)}"}