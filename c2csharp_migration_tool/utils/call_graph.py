"""
utils/call_graph.py — Cross-file Call Graph Analyzer
=====================================================
Giai đoạn 3: Phân tích caller/callee cross-file với LLM tool calling.

Luồng:
  1. build_function_index()  — regex scan toàn bộ tests/, tạo index LOCAL (không gọi API)
  2. extract_callees_local()  — regex tìm callee trong từng pattern (không gọi API)
  3. resolve_cross_file()     — LLM nhận ambiguous calls, chủ động gọi tool để tra cứu
  4. attach_to_patterns()     — gắn caller/callee vào từng pattern dict

LLM chỉ được cung cấp:
  - Danh sách ambiguous function names (không rõ từ file nào)
  - Tool: lookup_function(name)   → trả info từ index
  - Tool: lookup_callers(name)    → trả list files/lines gọi func này
  Không bao giờ nhận raw source code.
"""

from __future__ import annotations

import re
import json
import time
from pathlib import Path
from typing import Optional
from dataclasses import dataclass, field, asdict


# ══════════════════════════════════════════════════════════════════
# DATA MODELS
# ══════════════════════════════════════════════════════════════════

@dataclass
class FunctionEntry:
    """Một function đã được index từ source."""
    name:       str
    file:       str          # relative path từ tests/
    line_start: int
    line_end:   int          # 0 nếu chưa biết (prototype)
    kind:       str          # "definition" | "prototype" | "extern"
    signature:  str          # "int SjlGetQueue(int iMC, QUEUE *pQueue)"


@dataclass
class CallRef:
    """Một reference từ pattern này tới function khác."""
    name:       str
    kind:       str          # "stdlib" | "local" | "extern" | "unknown"
    file:       Optional[str]       # None nếu stdlib hoặc chưa resolve
    line:       Optional[int]
    pattern_id: Optional[int]       # pattern_id trong file đó


@dataclass
class PatternDependency:
    """Dependency info gắn vào một pattern."""
    source_file:  str
    callees:      list[CallRef] = field(default_factory=list)
    callers:      list[CallRef] = field(default_factory=list)


# ══════════════════════════════════════════════════════════════════
# STDLIB FUNCTION LIST (không cần resolve)
# ══════════════════════════════════════════════════════════════════

STDLIB_FUNCTIONS: set[str] = {
    # stdio
    "printf", "fprintf", "sprintf", "snprintf", "scanf", "fscanf", "sscanf",
    "fopen", "fclose", "fread", "fwrite", "fgets", "fputs", "feof", "rewind",
    "fseek", "ftell", "fflush", "perror", "puts", "gets",
    # string
    "strcpy", "strncpy", "strcat", "strncat", "strcmp", "strncmp",
    "strlen", "strstr", "strchr", "strrchr", "strtok", "strdup",
    # memory
    "malloc", "calloc", "realloc", "free", "memcpy", "memmove",
    "memset", "memcmp", "sizeof",
    # stdlib
    "atoi", "atof", "atol", "strtol", "strtod", "exit", "abort",
    "getenv", "system", "rand", "srand", "abs", "labs",
    # math
    "sqrt", "pow", "fabs", "ceil", "floor", "log", "exp",
    # time
    "time", "difftime", "mktime", "localtime", "gmtime", "strftime",
    # assert
    "assert",
    # C keywords / operators that look like calls
    "if", "while", "for", "switch", "return",
}

# Regex để tìm function definitions và prototypes
_RE_FUNC_DEF = re.compile(
    r'^(?:(?:extern|static|inline|const|unsigned|signed)\s+)*'
    r'(?:(?:unsigned|signed)\s+)?'
    r'(?:void|int|char|long|short|float|double|BOOL|UINT|ULONG|BYTE|\w+)\s*\*?\s*'
    r'(\w+)\s*\(([^)]*)\)\s*\{',
    re.MULTILINE,
)

_RE_FUNC_PROTO = re.compile(
    r'^(?:(?:extern|static|inline|const|unsigned|signed)\s+)*'
    r'(?:(?:unsigned|signed)\s+)?'
    r'(?:void|int|char|long|short|float|double|BOOL|UINT|ULONG|BYTE|\w+)\s*\*?\s*'
    r'(\w+)\s*\(([^)]*)\)\s*;',
    re.MULTILINE,
)

