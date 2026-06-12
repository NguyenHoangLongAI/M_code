"""
agents/cs_marker_mapper.py — Parse pattern-id markers from Agent4 output
==========================================================================
Agent 4 (LLM) được yêu cầu (xem CSHARP_GEN_SYSTEM) bọc output của mỗi
pattern bằng marker:

    // [P:<id>:START]
    ... C# code/comment ...
    // [P:<id>:END]

Module này:
  1. Scan toàn bộ cs_source, tìm tất cả marker pairs.
  2. Gắn cs_line_start / cs_line_end vào pattern tương ứng trong
     migration_data (dựa trên line number SAU KHI đã strip marker —
     vì marker lines sẽ bị xoá khỏi file C# cuối cùng).
  3. Trả về cs_source ĐÃ STRIP marker (clean) để ghi ra file .cs thật.

Hỗ trợ nesting (marker lồng nhau) và nhiều marker pairs cho cùng 1 id
(merge thành 1 range tổng — min start, max end).

Nếu Agent4 KHÔNG emit marker cho 1 pattern nào đó (LLM bỏ sót), pattern
đó sẽ có cs_line_start = cs_line_end = None — UI fallback theo cơ chế cũ
(marker "// ── Pattern #N" hoặc snippet search) như trước.
"""

from __future__ import annotations
import re

_MARKER_RE = re.compile(r'^\s*//\s*\[P:(\d+):(START|END)\]\s*$')


def extract_marker_mapping(cs_source: str) -> tuple[str, dict[int, list[int]]]:
    """
    Scan cs_source, trả về:
      - cs_clean: cs_source với mọi marker line đã bị loại bỏ
      - id_ranges: { pattern_id: [start_line, end_line] }  (1-based,
        theo line numbers trong cs_clean)

    Algorithm:
      - Đi qua từng dòng của cs_source.
      - Nếu dòng là marker START(id) -> push id vào active stack,
        ghi nhận "next output line index + 1" là candidate start cho id.
      - Nếu dòng là marker END(id) -> pop id khỏi stack, ghi nhận
        "current output line index" là candidate end cho id.
      - Mọi dòng KHÔNG phải marker -> append vào cs_clean, tăng
        output line index.
      - Nếu 1 id xuất hiện nhiều marker pairs -> merge range
        (min start, max end).
    """
    lines = cs_source.split("\n")
    clean_lines: list[str] = []
    out_idx = 0  # số dòng đã ghi vào clean_lines (0-based count)

    # pending_start[id] = output line number (1-based) sẽ là start
    #                       nếu dòng kế tiếp không phải marker khác
    pending_start: dict[int, int] = {}
    id_ranges: dict[int, list[int]] = {}

    for raw_line in lines:
        m = _MARKER_RE.match(raw_line)
        if m:
            pid  = int(m.group(1))
            kind = m.group(2)
            if kind == "START":
                # start = dòng tiếp theo trong clean output (1-based)
                pending_start[pid] = out_idx + 1
            else:  # END
                start = pending_start.pop(pid, None)
                end   = out_idx  # dòng cuối cùng đã ghi (1-based == out_idx)
                if start is None:
                    # END không có START tương ứng -> bỏ qua an toàn
                    continue
                if end < start:
                    # pattern rỗng (START ngay trước END, không có nội dung)
                    end = start
                if pid in id_ranges:
                    prev = id_ranges[pid]
                    id_ranges[pid] = [min(prev[0], start), max(prev[1], end)]
                else:
                    id_ranges[pid] = [start, end]
            continue  # marker line không được ghi vào clean output

        clean_lines.append(raw_line)
        out_idx += 1

    cs_clean = "\n".join(clean_lines)
    return cs_clean, id_ranges


def attach_marker_mapping(migration_data: list[dict], cs_source: str) -> str:
    """
    MUTATE migration_data in-place: gắn cs_line_start/cs_line_end cho mỗi
    pattern dựa trên marker [P:<id>:START]/[P:<id>:END] tìm thấy trong
    cs_source.

    Trả về cs_source ĐÃ STRIP marker (dùng để ghi file .cs cuối cùng).

    Pattern không có marker -> cs_line_start = cs_line_end = None
    (giữ nguyên để UI/fallback khác xử lý).
    """
    cs_clean, id_ranges = extract_marker_mapping(cs_source)

    matched = 0
    for p in migration_data:
        pid = p.get("id")
        rng = id_ranges.get(pid)
        if rng:
            p["cs_line_start"] = rng[0]
            p["cs_line_end"]   = rng[1]
            matched += 1
        else:
            p.setdefault("cs_line_start", None)
            p.setdefault("cs_line_end", None)

    print(f"  [CsMarkerMapper] Mapped {matched}/{len(migration_data)} patterns "
          f"via [P:id:START/END] markers from Agent4.")

    return cs_clean
