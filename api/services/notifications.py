"""Lógica interna de notificaciones programadas: revisión de SLA y envío de
resúmenes por WhatsApp a los responsables. Pensado para EventBridge cron."""


def send_scheduled_notifications() -> dict:
    """Revisa SLA (tareas bloqueadas/vencidas) y envía WhatsApp a los responsables.
    No necesita Bedrock — lógica directa."""
    from agent.tools import (
        enviar_notificacion, obtener_contactos_proyecto, verificar_sla
    )

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
