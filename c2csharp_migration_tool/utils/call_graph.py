"""
utils/call_graph.py — Cross-file Call Graph Analyzer  v4.0
===========================================================
v4.0 changes (on top of v3.1):
  - FUNCTION KIND CLASSIFICATION:
      Each callee/caller is now classified with a "func_kind" field:
        "call"   — regular function call (existing behaviour)
        "create" — constructor/factory/init/alloc: malloc, calloc, new*, init*,
                   create*, open*, alloc*, setup*, start*
        "system" — stdlib/OS system function (printf, memcpy, fopen, etc.)
  - _classify_func_kind(name, kind) — heuristic + stdlib set lookup
  - Pattern nodes gain two summary fields:
        "func_calls"   : list of callee names with func_kind=="call"
        "func_creates" : list of callee names with func_kind=="create"
  - These are persisted to Neo4j via neo4j_store.py (save_migration_result
    and save_call_graph_result already pick up the enriched pattern dicts)
  - resolve_with_llm: uses urllib.request (no boto3 private functions) — v3.1

Luồng:
  1. build_function_index()   — regex scan tests/ + header files từ #include
  2. extract_callees_local()  — regex tìm callee, classify func_kind
  3. resolve_with_llm()       — LLM nhận ambiguous calls + unscanned_file_list
  4. attach_to_patterns()     — gắn caller/callee + func_kind vào từng pattern dict

func_kind values:
  "call"   — ordinary function call (business logic)
  "create" — object/resource creation, allocation, initialization
  "system" — C stdlib / OS / well-known system functions
"""

from __future__ import annotations

import re
import json
import time
import urllib.request
import urllib.error
from pathlib import Path
from typing import Optional
from dataclasses import dataclass, field, asdict


# ══════════════════════════════════════════════════════════════════
# DATA MODELS
# ══════════════════════════════════════════════════════════════════

@dataclass
class FunctionEntry:
    name:       str
    file:       str
    line_start: int
    line_end:   int
    kind:       str          # "definition" | "prototype"
    signature:  str


@dataclass
class CallRef:
    name:                  str
    kind:                  str          # "stdlib" | "local" | "extern" | "unscanned" | "unknown"
    func_kind:             str          # "call" | "create" | "system"  ← NEW v4
    file:                  Optional[str]
    line:                  Optional[int]
    pattern_id:            Optional[int]
    definition_pattern_id: Optional[int] = None
    definition_file:       Optional[str] = None


@dataclass
class PatternDependency:
    source_file:  str
    callees:      list[CallRef] = field(default_factory=list)
    callers:      list[CallRef] = field(default_factory=list)


# ══════════════════════════════════════════════════════════════════
# STDLIB / SYSTEM FUNCTION SET
# ══════════════════════════════════════════════════════════════════

STDLIB_FUNCTIONS: set[str] = {
    "printf", "fprintf", "sprintf", "snprintf", "scanf", "fscanf", "sscanf",
    "fopen", "fclose", "fread", "fwrite", "fgets", "fputs", "feof", "rewind",
    "fseek", "ftell", "fflush", "perror", "puts", "gets",
    "strcpy", "strncpy", "strcat", "strncat", "strcmp", "strncmp",
    "strlen", "strstr", "strchr", "strrchr", "strtok", "strdup",
    "malloc", "calloc", "realloc", "free", "memcpy", "memmove",
    "memset", "memcmp", "sizeof",
    "atoi", "atof", "atol", "strtol", "strtod", "exit", "abort",
    "getenv", "system", "rand", "srand", "abs", "labs",
    "sqrt", "pow", "fabs", "ceil", "floor", "log", "exp",
    "time", "difftime", "mktime", "localtime", "gmtime", "strftime",
    "assert",
    "if", "while", "for", "switch", "return",
}

# Prefixes / exact names that indicate object/resource CREATION
_CREATE_PREFIXES = (
    "new", "init", "create", "alloc", "open", "setup", "start",
    "make", "build", "construct", "instantiate", "launch", "spawn",
    "register", "connect", "attach", "mount",
)
_CREATE_EXACT: set[str] = {
    "malloc", "calloc", "realloc",
    "fopen",
    "socket", "accept", "bind", "listen",
    "pthread_create", "fork",
    "dlopen",
}


