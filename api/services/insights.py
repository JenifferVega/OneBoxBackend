"""Insights internal logic: enriched listing for the frontend feed."""
from typing import Optional

from agent.tools import insights_table
from api.deps import scan_all_pages
from api.services.access import accessible_project_ids


def list_insights(uid: str, user_email: str, type: Optional[str] = None) -> list:
    """List AI insights/actions for ALL projects the user has access to
    (own + accepted invitations). Optionally filters by type."""
    # Fetch the projects the user has access to
    accessible_pids = accessible_project_ids(uid, user_email)

    # If they have access to none, return an empty list (do not scan everything)
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
            'decision':                {'badge': 'Decision',           'badgeColor': 'bg-blue-500/20 text-blue-400 border-blue-500/30',        'icon': '✓',  'iconColor': 'bg-emerald-500/20 text-emerald-400'},
            'blocker':                 {'badge': 'Client blocker',     'badgeColor': 'bg-red-500/20 text-red-400 border-red-500/30',           'icon': '🚧', 'iconColor': 'bg-red-500/20 text-red-400'},
            'task_created':            {'badge': 'Task',               'badgeColor': 'bg-violet-500/20 text-violet-400 border-violet-500/30',  'icon': '📋', 'iconColor': 'bg-violet-500/20 text-violet-400'},
            'work_done':               {'badge': 'Work done',          'badgeColor': 'bg-emerald-500/20 text-emerald-400 border-emerald-500/30','icon': '✅', 'iconColor': 'bg-emerald-500/20 text-emerald-400'},
            'followup':                {'badge': 'Follow-up',          'badgeColor': 'bg-indigo-500/20 text-indigo-400 border-indigo-500/30',  'icon': '📧', 'iconColor': 'bg-indigo-500/20 text-indigo-400'},
            'risk':                    {'badge': 'Risk',               'badgeColor': 'bg-orange-500/20 text-orange-400 border-orange-500/30',  'icon': '⚠',  'iconColor': 'bg-amber-500/20 text-amber-400'},
            'sla':                     {'badge': 'SLA',                'badgeColor': 'bg-red-500/20 text-red-400 border-red-500/30',           'icon': '🚨', 'iconColor': 'bg-red-500/20 text-red-400'},
            'notification':            {'badge': 'Notification',       'badgeColor': 'bg-sky-500/20 text-sky-400 border-sky-500/30',           'icon': '📱', 'iconColor': 'bg-sky-500/20 text-sky-400'},
            'classification':          {'badge': 'Classification',     'badgeColor': 'bg-teal-500/20 text-teal-400 border-teal-500/30',        'icon': '🧠', 'iconColor': 'bg-teal-500/20 text-teal-400'},
            'summary':                 {'badge': 'Summary',            'badgeColor': 'bg-purple-500/20 text-purple-400 border-purple-500/30',  'icon': '📊', 'iconColor': 'bg-purple-500/20 text-purple-400'},
            'project_characterization':{'badge': 'Actual type',        'badgeColor': 'bg-fuchsia-500/20 text-fuchsia-300 border-fuchsia-500/30','icon': '🎯', 'iconColor': 'bg-fuchsia-500/20 text-fuchsia-300'},
            'client_profile':          {'badge': 'Client profile',     'badgeColor': 'bg-cyan-500/20 text-cyan-300 border-cyan-500/30',        'icon': '👤', 'iconColor': 'bg-cyan-500/20 text-cyan-300'},
            'key_insight':             {'badge': 'Key insight',        'badgeColor': 'bg-amber-500/20 text-amber-300 border-amber-500/30',     'icon': '💡', 'iconColor': 'bg-amber-500/20 text-amber-300'},
            'metric':                  {'badge': 'Metric',             'badgeColor': 'bg-lime-500/20 text-lime-300 border-lime-500/30',        'icon': '📈', 'iconColor': 'bg-lime-500/20 text-lime-300'},
            'tech_issue':              {'badge': 'Technical issue',    'badgeColor': 'bg-rose-500/20 text-rose-300 border-rose-500/30',        'icon': '🔧', 'iconColor': 'bg-rose-500/20 text-rose-300'},
        }
        ui = type_map.get(ins_type, type_map['task_created'])

        # OBSERVATION vs ACTION.
        # Most insight types are things the AI NOTICED while reading a
        # project -- a risk, a summary, who the client is. Nothing was
        # performed. Labelling those "EXECUTED" (as this did for every single
        # row, because no code path ever wrote status='review' and the final
        # else fell through to EXECUTED anyway) told the user an action had
        # been taken when only a note had been written.
        #
        # Only the types below actually have a side effect somewhere else:
        # a task row, a sent message, an email assigned to a project.
        ACTION_TYPES = {'task_created', 'notification', 'classification',
                        'followup', 'sla'}
        is_action = ins_type in ACTION_TYPES

        if status == 'review':
            action_type = 'NEEDS REVIEW'
            action_color = 'text-amber-400'
            chip_color = 'bg-amber-500/20 text-amber-400'
            fe_status = 'review'
        elif status == 'error':
            action_type = 'FAILED'
            action_color = 'text-red-400'
            chip_color = 'bg-red-500/20 text-red-400'
            fe_status = 'error'
        elif is_action:
            # An action with no recorded actionsTaken is a claim we cannot
            # back up, so it is reported as recorded, not as executed.
            if actions_taken:
                action_type = 'EXECUTED'
                action_color = 'text-emerald-400'
                chip_color = 'bg-emerald-500/20 text-emerald-400'
                fe_status = 'executed'
            else:
                action_type = 'CREATED'
                action_color = 'text-violet-300'
                chip_color = 'bg-violet-500/20 text-violet-300'
                fe_status = 'executed'
        else:
            action_type = 'DETECTED'
            action_color = 'text-sky-300'
            chip_color = 'bg-sky-500/20 text-sky-300'
            fe_status = 'recorded'

        tags = []
        if ins.get('relatedPerson'):
            tags.append({'label': ins['relatedPerson'], 'color': 'bg-white/5 text-white/50'})
        tags.append({'label': action_type.title(), 'color': chip_color})

        enriched.append({
            'id': ins.get('insightId', ''),
            'insightId': ins.get('insightId', ''),
            'projectId': ins.get('projectId', ''),
            'projectName': ins.get('projectName', 'System'),
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
            'category': 'action' if is_action else 'observation',
            'chipColor': chip_color,
            'createdAt': created,
            'requiresReview': status == 'review',
        })

    return enriched
