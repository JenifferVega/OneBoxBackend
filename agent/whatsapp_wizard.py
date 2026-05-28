"""
Wizard conversacional de creación de proyectos vía WhatsApp.
Mantiene un estado por número de teléfono en la sesión y guía al usuario
paso a paso para recolectar la información necesaria antes de crear el proyecto.
"""
import re
from datetime import datetime
from typing import Optional, Tuple

from agent.project_helpers import (
    lookup_user_by_email, evaluate_description, create_project_full
)


# Estados del wizard
STEP_IDLE = 'idle'
STEP_AWAITING_EMAIL = 'awaiting_email'
STEP_AWAITING_NAME = 'awaiting_name'
STEP_AWAITING_DESCRIPTION = 'awaiting_description'
STEP_AWAITING_CHANNELS = 'awaiting_channels'
STEP_AWAITING_EMAILS = 'awaiting_emails'
STEP_AWAITING_PHONES = 'awaiting_phones'
STEP_CONFIRMING = 'confirming'

# Detectar intenciones
INTENT_GREETING = ['hola', 'hi', 'hello', 'buenas', 'qué tal', 'ola', 'oye', 'hey']
INTENT_CREATE_PROJECT = ['crear proyecto', 'nuevo proyecto', 'agregar proyecto', 'añadir proyecto',
                         'crear un proyecto', 'quiero crear', 'iniciar proyecto', 'arrancar proyecto']
# Palabras que cancelan el wizard. No incluye "no" porque es ambiguo
# (en pasos opcionales el usuario puede decir "no" para saltar, no para cancelar todo).
INTENT_CANCEL = ['cancelar', 'cancela', 'olvida', 'borra', 'dejalo', 'salir', 'salgo', 'abortar']
INTENT_LIST_PROJECTS = ['ver proyectos', 'mis proyectos', 'lista', 'listar', 'qué proyectos tengo']
INTENT_HELP = ['ayuda', 'help', '?', 'qué puedes', 'opciones']

EMAIL_REGEX = re.compile(r'[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}')
PHONE_REGEX = re.compile(r'\+?\d[\d\s\-]{7,}\d')


def detect_intent(message: str) -> str:
    """Detecta la intención del mensaje."""
    msg = message.strip().lower()
    if any(w in msg for w in INTENT_CANCEL) and len(msg) < 20:
        return 'cancel'
    if any(w in msg for w in INTENT_CREATE_PROJECT):
        return 'create_project'
    if any(w in msg for w in INTENT_LIST_PROJECTS):
        return 'list_projects'
    if any(w in msg for w in INTENT_HELP):
        return 'help'
    if any(msg == w or msg.startswith(w + ' ') or msg.startswith(w + '!') or msg.startswith(w + ',') for w in INTENT_GREETING):
        return 'greeting'
    return 'unknown'


def get_flow_state(session: dict) -> dict:
    """Obtiene o inicializa el estado del wizard."""
    return session.get('creationFlow') or {
        'step': STEP_IDLE,
        'data': {
            'name': '',
            'description': '',
            'channels': [],
            'emails': [],
            'phones': [],
        },
        'cognitoUserId': '',
        'cognitoEmail': '',
        'cognitoName': ''
    }


def reset_flow() -> dict:
    return {
        'step': STEP_IDLE,
        'data': {'name': '', 'description': '', 'channels': [], 'emails': [], 'phones': []},
        'cognitoUserId': '',
        'cognitoEmail': '',
        'cognitoName': ''
    }


def parse_emails(text: str) -> list:
    return list(set(m.lower() for m in EMAIL_REGEX.findall(text)))


def parse_phones(text: str) -> list:
    raw = PHONE_REGEX.findall(text)
    cleaned = []
    for p in raw:
        digits = re.sub(r'\D', '', p)
        if len(digits) >= 9:
            cleaned.append('+' + digits)
    return list(set(cleaned))


def parse_channels(text: str) -> list:
    msg = text.lower()
    channels = []
    if 'gmail' in msg or 'correo' in msg or 'email' in msg or 'mail' in msg:
        channels.append('Gmail')
    if 'whats' in msg or 'wapp' in msg or 'wpp' in msg:
        channels.append('WhatsApp')
    if 'ambos' in msg or 'los dos' in msg or 'todo' in msg:
        return ['Gmail', 'WhatsApp']
    return channels


