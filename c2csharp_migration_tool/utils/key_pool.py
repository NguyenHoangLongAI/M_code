"""
utils/key_pool.py — Bedrock Inline API Key Manager
"""
import os, threading
from typing import Optional

_lock = threading.Lock()
_call_count = 0
_initialized = False
_aws_config: dict = {}


def init_pool(keys=None) -> None:
    global _initialized, _aws_config
    with _lock:
        try:
            import keys as k
            config = {
                "bedrock_api_key": getattr(k, "BEDROCK_API_KEY", ""),
                "region_name":     getattr(k, "AWS_REGION", "ap-southeast-1"),
                "model_id":        getattr(k, "BEDROCK_MODEL_ID", "anthropic.claude-opus-4-5"),
            }
        except ImportError:
            config = {"bedrock_api_key":"","region_name":"ap-southeast-1",
                      "model_id":"anthropic.claude-opus-4-5"}

        for env, cfg in [("BEDROCK_API_KEY","bedrock_api_key"),
                         ("AWS_REGION","region_name"),
                         ("BEDROCK_MODEL_ID","model_id")]:
            v = os.environ.get(env,"")
            if v: config[cfg] = v

        _aws_config = config
        _initialized = True

        key = config.get("bedrock_api_key","")
        if key:
            print(f"  [Bedrock] API key : {key[:16]}...{key[-4:]}")
        else:
            print("  [Bedrock] WARNING: BEDROCK_API_KEY chưa set!")
            print("  [Bedrock]   → Thêm vào keys.py: BEDROCK_API_KEY = 'bedrock-api-key-...'")
        print(f"  [Bedrock] Region  : {config['region_name']}")
        print(f"  [Bedrock] Model   : {config['model_id']}")


def get_aws_config() -> dict:
    if not _initialized: init_pool()
    return dict(_aws_config)

def get_key() -> str: return "bedrock"
def mark_blocked(key, retry_after=None): pass
def mark_success(key):
    global _call_count
    with _lock: _call_count += 1
def mark_dead(key): pass
def status() -> dict:
    cfg = get_aws_config()
    return {"provider":"AWS Bedrock","model":cfg.get("model_id",""),
            "region":cfg.get("region_name",""),"total_calls":_call_count}
