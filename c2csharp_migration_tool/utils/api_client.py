"""
utils/api_client.py
Google Gemini API wrapper — dùng google-genai SDK mới (thay thế google.generativeai đã deprecated).
"""
import json
import re
import time

from google import genai
from google.genai import types
# Import class lỗi của SDK mới để handle chuẩn xác
from google.genai.errors import APIError

from config import GEMINI_API_KEY, GEMINI_MODEL, MAX_TOKENS

# ── Module-level client ────────────────────────────────────────
_client: genai.Client | None = None


def get_client() -> genai.Client:
    global _client
    if _client is None:
        if not GEMINI_API_KEY:
            raise RuntimeError(
                "GEMINI_API_KEY chưa được set.\n"
                "  export GEMINI_API_KEY=AIza..."
            )
        _client = genai.Client(api_key=GEMINI_API_KEY)
    return _client


# ── Core call ──────────────────────────────────────────────────

def call_claude(
        system_prompt: str,
        user_prompt: str,
        max_tokens: int = MAX_TOKENS,
        retries: int = 5,  # Tăng số lần thử lại đối với Free Tier (ví dụ: 5 lần)
        delay: float = 5.0,
) -> str:
    """
    Gọi Gemini với system + user prompt.
    Tự động handle lỗi 429 RESOURCE_EXHAUSTED bằng Exponential Backoff.
    """
    client = get_client()
    last_err = None

    for attempt in range(1, retries + 1):
        try:
            response = client.models.generate_content(
                model=GEMINI_MODEL,
                contents=user_prompt,
                config=types.GenerateContentConfig(
                    system_instruction=system_prompt,
                    max_output_tokens=max_tokens,
                    temperature=0.2,
                ),
            )
            return response.text

        except APIError as exc:
            # Handle lỗi từ chính SDK của Google
            last_err = exc
            msg = str(exc)
            print(f"[API] Attempt {attempt}/{retries} failed (APIError {exc.code}): {exc.message}")

            if attempt == retries:
                break

            # Nếu gặp lỗi Rate Limit / Quota Exhausted (429)
            if exc.code == 429:
                # Tìm thời gian cần chờ từ message lỗi
                retry_match = re.search(r"retry in ([0-9.]+)s", msg, re.I)
                if retry_match:
                    wait = float(retry_match.group(1)) + 1.5  # Cộng thêm 1.5s trừ hao
                else:
                    # Áp dụng Exponential Backoff chủ động nếu không parse được chuỗi
                    wait = delay * (2 ** (attempt - 1))

                print(f"[API] Rate limit hit. Waiting {wait:.2f}s before retrying...")
                time.sleep(wait)
            else:
                # Các lỗi API khác (ví dụ 400, 403, 500) thì chờ ngắn rồi thử lại hoặc raise luôn tùy bạn
                time.sleep(delay)

        except Exception as exc:
            # Handle các lỗi kết nối mạng hoặc lỗi không xác định khác
            last_err = exc
            print(f"[API] Attempt {attempt}/{retries} failed: {str(exc)}")
            if attempt < retries:
                time.sleep(delay * attempt)

    raise RuntimeError(f"Gemini API call failed after {retries} attempts. Last Error: {last_err}")

# ── JSON helpers ───────────────────────────────────────────────

def strip_json_fences(text: str) -> str:
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    return text.strip()


def parse_json_response(raw: str) -> list | dict:
    clean = strip_json_fences(raw)
    try:
        return json.loads(clean)
    except json.JSONDecodeError as exc:
        match = re.search(r"(\[[\s\S]*\]|\{[\s\S]*\})", clean)
        if match:
            try:
                return json.loads(match.group(1))
            except json.JSONDecodeError:
                pass
        raise ValueError(
            f"Could not parse JSON from response.\nError: {exc}\nRaw:\n{raw[:500]}"
        )