def menu_message() -> str:
    return (
        "👋 ¡Hola! Soy *OneBox*. ¿Qué quieres hacer?\n\n"
        "1️⃣  Crear un proyecto nuevo\n"
        "2️⃣  Ver mis proyectos\n"
        "3️⃣  Ayuda\n\n"
        "Responde con el número o cuéntame en tus palabras."
    )


def help_message() -> str:
    return (
        "🤖 *OneBox por WhatsApp*\n\n"
        "Puedo ayudarte a:\n"
        "• Crear proyectos nuevos (con análisis IA)\n"
        "• Consultar tus proyectos\n"
        "• Recibir alertas de tus pendientes\n\n"
        "Para *crear un proyecto*, escribe: \"crear proyecto\"\n"
        "Para *cancelar* en cualquier momento: \"cancelar\""
    )


def handle_wizard(
    session: dict,
    phone_number: str,
    message: str,
    auto_link_phone_func=None
) -> Tuple[Optional[str], Optional[dict]]:
    """
    Procesa el mensaje según el estado del wizard.
    Retorna (response_text, new_flow_state) o (None, None) si no es para el wizard.

    auto_link_phone_func: callable que recibe (phone, userId, email, name)
                          para vincular el número WhatsApp al usuario Cognito.
    """
    flow = get_flow_state(session)
    step = flow.get('step', STEP_IDLE)
    msg = message.strip()
    msg_lower = msg.lower()

    # === Cancelación universal ===
    # Nota: "no" NO cancela porque puede significar "no agregar más" en pasos opcionales.
    # En STEP_CONFIRMING sí lo manejamos como cancelación específica abajo.
    if step != STEP_IDLE and msg_lower in ['cancelar', 'cancela', 'salir', 'salgo', 'abortar', 'olvida', 'borra']:
        return ("✋ Cancelado. Si quieres empezar de nuevo, escríbeme \"crear proyecto\".", reset_flow())

    # === Estado IDLE: detectar intención ===
    if step == STEP_IDLE:
        intent = detect_intent(msg)

        if intent == 'greeting' or msg_lower in ['1', 'menu', 'menú', 'inicio']:
            return (menu_message(), flow)

        if intent == 'help' or msg_lower == '3':
            return (help_message(), flow)

        if intent == 'create_project' or msg_lower == '1':
            new_flow = reset_flow()
            new_flow['step'] = STEP_AWAITING_EMAIL
            return (
                "📋 *Crear nuevo proyecto*\n\n"
                "Para empezar necesito tu correo (debe ser el mismo de tu cuenta OneBox).\n\n"
                "Escribe \"cancelar\" en cualquier momento para abortar.",
                new_flow
            )

        if intent == 'list_projects' or msg_lower == '2':
            # Esto lo maneja el agente IA (run_agent), devolvemos None para que pase
            return (None, None)

        # Intención desconocida → devolvemos None para que el agente IA procese
        return (None, None)

    # === Estado: esperando email ===
    if step == STEP_AWAITING_EMAIL:
        emails = parse_emails(msg)
        if not emails:
            return (
                "❌ No detecté un correo válido en tu mensaje. Por favor envíame solo el correo, ej: *juan@empresa.com*",
                flow
            )
        email = emails[0]
        # Buscar en Cognito
        user = lookup_user_by_email(email)
        if not user:
            return (
                f"❌ No encontré una cuenta de OneBox con el correo *{email}*.\n\n"
                "Por favor regístrate primero en *oneboxmanager.com* y vuelve. O envíame otro correo si te equivocaste.\n\n"
                "(Escribe \"cancelar\" para salir)",
                flow
            )

        flow['cognitoUserId'] = user['userId']
        flow['cognitoEmail'] = user['email']
        flow['cognitoName'] = user.get('name', email.split('@')[0])
        flow['step'] = STEP_AWAITING_NAME

        # Auto-agregar el email del usuario como participante
        flow['data']['emails'] = [user['email']]

        # Vincular número si no está (le preguntamos al final, en STEP_CONFIRMING)
        return (
            f"✅ Encontré tu cuenta, *{flow['cognitoName']}*.\n\n"
            "*Paso 2/4* — ¿Cómo se llamará el proyecto?",
            flow
        )

    # === Estado: esperando nombre ===
    if step == STEP_AWAITING_NAME:
        if len(msg) < 3:
            return ("❌ El nombre es muy corto. Dame un nombre más descriptivo.", flow)
        if len(msg) > 80:
            return ("❌ El nombre es muy largo (máx 80 caracteres). Acórtalo un poco.", flow)
        flow['data']['name'] = msg
        flow['step'] = STEP_AWAITING_DESCRIPTION
        return (
            f"📝 Nombre: *{msg}*\n\n"
            "*Paso 3/4* — Cuéntame de qué trata el proyecto.\n\n"
            "Sé detallado: incluye objetivos, plazos, equipo y posibles riesgos. "
            "Cuanto mejor describas, mejor será el análisis con IA.",
            flow
        )

    # === Estado: esperando descripción ===
    if step == STEP_AWAITING_DESCRIPTION:
        if len(msg) < 30:
            return (
                "❌ Esa descripción es muy corta. Cuéntame más sobre objetivos, plazos y equipo.\n\n"
                "Ejemplo: \"Tienda online en 8 semanas con Stripe y Shopify, equipo de 4 personas, "
                "necesitamos diseño UX, desarrollo y marketing\"",
                flow
            )

        # Evaluar con IA si la descripción es suficiente
        eval_result = evaluate_description(flow['data']['name'], msg)
        if not eval_result.get('sufficient'):
            missing = eval_result.get('missing', 'más detalle')
            return (
                f"🤔 La descripción aún necesita más detalle para que la IA pueda analizarla bien.\n\n"
                f"Falta: *{missing}*\n\n"
                "Envíame una descripción más completa (combinando lo que ya escribiste con los detalles que faltan).",
                flow
            )

        flow['data']['description'] = msg
        flow['step'] = STEP_AWAITING_CHANNELS
        return (
            "✅ Descripción aceptada.\n\n"
            "*Paso 4/4* — ¿Qué canales usarás en este proyecto?\n\n"
            "• *Gmail* (correos)\n"
            "• *WhatsApp* (mensajes)\n"
            "• *Ambos*\n\n"
            "Escribe la opción.",
            flow
        )

    # === Estado: esperando canales ===
    if step == STEP_AWAITING_CHANNELS:
        channels = parse_channels(msg)
        if not channels:
            return (
                "❌ No entendí. Responde con: *Gmail*, *WhatsApp* o *Ambos*.",
                flow
            )
        flow['data']['channels'] = channels

        if 'Gmail' in channels:
            flow['step'] = STEP_AWAITING_EMAILS
            return (
                f"📧 Canal Gmail seleccionado.\n\n"
                f"Tu correo (*{flow['cognitoEmail']}*) ya está incluido como participante.\n\n"
                "¿Quieres agregar más correos del equipo? Envíalos separados por coma o espacio.\n"
                "Escribe *no* o *ninguno* si no quieres agregar más.",
                flow
            )
        else:
            # Solo WhatsApp
            flow['step'] = STEP_AWAITING_PHONES
            return (
                "📱 Canal WhatsApp seleccionado.\n\n"
                "Envíame los números del equipo en formato internacional (ej: +34600111222), separados por coma o espacio.\n"
                "Escribe *no* o *ninguno* si no quieres agregar más.",
                flow
            )

    # === Estado: esperando correos adicionales ===
    if step == STEP_AWAITING_EMAILS:
        if msg_lower in ['no', 'ninguno', 'siguiente', 'pasar', 'salta', 'skip']:
            extra_emails = []
        else:
            extra_emails = parse_emails(msg)
            if not extra_emails:
                return (
                    "❌ No detecté correos válidos. Envíalos así: *ana@empresa.com, marco@empresa.com*\n"
                    "O escribe *no* para saltar este paso.",
                    flow
                )
        # Combinar con el del usuario
        flow['data']['emails'] = list(set(flow['data']['emails'] + extra_emails))

        # Si hay WhatsApp también, pedir teléfonos
        if 'WhatsApp' in flow['data']['channels']:
            flow['step'] = STEP_AWAITING_PHONES
            return (
                f"✅ {len(flow['data']['emails'])} correo(s) registrados.\n\n"
                "Ahora envíame los números WhatsApp del equipo (formato +34600111222).\n"
                "Escribe *no* si no quieres agregar.",
                flow
            )
        # Si solo era Gmail, pasar a confirmación
        flow['step'] = STEP_CONFIRMING
        return (build_summary(flow), flow)

    # === Estado: esperando teléfonos adicionales ===
    if step == STEP_AWAITING_PHONES:
        if msg_lower in ['no', 'ninguno', 'siguiente', 'pasar', 'salta', 'skip']:
            phones = []
        else:
            phones = parse_phones(msg)
            if not phones:
                return (
                    "❌ No detecté números válidos. Usa formato internacional como *+34600111222*.\n"
                    "O escribe *no* para saltar.",
                    flow
                )
        # Auto-agregar el número del propio usuario
        if phone_number and phone_number not in phones:
            phones.append(phone_number if phone_number.startswith('+') else '+' + phone_number)
        flow['data']['phones'] = list(set(phones))
        flow['step'] = STEP_CONFIRMING
        return (build_summary(flow), flow)

    # === Estado: confirmando ===
    if step == STEP_CONFIRMING:
        if msg_lower in ['si', 'sí', 'yes', 'ok', 'dale', 'crear', 'confirmar', '1']:
            # Crear el proyecto
            try:
                participants = []
                for email in flow['data']['emails']:
                    nombre = email.split('@')[0]
                    if email == flow['cognitoEmail']:
                        nombre = flow['cognitoName']
                    participants.append({
                        'nombre': nombre,
                        'email': email,
                        'telefono': '',
                        'rol': 'Participante'
                    })
                for phone in flow['data']['phones']:
                    participants.append({
                        'nombre': phone,
                        'email': '',
                        'telefono': phone,
                        'rol': 'Contacto WhatsApp'
                    })

                result = create_project_full(
                    user_id=flow['cognitoUserId'],
                    name=flow['data']['name'],
                    description=flow['data']['description'],
                    project_type='Otro',
                    channels=flow['data']['channels'],
                    participants=participants
                )

                # Auto-vincular el número al usuario Cognito si la función está disponible
                linked_msg = ""
                if auto_link_phone_func:
                    try:
                        linked = auto_link_phone_func(
                            phone_number,
                            flow['cognitoUserId'],
                            flow['cognitoEmail'],
                            flow['cognitoName']
                        )
                        if linked:
                            linked_msg = "\n🔗 Tu número quedó vinculado a la cuenta para futuras conversaciones."
                    except Exception as e:
                        print(f"[Wizard] Error vinculando número: {e}")

                ig = result.get('insightsGenerated', {})
                count = ig.get('count', 0) if ig.get('generated') else 0
                analysis = ig.get('analysis') or {}
                tasks_count = len(analysis.get('tasks') or [])
                risks_count = len(analysis.get('risks') or [])
                decisions_count = len(analysis.get('decisions') or [])

                response = (
                    f"✅ *Proyecto creado*: {flow['data']['name']}\n\n"
                )
                if count > 0:
                    response += (
                        f"🤖 IA generó {count} insights:\n"
                        f"  • {tasks_count} tareas detectadas\n"
                        f"  • {risks_count} riesgos identificados\n"
                        f"  • {decisions_count} decisiones clave\n\n"
                    )
                response += (
                    f"📊 Revisa todo en https://www.oneboxmanager.com"
                    f"{linked_msg}"
                )

                return (response, reset_flow())
            except Exception as e:
                print(f"[Wizard] Error creando proyecto: {e}")
                import traceback; traceback.print_exc()
                return (
                    f"❌ Hubo un error creando el proyecto: {str(e)[:100]}\n\n"
                    "Por favor intenta de nuevo más tarde o crea el proyecto desde la web.",
                    reset_flow()
                )
        elif msg_lower in ['no', 'cancelar', 'cancela']:
            return ("✋ Cancelado. No se creó el proyecto.", reset_flow())
        else:
            return (
                f"🤔 No entendí. Responde *sí* para crear el proyecto o *no* para cancelar.\n\n"
                + build_summary(flow),
                flow
            )

    # Si llegamos aquí, no manejamos el estado
    return (None, None)


def build_summary(flow: dict) -> str:
    """Construye el mensaje de resumen previo a la confirmación."""
    data = flow['data']
    lines = ["📝 *Resumen antes de crear:*\n"]
    lines.append(f"📋 *Nombre:* {data['name']}")
    desc = data['description']
    if len(desc) > 150:
        desc = desc[:150] + "..."
    lines.append(f"📄 *Descripción:* {desc}")
    lines.append(f"📡 *Canales:* {', '.join(data['channels'])}")
    if data['emails']:
        lines.append(f"📧 *Correos:* {len(data['emails'])} ({', '.join(data['emails'][:3])}{'...' if len(data['emails']) > 3 else ''})")
    if data['phones']:
        lines.append(f"📱 *Teléfonos:* {len(data['phones'])} ({', '.join(data['phones'][:3])}{'...' if len(data['phones']) > 3 else ''})")
    lines.append("\n¿Confirmar? Responde *sí* o *no*.")
    return "\n".join(lines)
