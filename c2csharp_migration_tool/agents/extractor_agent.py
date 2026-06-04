"""
agents/extractor_agent.py — AGENT 1
Extracts ALL patterns including comment patterns.
"""
import json
from utils.api_client import call_claude, parse_json_response
from prompts.agent_prompts import EXTRACTOR_SYSTEM, EXTRACTOR_USER


def extract_patterns(filename: str, source_code: str) -> list[dict]:
    print("  [Agent1] Extracting patterns (including comments) ...")
    user_prompt = EXTRACTOR_USER.format(filename=filename, source_code=source_code)
    raw = call_claude(EXTRACTOR_SYSTEM, user_prompt, max_tokens=65536)
    try:
        patterns = parse_json_response(raw)
    except ValueError as exc:
        print(f"  [Agent1] WARNING: JSON parse failed – {exc}")
        print("  [Agent1] Raw (first 800):\n", raw[:800])
        patterns = []

    valid = []
    for i, p in enumerate(patterns):
        if not isinstance(p, dict):
            continue
        p.setdefault("id", i + 1)
        p.setdefault("source_snippet", "")
        p.setdefault("line_range", [0, 0])
        p.setdefault("raw_type", "unknown")
        valid.append(p)

    # Re-number sequentially to guarantee no gaps
    for i, p in enumerate(valid):
        p["id"] = i + 1

    print(f"  [Agent1] Extracted {len(valid)} patterns.")
    return valid
