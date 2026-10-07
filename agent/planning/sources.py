"""Reading the project's material: snapshot, documents, deltas, people.

No LLM and no AWS in here. The planning graph works on a SNAPSHOT with the
same shape as GET /api/projects/{id}/export, so a test case is just an export
file plus the documents -- and production loads the very same shape from
DynamoDB/S3 (see store.py).
"""
import re
import unicodedata
from datetime import datetime
from typing import Dict, List, Optional, Tuple

# ── text helpers ────────────────────────────────────────────────────────────

def norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode("ascii")
    return re.sub(r"\s+", " ", s.lower()).strip()


# Same line only ([ \t], not \s), and no "clave": "dos puntos clave:\nEl error"
# is ordinary Spanish and was being blanked out.
_SECRET = re.compile(
    r"(?i)\b(password|passwd|contrase(?:n|ñ)a|pwd)([ \t]*[:=][ \t]*)(?!\[REDACTED\])(\S+)")


# A chat message that is ONLY a password or a verification code: people paste
# them alone ("Onebox123***", "G-122966", "461308"). One token, no spaces;
# a password has letters AND digits plus a symbol or mixed case; a code is 6-12
# digits, optionally with a letter prefix. URLs, e-mails, paths and prices are
# never touched, and only a whole chat message is considered.
_CHAT_BODY = re.compile(r"^(.*?(?:\]\s|\s[-\u2013]\s)[^:]{1,60}:\s)(\S+)\s*$")
_CODE = re.compile(r"^(?:[A-Za-z]{1,3}-?)?\d{6,12}$")


def _looks_secret(tok: str) -> bool:
    if "@" in tok or "://" in tok or tok.startswith("www.") or tok.startswith("<"):
        return False
    if _CODE.match(tok):
        return True
    if not (8 <= len(tok) <= 40 and re.search(r"\d", tok) and re.search(r"[A-Za-z]", tok)):
        return False
    if "/" in tok or "€" in tok or "$" in tok:
        return False
    return bool(re.search(r"[^\w\s]|_", tok)) or (bool(re.search(r"[a-z]", tok)) and bool(re.search(r"[A-Z]", tok)))


def redact(text: str) -> Tuple[str, int]:
    """Blank out credentials before the text reaches any model.

    The BION chat carried a mailbox password in plain text, and every analysis
    sent it to the LLM. Returns the redacted text and how many were removed.
    Two forms: "password: x" anywhere, and a chat message that is only a
    password or a code.
    """
    text, n = _SECRET.subn(r"\1\2[REDACTED]", text or "")
    lines = text.split("\n")
    for i, line in enumerate(lines):
        m = _CHAT_BODY.match(line)
        if m and m.group(2) != "[REDACTED]" and _looks_secret(m.group(2)):
            lines[i] = m.group(1) + "[REDACTED]"
            n += 1
    return "\n".join(lines), n


# ── WhatsApp exports ────────────────────────────────────────────────────────
# iOS:     "[9/21/26, 5:34:50 PM] Claudia Ads Google: Habemus cuenta de Meta!"
# Android: "19/10/25, 23:43 - Santi: Perdona, no me acordaba"
#          "4/6/26, 12:15 p. m. - Santi: ..."
_WA_LINE = re.compile(
    r"^\[?(\d{1,2})/(\d{1,2})/(\d{2,4}),?\s(\d{1,2}):(\d{2})(?::(\d{2}))?"
    r"[\s\u202f\u00a0]?([AaPp]\.?\s?[Mm]\.?)?(?:\]\s|\s[-\u2013]\s)(.*)$")


def _day_first(matches) -> bool:
    """d/m or m/d is a property of the whole export (the phone's locale): look
    for a date that is only valid one way. Android Spanish exports are d/m;
    with no evidence, an Android-style line ("19/10/25, 20:22 - ") means d/m
    and an iOS-style one means the US order."""
    for m in matches:
        a, b = int(m.group(1)), int(m.group(2))
        if a > 12 >= b:
            return True
        if b > 12 >= a:
            return False
    return bool(matches) and not matches[0].group(0).startswith("[")


def _wa_ts(m, day_first: bool) -> Optional[datetime]:
    a, b, y, hh, mm, ss, ampm = (m.group(i) for i in range(1, 8))
    a, b, y, hh = int(a), int(b), int(y), int(hh)
    if y < 100:
        y += 2000
    day, month = (a, b) if day_first else (b, a)
    if ampm:
        pm = ampm.lower().startswith("p")
        hh = (hh % 12) + (12 if pm else 0)
    try:
        return datetime(y, month, day, hh, int(mm), int(ss or 0))
    except ValueError:
        return None


def is_message_start(line: str) -> bool:
    return bool(_WA_LINE.match((line or "").strip("\ufeff")))


