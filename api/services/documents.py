"""Lógica interna de documentos y texto: análisis con IA, drafts y creación
de proyectos a partir de documentos/texto pegado."""
import uuid
from datetime import datetime

from fastapi import HTTPException

from agent.tools import notifications_table, projects_table
from api.deps import attachments_table


def save_attachment_record(project_id: str, user_id: str, file_name: str,
                           file_size: int, content_type: str, ext: str,
                           s3_key: str, extracted_text: str = "",
                           source: str = "web",
                           uploaded_by: str = "",
                           uploaded_by_email: str = "") -> dict:
    """Guarda metadata del adjunto en DynamoDB.
    user_id: SIEMPRE el sub del owner del proyecto (para consistencia con
             el resto de items asociados al proyecto).
    uploaded_by / uploaded_by_email: quién subió el archivo (puede ser
             owner o invitado). Para trazabilidad."""
    now = datetime.utcnow().isoformat()
    attachment_id = f"{now}#{uuid.uuid4().hex[:8]}"
    item = {
        'projectId': project_id,
        'attachmentId': attachment_id,
        'userId': user_id,
        'fileName': file_name,
        'fileSize': file_size,
        'contentType': content_type,
        'extension': ext,
        's3Key': s3_key,
        'extractedTextPreview': (extracted_text or '')[:500],
        'extractedTextLength': len(extracted_text or ''),
        'source': source,
        'createdAt': now,
    }
    if uploaded_by:
        item['uploadedBy'] = uploaded_by
    if uploaded_by_email:
        item['uploadedByEmail'] = uploaded_by_email
    attachments_table.put_item(Item=item)
    return item


def analyze_text_preview(uid: str, text: str, source: str) -> dict:
    """Analiza un texto pegado SIN crear proyecto. Delega al agente en modo debug
    (dry-run) para obtener la misma profundidad que el chat: participantes reales,
    tareas con assigned_to y fechas. Devuelve draftId + sugerencia del agente."""
    from agent.document_parser import upload_to_s3
    from agent.graph import run_agent
    from agent.tools import clear_current_user, set_current_user

    text = (text or '').strip()
    if len(text) < 30:
        raise HTTPException(status_code=400, detail="El texto es muy corto. Pega al menos una conversación o un párrafo.")

    # Correr el agente en modo debug (sin escribir en DynamoDB)
    set_current_user(uid)
    try:
        message = f"Crea un proyecto a partir de esta conversación o texto:\n\n{text}"
        agent_result = run_agent(message, history=[], debug_mode=True, session_id=f"preview-{uid}")
    finally:
        clear_current_user()

    # Extraer datos del plan generado por el agente
    debug_info = agent_result.get("debug_info") or {}
    plan = debug_info.get("plan") or []

    project_name, project_type, description = "", "Otro", ""
    detected_participants, tasks = [], []

    for step in plan:
        tool = step.get("tool", "")
        params = step.get("params", {})
        if tool == "crear_proyecto":
            project_name = params.get("name", "")
            project_type = params.get("type", "Otro")
            description = params.get("description", "")
            for p in (params.get("participants") or []):
                if isinstance(p, dict):
                    detected_participants.append({
                        "name":  p.get("nombre", ""),
                        "role":  p.get("rol", ""),
                        "email": p.get("email", ""),
                        "phone": p.get("telefono", ""),
                    })
        elif tool == "crear_tarea":
            task_text = params.get("text", "")
            # Ignorar refs dinámicas (from_step)
            if task_text and isinstance(task_text, str):
                tasks.append({
                    "text":        task_text,
                    "assigned_to": params.get("assigned_to", ""),
                    "start_date":  params.get("start_date", ""),
                    "due_date":    params.get("due_date", ""),
                    "status":      params.get("status", "pending"),
                })

    # Guardar el texto como draft .txt en S3 + DynamoDB para el paso de confirmación
    draft_id = uuid.uuid4().hex
    source = source or 'paste'
    now = datetime.utcnow().isoformat()
    file_name = f"texto-pegado-{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}.txt"
    text_bytes = text.encode('utf-8')
    try:
        s3_key = upload_to_s3(text_bytes, f"_drafts/{uid}", file_name, 'text/plain')
        attachments_table.put_item(Item={
            'projectId': f'_draft#{uid}',
            'attachmentId': draft_id,
            'userId': uid,
            'fileName': file_name,
            'fileSize': len(text_bytes),
            'contentType': 'text/plain',
            'extension': 'txt',
            's3Key': s3_key,
            'extractedTextPreview': text[:5000],
            'extractedTextLength': len(text),
            'source': f'web_draft_{source}',
            'createdAt': now,
        })
    except Exception as e:
        print(f"[analyze_text_preview] Error guardando draft: {e}")

    return {
        "draftId": draft_id,
        "fileName": file_name,
        "fileSize": len(text_bytes),
        "extractedTextLength": len(text),
        "agentResponse": agent_result.get("response", ""),
        "suggestion": {
            "name":                  project_name,
            "type":                  project_type,
            "description":           description,
            "extractedNotes":        "",
            "detected_participants": detected_participants,
            "tasks":                 tasks,
        },
    }


