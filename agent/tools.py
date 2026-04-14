
import os
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

USER_ID = "7458a478-e071-70ff-d1af-8d513f275621"

TWILIO_ACCOUNT_SID = os.environ.get("TWILIO_ACCOUNT_SID", "")
TWILIO_AUTH_TOKEN = os.environ.get("TWILIO_AUTH_TOKEN", "")
TWILIO_PHONE_NUMBER = os.environ.get("TWILIO_PHONE_NUMBER", "+15005550006")
TWILIO_WHATSAPP_NUMBER = os.environ.get("TWILIO_WHATSAPP_NUMBER", "whatsapp:+14155238886")
TWILIO_TEST_MODE = os.environ.get("TWILIO_TEST_MODE", "false").lower() == "true"

notifications_table = dynamodb.Table('onebox-notifications')


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
            FilterExpression=Attr('projectId').eq('unassigned') & Attr('userId').eq(USER_ID)
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
            'userId': USER_ID,
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
    try:
        now = datetime.utcnow().isoformat()
        insight_id = f"{now}#{uuid.uuid4().hex[:8]}"
        item = {
            'userId': USER_ID,
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
    try:
        task_id = str(uuid.uuid4())
        now = datetime.utcnow().isoformat()
        item = {
            'projectId': project_id,
            'taskId': task_id,
            'userId': USER_ID,
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
    """Lista todos los proyectos del usuario."""
    try:
        from boto3.dynamodb.conditions import Key
        result = projects_table.query(
            IndexName='userId-index',
            KeyConditionExpression=Key('userId').eq(USER_ID)
        )
        return {"count": len(result['Items']), "projects": result['Items']}
    except Exception as e:
        return {"error": str(e)}



@register_tool("enviar_notificacion")
def enviar_notificacion(destinatario: str, mensaje: str, canal: str = "whatsapp", project_id: str = "", project_name: str = "") -> dict:
    """Envía una notificación por WhatsApp o SMS via Twilio (o simula en modo test)."""
    try:
        now = datetime.utcnow().isoformat()

        if canal == "whatsapp":
            from_number = TWILIO_WHATSAPP_NUMBER or "whatsapp:+14155238886"
            to_number = f"whatsapp:{destinatario}" if not destinatario.startswith("whatsapp:") else destinatario
        else:
            from_number = TWILIO_PHONE_NUMBER or "+15005550006"
            to_number = destinatario

        print(f"[Tool] enviar_notificacion → {canal} a {to_number} (test_mode={TWILIO_TEST_MODE})")

        if TWILIO_TEST_MODE:
            fake_sid = f"SM_TEST_{uuid.uuid4().hex[:16]}"
            tw_status = "test_simulated"
            print(f"[Tool] enviar_notificacion ← SIMULADO (SID: {fake_sid})")
        else:
            if not TWILIO_ACCOUNT_SID or not TWILIO_AUTH_TOKEN or "xxx" in TWILIO_ACCOUNT_SID:
                return {"error": "Twilio no configurado. Añade TWILIO_ACCOUNT_SID y TWILIO_AUTH_TOKEN reales al .env, o usa TWILIO_TEST_MODE=true"}

            from twilio.rest import Client
            client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)

            tw_message = client.messages.create(
                body=mensaje,
                from_=from_number,
                to=to_number
            )
            fake_sid = tw_message.sid
            tw_status = tw_message.status
            print(f"[Tool] enviar_notificacion ← OK (SID: {fake_sid})")

        notification_id = f"{now}#{uuid.uuid4().hex[:8]}"
        try:
            notifications_table.put_item(Item={
                'userId': USER_ID,
                'notificationId': notification_id,
                'projectId': project_id,
                'projectName': project_name,
                'canal': canal,
                'destinatario': destinatario,
                'mensaje': mensaje,
                'twilioSid': fake_sid,
                'status': tw_status,
                'createdAt': now
            })
        except Exception:
            pass 

        if project_id:
            conv_id = f"twilio#{fake_sid}"
            try:
                conversations_table.put_item(Item={
                    'projectId': project_id,
                    'conversationId': conv_id,
                    'userId': USER_ID,
                    'from': 'OneBox IA',
                    'fromEmail': '',
                    'subject': f'Notificación {canal.upper()} enviada',
                    'body': mensaje,
                    'date': now,
                    'channel': canal,
                    'status': 'sent',
                    'createdAt': now
                })
            except Exception:
                pass

        mode_label = "TEST" if TWILIO_TEST_MODE else "REAL"
        return {
            "success": True,
            "sid": fake_sid,
            "status": tw_status,
            "canal": canal,
            "destinatario": destinatario,
            "mode": mode_label
        }
    except Exception as e:
        return {"error": f"Error enviando {canal}: {str(e)}"}


@register_tool("listar_notificaciones")
def listar_notificaciones(project_id: str = "") -> dict:
    """Lista notificaciones enviadas, opcionalmente filtradas por proyecto."""
    try:
        from boto3.dynamodb.conditions import Key, Attr
        if project_id:
            result = notifications_table.scan(
                FilterExpression=Attr('projectId').eq(project_id) & Attr('userId').eq(USER_ID)
            )
        else:
            result = notifications_table.scan(
                FilterExpression=Attr('userId').eq(USER_ID)
            )
        items = sorted(result.get('Items', []), key=lambda x: x.get('createdAt', ''), reverse=True)
        return {"count": len(items), "notifications": items[:50]}
    except Exception as e:
        return {"error": str(e)}


@register_tool("obtener_contactos_proyecto")
def obtener_contactos_proyecto(project_id: str) -> dict:
    """Obtiene los participantes de un proyecto con sus teléfonos y tareas pendientes."""
    try:
        from boto3.dynamodb.conditions import Key, Attr

        result = projects_table.get_item(Key={'projectId': project_id})
        project = result.get('Item')
        if not project:
            return {"error": f"Proyecto {project_id} no encontrado"}

        tasks_result = tasks_table.scan(
            FilterExpression=Attr('projectId').eq(project_id) & Attr('userId').eq(USER_ID)
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
                'userId': USER_ID,
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
                'userId': USER_ID,
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
            'userId': USER_ID,
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
                'userId': USER_ID,
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
            FilterExpression=Attr('userId').eq(USER_ID) & Attr('status').eq('blocked')
        )
        blocked_tasks = blocked_result.get('Items', [])

        all_tasks_result = tasks_table.scan(
            FilterExpression=Attr('userId').eq(USER_ID) & Attr('status').eq('pending')
        )
        overdue_tasks = []
        for task in all_tasks_result.get('Items', []):
            due = task.get('dueDate', '')
            if due and due < today:
                overdue_tasks.append(task)

        unassigned_result = conversations_table.scan(
            FilterExpression=Attr('projectId').eq('unassigned') & Attr('userId').eq(USER_ID)
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
            FilterExpression=Attr('projectId').eq('unassigned') & Attr('userId').eq(USER_ID)
        )
        unassigned = unassigned_result.get('Items', [])

        if not unassigned:
            return {"success": True, "message": "No hay mensajes sin asignar", "suggestions": []}

        projects_result = projects_table.scan(
            FilterExpression=Attr('userId').eq(USER_ID)
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
            FilterExpression=Attr('userId').eq(USER_ID)
        )
        projects = proj_result.get('Items', [])

        tasks_result = tasks_table.scan(
            FilterExpression=Attr('userId').eq(USER_ID)
        )
        all_tasks = tasks_result.get('Items', [])

        inbox_result = conversations_table.scan(
            FilterExpression=Attr('projectId').eq('unassigned') & Attr('userId').eq(USER_ID)
        )
        unassigned = inbox_result.get('Items', [])

        insights_result = insights_table.scan(
            FilterExpression=Attr('userId').eq(USER_ID)
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