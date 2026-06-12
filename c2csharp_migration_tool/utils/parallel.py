"""
utils/parallel.py — Parallel Batch Executor
============================================
Thay thế vòng for tuần tự trong các agents bằng ThreadPoolExecutor.

Tại sao dùng threads (không phải async/multiprocessing)?
  - boto3 client calls là I/O-bound (HTTP request tới AWS Bedrock)
  - GIL không ảnh hưởng khi thread đang chờ network I/O
  - ThreadPoolExecutor cho phép submit tất cả batches cùng lúc
  - boto3 client is thread-safe khi dùng riêng mỗi thread (mỗi call tạo client mới)

Usage:
    results = run_batches_parallel(
        batches=list_of_batch_items,
        worker_fn=lambda batch, batch_idx, total: call_claude(...),
        max_workers=8,
        label="Agent2",
    )
    # results: list theo thứ tự batch gốc (không bị lộn xộn)
"""

from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Callable, TypeVar

T = TypeVar("T")

# ── Global throttle for Bedrock rate limits ────────────────────
# Bedrock có rate limit theo TPM/RPM. Dùng semaphore để giới hạn
# số concurrent calls thực sự đang chạy tại bất kỳ thời điểm nào.
_DEFAULT_MAX_WORKERS = 20   # đủ để song song tốt mà không bị throttle nặng

_print_lock = threading.Lock()


def _safe_print(*args, **kwargs):
    """Thread-safe print (tránh interleaving log lines)."""
    with _print_lock:
        print(*args, **kwargs)


def run_batches_parallel(
    batches: list[list[Any]],
    worker_fn: Callable[[list[Any], int, int], T],
    max_workers: int = _DEFAULT_MAX_WORKERS,
    label: str = "Agent",
    retry_on_exception: bool = True,
    max_retries: int = 3,
) -> list[T]:
    """
    Run worker_fn on all batches in parallel using ThreadPoolExecutor.

    Args:
        batches:     List of batches (each batch = list of items)
        worker_fn:   fn(batch, batch_idx, total) → result
                     batch_idx is 1-based for logging
        max_workers: Max concurrent threads (default 8)
        label:       Log prefix for progress messages
        retry_on_exception: Auto-retry failed batches (serial fallback)
        max_retries: How many times to retry a failed batch

    Returns:
        List of results in the SAME ORDER as input batches.
        Failed batches return None (or raise if retry exhausted).
    """
    if not batches:
        return []

    total = len(batches)
    actual_workers = min(max_workers, total)
    _safe_print(
        f"  [{label}] Parallel execution: {total} batches, "
        f"{actual_workers} concurrent workers"
    )

    results: list[T | None] = [None] * total
    errors:  list[Exception | None] = [None] * total
    completed = 0
    start_ts = time.time()

    def _run_with_retry(batch_idx: int, batch: list[Any]) -> tuple[int, T]:
        """Execute one batch with retry logic."""
        last_exc = None
        for attempt in range(1, max_retries + 1):
            try:
                result = worker_fn(batch, batch_idx + 1, total)
                elapsed = time.time() - start_ts
                _safe_print(
                    f"  [{label}] ✓ Batch {batch_idx+1}/{total} done "
                    f"(attempt {attempt}, {elapsed:.1f}s elapsed)"
                )
                return batch_idx, result
            except Exception as e:
                last_exc = e
                if attempt < max_retries:
                    wait = 2 ** attempt
                    _safe_print(
                        f"  [{label}] ⚠ Batch {batch_idx+1}/{total} "
                        f"attempt {attempt} failed: {e}. Retry in {wait}s..."
                    )
                    time.sleep(wait)
                else:
                    _safe_print(
                        f"  [{label}] ✗ Batch {batch_idx+1}/{total} "
                        f"failed after {max_retries} attempts: {e}"
                    )
        raise last_exc

    with ThreadPoolExecutor(max_workers=actual_workers) as executor:
        # Submit all batches immediately
        futures = {
            executor.submit(_run_with_retry, idx, batch): idx
            for idx, batch in enumerate(batches)
        }

        for future in as_completed(futures):
            try:
                batch_idx, result = future.result()
                results[batch_idx] = result
            except Exception as e:
                batch_idx = futures[future]
                errors[batch_idx] = e
                _safe_print(f"  [{label}] ✗ Batch {batch_idx+1} permanently failed: {e}")

    elapsed = time.time() - start_ts
    success = sum(1 for r in results if r is not None)
    _safe_print(
        f"  [{label}] Parallel done: {success}/{total} batches OK in {elapsed:.1f}s"
    )

    return results


def flatten_ordered_results(
    results: list[list[Any] | None],
    fallback_fn: Callable[[int], list[Any]] | None = None,
) -> list[Any]:
    """
    Flatten list-of-lists results into a single list.
    Skips None entries (failed batches), optionally replacing with fallback.

    Args:
        results:     Output of run_batches_parallel (list of lists or None)
        fallback_fn: Optional fn(batch_idx) → fallback list for failed batches
    """
    flat = []
    for idx, batch_result in enumerate(results):
        if batch_result is not None:
            flat.extend(batch_result)
        elif fallback_fn is not None:
            flat.extend(fallback_fn(idx))
    return flat
