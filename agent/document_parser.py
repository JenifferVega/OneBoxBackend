"""
Processes attached documents (PDF, DOCX, TXT, images) to extract text
and, optionally, generate project metadata with AI.

Used both from the web (direct upload) and from WhatsApp (Twilio Media).
"""
import os
import re
import io
import json
import base64
import uuid
import mimetypes
from datetime import datetime
from typing import Tuple

import boto3
import requests

from agent.llm import call_llm, extract_json_from_response


# =============================================================================
# Configuration
# =============================================================================

S3_ATTACHMENTS_BUCKET = os.environ.get(
    "S3_ATTACHMENTS_BUCKET", "onebox-attachments-191027238118"
)
AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")

MAX_FILE_SIZE_BYTES = 10 * 1024 * 1024  # 10 MB
MAX_TEXT_FOR_ANALYSIS = 50_000  # characters sent to the LLM for analysis

ALLOWED_EXTENSIONS = {
    'pdf': 'application/pdf',
    'docx': 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
    'doc': 'application/msword',
    'txt': 'text/plain',
    'md': 'text/markdown',
    'png': 'image/png',
    'jpg': 'image/jpeg',
    'jpeg': 'image/jpeg',
    'webp': 'image/webp',
}

_s3_client = None


def get_s3_client():
    global _s3_client
    if _s3_client is None:
        _s3_client = boto3.client("s3", region_name=AWS_REGION)
    return _s3_client


# =============================================================================
# Validation
# =============================================================================

def detect_extension(file_name: str, content_type: str = "") -> str:
    """Detects the normalized file extension."""
    if file_name and '.' in file_name:
        ext = file_name.rsplit('.', 1)[-1].lower().strip()
        if ext in ALLOWED_EXTENSIONS:
            return ext
    # Fallback by content-type
    ct = (content_type or '').lower()
    for ext, mime in ALLOWED_EXTENSIONS.items():
        if mime == ct:
            return ext
    if 'pdf' in ct:
        return 'pdf'
    if 'wordprocessingml' in ct or 'docx' in ct:
        return 'docx'
    if 'msword' in ct:
        return 'doc'
    if ct.startswith('image/'):
        if 'png' in ct: return 'png'
        if 'jpeg' in ct or 'jpg' in ct: return 'jpg'
        if 'webp' in ct: return 'webp'
    if ct.startswith('text/'):
        return 'txt'
    return ''


def validate_file(file_bytes: bytes, file_name: str, content_type: str = "") -> Tuple[bool, str, str]:
    """Returns (valid, ext, error_msg)."""
    if not file_bytes:
        return False, '', 'The file is empty.'
    if len(file_bytes) > MAX_FILE_SIZE_BYTES:
        return False, '', f'The file exceeds the {MAX_FILE_SIZE_BYTES // (1024*1024)} MB limit.'
    ext = detect_extension(file_name, content_type)
    if not ext:
        return False, '', f'Unsupported format. Allowed: {", ".join(ALLOWED_EXTENSIONS.keys())}.'
    return True, ext, ''


# =============================================================================
# Text extraction
# =============================================================================

def extract_text_pdf(file_bytes: bytes) -> str:
    try:
        from pypdf import PdfReader
        reader = PdfReader(io.BytesIO(file_bytes))
        chunks = []
        for page in reader.pages:
            try:
                chunks.append(page.extract_text() or '')
            except Exception:
                continue
        return '\n\n'.join(chunks).strip()
    except Exception as e:
        print(f"[document_parser] PDF error: {e}")
        return ''


def extract_text_docx(file_bytes: bytes) -> str:
    try:
        from docx import Document
        doc = Document(io.BytesIO(file_bytes))
        parts = []
        # Paragraphs
        for p in doc.paragraphs:
            if p.text and p.text.strip():
                parts.append(p.text.strip())
        # Tables
        for table in doc.tables:
            for row in table.rows:
                row_text = ' | '.join(c.text.strip() for c in row.cells if c.text and c.text.strip())
                if row_text:
                    parts.append(row_text)
        return '\n'.join(parts).strip()
    except Exception as e:
        print(f"[document_parser] DOCX error: {e}")
        return ''


