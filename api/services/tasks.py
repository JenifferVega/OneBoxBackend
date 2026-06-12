"""Lógica interna de tareas: listado, creación, actualización (con aviso de
bloqueo y de asignación) y borrado con subtareas."""
import threading
import uuid
from datetime import datetime

from boto3.dynamodb.conditions import Attr
from fastapi import HTTPException

from agent.tools import projects_table, tasks_table
from api.services.access import has_project_access


def _notify_assignment_async(project_id: str, task_text: str, assigned_to_name: str,
                              due_date: str = "", actor_uid: str = "",
                              actor_email: str = "") -> None:
    """Lanza en background la notificación a la persona recién asignada.

    Busca al participant cuyo `nombre` (o `email`) matchee con assigned_to_name
    y le manda email Y/O WhatsApp según los canales que tenga.

    Por qué en thread: no debe bloquear la respuesta del endpoint. Si SES o
    Twilio tardan o fallan, el usuario que creó/editó la tarea no debería
    esperar.

    IMPORTANTE — contextvars y threads:
      Las tools del agente (enviar_notificacion, _log_notification) leen
      _current_uid() de un contextvar. Los threads de Python NO heredan
      contextvars automáticamente, así que dentro del thread hay que
      re-establecer el contexto del usuario (actor_uid/actor_email) o las
      llamadas fallan con "Tool del agente invocada sin contexto de usuario".
    """
    def _worker():
        # Re-establecer el contexto del usuario en este thread (Python no
        # propaga contextvars a threads.threading.Thread). Si no se llama,
        # _current_uid() lanza RuntimeError y la notificación falla en silencio.
        from agent.tools import set_current_user, clear_current_user
        if actor_uid:
            set_current_user(actor_uid, actor_email or '')
        try:
            from agent.tools import enviar_notificacion
            proj = projects_table.get_item(Key={'projectId': project_id}).get('Item') or {}
            if not proj:
                return
            proj_name = proj.get('name', '')
            target_lower = (assigned_to_name or '').strip().lower()
            if not target_lower:
                return

            # Encontrar el participant: match por nombre exacto o email.
            target_part = None
            for part in proj.get('participants', []) or []:
                if not isinstance(part, dict):
                    continue
                p_name = (part.get('nombre', '') or '').strip().lower()
                p_email = (part.get('email', '') or '').strip().lower()
                if p_name == target_lower or (p_email and p_email == target_lower):
                    target_part = part
                    break
            if not target_part:
                # Si no aparece como participant, no podemos mandar nada.
                # (En el futuro podríamos también buscar por Cognito, pero hoy
                # solo notificamos a participantes registrados en el proyecto.)
                print(f"[task assignment] '{assigned_to_name}' no es participant de {project_id}, skip")
                return

            email = (target_part.get('email', '') or '').strip().lower()
            tel = (target_part.get('telefono') or target_part.get('phone') or '').strip()

            # Mensaje conciso (mismo para email y WhatsApp).
            lines = [
                f"📌 *{proj_name}* — Tarea nueva asignada a ti",
                "",
                f"Hola {target_part.get('nombre', '')}, te asignaron una tarea:",
                "",
                f"  • {task_text}",
            ]
            if due_date:
                lines.append(f"  📅 Vence: {due_date}")
            lines.append("")
            lines.append("Entra a OneBox para verla: https://www.oneboxmanager.com")
            msg = "\n".join(lines)

            sent_count = 0
            if email:
                res = enviar_notificacion(email, msg, canal='email',
                                          project_id=project_id, project_name=proj_name)
                if res.get('success') or res.get('status') == 'skipped_unverified':
                    sent_count += 1
            if tel:
                res = enviar_notificacion(tel, msg, canal='whatsapp',
                                          project_id=project_id, project_name=proj_name)
                if res.get('success'):
                    sent_count += 1
            print(f"[task assignment] notif a '{assigned_to_name}' → "
                  f"{sent_count} canal(es) (email={bool(email)}, tel={bool(tel)})")
        except Exception as e:
            print(f"[task assignment] error notificando: {e}")
        finally:
            # Limpiar el contexto del thread por higiene (los contextvars
            # del thread podrían persistir si el thread pool lo reusara).
            try:
                clear_current_user()
            except Exception:
                pass

    threading.Thread(target=_worker, daemon=True).start()


