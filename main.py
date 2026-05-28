

from dotenv import load_dotenv
load_dotenv()


import os
import json
from agent.graph import run_agent

def lambda_handler(event, context):
    """AWS Lambda handler."""

    headers = {
        "Content-Type": "application/json",
        "Access-Control-Allow-Origin": "*",
        "Access-Control-Allow-Headers": "Content-Type",
        "Access-Control-Allow-Methods": "POST, OPTIONS"
    }

    method = event.get("requestContext", {}).get("http", {}).get("method", "")
    if method == "OPTIONS" or event.get("httpMethod") == "OPTIONS":
        return {"statusCode": 200, "headers": headers, "body": ""}

    try:
        body = json.loads(event.get("body", "{}"))
        message = body.get("message", "")
        history = body.get("history", [])

        if not message:
            return {
                "statusCode": 400,
                "headers": headers,
                "body": json.dumps({"error": "El campo 'message' es requerido"})
            }

        print(f"[Agent] Mensaje: {message}")
        print(f"[Agent] Historial: {len(history)} mensajes")

        result = run_agent(message, history)

        print(f"[Agent] Tools: {result.get('tools_used', [])}")
        print(f"[Agent] Respuesta: {result.get('response', '')[:100]}...")

        return {
            "statusCode": 200,
            "headers": headers,
            "body": json.dumps({
                "response": result["response"],
                "toolsUsed": result.get("tools_used", [])
            }, ensure_ascii=False)
        }

    except Exception as e:
        print(f"[Agent] Error: {str(e)}")
        import traceback
        traceback.print_exc()

        return {
            "statusCode": 500,
            "headers": headers,
            "body": json.dumps({
                "error": "Error interno del agente",
                "details": str(e)
            })
        }