def extract_text_txt(file_bytes: bytes) -> str:
    for encoding in ('utf-8', 'utf-16', 'latin-1', 'cp1252'):
        try:
            return file_bytes.decode(encoding).strip()
        except (UnicodeDecodeError, LookupError):
            continue
    return ''


def extract_text_image(file_bytes: bytes, ext: str) -> str:
    """Uses Gemini Vision to extract text from an image."""
    if not GEMINI_API_KEY:
        return ''
    try:
        b64 = base64.b64encode(file_bytes).decode('utf-8')
        mime = ALLOWED_EXTENSIONS.get(ext, 'image/png')
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent?key={GEMINI_API_KEY}"
        payload = {
            "contents": [{
                "role": "user",
                "parts": [
                    {"text": "Extract all visible text in this image and describe it briefly. "
                             "If it is meeting minutes, a contract, brief or project document, identify the name, "
                             "dates, participants and objectives. Respond in English."},
                    {"inline_data": {"mime_type": mime, "data": b64}}
                ]
            }],
            "generationConfig": {"temperature": 0.1, "maxOutputTokens": 4096}
        }
        resp = requests.post(url, json=payload, timeout=60)
        if resp.status_code != 200:
            print(f"[document_parser] Gemini Vision error {resp.status_code}: {resp.text[:200]}")
            return ''
        data = resp.json()
        candidates = data.get("candidates", [])
        if not candidates:
            return ''
        parts = candidates[0].get("content", {}).get("parts", [])
        return '\n'.join(p.get("text", "") for p in parts if "text" in p).strip()
    except Exception as e:
        print(f"[document_parser] image vision error: {e}")
        return ''


def extract_text(file_bytes: bytes, ext: str) -> str:
    """Dispatches to the extractor based on the extension."""
    if ext == 'pdf':
        return extract_text_pdf(file_bytes)
    if ext in ('docx', 'doc'):
        return extract_text_docx(file_bytes)
    if ext in ('txt', 'md'):
        return extract_text_txt(file_bytes)
    if ext in ('png', 'jpg', 'jpeg', 'webp'):
        return extract_text_image(file_bytes, ext)
    return ''


# =============================================================================
# AI analysis: extract project metadata from the text
# =============================================================================

PROJECT_TYPES = ['Web Development', 'Infrastructure', 'Design', 'Marketing',
                 'Ecommerce', 'Consulting', 'Support', 'HR', 'Other']


