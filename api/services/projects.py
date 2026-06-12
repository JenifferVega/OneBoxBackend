"""Lógica interna de proyectos: listado enriquecido, detalle, creación,
participantes, invitaciones y borrado en cascada."""
import os
import uuid
from datetime import datetime

import boto3
from boto3.dynamodb.conditions import Attr, Key
from fastapi import HTTPException

from agent.tools import (
    conversations_table, insights_table, invitations_table,
    notifications_table, projects_table, tasks_table,
)
from api.deps import scan_all_pages

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


def list_projects(uid: str, user_email: str) -> list:
    """Lista todos los proyectos con datos enriquecidos (task counts, insights, etc.)."""
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
                        print(f"[list_projects] No se pudo auto-aceptar invitación {inv.get('invitationId')}: {e}")
                # Cargar el proyecto y añadirlo
                proj_item = projects_table.get_item(Key={'projectId': pid}).get('Item')
                if proj_item:
                    proj_item['_invited'] = True
                    # Auto-añadir al participants[] del proyecto si todavía no figura
                    # (idempotente: si ya está por email, no duplicamos)
                    try:
                        current_parts = proj_item.get('participants', []) or []
                        inv_email_norm = (inv.get('email', '') or '').strip().lower()
                        already_in = any(
                            (p.get('email', '') or '').strip().lower() == inv_email_norm
                            for p in current_parts if isinstance(p, dict)
                        )
                        if inv_email_norm and not already_in:
                            # Nombre = parte local del email (kotomivega@gmail.com → kotomivega)
                            derived_name = inv_email_norm.split('@')[0] or inv_email_norm
                            new_part = {
                                'nombre': derived_name,
                                'email': inv_email_norm,
                                'telefono': '',
                                'rol': 'Invitado',
                            }
                            updated_parts = current_parts + [new_part]
                            projects_table.update_item(
                                Key={'projectId': pid},
                                UpdateExpression="SET participants = :p",
                                ExpressionAttributeValues={':p': updated_parts},
                            )
                            proj_item['participants'] = updated_parts
                            print(f"[list_projects] Invitado {inv_email_norm} añadido a participants de {pid}")
                    except Exception as e:
                        print(f"[list_projects] No se pudo añadir invitado a participants de {pid}: {e}")
                    invited_projects.append(proj_item)
                    seen_proj_ids.add(pid)
        except Exception as e:
            print(f"[list_projects] Error consultando invitaciones: {e}")

    projects = own_projects + shared_projects + invited_projects

    # FIX (#23): el filtro previo Attr('userId').eq(uid) ocultaba las
    # tareas e insights de proyectos donde el usuario es shared o invited.
    # Las tareas/insights persistidos llevan userId = owner del proyecto
    # (no del usuario que las consulta), así un invitado nunca matcheaba y
    # veía los proyectos correctos pero vacíos de tareas/insights.
    #
    # Solución: scan completo y filtro client-side por projectId ∈ proyectos
    # accesibles. La autorización ya se hizo arriba (own + shared + invited):
    # ese set define exactamente qué puede ver, y aplicarlo aquí garantiza
    # que NUNCA caigan tareas de proyectos sin acceso.
    # Coste: mismo scan completo que antes; DynamoDB aplicaba el filtro
    # POST-scan internamente, así que el cambio no es más lento en práctica.
    accessible_pids = {p['projectId'] for p in projects}

    all_tasks_raw = scan_all_pages(tasks_table)
    all_tasks = [t for t in all_tasks_raw if t.get('projectId') in accessible_pids]

    all_insights_raw = scan_all_pages(insights_table)
    all_insights = sorted(
        [i for i in all_insights_raw if i.get('projectId') in accessible_pids],
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

            # Contar subtareas hijas de esta task (si las tiene)
            children = [c for c in proj_tasks if c.get('parentTaskId', '') == t.get('taskId', '')]
            children_done = len([c for c in children if c.get('status') == 'done'])
            tasks_list.append({
                'id': t.get('taskId', ''),
                'text': t.get('text', ''),
                'status': t.get('status', 'pending'),
                'description': t.get('description', ''),
                'overdue': is_overdue,
                'blockedReason': t.get('blockedReason', ''),
                'startDate': t.get('startDate', ''),
                'dueDate': t.get('dueDate', ''),
                'parentTaskId': t.get('parentTaskId', ''),
                'subtasksCount': len(children),
                'subtasksDone': children_done,
                'assignedTo': {
                    'nombre': assigned,
                    'iniciales': iniciales(assigned),
                    'color': 'from-violet-500 to-indigo-600',
                },
                'tags': tags,
            })

        # ¿El usuario logueado es el dueño de este proyecto?
        # Sirve para que el frontend oculte acciones owner-only
        # (eliminar, invitar, añadir/quitar participantes) a invitados.
        is_owner = proj.get('userId') == uid
        # Si NO es owner, ¿cómo entró? (sharedByEmail o invited)
        role = 'owner' if is_owner else ('invitedByEmail' if proj.get('_shared') else ('invitedByLink' if proj.get('_invited') else 'collaborator'))

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
            # === Permisos para el frontend ===
            'isOwner': is_owner,
            'role': role,
        })

    # Ordenar: activos primero, luego por nombre
    enriched.sort(key=lambda p: (0 if p['status'] == 'active' else 1, p['name']))
    return enriched


