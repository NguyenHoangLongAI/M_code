"""
agents/translator_agent.py — AGENT 3
Strict 1-to-1 translation. No optimisation. No hallucination.
Comment patterns are passed through verbatim.
"""
import json
from utils.api_client import call_claude, parse_json_response
from prompts.agent_prompts import TRANSLATOR_SYSTEM, TRANSLATOR_USER

_BATCH_SIZE = 12  # smaller: each pattern may have large snippet + response


def translate_patterns(classified: list[dict], source_code: str) -> list[dict]:
    print(f"  [Agent3] Translating {len(classified)} patterns ...")
    results = []
    batches = list(_chunked(classified, _BATCH_SIZE))
    for idx, batch in enumerate(batches):
        print(f"  [Agent3] Batch {idx+1}/{len(batches)} ({len(batch)} items) ...")
        raw = call_claude(
            TRANSLATOR_SYSTEM,
            TRANSLATOR_USER.format(
                classified_json=json.dumps(batch, ensure_ascii=False, indent=2),
                source_code=source_code,
            ),
            max_tokens=65536,
        )
        try:
            translated = parse_json_response(raw)
        except ValueError as exc:
            print(f"  [Agent3] WARNING batch {idx+1}: {exc}")
            translated = _fallback(batch)
        results.extend(_merge(batch, translated))
    print(f"  [Agent3] Done: {len(results)} translated.")
    return results


def _chunked(lst, n):
    for i in range(0, len(lst), n):
        yield lst[i:i+n]


def _merge(originals, translated):
    by_id = {t.get("id"): t for t in translated if isinstance(t, dict)}
    out = []
    for o in originals:
        t = by_id.get(o["id"], {})
        # For comment patterns: ensure snippet is preserved verbatim
        is_comment = "comment" in o.get("raw_type", "") or \
                     o.get("pattern_type", "") == "comment"
        cs_snippet = t.get("csharp_snippet", "")
        if is_comment and not cs_snippet:
            cs_snippet = o.get("source_snippet", "")  # verbatim fallback

        out.append({
            **o,
            "csharp_snippet":     cs_snippet,
            "summary_vi":         t.get("summary_vi", ""),
            "migration_strategy": t.get("migration_strategy", ""),
            "risk_level":         t.get("risk_level", "Trung bình"),
            "risk_strategy":      t.get("risk_strategy", "AI chủ động suggest"),
        })
    return out


def _fallback(batch):
    return [{
        "id": p["id"],
        "csharp_snippet": p.get("source_snippet","") if "comment" in p.get("raw_type","") else f"// TODO id={p['id']}",
        "summary_vi": "Cần kiểm tra thủ công.",
        "migration_strategy": "Manual",
        "risk_level": "Cao",
        "risk_strategy": "Làm thủ công",
    } for p in batch]