def analyze_document_for_project(text: str, fallback_name: str = "") -> dict:
    """
    Asks the AI to infer project metadata from the document text.
    Returns {name, type, description, extractedNotes}.
    """
    if not text or len(text.strip()) < 30:
        return {
            "name": fallback_name or "Untitled project",
            "type": "Other",
            "description": text.strip()[:500] if text else "",
            "extractedNotes": ""
        }

    # Cap the text sent to the LLM
    truncated = text[:MAX_TEXT_FOR_ANALYSIS]
    types_str = ', '.join(PROJECT_TYPES)

    prompt = f"""Analyze the following document or conversation and propose metadata to create a project. Your goal is to produce a FACTUAL description of the PROJECT itself, NOT of the conversation between the authors.

DOCUMENT OR CONVERSATION:
{truncated}

CRITICAL RULES FOR "description":
1. The description must be about THE PROJECT (what it is, what it has, what stack, what deliverables), NOT about the conversation between people.
   - ❌ INCORRECT: "Kevin asked Mateo for a project..." / "Santi passed the credentials to Belen..."
   - ✅ CORRECT: "GhostLink, a messaging application..." / "Update of the WordPress site les-sp.org..."
2. DO NOT narrate "X said to Y", "X proposed to Y", "X requested". Describe the project as a technical/executive brief.
3. FORBIDDEN to use generic language ("an initiative to optimize", "a series of improvements", "an important challenge"). That does NOT inform.
4. MANDATORY to mention concrete facts about the project:
   - Name of the product/project (e.g.: "GhostLink", "les-sp.org")
   - Specific features with detail
   - Exact tech stack if present (React, Node.js, MongoDB, etc.)
   - URLs / domains / platforms mentioned
   - Concrete tasks with detail
   - Exact metrics (hours, €, dates)
   - Specific decisions about the product
5. People who only participate in the conversation (chat authors) do NOT go in the description.
   - Exception: if a person is the end client, owner or key role of the project, then include them.
6. 5-8 sentences, dense with concrete information about the PROJECT.

EXAMPLE OF A GOOD description (reference, DO NOT copy):
"GhostLink: messaging application with a cyberpunk theme visually inspired by WhatsApp but oriented to 'secret agents'. Key features: real-time chats, statuses, groups, audio messages, fake video calls and self-destructing messages. UI: chat sidebar, right/left-aligned messages, sent/read checks, message bar with emojis and mandatory dark mode in black with neon green. Login and user profiles as 'agents' with alias, photo and status. Stack: React + Next.js + Tailwind on the frontend; Node.js + Express + Socket.io on the backend; MongoDB as the database. The product is conceived as a demo."

EXAMPLE OF A BAD description (DO NOT DO THIS):
"Kevin asked Mateo for a messaging app project. Mateo proposed GhostLink. The features include chats..." (this narrates the conversation, does not describe the project)

RULES FOR "name":
- Use the product/project name mentioned in the text (e.g.: "GhostLink", "LES España y Portugal").
- DO NOT use the names of the people who talked as the project name.
- Maximum 60 characters.

RULES FOR "type":
- Pick one exact category: {types_str}
- One that reflects the REAL scope (not just the requested one).

RULES FOR "extractedNotes":
- Short characterization of the project (max 200 chars).
- Example: "Mobile app with MERN stack and cyberpunk theme for product demo".

RULES FOR "detected_participants":
- List the people mentioned in the text (chat authors, clients, team members).
- Each with: name (the exact name), role_inferred (role inferred from context, e.g.: "Client", "Developer", "Designer", "PM", "Technical lead").
- If there are no clear people, return an empty list.
- Maximum 8 people.

DO NOT INVENT information not in the text.

RESPOND ONLY WITH JSON, no extra text or code fences:
{{
  "name": "...",
  "type": "...",
  "description": "...",
  "extractedNotes": "...",
  "detected_participants": [
    {{"name": "Kevin", "role_inferred": "Requester / Client"}},
    {{"name": "Mateo", "role_inferred": "Developer"}}
  ]
}}"""

    try:
        response = call_llm(
            system_prompt="You are an analyst who extracts CONCRETE and FACTUAL data from text: proper names, URLs, numbers, concrete tasks, specific decisions. Generic language is forbidden. You ALWAYS return valid JSON in the same language as the source text (default English) with no markdown code fences.",
            user_message=prompt,
            temperature=0.15,
            max_tokens=2048
        )
        analysis = extract_json_from_response(response)
        if not analysis:
            cleaned = response.strip()
            if cleaned.startswith('```'):
                cleaned = re.sub(r'^```(?:json)?\s*', '', cleaned)
                cleaned = re.sub(r'\s*```$', '', cleaned)
            try:
                analysis = json.loads(cleaned)
            except json.JSONDecodeError:
                last_brace = cleaned.rfind('}')
                if last_brace > 0:
                    analysis = json.loads(cleaned[:last_brace + 1])
                else:
                    raise

        # Validations / defaults
        name = str(analysis.get('name', '')).strip()[:80] or fallback_name or "Untitled project"
        ptype = str(analysis.get('type', 'Other')).strip()
        if ptype not in PROJECT_TYPES:
            ptype = 'Other'
        description = str(analysis.get('description', '')).strip()
        if not description:
            description = text.strip()[:500]
        notes = str(analysis.get('extractedNotes', '')).strip()[:300]

        # Process detected_participants
        raw_participants = analysis.get('detected_participants', []) or []
        detected_participants = []
        seen_names = set()
        for p in raw_participants[:8]:
            if not isinstance(p, dict):
                continue
            pname = str(p.get('name', '')).strip()[:80]
            prole = str(p.get('role_inferred', '')).strip()[:80]
            if pname and pname.lower() not in seen_names:
                seen_names.add(pname.lower())
                detected_participants.append({
                    'name': pname,
                    'role_inferred': prole or 'Participant'
                })

        return {
            "name": name,
            "type": ptype,
            "description": description,
            "extractedNotes": notes,
            "detected_participants": detected_participants
        }
    except Exception as e:
        print(f"[document_parser] analyze error: {e}")
        return {
            "name": fallback_name or "Project from document",
            "type": "Other",
            "description": text.strip()[:500],
            "extractedNotes": "",
            "detected_participants": []
        }


