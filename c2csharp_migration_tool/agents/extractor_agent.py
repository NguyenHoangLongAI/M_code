"""
agents/extractor_agent.py — AGENT 1
Chunked extraction for large files.
"""
import json
from utils.api_client import call_claude, parse_json_response
from prompts.agent_prompts import EXTRACTOR_SYSTEM, EXTRACTOR_USER

CHUNK_CHARS = 4000   # ký tự mỗi chunk (~1000 tokens input)
OVERLAP_LINES = 3    # overlap để không bỏ sót pattern ở ranh giới chunk


def extract_patterns(filename: str, source_code: str) -> list[dict]:
    print("  [Agent1] Extracting patterns (including comments) ...")
    chunks = _split_into_chunks(source_code, CHUNK_CHARS, OVERLAP_LINES)
    print(f"  [Agent1] File split into {len(chunks)} chunks.")

    all_patterns = []
    for idx, (chunk_text, line_offset) in enumerate(chunks):
        print(f"  [Agent1] Chunk {idx+1}/{len(chunks)} (line {line_offset}, {len(chunk_text)} chars) ...")
        user_prompt = EXTRACTOR_USER.format(
            filename=filename,
            source_code=chunk_text,
            line_offset=line_offset,
        )
        raw = call_claude(EXTRACTOR_SYSTEM, user_prompt, max_tokens=16000)
        try:
            patterns = parse_json_response(raw)
        except ValueError as exc:
            print(f"  [Agent1] WARNING chunk {idx+1}: {exc}")
            patterns = []

        for p in patterns:
            if isinstance(p, dict):
                p.setdefault("source_snippet", "")
                p.setdefault("line_range", [line_offset, line_offset])
                p.setdefault("raw_type", "unknown")
                all_patterns.append(p)

    # Deduplicate by (source_snippet, raw_type)
    seen = set()
    unique = []
    for p in all_patterns:
        key = (p.get("raw_type",""), p.get("source_snippet","")[:80])
        if key not in seen:
            seen.add(key)
            unique.append(p)

    # Re-number sequentially
    for i, p in enumerate(unique):
        p["id"] = i + 1

    print(f"  [Agent1] Extracted {len(unique)} patterns ({len(all_patterns)} raw, deduped).")
    return unique


def _split_into_chunks(source: str, max_chars: int, overlap_lines: int) -> list[tuple[str, int]]:
    """
    Split source into chunks of ~max_chars, split on line boundaries.
    Returns list of (chunk_text, start_line_number_1based).
    """
    lines = source.splitlines()
    chunks = []
    i = 0
    while i < len(lines):
        chunk_lines = []
        char_count = 0
        j = i
        while j < len(lines) and char_count < max_chars:
            chunk_lines.append(lines[j])
            char_count += len(lines[j]) + 1
            j += 1

        chunk_text = "\n".join(chunk_lines)
        chunks.append((chunk_text, i + 1))  # line_offset is 1-based

        if j >= len(lines):
            break

        # Next chunk starts overlap_lines before end of current chunk
        i = j - overlap_lines

    return chunks
