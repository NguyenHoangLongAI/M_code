"""
utils/api_client.py
Google Gemini API wrapper — google-genai SDK.
Includes:
  - Key rotation across multiple GEMINI_API_KEY_n env vars
  - Exponential backoff on 429
  - Partial-JSON recovery for truncated responses
"""
import json
import os
import re
import time

from google import genai
from google.genai import types
from google.genai.errors import APIError

from config import GEMINI_MODEL, MAX_TOKENS

# ── Key rotation ───────────────────────────────────────────────────────────
def _load_keys() -> list[str]:
    """
    Load API keys from environment.
    Checks:
      GEMINI_API_KEY        (single key, legacy)
      GEMINI_API_KEY_1 …    (numbered keys for rotation)
    """
    keys: list[str] = []
    # Numbered keys (rotation pool)
    for i in range(1, 50):
        k = os.environ.get(f"GEMINI_API_KEY_{i}", "").strip()
        if k:
            keys.append(k)
    # Fall back to bare key
    single = os.environ.get("GEMINI_API_KEY", "").strip()
    if single and single not in keys:
        keys.append(single)
    return keys

_API_KEYS: list[str] = _load_keys()
_key_idx: int = 0          # round-robin cursor
_clients: dict[str, genai.Client] = {}   # key → client cache


def _next_client() -> tuple[genai.Client, str]:
    """Return the next (client, masked_key) in round-robin order."""
    global _key_idx
    if not _API_KEYS:
        raise RuntimeError(
            "No Gemini API key found.\n"
            "  export GEMINI_API_KEY=AIza...\n"
            "  or GEMINI_API_KEY_1=... GEMINI_API_KEY_2=... for rotation"
        )
    key = _API_KEYS[_key_idx % len(_API_KEYS)]
    _key_idx += 1
    if key not in _clients:
        _clients[key] = genai.Client(api_key=key)
    masked = f"{key[:10]}…{key[-4:]}"
    return _clients[key], masked


# ── Core call ──────────────────────────────────────────────────────────────

def call_claude(
    system_prompt: str,
    user_prompt:   str,
    max_tokens:    int   = MAX_TOKENS,
    retries:       int   = 6,
    delay:         float = 5.0,
) -> str:
    """
    Call Gemini with system + user prompt.
    Rotates keys and applies exponential back-off on 429.
    """
    last_err = None

    for attempt in range(1, retries + 1):
        client, masked = _next_client()
        key_tag = f"(rotation {((_key_idx-1) % len(_API_KEYS))+1}/{len(_API_KEYS)})"
        print(f"[API] Using key {masked} {key_tag}")
        try:
            response = client.models.generate_content(
                model=GEMINI_MODEL,
                contents=user_prompt,
                config=types.GenerateContentConfig(
                    system_instruction=system_prompt,
                    max_output_tokens=max_tokens,
                    temperature=0.1,
                ),
            )
            return response.text

        except APIError as exc:
            last_err = exc
            print(f"[API] Attempt {attempt}/{retries} failed "
                  f"(APIError {exc.code}): {exc.message[:120]}")
            if attempt == retries:
                break
            if exc.code == 429:
                m = re.search(r"retry in ([0-9.]+)s", str(exc), re.I)
                wait = float(m.group(1)) + 2.0 if m else delay * (2 ** (attempt - 1))
                print(f"[API] Rate limit — waiting {wait:.1f}s …")
                time.sleep(wait)
            else:
                time.sleep(delay)

        except Exception as exc:
            last_err = exc
            print(f"[API] Attempt {attempt}/{retries} failed: {exc}")
            if attempt < retries:
                time.sleep(delay * attempt)

    raise RuntimeError(
        f"Gemini API call failed after {retries} attempts. Last: {last_err}"
    )


# ── JSON helpers ───────────────────────────────────────────────────────────

def strip_json_fences(text: str) -> str:
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    return text.strip()


def _recover_truncated_array(text: str) -> list | None:
    """
    Attempt to recover a JSON array that was truncated mid-stream.
    Tries progressively more aggressive truncation of the tail.
    Returns a list on success, None on failure.
    """
    if not text.lstrip().startswith("["):
        return None

    # Strategy 1: close at last complete "}" then add "]"
    last = text.rfind("}")
    if last != -1:
        for candidate in [text[:last+1] + "]", text[:last+1] + "\n]"]:
            try:
                result = json.loads(candidate)
                if isinstance(result, list):
                    return [p for p in result if isinstance(p, dict)]
            except json.JSONDecodeError:
                pass

    # Strategy 2: remove last incomplete object (second-to-last "}")
    second = text.rfind("}", 0, last) if last != -1 else -1
    if second != -1:
        for candidate in [text[:second+1] + "]", text[:second+1] + "\n]"]:
            try:
                result = json.loads(candidate)
                if isinstance(result, list):
                    return [p for p in result if isinstance(p, dict)]
            except json.JSONDecodeError:
                pass

    return None


def parse_json_response(raw: str) -> list | dict:
    clean = strip_json_fences(raw)

    # Normal parse
    try:
        return json.loads(clean)
    except json.JSONDecodeError:
        pass

    # Regex extraction of outermost array/object
    m = re.search(r"(\[[\s\S]*\]|\{[\s\S]*\})", clean)
    if m:
        try:
            return json.loads(m.group(1))
        except json.JSONDecodeError:
            pass

    # Truncation recovery
    recovered = _recover_truncated_array(clean)
    if recovered is not None:
        return recovered

    raise ValueError(
        f"Could not parse JSON from response.\nRaw (first 400):\n{raw[:400]}"
    )