def _classify_func_kind(name: str, resolved_kind: str) -> str:
    """
    Classify a callee into one of three functional roles:
      "system"  — stdlib / OS function
      "create"  — constructor / factory / alloc / init
      "call"    — ordinary business-logic call

    Priority: system > create > call
    """
    # System: stdlib set OR resolved as stdlib
    if resolved_kind == "stdlib" or name in STDLIB_FUNCTIONS:
        return "system"

    name_lower = name.lower()

    # Exact create names (includes some stdlib like malloc/fopen)
    if name in _CREATE_EXACT:
        return "create"

    # Prefix heuristic for create
    for prefix in _CREATE_PREFIXES:
        if name_lower.startswith(prefix):
            return "create"

    # Suffix heuristic: Init / Create / New / Open at END of camelCase name
    for suffix in ("init", "create", "new", "open", "alloc", "setup", "start",
                   "make", "build", "construct", "launch", "spawn"):
        if name_lower.endswith(suffix):
            return "create"

    return "call"


# ══════════════════════════════════════════════════════════════════
# REGEX helpers
# ══════════════════════════════════════════════════════════════════

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

_RE_CALL          = re.compile(r'\b([A-Za-z_]\w+)\s*\(')
_RE_INCLUDE_LOCAL = re.compile(r'#\s*include\s+"([^"]+)"', re.MULTILINE)
_SOURCE_EXTS      = {".c", ".pc", ".h", ".pro"}


def _scan_include_headers(source_files: list[Path], search_dirs: list[Path]) -> list[Path]:
    already_indexed = {f.name for f in source_files}
    found: list[Path] = []
    seen_names: set[str] = set()
    for src in source_files:
        try:
            text = src.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for m in _RE_INCLUDE_LOCAL.finditer(text):
            inc_name = Path(m.group(1)).name
            if inc_name in already_indexed or inc_name in seen_names:
                continue
            for d in search_dirs:
                candidate = d / inc_name
                if candidate.exists():
                    found.append(candidate)
                    seen_names.add(inc_name)
                    break
            else:
                seen_names.add(inc_name)
    return found


def _find_line_end(source: str, open_brace_pos: int) -> int:
    brace_start = source.find('{', open_brace_pos)
    if brace_start == -1:
        return source[:open_brace_pos].count('\n') + 1
    depth = 0
    i = brace_start
    in_str = in_char = in_lcomm = in_bcomm = False
    length = len(source)
    while i < length:
        c = source[i]
        if in_lcomm:
            if c == '\n': in_lcomm = False
            i += 1; continue
        if in_bcomm:
            if c == '*' and i + 1 < length and source[i + 1] == '/':
                in_bcomm = False; i += 2
            else: i += 1
            continue
        if in_str:
            if c == '\\': i += 2; continue
            if c == '"': in_str = False
            i += 1; continue
        if in_char:
            if c == '\\': i += 2; continue
            if c == "'": in_char = False
            i += 1; continue
        if c == '/' and i + 1 < length:
            if source[i + 1] == '/': in_lcomm = True; i += 2; continue
            if source[i + 1] == '*': in_bcomm = True; i += 2; continue
        if c == '"': in_str = True
        elif c == "'": in_char = True
        elif c == '{': depth += 1
        elif c == '}':
            depth -= 1
            if depth == 0:
                return source[:i + 1].count('\n') + 1
        i += 1
    return source.count('\n') + 1


# ══════════════════════════════════════════════════════════════════
# STEP 1 — BUILD FUNCTION INDEX
# ══════════════════════════════════════════════════════════════════

