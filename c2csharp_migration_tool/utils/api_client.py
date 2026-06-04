"""
utils/api_client.py
AWS Bedrock — dùng boto3 với credentials extract từ bedrock-api-key
"""
import json, re, time, random, urllib.parse, base64
import boto3
from botocore.config import Config
from typing import Optional
from config import MAX_TOKENS, BEDROCK_MODEL_ID
from utils.key_pool import get_aws_config, mark_success, mark_blocked


def _parse_bedrock_key(api_key: str) -> dict:
    """
    Decode bedrock-api-key-<base64> → extract AWS temp credentials.
    URL dạng: bedrock.amazonaws.com/?Action=CallWithBearerToken
              &X-Amz-Credential=KEYID%2FDATE%2FREGION%2F...
              &X-Amz-Security-Token=...
              &X-Amz-Signature=...
    """
    b64 = api_key.removeprefix("bedrock-api-key-")
    b64 += "=" * (-len(b64) % 4)
    decoded = base64.b64decode(b64).decode("utf-8")

    # Parse query string
    qs = urllib.parse.parse_qs(decoded.split("?", 1)[-1])

    # X-Amz-Credential = AKID/DATE/REGION/bedrock/aws4_request
    credential_str = qs.get("X-Amz-Credential", [""])[0]
    parts = credential_str.split("/")
    access_key_id = parts[0] if parts else ""

    security_token = qs.get("X-Amz-Security-Token", [""])[0]
    signature      = qs.get("X-Amz-Signature",      [""])[0]

    return {
        "aws_access_key_id":     access_key_id,
        "aws_secret_access_key": signature,      # dùng signature làm secret
        "aws_session_token":     security_token,
    }


def _get_client(region: str, creds: dict):
    return boto3.client(
        "bedrock-runtime",
        region_name=region,
        aws_access_key_id=creds["aws_access_key_id"],
        aws_secret_access_key=creds["aws_secret_access_key"],
        aws_session_token=creds["aws_session_token"],
        config=Config(
            read_timeout=300,
            connect_timeout=10,
            retries={"max_attempts": 0},  # tự handle retry
        ),
    )


def call_claude(
    system_prompt: str,
    user_prompt: str,
    max_tokens: int = MAX_TOKENS,
    max_retries: int = 6,
) -> str:
    cfg      = get_aws_config()
    model_id = cfg.get("model_id") or BEDROCK_MODEL_ID
    region   = cfg.get("region_name", "ap-southeast-1")
    api_key  = cfg.get("bedrock_api_key", "")

    if not api_key:
        raise RuntimeError("BEDROCK_API_KEY chưa được cấu hình.")

    creds  = _parse_bedrock_key(api_key)
    client = _get_client(region, creds)

    body = {
        "messages": [
            {"role": "user", "content": [{"text": user_prompt}]}  # bỏ "type"
        ],
        "inferenceConfig": {"maxTokens": max_tokens, "temperature": 0.2},
    }
    if system_prompt:
        body["system"] = [{"text": system_prompt}]

    last_err = None
    for attempt in range(1, max_retries + 1):
        try:
            print(f"[API] Bedrock boto3 — model={model_id} region={region} attempt={attempt}/{max_retries}")
            resp = client.converse(modelId=model_id, **body)

            content = resp.get("output", {}).get("message", {}).get("content", [])
            text = "".join(b.get("text", "") for b in content if "text" in b)

            if not text:
                raise RuntimeError(f"Response rỗng: {resp}")

            mark_success("bedrock")
            return text

        except client.exceptions.ThrottlingException:
            wait = _backoff(attempt)
            print(f"[API] ThrottlingException. Waiting {wait:.0f}s ...")
            mark_blocked("bedrock", wait)
            time.sleep(wait)

        except client.exceptions.ModelErrorException as e:
            raise RuntimeError(f"Model error: {e}")

        except Exception as e:
            err_str = str(e)
            # Key hết hạn
            if "ExpiredToken" in err_str or "expired" in err_str.lower():
                raise RuntimeError(
                    "Bedrock key đã hết hạn (12h).\n"
                    "→ Vào AWS Console → Bedrock → API keys → Generate key mới."
                )
            if "AccessDenied" in err_str or "UnrecognizedClient" in err_str:
                raise RuntimeError(f"Credentials không hợp lệ: {e}")

            last_err = err_str
            wait = _backoff(attempt)
            print(f"[API] Error attempt {attempt}: {e}. Waiting {wait:.0f}s ...")
            if attempt < max_retries:
                time.sleep(wait)

    raise RuntimeError(f"Bedrock thất bại sau {max_retries} lần. Lỗi: {last_err}")


def _backoff(attempt: int) -> float:
    return min(2 ** attempt, 60) + random.uniform(0, 2)


def strip_json_fences(text: str) -> str:
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    return text.strip()


import re


def _sanitize_json_string(raw: str) -> str:
    """
    Fix literal control characters inside JSON string values.
    json.loads rejects actual \t, \n, \r inside strings — they must be \\t etc.
    Strategy: scan character-by-character inside string literals and escape them.
    Faster alternative: use a regex to replace control chars between quotes.
    """

    # Replace literal tab, CR inside what looks like a JSON string token.
    # We do a two-pass approach:
    #  1. Collapse all literal \r\n / \r / \n inside strings → \\n
    #  2. Collapse literal \t inside strings → \\t
    # This regex matches a JSON string span and replaces control chars inside it.
    def fix_string(m):
        s = m.group(0)
        # s starts and ends with "
        inner = s[1:-1]
        # escape literal control chars that JSON forbids
        inner = inner.replace('\r\n', '\\n')
        inner = inner.replace('\r', '\\n')
        inner = inner.replace('\n', '\\n')
        inner = inner.replace('\t', '\\t')
        return '"' + inner + '"'

    # Match JSON string literals (handles \" escapes inside)
    return re.sub(r'"(?:[^"\\]|\\.)*"', fix_string, raw, flags=re.DOTALL)


def parse_json_response(raw: str):
    clean = strip_json_fences(raw)

    # Try direct parse first (most responses are fine)
    try:
        return json.loads(clean)
    except json.JSONDecodeError:
        pass

    # Sanitize control characters inside string literals, then retry
    try:
        sanitized = _sanitize_json_string(clean)
        return json.loads(sanitized)
    except json.JSONDecodeError:
        pass

    # Last resort: extract the outermost array/object
    m = re.search(r'(\[[\s\S]*\]|\{[\s\S]*\})', clean)
    if m:
        try:
            return json.loads(m.group(1))
        except json.JSONDecodeError:
            try:
                sanitized = _sanitize_json_string(m.group(1))
                return json.loads(sanitized)
            except json.JSONDecodeError:
                pass

    raise ValueError(f"Cannot parse JSON.\nRaw:\n{raw[:500]}")
