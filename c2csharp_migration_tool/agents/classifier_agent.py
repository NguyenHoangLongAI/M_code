"""
agents/classifier_agent.py — AGENT 2  (parallel version)
=========================================================
Thay đổi so với bản tuần tự:
  - Tất cả batches được submit song song ngay lập tức
  - Merge + re-merge theo thứ tự id gốc sau khi tất cả xong
  - Fallback per-batch: nếu 1 batch fail → dùng _fallback() cho batch đó
    (không làm hỏng toàn bộ pipeline)
"""

import json
from utils.api_client import call_claude, parse_json_response
from utils.parallel import run_batches_parallel
from prompts.agent_prompts import CLASSIFIER_SYSTEM, CLASSIFIER_USER

_BATCH_SIZE     = 50
_MAX_PAYLOAD_KB = 40
_MAX_WORKERS    = 40


def classify_patterns(extracted: list[dict]) -> list[dict]:
    print(f"  [Agent2] Classifying {len(extracted)} patterns in parallel ...")

    batches = list(_smart_batch(extracted, _BATCH_SIZE, _MAX_PAYLOAD_KB))
    total   = len(batches)
    print(f"  [Agent2] {total} batches × up to {_BATCH_SIZE} patterns → {min(_MAX_WORKERS, total)} workers")

    # ── Worker ────────────────────────────────────────────────────
    def classify_batch(batch: list[dict], batch_idx: int, total_batches: int) -> list[dict]:
        raw = call_claude(
            CLASSIFIER_SYSTEM,
            CLASSIFIER_USER.format(
                patterns_json=json.dumps(batch, ensure_ascii=False, indent=2)
            ),
        )
        try:
            classified = parse_json_response(raw)
        except ValueError as exc:
            print(f"  [Agent2] WARNING batch {batch_idx}: {exc}")
            classified = _fallback(batch)
        return _merge(batch, classified)

    # ── Parallel execution ────────────────────────────────────────
    parallel_results = run_batches_parallel(
        batches=batches,
        worker_fn=classify_batch,
        max_workers=_MAX_WORKERS,
        label="Agent2",
        max_retries=3,
    )

    # ── Flatten in order; use fallback for any failed batch ───────
    results = []
    for idx, batch_result in enumerate(parallel_results):
        if batch_result is not None:
            results.extend(batch_result)
        else:
            # Entire batch failed after retries → use fallback
            print(f"  [Agent2] ✗ Batch {idx+1} permanently failed, using fallback")
            results.extend(_fallback(batches[idx]))

    print(f"  [Agent2] Done: {len(results)} classified.")
    return results


# ── Helpers ───────────────────────────────────────────────────────

def _chunked(lst, n):
    for i in range(0, len(lst), n):
        yield lst[i:i + n]


def _merge(originals: list[dict], classified: list[dict]) -> list[dict]:
    by_id = {c.get("id"): c for c in classified if isinstance(c, dict)}
    out   = []
    for o in originals:
        c = by_id.get(o["id"], {})
        out.append({
            **o,
            "pattern_type":      c.get("pattern_type", "unknown"),
            "sub_types":         c.get("sub_types", []),
            "pattern_group":     c.get("pattern_group", []),
            "difficulty":        c.get("difficulty", "普通"),
            "csharp_popularity": c.get("csharp_popularity", 3),
            "needs_review":      c.get("needs_review", False),
        })
    return out


def _fallback(batch: list[dict]) -> list[dict]:
    return [
        {
            "id":                p["id"],
            "pattern_type":      p.get("raw_type", "unknown"),
            "sub_types":         [],
            "pattern_group":     [],
            "difficulty":        "普通",
            "csharp_popularity": 3,
            "needs_review":      False,
            **p,  # keep all original fields
        }
        for p in batch
    ]

def _smart_batch(lst: list, max_size: int, max_kb: int) -> list[list]:
    import json as _json
    batches, current, current_kb = [], [], 0.0
    for item in lst:
        item_kb = len(_json.dumps(item, ensure_ascii=False)) / 1024
        if current and (len(current) >= max_size or current_kb + item_kb > max_kb):
            batches.append(current)
            current, current_kb = [], 0.0
        current.append(item)
        current_kb += item_kb
    if current:
        batches.append(current)
    return batches