class FunctionIndex:
    def __init__(self):
        self._by_name:         dict[str, list[FunctionEntry]] = {}
        self._by_file:         dict[str, list[FunctionEntry]] = {}
        self._callers_of:      dict[str, list[dict]]          = {}
        self._unscanned_files: set[str]                       = set()
        self._include_map:     dict[str, str]                 = {}

    def build(self, tests_dir: Path, extra_search_dirs: list[Path] | None = None) -> "FunctionIndex":
        print("  [CallGraph] Building function index ...")
        search_dirs = [tests_dir] + (extra_search_dirs or [])
        c_files = sorted(
            list(tests_dir.glob("*.c")) + list(tests_dir.glob("*.pc")) +
            list(tests_dir.glob("*.h")) + list(tests_dir.glob("*.pro"))
        )
        for fpath in c_files:
            self._index_file(fpath)

        extra_headers = _scan_include_headers(c_files, search_dirs)
        already = {f.name for f in c_files}
        for hpath in extra_headers:
            if hpath.name not in already:
                print(f"  [CallGraph] Scanning included header: {hpath.name}")
                self._index_file(hpath)
                self._include_map[hpath.name] = str(hpath)
                c_files.append(hpath)
                already.add(hpath.name)

        for src in c_files:
            try:
                text = src.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            for m in _RE_INCLUDE_LOCAL.finditer(text):
                inc_name = Path(m.group(1)).name
                if inc_name not in already:
                    self._unscanned_files.add(inc_name)

        total = sum(len(v) for v in self._by_name.values())
        print(f"  [CallGraph] Indexed {total} functions across {len(c_files)} files.")
        if self._unscanned_files:
            print(f"  [CallGraph] Unscanned referenced files: {sorted(self._unscanned_files)}")
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
        rel = fpath.name
        def line_of(pos: int) -> int:
            return source[:pos].count("\n") + 1
        for m in _RE_FUNC_DEF.finditer(source):
            name = m.group(1)
            if name in ("if", "while", "for", "switch"):
                continue
            ln = line_of(m.start())
            ln_end = _find_line_end(source, m.end() - 1)
            sig = m.group(0).rstrip("{").strip()
            entry = FunctionEntry(name=name, file=rel, line_start=ln, line_end=ln_end,
                                  kind="definition", signature=sig[:120])
            self._by_name.setdefault(name, []).append(entry)
            self._by_file.setdefault(rel, []).append(entry)
        for m in _RE_FUNC_PROTO.finditer(source):
            name = m.group(1)
            if name in ("if", "while", "for", "switch"):
                continue
            existing = [e for e in self._by_name.get(name, []) if e.file == rel and e.kind == "definition"]
            if existing:
                continue
            ln = line_of(m.start())
            sig = m.group(0).rstrip(";").strip()
            entry = FunctionEntry(name=name, file=rel, line_start=ln, line_end=ln,
                                  kind="prototype", signature=sig[:120])
            self._by_name.setdefault(name, []).append(entry)
            self._by_file.setdefault(rel, []).append(entry)

    def _build_caller_map(self, c_files: list[Path]):
        print("  [CallGraph] Building caller map ...")
        for fpath in c_files:
            source = self._read(fpath)
            if not source:
                continue
            rel = fpath.name
            for m in _RE_CALL.finditer(source):
                callee_name = m.group(1)
                if callee_name in STDLIB_FUNCTIONS:
                    continue
                if callee_name not in self._by_name:
                    continue
                line_no = source[:m.start()].count("\n") + 1
                caller_func = self._find_enclosing_function(source, m.start(), rel)
                self._callers_of.setdefault(callee_name, []).append({
                    "file": rel, "line": line_no, "caller_func": caller_func,
                })
        total_refs = sum(len(v) for v in self._callers_of.values())
        print(f"  [CallGraph] Caller map: {total_refs} call references tracked.")

    def _find_enclosing_function(self, source: str, pos: int, file: str) -> Optional[str]:
        entries = self._by_file.get(file, [])
        before = [e for e in entries if e.kind == "definition"
                  and source[:pos].count("\n") + 1 >= e.line_start]
        if not before:
            return None
        return max(before, key=lambda e: e.line_start).name

    def lookup_function(self, name: str) -> dict:
        entries = self._by_name.get(name, [])
        if not entries:
            return {"found": False, "name": name}
        return {
            "found": True, "name": name,
            "entries": [
                {"file": e.file, "line_start": e.line_start, "line_end": e.line_end,
                 "kind": e.kind, "signature": e.signature}
                for e in entries
            ],
        }

    def lookup_callers(self, name: str) -> dict:
        refs = self._callers_of.get(name, [])
        return {"name": name, "callers": refs, "count": len(refs)}

    def all_local_names(self) -> set[str]:
        return set(self._by_name.keys())

    def lookup_unscanned(self) -> dict:
        return {
            "unscanned_files": sorted(self._unscanned_files),
            "count":           len(self._unscanned_files),
            "scanned_files":   sorted(self._by_file.keys()),
        }

    def to_summary(self) -> str:
        lines = [f"Function index: {sum(len(v) for v in self._by_name.values())} entries."]
        for fname, entries in sorted(self._by_name.items()):
            files = ", ".join(f"{e.file}:L{e.line_start}" for e in entries)
            lines.append(f"  {fname:<30} → {files}")
        return "\n".join(lines[:80])


