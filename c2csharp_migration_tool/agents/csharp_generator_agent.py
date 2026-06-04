"""
agents/csharp_generator_agent.py — AGENT 4  v5
===============================================
Receives:
  - filename       : original source filename
  - source_code    : full C/Pro*C source text
  - migration_data : enriched pattern list from Agents 1-3

Builds a COMPACT MIGRATION MAP from migration_data (not the raw JSON dump)
and passes it alongside the source to Gemini for faithful C# generation.

Compact map format (one row per pattern, pipe-delimited):
  LINE | KIND        | C SOURCE (truncated)          | C# EQUIVALENT
  ─────┼─────────────┼───────────────────────────────┼──────────────────────
  22   | rcs_tag     | static char RcsTag[]={...}    | private static readon…
  24   | pragma_dir  | #pragma PACK 4                | [StructLayout(Pack=4)]
  ...

This is ~60% fewer tokens than raw JSON while giving Agent 4 all it needs.
"""

import re
from utils.api_client import call_claude
from prompts.agent_prompts import CSHARP_GEN_SYSTEM, CSHARP_GEN_USER


# ── Max chars per cell to keep map readable ────────────────────
_SRC_MAX  = 72   # C source snippet column
_CS_MAX   = 80   # C# equivalent column
_NOTE_MAX = 60   # migration note column


def _truncate(s: str, n: int) -> str:
    """Truncate string, collapse whitespace, mark if cut."""
    s = " ".join(s.split())   # collapse newlines/tabs to single space
    return s if len(s) <= n else s[:n - 1] + "…"


def _build_migration_map(migration_data: list[dict]) -> str:
    """
    Build a compact pipe-delimited table from migration_data.
    Skips patterns where cs_equivalent matches source verbatim (pure comments).
    Groups by raw_type for readability.
    """
    if not migration_data:
        return "(no patterns)"

    lines: list[str] = []

    # Header
    lines.append(
        f"{'LINE':<6} | {'KIND':<18} | {'C SOURCE':<{_SRC_MAX}} | "
        f"{'C# EQUIVALENT':<{_CS_MAX}} | NOTE"
    )
    lines.append("─" * (6 + 3 + 18 + 3 + _SRC_MAX + 3 + _CS_MAX + 3 + 30))

    # Sort by line number for readability
    sorted_patterns = sorted(
        migration_data,
        key=lambda p: (p.get("line_range") or [0])[0]
    )

    for p in sorted_patterns:
        line_start = (p.get("line_range") or [0])[0]
        kind       = p.get("raw_type", "")[:18]
        src        = _truncate(p.get("source_snippet", ""), _SRC_MAX)
        cs         = _truncate(p.get("csharp_snippet", ""), _CS_MAX)
        note       = _truncate(p.get("migration_strategy", ""), _NOTE_MAX)
        risk       = p.get("risk_level", "")
        review     = " ⚠REVIEW" if p.get("needs_review") else ""

        # Skip trivial identical mappings (pure comment pass-throughs)
        is_comment = "comment" in kind
        if is_comment and not cs:
            cs = "(keep verbatim)"

        # Add risk indicator for high/very-high risk items
        if risk in ("Cao", "Rất cao"):
            note = f"[{risk}] " + note

        lines.append(
            f"{str(line_start):<6} | {kind:<18} | {src:<{_SRC_MAX}} | "
            f"{cs:<{_CS_MAX}} | {note}{review}"
        )

    return "\n".join(lines)


def generate_csharp_file(
    filename: str,
    source_code: str,
    migration_data: list[dict],
) -> str:
    """
    Generate complete C# file.
    Agent 4 now uses BOTH source_code AND the compact migration map.
    """
    print("  [Agent4] Building compact migration map ...")
    migration_map = _build_migration_map(migration_data)
    map_lines = migration_map.count("\n") + 1
    map_chars = len(migration_map)
    print(f"  [Agent4] Map: {map_lines} rows, {map_chars} chars "
          f"(vs ~{len(str(migration_data))} chars raw JSON)")

    print("  [Agent4] Generating C# source file ...")
    user_prompt = CSHARP_GEN_USER.format(
        filename=filename,
        source_code=source_code,
        migration_map=migration_map,
    )
    print(f"  [Agent4] Total prompt: ~{len(user_prompt)//4} tokens")

    cs = call_claude(CSHARP_GEN_SYSTEM, user_prompt, max_tokens=16000)
    cs = _strip_fences(cs)
    print(f"  [Agent4] Generated {len(cs)} chars.")
    return cs


def _strip_fences(text: str) -> str:
    text = text.strip()
    text = re.sub(r"^```(?:csharp|cs|c#)?\s*", "", text, flags=re.I)
    text = re.sub(r"\s*```\s*$", "", text)
    return text.strip()