def analyze_document_preview(uid: str, file_bytes: bytes, file_name: str, content_type: str) -> dict:
    """Analiza un documento (extrae texto + sugiere metadata) SIN crear el proyecto.
    El frontend muestra el preview, el usuario revisa/edita y luego confirma.
    Devuelve un draft_id que se usará después en /api/projects/from-document-draft."""
    from agent.document_parser import (
        analyze_document_for_project, extract_text, upload_to_s3, validate_file
    )

    valid, ext, error = validate_file(file_bytes, file_name or '', content_type or '')
    if not valid:
        raise HTTPException(status_code=400, detail=error)

    text = extract_text(file_bytes, ext)
    if not text or len(text.strip()) < 20:
        raise HTTPException(status_code=400, detail="No se pudo extraer texto del documento o es muy breve.")

    # Sugerir metadata con IA
    analysis = analyze_document_for_project(text)

    # Subir el archivo a un "draft" en S3 para confirmación posterior
    draft_id = uuid.uuid4().hex
    s3_key = upload_to_s3(file_bytes, f"_drafts/{uid}", file_name or f'doc.{ext}', content_type or '')

    # Guardar el draft en DynamoDB attachments con projectId especial "_draft"
    attachments_table.put_item(Item={
        'projectId': f'_draft#{uid}',
        'attachmentId': draft_id,
        'userId': uid,
        'fileName': file_name or f'doc.{ext}',
        'fileSize': len(file_bytes),
        'contentType': content_type or '',
        'extension': ext,
        's3Key': s3_key,
        'extractedTextPreview': text[:5000],
        'extractedTextLength': len(text),
        'source': 'web_draft',
        'createdAt': datetime.utcnow().isoformat(),
    })

    return {
        "draftId": draft_id,
        "fileName": file_name or f'doc.{ext}',
        "fileSize": len(file_bytes),
        "extractedTextLength": len(text),
        "suggestion": {
            "name": analysis['name'],
            "type": analysis['type'],
            "description": analysis['description'],
            "extractedNotes": analysis.get('extractedNotes', ''),
            "detected_participants": analysis.get('detected_participants', []),
        }
    }


