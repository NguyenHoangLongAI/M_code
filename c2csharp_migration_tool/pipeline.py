"""
pipeline.py — Migration Pipeline Orchestrator v8
=================================================
v8 changes vs v7:
  - Agent 4 (csharp_generator_agent, LLM) giữ nguyên — vẫn gọi LLM để
    sinh toàn bộ file C#, NHƯNG được yêu cầu emit marker comments
    // [P:<id>:START] ... // [P:<id>:END] cho mỗi pattern (xem
    prompts/agent_prompts.py — CSHARP_GEN_SYSTEM/USER).
  - Sau Agent4: agents/cs_marker_mapper.attach_marker_mapping() parse
    các marker này để gắn cs_line_start/cs_line_end vào từng pattern,
    đồng thời STRIP marker khỏi nội dung trước khi ghi file .cs cuối.
  - Fallback: nếu LLM bỏ sót marker cho 1 số pattern (cs_line_start
    vẫn None sau bước trên), dùng agents/cs_line_mapper.attach_cs_line_mapping()
    (fuzzy snippet-matching, không gọi LLM) để cố gắng map phần còn lại.

v7 changes vs v6:
  - Agent 1, 2, 3: tất cả batches chạy SONG SONG bằng ThreadPoolExecutor
    → file 50-100 chunks: từ N×T giây → ~T giây (T = thời gian 1 batch)
  - run_pipeline_multi(): chạy nhiều files SONG SONG (dùng cho batch migration)

Agent responsibilities:
  Agent 1 — Extract patterns (parallel chunks, no LLM order dependency)
  Agent 2 — Classify patterns (parallel batches)
  Agent 3 — Translate patterns → csharp_snippet + analysis (parallel batches)
  Agent 4 — [LLM] Generate full C# file with [P:id] markers
  Agent 5 — CSV report (pure Python)
  Agent 6 — Cross-file call graph (background thread after pipeline)
"""

import time
import json
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

from utils.file_utils import (
    read_source_file, ensure_output_dir,
    write_patterns_csv, write_csharp_file, save_json_debug,
)
from agents.extractor_agent          import extract_patterns
from agents.classifier_agent         import classify_patterns
from agents.translator_agent         import translate_patterns
from agents.csharp_generator_agent   import generate_csharp_file
from agents.cs_marker_mapper         import attach_marker_mapping
from agents.cs_line_mapper           import attach_cs_line_mapping
from agents.report_builder_agent     import build_csv_rows, print_summary
from config import OUTPUT_DIR


