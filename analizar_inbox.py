"""
Script que ejecuta el agente IA para analizar el inbox y crear proyectos automáticamente.
"""
import json
from agent.llm import call_llm, extract_json_from_response
from agent.tools import (
    analizar_inbox, crear_proyecto, asignar_correo_a_proyecto,
    crear_insight, crear_tarea, listar_proyectos
)

SYSTEM_PROMPT = """Eres el agente inteligente de OneBox. Tu trabajo es analizar correos del inbox y organizarlos automáticamente.

Vas a recibir una lista de correos sin asignar. Para cada correo debes:

1. CLASIFICAR: ¿Es un correo de trabajo/proyecto o es newsletter/spam/notificación automática?
   - Newsletters (LinkedIn, Platzi, Farmacia, Puppis, etc.) → IGNORAR
   - Alertas de seguridad de Google → IGNORAR
   - Correos de trabajo con personas reales sobre temas de proyectos → PROCESAR

2. AGRUPAR: Los correos de trabajo que hablan del mismo tema/proyecto deben ir al mismo proyecto.

3. Para cada GRUPO de correos de trabajo, genera un plan con:
   - Crear el proyecto (si no existe)
   - Asignar cada correo al proyecto
   - Crear insights (blocker, decision, followup, risk, task_created)
   - Crear tareas detectadas

RESPONDE SOLO CON JSON con esta estructura:
{
  "analysis": [
    {
      "action": "ignore",
      "conversation_id": "...",
      "reason": "Newsletter de LinkedIn"
    },
    {
      "action": "create_project",
      "project_name": "Migración Cloud AWS",
      "project_description": "Migración de infraestructura on-premise a AWS",
      "project_type": "Infraestructura",
      "participants": [{"nombre": "Carlos Méndez", "rol": "Tech Lead"}],
      "emails_to_assign": ["conversation_id_1", "conversation_id_2"],
      "insights": [
        {
          "type": "blocker",
          "title": "Falta aprobación de seguridad para deploy",
          "description": "El equipo no puede avanzar sin aprobación del equipo de seguridad",
          "related_person": "Carlos Méndez"
        }
      ],
      "tasks": [
        {
          "text": "Obtener aprobación de seguridad para deploy a producción",
          "assigned_to": "Jeniffer",
          "status": "blocked"
        }
      ]
    }
  ]
}

IMPORTANTE:
- Agrupa correos del MISMO tema en UN solo proyecto
- Detecta bloqueos, decisiones, riesgos y tareas pendientes
- Los participants son las personas mencionadas EN los correos
- Sé preciso con los tipos de insight: blocker, decision, followup, risk, task_created
"""

def main():
    print("="*70)
    print("🤖 ONEBOX IA - ANALIZANDO INBOX")
    print("="*70)
    
    inbox = analizar_inbox()
    emails = inbox.get('emails', [])
    print(f"\n📬 {len(emails)} correos sin asignar")
    
    if not emails:
        print("No hay correos para analizar.")
        return
    
    email_summaries = []
    for e in emails:
        email_summaries.append({
            'conversation_id': e['conversationId'],
            'from': e.get('from', ''),
            'fromEmail': e.get('fromEmail', ''),
            'subject': e.get('subject', ''),
            'body': e.get('body', '')[:500],
            'date': e.get('date', '')
        })
    
    emails_text = json.dumps(email_summaries, ensure_ascii=False, indent=2)
    
    print("\n🧠 Analizando correos con IA...")
    response = call_llm(
        system_prompt=SYSTEM_PROMPT,
        user_message=f"Analiza estos {len(emails)} correos y organízalos:\n\n{emails_text}",
        temperature=0.2,
        max_tokens=4096
    )
    
    plan = extract_json_from_response(response)
    
    if not plan or 'analysis' not in plan:
        print("❌ No se pudo parsear la respuesta del LLM")
        print(response[:500])
        return
    
    analysis = plan['analysis']
    
    ignored = 0
    projects_created = 0
    emails_assigned = 0
    insights_created = 0
    tasks_created = 0
    
    for item in analysis:
        action = item.get('action', '')
        
        if action == 'ignore':
            ignored += 1
            print(f"  ⏭️  Ignorado: {item.get('reason', '')}")
            continue
        
        if action == 'create_project':
            project_name = item['project_name']
            result = crear_proyecto(
                name=project_name,
                description=item.get('project_description', ''),
                type=item.get('project_type', 'Otro'),
                participants=item.get('participants', []),
                channels=['Gmail']
            )
            
            if result.get('success'):
                project_id = result['projectId']
                projects_created += 1
                print(f"\n  📁 Proyecto creado: {project_name} ({project_id})")
                
                for conv_id in item.get('emails_to_assign', []):
                    assign_result = asignar_correo_a_proyecto(
                        conversation_id=conv_id,
                        project_id=project_id,
                        project_name=project_name
                    )
                    if assign_result.get('success'):
                        emails_assigned += 1
                        print(f"    📧 Correo asignado: {conv_id[:50]}...")
                    else:
                        print(f"    ❌ Error asignando: {assign_result.get('error', '')}")
                
                for insight in item.get('insights', []):
                    insight_result = crear_insight(
                        project_id=project_id,
                        project_name=project_name,
                        type=insight['type'],
                        title=insight['title'],
                        description=insight.get('description', ''),
                        related_person=insight.get('related_person', ''),
                        actions=insight.get('actions', [])
                    )
                    if insight_result.get('success'):
                        insights_created += 1
                        print(f"    💡 Insight: [{insight['type']}] {insight['title']}")
                
                for task in item.get('tasks', []):
                    task_result = crear_tarea(
                        project_id=project_id,
                        text=task['text'],
                        assigned_to=task.get('assigned_to', ''),
                        status=task.get('status', 'pending')
                    )
                    if task_result.get('success'):
                        tasks_created += 1
                        print(f"    ✅ Tarea: {task['text']}")
            else:
                print(f"  ❌ Error creando proyecto: {result.get('error', '')}")
    
    print("\n" + "="*70)
    print("📊 RESUMEN")
    print("="*70)
    print(f"  📬 Correos analizados: {len(emails)}")
    print(f"  ⏭️  Ignorados (newsletters/spam): {ignored}")
    print(f"  📁 Proyectos creados: {projects_created}")
    print(f"  📧 Correos asignados: {emails_assigned}")
    print(f"  💡 Insights generados: {insights_created}")
    print(f"  ✅ Tareas creadas: {tasks_created}")


if __name__ == "__main__":
    main()