def create_project_from_draft(uid: str, req) -> dict:
    """Crea el proyecto definitivo a partir de un draft analizado previamente.
    Mueve el archivo del draft a la carpeta del proyecto y registra el adjunto."""
    from agent.document_parser import S3_ATTACHMENTS_BUCKET, get_s3_client
    from agent.project_helpers import create_project_full

    # Recuperar el draft
    draft = attachments_table.get_item(
        Key={'projectId': f'_draft#{uid}', 'attachmentId': req.draftId}
    ).get('Item')
    if not draft:
        raise HTTPException(status_code=404, detail="Borrador no encontrado o expirado")

    # Validaciones mínimas
    name = (req.name or '').strip()
    if not name:
        raise HTTPException(status_code=400, detail="El nombre del proyecto es requerido")
    if not req.channels or len(req.channels) == 0:
        raise HTTPException(status_code=400, detail="Selecciona al menos un canal")

    # Construir participantes
    participants = []
    seen_emails = set()
    seen_phones = set()

    # 1. Participantes detectados por IA (preservan el nombre original: Kevin, Mateo...)
    for p in (req.detectedParticipants or []):
        if not isinstance(p, dict):
            continue
        pname = (p.get('name') or '').strip()[:80]
        pemail = (p.get('email') or '').strip().lower()
        pphone_raw = (p.get('phone') or '').strip()
        prole = (p.get('role') or 'Participante').strip()[:80]
        pphone = ''
        if pphone_raw:
            pphone = pphone_raw if pphone_raw.startswith('+') else '+' + pphone_raw.replace(' ', '').replace('-', '')
        # Solo agregar si tiene nombre y al menos un canal de contacto (o solo nombre como referencia)
        if pname or pemail or pphone:
            participants.append({
                'nombre': pname or (pemail.split('@')[0] if pemail else pphone),
                'email': pemail if '@' in pemail else '',
                'telefono': pphone,
                'rol': prole or 'Participante'
            })
            if pemail and '@' in pemail:
                seen_emails.add(pemail)
            if pphone:
                seen_phones.add(pphone)

    # 2. Emails sueltos agregados manualmente (que NO vengan de detectados)
    for email in (req.emails or []):
        e = (email or '').strip().lower()
        if e and '@' in e and e not in seen_emails:
            participants.append({
                'nombre': e.split('@')[0],
                'email': e,
                'telefono': '',
                'rol': 'Contacto Email'
            })
            seen_emails.add(e)

    # 3. Teléfonos sueltos agregados manualmente
    for phone in (req.phones or []):
        pclean = (phone or '').strip()
        if pclean:
            formatted = pclean if pclean.startswith('+') else '+' + pclean
            if formatted not in seen_phones:
                participants.append({
                    'nombre': formatted,
                    'email': '',
                    'telefono': formatted,
                    'rol': 'Contacto WhatsApp'
                })
                seen_phones.add(formatted)

    # Crear el proyecto con la info revisada por el usuario
    result = create_project_full(
        user_id=uid,
        name=name,
        description=req.description or '',
        project_type=req.type or 'Otro',
        channels=req.channels,
        participants=participants,
        timing=req.timing or '',
        delivery_date=req.deliveryDate or ''
    )
    project_id = result['projectId']

    # Mover el archivo del draft a la carpeta del proyecto definitivo
    try:
        s3 = get_s3_client()
        old_key = draft['s3Key']
        # Nuevo key con la estructura normal de proyecto
        new_key = old_key.replace(f'projects/_drafts/{uid}', f'projects/{project_id}/{datetime.utcnow().strftime("%Y%m%d")}')
        s3.copy_object(
            Bucket=S3_ATTACHMENTS_BUCKET,
            CopySource={'Bucket': S3_ATTACHMENTS_BUCKET, 'Key': old_key},
            Key=new_key,
            ServerSideEncryption='AES256'
        )
        s3.delete_object(Bucket=S3_ATTACHMENTS_BUCKET, Key=old_key)

        # Registrar el adjunto definitivo
        save_attachment_record(
            project_id=project_id,
            user_id=uid,
            file_name=draft.get('fileName', 'documento'),
            file_size=int(draft.get('fileSize', 0)),
            content_type=draft.get('contentType', ''),
            ext=draft.get('extension', ''),
            s3_key=new_key,
            extracted_text=draft.get('extractedTextPreview', ''),
            source='web'
        )

        # Borrar el registro del draft
        attachments_table.delete_item(
            Key={'projectId': f'_draft#{uid}', 'attachmentId': req.draftId}
        )
    except Exception as e:
        print(f"[from_draft] Error moviendo draft: {e}")
        import traceback; traceback.print_exc()
        # No fallar la creación si el move falla

    return result