# Regex tìm function calls trong một snippet
_RE_CALL = re.compile(r'\b([A-Za-z_]\w+)\s*\(')


# ══════════════════════════════════════════════════════════════════
# STEP 1 — BUILD FUNCTION INDEX (local, no API)
# ══════════════════════════════════════════════════════════════════

class FunctionIndex:
    """
    Index toàn bộ function definitions/prototypes trong tests/.
    Cũng build reverse map: callers_of[func_name] = list of (file, line, context).
    """

    def __init__(self):
        self._by_name:    dict[str, list[FunctionEntry]] = {}  # name → entries (có thể nhiều file)
        self._by_file:    dict[str, list[FunctionEntry]] = {}  # file → entries
        self._callers_of: dict[str, list[dict]]          = {}  # func_name → [{file, line, caller_func}]

    # ── Build ────────────────────────────────────────────────────

    def build(self, tests_dir: Path) -> "FunctionIndex":
        """Scan tất cả .c/.pc/.h files trong tests_dir."""
        print("  [CallGraph] Building function index ...")
        c_files = (
            list(tests_dir.glob("*.c")) +
            list(tests_dir.glob("*.pc")) +
            list(tests_dir.glob("*.h")) +
            list(tests_dir.glob("*.pro"))
        )
        for fpath in sorted(c_files):
            self._index_file(fpath)

        total = sum(len(v) for v in self._by_name.values())
        print(f"  [CallGraph] Indexed {total} functions across {len(c_files)} files.")
        self._build_caller_map(c_files)
        return self

    def _read(self, fpath: Path) -> str:
        for enc in ("utf-8", "shift_jis", "cp932", "latin-1"):
            try:
                return fpath.read_text(encoding=enc)
            except (UnicodeDecodeError, LookupError):
                continue
        return ""

    def _index_file(self, fpath: Path):
        source = self._read(fpath)
        if not source:
            return
        lines = source.splitlines()
        rel = fpath.name

        def line_of(pos: int) -> int:
            return source[:pos].count("\n") + 1

        # Definitions
        for m in _RE_FUNC_DEF.finditer(source):
            name = m.group(1)
            if name in ("if", "while", "for", "switch"):
                continue
            ln = line_of(m.start())
            sig = m.group(0).rstrip("{").strip()
            entry = FunctionEntry(
                name=name, file=rel, line_start=ln,
                line_end=0, kind="definition", signature=sig[:120],
            )
            self._by_name.setdefault(name, []).append(entry)
            self._by_file.setdefault(rel, []).append(entry)

        # Prototypes (only if no definition found yet)
        for m in _RE_FUNC_PROTO.finditer(source):
            name = m.group(1)
            if name in ("if", "while", "for", "switch"):
                continue
            # Skip if already have a definition in this file
            existing = [e for e in self._by_name.get(name, []) if e.file == rel and e.kind == "definition"]
            if existing:
                continue
            ln = line_of(m.start())
            sig = m.group(0).rstrip(";").strip()
            entry = FunctionEntry(
                name=name, file=rel, line_start=ln,
                line_end=ln, kind="prototype", signature=sig[:120],
            )
            self._by_name.setdefault(name, []).append(entry)
            self._by_file.setdefault(rel, []).append(entry)

    def _build_caller_map(self, c_files: list[Path]):
        """Build reverse map: for each function call site, record who calls it."""
        print("  [CallGraph] Building caller map ...")
        for fpath in c_files:
            source = self._read(fpath)
            if not source:
                continue
            rel = fpath.name
            lines = source.splitlines()

            # Find all call sites
            for m in _RE_CALL.finditer(source):
                callee_name = m.group(1)
                if callee_name in STDLIB_FUNCTIONS:
                    continue
                if callee_name not in self._by_name:
                    continue  # unknown external

                line_no = source[:m.start()].count("\n") + 1

                # Find enclosing function name
                caller_func = self._find_enclosing_function(source, m.start(), rel)

                self._callers_of.setdefault(callee_name, []).append({
                    "file":        rel,
                    "line":        line_no,
                    "caller_func": caller_func,
                })

        total_refs = sum(len(v) for v in self._callers_of.values())
        print(f"  [CallGraph] Caller map: {total_refs} call references tracked.")

    def _find_enclosing_function(self, source: str, pos: int, file: str) -> Optional[str]:
        """Find which function contains the given position."""
        entries = self._by_file.get(file, [])
        # Find the definition whose line_start is closest before pos
        before = [e for e in entries if e.kind == "definition"
                  and source[:pos].count("\n") + 1 >= e.line_start]
        if not before:
            return None
        return max(before, key=lambda e: e.line_start).name

    # ── Query API (used as LLM tools) ────────────────────────────

    def lookup_function(self, name: str) -> dict:
        """
        Tool: lookup_function(name)
        Returns info about a function from the index.
        """
        entries = self._by_name.get(name, [])
        if not entries:
            return {"found": False, "name": name}
        return {
            "found":   True,
            "name":    name,
            "entries": [
                {
                    "file":      e.file,
                    "line":      e.line_start,
                    "kind":      e.kind,
                    "signature": e.signature,
                }
                for e in entries
            ],
        }

    def lookup_callers(self, name: str) -> dict:
        """
        Tool: lookup_callers(name)
        Returns list of all call sites for a function.
        """
        refs = self._callers_of.get(name, [])
        return {
            "name":    name,
            "callers": refs,
            "count":   len(refs),
        }

    def all_local_names(self) -> set[str]:
        """All indexed function names (local/project functions)."""
        return set(self._by_name.keys())

    def to_summary(self) -> str:
        """Compact summary for LLM system prompt."""
        lines = [f"Function index: {sum(len(v) for v in self._by_name.values())} entries across files."]
        for fname, entries in sorted(self._by_name.items()):
            files = ", ".join(f"{e.file}:L{e.line_start}" for e in entries)
            lines.append(f"  {fname:<30} → {files}")
        return "\n".join(lines[:80])  # cap at 80 lines to keep prompt small


