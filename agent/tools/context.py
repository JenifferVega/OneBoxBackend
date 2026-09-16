"""Per-request user context (contextvars). Multi-tenant isolation lives here.

Moved verbatim out of the old single-file agent/tools.py.
"""



# ============================================================================
# PER-REQUEST USER CONTEXT (CRITICAL FOR MULTI-TENANT ISOLATION)
# ----------------------------------------------------------------------------
# The agent must NOT have a global USER_ID. There used to be a hard-coded
# constant that caused ANY chatter to always see the data of the same user
# (a serious cross-tenant data leak).
#
# Now the /chat endpoint validates auth and sets these contextvars per
# request, and every tool reads _current_uid() / _current_email() instead.
#
# If a tool runs with no context set, _current_uid() raises RuntimeError to
# FAIL LOUDLY and avoid leaking data by accident.
# ============================================================================
import contextvars

_CURRENT_UID: contextvars.ContextVar = contextvars.ContextVar('onebox_uid', default=None)
_CURRENT_EMAIL: contextvars.ContextVar = contextvars.ContextVar('onebox_email', default=None)


def set_current_user(uid: str, email: str = "") -> None:
    """Sets the user context for this request. Called by /chat."""
    _CURRENT_UID.set(uid)
    _CURRENT_EMAIL.set((email or "").lower())


def clear_current_user() -> None:
    """Clears the context. Called in the endpoint's finally block."""
    _CURRENT_UID.set(None)
    _CURRENT_EMAIL.set(None)


def _current_uid() -> str:
    """Returns the uid of the asking user. Fails if there is no context."""
    uid = _CURRENT_UID.get()
    if not uid:
        raise RuntimeError(
            "Agent tool invoked without a user context. "
            "This is a security bug: the endpoint must call set_current_user() first."
        )
    return uid


def _current_email() -> str:
    return _CURRENT_EMAIL.get() or ""
