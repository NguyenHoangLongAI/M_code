"""
agents/csharp_generator_agent.py
AGENT 4 – C# File Generator

Produces a complete, compilable C# source file from the original C/Pro*C
source and the migration data collected by Agents 1-3.
"""

import json
from utils.api_client import call_claude
from prompts.agent_prompts import CSHARP_GEN_SYSTEM, CSHARP_GEN_USER


def generate_csharp_file(
    filename: str,
    source_code: str,
    migration_data: list[dict],
) -> str:
    """
    Ask Claude to produce a full C# source file.
    Returns the C# source as a string.
    """
    print("  [Agent4] Generating C# source file …")

    migration_json = json.dumps(migration_data, ensure_ascii=False, indent=2)

    user_prompt = CSHARP_GEN_USER.format(
        filename=filename,
        source_code=source_code,
        migration_json=migration_json,
    )

    csharp_source = call_claude(
        CSHARP_GEN_SYSTEM,
        user_prompt,
        max_tokens=8192,
    )

    # Strip any accidental markdown fences
    csharp_source = _strip_fences(csharp_source)

    print(f"  [Agent4] C# file generated ({len(csharp_source)} chars).")
    return csharp_source


def _strip_fences(text: str) -> str:
    import re
    text = text.strip()
    # Remove ```csharp or ```cs or ``` fences
    text = re.sub(r"^```(?:csharp|cs)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    return text.strip()