# ══════════════════════════════════════════════════════════════════
# STEP 2 — EXTRACT CALLEES LOCAL (no API)
# ══════════════════════════════════════════════════════════════════

def extract_callees_local(pattern: dict, index: FunctionIndex) -> list[CallRef]:
    """
    Regex-extract all function calls from pattern's source_snippet.
    Classify immediately if possible:
      - in STDLIB_FUNCTIONS → kind=stdlib
      - in index            → kind=local, file known
      - otherwise           → kind=unknown (needs LLM resolution)
    """
    snippet = pattern.get("source_snippet", "")
    if not snippet:
        return []

    seen: set[str] = set()
    results: list[CallRef] = []

    for m in _RE_CALL.finditer(snippet):
        name = m.group(1)
        if name in seen:
            continue
        seen.add(name)

        if name in STDLIB_FUNCTIONS:
            results.append(CallRef(name=name, kind="stdlib", file=None, line=None, pattern_id=None))
        elif name in index.all_local_names():
            entries = index.lookup_function(name)["entries"]
            # If only one definition → resolve immediately
            defs = [e for e in entries if e["kind"] == "definition"]
            if len(defs) == 1:
                results.append(CallRef(
                    name=name, kind="local",
                    file=defs[0]["file"], line=defs[0]["line"],
                    pattern_id=None,
                ))
            else:
                # Multiple definitions across files → ambiguous
                results.append(CallRef(name=name, kind="unknown", file=None, line=None, pattern_id=None))
        else:
            results.append(CallRef(name=name, kind="unknown", file=None, line=None, pattern_id=None))

    return results


# ══════════════════════════════════════════════════════════════════
# STEP 3 — LLM RESOLVER with TOOL CALLING
# ══════════════════════════════════════════════════════════════════

# Tool definitions for Bedrock converse API
TOOL_DEFINITIONS = [
    {
        "toolSpec": {
            "name": "lookup_function",
            "description": (
                "Look up a C function by name in the project's function index. "
                "Returns which file(s) define it, line numbers, and signature. "
                "Use this when you need to resolve which file a called function belongs to."
            ),
            "inputSchema": {
                "json": {
                    "type": "object",
                    "properties": {
                        "name": {
                            "type": "string",
                            "description": "Exact C function name to look up",
                        }
                    },
                    "required": ["name"],
                }
            },
        }
    },
    {
        "toolSpec": {
            "name": "lookup_callers",
            "description": (
                "Find all call sites for a given function name — which files and lines call it. "
                "Use this to determine the callers of a function."
            ),
            "inputSchema": {
                "json": {
                    "type": "object",
                    "properties": {
                        "name": {
                            "type": "string",
                            "description": "Exact C function name to find callers for",
                        }
                    },
                    "required": ["name"],
                }
            },
        }
    },
]

