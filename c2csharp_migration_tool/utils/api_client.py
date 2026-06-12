"""
utils/api_client.py
AWS Bedrock — Bearer token (bedrock-api-key format)
Hỗ trợ prompt caching qua anthropic-beta header.
"""
import json, re, time, random, urllib.request, urllib.error
from config import MAX_TOKENS, BEDROCK_MODEL_ID
from utils.key_pool import get_aws_config, mark_success, mark_blocked


def call_claude(
    system_prompt: str,
    user_prompt: str,
    max_tokens: int = MAX_TOKENS,
    max_retries: int = 6,
    cache_system: bool = False,   # cache system prompt (tiết kiệm tokens)
    cache_user: bool = False,     # cache user prompt (ít dùng)
) -> str:
    cfg      = get_aws_config()
    model_id = cfg.get("model_id") or BEDROCK_MODEL_ID
    region   = cfg.get("region_name", "ap-southeast-2")
    api_key  = cfg.get("bedrock_api_key", "")

    if not api_key:
        raise RuntimeError("BEDROCK_API_KEY chưa được cấu hình.")

    url = (
        f"https://bedrock-runtime.{region}.amazonaws.com"
        f"/model/{model_id}/converse"
    )

    # ── Build system block ─────────────────────────────────────
    system_block = None
    if system_prompt:
        if cache_system:
            system_block = [
                {"text": system_prompt},
                {"cachePoint": {"type": "default"}},
            ]
        else:
            system_block = [{"text": system_prompt}]

    # ── Build user message ─────────────────────────────────────
    if cache_user:
        user_content = [
            {"text": user_prompt},
            {"cachePoint": {"type": "default"}},
        ]
    else:
        user_content = [{"text": user_prompt}]

    body: dict = {
        "messages": [
            {"role": "user", "content": user_content}
        ],
        "inferenceConfig": {"maxTokens": max_tokens, "temperature": 0.2},
    }
    if system_block:
        body["system"] = system_block

    data = json.dumps(body).encode("utf-8")

    last_err = None
    for attempt in range(1, max_retries + 1):
        try:
            print(f"[API] Bedrock — model={model_id} region={region}"
                  f" cache={'S' if cache_system else ''}{'U' if cache_user else ''}"
                  f" attempt={attempt}/{max_retries}")

            req = urllib.request.Request(url, data=data, method="POST")
            req.add_header("Content-Type", "application/json")
            req.add_header("Content-Length", str(len(data)))
            req.add_header("Authorization", f"Bearer {api_key}")
            req.add_header("Connection", "keep-alive")
            # Bật prompt caching nếu cần
            if cache_system or cache_user:
                req.add_header("anthropic-beta", "prompt-caching-2024-07-31")

            # Timeout lớn hơn cho batch lớn (>50KB)
            timeout = 480 if len(data) > 50_000 else 300
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                result = json.loads(resp.read().decode("utf-8"))

            content = (
                result.get("output", {})
                      .get("message", {})
                      .get("content", [])
            )
            text = "".join(b.get("text", "") for b in content if "text" in b)

            if not text:
                raise RuntimeError(f"Response rỗng: {result}")

            # Log cache usage nếu có
            usage = result.get("usage", {})
            if usage.get("cacheReadInputTokens") or usage.get("cacheWriteInputTokens"):
                print(f"[API] Cache — read={usage.get('cacheReadInputTokens',0)}"
                      f" write={usage.get('cacheWriteInputTokens',0)}")

            mark_success("bedrock")
            return text

        except urllib.error.HTTPError as e:
            body_err = e.read().decode("utf-8", errors="replace")
            err_str  = f"HTTP {e.code}: {body_err[:300]}"

            if e.code == 429:
                wait = _backoff(attempt)
                print(f"[API] ThrottlingException (429). Waiting {wait:.0f}s ...")
                mark_blocked("bedrock", wait)
                time.sleep(wait)
            elif e.code in (400, 403):
                # Cache không được hỗ trợ → thử lại không cache
                if cache_system and "cachePoint" in body_err:
                    print(f"[API] Cache không hỗ trợ, retry không cache ...")
                    return call_claude(
                        system_prompt, user_prompt,
                        max_tokens=max_tokens,
                        max_retries=max_retries,
                        cache_system=False,
                        cache_user=False,
                    )
                raise RuntimeError(f"Bedrock error {e.code}: {body_err[:300]}")
            else:
                last_err = err_str
                wait = _backoff(attempt)
                print(f"[API] Error attempt {attempt}: {err_str}. Waiting {wait:.0f}s ...")
                if attempt < max_retries:
                    time.sleep(wait)

        except Exception as e:
            err_str = str(e)
            last_err = err_str
            # Remote closed connection → short wait, không tính là lỗi nặng
            is_disconnect = any(x in err_str.lower() for x in (
                "remote end closed", "connection reset", "remotedisconnected",
                "broken pipe", "connection aborted",
            ))
            wait = 3 if is_disconnect else _backoff(attempt)
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


def _sanitize_json_string(raw: str) -> str:
    def fix_string(m):
        s = m.group(0)
        inner = s[1:-1]
        inner = inner.replace('\r\n', '\\n')
        inner = inner.replace('\r',   '\\n')
        inner = inner.replace('\n',   '\\n')
        inner = inner.replace('\t',   '\\t')
        return '"' + inner + '"'
    return re.sub(r'"(?:[^"\\]|\\.)*"', fix_string, raw, flags=re.DOTALL)


def parse_json_response(raw: str):
    clean = strip_json_fences(raw)
    try:
        return json.loads(clean)
    except json.JSONDecodeError:
        pass
    try:
        return json.loads(_sanitize_json_string(clean))
    except json.JSONDecodeError:
        pass
    m = re.search(r'(\[[\s\S]*\]|\{[\s\S]*\})', clean)
    if m:
        try:
            return json.loads(m.group(1))
        except json.JSONDecodeError:
            try:
                return json.loads(_sanitize_json_string(m.group(1)))
            except json.JSONDecodeError:
                pass
    raise ValueError(f"Cannot parse JSON.\nRaw:\n{raw[:500]}")
