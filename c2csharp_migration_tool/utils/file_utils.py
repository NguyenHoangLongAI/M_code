"""
utils/file_utils.py
Helpers for reading source files and writing outputs.
"""

import os
import csv
import json
from pathlib import Path
from config import OUTPUT_DIR, PATTERN_CSV, CSHARP_FILE_SUFFIX, CSV_COLUMNS


# ── Source reader ──────────────────────────────────────────────
def read_source_file(path: str) -> tuple[str, str]:
    """
    Read a C / Pro*C source file.
    Returns (filename, content).
    Tries common encodings gracefully.
    """
    fpath = Path(path)
    if not fpath.exists():
        raise FileNotFoundError(f"Source file not found: {path}")

    for enc in ("utf-8", "shift_jis", "cp932", "latin-1", "utf-8-sig"):
        try:
            content = fpath.read_text(encoding=enc)
            print(f"  [IO] Read '{fpath.name}' with encoding={enc} ({len(content)} chars)")
            return fpath.name, content
        except (UnicodeDecodeError, LookupError):
            continue

    raise ValueError(f"Cannot decode '{path}' with any known encoding.")


# ── Output directory ───────────────────────────────────────────
def ensure_output_dir(base_dir: str = OUTPUT_DIR) -> Path:
    out = Path(base_dir)
    out.mkdir(parents=True, exist_ok=True)
    return out


# ── CSV writer ─────────────────────────────────────────────────
def write_patterns_csv(rows: list[dict], output_path: str) -> None:
    """
    Write the pattern analysis CSV.
    Each row must contain all keys from CSV_COLUMNS.
    """
    Path(output_path).parent.mkdir(parents=True, exist_ok=True)

    with open(output_path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=CSV_COLUMNS,
            extrasaction="ignore",
            quoting=csv.QUOTE_ALL,
        )
        writer.writeheader()
        for row in rows:
            # Normalise multiline strings for CSV
            clean = {}
            for k, v in row.items():
                if isinstance(v, str):
                    # Replace internal newlines with ↵ for readability
                    clean[k] = v.replace("\r\n", "\n").replace("\r", "\n")
                elif isinstance(v, list):
                    clean[k] = "; ".join(str(x) for x in v)
                else:
                    clean[k] = str(v) if v is not None else ""
            writer.writerow(clean)

    print(f"  [CSV] Written {len(rows)} patterns → {output_path}")


# ── C# source writer ───────────────────────────────────────────
def write_csharp_file(content: str, original_name: str, output_dir: str = OUTPUT_DIR) -> str:
    """Write migrated C# source. Returns the output path."""
    out_dir = ensure_output_dir(output_dir)
    stem = Path(original_name).stem
    out_path = out_dir / f"{stem}{CSHARP_FILE_SUFFIX}"
    out_path.write_text(content, encoding="utf-8")
    print(f"  [C#]  Written migrated file → {out_path}")
    return str(out_path)


# ── JSON debug dumps ───────────────────────────────────────────
def save_json_debug(data: object, name: str, output_dir: str = OUTPUT_DIR) -> None:
    """Save intermediate JSON for debugging."""
    out = ensure_output_dir(output_dir) / f"_debug_{name}.json"
    out.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")