def run_pipeline(source_path: str, output_dir: str = OUTPUT_DIR) -> dict:
    """
    Run the full migration pipeline for a single source file.
    Agents 1-3 run with internal parallelism (parallel batches/chunks).

    Returns:
        {
            "csv_path":  str,
            "cs_path":   str,
            "patterns":  list[dict],   # enriched with cs_line_start/end
        }
    """
    t0 = time.time()
    print(f"\n{'═'*60}")
    print(f"  C → C# Migration Pipeline  [v8 / Pattern-ID Markers]")
    print(f"  Source : {source_path}")
    print(f"  Output : {output_dir}")
    print(f"{'═'*60}\n")

    # ── Step 0: Read source ────────────────────────────────────
    print("[Step 0] Reading source file ...")
    filename, source_code = read_source_file(source_path)
    ensure_output_dir(output_dir)

    # ── Step 1: Extract patterns (parallel chunks) ─────────────
    print("\n[Step 1] Pattern Extraction (parallel chunks) ...")
    t1 = time.time()
    extracted = extract_patterns(filename, source_code)
    print(f"  [Step 1] Done in {time.time()-t1:.1f}s — {len(extracted)} patterns")
    save_json_debug(extracted, "1_extracted", output_dir)
    if not extracted:
        print("  WARNING: No patterns extracted. Check API key or source file.")

    # ── Step 2: Classify patterns (parallel batches) ───────────
    print("\n[Step 2] Pattern Classification (parallel batches) ...")
    t2 = time.time()
    classified = classify_patterns(extracted)
    print(f"  [Step 2] Done in {time.time()-t2:.1f}s")
    save_json_debug(classified, "2_classified", output_dir)

    # ── Step 3: Translate patterns (parallel batches) ──────────
    print("\n[Step 3] C# Translation (parallel batches) ...")
    t3 = time.time()
    migration_data = translate_patterns(classified, source_code)
    print(f"  [Step 3] Done in {time.time()-t3:.1f}s")
    save_json_debug(migration_data, "3_translated", output_dir)

    # ── Step 3b: Save per-file debug JSON for call graph ───────
    stem = Path(filename).stem
    per_file_debug = Path(output_dir) / f"_debug_3_{stem}.json"
    per_file_debug.write_text(
        json.dumps(migration_data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"  [Pipeline] Saved per-file patterns → {per_file_debug.name}")

    # ── Step 4: Generate C# file (LLM, with [P:id] markers) ────
    print("\n[Step 4] C# File Generation (LLM, pattern-id markers) ...")
    t4 = time.time()

    csharp_source_raw = generate_csharp_file(filename, source_code, migration_data)

    # Parse // [P:<id>:START] / // [P:<id>:END] markers → cs_line_start/end,
    # and return the source with markers stripped.
    csharp_source = attach_marker_mapping(migration_data, csharp_source_raw)

    # Fallback: patterns the LLM forgot to mark → fuzzy snippet matching
    # (no LLM call). Operates on the SAME dict objects (in-place mutation),
    # so migration_data stays consistent.
    unmapped = [p for p in migration_data if p.get("cs_line_start") is None]
    if unmapped:
        print(f"  [Step 4] {len(unmapped)} pattern(s) missing [P:id] markers "
              f"→ fallback fuzzy line mapping ...")
        attach_cs_line_mapping(unmapped, csharp_source)

    cs_path = write_csharp_file(csharp_source, filename, output_dir)
    print(f"  [Step 4] Done in {time.time()-t4:.1f}s")
    save_json_debug(migration_data, "4_assembled", output_dir)

    # Update per-file debug JSON with cs_line_start/end
    per_file_debug.write_text(
        json.dumps(migration_data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    # ── Step 5: CSV report ─────────────────────────────────────
    print("\n[Step 5] Building Pattern Report ...")
    csv_rows = build_csv_rows(migration_data)
    csv_path = str(Path(output_dir) / f"{stem}_patterns.csv")
    write_patterns_csv(csv_rows, csv_path)

    print_summary(csv_rows)
    elapsed = time.time() - t0
    print(f"\n{'═'*60}")
    print(f"  Pipeline complete in {elapsed:.1f}s")
    print(f"  → C# file : {cs_path}")
    print(f"  → CSV     : {csv_path}")
    print(f"  → Patterns: {len(migration_data)} (each has cs_line_start/cs_line_end)")
    print(f"{'═'*60}\n")

    return {
        "csv_path": csv_path,
        "cs_path":  cs_path,
        "patterns": migration_data,
    }


def run_pipeline_multi(
    source_paths: list[str],
    output_dir: str = OUTPUT_DIR,
    max_concurrent_files: int = 3,
) -> dict[str, dict]:
    """
    Run pipeline for MULTIPLE files concurrently.
    Each file runs its own parallel agents internally.

    WARNING: max_concurrent_files × agent_workers concurrent API calls.
    Recommended: max_concurrent_files=2-3 to avoid Bedrock throttling.

    Returns:
        { source_path: pipeline_result_dict }
    """
    print(f"\n{'═'*60}")
    print(f"  Multi-file Pipeline: {len(source_paths)} files, "
          f"{max_concurrent_files} concurrent")
    print(f"{'═'*60}\n")

    results: dict[str, dict] = {}
    errors:  dict[str, str]  = {}
    t0 = time.time()

    def _run_one(path: str) -> tuple[str, dict]:
        result = run_pipeline(path, output_dir)
        return path, result

    with ThreadPoolExecutor(max_workers=max_concurrent_files) as executor:
        futures = {executor.submit(_run_one, p): p for p in source_paths}
        for future in as_completed(futures):
            path = futures[future]
            try:
                _, result = future.result()
                results[path] = result
                print(f"  ✓ Completed: {Path(path).name}")
            except Exception as e:
                errors[path] = str(e)
                print(f"  ✗ Failed: {Path(path).name} — {e}")

    elapsed = time.time() - t0
    print(f"\nAll {len(source_paths)} files processed in {elapsed:.1f}s "
          f"({len(results)} OK, {len(errors)} failed)\n")

    if errors:
        print("Failed files:")
        for p, e in errors.items():
            print(f"  ✗ {Path(p).name}: {e}")

    return results