def list_tasks(uid: str, user_email: str, project_id: str) -> list:
    """Lista tareas de un proyecto. Accesible para owner Y invitados."""
    has, _is_owner, _proj = has_project_access(uid, user_email, project_id)
    if not has:
        raise HTTPException(status_code=403, detail="Sin acceso a este proyecto")
    # Tareas del proyecto (todas, sin filtrar por userId — pertenecen al proyecto)
    result = tasks_table.scan(
        FilterExpression=Attr('projectId').eq(project_id)
    )
    return sorted(
        result.get('Items', []),
        key=lambda x: x.get('createdAt', ''),
        reverse=True
    )


def create_task(uid: str, user_email: str, project_id: str, req) -> dict:
    """Crea una tarea en un proyecto. Accesible para owner Y invitados.
    La tarea queda con userId = owner del proyecto (no del creador), para que
    TODOS los con acceso al proyecto puedan editarla. Se guarda createdBy."""
    has, _is_owner, proj = has_project_access(uid, user_email, project_id)
    if not has:
        raise HTTPException(status_code=403, detail="Sin acceso a este proyecto")
    task_id = str(uuid.uuid4())
    now = datetime.utcnow().isoformat()
    owner_uid = proj.get('userId', uid)
    item = {
        'projectId': project_id,
        'taskId': task_id,
        'userId': owner_uid,           # tarea pertenece al proyecto, no al creador
        'createdBy': uid,              # quién la creó (owner o invitado)
        'createdByEmail': (user_email or '').strip().lower(),
        'text': req.text,
        'description': req.description,
        'status': req.status,
        'assignedTo': req.assigned_to,
        'startDate': req.start_date or '',
        'dueDate': req.due_date or '',
        'parentTaskId': (req.parent_task_id or '').strip(),  # '' si es tarea raíz
        'createdAt': now,
    }
    tasks_table.put_item(Item=item)

    # Si la tarea se creó CON alguien asignado, notificarle inmediatamente.
    # Pasamos uid/email del actor: el thread necesita re-establecer el
    # contextvar de usuario para que las tools del agente funcionen.
    if (req.assigned_to or '').strip():
        _notify_assignment_async(
            project_id=project_id,
            task_text=req.text or '',
            assigned_to_name=req.assigned_to,
            due_date=req.due_date or '',
            actor_uid=uid,
            actor_email=user_email or '',
        )

    return {"success": True, "taskId": task_id, "text": req.text}


