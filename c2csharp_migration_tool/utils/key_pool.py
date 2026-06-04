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
_dead: set[str] = set()
COOLDOWN_SECS = 65   # wait after 429 before retrying a key


# ── Public API ────────────────────────────────────────────────

def init_pool(keys: list[str]) -> None:
    """
    Khởi tạo pool với danh sách keys.
    Nên gọi một lần khi khởi động.
    """
    global _keys, _current_idx, _blocked, _call_count, _block_count, _dead

    with _lock:
        _keys = [k.strip() for k in keys if k.strip()]
        _current_idx = 0
        _blocked = {}
        _dead = set()
        _call_count = {k: 0 for k in _keys}
        _block_count = {k: 0 for k in _keys}
    if _keys:
        print(f"  [KeyPool] Loaded {len(_keys)} key(s).")
    else:
        print("  [KeyPool] No keys in pool — using env GEMINI_API_KEY fallback.")

def mark_dead(key: str) -> None:
    """
    Key/project đã hết quota ngày.
    Không được sử dụng lại trong process hiện tại.
    """

    with _lock:

        if key not in _keys:
            return

        _dead.add(key)

        key_short = key[:12] + "…" + key[-4:]

        print(
            f"  [KeyPool] Key {key_short} permanently disabled "
            f"(daily quota exhausted)."
        )

def get_key() -> str:

    global _current_idx

    while True:

        with _lock:

            if not _keys:
                from config import GEMINI_API_KEY
                return GEMINI_API_KEY

            now = time.time()

            alive_keys = [
                k for k in _keys
                if k not in _dead
            ]

            if not alive_keys:
                raise RuntimeError(
                    "All Gemini API keys exhausted "
                    "(daily quota exceeded)."
                )

            for offset in range(len(_keys)):

                idx = (_current_idx + offset) % len(_keys)

                key = _keys[idx]

                if key in _dead:
                    continue

                if _blocked.get(key, 0) <= now:

                    _current_idx = (idx + 1) % len(_keys)

                    return key

            blocked_alive = [
                k for k in alive_keys
                if k in _blocked
            ]

            if not blocked_alive:

                raise RuntimeError(
                    "No available Gemini API key."
                )

            earliest_key = min(
                blocked_alive,
                key=lambda k: _blocked[k]
            )

            wait = max(
                0.0,
                _blocked[earliest_key] - now
            )

        if wait > 0:

            print(
                f"  [KeyPool] All alive keys cooling down. "
                f"Waiting {wait:.0f}s..."
            )

            time.sleep(wait + 0.5)


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