def create_project_from_document(uid: str, file_bytes: bytes, file_name: str,
                                 content_type: str, name: str, channels: str) -> dict:
    """Crea un proyecto a partir de un documento. La IA infiere nombre, tipo
    y descripción si no se proveen. El documento queda anexado al proyecto."""
    from agent.document_parser import (
        analyze_document_for_project, extract_text, upload_to_s3, validate_file
    )
    from agent.project_helpers import create_project_full

    valid, ext, error = validate_file(file_bytes, file_name or '', content_type or '')
    if not valid:
        raise HTTPException(status_code=400, detail=error)

    # Extraer texto
    print(f"[from_document] Extrayendo texto de {file_name} ({len(file_bytes)} bytes, ext={ext})")
    text = extract_text(file_bytes, ext)
    if not text or len(text.strip()) < 20:
        raise HTTPException(status_code=400, detail="No se pudo extraer texto del documento o es demasiado breve.")
    print(f"[from_document] Texto extraído: {len(text)} caracteres")

    # Analizar con IA si no se dio nombre/tipo
    analysis = analyze_document_for_project(text, fallback_name=name or '')
    project_name = (name or analysis['name']).strip()[:80]
    project_type = analysis['type']
    description = analysis['description']
    if analysis.get('extractedNotes'):
        description += "\n\nNotas: " + analysis['extractedNotes']

    # Parsear canales
    channel_list = []
    if channels:
        channel_list = [c.strip() for c in channels.split(',') if c.strip()]
    if not channel_list:
        channel_list = ['Gmail']

    # Crear proyecto + insights
    result = create_project_full(
        user_id=uid,
        name=project_name,
        description=description,
        project_type=project_type,
        channels=channel_list,
        participants=[]
    )
    project_id = result['projectId']

    # Subir archivo a S3
    s3_key = upload_to_s3(file_bytes, project_id, file_name or f'doc.{ext}', content_type or '')

    # Registrar adjunto en DynamoDB
    att = save_attachment_record(
        project_id=project_id,
        user_id=uid,
        file_name=file_name or f'doc.{ext}',
        file_size=len(file_bytes),
        content_type=content_type or '',
        ext=ext,
        s3_key=s3_key,
        extracted_text=text,
        source='web'
    )

    result['attachment'] = {
        'attachmentId': att['attachmentId'],
        'fileName': att['fileName'],
        'fileSize': att['fileSize'],
    }
    result['analysis'] = analysis
    return result


def create_project_from_text(uid: str, text: str, name: str, channels, source: str) -> dict:
    """Crea un proyecto desde un texto pegado delegando al agente completo.
    Misma profundidad que el chat: participantes reales, tareas con assigned_to y fechas."""
    from agent.graph import run_agent
    from agent.tools import clear_current_user, set_current_user

    text = (text or '').strip()
    if len(text) < 30:
        raise HTTPException(status_code=400, detail="El texto es muy corto. Pega al menos una conversación completa o un párrafo descriptivo.")

    message = f"Crea un proyecto a partir de esta conversación o texto:\n\n{text}"
    if name:
        message = f"Crea un proyecto llamado '{name}' a partir de esta conversación o texto:\n\n{text}"

    set_current_user(uid)
    try:
        agent_result = run_agent(message, history=[], debug_mode=False, session_id=f"fromtext-{uid}")
    finally:
        clear_current_user()

    # Extraer projectId real de los resultados del agente
    project_id = None
    project_name = name or ""
    for step_result in (agent_result.get("results") or {}).values():
        if isinstance(step_result, dict) and step_result.get("projectId"):
            project_id = step_result["projectId"]
            project_name = step_result.get("name", project_name)
            break

    # Guardar el texto como adjunto .txt en el proyecto creado
    if project_id:
        try:
            from agent.document_parser import upload_to_s3
            fname = f"texto-pegado-{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}.txt"
            s3_key = upload_to_s3(text.encode('utf-8'), project_id, fname, 'text/plain')
            save_attachment_record(
                project_id=project_id,
                user_id=uid,
                file_name=fname,
                file_size=len(text.encode('utf-8')),
                content_type='text/plain',
                ext='txt',
                s3_key=s3_key,
                extracted_text=text,
                source=source or 'paste'
            )
        except Exception as e:
            print(f"[from_text] Error guardando adjunto: {e}")

    return {
        "success": True,
        "projectId": project_id,
        "name": project_name,
        "response": agent_result.get("response", ""),
        "tools_used": agent_result.get("tools_used", []),
    }


