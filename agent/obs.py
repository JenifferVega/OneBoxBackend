"""One place every log line goes through.

    from agent.obs import log, warn, error, integration, bind

    log("resolve_entity", query=q, candidates=len(scored))
    error("trello_push_failed", exc=e, project_id=pid)

Each call writes ONE line of JSON to stdout. That is all this does, and it is
deliberate: stdout is the one destination that works everywhere this code runs
-- a Lambda, a container on ECS or App Runner, and a laptop -- so nothing here
depends on how it is deployed. CloudWatch (or anything else) picks it up.

WHY IT EXISTS
A user reported that the Trello integration did not work for them. It worked
locally. The logs of the running system were hundreds of `print()` lines with
no owner: no user id, no session, no timestamp of their own. There was no way
to ask "show me what happened to THIS person" -- so the only way to find out
was to try to reproduce it. That is the cost of unstructured logs, and it is
paid on the worst day.

Every line carries the uid and session automatically, taken from the
contextvars the request already sets, so no call site has to remember to pass
them. In CloudWatch Logs Insights:

    fields @timestamp, event, level, error
    | filter uid = "7458a478-e071-70ff-d1af-8d513f275621"
    | sort @timestamp asc

and you have that person's session, in order, with what failed.
"""
import contextvars
import json
import os
import sys
import time
import traceback
import uuid
from datetime import datetime, timezone

# Correlates every line produced while handling one HTTP request. Set by the
# middleware; nothing else has to carry it.
_REQUEST_ID: contextvars.ContextVar = contextvars.ContextVar("onebox_request_id", default="")
_SESSION_ID: contextvars.ContextVar = contextvars.ContextVar("onebox_session_id", default="")

# Fields that must never reach a log line, whatever a caller passes. Tokens and
# credentials end up in logs by accident far more often than by design, and a
# log line is far easier to read than a database.
_SECRET_HINTS = ("token", "secret", "password", "apikey", "api_key",
                 "authorization", "credential", "cookie")

LEVEL = os.environ.get("ONEBOX_LOG_LEVEL", "INFO").upper()
_ORDER = {"DEBUG": 10, "INFO": 20, "WARN": 30, "ERROR": 40}
_PRETTY = os.environ.get("ONEBOX_LOG_PRETTY", "").lower() in ("1", "true", "yes")


def new_request_id() -> str:
    return uuid.uuid4().hex[:12]


def bind(request_id: str = "", session_id: str = "") -> None:
    """Attach ids to everything logged from here on in this request."""
    if request_id:
        _REQUEST_ID.set(request_id)
    if session_id:
        _SESSION_ID.set(session_id)


def clear() -> None:
    _REQUEST_ID.set("")
    _SESSION_ID.set("")


def _uid() -> str:
    """The current user, if there is one. Never raises: a logging call must not
    be able to break the request it is describing."""
    try:
        from agent.tools.context import _CURRENT_UID
        return _CURRENT_UID.get() or ""
    except Exception:
        return ""


def _safe(value):
    """Anything that will not blow up json.dumps, and is not enormous."""
    if isinstance(value, (str, int, float, bool)) or value is None:
        if isinstance(value, str) and len(value) > 2000:
            return value[:2000] + f"... [cut, {len(value) - 2000} more chars]"
        return value
    try:
        text = json.dumps(value, default=str, ensure_ascii=False)
    except Exception:
        text = repr(value)
    return text[:2000] + ("... [cut]" if len(text) > 2000 else "")


def _emit(level: str, event: str, fields: dict) -> None:
    if _ORDER.get(level, 20) < _ORDER.get(LEVEL, 20):
        return
    line = {
        "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
        "level": level,
        "event": event,
    }
    uid, rid, sid = _uid(), _REQUEST_ID.get(), _SESSION_ID.get()
    if uid:
        line["uid"] = uid
    if rid:
        line["request_id"] = rid
    if sid:
        line["session"] = sid

    for key, value in (fields or {}).items():
        k = str(key)
        if any(h in k.lower() for h in _SECRET_HINTS):
            line[k] = "[redacted]"
            continue
        line[k] = _safe(value)

    try:
        text = json.dumps(line, ensure_ascii=False,
                          indent=2 if _PRETTY else None, default=str)
    except Exception:
        text = json.dumps({"ts": line["ts"], "level": "ERROR",
                           "event": "log_serialisation_failed",
                           "original_event": event})
    print(text, file=sys.stdout, flush=True)


def debug(event: str, **fields):
    _emit("DEBUG", event, fields)


def log(event: str, **fields):
    _emit("INFO", event, fields)


def warn(event: str, **fields):
    _emit("WARN", event, fields)


def error(event: str, exc: BaseException = None, **fields):
    """An error, with the exception type, message and where it came from.

    `traceback` is the last few frames, not the whole stack: enough to find
    the line, short enough that nobody stops reading the logs.
    """
    if exc is not None:
        fields["error"] = f"{type(exc).__name__}: {exc}"
        tb = traceback.format_exception(type(exc), exc, exc.__traceback__)
        fields["traceback"] = "".join(tb[-4:]).strip()
    _emit("ERROR", event, fields)


class integration:
    """Times one call to an outside service and records how it ended.

        with integration("trello", "create_board", board=name):
            board = trello_service.create_board(uid, name, cols)

    Logs one line on success and one on failure, both carrying the user, the
    service, the operation and how long it took. This is what turns "Trello
    does not work for this user" from a reproduction exercise into a query.
    """

    def __init__(self, service: str, operation: str, **fields):
        self.service = service
        self.operation = operation
        self.fields = fields
        self.started = 0.0

    def __enter__(self):
        self.started = time.monotonic()
        return self

    def __exit__(self, exc_type, exc, tb):
        ms = round((time.monotonic() - self.started) * 1000)
        base = {"service": self.service, "operation": self.operation,
                "duration_ms": ms, **self.fields}
        if exc is None:
            log("integration_ok", **base)
        else:
            # status_code, when the exception carries one (HTTPException and
            # requests both do, under different names).
            status = getattr(exc, "status_code", None) or getattr(
                getattr(exc, "response", None), "status_code", None)
            if status:
                base["status_code"] = status
            error("integration_failed", exc=exc, **base)
        return False        # never swallow: the caller decides what to do
