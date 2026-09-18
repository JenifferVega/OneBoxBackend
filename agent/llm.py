

import os
import json
import requests
from typing import List, Optional

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
ANTHROPIC_MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-20250514")

BEDROCK_REGION = os.environ.get("BEDROCK_REGION", "us-east-1")
MODEL_ID = os.environ.get("MODEL_ID", "us.anthropic.claude-sonnet-4-20250514-v1:0")

LLM_PROVIDER = os.environ.get(
    "LLM_PROVIDER",
    "gemini" if GEMINI_API_KEY else ("anthropic" if ANTHROPIC_API_KEY else "bedrock")
)

_bedrock_client = None

def get_bedrock_client():
    """Gets or creates the Bedrock client."""
    global _bedrock_client
    if _bedrock_client is None:
        import boto3
        _bedrock_client = boto3.client(
            "bedrock-runtime",
            region_name=BEDROCK_REGION
        )
    return _bedrock_client


def _call_gemini(
    system_prompt: str,
    messages: list,
    temperature: float,
    max_tokens: int
) -> str:
    """Calls Gemini via the Google AI API."""
    contents = []
    for msg in messages:
        role = msg.get("role", "user")
        content = msg.get("content", "")
        gemini_role = "model" if role == "assistant" else "user"
        if isinstance(content, str):
            text = content
        elif isinstance(content, list):
            text = " ".join(b.get("text", "") for b in content if "text" in b)
        else:
            text = str(content)
        contents.append({"role": gemini_role, "parts": [{"text": text}]})

    response = requests.post(
        f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent?key={GEMINI_API_KEY}",
        headers={"content-type": "application/json"},
        json={
            "system_instruction": {"parts": [{"text": system_prompt}]},
            "contents": contents,
            "generationConfig": {
                "temperature": temperature,
                "maxOutputTokens": max_tokens
            }
        },
        timeout=120
    )

    if response.status_code != 200:
        raise Exception(f"Gemini API error {response.status_code}: {response.text}")

    data = response.json()
    candidates = data.get("candidates", [])
    if not candidates:
        raise Exception("Gemini returned no candidates")

    parts = candidates[0].get("content", {}).get("parts", [])
    text_parts = [p.get("text", "") for p in parts if "text" in p]
    return "\n".join(text_parts)


def _call_anthropic(
    system_prompt: str,
    messages: list,
    temperature: float,
    max_tokens: int
) -> str:
    """Calls Claude via the Anthropic API directly."""
    api_messages = []
    for msg in messages:
        role = msg.get("role", "user")
        content = msg.get("content", "")
        if isinstance(content, str):
            api_messages.append({"role": role, "content": content})
        elif isinstance(content, list):
            text = " ".join(b.get("text", "") for b in content if "text" in b)
            api_messages.append({"role": role, "content": text})

    response = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={
            "x-api-key": ANTHROPIC_API_KEY,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json"
        },
        json={
            "model": ANTHROPIC_MODEL,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "system": system_prompt,
            "messages": api_messages
        },
        timeout=120
    )

    if response.status_code != 200:
        raise Exception(f"Anthropic API error {response.status_code}: {response.text}")

    data = response.json()
    text_parts = []
    for block in data.get("content", []):
        if block.get("type") == "text":
            text_parts.append(block["text"])
    return "\n".join(text_parts)


def _call_bedrock(
    system_prompt: str,
    messages: list,
    temperature: float,
    max_tokens: int
) -> str:
    """Calls Claude via the Bedrock Converse API."""
    client = get_bedrock_client()

    response = client.converse(
        modelId=MODEL_ID,
        system=[{"text": system_prompt}],
        messages=messages,
        inferenceConfig={
            "maxTokens": max_tokens,
            "temperature": temperature
        }
    )

    output = response.get("output", {}).get("message", {})
    content_blocks = output.get("content", [])

    text_parts = []
    for block in content_blocks:
        if "text" in block:
            text_parts.append(block["text"])
    return "\n".join(text_parts)


def call_llm(
    system_prompt: str,
    user_message: str,
    history: Optional[List[dict]] = None,
    temperature: float = 0.3,
    max_tokens: int = 4096
) -> str:
    """
    Calls Claude via the Anthropic API directly or Bedrock.
    Uses ANTHROPIC_API_KEY if configured, otherwise falls back to Bedrock.
    """
    messages = []

    if history:
        for msg in history:
            role = msg.get("role", "user")
            content = msg.get("content", "")
            if isinstance(content, str):
                messages.append({
                    "role": role,
                    "content": [{"text": content}]
                })

    messages.append({
        "role": "user",
        "content": [{"text": user_message}]
    })

    try:
        if LLM_PROVIDER == "gemini":
            print(f"[LLM] Using Gemini ({GEMINI_MODEL})")
            return _call_gemini(system_prompt, messages, temperature, max_tokens)
        elif LLM_PROVIDER == "anthropic":
            print(f"[LLM] Using Anthropic API directly ({ANTHROPIC_MODEL})")
            return _call_anthropic(system_prompt, messages, temperature, max_tokens)
        else:
            print(f"[LLM] Using Bedrock ({MODEL_ID})")
            return _call_bedrock(system_prompt, messages, temperature, max_tokens)
    except Exception as e:
        print(f"[LLM] Error with {LLM_PROVIDER}: {str(e)}")
        raise


def extract_json_from_response(response: str) -> Optional[dict]:
    """
    Extracts JSON from an LLM response.
    The LLM may return raw JSON or JSON embedded in text.

    Args:
        response: LLM response

    Returns:
        dict, or None if it cannot be parsed
    """
    try:
        return json.loads(response)
    except json.JSONDecodeError:
        pass
    
    import re
    
    json_match = re.search(r'```(?:json)?\s*([\s\S]*?)\s*```', response)
    if json_match:
        try:
            return json.loads(json_match.group(1))
        except json.JSONDecodeError:
            pass
    
    brace_match = re.search(r'\{[\s\S]*\}', response)
    if brace_match:
        try:
            return json.loads(brace_match.group(0))
        except json.JSONDecodeError:
            pass

    # The three `pass` above are a cascade of parsing strategies, not swallowed
    # errors: each one means "try the next". Arriving HERE does mean something
    # went wrong -- the model answered with something no strategy could read --
    # and the caller only sees None. Worth one line.
    try:
        from agent import obs
        obs.warn("llm_json_unparseable", length=len(response or ""),
                 head=(response or "")[:200])
    except Exception:
        pass
    return None