"""Lógica interna de notificaciones programadas: revisión de pendientes
y envío de resúmenes por WhatsApp Y email. Pensado para EventBridge cron diario.

Diseño:
  - Para cada proyecto con tareas pendientes, se construye UN resumen.
  - Para cada participante:
      · si tiene email   → enviar_notificacion(email, mensaje, canal='email')
      · si tiene teléfono → enviar_notificacion(tel, mensaje, canal='whatsapp')
  - enviar_notificacion ya hace el sandbox-check de SES y registra en la tabla
    onebox-notifications. Si no hay canal en el participant, simplemente skip.
"""
from boto3.dynamodb.conditions import Attr


def send_scheduled_notifications() -> dict:
    """Cron diario: arma resumen de tareas pendientes por proyecto y manda
    a cada participante por email Y/O WhatsApp según los canales que tenga.

    "Pendientes" = status pending, in_progress o blocked (todo lo que no esté
    en done). Es la información que el usuario necesita ver de su equipo.
    """
    from agent.tools import (
        enviar_notificacion, projects_table, tasks_table, set_current_user,
        clear_current_user,
    )

    # Escaneo único de proyectos y tareas — más eficiente que llamar
    # obtener_contactos_proyecto por proyecto (que internamente vuelve a scan).
    try:
        proj_scan = projects_table.scan()
        all_projects = proj_scan.get('Items', [])
    except Exception as e:
        return {"success": False, "error": f"scan proyectos: {e}"}

    try:
        # Solo tareas no completadas. status=done o completed → excluidas.
        task_scan = tasks_table.scan(
            FilterExpression=Attr('status').ne('done') & Attr('status').ne('completed')
        )
        all_open_tasks = task_scan.get('Items', [])
    except Exception as e:
        return {"success": False, "error": f"scan tareas: {e}"}

    # Indexar tareas por projectId
    tasks_by_pid: dict = {}
    for t in all_open_tasks:
        pid = t.get('projectId', '')
        if pid:
            tasks_by_pid.setdefault(pid, []).append(t)

    notifications_sent = 0
    notifications_skipped = 0
    errors: list = []
    projects_processed = 0

    for proj in all_projects:
        pid = proj.get('projectId', '')
        if not pid:
            continue
        proj_tasks = tasks_by_pid.get(pid, [])
        if not proj_tasks:
            # Proyecto sin pendientes → nada que notificar
            continue

        project_name = proj.get('name', 'Proyecto')
        owner_uid = proj.get('userId', '')
        if not owner_uid:
            continue

        # Las tools del agente exigen contexto de usuario (multi-tenant).
        # Establecemos al owner del proyecto: es quien "envía" desde el sistema.
        set_current_user(owner_uid, '')
        try:
            participants = proj.get('participants', []) or []
            if not participants:
                continue

            # FIX: solo notificar a participantes que tienen tareas ASIGNADAS
            # a ellos. Antes mandaba a todos aunque las tareas estuvieran sin
            # asignar → ruido en proyectos con tareas no asignadas.
            #
            # Match: task.assignedTo == participant.nombre (o email). Las tareas
            # sin assignedTo no se cuentan para nadie y por ende nadie recibe
            # email/WhatsApp por esas. El owner puede verlas entrando a la app.
            project_processed_at_least_one = False
            for part in participants:
                if not isinstance(part, dict):
                    continue
                nombre = (part.get('nombre', '') or '').strip().lower()
                email = (part.get('email', '') or '').strip().lower()
                tel = (part.get('telefono', '') or '').strip()

                # Filtrar las tareas asignadas específicamente a este participante.
                # Match por nombre O por email (cualquiera de los dos).
                his_tasks = []
                for t in proj_tasks:
                    assigned = (t.get('assignedTo', '') or '').strip().lower()
                    if not assigned:
                        continue
                    if assigned == nombre or (email and assigned == email):
                        his_tasks.append(t)

                if not his_tasks:
                    # SKIP: este participante no tiene nada asignado en este
                    # proyecto → no recibe nada. Decisión de producto.
                    continue

                # Contar por estado SOLO de sus tareas
                pending = [t for t in his_tasks if t.get('status') == 'pending']
                in_progress = [t for t in his_tasks if t.get('status') == 'in_progress']
                blocked = [t for t in his_tasks if t.get('status') == 'blocked']

                # Mensaje personalizado para esta persona (incluye su nombre)
                lines = [
                    f"📋 *{project_name}* — Tus pendientes",
                    "",
                    f"Hola {part.get('nombre', '')}, tienes {len(his_tasks)} tarea(s) pendiente(s) en este proyecto.",
                    "",
                ]
                if blocked:
                    lines.append(f"🚫 Bloqueadas: {len(blocked)}")
                if in_progress:
                    lines.append(f"🔄 En curso: {len(in_progress)}")
                if pending:
                    lines.append(f"⏳ Por hacer: {len(pending)}")
                lines.append("")
                lines.append("📝 Detalle:")

                # Priorizar bloqueadas, después en curso, después pending.
                preview = blocked + in_progress + pending
                for t in preview[:10]:
                    status = t.get('status', '')
                    icon = {'pending': '⏳', 'in_progress': '🔄', 'blocked': '🚫'}.get(status, '•')
                    text = (t.get('text', '') or '')[:80]
                    due = (t.get('dueDate', '') or '').strip()
                    line = f"  {icon} {text}"
                    if due:
                        line += f" (vence {due})"
                    lines.append(line)
                if len(preview) > 10:
                    lines.append(f"  … y {len(preview) - 10} más")
                lines.append("")
                lines.append("Entra a OneBox para verlas: https://www.oneboxmanager.com")
                mensaje = "\n".join(lines)

                if email:
                    res = enviar_notificacion(
                        destinatario=email,
                        mensaje=mensaje,
                        canal='email',
                        project_id=pid,
                        project_name=project_name,
                    )
                    if res.get('success'):
                        notifications_sent += 1
                        project_processed_at_least_one = True
                    elif res.get('status') == 'skipped_unverified':
                        notifications_skipped += 1
                    else:
                        errors.append(f"email {email}: {res.get('error', '')[:120]}")

                if tel:
                    res = enviar_notificacion(
                        destinatario=tel,
                        mensaje=mensaje,
                        canal='whatsapp',
                        project_id=pid,
                        project_name=project_name,
                    )
                    if res.get('success'):
                        notifications_sent += 1
                        project_processed_at_least_one = True
                    else:
                        errors.append(f"whatsapp {tel}: {res.get('error', '')[:120]}")

            if project_processed_at_least_one:
                projects_processed += 1
        finally:
            clear_current_user()

    return {
        "success": True,
        "projects_processed": projects_processed,
        "notifications_sent": notifications_sent,
        "notifications_skipped": notifications_skipped,  # email no verificado en SES sandbox
        "errors": errors if errors else None,
    }
