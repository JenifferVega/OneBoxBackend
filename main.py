

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
    import uvicorn
    import boto3 as _boto3
    from boto3.dynamodb.conditions import Key, Attr
    from agent.tools import (
        projects_table, conversations_table, tasks_table,
        insights_table, notifications_table, USER_ID
    )
    dynamodb = _boto3.resource('dynamodb', region_name=os.environ.get('AWS_REGION', 'us-east-1'))

    def get_user_id(x_user_id: str = Header(default="")) -> str:
        """Extract user ID from request header, fallback to default."""
        return x_user_id if x_user_id else USER_ID

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

    class CreateTaskRequest(BaseModel):
        text: str
        assigned_to: str = ""
        status: str = "pending"
        description: str = ""

    class UpdateTaskRequest(BaseModel):
        text: Optional[str] = None
        status: Optional[str] = None
        assigned_to: Optional[str] = None
        description: Optional[str] = None

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
        uid = user_id if user_id else USER_ID
        user_email = x_user_email.lower() if x_user_email else ""
        try:
            proj_result = projects_table.scan(
                FilterExpression=Attr('userId').eq(uid)
            )
            own_projects = proj_result.get('Items', [])
            own_ids = {p['projectId'] for p in own_projects}

            shared_projects = []
            if user_email:
                all_proj_result = projects_table.scan()
                for p in all_proj_result.get('Items', []):
                    if p['projectId'] in own_ids:
                        continue  
                    participants = p.get('participants', [])
                    for part in participants:
                        part_email = (part.get('email', '') or '').lower()
                        part_name = (part.get('nombre', '') or '').lower()
                        if (part_email and part_email == user_email) or \
                           (user_email.split('@')[0] in part_name):
                            p['_shared'] = True  
                            shared_projects.append(p)
                            break

            projects = own_projects + shared_projects

            tasks_result = tasks_table.scan(
                FilterExpression=Attr('userId').eq(uid)
            )
            all_tasks = tasks_result.get('Items', [])

            insights_result = insights_table.scan(
                FilterExpression=Attr('userId').eq(uid)
            )
            all_insights = sorted(
                insights_result.get('Items', []),
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
                progress = round((done / total) * 100) if total > 0 else 0

                proj_insights = [i for i in all_insights if i.get('projectId') == pid]

                overdue = [t for t in proj_tasks
                           if t.get('status') == 'pending' and t.get('dueDate', '') and t.get('dueDate', '') < today]
                if blocked >= 2 or len(overdue) >= 2:
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
                    status_labels = {
                        'done': 'Completada', 'pending': 'Pendiente',
                        'blocked': 'Bloqueada', 'in_progress': 'En curso',
                    }
                    tags = [status_labels.get(t.get('status', ''), t.get('status', ''))]
                    if t.get('type') == 'reminder':
                        tags.append('Recordatorio')
                    if t.get('dueDate', '') and t.get('dueDate', '') < today and t.get('status') == 'pending':
                        tags.append('Vencida')

                    tasks_list.append({
                        'id': t.get('taskId', ''),
                        'text': t.get('text', ''),
                        'status': t.get('status', 'pending'),
                        'description': t.get('description', ''),
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
                    'daysLeft': 0,
                    'progress': progress,
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

             Ordenar: activos primero, luego por nombre
            enriched.sort(key=lambda p: (0 if p['status'] == 'active' else 1, p['name']))
            return enriched

        except Exception as e:
            print(f"[API] Error en get_projects: {e}")
            import traceback; traceback.print_exc()
            raise HTTPException(status_code=500, detail=str(e))


    @app.get("/api/projects/{project_id}")
    async def get_project(project_id: str, x_user_id: str = Header(default="")):
        """Obtiene un proyecto específico con todos sus datos."""
        uid = x_user_id if x_user_id else USER_ID
        try:
             Proyecto
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
        """Crea un nuevo proyecto."""
        uid = x_user_id if x_user_id else USER_ID
        try:
            import uuid
            project_id = "proj-" + uuid.uuid4().hex[:8]
            now = datetime.utcnow().isoformat()
            item = {
                'projectId': project_id,
                'userId': uid,
                'name': req.name,
                'description': req.description,
                'type': req.type,
                'status': 'active',
                'participants': req.participants or [],
                'channels': req.channels or ['Gmail'],
                'createdAt': now,
                'lastActivity': now,
            }
            projects_table.put_item(Item=item)
            return {"success": True, "projectId": project_id, "name": req.name}
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))


    class UpdateParticipantsRequest(BaseModel):
        participants: List[dict]

    @app.put("/api/projects/{project_id}/participants")
    async def update_participants(project_id: str, req: UpdateParticipantsRequest, x_user_id: str = Header(default="")):
        """Actualiza los participantes de un proyecto (incluye teléfonos)."""
        uid = x_user_id if x_user_id else USER_ID
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



    @app.get("/api/projects/{project_id}/tasks")
    async def get_tasks(project_id: str, x_user_id: str = Header(default="")):
        """Lista tareas de un proyecto."""
        uid = x_user_id if x_user_id else USER_ID
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
        uid = x_user_id if x_user_id else USER_ID
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
                'createdAt': now,
            }
            tasks_table.put_item(Item=item)
            return {"success": True, "taskId": task_id, "text": req.text}
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))


    @app.put("/api/tasks/{task_id}")
    async def update_task(task_id: str, req: UpdateTaskRequest, x_user_id: str = Header(default="")):
        """Actualiza una tarea."""
        uid = x_user_id if x_user_id else USER_ID
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
                updates['status'] = req.status
            if req.assigned_to is not None:
                updates['assignedTo'] = req.assigned_to
            if req.description is not None:
                updates['description'] = req.description

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
        uid = x_user_id if x_user_id else USER_ID
        try:
            filter_expr = Attr('userId').eq(uid)
            if type:
                filter_expr = filter_expr & Attr('type').eq(type)

            result = insights_table.scan(FilterExpression=filter_expr)
            insights = sorted(
                result.get('Items', []),
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
                    'decision':     {'badge': 'Decisión',         'badgeColor': 'bg-blue-500/20 text-blue-400 border-blue-500/30',    'icon': '✓', 'iconColor': 'bg-emerald-500/20 text-emerald-400'},
                    'blocker':      {'badge': 'Bloqueador',       'badgeColor': 'bg-red-500/20 text-red-400 border-red-500/30',       'icon': '●', 'iconColor': 'bg-red-500/20 text-red-400'},
                    'task_created': {'badge': 'Tarea Creada',     'badgeColor': 'bg-violet-500/20 text-violet-400 border-violet-500/30','icon': '📋','iconColor': 'bg-violet-500/20 text-violet-400'},
                    'followup':     {'badge': 'Follow-up',        'badgeColor': 'bg-indigo-500/20 text-indigo-400 border-indigo-500/30','icon': '📧','iconColor': 'bg-indigo-500/20 text-indigo-400'},
                    'risk':         {'badge': 'Riesgo',           'badgeColor': 'bg-orange-500/20 text-orange-400 border-orange-500/30','icon': '⚠','iconColor': 'bg-amber-500/20 text-amber-400'},
                    'sla':          {'badge': 'SLA',              'badgeColor': 'bg-red-500/20 text-red-400 border-red-500/30',       'icon': '🚨','iconColor': 'bg-red-500/20 text-red-400'},
                    'notification': {'badge': 'Notificación',     'badgeColor': 'bg-sky-500/20 text-sky-400 border-sky-500/30',       'icon': '📱','iconColor': 'bg-sky-500/20 text-sky-400'},
                    'classification':{'badge': 'Clasificación',   'badgeColor': 'bg-teal-500/20 text-teal-400 border-teal-500/30',    'icon': '🧠','iconColor': 'bg-teal-500/20 text-teal-400'},
                    'summary':      {'badge': 'Resumen',          'badgeColor': 'bg-purple-500/20 text-purple-400 border-purple-500/30','icon': '📊','iconColor': 'bg-purple-500/20 text-purple-400'},
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
                    'projectName': ins.get('projectName', 'Sistema'),
                    'type': ins_type,
                    'badge': ui['badge'],
                    'badgeColor': ui['badgeColor'],
                    'icon': ui['icon'],
                    'iconColor': ui['iconColor'],
                    'detected': ins.get('title', ''),
                    'action': ins.get('description', '') or ', '.join(actions_taken),
                    'actionType': action_type,
                    'actionColor': action_color,
                    'tags': tags,
                    'time': time_str,
                    'status': fe_status,
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
        uid = x_user_id if x_user_id else USER_ID
        try:
            result = conversations_table.scan(
                FilterExpression=Attr('projectId').eq('unassigned') & Attr('userId').eq(uid)
            )
            items = result.get('Items', [])
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
        uid = x_user_id if x_user_id else USER_ID
        try:
            filter_expr = Attr('userId').eq(uid)
            if projectId:
                filter_expr = filter_expr & Attr('projectId').eq(projectId)

            result = notifications_table.scan(FilterExpression=filter_expr)
            items = sorted(
                result.get('Items', []),
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
        from agent.llm import call_llm, extract_json_from_response

        uid = x_user_id if x_user_id else USER_ID

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

        uid = x_user_id if x_user_id else USER_ID

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
        uid = x_user_id if x_user_id else USER_ID
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
        uid = x_user_id if x_user_id else USER_ID
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

    @app.delete("/api/user/phone")
    async def unlink_phone(x_user_id: str = Header(default="")):
        """Desvincula el teléfono del usuario."""
        uid = x_user_id if x_user_id else USER_ID
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

   
    @app.get("/api/gmail/auth")
    async def gmail_auth(x_user_id: str = Header(default="")):
        """Genera URL de autorización de Google OAuth para conectar Gmail."""
        uid = x_user_id if x_user_id else USER_ID
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

        uid = state if state else USER_ID

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
        uid = x_user_id if x_user_id else USER_ID
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
        uid = x_user_id if x_user_id else USER_ID
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

    @app.post("/api/twilio/webhook")
    async def twilio_webhook(request: Request):
        """Webhook de Twilio para WhatsApp/SMS entrantes. Procesa con el agente y responde."""
        import threading

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

            user_info = _lookup_user_by_phone(clean_number)

            if not user_info:
                print(f"[Webhook] Número {clean_number} no vinculado, rechazando")
                _send_whatsapp_reply(
                    from_number,
                    "👋 ¡Hola! Tu número no está vinculado a una cuenta de OneBox.\n\n"
                    "Para usar OneBox por WhatsApp:\n"
                    "1️⃣ Regístrate en *oneboxmanager.com*\n"
                    "2️⃣ Inicia sesión\n"
                    "3️⃣ Ve a tu perfil y vincula tu número de WhatsApp\n\n"
                    "Después de eso podrás gestionar tus proyectos desde aquí. 🚀"
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
