"""
utils/key_pool.py — Gemini API Key Pool Manager
================================================
Logic:
  • Vòng lặp round-robin qua danh sách keys.
  • Nếu key hiện tại bị 429:
      1. Ghi nhận thời điểm bị block (cooldown).
      2. Chuyển sang key tiếp theo ngay lập tức.
      3. Key bị block sẽ không được dùng lại cho đến khi hết cooldown (60s).
  • Nếu TẤT CẢ keys đều đang bị block → chờ key nào hết cooldown sớm nhất.
  • Nếu không có key nào trong pool → fallback về GEMINI_API_KEY từ env/config.
"""

import time
import threading
from typing import Optional

# ── Internal state (thread-safe) ──────────────────────────────
_lock       = threading.Lock()
_keys: list[str]            = []   # active key list
_current_idx: int           = 0    # round-robin pointer
_blocked: dict[str, float]  = {}   # key → unblock_timestamp
_call_count: dict[str, int] = {}   # key → total successful calls
_block_count: dict[str, int]= {}   # key → total times blocked

COOLDOWN_SECS = 65   # wait after 429 before retrying a key


# ── Public API ────────────────────────────────────────────────

def init_pool(keys: list[str]) -> None:
    """
    Khởi tạo pool với danh sách keys.
    Nên gọi một lần khi khởi động.
    """
    global _keys, _current_idx, _blocked, _call_count, _block_count
    with _lock:
        _keys       = [k.strip() for k in keys if k.strip()]
        _current_idx = 0
        _blocked    = {}
        _call_count = {k: 0 for k in _keys}
        _block_count= {k: 0 for k in _keys}
    if _keys:
        print(f"  [KeyPool] Loaded {len(_keys)} key(s).")
    else:
        print("  [KeyPool] No keys in pool — using env GEMINI_API_KEY fallback.")


def get_key() -> str:
    """
    Trả về key tiếp theo khả dụng.
    Block cho đến khi có key (nếu tất cả đang cooldown).
    """
    with _lock:
        if not _keys:
            # Fallback: empty pool → use env key
            from config import GEMINI_API_KEY
            return GEMINI_API_KEY

        now = time.time()

        # Try round-robin from current index
        for offset in range(len(_keys)):
            idx = (_current_idx + offset) % len(_keys)
            key = _keys[idx]
            if _blocked.get(key, 0) <= now:
                # Found an available key
                _current_idx = (idx + 1) % len(_keys)
                return key

        # All keys blocked → find the one that unblocks soonest
        earliest_key = min(_blocked, key=_blocked.get)
        wait = max(0.0, _blocked[earliest_key] - now)

    # Release lock before sleeping
    if wait > 0:
        print(f"  [KeyPool] All {len(_keys)} keys on cooldown. "
              f"Waiting {wait:.0f}s for next available key…")
        time.sleep(wait + 0.5)

    # Retry after sleep
    return get_key()


def mark_blocked(key: str, retry_after: Optional[float] = None) -> None:
    """
    Đánh dấu key bị 429. Tự động chuyển sang key tiếp theo.
    retry_after: giây phải chờ (lấy từ Gemini error message nếu có).
    """
    cooldown = retry_after + 2.0 if retry_after else COOLDOWN_SECS
    with _lock:
        if key in _keys:
            _blocked[key]     = time.time() + cooldown
            _block_count[key] = _block_count.get(key, 0) + 1
            key_short = key[:12] + "…" + key[-4:]
            print(f"  [KeyPool] Key {key_short} blocked "
                  f"(#{_block_count[key]}) — cooldown {cooldown:.0f}s. "
                  f"Rotating to next key.")


def mark_success(key: str) -> None:
    """Ghi nhận call thành công."""
    with _lock:
        if key in _keys:
            _call_count[key] = _call_count.get(key, 0) + 1


def status() -> dict:
    """Trả về trạng thái pool (cho /health endpoint)."""
    now = time.time()
    with _lock:
        return {
            "total":   len(_keys),
            "available": sum(1 for k in _keys if _blocked.get(k, 0) <= now),
            "blocked":   sum(1 for k in _keys if _blocked.get(k, 0) > now),
            "calls":     dict(_call_count),
            "blocks":    dict(_block_count),
        }
