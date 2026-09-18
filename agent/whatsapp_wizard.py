"""
Conversational wizard for creating projects via WhatsApp.
Keeps per-phone state in the session and walks the user step by step to
collect the information needed before creating the project.
"""
import re
from typing import Optional, Tuple

from agent.project_helpers import (
    lookup_user_by_email, evaluate_description, create_project_full
)


# Wizard states
STEP_IDLE = 'idle'
STEP_AWAITING_EMAIL = 'awaiting_email'
STEP_AWAITING_NAME = 'awaiting_name'
STEP_AWAITING_DESCRIPTION = 'awaiting_description'
STEP_AWAITING_CHANNELS = 'awaiting_channels'
STEP_AWAITING_EMAILS = 'awaiting_emails'
STEP_AWAITING_PHONES = 'awaiting_phones'
STEP_CONFIRMING = 'confirming'

# Intent detection. Keeps Spanish keywords alongside English so both languages
# are recognised (the app defaults to English but Spanish speakers still work).
INTENT_GREETING = [
    'hi', 'hello', 'hey', 'good morning', 'good afternoon', 'good evening',
    'hola', 'holi', 'buenas', 'qué tal', 'ola', 'oye',
]
INTENT_CREATE_PROJECT = [
    'create project', 'new project', 'add project', 'start project',
    'create a project', 'i want to create', 'kick off project', 'begin project',
    'crear proyecto', 'nuevo proyecto', 'agregar proyecto', 'añadir proyecto',
    'crear un proyecto', 'quiero crear', 'iniciar proyecto', 'arrancar proyecto',
]
# Words that cancel the wizard. Does NOT include "no" because it is ambiguous
# (in optional steps the user may say "no" to skip, not to cancel everything).
INTENT_CANCEL = [
    'cancel', 'nevermind', 'never mind', 'stop', 'quit', 'abort', 'exit',
    'cancelar', 'cancela', 'olvida', 'borra', 'dejalo', 'salir', 'salgo', 'abortar',
]
INTENT_LIST_PROJECTS = [
    'see projects', 'my projects', 'list projects', 'what projects do i have',
    'ver proyectos', 'mis proyectos', 'lista', 'listar', 'qué proyectos tengo',
]
INTENT_HELP = ['help', '?', 'what can you do', 'options', 'ayuda', 'qué puedes', 'opciones']

EMAIL_REGEX = re.compile(r'[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}')
PHONE_REGEX = re.compile(r'\+?\d[\d\s\-]{7,}\d')


def detect_intent(message: str) -> str:
    """Detects the message intent."""
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
    """Gets or initializes the wizard state."""
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
    if 'gmail' in msg or 'email' in msg or 'mail' in msg:
        channels.append('Gmail')
    if 'whats' in msg or 'wapp' in msg or 'wpp' in msg:
        channels.append('WhatsApp')
    if 'both' in msg or 'all' in msg or 'ambos' in msg or 'los dos' in msg or 'todo' in msg:
        return ['Gmail', 'WhatsApp']
    return channels


def menu_message() -> str:
    return (
        "👋 Hi! I'm *OneBox*. What would you like to do?\n\n"
        "1️⃣  Create a new project\n"
        "2️⃣  See my projects\n"
        "3️⃣  Help\n\n"
        "Reply with the number or tell me in your own words."
    )


def help_message() -> str:
    return (
        "🤖 *OneBox on WhatsApp*\n\n"
        "I can help you:\n"
        "• Create updated projects (with AI analysis)\n"
        "• Check your projects\n"
        "• Receive alerts about your pending items\n\n"
        "To *create a project*, type: \"create project\"\n"
        "To *cancel* at any time: \"cancel\""
    )


