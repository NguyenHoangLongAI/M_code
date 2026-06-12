"""
agents/translator_agent.py — AGENT 3  (parallel version)
=========================================================
Thay đổi so với bản tuần tự:
  - Tất cả batches submit song song bằng ThreadPoolExecutor
  - source_code được pass vào mỗi worker (read-only, thread-safe)
  - Merge theo thứ tự id gốc sau khi all batches xong
  - Comment patterns vẫn được fallback verbatim nếu batch fail
"""

import json
from utils.api_client import call_claude, parse_json_response
from utils.parallel import run_batches_parallel
from prompts.agent_prompts import TRANSLATOR_SYSTEM, TRANSLATOR_USER

_BATCH_SIZE     = 32    # max patterns per batch
_MAX_PAYLOAD_KB = 60    # max KB per request body
_MAX_WORKERS    = 10


def translate_patterns(classified: list[dict], source_code: str) -> list[dict]:
    print(f"  [Agent3] Translating {len(classified)} patterns in parallel ...")

    batches = list(_smart_batch(classified, _BATCH_SIZE, _MAX_PAYLOAD_KB))
    total   = len(batches)
    print(f"  [Agent3] {total} batches × up to {_BATCH_SIZE} patterns → {min(_MAX_WORKERS, total)} workers")

    # ── Worker (source_code captured by closure — read-only) ──────
    def translate_batch(batch: list[dict], batch_idx: int, total_batches: int) -> list[dict]:
        raw = call_claude(
            TRANSLATOR_SYSTEM,
            TRANSLATOR_USER.format(
                classified_json=json.dumps(batch, ensure_ascii=False, indent=2),
                source_code=source_code,
            ),
            max_tokens=16000,
        )
        try:
            translated = parse_json_response(raw)
        except ValueError as exc:
            print(f"  [Agent3] WARNING batch {batch_idx}: {exc}")
            translated = _fallback(batch)
        return _merge(batch, translated)

    # ── Parallel execution ────────────────────────────────────────
    parallel_results = run_batches_parallel(
        batches=batches,
        worker_fn=translate_batch,
        max_workers=_MAX_WORKERS,
        label="Agent3",
        max_retries=3,
    )

    # ── Flatten in order; fallback for any permanently failed batch ─
    results = []
    for idx, batch_result in enumerate(parallel_results):
        if batch_result is not None:
            results.extend(batch_result)
        else:
            print(f"  [Agent3] ✗ Batch {idx+1} permanently failed, using fallback")
            results.extend(_fallback(batches[idx]))

    print(f"  [Agent3] Done: {len(results)} translated.")
    return results


# ── Helpers ───────────────────────────────────────────────────────

def _chunked(lst, n):
    for i in range(0, len(lst), n):
        yield lst[i:i + n]


def _smart_batch(lst: list, max_size: int, max_kb: int) -> list[list]:
    """
    Tạo batches đảm bảo:
      1. Không quá max_size patterns
      2. JSON payload không quá max_kb KB
    """
    import json as _json
    batches = []
    current = []
    current_kb = 0.0

    for item in lst:
        item_kb = len(_json.dumps(item, ensure_ascii=False)) / 1024
        # Nếu thêm item này sẽ vượt giới hạn → flush batch hiện tại
        if current and (len(current) >= max_size or current_kb + item_kb > max_kb):
            batches.append(current)
            current = []
            current_kb = 0.0
        current.append(item)
        current_kb += item_kb

    if current:
        batches.append(current)

    return batches


def _merge(originals: list[dict], translated: list[dict]) -> list[dict]:
    by_id = {t.get("id"): t for t in translated if isinstance(t, dict)}
    out   = []
    for o in originals:
        t = by_id.get(o["id"], {})
        # Comment patterns: preserve snippet verbatim if no CS output
        is_comment = (
            "comment" in o.get("raw_type", "")
            or o.get("pattern_type", "") == "comment"
        )
        cs_snippet = t.get("csharp_snippet", "")
        if is_comment and not cs_snippet:
            cs_snippet = o.get("source_snippet", "")

        out.append({
            **o,
            "csharp_snippet":     cs_snippet,
            "summary_vi":         t.get("summary_vi", ""),
            "migration_strategy": t.get("migration_strategy", ""),
            "risk_level":         t.get("risk_level", "中"),
            "risk_strategy":      t.get("risk_strategy", "AI提案"),
        })
    return out


def _fallback(batch: list[dict]) -> list[dict]:
    return [
        {
            "id":                 p["id"],
            "csharp_snippet":     (
                p.get("source_snippet", "")
                if "comment" in p.get("raw_type", "")
                else f"// TODO id={p['id']}"
            ),
            "summary_vi":         "手動確認が必要です。",
            "migration_strategy": "Manual",
            "risk_level":         "高",
            "risk_strategy":      "手動対応",
            **{k: v for k, v in p.items() if k not in (
                "csharp_snippet", "summary_vi", "migration_strategy",
                "risk_level", "risk_strategy",
            )},
        }
        for p in batch
    ]
