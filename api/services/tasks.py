"""Lógica interna de tareas: listado, creación, actualización (con aviso de
bloqueo por WhatsApp) y borrado con subtareas."""
import uuid
from datetime import datetime

from boto3.dynamodb.conditions import Attr
from fastapi import HTTPException

from agent.tools import projects_table, tasks_table
from api.services.access import has_project_access


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
                if not proj:
                    return
                # El acceso ya se validó antes de llegar aquí; el invitado
                # también puede disparar notificación al bloquear (todos los
                # participantes con teléfono reciben WhatsApp).
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