def handle_wizard(
    session: dict,
    phone_number: str,
    message: str,
    auto_link_phone_func=None
) -> Tuple[Optional[str], Optional[dict]]:
    """
    Processes the message based on the wizard state.
    Returns (response_text, new_flow_state), or (None, None) if not for the wizard.

    auto_link_phone_func: callable that receives (phone, userId, email, name)
                          to link the WhatsApp number to the Cognito user.
    """
    flow = get_flow_state(session)
    step = flow.get('step', STEP_IDLE)
    msg = message.strip()
    msg_lower = msg.lower()

    # === Universal cancellation ===
    # Note: "no" does NOT cancel because it may mean "don't add more" in optional steps.
    # In STEP_CONFIRMING we do handle it as an explicit cancel below.
    if step != STEP_IDLE and msg_lower in [
        'cancel', 'stop', 'quit', 'abort', 'exit', 'nevermind', 'never mind',
        'cancelar', 'cancela', 'salir', 'salgo', 'abortar', 'olvida', 'borra',
    ]:
        return ("✋ Cancelled. To start again, message me \"create project\".", reset_flow())

    # === IDLE state: detect intent ===
    if step == STEP_IDLE:
        intent = detect_intent(msg)

        if intent == 'greeting' or msg_lower in ['1', 'menu', 'menú', 'start', 'inicio']:
            return (menu_message(), flow)

        if intent == 'help' or msg_lower == '3':
            return (help_message(), flow)

        if intent == 'create_project' or msg_lower == '1':
            new_flow = reset_flow()
            new_flow['step'] = STEP_AWAITING_EMAIL
            return (
                "📋 *Create a new project*\n\n"
                "To get started I need your email (it must match your OneBox account).\n\n"
                "Type \"cancel\" at any time to abort.",
                new_flow
            )

        if intent == 'list_projects' or msg_lower == '2':
            # Handled by the AI agent (run_agent); return None to pass through
            return (None, None)

        # Unknown intent → return None so the AI agent processes it
        return (None, None)

    # === State: awaiting email ===
    if step == STEP_AWAITING_EMAIL:
        emails = parse_emails(msg)
        if not emails:
            return (
                "❌ I didn't detect a valid email in your message. Please send only the email, e.g.: *jane@company.com*",
                flow
            )
        email = emails[0]
        # Look up in Cognito
        user = lookup_user_by_email(email)
        if not user:
            return (
                f"❌ I couldn't find a OneBox account with the email *{email}*.\n\n"
                "Please sign up first at *oneboxmanager.com* and come back. Or send me another email if you mistyped it.\n\n"
                "(Type \"cancel\" to exit)",
                flow
            )

        flow['cognitoUserId'] = user['userId']
        flow['cognitoEmail'] = user['email']
        flow['cognitoName'] = user.get('name', email.split('@')[0])
        flow['step'] = STEP_AWAITING_NAME

        # Auto-add the user's email as a participant
        flow['data']['emails'] = [user['email']]

        # Link phone number if not linked (asked at the end, in STEP_CONFIRMING)
        return (
            f"✅ Found your account, *{flow['cognitoName']}*.\n\n"
            "*Step 2/4* — What will the project be called?",
            flow
        )

    # === State: awaiting name ===
    if step == STEP_AWAITING_NAME:
        if len(msg) < 3:
            return ("❌ That name is too short. Give me a more descriptive name.", flow)
        if len(msg) > 80:
            return ("❌ That name is too long (80 characters max). Shorten it a bit.", flow)
        flow['data']['name'] = msg
        flow['step'] = STEP_AWAITING_DESCRIPTION
        return (
            f"📝 Name: *{msg}*\n\n"
            "*Step 3/4* — Tell me what the project is about.\n\n"
            "Be detailed: include goals, deadlines, team and possible risks. "
            "The better the description, the better the AI analysis.",
            flow
        )

    # === State: awaiting description ===
    if step == STEP_AWAITING_DESCRIPTION:
        if len(msg) < 30:
            return (
                "❌ That description is too short. Tell me more about goals, deadlines and team.\n\n"
                "Example: \"Online store in 8 weeks with Stripe and Shopify, 4-person team, "
                "we need UX design, development and marketing\"",
                flow
            )

        # Evaluate with AI whether the description is sufficient
        eval_result = evaluate_description(flow['data']['name'], msg)
        if not eval_result.get('sufficient'):
            missing = eval_result.get('missing', 'more detail')
            return (
                f"🤔 The description still needs more detail so the AI can analyze it well.\n\n"
                f"Missing: *{missing}*\n\n"
                "Send me a more complete description (combining what you already wrote with the missing details).",
                flow
            )

        flow['data']['description'] = msg
        flow['step'] = STEP_AWAITING_CHANNELS
        return (
            "✅ Description accepted.\n\n"
            "*Step 4/4* — Which channels will this project use?\n\n"
            "• *Gmail* (emails)\n"
            "• *WhatsApp* (messages)\n"
            "• *Both*\n\n"
            "Type your choice.",
            flow
        )

    # === State: awaiting channels ===
    if step == STEP_AWAITING_CHANNELS:
        channels = parse_channels(msg)
        if not channels:
            return (
                "❌ I didn't get that. Reply with: *Gmail*, *WhatsApp* or *Both*.",
                flow
            )
        flow['data']['channels'] = channels

        if 'Gmail' in channels:
            flow['step'] = STEP_AWAITING_EMAILS
            return (
                f"📧 Gmail channel selected.\n\n"
                f"Your email (*{flow['cognitoEmail']}*) is already included as a participant.\n\n"
                "Would you like to add more team emails? Send them separated by commas or spaces.\n"
                "Type *no* or *none* if you don't want to add any more.",
                flow
            )
        else:
            # WhatsApp only
            flow['step'] = STEP_AWAITING_PHONES
            return (
                "📱 WhatsApp channel selected.\n\n"
                "Send me the team's numbers in international format (e.g.: +34600111222), separated by commas or spaces.\n"
                "Type *no* or *none* if you don't want to add any more.",
                flow
            )

    # === State: awaiting additional emails ===
    if step == STEP_AWAITING_EMAILS:
        if msg_lower in ['no', 'none', 'next', 'skip', 'ninguno', 'siguiente', 'pasar', 'salta']:
            extra_emails = []
        else:
            extra_emails = parse_emails(msg)
            if not extra_emails:
                return (
                    "❌ I didn't detect valid emails. Send them like this: *ana@company.com, marco@company.com*\n"
                    "Or type *no* to skip this step.",
                    flow
                )
        # Combine with the user's email
        flow['data']['emails'] = list(set(flow['data']['emails'] + extra_emails))

        # If WhatsApp is also selected, ask for phone numbers
        if 'WhatsApp' in flow['data']['channels']:
            flow['step'] = STEP_AWAITING_PHONES
            return (
                f"✅ {len(flow['data']['emails'])} email(s) registered.\n\n"
                "Now send me the team's WhatsApp numbers (format +34600111222).\n"
                "Type *no* if you don't want to add any.",
                flow
            )
        # If only Gmail, move on to confirmation
        flow['step'] = STEP_CONFIRMING
        return (build_summary(flow), flow)

    # === State: awaiting additional phone numbers ===
    if step == STEP_AWAITING_PHONES:
        if msg_lower in ['no', 'none', 'next', 'skip', 'ninguno', 'siguiente', 'pasar', 'salta']:
            phones = []
        else:
            phones = parse_phones(msg)
            if not phones:
                return (
                    "❌ I didn't detect valid numbers. Use international format like *+34600111222*.\n"
                    "Or type *no* to skip.",
                    flow
                )
        # Auto-add the user's own phone number
        if phone_number and phone_number not in phones:
            phones.append(phone_number if phone_number.startswith('+') else '+' + phone_number)
        flow['data']['phones'] = list(set(phones))
        flow['step'] = STEP_CONFIRMING
        return (build_summary(flow), flow)

    # === State: confirming ===
    if step == STEP_CONFIRMING:
        if msg_lower in ['yes', 'y', 'ok', 'confirm', 'create', 'go', '1', 'si', 'sí', 'dale', 'crear', 'confirmar']:
            # Create the project
            try:
                participants = []
                for email in flow['data']['emails']:
                    name = email.split('@')[0]
                    if email == flow['cognitoEmail']:
                        name = flow['cognitoName']
                    participants.append({
                        'name': name,
                        'email': email,
                        'phone': '',
                        'role': 'Participant'
                    })
                for phone in flow['data']['phones']:
                    participants.append({
                        'name': phone,
                        'email': '',
                        'phone': phone,
                        'role': 'WhatsApp contact'
                    })

                result = create_project_full(
                    user_id=flow['cognitoUserId'],
                    name=flow['data']['name'],
                    description=flow['data']['description'],
                    project_type='Other',
                    channels=flow['data']['channels'],
                    participants=participants
                )

                # Auto-link the phone number to the Cognito user if the function is available
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
                            linked_msg = "\n🔗 Your number is now linked to the account for future conversations."
                    except Exception as e:
                        print(f"[Wizard] Error linking phone number: {e}")

                ig = result.get('insightsGenerated', {})
                count = ig.get('count', 0) if ig.get('generated') else 0
                analysis = ig.get('analysis') or {}
                tasks_count = len(analysis.get('tasks') or [])
                risks_count = len(analysis.get('risks') or [])
                decisions_count = len(analysis.get('decisions') or [])

                response = (
                    f"✅ *Project created*: {flow['data']['name']}\n\n"
                )
                if count > 0:
                    response += (
                        f"🤖 AI generated {count} insights:\n"
                        f"  • {tasks_count} tasks detected\n"
                        f"  • {risks_count} risks identified\n"
                        f"  • {decisions_count} key decisions\n\n"
                    )
                response += (
                    f"📊 See everything at https://www.oneboxmanager.com"
                    f"{linked_msg}"
                )

                return (response, reset_flow())
            except Exception as e:
                print(f"[Wizard] Error creating project: {e}")
                import traceback; traceback.print_exc()
                return (
                    f"❌ There was an error creating the project: {str(e)[:100]}\n\n"
                    "Please try again later or create the project from the web.",
                    reset_flow()
                )
        elif msg_lower in ['no', 'cancel', 'cancelar', 'cancela']:
            return ("✋ Cancelled. The project was not created.", reset_flow())
        else:
            return (
                f"🤔 I didn't get that. Reply *yes* to create the project or *no* to cancel.\n\n"
                + build_summary(flow),
                flow
            )

    # If we got here, we don't handle this state
    return (None, None)


def build_summary(flow: dict) -> str:
    """Builds the summary message shown before confirmation."""
    data = flow['data']
    lines = ["📝 *Summary before creating:*\n"]
    lines.append(f"📋 *Name:* {data['name']}")
    desc = data['description']
    if len(desc) > 150:
        desc = desc[:150] + "..."
    lines.append(f"📄 *Description:* {desc}")
    lines.append(f"📡 *Channels:* {', '.join(data['channels'])}")
    if data['emails']:
        lines.append(f"📧 *Emails:* {len(data['emails'])} ({', '.join(data['emails'][:3])}{'...' if len(data['emails']) > 3 else ''})")
    if data['phones']:
        lines.append(f"📱 *Phones:* {len(data['phones'])} ({', '.join(data['phones'][:3])}{'...' if len(data['phones']) > 3 else ''})")
    lines.append("\nConfirm? Reply *yes* or *no*.")
    return "\n".join(lines)