# ══════════════════════════════════════════════════════════════════
# STEP 2 — EXTRACT CALLEES LOCAL  (with func_kind)
# ══════════════════════════════════════════════════════════════════

def extract_callees_local(pattern: dict, index: FunctionIndex) -> list[CallRef]:
    """
    Regex-extract all function calls from pattern's source_snippet.
    Classify each with:
      - kind      : "stdlib" | "local" | "unknown"
      - func_kind : "system" | "create" | "call"   ← NEW v4
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
            fk = _classify_func_kind(name, "stdlib")
            results.append(CallRef(name=name, kind="stdlib", func_kind=fk,
                                   file=None, line=None, pattern_id=None))
        elif name in index.all_local_names():
            entries = index.lookup_function(name)["entries"]
            defs = [e for e in entries if e["kind"] == "definition"]
            if len(defs) == 1:
                fk = _classify_func_kind(name, "local")
                results.append(CallRef(
                    name=name, kind="local", func_kind=fk,
                    file=defs[0]["file"], line=defs[0]["line_start"],
                    pattern_id=None,
                ))
            else:
                fk = _classify_func_kind(name, "unknown")
                results.append(CallRef(name=name, kind="unknown", func_kind=fk,
                                       file=None, line=None, pattern_id=None))
        else:
            fk = _classify_func_kind(name, "unknown")
            results.append(CallRef(name=name, kind="unknown", func_kind=fk,
                                   file=None, line=None, pattern_id=None))
    return results


# ══════════════════════════════════════════════════════════════════
# STEP 3 — LLM RESOLVER  (urllib.request, no boto3 private API)
# ══════════════════════════════════════════════════════════════════

TOOL_DEFINITIONS = [
    {
        "toolSpec": {
            "name": "lookup_function",
            "description": (
                "Look up a C function by name in the project's function index. "
                "Returns which file(s) define it, line numbers, and signature."
            ),
            "inputSchema": {"json": {"type": "object",
                "properties": {"name": {"type": "string"}}, "required": ["name"]}},
        }
    },
    {
        "toolSpec": {
            "name": "lookup_callers",
            "description": "Find all call sites for a given function name.",
            "inputSchema": {"json": {"type": "object",
                "properties": {"name": {"type": "string"}}, "required": ["name"]}},
        }
    },
    {
        "toolSpec": {
            "name": "list_unscanned_files",
            "description": "List all project files referenced via #include but not yet scanned.",
            "inputSchema": {"json": {"type": "object", "properties": {}, "required": []}},
        }
    },
]

_RESOLVER_SYSTEM = """
You are a C codebase dependency resolver.
Classify C function names: which file defines them, what kind they are,
AND classify their functional role as func_kind.

Tools: lookup_function(name), lookup_callers(name), list_unscanned_files()

Rules for "kind":
  - "local"     : found in index
  - "unscanned" : not in index, likely in an unscanned file
  - "extern"    : not in index, external library
  - "unknown"   : cannot determine