_RESOLVER_SYSTEM = """
You are a C codebase dependency resolver.
You will be given a list of C function names that appear in code snippets
and need to be resolved — which file defines them and who calls them.

You have two tools:
  - lookup_function(name)  → find where a function is defined
  - lookup_callers(name)   → find all call sites for a function

Rules:
1. Use tools ONLY for functions you actually need to resolve.
2. Do NOT call a tool for the same function twice.
3. After gathering all needed info, return a JSON array:
   [
     {
       "name": "FunctionName",
       "resolved_file": "filename.c or null if truly external",
       "resolved_line": 123,
       "kind": "local | extern | unknown",
       "callers": [{"file":"X.c","line":45,"caller_func":"Y"}]
     },
     ...
   ]
4. Return ONLY the JSON array after all tool calls are done. No prose.
"""


def resolve_with_llm(
    ambiguous_names: list[str],
    index: FunctionIndex,
    pattern_context: str = "",
) -> dict[str, dict]:
    """
    Use LLM with tool calling to resolve ambiguous function references.
    Returns dict: name → resolved info.

    LLM gets only function names + tools.
    Never receives raw source code.
    """
    if not ambiguous_names:
        return {}

    from utils.key_pool import get_aws_config
    from utils.api_client import _parse_bedrock_key, _get_client
    from config import BEDROCK_MODEL_ID

    cfg      = get_aws_config()
    model_id = cfg.get("model_id") or BEDROCK_MODEL_ID
    region   = cfg.get("region_name", "ap-southeast-2")
    api_key  = cfg.get("bedrock_api_key", "")

    if not api_key:
        raise RuntimeError("BEDROCK_API_KEY not configured.")

    creds  = _parse_bedrock_key(api_key)
    client = _get_client(region, creds)

    user_msg = (
        f"Resolve these C function references:\n"
        f"{json.dumps(ambiguous_names, ensure_ascii=False)}\n\n"
        f"Context: {pattern_context[:300] if pattern_context else '(none)'}\n\n"
        "Use the tools as needed, then return the JSON array."
    )

    messages = [{"role": "user", "content": [{"text": user_msg}]}]
    resolved: dict[str, dict] = {}

    print(f"  [CallGraph/LLM] Resolving {len(ambiguous_names)} ambiguous functions ...")

    # Agentic loop — LLM may call tools multiple times
    for iteration in range(10):  # hard cap
        resp = client.converse(
            modelId=model_id,
            system=[{"text": _RESOLVER_SYSTEM}],
            messages=messages,
            inferenceConfig={"maxTokens": 4096, "temperature": 0.0},
            toolConfig={"tools": TOOL_DEFINITIONS},
        )

        content_blocks = resp.get("output", {}).get("message", {}).get("content", [])
        stop_reason    = resp.get("stopReason", "")

        # Append assistant turn to history
        messages.append({"role": "assistant", "content": content_blocks})

        # ── Check if LLM wants to use tools ──────────────────────
        tool_use_blocks = [b for b in content_blocks if b.get("toolUse")]

        if tool_use_blocks:
            tool_results = []
            for block in tool_use_blocks:
                tu        = block["toolUse"]
                tool_name = tu["name"]
                tool_id   = tu["toolUseId"]
                tool_in   = tu.get("input", {})
                func_name = tool_in.get("name", "")

                print(f"  [CallGraph/LLM] Tool call: {tool_name}({func_name!r})")

                if tool_name == "lookup_function":
                    result = index.lookup_function(func_name)
                elif tool_name == "lookup_callers":
                    result = index.lookup_callers(func_name)
                else:
                    result = {"error": f"Unknown tool: {tool_name}"}

                tool_results.append({
                    "toolResult": {
                        "toolUseId": tool_id,
                        "content":   [{"text": json.dumps(result, ensure_ascii=False)}],
                    }
                })

            # Feed tool results back as user turn
            messages.append({"role": "user", "content": tool_results})
            continue  # next iteration — LLM processes results

        # ── No more tool calls — extract final JSON ───────────────
        text_blocks = [b.get("text", "") for b in content_blocks if b.get("text")]
        final_text  = "\n".join(text_blocks).strip()

        # Parse JSON array from response
        try:
            # Strip markdown fences if present
            clean = re.sub(r"^```(?:json)?\s*", "", final_text, flags=re.I)
            clean = re.sub(r"\s*```\s*$", "", clean)
            data  = json.loads(clean.strip())
            if isinstance(data, list):
                for item in data:
                    if isinstance(item, dict) and "name" in item:
                        resolved[item["name"]] = item
        except (json.JSONDecodeError, ValueError) as e:
            print(f"  [CallGraph/LLM] WARNING: Could not parse response: {e}")

        break  # done

    print(f"  [CallGraph/LLM] Resolved {len(resolved)}/{len(ambiguous_names)} functions.")
    return resolved


