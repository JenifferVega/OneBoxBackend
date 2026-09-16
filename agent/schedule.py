"""Deterministic time resolution for scheduled notifications.

Design philosophy (agreed on with the agent design):
  - The PLANNER (LLM) only INTERPRETS the natural language and emits a
    NORMALIZED time reference in params["schedule"] — it never computes the date.
  - This module (CODE, not LLM) does the exact math using the server's real
    clock (datetime.now). That way the LLM does not need to know what time it
    is: the code knows, which is where that knowledge belongs.

Normalized forms emitted by the planner (params["schedule"]):
  {"type": "relative",    "minutes": 120}                 → "in two hours"
  {"type": "relative",    "hours": 3}                      → "in three hours"
  {"type": "fixed_time",   "hour": 13, "minute": 0}         → "at 1pm" (today)
  {"type": "fixed_time",   "hour": 9, "day_offset": 1}      → "tomorrow at 9"
  {"type": "next_day", "day": "monday", "hour": 9}      → "on Monday at 9"
  {"type": "recurring",  "days": ["monday","friday"]}     → "every Monday and Friday"

resolve_time() returns one of:
  {"scheduled_at": "2026-07-24T17:00:00Z"}      # one-shot send
  {"recurring_days": ["monday", "friday"]}       # weekly recurring send
  {"error": "...", "suggestion": "..."}          # impossible instruction (past time, etc.)
"""
import os
from datetime import datetime, timedelta, timezone

try:
    from zoneinfo import ZoneInfo  # Python 3.9+
except ImportError:  # pragma: no cover
    ZoneInfo = None

# Default time zone. Configurable via SCHEDULER_TZ without touching code.
DEFAULT_TZ = os.environ.get("SCHEDULER_TZ", "America/Tegucigalpa")

# Minimum lead time into the future. Twilio requires >=15 min for native
# scheduling; we apply it globally to avoid poorly scheduled "instant" sends.
MIN_LEAD_MINUTES = 15

_WEEKDAYS = {
    "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
    "friday": 4, "saturday": 5, "sunday": 6,
}


def _tzinfo(tz: str):
    """Returns the requested ZoneInfo; falls back to UTC if unavailable."""
    if ZoneInfo is None:
        return timezone.utc
    try:
        return ZoneInfo(tz)
    except Exception:
        return timezone.utc


def _to_utc_iso(dt: datetime) -> str:
    """Converts a tz-aware datetime to ISO 8601 UTC with 'Z' suffix."""
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def resolve_time(ref: dict, tz: str = None, now: datetime = None,
                    min_lead_minutes: int = MIN_LEAD_MINUTES) -> dict:
    """Converts a normalized time reference to scheduled_at/recurring_days.

    Args:
        ref:  normalized dict emitted by the planner (see forms above).
        tz:   user IANA time zone (default DEFAULT_TZ).
        now:  current time (injectable for tests). If None, uses datetime.now(tz).

    Returns:
        dict with "scheduled_at" | "recurring_days" | "error" (+ "suggestion").
    """
    if not isinstance(ref, dict):
        return {"error": "invalid time reference"}

    tzinfo = _tzinfo(tz or DEFAULT_TZ)
    if now is None:
        now = datetime.now(tzinfo)
    else:
        # Normalize 'now' to the working tz (treat naive as UTC).
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        now = now.astimezone(tzinfo)

    type = (ref.get("type") or "").strip().lower()

    # ── Weekly recurring ────────────────────────────────────────────────────
    if type == "recurring":
        days = [str(d).strip().lower() for d in (ref.get("days") or [])]
        days = [d for d in days if d in _WEEKDAYS]
        if not days:
            return {"error": "no valid days provided for the recurring notification",
                    "suggestion": "which days? (Monday, Wednesday, Friday...)"}
        return {"recurring_days": days}

    # ── Relative: "in N hours / N minutes" ─────────────────────────────────
    if type == "relative":
        try:
            minutes = int(ref.get("minutes", 0)) + int(ref.get("hours", 0)) * 60
        except (TypeError, ValueError):
            return {"error": "invalid relative time"}
        if minutes <= 0:
            return {"error": "relative time must be greater than zero"}
        target = now + timedelta(minutes=minutes)

    # ── Fixed time: "at 1pm" (today, or with day_offset for tomorrow) ───────
    elif type == "fixed_time":
        try:
            hour = int(ref.get("hour", 0))
            minute = int(ref.get("minute", 0))
            offset = int(ref.get("day_offset", 0))
        except (TypeError, ValueError):
            return {"error": "invalid time"}
        if not (0 <= hour <= 23) or not (0 <= minute <= 59):
            return {"error": "invalid time (use 0-23 hours, 0-59 minutes)"}
        target = (now + timedelta(days=offset)).replace(
            hour=hour, minute=minute, second=0, microsecond=0)
        if target <= now and offset == 0:
            return {"error": f"that time ({hour:02d}:{minute:02d}) has already passed today",
                    "suggestion": "should I schedule it for tomorrow at that time?"}

    # ── Next weekday: "on Monday at 9" ──────────────────────────────────────
    elif type == "next_day":
        day = (ref.get("day") or "").strip().lower()
        if day not in _WEEKDAYS:
            return {"error": "invalid day of the week"}
        try:
            hour = int(ref.get("hour", 9))
            minute = int(ref.get("minute", 0))
        except (TypeError, ValueError):
            return {"error": "invalid time"}
        delta = (_WEEKDAYS[day] - now.weekday()) % 7
        target = (now + timedelta(days=delta)).replace(
            hour=hour, minute=minute, second=0, microsecond=0)
        if target <= now:  # today but already past → same day next week
            target += timedelta(days=7)

    else:
        return {"error": f"unrecognized time type: '{type}'"}

    # ── Common final validations ────────────────────────────────────────────
    if target <= now:
        return {"error": "the computed date has already passed"}
    if min_lead_minutes and target < now + timedelta(minutes=min_lead_minutes):
        return {"error": f"must be scheduled at least {min_lead_minutes} minutes in advance",
                "suggestion": "pick a slightly later time"}

    return {"scheduled_at": _to_utc_iso(target)}


def apply_schedule(params: dict, tz: str = None, now: datetime = None):
    """If params contains 'schedule' (normalized form), resolves it to
    scheduled_at/recurring_days and removes it from params.

    Returns:
        (modified_params, error|None). If there is an error, params is
        returned UNMODIFIED and the second value is the resolve_time error dict.
    """
    ref = params.get("schedule")
    if not isinstance(ref, dict):
        return params, None

    # The 15-minute minimum is a restriction of Twilio's NATIVE scheduling
    # (whatsapp/sms). Email goes through our own dispatcher and allows short leads.
    channel = (params.get("channel") or "").strip().lower()
    min_lead = 1 if channel == "email" else MIN_LEAD_MINUTES
    res = resolve_time(ref, tz=tz, now=now, min_lead_minutes=min_lead)
    if res.get("error"):
        return params, res

    updated = {k: v for k, v in params.items() if k != "schedule"}
    if "scheduled_at" in res:
        updated["scheduled_at"] = res["scheduled_at"]
    if "recurring_days" in res:
        updated["recurring_days"] = res["recurring_days"]
    return updated, None