def whatsapp_messages(text: str) -> List[Tuple[datetime, str]]:
    """[(timestamp, block)] -- a block is the line plus its continuation lines."""
    lines = (text or "").splitlines()
    found = [(i, _WA_LINE.match(line.strip("\ufeff"))) for i, line in enumerate(lines)]
    day_first = _day_first([m for _i, m in found if m])
    out: List[Tuple[datetime, str]] = []
    for (_i, m), line in zip(found, lines):
        ts = _wa_ts(m, day_first) if m else None
        if ts:
            out.append((ts, line))
        elif out:
            out[-1] = (out[-1][0], out[-1][1] + "\n" + line)
    return out


def is_whatsapp(text: str) -> bool:
    return len(whatsapp_messages(text[:5000])) >= 3


def describe_source(name: str, text: str) -> dict:
    msgs = whatsapp_messages(text) if is_whatsapp(text) else []
    return {
        "name": name,
        "kind": "whatsapp" if msgs else "document",
        "chars": len(text or ""),
        "first_ts": msgs[0][0].isoformat() if msgs else "",
        "last_ts": msgs[-1][0].isoformat() if msgs else "",
    }


def whatsapp_delta(new_text: str, previous_texts: List[str]) -> Tuple[str, dict]:
    """The part of a WhatsApp export that was not in any earlier export.

    Exporting a chat always yields the WHOLE history, so the second upload of
    the BION chat contained the first one entire. Analysing it again from the
    top re-created tasks that were already done.
    """
    new_msgs = whatsapp_messages(new_text)
    cutoff = None
    for t in previous_texts:
        msgs = whatsapp_messages(t)
        if msgs and (cutoff is None or msgs[-1][0] > cutoff):
            cutoff = msgs[-1][0]
    if cutoff is None:
        return new_text, {"delta": False, "cutoff": "", "messages": len(new_msgs)}
    kept = [blk for ts, blk in new_msgs if ts > cutoff]
    return "\n".join(kept), {"delta": True, "cutoff": cutoff.isoformat(),
                             "messages": len(kept), "skipped": len(new_msgs) - len(kept)}


# ── snapshot ────────────────────────────────────────────────────────────────
_ALIASES = {
    "name": ("name", "nombre", "fullname", "full_name", "displayname"),
    "role": ("role", "rol", "cargo", "puesto"),
    "email": ("email", "correo", "mail"),
    "phone": ("phone", "telefono", "teléfono", "celular", "movil", "whatsapp"),
}


def normalize_participant(p) -> dict:
    if not isinstance(p, dict):
        return {"name": str(p or "").strip(), "role": "", "email": "", "phone": ""}
    low = {str(k).lower(): v for k, v in p.items()}
    out = {}
    for field, keys in _ALIASES.items():
        out[field] = next((str(low[k]).strip() for k in keys
                           if isinstance(low.get(k), str) and low[k].strip()), "")
    out["email"] = out["email"].lower()
    return out


def load_snapshot(export: dict) -> dict:
    """Normalise an export (GET /api/projects/{id}/export) into graph state."""
    project = dict(export.get("project") or {})
    participants = [normalize_participant(p) for p in (project.get("participants") or [])]
    tasks = []
    for t in export.get("tasks") or []:
        t = dict(t)
        t.setdefault("dependsOn", [])
        if not t.get("dateStatus"):
            # Dates written by the old analysis were never agreed with anyone.
            t["dateStatus"] = ("estimated" if (t.get("createdBy") in ("IA", "OneBox IA")
                                               and (t.get("dueDate") or t.get("startDate")))
                               else ("committed" if t.get("dueDate") else ""))
        tasks.append(t)
    sources = []
    for a in export.get("attachments") or []:
        text = a.get("fullText") or a.get("extractedTextPreview") or ""
        sources.append({**describe_source(a.get("fileName", ""), text),
                        "attachmentId": a.get("attachmentId", ""),
                        "createdAt": a.get("createdAt", ""),
                        "text": text})
    sources.sort(key=lambda s: s["createdAt"])
    return {
        "project": project,
        "participants": participants,
        "tasks": tasks,
        "sources": sources,
        "insights": export.get("insights") or [],
        "planVersion": int(project.get("planVersion") or 0),
    }


# ── people ──────────────────────────────────────────────────────────────────

def match_person(name: str, participants: List[dict], aliases: Optional[Dict[str, str]] = None) -> Optional[dict]:
    """The participant a free-text name refers to, or None.

    'Claudia Ads Google' (WhatsApp display name) is the participant 'Claudia'.
    Matching is by tokens, not by exact string, which is what made every
    assignment in the BION project point at nobody.
    """
    n = norm(name)
    if not n:
        return None
    if aliases and n in aliases:
        n = norm(aliases[n])
    for p in participants:
        if n in (norm(p.get("email")), norm(p.get("name"))):
            return p
    tokens = set(n.split())
    for p in participants:
        pn = norm(p.get("name"))
        if pn and (set(pn.split()) <= tokens or tokens <= set(pn.split())):
            return p
    return None