Rules for "func_kind":
  - "system"  : stdlib / OS function (printf, malloc, fopen, memcpy, etc.)
  - "create"  : constructor / factory / init / alloc / open
                (starts/ends with: new, init, create, alloc, open, setup, start, make, build)
  - "call"    : ordinary business-logic function call

Return JSON array ONLY:
[{"name":..., "resolved_file":..., "resolved_line_start":..., "resolved_line_end":...,
  "kind":..., "func_kind":..., "suggested_file":..., "confidence":...}]
"""


def resolve_with_llm(
    ambiguous_names: list[str],
    index: FunctionIndex,
    pattern_context: str = "",
) -> dict[str, dict]:
    if not ambiguous_names:
        return {}

    from utils.key_pool import get_aws_config
    from config import BEDROCK_MODEL_ID

    cfg      = get_aws_config()
    model_id = cfg.get("model_id") or BEDROCK_MODEL_ID
    region   = cfg.get("region_name", "ap-southeast-2")
    api_key  = cfg.get("bedrock_api_key", "")

    if not api_key:
        raise RuntimeError("BEDROCK_API_KEY not configured.")

    url = (
        f"https://bedrock-runtime.{region}.amazonaws.com"
        f"/model/{model_id}/converse"
    )

    user_msg = (
        f"Resolve these C function references:\n"
        f"{json.dumps(ambiguous_names, ensure_ascii=False)}\n\n"
        f"Context: {pattern_context[:300] if pattern_context else '(none)'}\n\n"
        "Steps: 1. lookup_function for each. "
        "2. list_unscanned_files once if needed. "
        "3. Return JSON array including func_kind for each."
    )

    messages = [{"role": "user", "content": [{"text": user_msg}]}]
    resolved: dict[str, dict] = {}

    print(f"  [CallGraph/LLM] Resolving {len(ambiguous_names)} ambiguous functions ...")

    for iteration in range(15):
        body = {
            "system": [{"text": _RESOLVER_SYSTEM}],
            "messages": messages,
            "inferenceConfig": {"maxTokens": 4096, "temperature": 0.0},
            "toolConfig": {"tools": TOOL_DEFINITIONS},
        }
        data = json.dumps(body).encode("utf-8")

        req = urllib.request.Request(url, data=data, method="POST")
        req.add_header("Content-Type", "application/json")
        req.add_header("Content-Length", str(len(data)))
        req.add_header("Authorization", f"Bearer {api_key}")

        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                result = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            body_err = e.read().decode("utf-8", errors="replace")
            print(f"  [CallGraph/LLM] HTTP error {e.code}: {body_err[:200]}")
            break
        except Exception as e:
            print(f"  [CallGraph/LLM] Request error: {e}")
            break

        content_blocks = result.get("output", {}).get("message", {}).get("content", [])
        messages.append({"role": "assistant", "content": content_blocks})

        tool_use_blocks = [b for b in content_blocks if b.get("toolUse")]

        if tool_use_blocks:
            tool_results = []
            for block in tool_use_blocks:
                tu        = block["toolUse"]
                tool_name = tu["name"]
                tool_id   = tu["toolUseId"]
                func_name = tu.get("input", {}).get("name", "")

                print(f"  [CallGraph/LLM] Tool call: {tool_name}"
                      + (f"({func_name!r})" if func_name else "()"))

                if tool_name == "lookup_function":
                    tool_result = index.lookup_function(func_name)
                elif tool_name == "lookup_callers":
                    tool_result = index.lookup_callers(func_name)
                elif tool_name == "list_unscanned_files":
                    unscanned = sorted(index._unscanned_files)
                    tool_result = {
                        "unscanned_files": unscanned,
                        "count": len(unscanned),
                        "hint": "Match function name prefix to file stem.",
                    }
                    print(f"  [CallGraph/LLM] Unscanned files: {unscanned}")
                else:
                    tool_result = {"error": f"Unknown tool: {tool_name}"}

                tool_results.append({
                    "toolResult": {
                        "toolUseId": tool_id,
                        "content": [{"text": json.dumps(tool_result, ensure_ascii=False)}],
                    }
                })
            messages.append({"role": "user", "content": tool_results})
            continue

        text_blocks = [b.get("text", "") for b in content_blocks if b.get("text")]
        final_text  = "\n".join(text_blocks).strip()
        try:
            clean = re.sub(r"^```(?:json)?\s*", "", final_text, flags=re.I)
            clean = re.sub(r"\s*```\s*$", "", clean)
            data_parsed = json.loads(clean.strip())
            if isinstance(data_parsed, list):
                for item in data_parsed:
                    if isinstance(item, dict) and "name" in item:
                        # Ensure func_kind is set even if LLM forgot it
                        if "func_kind" not in item:
                            item["func_kind"] = _classify_func_kind(
                                item["name"], item.get("kind", "unknown")
                            )
                        resolved[item["name"]] = item
        except (json.JSONDecodeError, ValueError) as e:
            print(f"  [CallGraph/LLM] WARNING: Could not parse response: {e}")
        break

    print(f"  [CallGraph/LLM] Resolved {len(resolved)}/{len(ambiguous_names)} functions.")
    return resolved


# ══════════════════════════════════════════════════════════════════
# STEP 4 — ATTACH TO PATTERNS  (with func_kind propagation)
# ══════════════════════════════════════════════════════════════════

def _attach_func_kind_summary(p: dict) -> None:
    """
    After callees are finalized, compute two summary lists on the pattern:
      p["func_calls"]   = [name, ...] where func_kind == "call"
      p["func_creates"] = [name, ...] where func_kind == "create"
    These are stored as JSON arrays in Neo4j Pattern nodes.
    """
    calls   = []
    creates = []
    for c in p.get("callees", []):
        fk = c.get("func_kind", "call")
        name = c.get("name", "")
        if not name:
            continue
        if fk == "create":
            creates.append(name)
        elif fk == "call":
            calls.append(name)
        # system functions are omitted from both lists
    p["func_calls"]   = calls
    p["func_creates"] = creates


def analyze_dependencies(
    all_file_patterns: dict[str, list[dict]],
    tests_dir: Path,
) -> dict[str, list[dict]]:
    print("\n[CallGraph] Starting cross-file dependency analysis ...")

    index = FunctionIndex().build(tests_dir, extra_search_dirs=[tests_dir.parent])

    ambiguous_per_file: dict[str, set[str]] = {}
    for filename, patterns in all_file_patterns.items():
        for p in patterns:
            p["source_file"] = filename
            callees = extract_callees_local(p, index)
            p["callees"] = [asdict(c) for c in callees]
            unknowns = {c.name for c in callees if c.kind == "unknown"}
            if unknowns:
                ambiguous_per_file.setdefault(filename, set()).update(unknowns)

    llm_resolved: dict[str, dict] = {}
    for filename, ambiguous_names in ambiguous_per_file.items():
        names_list = sorted(ambiguous_names)
        print(f"  [CallGraph] {filename}: {len(names_list)} ambiguous calls → LLM")
        resolved = resolve_with_llm(names_list, index, pattern_context=f"File: {filename}")
        llm_resolved.update(resolved)

    # Apply LLM resolutions (including func_kind from LLM)
    for filename, patterns in all_file_patterns.items():
        for p in patterns:
            updated_callees = []
            for c in p.get("callees", []):
                if c["kind"] == "unknown" and c["name"] in llm_resolved:
                    r = llm_resolved[c["name"]]
                    c["kind"]           = r.get("kind", "extern")
                    c["func_kind"]      = r.get("func_kind",
                                                 _classify_func_kind(c["name"], c["kind"]))
                    c["file"]           = r.get("resolved_file")
                    c["line"]           = r.get("resolved_line_start")
                    c["line_end"]       = r.get("resolved_line_end")
                    c["suggested_file"] = r.get("suggested_file")
                    c["confidence"]     = r.get("confidence", "low")
                # Ensure func_kind is always present (fallback for any missed cases)
                if "func_kind" not in c:
                    c["func_kind"] = _classify_func_kind(c["name"], c.get("kind", "unknown"))
                updated_callees.append(c)
            p["callees"] = updated_callees

    # Build func_to_pattern index
    func_to_pattern: dict[tuple[str, str], int] = {}
    for filename, patterns in all_file_patterns.items():
        for p in patterns:
            if p.get("raw_type") in ("func_def", "func_definition", "func_prototype",
                                     "function_def", "function_definition"):
                m = _RE_FUNC_DEF.search(p.get("source_snippet", ""))
                if m:
                    func_to_pattern[(filename, m.group(1))] = p["id"]
                    continue
                m2 = _RE_FUNC_PROTO.search(p.get("source_snippet", ""))
                if m2:
                    func_to_pattern[(filename, m2.group(1))] = p["id"]

        file_entries = index._by_file.get(filename, [])
        for entry in file_entries:
            if entry.kind != "definition":
                continue
            key = (filename, entry.name)
            if key in func_to_pattern:
                continue
            for p in patterns:
                lr = p.get("line_range") or [0, 0]
                if lr[0] <= entry.line_start <= (lr[1] if lr[1] else lr[0] + 1):
                    func_to_pattern[key] = p["id"]
                    break

    print(f"  [CallGraph] func_to_pattern index: {len(func_to_pattern)} entries")

    # Resolve pattern_id + definition fields in callees
    for filename, patterns in all_file_patterns.items():
        for p in patterns:
            resolved_callees = []
            for c in p.get("callees", []):
                callee_name = c["name"]
                callee_file = c.get("file")

                if callee_file:
                    if c.get("pattern_id") is None:
                        pid = func_to_pattern.get((callee_file, callee_name))
                        if pid is not None:
                            c["pattern_id"] = pid
                    if c.get("pattern_id") is not None:
                        c["definition_pattern_id"] = c["pattern_id"]
                        c["definition_file"]       = callee_file
                    if c.get("line") is None:
                        entries = index.lookup_function(callee_name).get("entries", [])
                        for e in entries:
                            if e["file"] == callee_file:
                                c["line"]     = e["line_start"]
                                c["line_end"] = e["line_end"]
                                break

                if c["kind"] == "local" and c.get("definition_pattern_id") is None:
                    entries = index.lookup_function(callee_name).get("entries", [])
                    for e in entries:
                        if e["kind"] == "definition":
                            pid = func_to_pattern.get((e["file"], callee_name))
                            if pid is not None:
                                c["definition_pattern_id"] = pid
                                c["definition_file"]       = e["file"]
                                if c.get("line_end") is None:
                                    c["line_end"] = e["line_end"]
                            break

                resolved_callees.append(c)
            p["callees"] = resolved_callees

            # Compute func_calls / func_creates summary lists
            _attach_func_kind_summary(p)

    # Attach callers + definition fields
    for filename, patterns in all_file_patterns.items():
        for p in patterns:
            func_name = None
            raw = p.get("raw_type", "")
            if any(tag in raw for tag in ("func_def", "func_definition",
                                          "function_def", "function_definition")):
                m = _RE_FUNC_DEF.search(p.get("source_snippet", ""))
                if m:
                    func_name = m.group(1)
            if func_name is None:
                lr = p.get("line_range") or [0, 0]
                for entry in index._by_file.get(filename, []):
                    if entry.kind == "definition" and entry.line_start == lr[0]:
                        func_name = entry.name
                        break
            if func_name is None:
                continue

            own_pid = func_to_pattern.get((filename, func_name))

            caller_refs = index.lookup_callers(func_name).get("callers", [])
            callers_out = []
            for ref in caller_refs:
                caller_func = ref.get("caller_func")
                pid = func_to_pattern.get((ref["file"], caller_func or ""))

                caller_line_start = caller_line_end = None
                if caller_func:
                    caller_entries = index.lookup_function(caller_func).get("entries", [])
                    for ce in caller_entries:
                        if ce["file"] == ref["file"]:
                            caller_line_start = ce["line_start"]
                            caller_line_end   = ce["line_end"]
                            break

                # func_kind of the caller reference = kind of THIS function
                # (how the caller perceives this callee)
                caller_fk = _classify_func_kind(func_name, "local")

                callers_out.append({
                    "name":                  func_name,
                    "func_kind":             caller_fk,          # ← NEW v4
                    "file":                  ref["file"],
                    "line":                  ref["line"],
                    "caller_func":           caller_func,
                    "caller_line_start":     caller_line_start,
                    "caller_line_end":       caller_line_end,
                    "pattern_id":            pid,
                    "definition_pattern_id": own_pid,
                    "definition_file":       filename,
                })
            p["callers"] = callers_out

    print("[CallGraph] Dependency analysis complete.\n")
    return all_file_patterns


# ══════════════════════════════════════════════════════════════════
# PUBLIC HELPER — UNSCANNED FILE REPORT
# ══════════════════════════════════════════════════════════════════

def get_unscanned_report(all_file_patterns: dict[str, list[dict]], tests_dir: Path) -> dict:
    index = FunctionIndex().build(tests_dir, extra_search_dirs=[tests_dir.parent])
    file_to_unknowns: dict[str, list[dict]] = {}
    for filename, patterns in all_file_patterns.items():
        for p in patterns:
            for c in p.get("callees", []):
                if c.get("kind") not in ("unknown", "unscanned", None):
                    continue
                name = c.get("name", "")
                if not name or name in STDLIB_FUNCTIONS:
                    continue
                suggested = _guess_file(name, index._unscanned_files)
                key = suggested or "__unknown__"
                file_to_unknowns.setdefault(key, []).append({
                    "caller_file":  filename,
                    "caller_func":  _find_enclosing_func_name(p),
                    "callee_name":  name,
                    "func_kind":    c.get("func_kind", "call"),
                    "pattern_id":   p.get("id"),
                    "call_line":    c.get("line"),
                    "suggested_by": "prefix_match" if suggested else "no_match",
                })

    all_unscanned = index._unscanned_files.copy()
    report_entries = []
    for fname in sorted(all_unscanned):
        callers = file_to_unknowns.get(fname, [])
        unknown_callees = sorted({c["callee_name"] for c in callers})
        found_at = None
        for search_dir in [tests_dir.parent, tests_dir]:
            candidate = search_dir / fname
            if candidate.exists():
                found_at = str(candidate)
                break
        report_entries.append({
            "file": fname, "found_at": found_at, "callers": callers,
            "unknown_callees": unknown_callees, "priority": "high" if callers else "low",
        })
    report_entries.sort(key=lambda e: (e["priority"] != "high", e["file"]))
    total_unknown_calls = sum(len(e["callers"]) for e in report_entries)
    orphan_calls = file_to_unknowns.get("__unknown__", [])
    return {
        "unscanned_files":       report_entries,
        "orphan_unknown_calls":  orphan_calls,
        "total_unscanned":       len(report_entries),
        "total_unknown_calls":   total_unknown_calls + len(orphan_calls),
    }


def _guess_file(func_name: str, unscanned: set[str]) -> str | None:
    if not unscanned:
        return None
    tokens = re.findall(r'[A-Z][a-z0-9]*|[a-z0-9]+', func_name)
    tokens_lower = {t.lower() for t in tokens}
    best_match: str | None = None
    best_score = 0
    for fname in unscanned:
        stem = Path(fname).stem
        stem_tokens = set(re.findall(r'[A-Z][a-z0-9]*|[a-z0-9]+', stem))
        stem_lower = {t.lower() for t in stem_tokens}
        score = len(tokens_lower & stem_lower)
        if score > best_score:
            best_score = score
            best_match = fname
    return best_match if best_score >= 1 else None


def _find_enclosing_func_name(pattern: dict) -> str | None:
    raw = pattern.get("raw_type", "")
    if any(tag in raw for tag in ("func_def", "func_definition",
                                  "function_def", "function_definition")):
        m = _RE_FUNC_DEF.search(pattern.get("source_snippet", ""))
        if m:
            return m.group(1)
    return None
