"""
agents/classifier_agent.py — AGENT 2
"""
import json
from utils.api_client import call_claude, parse_json_response
from prompts.agent_prompts import CLASSIFIER_SYSTEM, CLASSIFIER_USER

_BATCH_SIZE = 25


def classify_patterns(extracted: list[dict]) -> list[dict]:
    print(f"  [Agent2] Classifying {len(extracted)} patterns ...")
    results = []
    batches = list(_chunked(extracted, _BATCH_SIZE))
    for idx, batch in enumerate(batches):
        print(f"  [Agent2] Batch {idx+1}/{len(batches)} ({len(batch)} items) ...")
        raw = call_claude(
            CLASSIFIER_SYSTEM,
            CLASSIFIER_USER.format(patterns_json=json.dumps(batch, ensure_ascii=False, indent=2))
        )
        try:
            classified = parse_json_response(raw)
        except ValueError as exc:
            print(f"  [Agent2] WARNING batch {idx+1}: {exc}")
            classified = _fallback(batch)
        results.extend(_merge(batch, classified))
    print(f"  [Agent2] Done: {len(results)} classified.")
    return results


def _chunked(lst, n):
    for i in range(0, len(lst), n):
        yield lst[i:i+n]


def _merge(originals, classified):
    by_id = {c.get("id"): c for c in classified if isinstance(c, dict)}
    out = []
    for o in originals:
        c = by_id.get(o["id"], {})
        out.append({
            **o,
            "pattern_type":      c.get("pattern_type", "unknown"),
            "sub_types":         c.get("sub_types", []),
            "pattern_group":     c.get("pattern_group", []),
            "difficulty":        c.get("difficulty", "Trung bình"),
            "csharp_popularity": c.get("csharp_popularity", 3),
            "needs_review":      c.get("needs_review", False),
        })
    return out


def _fallback(batch):
    return [{"id": p["id"], "pattern_type": p.get("raw_type","unknown"),
             "sub_types":[], "pattern_group":[], "difficulty":"Trung bình",
             "csharp_popularity":3, "needs_review": False} for p in batch]
