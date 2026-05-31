"""
agents/translator_agent.py
AGENT 3 – C# Translator

Converts each classified C pattern into idiomatic C# and generates
the analysis fields required by the CSV report.
"""

import json
from utils.api_client import call_claude, parse_json_response
from prompts.agent_prompts import TRANSLATOR_SYSTEM, TRANSLATOR_USER

_BATCH_SIZE = 15   # smaller batches because responses are larger


def translate_patterns(classified: list[dict], source_code: str) -> list[dict]:
    """
    Translate classified patterns to C#.

    Adds these fields to each dict:
      - csharp_snippet
      - summary_vi
      - migration_strategy
      - risk_level
      - risk_strategy
    """
    print(f"  [Agent3] Translating {len(classified)} patterns …")

    results: list[dict] = []
    batches = list(_chunked(classified, _BATCH_SIZE))

    for batch_idx, batch in enumerate(batches):
        print(f"  [Agent3] Batch {batch_idx+1}/{len(batches)} ({len(batch)} items) …")
        classified_json = json.dumps(batch, ensure_ascii=False, indent=2)
        user_prompt = TRANSLATOR_USER.format(
            classified_json=classified_json,
            source_code=source_code,
        )

        raw = call_claude(TRANSLATOR_SYSTEM, user_prompt)

        try:
            translated_batch = parse_json_response(raw)
        except ValueError as exc:
            print(f"  [Agent3] WARNING: batch {batch_idx+1} parse failed – {exc}")
            translated_batch = _fallback_translate(batch)

        results.extend(_merge_batch(batch, translated_batch))

    print(f"  [Agent3] Translation complete: {len(results)} patterns.")
    return results


# ── Helpers ────────────────────────────────────────────────────

def _chunked(lst: list, n: int):
    for i in range(0, len(lst), n):
        yield lst[i : i + n]


def _merge_batch(originals: list[dict], translated: list[dict]) -> list[dict]:
    tr_by_id = {t.get("id"): t for t in translated if isinstance(t, dict)}
    merged = []
    for orig in originals:
        pid = orig["id"]
        tr = tr_by_id.get(pid, {})
        merged.append({
            **orig,
            "csharp_snippet":     tr.get("csharp_snippet", "// TODO: manual migration"),
            "summary_vi":         tr.get("summary_vi", ""),
            "migration_strategy": tr.get("migration_strategy", ""),
            "risk_level":         tr.get("risk_level", "Trung bình"),
            "risk_strategy":      tr.get("risk_strategy", "AI chủ động suggest"),
        })
    return merged


def _fallback_translate(batch: list[dict]) -> list[dict]:
    return [
        {
            "id": p["id"],
            "csharp_snippet": f"// TODO: translate pattern id={p['id']}",
            "summary_vi": "Cần kiểm tra thủ công.",
            "migration_strategy": "Manual review required.",
            "risk_level": "Cao",
            "risk_strategy": "Làm thủ công",
        }
        for p in batch
    ]