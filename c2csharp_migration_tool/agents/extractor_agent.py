"""
agents/extractor_agent.py — AGENT 1  (parallel + prompt cache)
===============================================================
Cache strategy:
  - cache_system=True → EXTRACTOR_SYSTEM cached sau lần gọi đầu tiên
  - System prompt ~800 tokens → tất cả chunk từ chunk 2 trở đi đều HIT cache
  - Tiết kiệm ~90% system prompt tokens trên N chunks
"""

import json
from utils.api_client import call_claude, parse_json_response
from utils.parallel import run_batches_parallel
from prompts.agent_prompts import EXTRACTOR_SYSTEM, EXTRACTOR_USER

CHUNK_CHARS   = 4000
OVERLAP_LINES = 3
_MAX_WORKERS  = 20


def extract_patterns(filename: str, source_code: str) -> list[dict]:
    print("  [Agent1] Extracting patterns (parallel + cache) ...")

    chunks = _split_into_chunks(source_code, CHUNK_CHARS, OVERLAP_LINES)
    total  = len(chunks)
    print(f"  [Agent1] {total} chunks → {min(_MAX_WORKERS, total)} parallel workers")

    def process_chunk(chunk_info: tuple, batch_idx: int, total_batches: int) -> list[dict]:
        chunk_text, line_offset = chunk_info
        user_prompt = EXTRACTOR_USER.format(
            filename=filename,
            source_code=chunk_text,
            line_offset=line_offset,
        )
        raw = call_claude(
            EXTRACTOR_SYSTEM,
            user_prompt,
            max_tokens=16000,
            cache_system=True,   # ← cache system prompt
        )
        try:
            patterns = parse_json_response(raw)
        except ValueError as exc:
            print(f"  [Agent1] WARNING chunk {batch_idx}: {exc}")
            patterns = []

        result = []
        for p in patterns:
            if isinstance(p, dict):
                p.setdefault("source_snippet", "")
                p.setdefault("line_range", [line_offset, line_offset])
                p.setdefault("raw_type", "unknown")
                result.append(p)
        return result

    parallel_results = run_batches_parallel(
        batches=[(ct, lo) for ct, lo in chunks],
        worker_fn=process_chunk,
        max_workers=_MAX_WORKERS,
        label="Agent1",
        max_retries=3,
    )

    all_patterns = []
    for chunk_result in parallel_results:
        if chunk_result:
            all_patterns.extend(chunk_result)

    # Dedup + sort + re-number
    seen   = set()
    unique = []
    for p in all_patterns:
        key = (p.get("raw_type", ""), p.get("source_snippet", "")[:80])
        if key not in seen:
            seen.add(key)
            unique.append(p)

    unique.sort(key=lambda p: (p.get("line_range") or [0, 0])[0])
    for i, p in enumerate(unique):
        p["id"] = i + 1

    print(f"  [Agent1] Extracted {len(unique)} patterns ({len(all_patterns)} raw, deduped).")
    return unique


def _split_into_chunks(source: str, max_chars: int, overlap_lines: int) -> list[tuple[str, int]]:
    lines  = source.splitlines()
    chunks = []
    i = 0
    while i < len(lines):
        chunk_lines = []
        char_count  = 0
        j = i
        while j < len(lines) and char_count < max_chars:
            chunk_lines.append(lines[j])
            char_count += len(lines[j]) + 1
            j += 1
        chunks.append(("\n".join(chunk_lines), i + 1))
        if j >= len(lines):
            break
        i = j - overlap_lines
    return chunks
