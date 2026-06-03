"""
agents/extractor_agent.py
AGENT 1 – Pattern Extractor  (chunked, truncation-safe)

Splits large source files into overlapping line-windows so each API call
stays well within the output-token limit, then merges and re-numbers results.
"""

import json
from utils.api_client import call_claude, parse_json_response
from prompts.agent_prompts import EXTRACTOR_SYSTEM, EXTRACTOR_USER

# ── Tunables ──────────────────────────────────────────────────
_CHUNK_LINES   = 120   # lines per chunk sent to the model
_OVERLAP_LINES = 15    # overlap between consecutive chunks (catches split constructs)
_MAX_TOKENS    = 32768  # per-call output token budget (gemini-2.5-flash supports up to 65k)


def extract_patterns(filename: str, source_code: str) -> list[dict]:
    """
    Extract all patterns from *source_code*.

    Strategy
    --------
    1. Split source into overlapping windows of _CHUNK_LINES lines.
    2. Call the model once per window, passing absolute line numbers so
       the model can report correct line_range values.
    3. Merge results, drop cross-chunk duplicates (same snippet text),
       and renumber ids sequentially.
    """
    lines = source_code.splitlines()
    total = len(lines)
    print(f"  [Agent1] Extracting patterns — {total} lines, "
          f"chunk={_CHUNK_LINES}, overlap={_OVERLAP_LINES} …")

    if total == 0:
        return []

    # Build windows: list of (start_line_1based, chunk_text)
    windows: list[tuple[int, str]] = []
    start = 0
    while start < total:
        end   = min(start + _CHUNK_LINES, total)
        chunk = "\n".join(lines[start:end])
        windows.append((start + 1, chunk))       # 1-based start
        if end >= total:
            break
        start = end - _OVERLAP_LINES             # step back for overlap

    all_patterns: list[dict] = []

    for win_idx, (line_offset, chunk_text) in enumerate(windows):
        print(f"  [Agent1] Chunk {win_idx+1}/{len(windows)} "
              f"(lines {line_offset}–{line_offset + chunk_text.count(chr(10))}) …")

        user_prompt = EXTRACTOR_USER.format(
            filename=filename,
            source_code=chunk_text,
            line_offset=line_offset,
        )

        raw = call_claude(EXTRACTOR_SYSTEM, user_prompt, max_tokens=_MAX_TOKENS)

        try:
            chunk_patterns = parse_json_response(raw)
        except ValueError as exc:
            # Attempt to salvage a truncated array
            chunk_patterns = _recover_partial_json(raw)
            if chunk_patterns:
                print(f"  [Agent1] WARNING chunk {win_idx+1}: partial recovery "
                      f"→ {len(chunk_patterns)} patterns")
            else:
                print(f"  [Agent1] WARNING chunk {win_idx+1}: parse failed – {exc}")
                chunk_patterns = []

        # Adjust line numbers by offset (model sees lines 1…N inside the chunk,
        # but we told it the real offset in the prompt — model should already report
        # absolute numbers.  As a safety net, shift any line_range that looks
        # relative (starts at 1 and offset > 1).
        for p in chunk_patterns:
            if isinstance(p, dict):
                lr = p.get("line_range", [0, 0])
                if (isinstance(lr, list) and len(lr) == 2
                        and line_offset > 1
                        and lr[0] <= _CHUNK_LINES):          # looks relative
                    p["line_range"] = [
                        lr[0] + line_offset - 1,
                        lr[1] + line_offset - 1,
                    ]

        all_patterns.extend([p for p in chunk_patterns if isinstance(p, dict)])

    # ── Deduplicate by source_snippet (overlapping chunks produce dupes) ──
    seen_snippets: set[str] = set()
    unique: list[dict] = []
    for p in all_patterns:
        key = (p.get("source_snippet", "") or "").strip()
        if key and key in seen_snippets:
            continue
        seen_snippets.add(key)
        unique.append(p)

    # ── Stable sort by start line, then renumber ids ──
    unique.sort(key=lambda p: (p.get("line_range") or [0])[0])
    for i, p in enumerate(unique, start=1):
        p["id"] = i

    # ── Validate required fields ──
    valid: list[dict] = []
    for p in unique:
        p.setdefault("source_snippet", "")
        p.setdefault("line_range", [0, 0])
        p.setdefault("raw_type", "unknown")
        valid.append(p)

    print(f"  [Agent1] Extracted {len(valid)} patterns total.")
    return valid


# ── Helpers ──────────────────────────────────────────────────

def _recover_partial_json(raw: str) -> list[dict]:
    """
    Try to recover a JSON array that was cut off mid-stream.

    Approach: strip the trailing incomplete object and close the array,
    then parse.  Returns [] if recovery fails.
    """
    import re

    text = raw.strip()
    # Remove markdown fences
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text).strip()

    if not text.startswith("["):
        return []

    # Find last complete object: scan backwards for "},"  or "}" near end
    # Strategy: try trimming from last "}" + "]"
    last_brace = text.rfind("}")
    if last_brace == -1:
        return []

    candidate = text[: last_brace + 1] + "]"
    try:
        result = json.loads(candidate)
        if isinstance(result, list):
            return [p for p in result if isinstance(p, dict)]
    except json.JSONDecodeError:
        pass

    # Fallback: remove last incomplete item (find second-to-last "}")
    second_last = text.rfind("}", 0, last_brace)
    if second_last == -1:
        return []
    candidate2 = text[: second_last + 1] + "]"
    try:
        result2 = json.loads(candidate2)
        if isinstance(result2, list):
            return [p for p in result2 if isinstance(p, dict)]
    except json.JSONDecodeError:
        pass

    return []