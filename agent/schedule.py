"""Resolución determinista de tiempo para notificaciones programadas.

Filosofía (acordada con el diseño del agente):
  - El PLANNER (LLM) solo INTERPRETA el lenguaje y emite una referencia de
    tiempo NORMALIZADA en params["programar"] — nunca calcula la fecha.
  - Este módulo (CÓDIGO, no LLM) hace la matemática exacta usando la hora real
    del servidor (datetime.now). Así el LLM no necesita saber qué hora es: la
    sabe el código, que es donde debe estar.

Formas normalizadas que emite el planner (params["programar"]):
  {"tipo": "relativo",    "minutos": 120}                 → "en dos horas"
  {"tipo": "relativo",    "horas": 3}                      → "en tres horas"
  {"tipo": "hora_fija",   "hora": 13, "minuto": 0}         → "a la 1pm" (hoy)
  {"tipo": "hora_fija",   "hora": 9, "dia_offset": 1}      → "mañana a las 9"
  {"tipo": "proximo_dia", "dia": "monday", "hora": 9}      → "el lunes a las 9"
  {"tipo": "recurrente",  "dias": ["monday","friday"]}     → "cada lunes y viernes"

resolver_tiempo() devuelve uno de:
  {"scheduled_at": "2026-07-24T17:00:00Z"}      # envío único
  {"recurring_days": ["monday", "friday"]}       # envío recurrente semanal
  {"error": "...", "sugerencia": "..."}          # instrucción imposible (hora pasada, etc.)
"""
import os
from datetime import datetime, timedelta, timezone

try:
    from zoneinfo import ZoneInfo  # Python 3.9+
except ImportError:  # pragma: no cover
    ZoneInfo = None

# Zona horaria por defecto. Configurable sin tocar código con SCHEDULER_TZ.
DEFAULT_TZ = os.environ.get("SCHEDULER_TZ", "America/Tegucigalpa")

# Buffer mínimo hacia el futuro. Twilio exige >=15 min para scheduling nativo;
# lo aplicamos a todo para evitar envíos "instantáneos" mal programados.
MIN_LEAD_MINUTES = 15

_WEEKDAYS = {
    "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
    "friday": 4, "saturday": 5, "sunday": 6,
}


def _tzinfo(tz: str):
    """Devuelve el ZoneInfo pedido; cae a UTC si no está disponible."""
    if ZoneInfo is None:
        return timezone.utc
    try:
        return ZoneInfo(tz)
    except Exception:
        return timezone.utc


def _to_utc_iso(dt: datetime) -> str:
    """Convierte un datetime con tz a ISO 8601 UTC con sufijo 'Z'."""
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def resolver_tiempo(ref: dict, tz: str = None, now: datetime = None) -> dict:
    """Convierte una referencia de tiempo normalizada a scheduled_at/recurring_days.

    Args:
        ref:  dict normalizado emitido por el planner (ver formas arriba).
        tz:   zona horaria IANA del usuario (default DEFAULT_TZ).
        now:  hora actual (inyectable para tests). Si None, usa datetime.now(tz).

    Returns:
        dict con "scheduled_at" | "recurring_days" | "error"(+"sugerencia").
    """
    if not isinstance(ref, dict):
        return {"error": "referencia de tiempo inválida"}

    tzinfo = _tzinfo(tz or DEFAULT_TZ)
    if now is None:
        ahora = datetime.now(tzinfo)
    else:
        # Normaliza 'now' a la tz de trabajo (acepta naive como UTC).
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        ahora = now.astimezone(tzinfo)

    tipo = (ref.get("tipo") or "").strip().lower()

    # ── Recurrente semanal ──────────────────────────────────────────────────
    if tipo == "recurrente":
        dias = [str(d).strip().lower() for d in (ref.get("dias") or [])]
        dias = [d for d in dias if d in _WEEKDAYS]
        if not dias:
            return {"error": "no indicaste días válidos para la notificación recurrente",
                    "sugerencia": "¿qué días? (lunes, miércoles, viernes...)"}
        return {"recurring_days": dias}

    # ── Relativo: "en N horas / N minutos" ─────────────────────────────────
    if tipo == "relativo":
        try:
            minutos = int(ref.get("minutos", 0)) + int(ref.get("horas", 0)) * 60
        except (TypeError, ValueError):
            return {"error": "tiempo relativo inválido"}
        if minutos <= 0:
            return {"error": "el tiempo relativo debe ser mayor a cero"}
        objetivo = ahora + timedelta(minutes=minutos)

    # ── Hora fija: "a la 1pm" (hoy, o con dia_offset para mañana) ───────────
    elif tipo == "hora_fija":
        try:
            hora = int(ref.get("hora", 0))
            minuto = int(ref.get("minuto", 0))
            offset = int(ref.get("dia_offset", 0))
        except (TypeError, ValueError):
            return {"error": "hora inválida"}
        if not (0 <= hora <= 23) or not (0 <= minuto <= 59):
            return {"error": "hora inválida (usa 0-23 horas, 0-59 minutos)"}
        objetivo = (ahora + timedelta(days=offset)).replace(
            hour=hora, minute=minuto, second=0, microsecond=0)
        if objetivo <= ahora and offset == 0:
            return {"error": f"esa hora ({hora:02d}:{minuto:02d}) ya pasó hoy",
                    "sugerencia": "¿la programo para mañana a esa hora?"}

    # ── Próximo día de la semana: "el lunes a las 9" ────────────────────────
    elif tipo == "proximo_dia":
        dia = (ref.get("dia") or "").strip().lower()
        if dia not in _WEEKDAYS:
            return {"error": "día de la semana inválido"}
        try:
            hora = int(ref.get("hora", 9))
            minuto = int(ref.get("minuto", 0))
        except (TypeError, ValueError):
            return {"error": "hora inválida"}
        delta = (_WEEKDAYS[dia] - ahora.weekday()) % 7
        objetivo = (ahora + timedelta(days=delta)).replace(
            hour=hora, minute=minuto, second=0, microsecond=0)
        if objetivo <= ahora:  # es hoy pero ya pasó → misma día la próxima semana
            objetivo += timedelta(days=7)

    else:
        return {"error": f"tipo de tiempo no reconocido: '{tipo}'"}

    # ── Validaciones finales comunes ────────────────────────────────────────
    if objetivo <= ahora:
        return {"error": "la fecha calculada ya pasó"}
    if objetivo < ahora + timedelta(minutes=MIN_LEAD_MINUTES):
        return {"error": f"debe programarse con al menos {MIN_LEAD_MINUTES} minutos de anticipación",
                "sugerencia": "elige una hora un poco más adelante"}

    return {"scheduled_at": _to_utc_iso(objetivo)}


def aplicar_programacion(params: dict, tz: str = None, now: datetime = None):
    """Si params trae 'programar' (forma normalizada), la resuelve a
    scheduled_at/recurring_days y la elimina de params.

    Returns:
        (params_modificados, error|None). Si hay error, params se devuelve
        SIN modificar y el segundo valor es el dict de error de resolver_tiempo.
    """
    ref = params.get("programar")
    if not isinstance(ref, dict):
        return params, None

    res = resolver_tiempo(ref, tz=tz, now=now)
    if res.get("error"):
        return params, res

    nuevos = {k: v for k, v in params.items() if k != "programar"}
    if "scheduled_at" in res:
        nuevos["scheduled_at"] = res["scheduled_at"]
    if "recurring_days" in res:
        nuevos["recurring_days"] = res["recurring_days"]
    return nuevos, None
