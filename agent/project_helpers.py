"""
Funciones compartidas para crear proyectos con análisis de IA.
Usadas tanto por el endpoint web POST /api/projects como por el flujo de WhatsApp.
"""
import os
import json
import re
import uuid
from datetime import datetime
from typing import Optional

import boto3

from agent.tools import (
    projects_table, insights_table, notifications_table, tasks_table
)
from agent.llm import call_llm, extract_json_from_response


COGNITO_USER_POOL_ID = os.environ.get("COGNITO_USER_POOL_ID", "us-east-1_b76prubhx")
COGNITO_REGION = os.environ.get("AWS_REGION", "us-east-1")

_cognito_client = None


def get_cognito_client():
    global _cognito_client
    if _cognito_client is None:
        _cognito_client = boto3.client("cognito-idp", region_name=COGNITO_REGION)
    return _cognito_client


def lookup_user_by_email(email: str) -> Optional[dict]:
    """
    Busca un usuario en Cognito por su email.
    Retorna {userId, email, name} si existe, None si no.
    """
    if not email or '@' not in email:
        return None
    try:
        client = get_cognito_client()
        # Buscar por email (debe estar verificado o no, pero existir)
        response = client.list_users(
            UserPoolId=COGNITO_USER_POOL_ID,
            Filter=f'email = "{email.strip().lower()}"',
            Limit=1
        )
        users = response.get('Users', [])
        if not users:
            return None

        user = users[0]
        attrs = {a['Name']: a['Value'] for a in user.get('Attributes', [])}
        return {
            'userId': attrs.get('sub', user.get('Username', '')),
            'email': attrs.get('email', email),
            'name': attrs.get('name') or attrs.get('given_name') or attrs.get('email', '').split('@')[0],
            'status': user.get('UserStatus', '')
        }
    except Exception as e:
        print(f"[Cognito] Error buscando usuario por email {email}: {e}")
        return None


