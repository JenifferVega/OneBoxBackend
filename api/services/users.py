"""User profile and preferences (table onebox-users, key userId).

Today: timezone and language. The table is the user's general profile, so
later fields (globalRole from docs/roles_design.md) go here too instead of
getting a table each.

The timezone is read on EVERY agent turn -- the planner's "today", scheduled
sends, recurring sends -- so it is cached briefly per user. A WhatsApp turn has
no browser to ask, which is why this lives in the backend and not in the
frontend's localStorage.

Never breaks a request: if the table is missing or unreachable, reads return
the defaults and only an explicit save reports the problem.
"""
import os
import time
from datetime import datetime, timezone

from botocore.exceptions import ClientError

from api.deps import users_table

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None

# Same default the scheduler has always used, so a user who never chose a
# zone keeps the behaviour they had. SCHEDULER_TZ still overrides it.
DEFAULT_TIMEZONE = os.environ.get("SCHEDULER_TZ", "America/Tegucigalpa")
DEFAULT_LANGUAGE = "en"          # the frontend's own default (src/i18n.ts)
SUPPORTED_LANGUAGES = ("es", "en")

_CACHE_TTL_SECONDS = 300
_cache: dict = {}                # uid -> (expires_at, item)
_CACHE_MAX = 2000


def is_valid_timezone(tz: str) -> bool:
    """A real IANA zone name ("America/Bogota"), not an offset or a guess."""
    if not isinstance(tz, str) or not tz.strip() or ZoneInfo is None:
        return False
    try:
        ZoneInfo(tz.strip())
        return True
    except Exception:
        return False


def _load(uid: str) -> dict:
    """The stored record, or {} -- from cache when fresh."""
    if not uid:
        return {}
    hit = _cache.get(uid)
    if hit and hit[0] > time.monotonic():
        return hit[1]
    try:
        item = users_table.get_item(Key={"userId": uid}).get("Item") or {}
    except Exception as e:
        # Missing table, no permission, network: the defaults are a correct
        # answer for reading. Not cached, so it recovers on its own.
        print(f"[users] could not read settings for {uid[:8]}…: {e}")
        return {}
    if len(_cache) >= _CACHE_MAX:
        _cache.pop(next(iter(_cache)), None)
    _cache[uid] = (time.monotonic() + _CACHE_TTL_SECONDS, item)
    return item


def get_settings(uid: str) -> dict:
    """What the frontend shows. `timezone`/`language` are None when the user
    never saved them, so the frontend can tell "not chosen" from "chosen" and
    fill the timezone in from the browser."""
    item = _load(uid)
    return {
        "timezone": item.get("timezone") or None,
        "language": item.get("language") or None,
        "defaults": {"timezone": DEFAULT_TIMEZONE, "language": DEFAULT_LANGUAGE},
    }


def user_timezone(uid: str) -> str:
    """The zone to compute "today" and schedules in. Always a valid name."""
    tz = (_load(uid).get("timezone") or "").strip()
    return tz if is_valid_timezone(tz) else DEFAULT_TIMEZONE


def update_settings(uid: str, timezone_name=None, language=None) -> dict:
    """Partial update. Raises ValueError on invalid input, RuntimeError when
    the table cannot be written."""
    updates = {}
    if timezone_name is not None:
        if not is_valid_timezone(timezone_name):
            raise ValueError(f"'{timezone_name}' is not a valid IANA timezone "
                             f"(e.g. America/Bogota).")
        updates["timezone"] = timezone_name.strip()
    if language is not None:
        lang = str(language).strip().lower()
        if lang not in SUPPORTED_LANGUAGES:
            raise ValueError(f"language must be one of {', '.join(SUPPORTED_LANGUAGES)}.")
        updates["language"] = lang
    if not updates:
        return get_settings(uid)

    updates["updatedAt"] = datetime.now(timezone.utc).isoformat()
    names = {f"#k{i}": k for i, k in enumerate(updates)}
    values = {f":v{i}": v for i, v in enumerate(updates.values())}
    try:
        users_table.update_item(
            Key={"userId": uid},
            UpdateExpression="SET " + ", ".join(f"#k{i} = :v{i}" for i in range(len(updates))),
            ExpressionAttributeNames=names,
            ExpressionAttributeValues=values,
        )
    except ClientError as e:
        raise RuntimeError(f"could not save settings: "
                           f"{e.response['Error'].get('Code', '')}") from e
    _cache.pop(uid, None)
    return get_settings(uid)


def local_time_label(utc_iso: str, tz: str) -> str:
    """'2026-10-06 15:00 (America/Bogota)' from a UTC ISO timestamp, for the
    narrator: a user should never be told a time in UTC."""
    if not utc_iso or ZoneInfo is None:
        return ""
    try:
        dt = datetime.fromisoformat(str(utc_iso).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(ZoneInfo(tz)).strftime("%Y-%m-%d %H:%M") + f" ({tz})"
    except Exception:
        return ""