def update_task(uid: str, user_email: str, task_id: str, req) -> dict:
    """Actualiza una tarea. Accesible para owner Y invitados con acceso al proyecto."""
    # Buscamos la tarea por taskId solamente; luego verificamos acceso al proyecto.
    result = tasks_table.scan(
        FilterExpression=Attr('taskId').eq(task_id)
    )
    items = result.get('Items', [])
    if not items:
        raise HTTPException(status_code=404, detail="Tarea no encontrada")

    task = items[0]
    has, _is_owner, _proj = has_project_access(uid, user_email, task['projectId'])
    if not has:
        raise HTTPException(status_code=403, detail="Sin acceso a este proyecto")
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
    if req.parent_task_id is not None:
        # '' = mover a raíz; valor = convertir en subtarea de esa task.
        # No permitimos que una tarea sea subtarea de sí misma.
        if req.parent_task_id and req.parent_task_id == task_id:
            raise HTTPException(status_code=400, detail="Una tarea no puede ser subtarea de sí misma")
        updates['parentTaskId'] = req.parent_task_id.strip()

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

    # Notificar inmediato a la persona si cambió su asignación (de vacío o de
    # otra persona a ELLA). Cuando solo se edita otra cosa (texto, fecha) y
    # assigned_to ya estaba seteado a la misma persona, NO mandamos otra vez.
    old_assigned = (task.get('assignedTo', '') or '').strip()
    new_assigned = updates.get('assignedTo')
    if (
        new_assigned is not None
        and new_assigned.strip()                              # asigna a alguien
        and new_assigned.strip().lower() != old_assigned.lower()  # y cambió
    ):
        _notify_assignment_async(
            project_id=task['projectId'],
            task_text=updates.get('text', task.get('text', '')),
            assigned_to_name=new_assigned,
            due_date=updates.get('dueDate', task.get('dueDate', '')),
            actor_uid=uid,
            actor_email=user_email or '',
        )

    # Si la tarea ACABA de pasar a 'blocked', avisar por WhatsApp a los
    # participantes con teléfono (en un hilo, para no bloquear la respuesta).
    old_status = task.get('status', '')
    new_status = updates.get('status')
    if new_status == 'blocked' and old_status != 'blocked':
        proj_id = task['projectId']  # threading ya importado arriba
        task_text = updates.get('text', task.get('text', ''))
        reason = (updates.get('blockedReason') or task.get('blockedReason') or '').strip()
        # Capturar contexto para el thread (los contextvars no se propagan
        # automáticamente — mismo fix que en _notify_assignment_async).
        thread_uid = uid
        thread_email = user_email or ''

        def _notify_blocked():
            from agent.tools import enviar_notificacion, set_current_user, clear_current_user
            if thread_uid:
                set_current_user(thread_uid, thread_email)
            try:
                proj = projects_table.get_item(Key={'projectId': proj_id}).get('Item') or {}
                if not proj:
                    return
                # El acceso ya se validó antes de llegar aquí; el invitado
                # también puede disparar notificación al bloquear.
                # Notificamos por AMBOS canales: WhatsApp y email.
                proj_name = proj.get('name', '')
                msg = (f"🔴 OneBox: la tarea \"{task_text}\" del proyecto "
                       f"\"{proj_name}\" está BLOQUEADA y requiere atención.")
                if reason:
                    msg += f"\nMotivo: {reason}"
                sent = 0
                for part in proj.get('participants', []):
                    tel = (part.get('telefono') or part.get('phone') or '').strip()
                    email = (part.get('email', '') or '').strip().lower()
                    if tel:
                        res = enviar_notificacion(tel, msg, canal='whatsapp',
                                                  project_id=proj_id, project_name=proj_name)
                        if res.get('success'):
                            sent += 1
                    if email:
                        res = enviar_notificacion(email, msg, canal='email',
                                                  project_id=proj_id, project_name=proj_name)
                        if res.get('success'):
                            sent += 1
                print(f"[update_task] Tarea bloqueada → {sent} notificación(es) enviada(s)")
            except Exception as e:
                print(f"[update_task] Error notificando bloqueo: {e}")
            finally:
                try:
                    clear_current_user()
                except Exception:
                    pass
        threading.Thread(target=_notify_blocked, daemon=True).start()

    return {"success": True, "taskId": task_id}


def delete_task(uid: str, user_email: str, task_id: str, cascade: bool) -> dict:
    """Elimina una tarea. Owner Y invitados con acceso al proyecto pueden borrar.

    Si la tarea tiene subtareas:
      - cascade=true  → borra también todos los hijos.
      - cascade=false → los hijos quedan como tareas raíz (parentTaskId = '').
    """
    # Localizar la tarea por taskId
    result = tasks_table.scan(FilterExpression=Attr('taskId').eq(task_id))
    items = result.get('Items', [])
    if not items:
        raise HTTPException(status_code=404, detail="Tarea no encontrada")
    task = items[0]
    project_id = task['projectId']
    has, _is_owner, _proj = has_project_access(uid, user_email, project_id)
    if not has:
        raise HTTPException(status_code=403, detail="Sin acceso a este proyecto")

    # Buscar subtareas
    children_resp = tasks_table.scan(
        FilterExpression=Attr('projectId').eq(project_id) & Attr('parentTaskId').eq(task_id)
    )
    children = children_resp.get('Items', [])

    if children:
        if cascade:
            # Borrar todos los hijos también.
            for child in children:
                tasks_table.delete_item(Key={'projectId': project_id, 'taskId': child['taskId']})
        else:
            # Promover hijos a tareas raíz.
            for child in children:
                tasks_table.update_item(
                    Key={'projectId': project_id, 'taskId': child['taskId']},
                    UpdateExpression="SET parentTaskId = :p",
                    ExpressionAttributeValues={':p': ''},
                )

    tasks_table.delete_item(Key={'projectId': project_id, 'taskId': task_id})
    return {"success": True, "taskId": task_id, "childrenAffected": len(children), "cascade": cascade}
