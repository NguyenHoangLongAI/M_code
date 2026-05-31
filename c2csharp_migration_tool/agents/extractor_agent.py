"""
agents/extractor_agent.py
AGENT 1 – Pattern Extractor

Scans C/Pro*C source and returns a list of raw pattern snippets with
position info and a coarse type label.
"""

import json
from utils.api_client import call_claude, parse_json_response
from prompts.agent_prompts import EXTRACTOR_SYSTEM, EXTRACTOR_USER


def extract_patterns(filename: str, source_code: str) -> list[dict]:
    """
    Call Claude to extract all patterns from the source.

    Returns a list of dicts:
        {
          "id": int,
          "source_snippet": str,
          "line_range": [int, int],
          "raw_type": str,
        }
    """
    print("  [Agent1] Extracting patterns …")

    user_prompt = EXTRACTOR_USER.format(
        filename=filename,
        source_code=source_code,
    )

    raw = call_claude(EXTRACTOR_SYSTEM, user_prompt)

    try:
        patterns = parse_json_response(raw)
    except ValueError as exc:
        print(f"  [Agent1] WARNING: JSON parse failed – {exc}")
        print("  [Agent1] Raw response (first 800 chars):\n", raw[:800])
        patterns = []

    # Validate / normalise
    valid = []
    for i, p in enumerate(patterns):
        if not isinstance(p, dict):
            continue
        p.setdefault("id", i + 1)
        p.setdefault("source_snippet", "")
        p.setdefault("line_range", [0, 0])
        p.setdefault("raw_type", "unknown")
        valid.append(p)

    print(f"  [Agent1] Extracted {len(valid)} patterns.")
    return valid