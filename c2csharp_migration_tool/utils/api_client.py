"""
utils/api_client.py
Gemini API wrapper với Key Pool rotation.

Features:
- Auto rotate key khi gặp rate limit (429 retryable)
- Dừng ngay khi gặp Daily Quota Exhausted
- Retry cho lỗi 5xx/network
- Cache client theo API key
"""

import json
import re
import threading
import time
from typing import Optional

from google import genai
from google.genai import types
from google.genai.errors import APIError

from config import GEMINI_MODEL, MAX_TOKENS
from utils.key_pool import (
    get_key,
    mark_blocked,
    mark_success,
    mark_dead,
    status,
)


# ============================================================
# Client cache
# ============================================================

_clients: dict[str, genai.Client] = {}
_clients_lock = threading.Lock()


def _get_client(key: str) -> genai.Client:
    with _clients_lock:
        if key not in _clients:
            _clients[key] = genai.Client(api_key=key)

        return _clients[key]


# ============================================================
# Helpers
# ============================================================

def _parse_retry_after(msg: str) -> Optional[float]:
    """
    Parse:
        retry in 17.3s
        retry after 30s
    """
    match = re.search(
        r"retry.{0,20}?([0-9]+(?:\.[0-9]+)?)\s*s",
        msg,
        re.IGNORECASE,
    )

    if match:
        return float(match.group(1))

    return None


def _is_daily_quota_error(msg: str) -> bool:
    """
    Detect daily quota exhaustion.

    Example:
        GenerateRequestsPerDayPerModel-FreeTier
        quota exceeded for metric
        free_tier_requests
    """

    msg = msg.lower()

    patterns = [
        "generaterequestsperday",
        "quota exceeded for metric",
        "free_tier_requests",
        "perday",
    ]

    return any(p in msg for p in patterns)


def _short_key(key: str) -> str:
    if len(key) < 16:
        return key

    return f"{key[:10]}...{key[-4:]}"


# ============================================================
# Core API
# ============================================================

def call_claude(
    system_prompt: str,
    user_prompt: str,
    max_tokens: int = MAX_TOKENS,
    max_key_rotations: int = 12,
    non_rate_retries: int = 3,
) -> str:
    """
    Gemini API wrapper.

    Returns:
        str

    Raises:
        RuntimeError
        APIError
    """

    last_err = None
    rotations = 0

    while rotations < max_key_rotations:

        key = get_key()

        if not key:
            raise RuntimeError(
                "No Gemini API key available."
            )

        client = _get_client(key)

        print(
            f"[API] Using key {_short_key(key)} "
            f"(rotation {rotations + 1}/{max_key_rotations})"
        )

        for attempt in range(1, non_rate_retries + 1):

            try:

                response = client.models.generate_content(
                    model=GEMINI_MODEL,
                    contents=user_prompt,
                    config=types.GenerateContentConfig(
                        system_instruction=system_prompt or None,
                        max_output_tokens=max_tokens,
                        temperature=0.2,
                    ),
                )

                text = getattr(response, "text", None)

                if not text:
                    raise RuntimeError(
                        "Gemini returned empty response."
                    )

                mark_success(key)

                return text

            except APIError as exc:

                last_err = exc

                code = getattr(exc, "code", None) or 0
                msg = getattr(exc, "message", str(exc))

                # --------------------------------------------------
                # 429
                # --------------------------------------------------
                if code == 429:

                    if _is_daily_quota_error(msg):
                        print(
                            f"[API] Daily quota exhausted "
                            f"for {_short_key(key)}"
                        )

                        mark_dead(key)

                        rotations += 1

                        break

                    retry_after = _parse_retry_after(msg)

                    print(
                        f"[API] Rate limit on {_short_key(key)} "
                        f"-> rotating key"
                    )

                    mark_blocked(
                        key,
                        retry_after
                    )

                    rotations += 1

                    break

                # --------------------------------------------------
                # 5xx
                # --------------------------------------------------
                elif code in (500, 502, 503, 504):

                    wait = min(5 * attempt, 30)

                    print(
                        f"[API] Server error {code} "
                        f"(attempt {attempt}/{non_rate_retries}) "
                        f"Retry in {wait}s"
                    )

                    if attempt < non_rate_retries:
                        time.sleep(wait)
                        continue

                    rotations += 1
                    break

                # --------------------------------------------------
                # Other API errors
                # --------------------------------------------------
                else:
                    raise

            except Exception as exc:

                last_err = exc

                wait = min(5 * attempt, 30)

                print(
                    f"[API] Network/Unknown error "
                    f"(attempt {attempt}/{non_rate_retries}): "
                    f"{exc}"
                )

                if attempt < non_rate_retries:
                    time.sleep(wait)
                    continue

                rotations += 1
                break

    raise RuntimeError(
        f"Gemini API failed after "
        f"{rotations} key rotation(s). "
        f"Last error: {last_err}"
    )


# ============================================================
# JSON Helpers
# ============================================================

def strip_json_fences(text: str) -> str:

    text = text.strip()

    text = re.sub(
        r"^```(?:json)?\s*",
        "",
        text,
    )

    text = re.sub(
        r"\s*```$",
        "",
        text,
    )

    return text.strip()


def parse_json_response(raw: str):

    clean = strip_json_fences(raw)

    try:
        return json.loads(clean)

    except json.JSONDecodeError as exc:

        match = re.search(
            r"(\[[\s\S]*\]|\{[\s\S]*\})",
            clean,
        )

        if match:
            try:
                return json.loads(match.group(1))
            except json.JSONDecodeError:
                pass

        raise ValueError(
            f"Cannot parse JSON.\n"
            f"Error: {exc}\n"
            f"Raw:\n{raw[:500]}"
        )