def analyze_text_for_project(uid: str, project_id: str, text: str, source: str,
                              user_email: str = "") -> dict:
    """Analiza un texto pegado dentro de un proyecto existente.
    Genera nuevos insights (tareas, riesgos, decisiones) sin crear un proyecto nuevo.

    Permite tanto al owner como a los invitados con acceso al proyecto pegar
    texto y generar insights. Antes solo el owner podía, lo cual era inconsistente
    con upload_attachment (que sí permite invitados).
    """
    from agent.document_parser import upload_to_s3
    from agent.project_helpers import generate_insights_for_project
    from api.services.access import has_project_access

    has, _is_owner, existing = has_project_access(uid, user_email, project_id)
    if not existing:
        raise HTTPException(status_code=404, detail="Proyecto no encontrado")
    if not has:
        raise HTTPException(status_code=403, detail="Sin acceso a este proyecto")

    # Los insights y el adjunto se guardan asociados al owner (no al invitado
    # que pegó el texto) para que la trazabilidad funcione con el filtro
    # por projectId en /api/projects.
    owner_uid = existing.get('userId', uid)

    text = (text or '').strip()
    if len(text) < 30:
        raise HTTPException(status_code=400, detail="El texto es muy corto.")

    # Guardar el texto como "adjunto" tipo .txt
    fname = f"texto-pegado-{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}.txt"
    try:
        s3_key = upload_to_s3(text.encode('utf-8'), project_id, fname, 'text/plain')
        save_attachment_record(
            project_id=project_id,
            user_id=owner_uid,
            file_name=fname,
            file_size=len(text.encode('utf-8')),
            content_type='text/plain',
            ext='txt',
            s3_key=s3_key,
            extracted_text=text,
            source=source or 'paste'
        )
    except Exception as e:
        print(f"[analyze_text] Error guardando texto: {e}")

    # Generar insights con la IA — los insights van asociados al owner para que
    # aparezcan en la vista del owner Y de los invitados (el filtro por
    # projectId en /api/projects los muestra a todos los que tienen acceso).
    insights_result = generate_insights_for_project(
        user_id=owner_uid,
        project_id=project_id,
        project_name=existing.get('name', 'Proyecto'),
        project_type=existing.get('type', 'Otro'),
        description=text[:5000],
        participants_count=len(existing.get('participants', []))
    )

    # Notificación in-app (queda en el feed del owner)
    if insights_result.get('generated') and insights_result.get('count', 0) > 0:
        try:
            notifications_table.put_item(Item={
                'userId': owner_uid,
                'notificationId': f"{datetime.utcnow().isoformat()}#{uuid.uuid4().hex[:8]}",
                'projectId': project_id,
                'projectName': existing.get('name', 'Proyecto'),
                'type': 'text_analyzed',
                'title': f'Texto analizado: {insights_result["count"]} insights',
                'mensaje': f'La IA analizó el texto pegado y generó {insights_result["count"]} insights nuevos.',
                'canal': 'system',
                'status': 'unread',
                'createdAt': datetime.utcnow().isoformat(),
            })
        except Exception as e:
            print(f"[analyze_text] notif error: {e}")

    return {
        "success": True,
        "insightsGenerated": insights_result,
        "savedAs": fname
    }
