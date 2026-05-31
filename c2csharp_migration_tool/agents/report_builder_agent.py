"""
agents/report_builder_agent.py
AGENT 5 – CSV Report Builder  (pure Python, no LLM call)

Merges all intermediate data into the final CSV row format.
"""

from config import CSV_COLUMNS


def build_csv_rows(migration_data: list[dict]) -> list[dict]:
    """
    Convert the enriched pattern list into CSV-ready dicts.

    Column mapping:
      No                   ← id
      Pattern_C_ProC       ← source_snippet
      Pattern_Type         ← pattern_type
      Pattern_SubType      ← sub_types  (joined)
      Pattern_Group        ← pattern_group (joined)
      Summary              ← summary_vi
      Pattern_CSharp       ← csharp_snippet
      Difficulty           ← difficulty
      Migration_Strategy   ← migration_strategy
      Risk_Level           ← risk_level
      Risk_Strategy        ← risk_strategy
      CSharp_Popularity    ← "{csharp_popularity}/5"
    """
    rows = []
    for p in migration_data:
        sub_types  = p.get("sub_types", [])
        pat_group  = p.get("pattern_group", [])
        popularity = p.get("csharp_popularity", 3)

        row = {
            "No":                 str(p.get("id", "")),
            "Pattern_C_ProC":     p.get("source_snippet", ""),
            "Pattern_Type":       p.get("pattern_type", ""),
            "Pattern_SubType":    "; ".join(sub_types) if isinstance(sub_types, list) else str(sub_types),
            "Pattern_Group":      "; ".join(pat_group) if isinstance(pat_group, list) else str(pat_group),
            "Summary":            p.get("summary_vi", ""),
            "Pattern_CSharp":     p.get("csharp_snippet", ""),
            "Difficulty":         p.get("difficulty", ""),
            "Migration_Strategy": p.get("migration_strategy", ""),
            "Risk_Level":         p.get("risk_level", ""),
            "Risk_Strategy":      p.get("risk_strategy", ""),
            "CSharp_Popularity":  f"{popularity}/5",
        }
        rows.append(row)

    return rows


def print_summary(rows: list[dict]) -> None:
    """Print a terminal summary table."""
    print("\n" + "═" * 90)
    print(f"{'No':>4}  {'Type':<18} {'Difficulty':<12} {'Risk':<12} {'Popularity':>10}")
    print("─" * 90)
    for r in rows:
        print(
            f"{r['No']:>4}  "
            f"{r['Pattern_Type']:<18} "
            f"{r['Difficulty']:<12} "
            f"{r['Risk_Level']:<12} "
            f"{r['CSharp_Popularity']:>10}"
        )
    print("═" * 90)
    print(f"Total patterns: {len(rows)}\n")