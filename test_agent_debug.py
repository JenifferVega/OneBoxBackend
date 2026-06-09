"""
Test de self-play del agente OneBox en modo debug.
Corre conversaciones predefinidas y muestra el debug_info de cada turno.

Uso:
    python test_agent_debug.py

Requiere que el backend esté corriendo (python main.py) y ngrok activo.
"""
import json
import time
import urllib.request
import urllib.error

#BASE    = "https://sumaikun.ngrok.app"
BASE = "http://localhost:8000"
UID     = "test-debug-user-001"
EMAIL   = "debug@onebox.test"
HEADERS = {
    "Content-Type": "application/json",
    "x-user-id": UID,
    "x-user-email": EMAIL,
    "ngrok-skip-browser-warning": "true",
}


# ─────────────────────────────────────────────
def chat(message: str, history: list = None, debug: bool = True) -> dict:
    payload = json.dumps({
        "message": message,
        "history": history or [],
        "debug": debug,
    }).encode()
    req = urllib.request.Request(
        f"{BASE}/chat", data=payload, headers=HEADERS, method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=90) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return {"error": e.read().decode(), "http_status": e.code}
    except Exception as e:
        return {"error": str(e)}


def print_turn(turn_num: int, message: str, result: dict):
    print(f"\n{'─'*60}")
    print(f"  Turno {turn_num} → \"{message}\"")
    print(f"{'─'*60}")
    print(f"  RESPONSE : {result.get('response', 'N/A')}")
    print(f"  TOOLS    : {result.get('toolsUsed', [])}")
    di = result.get("debug_info")
    if di:
        print(f"  DEBUG_INFO:\n{json.dumps(di, indent=4, ensure_ascii=False)}")
    else:
        print("  DEBUG_INFO: (no disponible)")
    if result.get("error"):
        print(f"  ⚠️  ERROR: {result['error']}")


def run_scenario(title: str, turns: list):
    print(f"\n{'='*60}")
    print(f"  ESCENARIO: {title}")
    print(f"{'='*60}")
    history = []
    for i, message in enumerate(turns, 1):
        result = chat(message, history, debug=True)
        print_turn(i, message, result)
        # acumular historial para el siguiente turno
        if "response" in result:
            history.append({"role": "user",      "content": message})
            history.append({"role": "assistant",  "content": result["response"]})
        time.sleep(1)  # evitar rate-limit
    return history


# ─────────────────────────────────────────────
#  ESCENARIOS
# ─────────────────────────────────────────────

if __name__ == "__main__":

    # ── 1. Intención sin datos → debe pedir nombre ─────────────────
    run_scenario(
        "Creación de proyecto sin datos (debe pedir nombre)",
        ["quiero crear un proyecto"],
    )

    # ── 2. Todo en un mensaje → debe extraer sin preguntar ─────────
    run_scenario(
        "Proyecto completo en un mensaje (no debe preguntar nada)",
        [
            "crea un proyecto de Marketing llamado Nova. "
            "Se encargará Laura Gómez (coordinadora) y Daniel Rojas (dev). "
            "Fases: investigación de mercado, identidad de marca, lanzamiento digital.",
        ],
    )

    # ── 3. Flujo paso a paso (multi-turn) ──────────────────────────
    run_scenario(
        "Creación paso a paso con cambio de tema en el medio",
        [
            "quiero crear un proyecto",          # turno 1: pide nombre
            "se llama Alpha",                    # turno 2: da nombre
            "es de backend",                     # turno 3: da tipo
            "muéstrame mis correos",             # turno 4: CAMBIO DE TEMA — no debe crear proyecto
            "Alpha es una app de reservas para hoteles, "
            "coordinada por Ana Torres. Fases: "
            "diseño UX, desarrollo API, pruebas QA, despliegue.",  # turno 5: retoma con descripción
        ],
    )

    # ── 4. Tipo inferido desde lenguaje natural ─────────────────────
    run_scenario(
        "Inferencia de tipo desde lenguaje natural",
        [
            "crea un proyecto para una app móvil de delivery llamada QuickBite, "
            "durará 2 meses, participan Carlos (iOS) y Marta (backend).",
        ],
    )

    # ── 5. Tarea sin proyecto especificado ─────────────────────────
    run_scenario(
        "Crear tarea sin especificar proyecto (debe listar proyectos primero)",
        ["crea una tarea urgente: revisar presupuesto Q3"],
    )

    # ── 6. Notificación a equipo (debe obtener contactos primero) ───
    run_scenario(
        "Enviar WhatsApp al equipo sin teléfono explícito",
        ["manda un WhatsApp al equipo del proyecto Alpha que hay reunión mañana a las 10am"],
    )

    print(f"\n{'='*60}")
    print("  FIN DE PRUEBAS")
    print(f"{'='*60}\n")
