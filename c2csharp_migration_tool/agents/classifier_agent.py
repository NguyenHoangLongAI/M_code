"""
agents/classifier_agent.py
AGENT 2 – Pattern Classifier

Takes extracted patterns and enriches each with:
  - pattern_type, sub_types, pattern_group
  - difficulty
  - csharp_popularity
"""

import json
from utils.api_client import call_claude, parse_json_response
from prompts.agent_prompts import CLASSIFIER_SYSTEM, CLASSIFIER_USER

# Max patterns per API call (avoid token overflow)
_BATCH_SIZE = 20


def classify_patterns(extracted: list[dict]) -> list[dict]:
    """
    Classify each pattern.
    Sends in batches to stay within token limits.

    Returns enriched list (original fields + classifier fields).
    """
    print(f"  [Agent2] Classifying {len(extracted)} patterns …")

    results: list[dict] = []
    batches = list(_chunked(extracted, _BATCH_SIZE))

    for batch_idx, batch in enumerate(batches):
        print(f"  [Agent2] Batch {batch_idx+1}/{len(batches)} ({len(batch)} items) …")
        patterns_json = json.dumps(batch, ensure_ascii=False, indent=2)
        user_prompt = CLASSIFIER_USER.format(patterns_json=patterns_json)

        raw = call_claude(CLASSIFIER_SYSTEM, user_prompt)

        try:
            classified_batch = parse_json_response(raw)
        except ValueError as exc:
            print(f"  [Agent2] WARNING: batch {batch_idx+1} parse failed – {exc}")
            classified_batch = _fallback_classify(batch)

        results.extend(_normalise_batch(batch, classified_batch))

    print(f"  [Agent2] Classification complete: {len(results)} patterns.")
    return results


# ── Helpers ────────────────────────────────────────────────────

def _chunked(lst: list, n: int):
    for i in range(0, len(lst), n):
        yield lst[i : i + n]


def _normalise_batch(originals: list[dict], classified: list[dict]) -> list[dict]:
    """Merge classifier output back into original dicts (by id)."""
    cls_by_id = {c.get("id"): c for c in classified if isinstance(c, dict)}
    merged = []
    for orig in originals:
        pid = orig["id"]
        cls = cls_by_id.get(pid, {})
        merged.append({
            **orig,
            "pattern_type":      cls.get("pattern_type", "unknown"),
            "sub_types":         cls.get("sub_types", []),
            "pattern_group":     cls.get("pattern_group", []),
            "difficulty":        cls.get("difficulty", "普通"),
            "csharp_popularity": cls.get("csharp_popularity", 3),
        })
    return merged


def _fallback_classify(batch: list[dict]) -> list[dict]:
    """Minimal fallback when Claude response cannot be parsed."""
    return [
        {
            "id": p["id"],
            "pattern_type": p.get("raw_type", "unknown"),
            "sub_types": [],
            "pattern_group": [],
            "difficulty": "普通",
            "csharp_popularity": 3,
        }
        for p in batch
    ]