if __name__ == "__main__":
    from fastapi import FastAPI, HTTPException, Query, Request, Header
    from fastapi.middleware.cors import CORSMiddleware
    from pydantic import BaseModel
    from typing import List, Optional
    from datetime import datetime, timedelta
    import uuid
    import uvicorn
    import boto3 as _boto3
    from boto3.dynamodb.conditions import Key, Attr
    from agent.tools import (
        projects_table, conversations_table, tasks_table,
        insights_table, notifications_table, invitations_table, USER_ID
    )
    from agent.llm import call_llm, extract_json_from_response
    dynamodb = _boto3.resource('dynamodb', region_name=os.environ.get('AWS_REGION', 'us-east-1'))

    def require_uid(uid_value: str) -> str:
        """Devuelve el userId del request o lanza 401 si falta.
        NUNCA cae en un USER_ID por defecto: ese fallback filtraba datos de una
        cuenta real a cualquiera que llamara sin identificarse (fuga entre usuarios)."""
        if not uid_value:
            raise HTTPException(status_code=401, detail="x-user-id requerido")
        return uid_value

    def get_user_id(x_user_id: str = Header(default="")) -> str:
        """Extrae el userId del header x-user-id. Lanza 401 si falta."""
        return require_uid(x_user_id)

    def scan_all_pages(table, **scan_kwargs):
        """Realiza un Scan paginado completo en una tabla DynamoDB.
        Necesario porque scan() devuelve máximo 1 MB de items y aplica el FilterExpression
        DESPUÉS de leer; sin paginar, items que coinciden con el filtro pueden quedar
        invisibles si están en páginas posteriores. Devuelve la lista completa de Items."""
        items = []
        last_key = None
        while True:
            kwargs = dict(scan_kwargs)
            if last_key:
                kwargs['ExclusiveStartKey'] = last_key
            res = table.scan(**kwargs)
            items.extend(res.get('Items', []))
            last_key = res.get('LastEvaluatedKey')
            if not last_key:
                break
        return items

    app = FastAPI(title="OneBox Agent", version="1.0.0")

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://localhost:5173", "http://localhost:3000", "https://www.oneboxmanager.com", "https://oneboxmanager.com", "https://d1mft4quq3ui5e.cloudfront.net"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    
    class ChatRequest(BaseModel):
        message: str
        history: Optional[List[dict]] = []

    class ChatResponse(BaseModel):
        response: str
        toolsUsed: List[str] = []

    class CreateProjectRequest(BaseModel):
        name: str
        description: str = ""
        type: str = "Otro"
        participants: Optional[List[dict]] = []
        channels: Optional[List[str]] = ["Gmail"]
        timing: Optional[str] = ""  # Plazo del proyecto (libre, ej: "8 semanas", "30/06/2026", "Q3 2026")
        deliveryDate: Optional[str] = ""  # Fecha de entrega ISO (opcional)

    class CreateTaskRequest(BaseModel):
        text: str
        assigned_to: str = ""
        status: str = "pending"
        description: str = ""
        start_date: Optional[str] = None   # YYYY-MM-DD (opcional)
        due_date: Optional[str] = None     # YYYY-MM-DD (opcional)

    class UpdateTaskRequest(BaseModel):
        text: Optional[str] = None
        status: Optional[str] = None
        assigned_to: Optional[str] = None
        description: Optional[str] = None
        blocked_reason: Optional[str] = None  # Motivo del bloqueo (opcional)
        start_date: Optional[str] = None      # YYYY-MM-DD
        due_date: Optional[str] = None        # YYYY-MM-DD

    class AssignRequest(BaseModel):
        projectId: str

    
    TEAM_COLORS = [
        'from-violet-500 to-indigo-600',
        'from-blue-500 to-cyan-600',
        'from-pink-500 to-rose-600',
        'from-emerald-500 to-green-600',
        'from-amber-500 to-orange-600',
        'from-sky-500 to-blue-600',
        'from-rose-500 to-red-600',
    ]

    def iniciales(nombre: str) -> str:
        if not nombre:
            return "?"
        return ''.join([w[0].upper() for w in nombre.split() if w])[:2]

    def channel_icon(name: str) -> str:
        m = {'gmail': 'email', 'email': 'email', 'whatsapp': 'whatsapp',
             'slack': 'slack', 'sms': 'sms', 'partners': 'partners'}
        return m.get(name.lower(), 'email')

   
    @app.post("/chat", response_model=ChatResponse)
    async def chat(request: ChatRequest):
        """Endpoint principal del agente."""
        try:
            result = run_agent(request.message, request.history)
            return ChatResponse(
                response=result["response"],
                toolsUsed=result.get("tools_used", [])
            )
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))

    @app.get("/health")
    async def health():
        return {"status": "ok", "agent": "OneBox Agent v1.0"}

   

    @app.get("/api/projects")
    async def get_projects(user_id: str = Header(alias="x-user-id", default=""), x_user_email: str = Header(default="")):
        """Lista todos los proyectos con datos enriquecidos (task counts, insights, etc.)."""
        uid = require_uid(user_id)
        user_email = x_user_email.lower() if x_user_email else ""
        try:
            # IMPORTANTE: usamos scan_all_pages para evitar perder items por paginación.
            # DynamoDB Scan tiene límite de 1MB por página y aplica el filtro DESPUÉS de leer.
            own_projects = scan_all_pages(
                projects_table,
                FilterExpression=Attr('userId').eq(uid)
            )
            own_ids = {p['projectId'] for p in own_projects}

            # Proyectos compartidos: solo si el email del usuario aparece EXACTAMENTE
            # como participante. Quitamos el match por nombre (muy permisivo y peligroso
            # — podía mostrar proyectos de otros usuarios por coincidencias casuales).
            shared_projects = []
            if user_email:
                all_proj_items = scan_all_pages(projects_table)
                for p in all_proj_items:
                    if p['projectId'] in own_ids:
                        continue
                    participants = p.get('participants', [])
                    for part in participants:
                        part_email = (part.get('email', '') or '').strip().lower()
                        # Solo match por EMAIL EXACTO. Nada más.
                        if part_email and part_email == user_email:
                            p['_shared'] = True
                            shared_projects.append(p)
                            break

            # Proyectos por INVITACIÓN: si el usuario tiene invitaciones pendientes
            # con su email, las marcamos como aceptadas y añadimos los proyectos.
            invited_projects = []
            if user_email:
                try:
                    inv_resp = invitations_table.query(
                        IndexName='email-index',
                        KeyConditionExpression=Key('email').eq(user_email),
                    )
                    inv_now = datetime.utcnow().isoformat()
                    seen_proj_ids = own_ids | {p['projectId'] for p in shared_projects}
                    for inv in inv_resp.get('Items', []):
                        pid = inv.get('projectId')
                        if not pid or pid in seen_proj_ids:
                            continue
                        # Auto-aceptar si está pendiente
                        if inv.get('status') == 'pending':
                            try:
                                invitations_table.update_item(
                                    Key={'invitationId': inv['invitationId']},
                                    UpdateExpression="SET #s = :s, acceptedAt = :a, acceptedBy = :b",
                                    ExpressionAttributeNames={'#s': 'status'},
                                    ExpressionAttributeValues={':s': 'accepted', ':a': inv_now, ':b': uid},
                                )
                            except Exception as e:
                                print(f"[get_projects] No se pudo auto-aceptar invitación {inv.get('invitationId')}: {e}")
                        # Cargar el proyecto y añadirlo
                        proj_item = projects_table.get_item(Key={'projectId': pid}).get('Item')
                        if proj_item:
                            proj_item['_invited'] = True
                            invited_projects.append(proj_item)
                            seen_proj_ids.add(pid)
                except Exception as e:
                    print(f"[get_projects] Error consultando invitaciones: {e}")

            projects = own_projects + shared_projects + invited_projects

            all_tasks = scan_all_pages(
                tasks_table,
                FilterExpression=Attr('userId').eq(uid)
            )

            all_insights_raw = scan_all_pages(
                insights_table,
                FilterExpression=Attr('userId').eq(uid)
            )
            all_insights = sorted(
                all_insights_raw,
                key=lambda x: x.get('createdAt', ''),
                reverse=True
            )

            today = datetime.utcnow().strftime("%Y-%m-%d")
            enriched = []

            for proj in projects:
                pid = proj['projectId']

                # Tareas de este proyecto
                proj_tasks = [t for t in all_tasks if t.get('projectId') == pid]
                done = len([t for t in proj_tasks if t.get('status') == 'done'])
                pending = len([t for t in proj_tasks if t.get('status') == 'pending'])
                blocked = len([t for t in proj_tasks if t.get('status') == 'blocked'])
                total = len(proj_tasks)

                # =====================================================
                # CÁLCULO DE % DE AVANCE CON LÓGICA DE BLOQUEOS CRÍTICOS
                # =====================================================
                # Bloqueos críticos que fuerzan progress=0:
                #   - Sin participantes en el proyecto
                #   - Más bloqueos que tareas hechas (proyecto atascado)
                # Si hay bloqueos pero también progreso, el % se penaliza.
                # Si todo está OK, cálculo normal.
                participants_list = proj.get('participants', [])
                no_participants = len(participants_list) == 0
                project_stuck = blocked > 0 and blocked >= done  # más bloqueos que avance real
                progress_blocked_reason = ''

                if total == 0:
                    progress = 0
                elif no_participants and total > 0:
                    progress = 0
                    progress_blocked_reason = 'Sin participantes asignados'
                elif project_stuck:
                    # Proyecto atascado: calculamos avance pero lo penalizamos a la mitad
                    progress = max(0, round((done / total) * 100 * 0.5))
                    progress_blocked_reason = f'{blocked} tarea(s) bloqueada(s) frenan el avance'
                else:
                    progress = round((done / total) * 100)

                proj_insights = [i for i in all_insights if i.get('projectId') == pid]

                overdue = [t for t in proj_tasks
                           if t.get('status') == 'pending' and t.get('dueDate', '') and t.get('dueDate', '') < today]
                # Lógica SLA mejorada: considerar también la falta de participantes
                if no_participants and total > 0:
                    sla = 'sla_vencido'  # Crítico: tareas pero sin equipo
                elif blocked >= 2 or len(overdue) >= 2:
                    sla = 'sla_vencido'
                elif blocked > 0 or len(overdue) > 0:
                    sla = 'en_riesgo'
                else:
                    sla = 'on_track'

                participants = proj.get('participants', [])
                team = []
                for i, p in enumerate(participants):
                    nombre = p.get('nombre', str(p)) if isinstance(p, dict) else str(p)
                    rol = p.get('rol', 'Miembro') if isinstance(p, dict) else 'Miembro'
                    email = p.get('email', '') if isinstance(p, dict) else ''
                    telefono = p.get('telefono', '') if isinstance(p, dict) else ''
                    tareas_count = len([t for t in proj_tasks if t.get('assignedTo', '') == nombre])
                    team.append({
                        'nombre': nombre,
                        'iniciales': iniciales(nombre),
                        'rol': rol,
                        'email': email,
                        'telefono': telefono,
                        'color': TEAM_COLORS[i % len(TEAM_COLORS)],
                        'tareas': tareas_count,
                    })

                raw_channels = proj.get('channels', [])
                channels = []
                for ch in raw_channels:
                    ch_name = ch if isinstance(ch, str) else ch.get('nombre', '')
                    channels.append({
                        'nombre': ch_name,
                        'icon': channel_icon(ch_name),
                        'lastActivity': '',
                        'unread': 0,
                    })

                last_action = {'detected': '', 'action': ''}
                if proj_insights:
                    latest = proj_insights[0]
                    actions_txt = ', '.join(latest.get('actionsTaken', []))
                    last_action = {
                        'detected': latest.get('title', ''),
                        'action': latest.get('description', '') or actions_txt,
                    }

                ai_actions = []
                for ins in proj_insights[:8]:
                    created = ins.get('createdAt', '')
                    time_str = created[11:16] if len(created) > 16 else created
                    actions_taken = ins.get('actionsTaken', [])
                    ai_actions.append({
                        'id': ins.get('insightId', ''),
                        'detected': ins.get('title', ''),
                        'executed': ins.get('description', '') or ', '.join(actions_taken),
                        'channel': 'IA',
                        'channelIcon': 'email',
                        'time': time_str,
                    })

                tasks_list = []
                for t in proj_tasks:
                    assigned = t.get('assignedTo', '') or 'Sin asignar'
                    # El ESTADO de la tarea es un campo propio (status), NO un tag.
                    # No lo metemos en tags para evitar la redundancia "Pendiente: Completada"
                    # (el frontend ya pinta el estado como badge a partir de status).
                    # tags solo lleva etiquetas reales: recordatorio, vencida.
                    is_overdue = bool(
                        t.get('dueDate', '') and t.get('dueDate', '') < today
                        and t.get('status') == 'pending'
                    )
                    tags = []
                    if t.get('type') == 'reminder':
                        tags.append('Recordatorio')
                    if is_overdue:
                        tags.append('Vencida')

                    tasks_list.append({
                        'id': t.get('taskId', ''),
                        'text': t.get('text', ''),
                        'status': t.get('status', 'pending'),
                        'description': t.get('description', ''),
                        'overdue': is_overdue,
                        'blockedReason': t.get('blockedReason', ''),
                        'startDate': t.get('startDate', ''),
                        'dueDate': t.get('dueDate', ''),
                        'assignedTo': {
                            'nombre': assigned,
                            'iniciales': iniciales(assigned),
                            'color': 'from-violet-500 to-indigo-600',
                        },
                        'tags': tags,
                    })

                enriched.append({
                    'projectId': pid,
                    'name': proj.get('name', ''),
                    'client': proj.get('client', ''),
                    'description': proj.get('description', ''),
                    'status': proj.get('status', 'active'),
                    'sla': sla,
                    'type': proj.get('type', 'Otro'),
                    'deliveryDate': proj.get('deliveryDate', ''),
                    'timing': proj.get('timing', ''),
                    'daysLeft': 0,
                    'progress': progress,
                    'progressBlockedReason': progress_blocked_reason,
                    'hechas': done,
                    'pendientes': pending,
                    'bloqueadas': blocked,
                    'mensajesIA': len(proj_insights),
                    'lastAction': last_action,
                    'team': team,
                    'channels': channels,
                    'etiquetas': [],
                    'slaMetrics': {
                        'respuestaCliente': '-',
                        'tareasResponsable': 0,
                        'respuestaPartner': '-',
                        'tareasBlockeadas24h': blocked,
                    },
                    'startDate': proj.get('createdAt', '')[:10],
                    'tasks': tasks_list,
                    'aiActions': ai_actions,
                    'notifications': [],
                })

            # Ordenar: activos primero, luego por nombre
            enriched.sort(key=lambda p: (0 if p['status'] == 'active' else 1, p['name']))
            return enriched

        except Exception as e:
            print(f"[API] Error en get_projects: {e}")
            import traceback; traceback.print_exc()
            raise HTTPException(status_code=500, detail=str(e))


    @app.get("/api/projects/{project_id}")
    async def get_project(project_id: str, x_user_id: str = Header(default="")):
        """Obtiene un proyecto específico con todos sus datos."""
        uid = require_uid(x_user_id)
        try:
            # Proyecto
            proj_result = projects_table.get_item(Key={'projectId': project_id})
            if 'Item' not in proj_result:
                raise HTTPException(status_code=404, detail="Proyecto no encontrado")
            proj = proj_result['Item']

            tasks_result = tasks_table.scan(
                FilterExpression=Attr('projectId').eq(project_id) & Attr('userId').eq(uid)
            )
            tasks = tasks_result.get('Items', [])

            convs_result = conversations_table.query(
                KeyConditionExpression=Key('projectId').eq(project_id)
            )
            conversations = convs_result.get('Items', [])

            insights_result = insights_table.scan(
                FilterExpression=Attr('userId').eq(uid) & Attr('projectId').eq(project_id)
            )
            insights = sorted(
                insights_result.get('Items', []),
                key=lambda x: x.get('createdAt', ''),
                reverse=True
            )

            notif_result = notifications_table.scan(
                FilterExpression=Attr('userId').eq(uid) & Attr('projectId').eq(project_id)
            )
            notifications = sorted(
                notif_result.get('Items', []),
                key=lambda x: x.get('createdAt', ''),
                reverse=True
            )

            return {
                "project": proj,
                "tasks": tasks,
                "conversations": conversations,
                "insights": insights,
                "notifications": notifications,
            }
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))


    @app.post("/api/projects")
    async def create_project(req: CreateProjectRequest, x_user_id: str = Header(default="")):
        """Crea un nuevo proyecto con análisis IA, notificaciones e insights.
        Usa la función compartida `create_project_full` (también usada por el flujo de WhatsApp)."""
        uid = require_uid(x_user_id)
        try:
            from agent.project_helpers import create_project_full
            result = create_project_full(
                user_id=uid,
                name=req.name,
                description=req.description,
                project_type=req.type,
                channels=req.channels,
                participants=req.participants,
                timing=req.timing or '',
                delivery_date=req.deliveryDate or ''
            )
            return result
        except Exception as e:
            print(f"[create_project] Error: {e}")
            import traceback; traceback.print_exc()
            raise HTTPException(status_code=500, detail=str(e))


    class UpdateParticipantsRequest(BaseModel):
        participants: List[dict]

    @app.put("/api/projects/{project_id}/participants")
    async def update_participants(project_id: str, req: UpdateParticipantsRequest, x_user_id: str = Header(default="")):
        """Actualiza los participantes de un proyecto (incluye teléfonos)."""
        uid = require_uid(x_user_id)
        try:
            projects_table.update_item(
                Key={'projectId': project_id},
                UpdateExpression="SET participants = :p",
                ExpressionAttributeValues={':p': req.participants},
                ConditionExpression=Attr('userId').eq(uid)
            )
            return {"success": True, "projectId": project_id}
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))


    class InviteRequest(BaseModel):
        email: str

    @app.post("/api/projects/{project_id}/invite")
    async def invite_user_to_project(project_id: str, req: InviteRequest, x_user_id: str = Header(default="")):
        """Invita a un usuario al proyecto: crea usuario en Cognito (le manda email
        con contraseña temporal) y guarda la invitación. Al primer login del invitado,
        el sistema le añade el proyecto automáticamente."""
        uid = require_uid(x_user_id)
        email = (req.email or '').strip().lower()
        if not email or '@' not in email:
            raise HTTPException(status_code=400, detail="Email inválido")

        # Verificar que el proyecto pertenece al usuario
        proj = projects_table.get_item(Key={'projectId': project_id}).get('Item')
        if not proj:
            raise HTTPException(status_code=404, detail="Proyecto no encontrado")
        if proj.get('userId') != uid:
            raise HTTPException(status_code=403, detail="No tienes permiso sobre este proyecto")

        # Crear usuario en Cognito (si no existe). Cognito enviará el email automáticamente.
        cognito_client = _boto3.client('cognito-idp', region_name='us-east-1')
        pool_id = os.environ.get('COGNITO_USER_POOL_ID', 'us-east-1_b76prubhx')
        cognito_user_existed = False
        email_resent = False
        user_status = None
        try:
            cognito_client.admin_create_user(
                UserPoolId=pool_id,
                Username=email,
                UserAttributes=[
                    {'Name': 'email', 'Value': email},
                    {'Name': 'email_verified', 'Value': 'true'},
                ],
                DesiredDeliveryMediums=['EMAIL'],
            )
        except cognito_client.exceptions.UsernameExistsException:
            cognito_user_existed = True
            # El usuario ya existe. Si todavía no activó la cuenta
            # (FORCE_CHANGE_PASSWORD), re-enviamos el email de invitación con
            # MessageAction=RESEND para recordarle que tiene proyectos esperando.
            try:
                u = cognito_client.admin_get_user(UserPoolId=pool_id, Username=email)
                user_status = u.get('UserStatus')
                if user_status == 'FORCE_CHANGE_PASSWORD':
                    cognito_client.admin_create_user(
                        UserPoolId=pool_id,
                        Username=email,
                        MessageAction='RESEND',
                        DesiredDeliveryMediums=['EMAIL'],
                    )
                    email_resent = True
                    print(f"[invite] Email reenviado a {email} (estaba en FORCE_CHANGE_PASSWORD)")
            except Exception as inner:
                # No bloqueamos el flujo si el resend falla; la invitación queda guardada.
                print(f"[invite] No se pudo reenviar el email: {inner}")
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Cognito error: {str(e)}")

        # Guardar invitación (pendiente). Si ya estaba aceptada, no duplicamos.
        now = datetime.utcnow().isoformat()
        invitation_id = str(uuid.uuid4())
        try:
            invitations_table.put_item(Item={
                'invitationId': invitation_id,
                'email': email,
                'projectId': project_id,
                'projectName': proj.get('name', ''),
                'invitedBy': uid,
                'status': 'pending',
                'createdAt': now,
            })
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"DynamoDB error: {str(e)}")

        # Mensaje contextual según el caso
        if not cognito_user_existed:
            msg = "Invitación enviada. El usuario recibirá un correo con su contraseña temporal."
        elif email_resent:
            msg = "El usuario ya tenía cuenta pendiente de activar. Le reenviamos el correo de invitación."
        elif user_status == 'CONFIRMED':
            msg = "El usuario ya tiene cuenta activa. El proyecto le aparecerá la próxima vez que inicie sesión."
        else:
            msg = "Invitación registrada. El proyecto le aparecerá al iniciar sesión."

        return {
            "success": True,
            "invitationId": invitation_id,
            "email": email,
            "cognitoUserExisted": cognito_user_existed,
            "emailResent": email_resent,
            "userStatus": user_status,
            "message": msg,
        }


    @app.delete("/api/projects/{project_id}")
    async def delete_project(project_id: str, x_user_id: str = Header(default="")):
        """Elimina un proyecto y sus datos relacionados (insights, notificaciones, tareas)."""
        uid = require_uid(x_user_id)
        try:
            # Verificar que el proyecto pertenece al usuario
            existing = projects_table.get_item(Key={'projectId': project_id}).get('Item')
            if not existing:
                raise HTTPException(status_code=404, detail="Proyecto no encontrado")
            if existing.get('userId') != uid:
                raise HTTPException(status_code=403, detail="No tienes permiso para eliminar este proyecto")

            project_name = existing.get('name', 'Proyecto')

            # Eliminar el proyecto
            projects_table.delete_item(Key={'projectId': project_id})
            print(f"[delete_project] Proyecto {project_id} eliminado")

            # Eliminar insights asociados
            try:
                insights_to_delete = insights_table.scan(
                    FilterExpression=Attr('userId').eq(uid) & Attr('projectId').eq(project_id),
                    ProjectionExpression='userId,insightId'
                ).get('Items', [])
                for ins in insights_to_delete:
                    insights_table.delete_item(Key={'userId': ins['userId'], 'insightId': ins['insightId']})
                print(f"[delete_project] {len(insights_to_delete)} insights eliminados")
            except Exception as e:
                print(f"[delete_project] Error borrando insights: {e}")

            # Eliminar notificaciones asociadas
            try:
                notifs_to_delete = notifications_table.query(
                    KeyConditionExpression=Key('userId').eq(uid),
                    FilterExpression=Attr('projectId').eq(project_id),
                    ProjectionExpression='userId,notificationId'
                ).get('Items', [])
                for n in notifs_to_delete:
                    notifications_table.delete_item(Key={'userId': n['userId'], 'notificationId': n['notificationId']})
                print(f"[delete_project] {len(notifs_to_delete)} notificaciones eliminadas")
            except Exception as e:
                print(f"[delete_project] Error borrando notificaciones: {e}")

            # Eliminar tareas asociadas
            try:
                tasks_to_delete = tasks_table.query(
                    KeyConditionExpression=Key('projectId').eq(project_id),
                    ProjectionExpression='projectId,taskId'
                ).get('Items', [])
                for t in tasks_to_delete:
                    tasks_table.delete_item(Key={'projectId': t['projectId'], 'taskId': t['taskId']})
                print(f"[delete_project] {len(tasks_to_delete)} tareas eliminadas")
            except Exception as e:
                print(f"[delete_project] Error borrando tareas: {e}")

            return {"success": True, "projectId": project_id, "name": project_name}

        except HTTPException:
            raise
        except Exception as e:
            print(f"[delete_project] Error: {e}")
            import traceback; traceback.print_exc()
            raise HTTPException(status_code=500, detail=str(e))


    # ========================================================================
    # ATTACHMENTS / DOCUMENTOS
    # ========================================================================
    from fastapi import UploadFile, File, Form

    attachments_table = dynamodb.Table('onebox-attachments')

    def _save_attachment_record(project_id: str, user_id: str, file_name: str,
                                 file_size: int, content_type: str, ext: str,
                                 s3_key: str, extracted_text: str = "",
                                 source: str = "web") -> dict:
        """Guarda metadata del adjunto en DynamoDB."""
        now = datetime.utcnow().isoformat()
        attachment_id = f"{now}#{uuid.uuid4().hex[:8]}"
        item = {
            'projectId': project_id,
            'attachmentId': attachment_id,
            'userId': user_id,
            'fileName': file_name,
            'fileSize': file_size,
            'contentType': content_type,
            'extension': ext,
            's3Key': s3_key,
            'extractedTextPreview': (extracted_text or '')[:500],
            'extractedTextLength': len(extracted_text or ''),
            'source': source,
            'createdAt': now,
        }
        attachments_table.put_item(Item=item)
        return item

    class AnalyzeTextPreviewRequest(BaseModel):
        text: str
        source: Optional[str] = "paste"

    @app.post("/api/text/analyze")
    async def analyze_text_preview(req: AnalyzeTextPreviewRequest, x_user_id: str = Header(default="")):
        """Analiza un texto pegado SIN crear proyecto. Devuelve draftId + sugerencia.
        Equivalente a /api/documents/analyze pero para texto. Reusa /api/projects/from-document-draft
        para confirmar."""
        from agent.document_parser import analyze_document_for_project, upload_to_s3

        uid = require_uid(x_user_id)
        try:
            text = (req.text or '').strip()
            if len(text) < 30:
                raise HTTPException(status_code=400, detail="El texto es muy corto. Pega al menos una conversación o un párrafo.")

            # Sugerir metadata con IA
            analysis = analyze_document_for_project(text)

            # Guardar como draft .txt en S3 + DynamoDB (igual que un documento)
            draft_id = uuid.uuid4().hex
            source = req.source or 'paste'
            now = datetime.utcnow().isoformat()
            file_name = f"texto-pegado-{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}.txt"
            text_bytes = text.encode('utf-8')
            s3_key = upload_to_s3(text_bytes, f"_drafts/{uid}", file_name, 'text/plain')

            attachments_table.put_item(Item={
                'projectId': f'_draft#{uid}',
                'attachmentId': draft_id,
                'userId': uid,
                'fileName': file_name,
                'fileSize': len(text_bytes),
                'contentType': 'text/plain',
                'extension': 'txt',
                's3Key': s3_key,
                'extractedTextPreview': text[:5000],
                'extractedTextLength': len(text),
                'source': f'web_draft_{source}',
                'createdAt': now,
            })

            return {
                "draftId": draft_id,
                "fileName": file_name,
                "fileSize": len(text_bytes),
                "extractedTextLength": len(text),
                "suggestion": {
                    "name": analysis['name'],
                    "type": analysis['type'],
                    "description": analysis['description'],
                    "extractedNotes": analysis.get('extractedNotes', ''),
                    "detected_participants": analysis.get('detected_participants', []),
                }
            }

        except HTTPException:
            raise
        except Exception as e:
            print(f"[analyze_text_preview] Error: {e}")
            import traceback; traceback.print_exc()
            raise HTTPException(status_code=500, detail=str(e))


    @app.post("/api/documents/analyze")
    async def analyze_document_preview(
        file: UploadFile = File(...),
        x_user_id: str = Header(default="")
    ):
        """Analiza un documento (extrae texto + sugiere metadata) SIN crear el proyecto.
        El frontend muestra el preview, el usuario revisa/edita y luego confirma.
        Devuelve un draft_id que se usará después en /api/projects/from-document-draft."""
        from agent.document_parser import (
            validate_file, extract_text, analyze_document_for_project, upload_to_s3
        )

        uid = require_uid(x_user_id)
        try:
            file_bytes = await file.read()
            valid, ext, error = validate_file(file_bytes, file.filename or '', file.content_type or '')
            if not valid:
                raise HTTPException(status_code=400, detail=error)

            text = extract_text(file_bytes, ext)
            if not text or len(text.strip()) < 20:
                raise HTTPException(status_code=400, detail="No se pudo extraer texto del documento o es muy breve.")

            # Sugerir metadata con IA
            analysis = analyze_document_for_project(text)

            # Subir el archivo a un "draft" en S3 para confirmación posterior
            draft_id = uuid.uuid4().hex
            s3_key = upload_to_s3(file_bytes, f"_drafts/{uid}", file.filename or f'doc.{ext}', file.content_type or '')

            # Guardar el draft en DynamoDB attachments con projectId especial "_draft"
            attachments_table.put_item(Item={
                'projectId': f'_draft#{uid}',
                'attachmentId': draft_id,
                'userId': uid,
                'fileName': file.filename or f'doc.{ext}',
                'fileSize': len(file_bytes),
                'contentType': file.content_type or '',
                'extension': ext,
                's3Key': s3_key,
                'extractedTextPreview': text[:5000],
                'extractedTextLength': len(text),
                'source': 'web_draft',
                'createdAt': datetime.utcnow().isoformat(),
            })

            return {
                "draftId": draft_id,
                "fileName": file.filename or f'doc.{ext}',
                "fileSize": len(file_bytes),
                "extractedTextLength": len(text),
                "suggestion": {
                    "name": analysis['name'],
                    "type": analysis['type'],
                    "description": analysis['description'],
                    "extractedNotes": analysis.get('extractedNotes', ''),
                    "detected_participants": analysis.get('detected_participants', []),
                }
            }

        except HTTPException:
            raise
        except Exception as e:
            print(f"[analyze_document] Error: {e}")
            import traceback; traceback.print_exc()
            raise HTTPException(status_code=500, detail=str(e))


    class CreateProjectFromDraftRequest(BaseModel):
        draftId: str
        name: str
        type: Optional[str] = "Otro"
        description: str
        channels: List[str] = []
        emails: Optional[List[str]] = []
        phones: Optional[List[str]] = []
        timing: Optional[str] = ""
        deliveryDate: Optional[str] = ""
        # Participantes detectados por IA: cada uno con {name, email, phone, role}
        # Permite preservar el nombre real (Kevin/Mateo) en lugar de usar el email como nombre.
        detectedParticipants: Optional[List[dict]] = []

    @app.post("/api/projects/from-document-draft")
    async def create_project_from_draft(req: CreateProjectFromDraftRequest, x_user_id: str = Header(default="")):
        """Crea el proyecto definitivo a partir de un draft analizado previamente.
        Mueve el archivo del draft a la carpeta del proyecto y registra el adjunto."""
        from agent.document_parser import S3_ATTACHMENTS_BUCKET, get_s3_client
        from agent.project_helpers import create_project_full

        uid = require_uid(x_user_id)
        try:
            # Recuperar el draft
            draft = attachments_table.get_item(
                Key={'projectId': f'_draft#{uid}', 'attachmentId': req.draftId}
            ).get('Item')
            if not draft:
                raise HTTPException(status_code=404, detail="Borrador no encontrado o expirado")

            # Validaciones mínimas
            name = (req.name or '').strip()
            if not name:
                raise HTTPException(status_code=400, detail="El nombre del proyecto es requerido")
            if not req.channels or len(req.channels) == 0:
                raise HTTPException(status_code=400, detail="Selecciona al menos un canal")

            # Construir participantes
            participants = []
            seen_emails = set()
            seen_phones = set()

            # 1. Participantes detectados por IA (preservan el nombre original: Kevin, Mateo...)
            for p in (req.detectedParticipants or []):
                if not isinstance(p, dict):
                    continue
                pname = (p.get('name') or '').strip()[:80]
                pemail = (p.get('email') or '').strip().lower()
                pphone_raw = (p.get('phone') or '').strip()
                prole = (p.get('role') or 'Participante').strip()[:80]
                pphone = ''
                if pphone_raw:
                    pphone = pphone_raw if pphone_raw.startswith('+') else '+' + pphone_raw.replace(' ', '').replace('-', '')
                # Solo agregar si tiene nombre y al menos un canal de contacto (o solo nombre como referencia)
                if pname or pemail or pphone:
                    participants.append({
                        'nombre': pname or (pemail.split('@')[0] if pemail else pphone),
                        'email': pemail if '@' in pemail else '',
                        'telefono': pphone,
                        'rol': prole or 'Participante'
                    })
                    if pemail and '@' in pemail:
                        seen_emails.add(pemail)
                    if pphone:
                        seen_phones.add(pphone)

            # 2. Emails sueltos agregados manualmente (que NO vengan de detectados)
            for email in (req.emails or []):
                e = (email or '').strip().lower()
                if e and '@' in e and e not in seen_emails:
                    participants.append({
                        'nombre': e.split('@')[0],
                        'email': e,
                        'telefono': '',
                        'rol': 'Contacto Email'
                    })
                    seen_emails.add(e)

            # 3. Teléfonos sueltos agregados manualmente
            for phone in (req.phones or []):
                pclean = (phone or '').strip()
                if pclean:
                    formatted = pclean if pclean.startswith('+') else '+' + pclean
                    if formatted not in seen_phones:
                        participants.append({
                            'nombre': formatted,
                            'email': '',
                            'telefono': formatted,
                            'rol': 'Contacto WhatsApp'
                        })
                        seen_phones.add(formatted)

            # Crear el proyecto con la info revisada por el usuario
            result = create_project_full(
                user_id=uid,
                name=name,
                description=req.description or '',
                project_type=req.type or 'Otro',
                channels=req.channels,
                participants=participants,
                timing=req.timing or '',
                delivery_date=req.deliveryDate or ''
            )
            project_id = result['projectId']

            # Mover el archivo del draft a la carpeta del proyecto definitivo
            try:
                s3 = get_s3_client()
                old_key = draft['s3Key']
                # Nuevo key con la estructura normal de proyecto
                new_key = old_key.replace(f'projects/_drafts/{uid}', f'projects/{project_id}/{datetime.utcnow().strftime("%Y%m%d")}')
                s3.copy_object(
                    Bucket=S3_ATTACHMENTS_BUCKET,
                    CopySource={'Bucket': S3_ATTACHMENTS_BUCKET, 'Key': old_key},
                    Key=new_key,
                    ServerSideEncryption='AES256'
                )
                s3.delete_object(Bucket=S3_ATTACHMENTS_BUCKET, Key=old_key)

                # Registrar el adjunto definitivo
                _save_attachment_record(
                    project_id=project_id,
                    user_id=uid,
                    file_name=draft.get('fileName', 'documento'),
                    file_size=int(draft.get('fileSize', 0)),
                    content_type=draft.get('contentType', ''),
                    ext=draft.get('extension', ''),
                    s3_key=new_key,
                    extracted_text=draft.get('extractedTextPreview', ''),
                    source='web'
                )

                # Borrar el registro del draft
                attachments_table.delete_item(
                    Key={'projectId': f'_draft#{uid}', 'attachmentId': req.draftId}
                )
            except Exception as e:
                print(f"[from_draft] Error moviendo draft: {e}")
                import traceback; traceback.print_exc()
                # No fallar la creación si el move falla

            return result

        except HTTPException:
            raise
        except Exception as e:
            print(f"[from_draft] Error: {e}")
            import traceback; traceback.print_exc()
            raise HTTPException(status_code=500, detail=str(e))


    @app.post("/api/projects/from-document")
    async def create_project_from_document(
        file: UploadFile = File(...),
        name: Optional[str] = Form(None),
        channels: Optional[str] = Form(None),  # CSV: "Gmail,WhatsApp"
        x_user_id: str = Header(default="")
    ):
        """Crea un proyecto a partir de un documento. La IA infiere nombre, tipo
        y descripción si no se proveen. El documento queda anexado al proyecto."""
        from agent.document_parser import (
            validate_file, extract_text, analyze_document_for_project, upload_to_s3
        )
        from agent.project_helpers import create_project_full

        uid = require_uid(x_user_id)
        try:
            file_bytes = await file.read()
            valid, ext, error = validate_file(file_bytes, file.filename or '', file.content_type or '')
            if not valid:
                raise HTTPException(status_code=400, detail=error)

            # Extraer texto
            print(f"[from_document] Extrayendo texto de {file.filename} ({len(file_bytes)} bytes, ext={ext})")
            text = extract_text(file_bytes, ext)
            if not text or len(text.strip()) < 20:
                raise HTTPException(status_code=400, detail="No se pudo extraer texto del documento o es demasiado breve.")
            print(f"[from_document] Texto extraído: {len(text)} caracteres")

            # Analizar con IA si no se dio nombre/tipo
            analysis = analyze_document_for_project(text, fallback_name=name or '')
            project_name = (name or analysis['name']).strip()[:80]
            project_type = analysis['type']
            description = analysis['description']
            if analysis.get('extractedNotes'):
                description += "\n\nNotas: " + analysis['extractedNotes']

            # Parsear canales
            channel_list = []
            if channels:
                channel_list = [c.strip() for c in channels.split(',') if c.strip()]
            if not channel_list:
                channel_list = ['Gmail']

            # Crear proyecto + insights
            result = create_project_full(
                user_id=uid,
                name=project_name,
                description=description,
                project_type=project_type,
                channels=channel_list,
                participants=[]
            )
            project_id = result['projectId']

            # Subir archivo a S3
            s3_key = upload_to_s3(file_bytes, project_id, file.filename or f'doc.{ext}', file.content_type or '')

            # Registrar adjunto en DynamoDB
            att = _save_attachment_record(
                project_id=project_id,
                user_id=uid,
                file_name=file.filename or f'doc.{ext}',
                file_size=len(file_bytes),
                content_type=file.content_type or '',
                ext=ext,
                s3_key=s3_key,
                extracted_text=text,
                source='web'
            )

            result['attachment'] = {
                'attachmentId': att['attachmentId'],
                'fileName': att['fileName'],
                'fileSize': att['fileSize'],
            }
            result['analysis'] = analysis
            return result

        except HTTPException:
            raise
        except Exception as e:
            print(f"[from_document] Error: {e}")
            import traceback; traceback.print_exc()
            raise HTTPException(status_code=500, detail=str(e))

    class AnalyzeTextRequest(BaseModel):
        text: str
        source: Optional[str] = "paste"  # "paste", "whatsapp", "gmail", "manual"

    class CreateProjectFromTextRequest(BaseModel):
        text: str
        name: Optional[str] = None
        channels: Optional[List[str]] = None
        source: Optional[str] = "paste"

    @app.post("/api/projects/from-text")
    async def create_project_from_text(req: CreateProjectFromTextRequest, x_user_id: str = Header(default="")):
        """Crea un proyecto desde un texto pegado (conversación WhatsApp, correo, notas).
        La IA infiere nombre, tipo, descripción y genera insights automáticamente."""
        from agent.document_parser import analyze_document_for_project
        from agent.project_helpers import create_project_full

        uid = require_uid(x_user_id)
        try:
            text = (req.text or '').strip()
            if len(text) < 30:
                raise HTTPException(status_code=400, detail="El texto es muy corto. Pega al menos una conversación completa o un párrafo descriptivo.")

            # IA infiere metadata
            analysis = analyze_document_for_project(text, fallback_name=req.name or '')
            project_name = (req.name or analysis['name']).strip()[:80]
            project_type = analysis['type']
            description = analysis['description']
            if analysis.get('extractedNotes'):
                description += "\n\nNotas: " + analysis['extractedNotes']

            channel_list = req.channels or ['Gmail']

            # Crear proyecto + insights
            result = create_project_full(
                user_id=uid,
                name=project_name,
                description=description,
                project_type=project_type,
                channels=channel_list,
                participants=[]
            )
            project_id = result['projectId']

            # Guardar el texto pegado como "adjunto" tipo .txt en el proyecto
            try:
                from agent.document_parser import upload_to_s3
                fname = f"texto-pegado-{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}.txt"
                s3_key = upload_to_s3(text.encode('utf-8'), project_id, fname, 'text/plain')
                _save_attachment_record(
                    project_id=project_id,
                    user_id=uid,
                    file_name=fname,
                    file_size=len(text.encode('utf-8')),
                    content_type='text/plain',
                    ext='txt',
                    s3_key=s3_key,
                    extracted_text=text,
                    source=req.source or 'paste'
                )
            except Exception as e:
                print(f"[from_text] Error guardando texto: {e}")

            result['analysis'] = analysis
            return result

        except HTTPException:
            raise
        except Exception as e:
            print(f"[from_text] Error: {e}")
            import traceback; traceback.print_exc()
            raise HTTPException(status_code=500, detail=str(e))

    @app.post("/api/projects/{project_id}/analyze-text")
    async def analyze_text_for_project(project_id: str, req: AnalyzeTextRequest, x_user_id: str = Header(default="")):
        """Analiza un texto pegado dentro de un proyecto existente.
        Genera nuevos insights (tareas, riesgos, decisiones) sin crear un proyecto nuevo."""
        from agent.document_parser import upload_to_s3
        from agent.project_helpers import generate_insights_for_project

        uid = require_uid(x_user_id)
        try:
            existing = projects_table.get_item(Key={'projectId': project_id}).get('Item')
            if not existing:
                raise HTTPException(status_code=404, detail="Proyecto no encontrado")
            if existing.get('userId') != uid:
                raise HTTPException(status_code=403, detail="No tienes permiso")

            text = (req.text or '').strip()
            if len(text) < 30:
                raise HTTPException(status_code=400, detail="El texto es muy corto.")

            # Guardar el texto como "adjunto" tipo .txt
            fname = f"texto-pegado-{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}.txt"
            try:
                s3_key = upload_to_s3(text.encode('utf-8'), project_id, fname, 'text/plain')
                _save_attachment_record(
                    project_id=project_id,
                    user_id=uid,
                    file_name=fname,
                    file_size=len(text.encode('utf-8')),
                    content_type='text/plain',
                    ext='txt',
                    s3_key=s3_key,
                    extracted_text=text,
                    source=req.source or 'paste'
                )
            except Exception as e:
                print(f"[analyze_text] Error guardando texto: {e}")

            # Generar insights con la IA
            insights_result = generate_insights_for_project(
                user_id=uid,
                project_id=project_id,
                project_name=existing.get('name', 'Proyecto'),
                project_type=existing.get('type', 'Otro'),
                description=text[:5000],
                participants_count=len(existing.get('participants', []))
            )

            # Notificación in-app
            if insights_result.get('generated') and insights_result.get('count', 0) > 0:
                try:
                    notifications_table.put_item(Item={
                        'userId': uid,
                        'notificationId': f"{datetime.utcnow().isoformat()}#{uuid.uuid4().hex[:8]}",
                        'projectId': project_id,
                        'projectName': existing.get('name', 'Proyecto'),
                        'type': 'text_analyzed',
                        'title': f'Texto analizado: {insights_result["count"]} insights',
                        'mensaje': f'La IA analizó el texto pegado y generó {insights_result["count"]} insights nuevos.',
                        'canal': 'system',
                        'status': 'unread',
                        'createdAt': datetime.utcnow().isoformat(),
                    })
                except Exception as e:
                    print(f"[analyze_text] notif error: {e}")

            return {
                "success": True,
                "insightsGenerated": insights_result,
                "savedAs": fname
            }

        except HTTPException:
            raise
        except Exception as e:
            print(f"[analyze_text] Error: {e}")
            import traceback; traceback.print_exc()
            raise HTTPException(status_code=500, detail=str(e))


    @app.post("/api/projects/{project_id}/attachments")
    async def upload_attachment(
        project_id: str,
        file: UploadFile = File(...),
        x_user_id: str = Header(default="")
    ):
        """Adjunta un documento a un proyecto existente. La IA genera insights
        adicionales con el contenido del documento."""
        from agent.document_parser import validate_file, extract_text, upload_to_s3
        from agent.project_helpers import generate_insights_for_project

        uid = require_uid(x_user_id)
        try:
            # Verificar que el proyecto pertenece al usuario
            existing = projects_table.get_item(Key={'projectId': project_id}).get('Item')
            if not existing:
                raise HTTPException(status_code=404, detail="Proyecto no encontrado")
            if existing.get('userId') != uid:
                raise HTTPException(status_code=403, detail="No tienes permiso para este proyecto")

            file_bytes = await file.read()
            valid, ext, error = validate_file(file_bytes, file.filename or '', file.content_type or '')
            if not valid:
                raise HTTPException(status_code=400, detail=error)

            text = extract_text(file_bytes, ext)
            print(f"[attachment] {file.filename}: {len(text)} caracteres extraídos")

            # Subir a S3
            s3_key = upload_to_s3(file_bytes, project_id, file.filename or f'doc.{ext}', file.content_type or '')

            # Registrar adjunto
            att = _save_attachment_record(
                project_id=project_id,
                user_id=uid,
                file_name=file.filename or f'doc.{ext}',
                file_size=len(file_bytes),
                content_type=file.content_type or '',
                ext=ext,
                s3_key=s3_key,
                extracted_text=text,
                source='web'
            )

            # Si hay texto suficiente, generar insights adicionales
            insights_result = {"generated": False, "reason": "no_text"}
            if text and len(text.strip()) >= 100:
                insights_result = generate_insights_for_project(
                    user_id=uid,
                    project_id=project_id,
                    project_name=existing.get('name', 'Proyecto'),
                    project_type=existing.get('type', 'Otro'),
                    description=text[:5000],
                    participants_count=len(existing.get('participants', []))
                )

                # Notificación in-app
                if insights_result.get('generated') and insights_result.get('count', 0) > 0:
                    try:
                        notifications_table.put_item(Item={
                            'userId': uid,
                            'notificationId': f"{datetime.utcnow().isoformat()}#{uuid.uuid4().hex[:8]}",
                            'projectId': project_id,
                            'projectName': existing.get('name', 'Proyecto'),
                            'type': 'document_analyzed',
                            'title': f'Documento analizado: {file.filename}',
                            'mensaje': f'La IA generó {insights_result["count"]} nuevos insights desde "{file.filename}"',
                            'canal': 'system',
                            'status': 'unread',
                            'createdAt': datetime.utcnow().isoformat(),
                        })
                    except Exception as e:
                        print(f"[attachment] notif error: {e}")

            return {
                "success": True,
                "attachment": {
                    'attachmentId': att['attachmentId'],
                    'fileName': att['fileName'],
                    'fileSize': att['fileSize'],
                    'extractedTextLength': att['extractedTextLength'],
                },
                "insightsGenerated": insights_result
            }

        except HTTPException:
            raise
        except Exception as e:
            print(f"[attachment] Error: {e}")
            import traceback; traceback.print_exc()
            raise HTTPException(status_code=500, detail=str(e))

    @app.get("/api/projects/{project_id}/attachments")
    async def list_attachments(project_id: str, x_user_id: str = Header(default="")):
        """Lista los adjuntos de un proyecto."""
        uid = require_uid(x_user_id)
        try:
            existing = projects_table.get_item(Key={'projectId': project_id}).get('Item')
            if not existing:
                raise HTTPException(status_code=404, detail="Proyecto no encontrado")
            if existing.get('userId') != uid:
                raise HTTPException(status_code=403, detail="No tienes permiso")

            result = attachments_table.query(
                KeyConditionExpression=Key('projectId').eq(project_id),
                ScanIndexForward=False
            )
            items = result.get('Items', [])
            return [{
                'attachmentId': i.get('attachmentId'),
                'fileName': i.get('fileName'),
                'fileSize': int(i.get('fileSize', 0)),
                'contentType': i.get('contentType', ''),
                'extension': i.get('extension', ''),
                'extractedTextPreview': i.get('extractedTextPreview', ''),
                'extractedTextLength': int(i.get('extractedTextLength', 0)),
                'source': i.get('source', 'web'),
                'createdAt': i.get('createdAt', ''),
            } for i in items]
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))

    @app.get("/api/attachments/{project_id}/{attachment_id}/download")
    async def download_attachment(project_id: str, attachment_id: str, x_user_id: str = Header(default="")):
        """Genera URL presignada de S3 para descargar el adjunto."""
        from agent.document_parser import generate_download_url
        uid = require_uid(x_user_id)
        try:
            item = attachments_table.get_item(
                Key={'projectId': project_id, 'attachmentId': attachment_id}
            ).get('Item')
            if not item:
                raise HTTPException(status_code=404, detail="Adjunto no encontrado")
            if item.get('userId') != uid:
                raise HTTPException(status_code=403, detail="No tienes permiso")

            url = generate_download_url(item['s3Key'], item.get('fileName', 'documento'))
            return {"url": url, "fileName": item.get('fileName'), "expiresIn": 600}
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))

    @app.delete("/api/attachments/{project_id}/{attachment_id}")
    async def delete_attachment(project_id: str, attachment_id: str, x_user_id: str = Header(default="")):
        """Elimina un adjunto (S3 + registro DynamoDB)."""
        from agent.document_parser import delete_from_s3
        uid = require_uid(x_user_id)
        try:
            item = attachments_table.get_item(
                Key={'projectId': project_id, 'attachmentId': attachment_id}
            ).get('Item')
            if not item:
                raise HTTPException(status_code=404, detail="Adjunto no encontrado")
            if item.get('userId') != uid:
                raise HTTPException(status_code=403, detail="No tienes permiso")

            delete_from_s3(item.get('s3Key', ''))
            attachments_table.delete_item(Key={'projectId': project_id, 'attachmentId': attachment_id})
            return {"success": True}
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))


    @app.get("/api/projects/{project_id}/tasks")
    async def get_tasks(project_id: str, x_user_id: str = Header(default="")):
        """Lista tareas de un proyecto."""
        uid = require_uid(x_user_id)
        try:
            result = tasks_table.scan(
                FilterExpression=Attr('projectId').eq(project_id) & Attr('userId').eq(uid)
            )
            tasks = sorted(
                result.get('Items', []),
                key=lambda x: x.get('createdAt', ''),
                reverse=True
            )
            return tasks
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))


    @app.post("/api/projects/{project_id}/tasks")
    async def create_task(project_id: str, req: CreateTaskRequest, x_user_id: str = Header(default="")):
        """Crea una tarea en un proyecto."""
        uid = require_uid(x_user_id)
        try:
            import uuid
            task_id = str(uuid.uuid4())
            now = datetime.utcnow().isoformat()
            item = {
                'projectId': project_id,
                'taskId': task_id,
                'userId': uid,
                'text': req.text,
                'description': req.description,
                'status': req.status,
                'createdBy': 'usuario',
                'assignedTo': req.assigned_to,
                'startDate': req.start_date or '',
                'dueDate': req.due_date or '',
                'createdAt': now,
            }
            tasks_table.put_item(Item=item)
            return {"success": True, "taskId": task_id, "text": req.text}
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))


    @app.put("/api/tasks/{task_id}")
    async def update_task(task_id: str, req: UpdateTaskRequest, x_user_id: str = Header(default="")):
        """Actualiza una tarea."""
        uid = require_uid(x_user_id)
        try:
            result = tasks_table.scan(
                FilterExpression=Attr('taskId').eq(task_id) & Attr('userId').eq(uid)
            )
            items = result.get('Items', [])
            if not items:
                raise HTTPException(status_code=404, detail="Tarea no encontrada")

            task = items[0]
            updates = {}
            if req.text is not None:
                updates['text'] = req.text
            if req.status is not None:
                # Normalizar 'completed' (legacy) → 'done' para mantener consistencia
                normalized_status = 'done' if req.status == 'completed' else req.status
                updates['status'] = normalized_status
            if req.assigned_to is not None:
                updates['assignedTo'] = req.assigned_to
            if req.description is not None:
                updates['description'] = req.description
            if req.blocked_reason is not None:
                updates['blockedReason'] = req.blocked_reason
            if req.start_date is not None:
                updates['startDate'] = req.start_date
            if req.due_date is not None:
                updates['dueDate'] = req.due_date

            if updates:
                expr_parts = []
                expr_values = {}
                expr_names = {}
                for i, (k, v) in enumerate(updates.items()):
                    expr_parts.append(f"#k{i} = :v{i}")
                    expr_values[f":v{i}"] = v
                    expr_names[f"#k{i}"] = k

                tasks_table.update_item(
                    Key={'projectId': task['projectId'], 'taskId': task_id},
                    UpdateExpression="SET " + ", ".join(expr_parts),
                    ExpressionAttributeValues=expr_values,
                    ExpressionAttributeNames=expr_names,
                )

            # Si la tarea ACABA de pasar a 'blocked', avisar por WhatsApp a los
            # participantes con teléfono (en un hilo, para no bloquear la respuesta).
            old_status = task.get('status', '')
            new_status = updates.get('status')
            if new_status == 'blocked' and old_status != 'blocked':
                import threading
                proj_id = task['projectId']
                task_text = updates.get('text', task.get('text', ''))
                reason = (updates.get('blockedReason') or task.get('blockedReason') or '').strip()
                def _notify_blocked():
                    try:
                        from agent.tools import enviar_notificacion
                        proj = projects_table.get_item(Key={'projectId': proj_id}).get('Item') or {}
                        if proj.get('userId') != uid:
                            return  # seguridad: solo el dueño del proyecto
                        proj_name = proj.get('name', '')
                        msg = (f"🔴 OneBox: la tarea \"{task_text}\" del proyecto "
                               f"\"{proj_name}\" está BLOQUEADA y requiere atención.")
                        if reason:
                            msg += f"\nMotivo: {reason}"
                        sent = 0
                        for part in proj.get('participants', []):
                            tel = (part.get('telefono') or part.get('phone') or '').strip()
                            if not tel:
                                continue
                            res = enviar_notificacion(tel, msg, canal='whatsapp',
                                                      project_id=proj_id, project_name=proj_name)
                            if res.get('success'):
                                sent += 1
                        print(f"[update_task] Tarea bloqueada → {sent} notificación(es) enviada(s)")
                    except Exception as e:
                        print(f"[update_task] Error notificando bloqueo: {e}")
                threading.Thread(target=_notify_blocked, daemon=True).start()

            return {"success": True, "taskId": task_id}
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))


    @app.delete("/api/tasks/{task_id}")
    async def delete_task(task_id: str, x_user_id: str = Header(default="")):
        """Elimina una tarea (solo si pertenece al usuario)."""
        uid = require_uid(x_user_id)
        try:
            # Localizar la tarea por scan (no tenemos GSI por taskId)
            result = tasks_table.scan(
                FilterExpression=Attr('taskId').eq(task_id) & Attr('userId').eq(uid)
            )
            items = result.get('Items', [])
            if not items:
                raise HTTPException(status_code=404, detail="Tarea no encontrada")
            task = items[0]
            tasks_table.delete_item(Key={'projectId': task['projectId'], 'taskId': task_id})
            return {"success": True, "taskId": task_id}
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))


    @app.get("/api/projects/{project_id}/conversations")
    async def get_conversations(project_id: str):
        """Lista conversaciones de un proyecto."""
        try:
            result = conversations_table.query(
                KeyConditionExpression=Key('projectId').eq(project_id)
            )
            convs = sorted(
                result.get('Items', []),
                key=lambda x: x.get('date', x.get('createdAt', '')),
                reverse=True
            )
            for c in convs:
                if c.get('body'):
                    c['body'] = c['body'][:500]
            return convs
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))


  

    @app.get("/api/insights")
    async def get_insights(type: Optional[str] = Query(None), x_user_id: str = Header(default="")):
        """Lista insights/acciones de la IA, opcionalmente filtradas por tipo."""
        uid = require_uid(x_user_id)
        try:
            filter_expr = Attr('userId').eq(uid)
            if type:
                filter_expr = filter_expr & Attr('type').eq(type)

            insights = sorted(
                scan_all_pages(insights_table, FilterExpression=filter_expr),
                key=lambda x: x.get('createdAt', ''),
                reverse=True
            )

            enriched = []
            for ins in insights:
                ins_type = ins.get('type', 'task_created')
                actions_taken = ins.get('actionsTaken', [])
                status = ins.get('status', 'new')
                created = ins.get('createdAt', '')
                time_str = created[11:16] if len(created) > 16 else created[:10]

                type_map = {
                    'decision':                {'badge': 'Decisión',          'badgeColor': 'bg-blue-500/20 text-blue-400 border-blue-500/30',        'icon': '✓',  'iconColor': 'bg-emerald-500/20 text-emerald-400'},
                    'blocker':                 {'badge': 'Bloqueo cliente',   'badgeColor': 'bg-red-500/20 text-red-400 border-red-500/30',           'icon': '🚧', 'iconColor': 'bg-red-500/20 text-red-400'},
                    'task_created':            {'badge': 'Tarea',             'badgeColor': 'bg-violet-500/20 text-violet-400 border-violet-500/30',  'icon': '📋', 'iconColor': 'bg-violet-500/20 text-violet-400'},
                    'work_done':               {'badge': 'Trabajo realizado', 'badgeColor': 'bg-emerald-500/20 text-emerald-400 border-emerald-500/30','icon': '✅', 'iconColor': 'bg-emerald-500/20 text-emerald-400'},
                    'followup':                {'badge': 'Follow-up',         'badgeColor': 'bg-indigo-500/20 text-indigo-400 border-indigo-500/30',  'icon': '📧', 'iconColor': 'bg-indigo-500/20 text-indigo-400'},
                    'risk':                    {'badge': 'Riesgo',            'badgeColor': 'bg-orange-500/20 text-orange-400 border-orange-500/30',  'icon': '⚠',  'iconColor': 'bg-amber-500/20 text-amber-400'},
                    'sla':                     {'badge': 'SLA',               'badgeColor': 'bg-red-500/20 text-red-400 border-red-500/30',           'icon': '🚨', 'iconColor': 'bg-red-500/20 text-red-400'},
                    'notification':            {'badge': 'Notificación',      'badgeColor': 'bg-sky-500/20 text-sky-400 border-sky-500/30',           'icon': '📱', 'iconColor': 'bg-sky-500/20 text-sky-400'},
                    'classification':          {'badge': 'Clasificación',     'badgeColor': 'bg-teal-500/20 text-teal-400 border-teal-500/30',        'icon': '🧠', 'iconColor': 'bg-teal-500/20 text-teal-400'},
                    'summary':                 {'badge': 'Resumen',           'badgeColor': 'bg-purple-500/20 text-purple-400 border-purple-500/30',  'icon': '📊', 'iconColor': 'bg-purple-500/20 text-purple-400'},
                    'project_characterization':{'badge': 'Tipo real',         'badgeColor': 'bg-fuchsia-500/20 text-fuchsia-300 border-fuchsia-500/30','icon': '🎯', 'iconColor': 'bg-fuchsia-500/20 text-fuchsia-300'},
                    'client_profile':          {'badge': 'Perfil cliente',    'badgeColor': 'bg-cyan-500/20 text-cyan-300 border-cyan-500/30',        'icon': '👤', 'iconColor': 'bg-cyan-500/20 text-cyan-300'},
                    'key_insight':             {'badge': 'Insight clave',     'badgeColor': 'bg-amber-500/20 text-amber-300 border-amber-500/30',     'icon': '💡', 'iconColor': 'bg-amber-500/20 text-amber-300'},
                    'metric':                  {'badge': 'Métrica',           'badgeColor': 'bg-lime-500/20 text-lime-300 border-lime-500/30',        'icon': '📈', 'iconColor': 'bg-lime-500/20 text-lime-300'},
                    'tech_issue':              {'badge': 'Problema técnico',  'badgeColor': 'bg-rose-500/20 text-rose-300 border-rose-500/30',        'icon': '🔧', 'iconColor': 'bg-rose-500/20 text-rose-300'},
                }
                ui = type_map.get(ins_type, type_map['task_created'])

                if status in ('executed', 'new'):
                    action_type = 'EJECUTÓ'
                    action_color = 'text-emerald-400'
                    fe_status = 'executed'
                elif status == 'review':
                    action_type = 'PAUSÓ'
                    action_color = 'text-amber-400'
                    fe_status = 'review'
                else:
                    action_type = 'EJECUTÓ'
                    action_color = 'text-emerald-400'
                    fe_status = 'executed'

                tags = []
                if ins.get('relatedPerson'):
                    tags.append({'label': ins['relatedPerson'], 'color': 'bg-white/5 text-white/50'})
                tags.append({'label': 'Ejecutado' if fe_status == 'executed' else 'Pendiente',
                             'color': 'bg-emerald-500/20 text-emerald-400' if fe_status == 'executed' else 'bg-amber-500/20 text-amber-400'})

                enriched.append({
                    'id': ins.get('insightId', ''),
                    'insightId': ins.get('insightId', ''),
                    'projectId': ins.get('projectId', ''),
                    'projectName': ins.get('projectName', 'Sistema'),
                    'type': ins_type,
                    'badge': ui['badge'],
                    'badgeColor': ui['badgeColor'],
                    'icon': ui['icon'],
                    'iconColor': ui['iconColor'],
                    'detected': ins.get('title', ''),
                    'title': ins.get('title', ''),
                    'description': ins.get('description', '') or ', '.join(actions_taken),
                    'action': ins.get('description', '') or ', '.join(actions_taken),
                    'actionType': action_type,
                    'actionColor': action_color,
                    'tags': tags,
                    'time': time_str,
                    'status': fe_status,
                    'createdAt': created,
                    'requiresReview': status == 'review',
                })

            return enriched

        except Exception as e:
            print(f"[API] Error en get_insights: {e}")
            import traceback; traceback.print_exc()
            raise HTTPException(status_code=500, detail=str(e))


    @app.get("/api/inbox")
    async def get_inbox(x_user_id: str = Header(default="")):
        """Lista conversaciones sin asignar del inbox."""
        uid = require_uid(x_user_id)
        try:
            items = scan_all_pages(
                conversations_table,
                FilterExpression=Attr('projectId').eq('unassigned') & Attr('userId').eq(uid)
            )
            for item in items:
                if item.get('body'):
                    item['body'] = item['body'][:500]
            items.sort(key=lambda x: x.get('date', x.get('createdAt', '')), reverse=True)
            return items
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))


    @app.post("/api/inbox/{conversation_id}/assign")
    async def assign_to_project(conversation_id: str, req: AssignRequest):
        """Asigna una conversación del inbox a un proyecto."""
        try:
            result = conversations_table.get_item(
                Key={'projectId': 'unassigned', 'conversationId': conversation_id}
            )
            if 'Item' not in result:
                raise HTTPException(status_code=404, detail="Conversación no encontrada")

            item = result['Item']
            item['projectId'] = req.projectId
            item['status'] = 'assigned'
            conversations_table.put_item(Item=item)

            conversations_table.delete_item(
                Key={'projectId': 'unassigned', 'conversationId': conversation_id}
            )

            return {"success": True, "conversationId": conversation_id, "projectId": req.projectId}
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))


    @app.get("/api/notifications")
    async def get_notifications(projectId: Optional[str] = Query(None), x_user_id: str = Header(default="")):
        """Lista notificaciones enviadas."""
        uid = require_uid(x_user_id)
        try:
            filter_expr = Attr('userId').eq(uid)
            if projectId:
                filter_expr = filter_expr & Attr('projectId').eq(projectId)

            items = sorted(
                scan_all_pages(notifications_table, FilterExpression=filter_expr),
                key=lambda x: x.get('createdAt', ''),
                reverse=True
            )
            return items[:50]
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))



    def _fetch_gmail_emails(user_id: str, max_results: int = 20) -> list:
        """Fetches emails from Gmail API using the user's stored refresh token."""
        import requests as _req

        token_item = _user_tokens_table.get_item(Key={'userId': user_id}).get('Item', {})
        refresh_token = token_item.get('gmailRefreshToken', '')
        if not refresh_token:
            print(f"[Gmail] No refresh token for user {user_id}")
            return []

        token_resp = _req.post('https://oauth2.googleapis.com/token', data={
            'client_id': GOOGLE_CLIENT_ID,
            'client_secret': GOOGLE_CLIENT_SECRET,
            'refresh_token': refresh_token,
            'grant_type': 'refresh_token'
        }, timeout=15)
        if token_resp.status_code != 200:
            print(f"[Gmail] Token refresh failed: {token_resp.text}")
            return []
        access_token = token_resp.json().get('access_token', '')

        list_resp = _req.get(
            f'https://gmail.googleapis.com/gmail/v1/users/me/messages',
            headers={'Authorization': f'Bearer {access_token}'},
            params={'maxResults': max_results, 'q': 'is:inbox -category:promotions -category:social -category:updates -category:forums'},
            timeout=15
        )
        if list_resp.status_code != 200:
            print(f"[Gmail] List messages failed: {list_resp.text}")
            return []

        messages = list_resp.json().get('messages', [])
        emails = []

        for msg in messages[:max_results]:
            msg_resp = _req.get(
                f'https://gmail.googleapis.com/gmail/v1/users/me/messages/{msg["id"]}',
                headers={'Authorization': f'Bearer {access_token}'},
                params={'format': 'full'},
                timeout=15
            )
            if msg_resp.status_code != 200:
                continue

            msg_data = msg_resp.json()
            hdrs = {h['name']: h['value'] for h in msg_data.get('payload', {}).get('headers', [])}

            body_text = msg_data.get('snippet', '')
            payload = msg_data.get('payload', {})
            def _extract_body(part):
                if part.get('mimeType') == 'text/plain' and part.get('body', {}).get('data'):
                    import base64
                    return base64.urlsafe_b64decode(part['body']['data']).decode('utf-8', errors='ignore')
                for sub in part.get('parts', []):
                    result = _extract_body(sub)
                    if result:
                        return result
                return ''
            full_body = _extract_body(payload)
            if not full_body:
                full_body = msg_data.get('snippet', '')

            to_field = hdrs.get('To', '')
            cc_field = hdrs.get('Cc', '')

            emails.append({
                'id': msg['id'],
                'from': hdrs.get('From', ''),
                'fromEmail': hdrs.get('From', '').split('<')[-1].rstrip('>') if '<' in hdrs.get('From', '') else hdrs.get('From', ''),
                'to': to_field,
                'cc': cc_field,
                'subject': hdrs.get('Subject', ''),
                'snippet': msg_data.get('snippet', ''),
                'body': full_body[:2000], 
                'date': hdrs.get('Date', '')
            })

        print(f"[Gmail] Fetched {len(emails)} emails for user {user_id}")
        return emails

    @app.post("/api/scheduled/gmail-sync")
    async def scheduled_gmail_sync(x_user_id: str = Header(default="")):
        """
        Sincroniza Gmail, trae correos nuevos y los analiza con IA.
        Crea proyectos, tareas e insights automáticamente.
        Usa el refresh token del usuario almacenado en DynamoDB.
        """
        from agent.tools import (
            analizar_inbox, crear_proyecto,
            asignar_correo_a_proyecto, crear_insight, crear_tarea,
            listar_proyectos
        )

        uid = require_uid(x_user_id)

        try:
            print(f"[Gmail Sync] Fetching emails for user {uid}...")
            gmail_emails = _fetch_gmail_emails(uid, max_results=50)
            print(f"[Gmail Sync] {len(gmail_emails)} emails from Gmail")

            SPAM_DOMAINS = [
                'bancolombia', 'homecenter', 'airbnb', 'puppis', 'dermosalud',
                'rappi', 'uber', 'samsung', 'adidas', 'temu', 'farmatodo',
                'linkedin', 'coursera', 'platzi', 'craftsy', 'medu.mx',
                'clickup', 'ngrok', 'livevoice', 'sura', 'coomeva',
                'loyal.ink', 'design.com', 'harumiglobal', 'npmjs',
                'worldoffice', 'exito.com', 'sodimac', 'nequi',
                'noreply', 'no-reply', 'no-responder', 'mailer-daemon',
                'notifications@', 'alertas@', 'notificaciones@',
                'news@', 'info@', 'express@', 'team@m.', 'informacion@',
                'alert@', 'editor@',
                'hello.platzi', 'hello.rappi', 'hello.design',
                'mail.clickup', 'mail.coursera',
                'email.samsung', 'e.exito',
                'farmaciasiman', 'glam', 'paiz.com',
                'accounts.google', 'pse', 'firmaelectronica',
                'amazon.com', 'apple.com', 'netflix', 'spotify',
                'mercadolibre', 'mercadopago', 'paypal', 'stripe',
                'cibergestion', 'pagares.bvc', 'jobalerts',
                'correo.paiz', 'siman.com',
            ]
            SPAM_SUBJECT = [
                'newsletter', 'unsubscribe', 'marketing', 'promocion', 'oferta',
                'descuento', 'verify', 'verification', 'encuesta', 'survey',
                'off en', '% off', 'envío gratis', 'tu opinión', 'alerta de seguridad',
                'alertas y notificaciones', 'factura electr', 'pedido se ha entregado',
                'cmr puntos', 'antipulgas', 'two-factor', '2fa',
                'busca personal', 'empleo', 'vacaciones', 'plan de salud',
                'comprobante', 'transacción', 'código de verificación',
                'pasaporte', 'orden de compra', 'confirmación de pago',
                'dcto', 'descubre', 'easter sale', 'black friday',
                'precios bajos', 'tu pedido', 'has recibido un documento',
                'firmante de', 'pagaré', 'welcome to',
            ]

            new_emails = 0
            spam_filtered = 0
            for email in gmail_emails:
                conv_id = email.get('id', email.get('messageId', ''))
                if not conv_id:
                    continue

                # Filtrar spam antes de guardar
                from_field = (email.get('from', '') + ' ' + email.get('fromEmail', '')).lower()
                subject = (email.get('subject', '') or '').lower()
                body = (email.get('snippet', email.get('body', '')) or '').lower()

                is_spam = (
                    any(kw in from_field for kw in SPAM_DOMAINS) or
                    any(kw in subject for kw in SPAM_SUBJECT)
                )
                if is_spam:
                    spam_filtered += 1
                    continue

                try:
                    conversations_table.put_item(
                        Item={
                            'projectId': 'unassigned',
                            'conversationId': f"gmail#{conv_id}",
                            'userId': uid,
                            'from': email.get('from', ''),
                            'fromEmail': email.get('fromEmail', email.get('from', '')),
                            'to': email.get('to', ''),
                            'cc': email.get('cc', ''),
                            'subject': email.get('subject', ''),
                            'body': email.get('body', email.get('snippet', ''))[:2000],
                            'date': email.get('date', datetime.utcnow().isoformat()),
                            'channel': 'gmail',
                            'status': 'unassigned',
                            'createdAt': datetime.utcnow().isoformat()
                        },
                        ConditionExpression='attribute_not_exists(conversationId)'
                    )
                    new_emails += 1
                except Exception:
                    pass 

            print(f"[Gmail Sync] {new_emails} new emails saved, {spam_filtered} spam filtered out")

            inbox_result = analizar_inbox()
            all_unassigned = inbox_result.get('emails', [])
            unassigned = sorted(all_unassigned, key=lambda x: x.get('date', x.get('createdAt', '')), reverse=True)[:20]
            print(f"[Gmail Sync] {len(unassigned)} unassigned emails to analyze")

            if not unassigned:
                return {
                    "success": True,
                    "new_emails": new_emails,
                    "projects_created": 0
                }

            email_summaries = []
            for e in unassigned:
                email_summaries.append({
                    'conversation_id': e.get('conversationId', ''),
                    'from': e.get('from', ''),
                    'fromEmail': e.get('fromEmail', ''),
                    'to': e.get('to', ''),
                    'cc': e.get('cc', ''),
                    'subject': e.get('subject', ''),
                    'body': e.get('body', '')[:1500],
                    'date': e.get('date', '')
                })

            existing = listar_proyectos()
            existing_names = [p.get('name', '') for p in existing.get('projects', [])]

            analysis_prompt = f"""Eres el clasificador de correos de OneBox. Tu trabajo es detectar correos que son proyectos de trabajo REALES y crearlos.

PROYECTOS EXISTENTES (NO crear duplicados):
{json.dumps(existing_names, ensure_ascii=False)}

CORREOS SIN ASIGNAR:
{json.dumps(email_summaries, ensure_ascii=False, indent=2)}

REGLAS CRÍTICAS:
1. SOLO ignora correos que sean CLARAMENTE newsletters, marketing, alertas automáticas de sistemas o spam.
2. Si un correo es de una PERSONA REAL hablando de trabajo, un proyecto, una solicitud, un requerimiento, una tarea, un problema técnico, una cotización, o cualquier tema profesional → SIEMPRE créalo como proyecto con action "create_project".
3. Si el correo ya pertenece a un proyecto existente → action "assign_to_existing"
4. EN CASO DE DUDA, créalo como proyecto. Es mejor crear un proyecto de más que perder un correo importante.
5. Agrupa correos del MISMO tema en un solo proyecto.
6. Detecta bloqueos, decisiones, riesgos y tareas dentro de cada proyecto.
7. En participants SOLO incluye personas con email verificable:
   - Del campo "from": el remitente con su email
   - Del campo "to": todos los destinatarios con sus emails
   - Del campo "cc": todos los CC con sus emails
   - NO agregues personas mencionadas en el cuerpo del correo que no tengan email en los campos from/to/cc
   Formato: {{"nombre": "Nombre", "email": "correo@ejemplo.com", "rol": "Rol detectado"}}
8. Lee el CUERPO COMPLETO del correo (campo "body") para detectar tareas, bloqueos, decisiones y riesgos. No te limites al subject.

EJEMPLOS de correos que SÍ son proyectos (NO ignorar):
- "Necesito una aplicación web para..." → create_project
- "Te envío los requerimientos de..." → create_project
- "Hay un problema con el servidor..." → create_project
- "¿Puedes cotizar...?" → create_project
- Cualquier correo de un colega/cliente sobre trabajo → create_project

EJEMPLOS de correos para IGNORAR:
- "Tu pedido de Amazon ha sido enviado" → ignore
- "50% de descuento en..." → ignore
- "Alerta de seguridad de Google" → ignore
- "Nuevas ofertas de empleo" → ignore

RESPONDE SOLO JSON:
{{
  "analysis": [
    {{"action": "ignore", "conversation_id": "...", "reason": "..."}},
    {{"action": "create_project", "project_name": "...", "project_description": "...", "project_type": "...", "participants": [{{"nombre": "...", "email": "correo@ejemplo.com", "rol": "..."}}], "emails_to_assign": ["..."], "insights": [{{"type": "blocker|decision|followup|risk|task_created", "title": "...", "description": "...", "related_person": "..."}}], "tasks": [{{"text": "...", "assigned_to": "...", "status": "pending|blocked"}}]}},
    {{"action": "assign_to_existing", "project_name": "...", "emails_to_assign": ["..."]}}
  ]
}}"""

            print("[Gmail Sync] Analyzing with LLM...")
            print(f"[Gmail Sync] Email summaries: {json.dumps(email_summaries[:3], ensure_ascii=False)[:500]}")
            response = call_llm(
                system_prompt="Eres el agente inteligente de OneBox. Clasificas correos y creas proyectos automáticamente. IMPORTANTE: Los correos de trabajo, solicitudes de proyectos, tareas, o comunicaciones de equipo DEBEN crear proyectos. Solo ignora newsletters automáticos, spam, códigos de verificación y alertas de marketing.",
                user_message=analysis_prompt,
                temperature=0.2,
                max_tokens=8192
            )

            print(f"[Gmail Sync] LLM raw response length: {len(response)}")
            print(f"[Gmail Sync] LLM raw response preview: {response[:1000]}")

            plan = extract_json_from_response(response)
            if not plan or 'analysis' not in plan:
                import re
                cleaned = response.strip()
                if cleaned.startswith('```'):
                    cleaned = re.sub(r'^```(?:json)?\s*', '', cleaned)
                    cleaned = re.sub(r'\s*```$', '', cleaned)
                try:
                    plan = json.loads(cleaned)
                except Exception:
                    pass

            if not plan or 'analysis' not in plan:
                print(f"[Gmail Sync] Could not parse LLM response: {response[:500]}")
                return {
                    "success": False,
                    "error": "Could not parse LLM analysis",
                    "new_emails": new_emails
                }

            print(f"[Gmail Sync] Plan: {json.dumps(plan, ensure_ascii=False)[:500]}")

            projects_created = 0
            emails_assigned = 0
            insights_count = 0
            tasks_count = 0
            ignored = 0

            for item in plan['analysis']:
                action = item.get('action', '')

                if action == 'ignore':
                    ignored += 1
                    print(f"[Gmail Sync] IGNORED: {item.get('conversation_id','')} - {item.get('reason','')}")
                    continue

                if action == 'create_project':
                    result = crear_proyecto(
                        name=item['project_name'],
                        description=item.get('project_description', ''),
                        type=item.get('project_type', 'Otro'),
                        participants=item.get('participants', []),
                        channels=['Gmail']
                    )
                    if result.get('success'):
                        pid = result['projectId']
                        projects_created += 1

                        for conv_id in item.get('emails_to_assign', []):
                            r = asignar_correo_a_proyecto(conv_id, pid, item['project_name'])
                            if r.get('success'):
                                emails_assigned += 1

                        for ins in item.get('insights', []):
                            r = crear_insight(pid, item['project_name'], ins['type'], ins['title'],
                                            ins.get('description', ''), ins.get('related_person', ''))
                            if r.get('success'):
                                insights_count += 1

                        for task in item.get('tasks', []):
                            r = crear_tarea(pid, task['text'], task.get('assigned_to', ''), task.get('status', 'pending'))
                            if r.get('success'):
                                tasks_count += 1

                elif action == 'assign_to_existing':
                    target_name = item.get('project_name', '')
                    target_pid = ''
                    for p in existing.get('projects', []):
                        if p.get('name', '').lower() == target_name.lower():
                            target_pid = p.get('projectId', '')
                            break
                    if target_pid:
                        for conv_id in item.get('emails_to_assign', []):
                            r = asignar_correo_a_proyecto(conv_id, target_pid, target_name)
                            if r.get('success'):
                                emails_assigned += 1

            result = {
                "success": True,
                "new_emails": new_emails,
                "ignored": ignored,
                "projects_created": projects_created,
                "emails_assigned": emails_assigned,
                "insights_created": insights_count,
                "tasks_created": tasks_count
            }
            print(f"[Gmail Sync] Done: {result}")
            return result

        except Exception as e:
            print(f"[Gmail Sync] Error: {e}")
            import traceback; traceback.print_exc()
            raise HTTPException(status_code=500, detail=str(e))



    @app.post("/api/scheduled/notifications")
    async def scheduled_notifications():
        """
        Endpoint para notificaciones automáticas.
        Revisa SLA (tareas bloqueadas/vencidas) y envía WhatsApp a los responsables.
        Diseñado para ser invocado por EventBridge cron cada mañana.
        No necesita Bedrock — lógica directa.
        """
        from agent.tools import (
            verificar_sla, obtener_contactos_proyecto, enviar_notificacion
        )

        try:

            sla_result = verificar_sla()
            alerts = sla_result.get('alerts', [])

            if not alerts:
                return {
                    "success": True,
                    "message": "No hay alertas activas",
                    "notifications_sent": 0
                }

            project_alerts = {}
            for alert in alerts:
                pid = alert.get('project_id', '')
                if not pid or alert.get('type') == 'unassigned_inbox':
                    continue
                if pid not in project_alerts:
                    project_alerts[pid] = []
                project_alerts[pid].append(alert)

            notifications_sent = 0
            errors = []

            for pid, p_alerts in project_alerts.items():
                contacts_result = obtener_contactos_proyecto(pid)
                if contacts_result.get('error'):
                    errors.append(f"Error obteniendo contactos de {pid}: {contacts_result['error']}")
                    continue

                project_name = contacts_result.get('project_name', pid)
                contactos = contacts_result.get('contactos', [])

                for contacto in contactos:
                    telefono = contacto.get('telefono', '')
                    nombre = contacto.get('nombre', '')
                    if not telefono:
                        continue

                    person_alerts = [a for a in p_alerts if a.get('assigned_to', '') == nombre]
                    if not person_alerts:
                        person_alerts = p_alerts

                    lines = [f"📋 *{project_name}* — Resumen de alertas\n"]
                    for a in person_alerts[:5]:
                        if a['type'] == 'blocked':
                            lines.append(f"🚫 *Bloqueada:* {a.get('task', '')}")
                        elif a['type'] == 'overdue':
                            lines.append(f"⏰ *Vencida:* {a.get('task', '')} (fecha: {a.get('due_date', '')})")

                    tareas_pendientes = contacto.get('tareas_pendientes', [])
                    if tareas_pendientes:
                        lines.append(f"\n📌 *Tus pendientes ({len(tareas_pendientes)}):*")
                        for t in tareas_pendientes[:5]:
                            status_icon = {'pending': '⏳', 'in_progress': '🔄', 'blocked': '🚫'}.get(t['status'], '•')
                            lines.append(f"  {status_icon} {t['text']}")

                    lines.append(f"\n_Enviado por OneBox_")
                    mensaje = "\n".join(lines)

                    send_result = enviar_notificacion(
                        destinatario=telefono,
                        mensaje=mensaje,
                        canal="whatsapp",
                        project_id=pid,
                        project_name=project_name
                    )
                    if send_result.get('success'):
                        notifications_sent += 1
                    else:
                        errors.append(f"Error enviando a {nombre}: {send_result.get('error', '')}")

            return {
                "success": True,
                "total_alerts": len(alerts),
                "projects_with_alerts": len(project_alerts),
                "notifications_sent": notifications_sent,
                "errors": errors if errors else None
            }

        except Exception as e:
            print(f"[Scheduled] Error en notificaciones: {e}")
            import traceback; traceback.print_exc()
            raise HTTPException(status_code=500, detail=str(e))



    @app.post("/api/test-whatsapp")
    async def test_whatsapp(request: Request):
        """Test directo de envío de WhatsApp sin pasar por el agente."""
        from agent.tools import enviar_notificacion
        body = await request.json()
        phone = body.get("phone", "")
        message = body.get("message", "Prueba de OneBox")
        if not phone:
            raise HTTPException(status_code=400, detail="phone requerido")
        result = enviar_notificacion(
            destinatario=phone,
            mensaje=message,
            canal="whatsapp",
            project_id="test",
            project_name="Test"
        )
        return result


    GOOGLE_CLOUD_PROJECT = os.environ.get('GOOGLE_CLOUD_PROJECT', 'gmail-lambda-project')
    GMAIL_PUBSUB_TOPIC = f"projects/{GOOGLE_CLOUD_PROJECT}/topics/gmail-notifications"

    @app.post("/api/gmail/push-notification")
    async def gmail_push_notification(request: Request):
        """
        Webhook que recibe notificaciones de Google Pub/Sub cuando llega un correo nuevo.
        Dispara el sync de Gmail automáticamente.
        """
        import threading
        import base64 as _base64

        try:
            body = await request.json()
            message = body.get('message', {})
            data = message.get('data', '')

            if data:
                decoded = json.loads(_base64.b64decode(data).decode('utf-8'))
                email_address = decoded.get('emailAddress', '')
                history_id = decoded.get('historyId', '')
                print(f"[Gmail Push] Correo nuevo para {email_address} (historyId: {history_id})")
            else:
                print("[Gmail Push] Notificación sin data")

            
            uid = USER_ID 
            try:
                result = _user_tokens_table.scan()
                for item in result.get('Items', []):
                    if item.get('gmailEmail', '').lower() == (email_address or '').lower():
                        uid = item.get('userId', USER_ID)
                        break
            except Exception:
                pass

            def _sync():
                try:
                    import urllib.request as _req
                    payload = json.dumps({}).encode('utf-8')
                    req = _req.Request(
                        f"http://localhost:8000/api/scheduled/gmail-sync",
                        data=payload,
                        headers={'Content-Type': 'application/json', 'x-user-id': uid},
                        method='POST'
                    )
                    with _req.urlopen(req, timeout=120) as resp:
                        result = json.loads(resp.read().decode('utf-8'))
                        print(f"[Gmail Push] Sync result: {result}")
                except Exception as e:
                    print(f"[Gmail Push] Sync error: {e}")

            thread = threading.Thread(target=_sync)
            thread.start()

            return {"status": "ok"}

        except Exception as e:
            print(f"[Gmail Push] Error: {e}")
            return {"status": "ok"} 


    @app.post("/api/gmail/register-watch")
    async def gmail_register_watch(x_user_id: str = Header(default="")):
        """Registra el watch de Gmail para recibir notificaciones push via Pub/Sub."""
        import urllib.request as _req
        import requests

        uid = require_uid(x_user_id)

        try:
            token_item = _user_tokens_table.get_item(Key={'userId': uid}).get('Item', {})
            refresh_token = token_item.get('gmailRefreshToken', '')
            if not refresh_token:
                return {"error": "No Gmail token found"}

            resp = requests.post('https://oauth2.googleapis.com/token', data={
                'client_id': GOOGLE_CLIENT_ID,
                'client_secret': GOOGLE_CLIENT_SECRET,
                'refresh_token': refresh_token,
                'grant_type': 'refresh_token'
            }, timeout=15)
            if resp.status_code != 200:
                return {"error": f"Token refresh failed: {resp.text}"}

            access_token = resp.json()['access_token']

            watch_resp = requests.post(
                'https://www.googleapis.com/gmail/v1/users/me/watch',
                headers={'Authorization': f'Bearer {access_token}', 'Content-Type': 'application/json'},
                json={
                    'topicName': GMAIL_PUBSUB_TOPIC,
                    'labelIds': ['INBOX']
                },
                timeout=15
            )

            if watch_resp.status_code == 200:
                watch_data = watch_resp.json()
                print(f"[Gmail Watch] Registered for user {uid}: {watch_data}")
                return {"success": True, "expiration": watch_data.get('expiration', ''), "historyId": watch_data.get('historyId', '')}
            else:
                return {"error": f"Watch failed: {watch_resp.text}"}

        except Exception as e:
            return {"error": str(e)}



    _user_phones_table = dynamodb.Table('onebox-user-phones')

    class LinkPhoneRequest(BaseModel):
        phoneNumber: str

    @app.post("/api/user/phone")
    async def link_phone(req: LinkPhoneRequest, x_user_id: str = Header(default=""), x_user_email: str = Header(default=""), x_user_name: str = Header(default="")):
        """Vincula un número de WhatsApp con el usuario autenticado."""
        uid = require_uid(x_user_id)
        try:
            phone = req.phoneNumber.strip()
            if not phone.startswith('+'):
                phone = '+' + phone

            _user_phones_table.put_item(Item={
                'phoneNumber': phone,
                'userId': uid,
                'email': x_user_email,
                'name': x_user_name,
                'linkedAt': datetime.utcnow().isoformat()
            })
            return {"success": True, "phoneNumber": phone, "userId": uid}
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))

    @app.get("/api/user/phone")
    async def get_user_phone(x_user_id: str = Header(default="")):
        """Obtiene el teléfono vinculado del usuario."""
        uid = require_uid(x_user_id)
        try:
            result = _user_phones_table.scan(
                FilterExpression=Attr('userId').eq(uid)
            )
            items = result.get('Items', [])
            if items:
                return {"phoneNumber": items[0]['phoneNumber'], "linked": True}
            return {"phoneNumber": "", "linked": False}
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))

    @app.get("/api/user/phones")
    async def get_user_phones(x_user_id: str = Header(default="")):
        """Obtiene todos los teléfonos de WhatsApp vinculados del usuario."""
        uid = require_uid(x_user_id)
        try:
            result = _user_phones_table.scan(
                FilterExpression=Attr('userId').eq(uid)
            )
            items = result.get('Items', [])
            phones = [
                {
                    'phoneNumber': item['phoneNumber'],
                    'name': item.get('name', ''),
                    'email': item.get('email', ''),
                    'linkedAt': item.get('linkedAt', '')
                }
                for item in items
            ]
            return {"success": True, "phones": phones}
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))

    @app.delete("/api/user/phone")
    async def unlink_phone(x_user_id: str = Header(default="")):
        """Desvincula el teléfono del usuario."""
        uid = require_uid(x_user_id)
        try:
            result = _user_phones_table.scan(
                FilterExpression=Attr('userId').eq(uid)
            )
            for item in result.get('Items', []):
                _user_phones_table.delete_item(Key={'phoneNumber': item['phoneNumber']})
            return {"success": True}
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))

    def _lookup_user_by_phone(phone_number: str) -> dict:
        """Busca qué usuario tiene este número vinculado."""
        try:
            # Normalizar
            phone = phone_number.strip()
            if not phone.startswith('+'):
                phone = '+' + phone

            result = _user_phones_table.get_item(Key={'phoneNumber': phone})
            item = result.get('Item')
            if item:
                return {
                    'userId': item['userId'],
                    'email': item.get('email', ''),
                    'name': item.get('name', '')
                }
            return {}
        except Exception:
            return {}


    _user_tokens_table = dynamodb.Table('onebox-user-tokens')

    GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID")
    GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET")
    
    @app.get("/api/gmail/auth")
    async def gmail_auth(x_user_id: str = Header(default="")):
        """Genera URL de autorización de Google OAuth para conectar Gmail."""
        uid = require_uid(x_user_id)
        from urllib.parse import urlencode as _urlencode

        params = {
            'client_id': GOOGLE_CLIENT_ID,
            'redirect_uri': GOOGLE_REDIRECT_URI,
            'response_type': 'code',
            'scope': ' '.join(GOOGLE_SCOPES),
            'access_type': 'offline',
            'prompt': 'consent',
            'state': uid 
        }
        auth_url = f"https://accounts.google.com/o/oauth2/v2/auth?{_urlencode(params)}"
        return {"auth_url": auth_url}

    @app.get("/api/gmail/callback")
    async def gmail_callback(code: str = Query(...), state: str = Query("")):
        """Callback de Google OAuth. Intercambia code por tokens y guarda."""
        import requests as _requests

        uid = require_uid(state)

        try:
            print(f"[Gmail OAuth] Exchanging code for user {uid}")
            token_resp = _requests.post('https://oauth2.googleapis.com/token', data={
                'code': code,
                'client_id': GOOGLE_CLIENT_ID,
                'client_secret': GOOGLE_CLIENT_SECRET,
                'redirect_uri': GOOGLE_REDIRECT_URI,
                'grant_type': 'authorization_code'
            }, timeout=30)

            print(f"[Gmail OAuth] Token response status: {token_resp.status_code}")
            print(f"[Gmail OAuth] Token response body: {token_resp.text[:500]}")

            if token_resp.status_code != 200:
                raise Exception(f"Token exchange failed: {token_resp.status_code} - {token_resp.text}")

            tokens = token_resp.json()
            refresh_token = tokens.get('refresh_token', '')
            access_token = tokens.get('access_token', '')

            if not refresh_token:
                existing = _user_tokens_table.get_item(Key={'userId': uid}).get('Item', {})
                refresh_token = existing.get('gmailRefreshToken', '')

            user_info_resp = _requests.get(
                'https://www.googleapis.com/oauth2/v2/userinfo',
                headers={'Authorization': f'Bearer {access_token}'},
                timeout=15
            )
            user_info = user_info_resp.json()
            gmail_email = user_info.get('email', '')

            _user_tokens_table.put_item(Item={
                'userId': uid,
                'gmailRefreshToken': refresh_token,
                'gmailEmail': gmail_email,
                'gmailConnected': True,
                'connectedAt': datetime.utcnow().isoformat()
            })

            print(f"[Gmail OAuth] Usuario {uid} conectó Gmail: {gmail_email}")

            from fastapi.responses import RedirectResponse
            return RedirectResponse(url="https://www.oneboxmanager.com/?gmail=connected")

        except Exception as e:
            print(f"[Gmail OAuth] Error: {e}")
            import traceback; traceback.print_exc()
            from fastapi.responses import RedirectResponse
            return RedirectResponse(url=f"https://www.oneboxmanager.com/?gmail=error&detail={str(e)[:100]}")

    @app.get("/api/gmail/status")
    async def gmail_status(x_user_id: str = Header(default="")):
        """Verifica si el usuario tiene Gmail conectado."""
        uid = require_uid(x_user_id)
        try:
            result = _user_tokens_table.get_item(Key={'userId': uid})
            item = result.get('Item')
            if item and item.get('gmailConnected'):
                return {
                    "connected": True,
                    "email": item.get('gmailEmail', ''),
                    "connectedAt": item.get('connectedAt', '')
                }
            return {"connected": False}
        except Exception:
            return {"connected": False}

    @app.delete("/api/gmail/disconnect")
    async def gmail_disconnect(x_user_id: str = Header(default="")):
        """Desconecta Gmail del usuario."""
        uid = require_uid(x_user_id)
        try:
            _user_tokens_table.delete_item(Key={'userId': uid})
            return {"success": True}
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))



    from urllib.parse import parse_qs as _parse_qs, urlencode as _urlencode

    _sessions_table = dynamodb.Table('onebox-whatsapp-sessions')
    _SESSION_TIMEOUT_HOURS = 2
    _MAX_HISTORY = 10

    def _send_whatsapp_reply(to_number: str, message: str):
        """Envía respuesta por WhatsApp usando Twilio API."""
        try:
            from twilio.rest import Client
            sid = os.environ.get('TWILIO_ACCOUNT_SID', '')
            token = os.environ.get('TWILIO_AUTH_TOKEN', '')
            wa_number = os.environ.get('TWILIO_WHATSAPP_NUMBER', 'whatsapp:+14155238886')
            client = Client(sid, token)
            client.messages.create(body=message, from_=wa_number, to=to_number)
            print(f"[Webhook] Respuesta enviada a {to_number}")
        except Exception as e:
            print(f"[Webhook] Error enviando respuesta: {e}")

    def _get_session(phone_number: str) -> dict:
        try:
            result = _sessions_table.get_item(Key={'phoneNumber': phone_number})
            session = result.get('Item')
            if session:
                last_activity = session.get('lastActivity', '')
                if last_activity:
                    last_time = datetime.fromisoformat(last_activity)
                    if datetime.utcnow() - last_time > timedelta(hours=_SESSION_TIMEOUT_HOURS):
                        return _create_session(phone_number)
                return session
            return _create_session(phone_number)
        except Exception:
            return _create_session(phone_number)

    def _create_session(phone_number: str) -> dict:
        session = {
            'phoneNumber': phone_number,
            'activeProjectId': '',
            'activeProjectName': '',
            'history': [],
            'lastActivity': datetime.utcnow().isoformat(),
            'createdAt': datetime.utcnow().isoformat()
        }
        _sessions_table.put_item(Item=session)
        return session

    def _update_session(phone_number, message, response, project_id='', project_name=''):
        try:
            session = _get_session(phone_number)
            history = session.get('history', [])
            history.append({'role': 'user', 'content': message})
            history.append({'role': 'assistant', 'content': response})
            if len(history) > _MAX_HISTORY * 2:
                history = history[-(_MAX_HISTORY * 2):]

            update_expr = "SET #h = :history, lastActivity = :now"
            expr_values = {':history': history, ':now': datetime.utcnow().isoformat()}
            expr_names = {'#h': 'history'}
            if project_id:
                update_expr += ", activeProjectId = :pid, activeProjectName = :pname"
                expr_values[':pid'] = project_id
                expr_values[':pname'] = project_name

            _sessions_table.update_item(
                Key={'phoneNumber': phone_number},
                UpdateExpression=update_expr,
                ExpressionAttributeValues=expr_values,
                ExpressionAttributeNames=expr_names
            )
        except Exception as e:
            print(f"[Session] Error: {e}")

    def _build_context(session, new_message):
        parts = []
        active = session.get('activeProjectId', '')
        name = session.get('activeProjectName', '')
        if active:
            parts.append(f"[CONTEXTO: El usuario está hablando sobre el proyecto '{name}' (ID: {active}). "
                         f"Si el mensaje se refiere a este proyecto, úsalo. Si habla de algo nuevo, crea uno nuevo.]")
        parts.append(new_message)
        return "\n".join(parts)

    def _extract_project(response_text, tools_used):
        import re
        if any(t in tools_used for t in ['crear_proyecto', 'listar_proyectos', 'obtener_contactos_proyecto']):
            id_match = re.search(r'proj-[a-f0-9]+', response_text)
            name_match = re.search(r'\*\*(.+?)\*\*', response_text)
            return (id_match.group(0) if id_match else '', name_match.group(1) if name_match else '')
        return ('', '')

    def _auto_link_phone(phone: str, user_id: str, email: str, name: str) -> bool:
        """Vincula un número de WhatsApp a un usuario Cognito (si no estaba vinculado)."""
        try:
            phone_clean = phone if phone.startswith('+') else '+' + phone
            existing = _user_phones_table.get_item(Key={'phoneNumber': phone_clean}).get('Item')
            if existing:
                return False  # Ya estaba vinculado
            _user_phones_table.put_item(Item={
                'phoneNumber': phone_clean,
                'userId': user_id,
                'email': email,
                'name': name,
                'linkedAt': datetime.utcnow().isoformat(),
                'linkedVia': 'whatsapp_wizard'
            })
            print(f"[auto_link_phone] {phone_clean} → {user_id}")
            return True
        except Exception as e:
            print(f"[auto_link_phone] Error: {e}")
            return False

    @app.post("/api/twilio/webhook")
    async def twilio_webhook(request: Request):
        """Webhook de Twilio para WhatsApp/SMS entrantes. Procesa con wizard o agente IA."""
        import threading
        from agent.whatsapp_wizard import handle_wizard, get_flow_state, STEP_IDLE

        try:
            body_raw = (await request.body()).decode('utf-8')
            params = _parse_qs(body_raw)

            from_number = params.get('From', [''])[0]
            message_body = params.get('Body', [''])[0]
            message_sid = params.get('MessageSid', [''])[0]
            num_media = int(params.get('NumMedia', ['0'])[0])

            canal = 'whatsapp' if from_number.startswith('whatsapp:') else 'sms'
            clean_number = from_number.replace('whatsapp:', '')

            if message_body.strip().lower().startswith('join'):
                print(f"[Webhook] Mensaje de join sandbox de {clean_number}, ignorando")
                return {"status": "ok", "action": "join_ignored"}

            # Buscar si el número ya está vinculado a un usuario
            user_info = _lookup_user_by_phone(clean_number)

            # =================================================================
            # ¿Hay un archivo adjunto? Procesar directamente
            # =================================================================
            if num_media > 0:
                media_url = params.get('MediaUrl0', [''])[0]
                media_ct = params.get('MediaContentType0', [''])[0]
                if not user_info:
                    _send_whatsapp_reply(
                        from_number,
                        "📎 Recibí tu archivo, pero tu número no está vinculado a una cuenta de OneBox.\n\n"
                        "Para crear proyectos desde documentos, primero vincula tu número:\n"
                        "1️⃣ Inicia sesión en oneboxmanager.com\n"
                        "2️⃣ Ve a tu perfil → vincula tu número\n\n"
                        "O escribe *crear proyecto* para validarte por correo y crear uno desde cero."
                    )
                    return {"status": "ok", "action": "media_no_account"}

                # Procesar el archivo en background para no bloquear el webhook
                def _process_media():
                    try:
                        from agent.document_parser import (
                            download_from_twilio, validate_file, extract_text,
                            analyze_document_for_project, upload_to_s3
                        )
                        from agent.project_helpers import create_project_full

                        sid = os.environ.get('TWILIO_ACCOUNT_SID', '')
                        tok = os.environ.get('TWILIO_AUTH_TOKEN', '')
                        file_bytes, ct, fname = download_from_twilio(media_url, sid, tok)
                        if not file_bytes:
                            _send_whatsapp_reply(from_number, "⚠️ No pude descargar el archivo. Intenta de nuevo o súbelo desde la web.")
                            return

                        valid, ext, error = validate_file(file_bytes, fname, ct or media_ct)
                        if not valid:
                            _send_whatsapp_reply(from_number, f"⚠️ {error}")
                            return

                        text = extract_text(file_bytes, ext)
                        if not text or len(text.strip()) < 30:
                            _send_whatsapp_reply(from_number, "⚠️ No pude extraer suficiente texto del archivo. Asegúrate de que no esté escaneado o protegido.")
                            return

                        _send_whatsapp_reply(from_number, f"📄 Documento recibido ({len(file_bytes)//1024} KB).\n🤖 Analizando con IA...")

                        analysis = analyze_document_for_project(text)
                        description = analysis['description']
                        if analysis.get('extractedNotes'):
                            description += "\n\nNotas: " + analysis['extractedNotes']

                        result = create_project_full(
                            user_id=user_info['userId'],
                            name=analysis['name'],
                            description=description,
                            project_type=analysis['type'],
                            channels=['Gmail', 'WhatsApp'],
                            participants=[{
                                'nombre': user_info.get('name', ''),
                                'email': user_info.get('email', ''),
                                'telefono': clean_number,
                                'rol': 'Creador'
                            }]
                        )
                        project_id = result['projectId']

                        s3_key = upload_to_s3(file_bytes, project_id, fname or f'doc.{ext}', ct or media_ct)
                        _save_attachment_record(
                            project_id=project_id,
                            user_id=user_info['userId'],
                            file_name=fname or f'doc.{ext}',
                            file_size=len(file_bytes),
                            content_type=ct or media_ct,
                            ext=ext,
                            s3_key=s3_key,
                            extracted_text=text,
                            source='whatsapp'
                        )

                        ig = result.get('insightsGenerated', {})
                        count = ig.get('count', 0) if ig.get('generated') else 0
                        msg = (
                            f"✅ *Proyecto creado: {analysis['name']}*\n"
                            f"📁 Tipo: {analysis['type']}\n\n"
                        )
                        if count > 0:
                            an = ig.get('analysis', {}) or {}
                            msg += (
                                f"🤖 La IA generó {count} insights:\n"
                                f"  • {len(an.get('tasks') or [])} tareas\n"
                                f"  • {len(an.get('risks') or [])} riesgos\n"
                                f"  • {len(an.get('decisions') or [])} decisiones\n\n"
                            )
                        msg += f"📎 Documento adjuntado al proyecto.\n📊 Revisa todo en https://www.oneboxmanager.com"
                        _send_whatsapp_reply(from_number, msg)
                    except Exception as e:
                        print(f"[Webhook media] Error: {e}")
                        import traceback; traceback.print_exc()
                        _send_whatsapp_reply(from_number, f"⚠️ Error procesando el documento: {str(e)[:80]}")

                threading.Thread(target=_process_media).start()
                return {"status": "ok", "action": "media_processing"}

            # Cargar sesión del wizard (siempre, esté vinculado o no)
            session = _get_session(clean_number)
            flow = get_flow_state(session)
            in_wizard = flow.get('step', STEP_IDLE) != STEP_IDLE

            # Si NO hay número vinculado Y NO está en wizard activo: invitar a wizard o registrarse
            if not user_info and not in_wizard:
                print(f"[Webhook] Número {clean_number} no vinculado, ofreciendo wizard")
                msg_lower = message_body.strip().lower()
                # Si el usuario quiere crear un proyecto, lanzamos el wizard (validará el email)
                from agent.whatsapp_wizard import detect_intent
                intent = detect_intent(message_body)

                if intent in ('create_project', 'greeting', 'help'):
                    # Permitir entrar al wizard incluso sin vinculación previa
                    pass
                else:
                    _send_whatsapp_reply(
                        from_number,
                        "👋 ¡Hola! Soy *OneBox*.\n\n"
                        "Tu número aún no está vinculado a una cuenta. Pero puedo ayudarte a crear tu primer proyecto si tienes una cuenta de OneBox con tu correo.\n\n"
                        "Escribe *crear proyecto* para empezar, o *ayuda* para más opciones.\n\n"
                        "Si aún no tienes cuenta, regístrate primero en *oneboxmanager.com*."
                    )
                    return {"status": "ok", "action": "no_account_prompt"}

            # Procesar el wizard si aplica (o pasar al agente si retorna None)
            wizard_response, new_flow = handle_wizard(
                session=session,
                phone_number=clean_number,
                message=message_body,
                auto_link_phone_func=_auto_link_phone
            )

            if wizard_response is not None:
                # El wizard manejó el mensaje
                if new_flow is not None:
                    try:
                        _sessions_table.update_item(
                            Key={'phoneNumber': clean_number},
                            UpdateExpression="SET creationFlow = :f, lastActivity = :now",
                            ExpressionAttributeValues={
                                ':f': new_flow,
                                ':now': datetime.utcnow().isoformat()
                            }
                        )
                    except Exception as e:
                        print(f"[Webhook] Error actualizando flow: {e}")
                _send_whatsapp_reply(from_number, wizard_response)
                return {"status": "ok", "action": "wizard_handled"}

            # Si el wizard no manejó el mensaje y no hay usuario vinculado, no podemos continuar
            if not user_info:
                _send_whatsapp_reply(
                    from_number,
                    "👋 Para usar el agente IA necesitas vincular tu número.\n\n"
                    "Escribe *crear proyecto* para crear uno con tu correo, o vincula tu número en *oneboxmanager.com*."
                )
                return {"status": "ok", "action": "unregistered_user"}

            resolved_user_id = user_info['userId']
            resolved_name = user_info.get('name', clean_number)

            print(f"[Webhook] {canal} de {clean_number} (user: {resolved_name}): {message_body[:100]}")

            now = datetime.utcnow().isoformat()
            try:
                conversations_table.put_item(
                    Item={
                        'projectId': 'unassigned',
                        'conversationId': f"twilio#{message_sid}",
                        'userId': resolved_user_id,
                        'from': clean_number,
                        'fromEmail': '',
                        'subject': f'Mensaje {canal.upper()} entrante',
                        'body': message_body,
                        'date': now,
                        'channel': canal,
                        'twilioMessageSid': message_sid,
                        'hasAttachments': num_media > 0,
                        'status': 'unassigned',
                        'createdAt': now
                    },
                    ConditionExpression='attribute_not_exists(conversationId)'
                )
            except Exception:
                pass 

            _resolved_uid = resolved_user_id
            _resolved_name = resolved_name

            def _process():
                try:
                    import agent.tools as _tools
                    _tools.USER_ID = _resolved_uid

                    session = _get_session(clean_number)
                    history = session.get('history', [])
                    context_message = _build_context(session, message_body)

                    result = run_agent(context_message, history[-6:])
                    agent_response = result.get('response', 'No pude procesar tu mensaje.')
                    tools_used = result.get('tools_used', [])

                    if len(agent_response) > 1500:
                        agent_response = agent_response[:1500] + "\n\n_...mensaje truncado_"

                    project_id, project_name = _extract_project(agent_response, tools_used)
                    _update_session(clean_number, message_body, agent_response, project_id, project_name)
                    _send_whatsapp_reply(from_number, agent_response)
                except Exception as e:
                    print(f"[Webhook] Error procesando: {e}")
                    import traceback; traceback.print_exc()
                    _send_whatsapp_reply(from_number, "⚠️ Hubo un error procesando tu mensaje. Intenta de nuevo.")

            thread = threading.Thread(target=_process)
            thread.start()

            return {"status": "ok", "action": "processing"}

        except Exception as e:
            print(f"[Webhook] Error: {e}")
            return {"status": "error", "detail": str(e)}


  
    print("\n🚀 Iniciando OneBox Agent en http://localhost:8000")
    print("📖 Docs en http://localhost:8000/docs")
    print("📡 REST API: /api/projects, /api/insights, /api/inbox, /api/notifications")
    print("📱 Twilio webhook: /api/twilio/webhook\n")
    uvicorn.run(app, host="0.0.0.0", port=8000)
