"""
agents/cs_line_mapper.py — Post-processing: map pattern → C# line range
========================================================================
Agent 4 (LLM, csharp_generator_agent.py) sinh ra TOÀN BỘ file C# dạng
1 chuỗi text. Module này KHÔNG gọi LLM — chỉ search csharp_snippet (đã có
từ Agent 3) trong nội dung .cs đã sinh, để gắn cs_line_start/cs_line_end
vào từng pattern.

v2 — fix 2 nhóm lỗi không tìm được mapping:
  1. Comment-only snippets (raw_type *_comment, hoặc csharp_snippet toàn
     bộ là dòng // hoặc /* ... */): trước đây bị loại hết bởi
     _is_meaningful() -> meaningful=[] -> None. Giờ fallback dùng chính
     các dòng comment làm anchor khi snippet không có code "thật".
  2. Brace-style mismatch: Agent3 sinh "if (x) {" (K&R), nhưng Agent4 LLM
     thường viết lại theo Allman style:
         if (x)
         {
     -> _normalize() giờ strip dấu '{' / '}' ở CUỐI dòng trước khi so
        sánh, để "if (x) {" và "if (x)" được coi là khớp.
"""

from __future__ import annotations
import re


def _normalize(line: str) -> str:
    """
    Collapse whitespace + strip trailing '{'/'}' (và khoảng trắng quanh nó)
    để khoan dung khác biệt brace-style (K&R vs Allman) và indent.
    """
    s = " ".join(line.split())
    s = re.sub(r"\s*[{}]\s*$", "", s)
    return s.strip()


def _is_code_line(line: str) -> bool:
    """Dòng có 'nội dung' code thực sự — không phải brace/comment/blank thuần."""
    t = line.strip()
    if not t:
        return False
    if t in ("{", "}", "};"):
        return False
    if t.startswith("//") or t.startswith("/*") or t.startswith("*"):
        return False
    return True


def _is_comment_line(line: str) -> bool:
    t = line.strip()
    return bool(t) and (t.startswith("//") or t.startswith("/*") or t.startswith("*") or t.endswith("*/"))


def _snippet_lines(snippet: str) -> list[str]:
    return [l for l in (snippet or "").split("\n")]


def _build_anchor_pool(s_lines: list[str]) -> list[str]:
    """
    Trả về danh sách các dòng (normalized) dùng để tìm anchor + verify,
    theo thứ tự xuất hiện trong snippet.

    - Ưu tiên các dòng "code thật" (_is_code_line).
    - Nếu KHÔNG có dòng code nào (snippet toàn comment, ví dụ
      raw_type=*_comment hoặc snippet là block // ... // toàn bộ),
      fallback dùng các dòng comment có nội dung (bỏ dòng trống).
    """
    code_lines = [_normalize(l) for l in s_lines if _is_code_line(l)]
    if code_lines:
        return code_lines

    comment_lines = [_normalize(l) for l in s_lines if _is_comment_line(l)]
    if comment_lines:
        return comment_lines

    return [_normalize(l) for l in s_lines if l.strip()]


def attach_cs_line_mapping(migration_data: list[dict], cs_source: str) -> None:
    """
    MUTATE migration_data in-place: thêm cs_line_start / cs_line_end
    cho mỗi pattern, dựa trên việc match csharp_snippet vào cs_source.
    """
    cs_lines = cs_source.split("\n")
    cs_norm  = [_normalize(l) for l in cs_lines]
    n        = len(cs_lines)

    ordered = sorted(
        migration_data,
        key=lambda p: (p.get("line_range") or [0, 0])[0]
    )

    cursor  = 0
    matched = 0

    for p in ordered:
        snippet = p.get("csharp_snippet", "") or ""
        s_lines = _snippet_lines(snippet)
        anchors = _build_anchor_pool(s_lines)

        if not anchors:
            p["cs_line_start"] = None
            p["cs_line_end"]   = None
            continue

        anchors = [a for a in anchors if a] or anchors
        anchor = anchors[0]
        if not anchor:
            p["cs_line_start"] = None
            p["cs_line_end"]   = None
            continue

        found_idx = None

        for i in range(cursor, n):
            if cs_norm[i] and (cs_norm[i] == anchor or anchor in cs_norm[i]):
                found_idx = i
                break

        if found_idx is None:
            for i in range(0, n):
                if cs_norm[i] and (cs_norm[i] == anchor or anchor in cs_norm[i]):
                    found_idx = i
                    break

        if found_idx is None:
            p["cs_line_start"] = None
            p["cs_line_end"]   = None
            continue

        snippet_len   = len(s_lines)
        candidate_end = found_idx + snippet_len - 1
        end_idx       = min(candidate_end, n - 1)

        remaining = anchors[1:]
        j = found_idx + 1
        last_match = found_idx
        for rline in remaining:
            advanced = False
            for k in range(j, min(j + 3, n)):
                if cs_norm[k] and (cs_norm[k] == rline or rline in cs_norm[k]):
                    last_match = k
                    j = k + 1
                    advanced = True
                    break
            if not advanced:
                break

        end_idx = max(end_idx, last_match)
        end_idx = min(end_idx, n - 1)

        p["cs_line_start"] = found_idx + 1
        p["cs_line_end"]   = end_idx + 1
        cursor = max(cursor, found_idx + 1)
        matched += 1

    print(f"  [CsLineMapper] Mapped {matched}/{len(migration_data)} patterns "
          f"to C# line ranges (post-process, no LLM).")
