"""Lógica interna de insights: listado enriquecido para el feed del frontend."""
from typing import Optional

from agent.tools import insights_table
from api.deps import scan_all_pages
from api.services.access import accessible_project_ids


def list_insights(uid: str, user_email: str, type: Optional[str] = None) -> list:
    """Lista insights/acciones de la IA de TODOS los proyectos a los que el
    usuario tiene acceso (own + invitados aceptados). Opcionalmente filtra por tipo."""
    # Obtener los proyectos a los que el usuario tiene acceso
    accessible_pids = accessible_project_ids(uid, user_email)

    # Si no tiene acceso a ninguno, devolver lista vacía (no escanear todo)
    if not accessible_pids:
        return []

    all_insights = scan_all_pages(insights_table)
    insights_filtered = [
        i for i in all_insights
        if i.get('projectId') in accessible_pids
        and (not type or i.get('type') == type)
    ]
    insights = sorted(
        insights_filtered,
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
