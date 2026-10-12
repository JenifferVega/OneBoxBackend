"""Two-language content (es / en): the storage shape and the translator.

Every text shown to users is one of three kinds:

  T  template   written by code ("Tarea creada: X").  Stored as
                msg = {"key": "insight.task_created", "params": {...}}; the
                interface renders it from locales/*.json. Never translated.
  G  generated  written by the AI (insight summaries, reasons, notes). The
                original string field stays as it is (old readers keep working)
                and its two versions live next to it:
                    i18n = {"<field>": {"lang": "es", "es": "...", "en": "...",
                                        "srcHash": "<hash of the original>"}}
                A version is valid only while srcHash matches the original: an
                edited text is translated again, an unchanged one never twice.
  V  verbatim   from people or documents (task titles, quotes, names). As
                written, never translated.

Translation: one LLM call per batch of short texts that both DETECTS the
language (es or en) and writes the other one. TRANSLATE_LLM=provider:model
(default gemini:gemini-3.5-flash-lite: about $0.70 per million characters;
2.5 Flash-Lite is closed to new API keys).
"""
import hashlib
import json
import os
from typing import Dict, Iterable, List, Literal, Optional

from pydantic import BaseModel, Field

LANGS = ("es", "en")
DEFAULT_LANG = "es"            # records written before i18n existed
TRANSLATE_LLM = os.environ.get("TRANSLATE_LLM", "gemini:gemini-3.5-flash-lite")
BATCH_CHARS = 12000            # characters of source text per call


def other(lang: str) -> str:
    return "en" if lang == "es" else "es"


def norm_lang(lang: str) -> str:
    lang = (lang or "").lower()[:2]
    return lang if lang in LANGS else DEFAULT_LANG


def src_hash(text: str) -> str:
    return hashlib.sha1((text or "").strip().encode("utf-8")).hexdigest()[:12]


# ── building and reading the G shape ────────────────────────────────────────

def g_entry(text: str, lang: str, translation: str = "") -> dict:
    """The i18n entry of one generated field."""
    lang = norm_lang(lang)
    entry = {"lang": lang, lang: text, "srcHash": src_hash(text)}
    if translation:
        entry[other(lang)] = translation
    return entry


def is_complete(entry: Optional[dict], text: str) -> bool:
    """Both versions present and made from THIS original."""
    return bool(entry) and entry.get("srcHash") == src_hash(text) and all(entry.get(lg) for lg in LANGS)


def pick(item: dict, field: str, lang: str) -> dict:
    """{text, lang, pending} of one field for a viewer: the viewer's version
    when it exists, else the original marked pending."""
    text = item.get(field) or ""
    entry = (item.get("i18n") or {}).get(field)
    lang = norm_lang(lang)
    if entry and entry.get("srcHash") == src_hash(text):
        if entry.get(lang):
            return {"text": entry[lang], "lang": lang, "pending": False}
        return {"text": text, "lang": entry.get("lang", DEFAULT_LANG), "pending": True}
    return {"text": text, "lang": DEFAULT_LANG, "pending": lang != DEFAULT_LANG}


def versions(item: dict, fields: Dict[str, str]) -> Dict[str, dict]:
    """What the API sends next to a record: {"es": {out: text}, "en": {...}}
    for fields = {out_name: stored_field}. A language appears only when its
    version is valid for the current original; the interface falls back to
    the plain field otherwise. Switching language is then local, no request."""
    out: Dict[str, dict] = {lg: {} for lg in LANGS}
    stored = item.get("i18n") or {}
    for name, field in fields.items():
        text = item.get(field) or ""
        entry = stored.get(field)
        if not text or not entry or entry.get("srcHash") != src_hash(text if isinstance(text, str) else ""):
            continue
        for lg in LANGS:
            if entry.get(lg):
                out[lg][name] = entry[lg]
    return {lg: v for lg, v in out.items() if v}


# ── the translator ──────────────────────────────────────────────────────────

class _Out(BaseModel):
    id: str = ""
    lang: Literal["es", "en"] = "es"
    translation: str = Field("", description="The text in the OTHER language")


class _Batch(BaseModel):
    items: List[_Out] = Field(default_factory=list)


PROMPT = """You translate short texts of a project-management app between Spanish
and English. For EACH item: detect whether it is written in Spanish ('es') or
English ('en') and write it in the OTHER language.

Rules:
- Keep the meaning, tone and length. No explanations, no additions.
- Do NOT translate: names of people, companies, products and projects; task
  titles inside quotes; text inside quotation marks (literal quotes); emails,
  URLs, ids, dates in YYYY-MM-DD, numbers, amounts and currency.
- Keep line breaks, bullets and markdown as they are.
- If an item mixes languages, use the main one as 'lang'.
- Return one result per item, with the SAME id.

ITEMS (one JSON object per line; line breaks inside a text are written \n):
{items}
"""


def _llm():
    from agent.graph.llm_factory import create_llm
    return create_llm(node="translator", env_value=TRANSLATE_LLM, temperature=0.0, max_tokens=8192,
                      with_fallbacks=False, timeout=120)


def _batches(texts: Dict[str, str]) -> Iterable[Dict[str, str]]:
    cur: Dict[str, str] = {}
    size = 0
    for k, v in texts.items():
        if cur and size + len(v) > BATCH_CHARS:
            yield cur
            cur, size = {}, 0
        cur[k] = v
        size += len(v)
    if cur:
        yield cur


def translate(texts: Dict[str, str], llm=None) -> Dict[str, dict]:
    """{id: text} -> {id: {"lang": "es"|"en", "translation": "..."}}. Ids the
    model skipped or answered empty are left out (the caller retries later)."""
    from agent.planning.nodes import call_structured
    llm = llm or _llm()
    out: Dict[str, dict] = {}
    for batch in _batches({k: v for k, v in texts.items() if (v or "").strip()}):
        lines = "\n".join(json.dumps({"id": k, "text": v}, ensure_ascii=False) for k, v in batch.items())
        res, err = call_structured(llm, _Batch, PROMPT.replace("{items}", lines),
                                   is_empty=lambda o: not o.items)
        if err:
            print(f"[i18n] batch of {len(batch)} failed: {err[:200]}")
            continue
        for r in res.items:
            if r.id in batch and r.translation.strip():
                out[r.id] = {"lang": norm_lang(r.lang), "translation": r.translation.strip()}
    return out
