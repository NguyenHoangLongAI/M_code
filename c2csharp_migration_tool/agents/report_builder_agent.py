"""
agents/report_builder_agent.py
AGENT 5 – CSV Report Builder  (pure Python, no LLM call)

Merges all intermediate data into the final CSV row format.
"""

from config import CSV_COLUMNS


def build_csv_rows(migration_data: list[dict]) -> list[dict]:
    """
    Convert the enriched pattern list into CSV-ready dicts.

    Column mapping (Japanese):
      番号               ← id
      C_ProCパターン     ← source_snippet
      パターン種別       ← pattern_type
      サブタイプ         ← sub_types  (joined)
      パターングループ   ← pattern_group (joined)
      概要               ← summary_vi
      C#パターン         ← csharp_snippet
      難易度             ← difficulty
      移行戦略           ← migration_strategy
      リスクレベル       ← risk_level
      リスク戦略         ← risk_strategy
      C#普及度           ← "{csharp_popularity}/5"
    """
    rows = []
    for p in migration_data:
        sub_types  = p.get("sub_types", [])
        pat_group  = p.get("pattern_group", [])
        popularity = p.get("csharp_popularity", 3)

        row = {
            "番号":             str(p.get("id", "")),
            "C_ProCパターン":   p.get("source_snippet", ""),
            "パターン種別":     p.get("pattern_type", ""),
            "サブタイプ":       "; ".join(sub_types) if isinstance(sub_types, list) else str(sub_types),
            "パターングループ": "; ".join(pat_group) if isinstance(pat_group, list) else str(pat_group),
            "概要":             p.get("summary_vi", ""),
            "C#パターン":       p.get("csharp_snippet", ""),
            "難易度":           p.get("difficulty", ""),
            "移行戦略":         p.get("migration_strategy", ""),
            "リスクレベル":     p.get("risk_level", ""),
            "リスク戦略":       p.get("risk_strategy", ""),
            "C#普及度":         f"{popularity}/5",
        }
        rows.append(row)

    return rows


def print_summary(rows: list[dict]) -> None:
    """Print a terminal summary table."""
    print("\n" + "═" * 90)
    print(f"{'番号':>4}  {'種別':<18} {'難易度':<14} {'リスク':<14} {'普及度':>8}")
    print("─" * 90)
    for r in rows:
        print(
            f"{r['番号']:>4}  "
            f"{r['パターン種別']:<18} "
            f"{r['難易度']:<14} "
            f"{r['リスクレベル']:<14} "
            f"{r['C#普及度']:>8}"
        )
    print("═" * 90)
    print(f"パターン合計: {len(rows)}\n")