# =============================================================================
# S3 storage
# =============================================================================

def upload_to_s3(file_bytes: bytes, project_id: str, file_name: str, content_type: str = "application/octet-stream") -> str:
    """Uploads the file to S3 and returns the S3 key."""
    safe_name = re.sub(r'[^\w\s\-\.]', '_', file_name)[:200]
    key = f"projects/{project_id}/{datetime.utcnow().strftime('%Y%m%d')}/{uuid.uuid4().hex[:8]}_{safe_name}"
    client = get_s3_client()
    client.put_object(
        Bucket=S3_ATTACHMENTS_BUCKET,
        Key=key,
        Body=file_bytes,
        ContentType=content_type or 'application/octet-stream',
        ServerSideEncryption='AES256'
    )
    print(f"[document_parser] S3 upload OK: {key} ({len(file_bytes)} bytes)")
    return key


def generate_download_url(s3_key: str, file_name: str = "", expires_in: int = 600) -> str:
    """Generates a presigned URL to download a file (valid for X seconds).

    Content-Disposition follows RFC 5987 with a double filename so that names
    with accents, ñ or emoji can be sent without breaking S3's ISO-8859-1
    validation (S3 rejects any non-ASCII character in the header value with
    "InvalidArgument: Header value cannot be represented using ISO-8859-1").

      · filename=  → transliterated ASCII version, fallback for old browsers.
      · filename*= → percent-encoded UTF-8, what modern Chrome/Safari/Firefox use.
    """
    import unicodedata
    from urllib.parse import quote

    client = get_s3_client()
    params = {
        'Bucket': S3_ATTACHMENTS_BUCKET,
        'Key': s3_key,
    }
    if file_name:
        # ASCII fallback: NFKD splits accented characters into a base letter
        # plus combining marks; encode('ascii', 'ignore') keeps only the base.
        ascii_fallback = (
            unicodedata.normalize('NFKD', file_name)
            .encode('ascii', 'ignore')
            .decode('ascii')
            .replace('"', '')
        ) or 'download'
        utf8_encoded = quote(file_name, safe='')
        params['ResponseContentDisposition'] = (
            f'attachment; filename="{ascii_fallback}"; '
            f"filename*=UTF-8''{utf8_encoded}"
        )
    return client.generate_presigned_url('get_object', Params=params, ExpiresIn=expires_in)


def delete_from_s3(s3_key: str) -> bool:
    try:
        client = get_s3_client()
        client.delete_object(Bucket=S3_ATTACHMENTS_BUCKET, Key=s3_key)
        return True
    except Exception as e:
        print(f"[document_parser] S3 delete error: {e}")
        return False


# =============================================================================
# Twilio: download a file from a MediaUrl
# =============================================================================

def download_from_twilio(media_url: str, account_sid: str, auth_token: str) -> Tuple[bytes, str, str]:
    """Downloads a file from a Twilio MediaUrl.
    Returns (file_bytes, content_type, filename)."""
    try:
        resp = requests.get(media_url, auth=(account_sid, auth_token), timeout=60, allow_redirects=True)
        if resp.status_code != 200:
            print(f"[document_parser] Twilio download error {resp.status_code}: {resp.text[:200]}")
            return b'', '', ''
        ct = resp.headers.get('Content-Type', 'application/octet-stream').split(';')[0].strip()
        # Infer name
        ext = mimetypes.guess_extension(ct) or ''
        if ext.startswith('.'):
            ext = ext[1:]
        if not ext:
            ext = detect_extension('', ct)
        fname = f"whatsapp_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}.{ext or 'bin'}"
        return resp.content, ct, fname
    except Exception as e:
        print(f"[document_parser] Twilio download exc: {e}")
        return b'', '', ''