def evaluate_description(name: str, description: str) -> dict:
    """
    Usa la IA para evaluar si la descripción del proyecto es suficiente
    para generar insights útiles.
    Retorna {sufficient: bool, missing: str, score: int}
    """
    if not description or len(description.strip()) < 20:
        return {
            "sufficient": False,
            "missing": "una descripción más detallada (objetivos, plazos, equipo)",
            "score": 0
        }

    prompt = f"""Evalúa si esta descripción de proyecto es suficiente para generar un análisis útil con tareas, riesgos y decisiones clave.

PROYECTO: {name}
DESCRIPCIÓN: {description}

Una buena descripción debe mencionar al menos 2 de estos: objetivo, plazos, equipo, alcance/funcionalidades, presupuesto, riesgos, dependencias.

Responde SOLO un JSON con este formato exacto:
{{
  "sufficient": true|false,
  "missing": "qué falta para mejorar (1 frase corta) o vacío si es suficiente",
  "score": 1-10
}}"""

    try:
        response = call_llm(
            system_prompt="Eres un evaluador de descripciones de proyectos. Responde SOLO JSON válido.",
            user_message=prompt,
            temperature=0.1,
            max_tokens=200
        )
        analysis = extract_json_from_response(response)
        if not analysis:
            # Fallback: si la descripción tiene >80 caracteres, asumimos OK
            return {
                "sufficient": len(description) >= 80,
                "missing": "más detalle si quieres mejor análisis" if len(description) < 80 else "",
                "score": min(10, len(description) // 20)
            }
        return {
            "sufficient": bool(analysis.get('sufficient', False)),
            "missing": str(analysis.get('missing', ''))[:200],
            "score": int(analysis.get('score', 0))
        }
    except Exception as e:
        print(f"[evaluate_description] Error: {e}")
        # Fallback simple
        return {
            "sufficient": len(description) >= 80,
            "missing": "" if len(description) >= 80 else "más detalle sobre objetivos y plazos",
            "score": min(10, len(description) // 20)
        }


def generate_insights_for_project(
    user_id: str,
    project_id: str,
    project_name: str,
    project_type: str,
    description: str,
    participants_count: int = 0
) -> dict:
    """
    Llama a la IA para analizar la descripción y crear insights en DynamoDB.
    Retorna {generated: bool, count: int, analysis: dict}
    """
    if not description:
        return {"generated": False, "reason": "no_description"}

    now = datetime.utcnow().isoformat()
    # Fecha de hoy para que el LLM pueda escalonar las tareas a partir de aquí
    today_str = datetime.utcnow().strftime('%Y-%m-%d')

    analysis_prompt = f"""Eres un analista senior de proyectos. Tu trabajo es extraer un resumen FÁCTICO del PROYECTO en sí (qué es, qué tiene, qué stack usa, qué entregables), NO de la conversación entre los autores.

PROYECTO: {project_name}
TIPO DECLARADO: {project_type}
PARTICIPANTES: {participants_count} personas

CONTENIDO A ANALIZAR (conversación, brief, acta, correo, etc.):
{description}

REGLAS CRÍTICAS PARA EL "summary":
1. El summary debe describir EL PROYECTO, no la conversación.
   - ❌ INCORRECTO: "Kevin solicitó a Mateo un proyecto..." / "Santi pasó las credenciales a Belen..."
   - ✅ CORRECTO: "GhostLink, aplicación de mensajería..." / "Actualización del sitio WordPress les-sp.org..."
2. NO narres "X dijo a Y", "X propuso a Y", "X solicitó a Y". Describe el proyecto como un brief técnico/ejecutivo.
3. PROHIBIDO usar lenguaje genérico tipo "iniciativa para optimizar", "una serie de mejoras", "un desafío importante".
4. OBLIGATORIO mencionar HECHOS específicos sobre EL PROYECTO:
   - Nombre del producto/proyecto (ej: "GhostLink", "les-sp.org")
   - Funcionalidades específicas
   - Stack tecnológico exacto si aparece (React, Node.js, MongoDB, etc.)
   - URLs / dominios / plataformas
   - Tareas concretas con detalles
   - Métricas exactas (horas, precios, fechas)
   - Decisiones puntuales sobre el producto
5. Las personas que solo conversan NO van en el summary.
   - Excepción: si una persona es el cliente final, responsable o rol clave del proyecto, sí incluir.
6. 4-7 oraciones, densas en información concreta SOBRE EL PROYECTO.

REGLAS ESTRICTAS PARA "work_done" vs "tasks" (MUY IMPORTANTE):
- "work_done" = SOLO cosas EXPLÍCITAMENTE ya hechas/terminadas en el texto.
- "tasks" = cosas pendientes/propuestas/requisitos que TODAVÍA NO se han hecho.

Indicadores de TRABAJO HECHO (verbos en pasado de acciones completadas):
  - "ya hicimos X", "reorganizamos Y", "implementamos Z", "está completo W"
  - "redirigimos las novedades", "eliminamos el contenido obsoleto"
  - "se realizó", "se completó", "se entregó"

Indicadores que NO son trabajo hecho (van a "tasks", NO a "work_done"):
  - "tendría", "incluiría", "el stack será", "sería como", "se hará con"
  - "necesitamos", "hay que", "vamos a hacer", "queda pendiente"
  - Definiciones de funcionalidades futuras
  - Stack tecnológico decidido pero NO implementado
  - Propuestas en una conversación inicial

EJEMPLO 1 — conversación propositiva (todo es futuro):
Texto: "Kevin: necesito una app. Mateo: tendría chats en tiempo real. El stack sería React+Node."
- work_done: [] (NADA está hecho, solo se propuso)
- tasks: ["Implementar chats en tiempo real", "Configurar stack React + Node"]
- decisions: ["Stack tecnológico: React + Node"]

EJEMPLO 2 — chat con trabajo real ya hecho:
Texto: "Santi: reorganizamos el home y eliminamos contenido obsoleto. ~10 horas."
- work_done: ["Reorganización del home", "Eliminación de contenido obsoleto"]
- tasks: [] (nada pendiente nuevo mencionado)
- metrics: ["~10 horas trabajadas"]

REGLA DE ORO: si el texto entero es una conversación de planificación/propuesta sin trabajo terminado, "work_done" debe estar VACÍO. NO inventes trabajo hecho.

REGLAS PARA EL RESTO DE CAMPOS:
- Distingue TRABAJO REALIZADO (lo ya hecho según el texto) de TAREAS PENDIENTES (lo que falta).
- Distingue RIESGOS generales de BLOQUEOS específicos del cliente (falta contenido, credenciales, dependencias externas).
- Identifica PROBLEMAS TÉCNICOS concretos (plugins, hosting, accesos, integraciones).
- Captura MÉTRICAS exactas si aparecen (horas, €, fechas).
- Caracteriza el PROYECTO REAL (no solo la categoría).
- Caracteriza el CLIENTE si se infiere.
- NO inventes datos que no estén en el texto.

EJEMPLO DE BUEN summary (referencia, no copiar):
"Actualización del sitio WordPress les-sp.org del cliente LES España y Portugal. Santi pasó las credenciales a Belen, quien confirmó el acceso. Tareas pendientes: actualizar la junta directiva, revisar los beneficios de socio (sin cambios desde hace 9 años), añadir publicaciones (las últimas son de 2022), llenar la videoteca y agregar testimonios. Bloqueo: los plugins están desactualizados y el hosting no permite actualizarlos completamente. Trabajo ya hecho: reorganización del home, eliminación de contenido obsoleto, redirección de novedades a LinkedIn y creación de 2 versiones de /comites-les-espana-y-portugal. Estimación: ~10 horas a 14.5€/hora. Decidieron no añadir plugin de seguridad porque hacen backup diario."

EJEMPLO DE MAL summary (NO HAGAS ESTO):
"El proyecto es una iniciativa para optimizar y actualizar el sitio web institucional. Existen una serie de mejoras necesarias y un bloqueo técnico crítico que representa un desafío importante. Cliente institucional con un sitio web desactualizado."

FECHAS DE TAREAS — IMPORTANTE PARA EL GANTT:
Para "tasks", "work_done" y "blockers" devuelve OBJETOS con start_date y due_date estimadas (formato YYYY-MM-DD):
- HOY es {today_str}. Distribuye las tareas secuencialmente desde mañana.
- Si el texto menciona deadlines explícitos ("antes del 15 de junio", "para el viernes"), úsalos.
- Si no hay deadlines, estima por complejidad:
  · Tareas pequeñas (configurar, ajustar, revisar): 2-3 días
  · Tareas medianas (implementar, integrar, diseñar): 5-7 días
  · Tareas grandes (módulo completo, release): 10-15 días
- Para "work_done" pon fechas en el pasado (cuando estimes que se hizo) si hay pista; si no, déjalas en blanco.
- Escalona con 1-2 días de margen entre tareas para que el Gantt se vea limpio.

RESPONDE SOLO JSON, sin texto extra ni bloques de código:
{{
  "summary": "Resumen FÁCTICO con nombres, URLs, números, tareas y decisiones concretas mencionadas en el texto (4-7 oraciones)",
  "project_type_real": "Caracterización concreta en una frase (ej: 'Actualización de WordPress institucional con bloqueo de hosting')",
  "client_profile": "Caracterización del cliente con datos del texto (ej: 'Asociación LES España y Portugal con web institucional desactualizada'). Vacío si no hay datos suficientes.",
  "key_insight": "La observación estratégica más importante en una frase concreta",
  "work_done": [{{"text": "Tarea ya hecha", "start_date": "YYYY-MM-DD o vacío", "due_date": "YYYY-MM-DD o vacío"}}],
  "tasks": [{{"text": "Tarea pendiente concreta", "start_date": "YYYY-MM-DD", "due_date": "YYYY-MM-DD"}}],
  "risks": ["Riesgo general 1", "Riesgo 2"],
  "blockers": [{{"text": "Bloqueo específico con detalle", "start_date": "YYYY-MM-DD", "due_date": "YYYY-MM-DD"}}],
  "decisions": ["Decisión concreta tomada o pendiente", "Otra decisión"],
  "metrics": ["Métrica exacta del texto (ej: '~10 horas estimadas', '14.5€/hora')"],
  "tech_issues": ["Problema técnico específico mencionado"]
}}"""

    try:
        print(f"[insights] Llamando LLM para {project_name}")
        response = call_llm(
            system_prompt="Eres un analista de proyectos que extrae datos CONCRETOS y FÁCTICOS de un texto. Tu prioridad es ser específico: nombres propios, URLs, números, fechas exactas, tareas concretas. Prohibido el lenguaje genérico. Devuelves SIEMPRE JSON válido en español sin bloques de código markdown.",
            user_message=analysis_prompt,
            temperature=0.15,
            max_tokens=4096
        )
        print(f"[insights] Respuesta LLM ({len(response)} chars)")

        # Intentar parsear JSON
        analysis = extract_json_from_response(response)
        if not analysis:
            cleaned = response.strip()
            if cleaned.startswith('```'):
                cleaned = re.sub(r'^```(?:json)?\s*', '', cleaned)
                cleaned = re.sub(r'\s*```$', '', cleaned)
            try:
                analysis = json.loads(cleaned)
            except json.JSONDecodeError:
                # Reparar JSON truncado
                last_brace = cleaned.rfind('}')
                if last_brace > 0:
                    truncated = cleaned[:last_brace + 1]
                    open_brackets = truncated.count('[') - truncated.count(']')
                    if open_brackets > 0:
                        last_comma = truncated.rfind(',')
                        if last_comma > 0:
                            truncated = truncated[:last_comma] + (']' * open_brackets) + '}'
                    analysis = json.loads(truncated)
                    print("[insights] JSON reparado tras truncamiento")
                else:
                    raise

        created = 0

        def _save_insight(itype, title, description=''):
            """Helper para crear un insight."""
            nonlocal created
            insights_table.put_item(Item={
                'userId': user_id,
                'insightId': f"{datetime.utcnow().isoformat()}#{uuid.uuid4().hex[:8]}",
                'projectId': project_id,
                'projectName': project_name,
                'type': itype,
                'title': title,
                'description': description or '',
                'status': 'created',
                'createdAt': datetime.utcnow().isoformat(),
            })
            created += 1

        def _save_task(item_or_text, task_status, source_label=''):
            """Helper para crear una tarea operativa real.

            Acepta tanto string (formato viejo) como dict {text, start_date, due_date}
            (formato nuevo con fechas estimadas por la IA). Guardar fechas hace
            que la tarea aparezca en la Vista Gantt del proyecto desde el día 1.
            """
            try:
                # Soportar ambos formatos para no romper si el LLM devuelve viejo
                if isinstance(item_or_text, dict):
                    text = str(item_or_text.get('text', ''))[:500]
                    start_date = (item_or_text.get('start_date') or '').strip()
                    due_date = (item_or_text.get('due_date') or '').strip()
                else:
                    text = str(item_or_text)[:500]
                    start_date = ''
                    due_date = ''
                if not text:
                    return
                tasks_table.put_item(Item={
                    'projectId': project_id,
                    'taskId': uuid.uuid4().hex,
                    'userId': user_id,
                    'text': text,
                    'status': task_status,  # pending, in_progress, completed, blocked
                    'createdBy': 'IA',
                    'assignedTo': '',
                    'startDate': start_date,
                    'dueDate': due_date,
                    'sourceLabel': source_label,
                    'createdAt': datetime.utcnow().isoformat(),
                })
            except Exception as e:
                print(f"[insights] No se pudo crear tarea operativa: {e}")

        # 1. Resumen narrativo (descripción enriquecida)
        if analysis.get('summary'):
            _save_insight('summary', 'Resumen del Proyecto', analysis['summary'])

        # 2. Caracterización real del proyecto
        if analysis.get('project_type_real'):
            _save_insight('project_characterization', analysis['project_type_real'],
                          f'Tipo real del proyecto detectado por IA')

        # 3. Perfil del cliente
        if analysis.get('client_profile'):
            _save_insight('client_profile', analysis['client_profile'],
                          f'Perfil del cliente inferido por IA')

        # 4. Insight clave (callout destacado)
        if analysis.get('key_insight'):
            _save_insight('key_insight', analysis['key_insight'],
                          f'Observación estratégica del proyecto')

        # Helper para extraer el texto de un item que puede ser string o dict
        # (la IA ahora devuelve dicts con start_date/due_date para work_done/tasks/blockers)
        def _text_of(item):
            if isinstance(item, dict):
                return str(item.get('text', ''))
            return str(item)

        # 5. Trabajo ya realizado → también como tareas COMPLETADAS (status='done')
        for item in (analysis.get('work_done') or [])[:10]:
            _save_insight('work_done', _text_of(item), f'Trabajo realizado en {project_name}')
            _save_task(item, 'done', 'work_done')

        # 6. Tareas pendientes → también como tareas PENDIENTES
        for task in (analysis.get('tasks') or [])[:8]:
            _save_insight('task_created', _text_of(task), f'Tarea detectada automáticamente en {project_name}')
            _save_task(task, 'pending', 'task_created')

        # 7. Riesgos generales
        for risk in (analysis.get('risks') or [])[:5]:
            _save_insight('risk', str(risk), f'Riesgo identificado en {project_name}')

        # 8. Bloqueos específicos del cliente → también como tareas BLOQUEADAS
        for blocker in (analysis.get('blockers') or [])[:5]:
            _save_insight('blocker', _text_of(blocker), f'Bloqueo o dependencia del cliente en {project_name}')
            _save_task(blocker, 'blocked', 'blocker')

        # 9. Decisiones (tomadas o pendientes)
        for decision in (analysis.get('decisions') or [])[:5]:
            _save_insight('decision', str(decision), f'Decisión clave para {project_name}')

        # 10. Métricas (horas, costes, plazos)
        for metric in (analysis.get('metrics') or [])[:5]:
            _save_insight('metric', str(metric), f'Métrica detectada en {project_name}')

        # 11. Problemas técnicos
        for issue in (analysis.get('tech_issues') or [])[:5]:
            _save_insight('tech_issue', str(issue), f'Problema técnico identificado en {project_name}')

        print(f"[insights] {created} insights creados para {project_name} (con tareas operativas sincronizadas)")
        return {"generated": True, "count": created, "analysis": analysis}

    except Exception as e:
        print(f"[insights] Error: {e}")
        import traceback; traceback.print_exc()
        return {"generated": False, "reason": str(e)}


def create_project_full(
    user_id: str,
    name: str,
    description: str = "",
    project_type: str = "Otro",
    channels: list = None,
    participants: list = None,
    timing: str = "",
    delivery_date: str = ""
) -> dict:
    """
    Crea un proyecto completo con:
    - Registro en DynamoDB
    - Notificación in-app de proyecto creado
    - Análisis con IA → insights (resumen, tareas, riesgos, decisiones)
    - Notificación in-app de "IA analizó tu proyecto"

    Función reusable para flujo web (POST /api/projects) y flujo WhatsApp (tool del agente).
    Retorna {success, projectId, name, insightsGenerated}.
    """
    project_id = "proj-" + uuid.uuid4().hex[:8]
    now = datetime.utcnow().isoformat()
    channels = channels or ['Gmail']
    participants = participants or []

    # 1. Crear proyecto
    item = {
        'projectId': project_id,
        'userId': user_id,
        'name': name,
        'description': description,
        'type': project_type,
        'status': 'active',
        'participants': participants,
        'channels': channels,
        'timing': timing or '',
        'deliveryDate': delivery_date or '',
        'createdAt': now,
        'lastActivity': now,
    }
    projects_table.put_item(Item=item)
    print(f"[create_project_full] Proyecto {project_id} creado: {name}")

    # 2. Notificación de proyecto creado
    try:
        notifications_table.put_item(Item={
            'userId': user_id,
            'notificationId': f"{now}#{uuid.uuid4().hex[:8]}",
            'projectId': project_id,
            'projectName': name,
            'type': 'project_created',
            'title': f'Proyecto creado: {name}',
            'mensaje': f'Tu proyecto "{name}" fue creado con canales: {", ".join(channels) if channels else "ninguno"}',
            'canal': 'system',
            'status': 'unread',
            'createdAt': now,
        })
    except Exception as e:
        print(f"[create_project_full] Error en notificación de proyecto: {e}")

    # 3. Generar insights con IA
    insights_result = generate_insights_for_project(
        user_id=user_id,
        project_id=project_id,
        project_name=name,
        project_type=project_type,
        description=description,
        participants_count=len(participants)
    )

    # 3.5 Si la descripción original es larga (parece chat / texto crudo) y la IA generó
    # un resumen ejecutivo, reemplazamos la descripción del proyecto por SOLO el summary
    # (las demás categorías —Caracterización, Perfil cliente, Insight clave— viven en
    # el sidebar como insights separados, así evitamos redundancia).
    # Mantenemos la original como `originalDescription` por si se necesita auditar.
    final_description = description
    try:
        analysis = (insights_result or {}).get('analysis') or {}
        summary = analysis.get('summary', '').strip() if isinstance(analysis, dict) else ''
        # Heurística para detectar texto crudo / largo:
        # - más de 250 caracteres
        # - O contiene patrones tipo timestamp WhatsApp ("12/11/25, 13:03 -")
        is_long = len(description or '') > 250
        looks_like_chat = bool(re.search(r'\d{1,2}[/-]\d{1,2}[/-]\d{2,4}.{0,12}\d{1,2}:\d{2}.{0,5}-', description or ''))
        if summary and (is_long or looks_like_chat):
            # SOLO el summary puro como descripción. Limpio y sin redundancia.
            try:
                projects_table.update_item(
                    Key={'projectId': project_id},
                    UpdateExpression='SET description = :d, originalDescription = :o',
                    ExpressionAttributeValues={
                        ':d': summary,
                        ':o': description,
                    }
                )
                final_description = summary
                print(f"[create_project_full] Descripción reemplazada por summary IA ({len(summary)} chars). Original guardada en originalDescription.")
            except Exception as e:
                print(f"[create_project_full] Error actualizando descripción: {e}")
    except Exception as e:
        print(f"[create_project_full] Error en post-procesado descripción: {e}")

    # 4. Notificación de insights generados
    if insights_result.get('generated') and insights_result.get('count', 0) > 0:
        try:
            notifications_table.put_item(Item={
                'userId': user_id,
                'notificationId': f"{datetime.utcnow().isoformat()}#{uuid.uuid4().hex[:8]}",
                'projectId': project_id,
                'projectName': name,
                'type': 'insights_generated',
                'title': f'IA analizó tu proyecto: {insights_result["count"]} insights',
                'mensaje': f'Generamos {insights_result["count"]} insights automáticamente para "{name}". Revisa el panel de Inteligencia.',
                'canal': 'system',
                'status': 'unread',
                'createdAt': datetime.utcnow().isoformat(),
            })
        except Exception as e:
            print(f"[create_project_full] Error en notificación de insights: {e}")

    # 5. Notificar por WhatsApp a todos los participantes con teléfono + al dueño si tiene número vinculado
    try:
        import threading
        threading.Thread(
            target=_notify_whatsapp_async,
            args=(user_id, project_id, name, project_type, final_description, participants, insights_result, channels),
            daemon=True
        ).start()
    except Exception as e:
        print(f"[create_project_full] No se pudo iniciar notificación WhatsApp: {e}")

    return {
        "success": True,
        "projectId": project_id,
        "name": name,
        "description": final_description,
        "insightsGenerated": insights_result
    }


def _notify_whatsapp_async(
    owner_user_id: str,
    project_id: str,
    project_name: str,
    project_type: str,
    description: str,
    participants: list,
    insights_result: dict,
    channels: list
):
    """Envía mensajes de WhatsApp en background a participantes y al dueño cuando se crea un proyecto.
    Solo se envía si el canal WhatsApp está incluido en el proyecto, o si el participante
    está marcado como contacto WhatsApp explícitamente (tiene teléfono).
    """
    try:
        from agent.tools import enviar_notificacion
        import boto3 as _boto3_local

        # Determinar destinatarios únicos por número de teléfono
        recipients = []  # list of {nombre, telefono, isOwner}
        seen_phones = set()

        # Participantes con teléfono
        for p in (participants or []):
            phone = (p.get('telefono') or '').strip()
            if not phone or phone in seen_phones:
                continue
            seen_phones.add(phone)
            recipients.append({
                'nombre': p.get('nombre') or 'Equipo',
                'telefono': phone if phone.startswith('+') else '+' + phone,
                'isOwner': False,
            })

        # Buscar el teléfono vinculado del dueño (tabla onebox-user-phones)
        try:
            dynamodb_local = _boto3_local.resource('dynamodb', region_name=os.environ.get('AWS_REGION', 'us-east-1'))
            phones_table = dynamodb_local.Table('onebox-user-phones')
            scan_res = phones_table.scan(
                FilterExpression='userId = :u',
                ExpressionAttributeValues={':u': owner_user_id}
            )
            for item in scan_res.get('Items', []):
                p = item.get('phoneNumber', '')
                if p and p not in seen_phones:
                    seen_phones.add(p)
                    recipients.append({
                        'nombre': item.get('name') or 'Tú',
                        'telefono': p if p.startswith('+') else '+' + p,
                        'isOwner': True,
                    })
        except Exception as e:
            print(f"[_notify_whatsapp] error obteniendo teléfono del dueño: {e}")

        if not recipients:
            print(f"[_notify_whatsapp] {project_id}: sin destinatarios con teléfono, saltando")
            return

        # Componer mensaje
        ig_count = 0
        analysis = {}
        if insights_result and insights_result.get('generated'):
            ig_count = insights_result.get('count', 0)
            analysis = insights_result.get('analysis', {}) or {}

        n_tasks = len(analysis.get('tasks') or [])
        n_risks = len(analysis.get('risks') or [])
        n_decisions = len(analysis.get('decisions') or [])

        # Recortar descripción para el mensaje
        desc_short = (description or '').strip()
        if len(desc_short) > 200:
            desc_short = desc_short[:200] + '...'

        for r in recipients:
            try:
                if r['isOwner']:
                    header = f"✅ *Proyecto creado:* {project_name}"
                    role_line = "Eres el responsable del proyecto."
                else:
                    header = f"📋 *Te incluyeron en un nuevo proyecto:* {project_name}"
                    role_line = f"Hola {r['nombre']}, te añadieron al equipo."

                lines = [header, '', role_line]
                if project_type and project_type != 'Otro':
                    lines.append(f"📁 Tipo: *{project_type}*")
                if desc_short:
                    lines.append('')
                    lines.append(f"_{desc_short}_")

                if ig_count > 0:
                    lines.append('')
                    lines.append(f"🤖 La IA analizó el proyecto y generó *{ig_count} insights*:")
                    if n_tasks: lines.append(f"  • {n_tasks} tareas detectadas")
                    if n_risks: lines.append(f"  • {n_risks} riesgos identificados")
                    if n_decisions: lines.append(f"  • {n_decisions} decisiones clave")

                if channels:
                    lines.append('')
                    lines.append(f"📡 Canales: {', '.join(channels)}")

                lines.append('')
                lines.append("📊 Revísalo en https://www.oneboxmanager.com")
                lines.append('')
                lines.append("_Mensaje automático de OneBox_")

                message = "\n".join(lines)

                send = enviar_notificacion(
                    destinatario=r['telefono'],
                    mensaje=message,
                    canal='whatsapp',
                    project_id=project_id,
                    project_name=project_name
                )
                if send.get('success'):
                    print(f"[_notify_whatsapp] enviado a {r['telefono']} ({'dueño' if r['isOwner'] else r['nombre']})")
                else:
                    print(f"[_notify_whatsapp] fallo enviando a {r['telefono']}: {send.get('error', 'unknown')}")
            except Exception as e:
                print(f"[_notify_whatsapp] excepción enviando a {r['telefono']}: {e}")

        print(f"[_notify_whatsapp] {project_id}: {len(recipients)} destinatario(s) procesado(s)")

    except Exception as e:
        print(f"[_notify_whatsapp_async] Error general: {e}")
        import traceback; traceback.print_exc()