def get_project_detail(uid: str, project_id: str) -> dict:
    """Obtiene un proyecto específico con todos sus datos."""
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


def create_project(uid: str, name: str, description: str, project_type: str,
                   channels, participants, timing: str, delivery_date: str) -> dict:
    """Crea un nuevo proyecto con análisis IA, notificaciones e insights.
    Usa la función compartida `create_project_full` (también usada por el flujo de WhatsApp)."""
    from agent.project_helpers import create_project_full
    return create_project_full(
        user_id=uid,
        name=name,
        description=description,
        project_type=project_type,
        channels=channels,
        participants=participants,
        timing=timing or '',
        delivery_date=delivery_date or ''
    )


def update_participants(uid: str, project_id: str, participants: list) -> dict:
    """Actualiza los participantes de un proyecto (incluye teléfonos)."""
    projects_table.update_item(
        Key={'projectId': project_id},
        UpdateExpression="SET participants = :p",
        ExpressionAttributeValues={':p': participants},
        ConditionExpression=Attr('userId').eq(uid)
    )
    return {"success": True, "projectId": project_id}


def invite_user(uid: str, project_id: str, email: str = '', phone: str = '',
                name: str = '', role: str = '', send_notification: bool = True) -> dict:
    """Añade una persona al equipo del proyecto. Acepta email y/o teléfono.

    - Si viene email + send_notification=True: crea usuario en Cognito (le manda
      email con contraseña temporal) y guarda invitación pendiente.
    - Si viene teléfono + send_notification=True: manda mensaje de WhatsApp via
      Twilio avisando que fue añadido al proyecto.
    - En todos los casos: registra el contacto en participants[] del proyecto.
    - Si send_notification=False: solo registra el contacto, sin notificar.

    Reglas: requiere al menos email O teléfono. Si solo viene teléfono, NO crea
    cuenta en Cognito (no podríamos autenticar a alguien solo con número).
    """
    from agent.tools import enviar_notificacion, set_current_user, _normalize_e164

    email = (email or '').strip().lower()
    phone_raw = (phone or '').strip()
    name = (name or '').strip()
    role = (role or 'Invitado').strip()
    send_notification = send_notification if send_notification is not None else True

    # Validar al menos un canal
    if not email and not phone_raw:
        raise HTTPException(status_code=400, detail="Se requiere email o teléfono")
    if email and '@' not in email:
        raise HTTPException(status_code=400, detail="Email inválido")

    # Validar teléfono si viene
    phone = ''
    if phone_raw:
        phone = _normalize_e164(phone_raw) or ''
        if not phone:
            raise HTTPException(status_code=400, detail=f"Teléfono inválido: '{phone_raw}'. Usa formato E.164 (+34600000000)")

    # Verificar que el proyecto pertenece al usuario
    proj = projects_table.get_item(Key={'projectId': project_id}).get('Item')
    if not proj:
        raise HTTPException(status_code=404, detail="Proyecto no encontrado")
    if proj.get('userId') != uid:
        raise HTTPException(status_code=403, detail="No tienes permiso sobre este proyecto")

    # 1) AÑADIR/ACTUALIZAR EN participants[] DEL PROYECTO
    # ────────────────────────────────────────────────────
    # Match por email si viene, sino por teléfono. Si ya existe, actualizamos.
    # Si no, lo añadimos.
    current_parts = proj.get('participants', []) or []
    existing_idx = None
    for i, p in enumerate(current_parts):
        if not isinstance(p, dict):
            continue
        p_email = (p.get('email', '') or '').strip().lower()
        p_phone = (p.get('telefono', '') or '').strip()
        if email and p_email == email:
            existing_idx = i
            break
        if phone and p_phone == phone:
            existing_idx = i
            break
    derived_name = name or (email.split('@')[0] if email else phone)
    new_part = {
        'nombre': derived_name,
        'email': email,
        'telefono': phone,
        'rol': role,
    }
    if existing_idx is not None:
        # Merge: no sobreescribir campos llenos con vacíos
        current = current_parts[existing_idx]
        for k, v in new_part.items():
            if v:
                current[k] = v
        current_parts[existing_idx] = current
    else:
        current_parts.append(new_part)
    try:
        projects_table.update_item(
            Key={'projectId': project_id},
            UpdateExpression="SET participants = :p",
            ExpressionAttributeValues={':p': current_parts},
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error guardando participante: {str(e)}")

    # Si NO hay que notificar, terminamos acá (solo se registró el contacto)
    if not send_notification:
        return {
            "success": True,
            "saved": True,
            "notified": False,
            "message": "Contacto añadido al proyecto sin notificación.",
        }

    # 2) ENVÍO DE NOTIFICACIONES (email y/o WhatsApp)
    # ───────────────────────────────────────────────
    notify_email_result = None
    notify_whatsapp_result = None

    # 2a) Email: crea usuario en Cognito (si no existe) y guarda invitación
    if email:
        notify_email_result = _invite_by_email(
            email=email, uid=uid, project_id=project_id,
            project_name=proj.get('name', ''),
        )

    # 2b) WhatsApp: solo notificación, NO crea cuenta.
    # El mensaje cambia según si también se envió un email:
    #  - Con email: le decimos que revise su correo para el link de acceso.
    #  - Sin email: solo aviso de notificación; sin email no puede entrar a la web.
    if phone:
        # Necesitamos contexto de usuario para que la tool registre la notif
        set_current_user(uid, "")
        project_name = proj.get('name', 'tu proyecto')
        email_was_sent = bool(
            email
            and notify_email_result
            and notify_email_result.get('success') is not False
        )
        if email_was_sent:
            mensaje = (
                f"Hola {derived_name}, te añadieron al proyecto *{project_name}* en OneBox. "
                f"Te enviamos un correo a {email} con el enlace para registrarte y acceder a la plataforma. "
                f"Después de entrar te avisaremos por aquí de tareas bloqueadas y novedades importantes."
            )
        else:
            mensaje = (
                f"Hola {derived_name}, te añadieron al proyecto *{project_name}* en OneBox. "
                f"Te avisaremos por aquí de tareas bloqueadas y novedades importantes del proyecto."
            )
        notify_whatsapp_result = enviar_notificacion(
            destinatario=phone,
            mensaje=mensaje,
            canal='whatsapp',
            project_id=project_id,
            project_name=project_name,
        )

    return {
        "success": True,
        "saved": True,
        "notified": True,
        "email": notify_email_result,
        "whatsapp": notify_whatsapp_result,
    }


def _invite_by_email(email: str, uid: str, project_id: str, project_name: str) -> dict:
    """Helper: lógica de invitación por email (Cognito + invitations_table).
    Devuelve dict con el resultado en vez de raise para que el endpoint pueda
    combinar email + whatsapp en una sola respuesta."""
    # Crear usuario en Cognito (si no existe). Cognito enviará el email automáticamente.
    cognito_client = boto3.client('cognito-idp', region_name='us-east-1')
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
        # No hacemos raise: devolvemos error en el dict para no romper el WhatsApp
        return {"success": False, "error": f"Cognito error: {str(e)}"}

    # Guardar invitación (pendiente). Si ya estaba aceptada, no duplicamos.
    now = datetime.utcnow().isoformat()
    invitation_id = str(uuid.uuid4())
    try:
        invitations_table.put_item(Item={
            'invitationId': invitation_id,
            'email': email,
            'projectId': project_id,
            'projectName': project_name,
            'invitedBy': uid,
            'status': 'pending',
            'createdAt': now,
        })
    except Exception as e:
        return {"success": False, "error": f"DynamoDB error: {str(e)}"}

    # Mensaje contextual según el caso + share_url cuando hace falta avisar
    # manualmente al invitante (porque Cognito NO envía email).
    #
    # Casos donde Cognito SÍ envía email automáticamente:
    #   - Usuario nuevo (admin_create_user con DesiredDeliveryMediums=EMAIL)
    #   - Usuario en FORCE_CHANGE_PASSWORD reenviado (MessageAction=RESEND)
    #
    # Casos donde Cognito NO envía email → ahí necesitamos share_url:
    #   - userStatus = 'EXTERNAL_PROVIDER' (la persona se registró con Google)
    #   - userStatus = 'CONFIRMED'         (la persona ya activó la cuenta)
    # En ambos casos, sin share_url el invitado NO se entera de que fue invitado.
    needs_manual_share = cognito_user_existed and not email_resent
    share_url = ''
    if needs_manual_share:
        # Link directo a la home logueada de la app. Cuando la persona entre,
        # el endpoint /api/projects auto-acepta su invitación y le aparece
        # el proyecto en la lista.
        share_url = f"https://www.oneboxmanager.com/?proyecto={project_id}"

    if not cognito_user_existed:
        msg = "Invitación enviada. El usuario recibirá un correo con su contraseña temporal."
    elif email_resent:
        msg = "El usuario ya tenía cuenta pendiente de activar. Le reenviamos el correo de invitación."
    elif user_status == 'CONFIRMED':
        msg = (
            "El usuario ya tiene cuenta activa pero NO recibe email automático. "
            "Comparte el link directo del proyecto que está debajo."
        )
    else:
        # EXTERNAL_PROVIDER (Google), UNCONFIRMED, etc. — Cognito no manda nada
        msg = (
            "El usuario está registrado con login externo (Google) y Cognito NO "
            "envía email automático. Comparte el link directo del proyecto."
        )

    return {
        "success": True,
        "invitationId": invitation_id,
        "email": email,
        "cognitoUserExisted": cognito_user_existed,
        "emailResent": email_resent,
        "userStatus": user_status,
        "message": msg,
        # share_url: si no está vacío, el frontend debe mostrar el botón
        # "Copiar link para compartir" para que el invitante avise manualmente
        # al invitado por su canal de preferencia (WhatsApp, Slack, etc.).
        "share_url": share_url,
        "needs_manual_share": needs_manual_share,
    }


def remove_participant(uid: str, project_id: str, email: str = '', phone: str = '', name: str = '') -> dict:
    """Elimina un participante del equipo del proyecto.

    Comportamiento (decisiones del producto):
      1. Lo saca de participants[] del proyecto.
      2. Vacía assignedTo de TODAS las tareas del proyecto asignadas a esa
         persona → quedan como 'Sin asignar' (no se borran).
      3. Si existía invitación en onebox-invitations por su email para este
         proyecto, la borra → pierde el acceso si era invitada.
      4. NO envía notificación al eliminado.

    Solo el owner del proyecto puede eliminar participantes (RBAC).
    """
    proj = projects_table.get_item(Key={'projectId': project_id}).get('Item')
    if not proj:
        raise HTTPException(status_code=404, detail="Proyecto no encontrado")
    if proj.get('userId') != uid:
        raise HTTPException(status_code=403, detail="Solo el dueño puede eliminar participantes")

    # Buscar al participante por email > phone > name
    target_email = (email or '').strip().lower()
    target_phone = (phone or '').strip()
    target_name = (name or '').strip()
    if not (target_email or target_phone or target_name):
        raise HTTPException(status_code=400, detail="Indica email, phone o name del participante a eliminar")

    parts = proj.get('participants', []) or []
    target_idx = None
    for i, p in enumerate(parts):
        if not isinstance(p, dict):
            continue
        p_email = (p.get('email', '') or '').strip().lower()
        p_phone = (p.get('telefono', '') or '').strip()
        p_name = (p.get('nombre', '') or '').strip()
        if target_email and p_email == target_email:
            target_idx = i
            break
        if target_phone and p_phone == target_phone:
            target_idx = i
            break
        if target_name and p_name == target_name:
            target_idx = i
            break

    if target_idx is None:
        raise HTTPException(status_code=404, detail="Participante no encontrado")

    # 1) Quitar de participants[]
    removed = parts.pop(target_idx)
    try:
        projects_table.update_item(
            Key={'projectId': project_id},
            UpdateExpression="SET participants = :p",
            ExpressionAttributeValues={':p': parts},
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error actualizando proyecto: {str(e)}")

    # 2) Vaciar assignedTo de sus tareas en este proyecto
    tasks_orphaned = 0
    removed_name = (removed.get('nombre', '') or '').strip()
    if removed_name:
        try:
            tres = tasks_table.scan(
                FilterExpression=Attr('projectId').eq(project_id) & Attr('assignedTo').eq(removed_name)
            )
            for t in tres.get('Items', []):
                tasks_table.update_item(
                    Key={'projectId': t['projectId'], 'taskId': t['taskId']},
                    UpdateExpression="SET assignedTo = :empty",
                    ExpressionAttributeValues={':empty': ''},
                )
                tasks_orphaned += 1
        except Exception as e:
            print(f"[remove_participant] error vaciando assignedTo de tareas: {e}")

    # 3) Borrar invitaciones del removido para este proyecto (pierde acceso)
    invitations_revoked = 0
    removed_email = (removed.get('email', '') or '').strip().lower()
    if removed_email:
        try:
            inv_resp = invitations_table.query(
                IndexName='email-index',
                KeyConditionExpression=Key('email').eq(removed_email),
            )
            for inv in inv_resp.get('Items', []):
                if inv.get('projectId') == project_id:
                    invitations_table.delete_item(Key={'invitationId': inv['invitationId']})
                    invitations_revoked += 1
        except Exception as e:
            print(f"[remove_participant] error borrando invitaciones: {e}")

    return {
        "success": True,
        "removed": {
            "nombre": removed.get('nombre', ''),
            "email": removed_email,
            "telefono": removed.get('telefono', ''),
        },
        "tasksOrphaned": tasks_orphaned,
        "invitationsRevoked": invitations_revoked,
    }


def delete_project(uid: str, project_id: str) -> dict:
    """Elimina un proyecto y sus datos relacionados (insights, notificaciones, tareas)."""
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