# ══════════════════════════════════════════════════════════════════
# STEP 4 — ATTACH TO PATTERNS
# ══════════════════════════════════════════════════════════════════

def analyze_dependencies(
    all_file_patterns: dict[str, list[dict]],
    tests_dir: Path,
) -> dict[str, list[dict]]:
    """
    Main entry point. Run after all files have been extracted.

    Args:
        all_file_patterns: { filename: [pattern_dict, ...] }
        tests_dir:         Path to tests/ directory

    Returns:
        Same structure with dependency fields added to each pattern:
          pattern["source_file"]  = "SjlComFunc.c"
          pattern["callees"]      = [{"name":..., "kind":..., "file":..., "line":...}, ...]
          pattern["callers"]      = [{"name":..., "file":..., "line":..., "caller_func":...}, ...]
    """
    print("\n[CallGraph] Starting cross-file dependency analysis ...")

    # ── Step 1: Build index ───────────────────────────────────────
    index = FunctionIndex().build(tests_dir)

    # ── Step 2: Attach source_file + extract callees locally ─────
    ambiguous_per_file: dict[str, set[str]] = {}  # file → ambiguous names

    for filename, patterns in all_file_patterns.items():
        for p in patterns:
            p["source_file"] = filename

            callees = extract_callees_local(p, index)
            p["callees"] = [asdict(c) for c in callees]

            # Collect unknown names for LLM resolution
            unknowns = {c.name for c in callees if c.kind == "unknown"}
            if unknowns:
                ambiguous_per_file.setdefault(filename, set()).update(unknowns)

    # ── Step 3: LLM resolves ambiguous calls (one batch per file) ─
    llm_resolved: dict[str, dict] = {}

    for filename, ambiguous_names in ambiguous_per_file.items():
        names_list = sorted(ambiguous_names)
        print(f"  [CallGraph] {filename}: {len(names_list)} ambiguous calls → LLM")
        resolved = resolve_with_llm(
            names_list,
            index,
            pattern_context=f"File: {filename}",
        )
        llm_resolved.update(resolved)

    # ── Step 3b: Apply LLM resolutions back to callees ───────────
    for filename, patterns in all_file_patterns.items():
        for p in patterns:
            updated_callees = []
            for c in p.get("callees", []):
                if c["kind"] == "unknown" and c["name"] in llm_resolved:
                    r = llm_resolved[c["name"]]
                    c["kind"] = r.get("kind", "extern")
                    c["file"] = r.get("resolved_file")
                    c["line"] = r.get("resolved_line")
                updated_callees.append(c)
            p["callees"] = updated_callees

    # ── Step 4: Attach callers using index ────────────────────────
    # Build pattern_id lookup: (file, func_name) → pattern_id
    func_to_pattern: dict[tuple[str, str], int] = {}
    for filename, patterns in all_file_patterns.items():
        for p in patterns:
            if p.get("raw_type") in ("func_def", "func_prototype", "func_definition"):
                # Try to extract function name from snippet
                m = _RE_FUNC_DEF.search(p.get("source_snippet", ""))
                if m:
                    func_to_pattern[(filename, m.group(1))] = p["id"]

    for filename, patterns in all_file_patterns.items():
        for p in patterns:
            if p.get("raw_type") not in ("func_def", "func_definition"):
                continue
            m = _RE_FUNC_DEF.search(p.get("source_snippet", ""))
            if not m:
                continue
            func_name = m.group(1)

            caller_refs = index.lookup_callers(func_name).get("callers", [])
            callers_out = []
            for ref in caller_refs:
                pid = func_to_pattern.get((ref["file"], ref.get("caller_func", "")))
                callers_out.append({
                    "name":        func_name,
                    "file":        ref["file"],
                    "line":        ref["line"],
                    "caller_func": ref.get("caller_func"),
                    "pattern_id":  pid,
                })
            p["callers"] = callers_out

    print("[CallGraph] Dependency analysis complete.\n")
    return all_file_patterns
