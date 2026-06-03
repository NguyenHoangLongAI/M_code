"""
pipeline.py — Migration Pipeline Orchestrator v5

Agent responsibilities:
  Agent 1 — Extract all patterns incl. comments (Gemini)
  Agent 2 — Classify each pattern (Gemini, batched)
  Agent 3 — Translate each pattern → csharp_snippet + analysis (Gemini, batched)
  Agent 4 — Generate full .cs  using source_code + compact migration map (Gemini, 1 call)
  Agent 5 — Build CSV from full migration_data (pure Python, no LLM)

v5 change: Agent 4 receives both source_code and migration_data.
           Agent builds a compact pipe-delimited map internally — no raw JSON dump.
"""

import time
from pathlib import Path

from utils.file_utils import (
    read_source_file, ensure_output_dir,
    write_patterns_csv, write_csharp_file, save_json_debug,
)
from agents.extractor_agent        import extract_patterns
from agents.classifier_agent       import classify_patterns
from agents.translator_agent       import translate_patterns
from agents.csharp_generator_agent import generate_csharp_file
from agents.report_builder_agent   import build_csv_rows, print_summary
from config import OUTPUT_DIR


def run_pipeline(source_path: str, output_dir: str = OUTPUT_DIR) -> dict:
    t0 = time.time()
    print(f"\n{'═'*60}")
    print(f"  C → C# Migration Pipeline  [Gemini v5]")
    print(f"  Source : {source_path}")
    print(f"  Output : {output_dir}")
    print(f"{'═'*60}\n")

    # ── Step 0: Read source ────────────────────────────────────
    print("[Step 0] Reading source file ...")
    filename, source_code = read_source_file(source_path)
    ensure_output_dir(output_dir)

    # ── Step 1: Extract patterns ───────────────────────────────
    print("\n[Step 1] Pattern Extraction ...")
    extracted = extract_patterns(filename, source_code)
    save_json_debug(extracted, "1_extracted", output_dir)
    if not extracted:
        print("  WARNING: No patterns extracted. Check API key or source file.")

    # ── Step 2: Classify patterns ──────────────────────────────
    print("\n[Step 2] Pattern Classification ...")
    classified = classify_patterns(extracted)
    save_json_debug(classified, "2_classified", output_dir)

    # ── Step 3: Translate patterns → csharp_snippet + analysis ─
    print("\n[Step 3] C# Translation ...")
    migration_data = translate_patterns(classified, source_code)
    save_json_debug(migration_data, "3_translated", output_dir)

    # ── Step 4: Generate C# file ────────────────────────────────
    # Agent 4 receives source_code + migration_data.
    # Internally builds a compact migration map (not raw JSON).
    print("\n[Step 4] C# File Generation ...")
    csharp_source = generate_csharp_file(filename, source_code, migration_data)
    cs_path = write_csharp_file(csharp_source, filename, output_dir)

    # ── Step 5: CSV report ─────────────────────────────────────
    print("\n[Step 5] Building Pattern Report ...")
    csv_rows = build_csv_rows(migration_data)
    stem     = Path(filename).stem
    csv_path = str(Path(output_dir) / f"{stem}_patterns.csv")
    write_patterns_csv(csv_rows, csv_path)

    print_summary(csv_rows)
    elapsed = time.time() - t0
    print(f"Pipeline complete in {elapsed:.1f}s")
    print(f"  → C# file : {cs_path}")
    print(f"  → CSV     : {csv_path}\n")

    return {
        "csv_path": csv_path,
        "cs_path":  cs_path,
        "patterns": migration_data,
    }
