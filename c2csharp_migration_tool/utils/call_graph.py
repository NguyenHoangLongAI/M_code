"""
utils/call_graph.py — Cross-file Call Graph Analyzer  v2
=========================================================
Giai đoạn 3: Phân tích caller/callee cross-file với LLM tool calling.

Luồng:
  1. build_function_index()   — regex scan tests/ + header files từ #include
  2. extract_callees_local()  — regex tìm callee trong từng pattern (không gọi API)
  3. resolve_with_llm()       — LLM nhận ambiguous calls + unscanned_file_list
                                để suy luận ChkComCtrlLog nằm ở file nào
  4. attach_to_patterns()     — gắn caller/callee vào từng pattern dict

Phân loại callee kind:
  stdlib   — hàm trong STDLIB_FUNCTIONS (printf, memcpy, ...)
  local    — tìm thấy trong index (tests/ hoặc header được #include)
  extern   — LLM xác nhận là external lib (không có trong project)
  unscanned — LLM suy luận nằm ở file chưa được migrate/scan
  unknown  — không thể xác định

LLM được cung cấp:
  - Danh sách ambiguous function names
  - Tool: lookup_function(name)      → tra index
  - Tool: lookup_callers(name)       → tra caller map
  - Tool: list_unscanned_files()     → danh sách file chưa scan
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
    kind:       str          # "stdlib" | "local" | "extern" | "unscanned" | "unknown"
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

# Regex tìm #include local headers (không phải system headers)
_RE_INCLUDE_LOCAL = re.compile(r'#\s*include\s+"([^"]+)"', re.MULTILINE)

# Extensions được coi là source/header files
_SOURCE_EXTS = {".c", ".pc", ".h", ".pro"}


def _scan_include_headers(
    source_files: list[Path],
    search_dirs: list[Path],
) -> list[Path]:
    """
    Parse tất cả #include "..." trong source_files,
    tìm file tương ứng trong search_dirs.
    Trả về list các header Path đã tìm thấy (chưa có trong source_files).

    Ví dụ:
      #include "ComChk.h"  →  tìm ComChk.h trong tests/, ../include/, ...
    """
    already_indexed = {f.name for f in source_files}
    found: list[Path] = []
    seen_names: set[str] = set()

    for src in source_files:
        try:
            text = src.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for m in _RE_INCLUDE_LOCAL.finditer(text):
            inc_name = Path(m.group(1)).name   # lấy tên file, bỏ subdir prefix
            if inc_name in already_indexed or inc_name in seen_names:
                continue
            # Tìm trong search_dirs
            for d in search_dirs:
                candidate = d / inc_name
                if candidate.exists():
                    found.append(candidate)
                    seen_names.add(inc_name)
                    break
            else:
                # Không tìm thấy → ghi nhận là "tham chiếu tới file chưa scan"
                seen_names.add(inc_name)   # tránh log trùng

    return found


def _find_line_end(source: str, open_brace_pos: int) -> int:
    """
    Scan từng ký tự từ vị trí '{' đầu tiên của function body,
    đếm bậc mở/đóng để tìm '}' đóng tương ứng.
    Trả về line number (1-based) của dòng chứa '}' đóng.
    Bỏ qua '{' và '}' trong string literals và comments.

    Ví dụ:
      int foo(int x) {          ← open_brace_pos trỏ đến '{' này
          if (x > 0) { ... }
      }                         ← trả về line của '}' này
    """
    # Tìm vị trí '{' thực sự (open_brace_pos trỏ đến cuối match của regex)
    # regex match kết thúc bằng '{', nên tìm lại từ vị trí đó
    brace_start = source.find('{', open_brace_pos)
    if brace_start == -1:
        return source[:open_brace_pos].count('\n') + 1

    depth    = 0
    i        = brace_start
    in_str   = False   # inside double-quoted string
    in_char  = False   # inside single-quoted char
    in_lcomm = False   # inside // line comment
    in_bcomm = False   # inside /* block comment */
    length   = len(source)

    while i < length:
        c = source[i]

        # ── Handle line comment end ────────────────────────────────
        if in_lcomm:
            if c == '\n':
                in_lcomm = False
            i += 1
            continue

        # ── Handle block comment end ───────────────────────────────
        if in_bcomm:
            if c == '*' and i + 1 < length and source[i + 1] == '/':
                in_bcomm = False
                i += 2
            else:
                i += 1
            continue

        # ── Handle string literal end ──────────────────────────────
        if in_str:
            if c == '\\':
                i += 2   # skip escaped char
                continue
            if c == '"':
                in_str = False
            i += 1
            continue

        # ── Handle char literal end ────────────────────────────────
        if in_char:
            if c == '\\':
                i += 2
                continue
            if c == "'":
                in_char = False
            i += 1
            continue

        # ── Normal code ────────────────────────────────────────────
        if c == '/' and i + 1 < length:
            if source[i + 1] == '/':
                in_lcomm = True
                i += 2
                continue
            if source[i + 1] == '*':
                in_bcomm = True
                i += 2
                continue

        if c == '"':
            in_str = True
        elif c == "'":
            in_char = True
        elif c == '{':
            depth += 1
        elif c == '}':
            depth -= 1
            if depth == 0:
                # Found the closing brace
                return source[:i + 1].count('\n') + 1

        i += 1

    # Fallback: return last line
    return source.count('\n') + 1


# ══════════════════════════════════════════════════════════════════
# STEP 1 — BUILD FUNCTION INDEX (local, no API)
# ══════════════════════════════════════════════════════════════════

class FunctionIndex:
    """
    Index toàn bộ function definitions/prototypes trong tests/ + header files.
    Cũng build reverse map: callers_of[func_name] = list of (file, line, context).

    Extra:
      _unscanned_files: set[str]  — tên file được #include nhưng không tìm thấy
                                    → dùng để LLM suy luận "nằm ở đâu"
      _include_map: dict[str, str] — header_name → full path đã scan
    """

    def __init__(self):
        self._by_name:        dict[str, list[FunctionEntry]] = {}
        self._by_file:        dict[str, list[FunctionEntry]] = {}
        self._callers_of:     dict[str, list[dict]]          = {}
        self._unscanned_files: set[str]                      = set()
        self._include_map:    dict[str, str]                 = {}   # name → rel path

    # ── Build ────────────────────────────────────────────────────

    def build(self, tests_dir: Path, extra_search_dirs: list[Path] | None = None) -> "FunctionIndex":
        """
        Scan tất cả .c/.pc/.h/.pro files trong tests_dir.
        Sau đó parse #include "..." để scan thêm header files.
        extra_search_dirs: thư mục bổ sung để tìm header (vd: ../include/)
        """
        print("  [CallGraph] Building function index ...")

        search_dirs = [tests_dir] + (extra_search_dirs or [])

        # ── Pass 1: scan tất cả files trong tests/ ────────────────
        c_files = sorted(
            list(tests_dir.glob("*.c")) +
            list(tests_dir.glob("*.pc")) +
            list(tests_dir.glob("*.h")) +
            list(tests_dir.glob("*.pro"))
        )
        for fpath in c_files:
            self._index_file(fpath)

        # ── Pass 2: scan thêm header files từ #include "..." ──────
        extra_headers = _scan_include_headers(c_files, search_dirs)
        already = {f.name for f in c_files}

        for hpath in extra_headers:
            if hpath.name not in already:
                print(f"  [CallGraph] Scanning included header: {hpath.name}")
                self._index_file(hpath)
                self._include_map[hpath.name] = str(hpath)
                c_files.append(hpath)
                already.add(hpath.name)

        # ── Track includes không tìm thấy ─────────────────────────
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
        lines = source.splitlines()
        rel = fpath.name

        def line_of(pos: int) -> int:
            return source[:pos].count("\n") + 1

        # Definitions
        for m in _RE_FUNC_DEF.finditer(source):
            name = m.group(1)
            if name in ("if", "while", "for", "switch"):
                continue
            ln      = line_of(m.start())
            ln_end  = _find_line_end(source, m.end() - 1)  # m.end()-1 = pos of '{'
            sig     = m.group(0).rstrip("{").strip()
            entry   = FunctionEntry(
                name=name, file=rel, line_start=ln,
                line_end=ln_end, kind="definition", signature=sig[:120],
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
                    "file":       e.file,
                    "line_start": e.line_start,
                    "line_end":   e.line_end,
                    "kind":       e.kind,
                    "signature":  e.signature,
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

    def lookup_unscanned(self) -> dict:
        """
        Returns unscanned files info for UI + LLM.
        Includes: which files are unscanned, and which local functions call into them.
        """
        # Map: unscanned_file → list of (caller_file, func_name) that call something unknown
        # We infer this from _callers_of gaps: functions called but not in _by_name
        return {
            "unscanned_files": sorted(self._unscanned_files),
            "count":           len(self._unscanned_files),
            "scanned_files":   sorted(self._by_file.keys()),
        }

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
                    file=defs[0]["file"], line=defs[0]["line_start"],
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
    {
        "toolSpec": {
            "name": "list_unscanned_files",
            "description": (
                "List all project files that are referenced via #include but have NOT been "
                "scanned/migrated yet. Use this to determine if an unknown function likely "
                "comes from a file that hasn't been processed."
            ),
            "inputSchema": {
                "json": {
                    "type": "object",
                    "properties": {},
                    "required": [],
                }
            },
        }
    },
]

_RESOLVER_SYSTEM = """
You are a C codebase dependency resolver.
You will be given a list of C function names that could not be automatically resolved
and need to be classified: which file defines them (if known) and what kind they are.

You have three tools:
  - lookup_function(name)     → check if function exists in the scanned index
  - lookup_callers(name)      → find call sites for a function
  - list_unscanned_files()    → list files referenced via #include but not yet scanned

Classification rules:
  - "local"      : found in the project index (lookup_function returned found=true)
  - "unscanned"  : NOT in index, BUT likely belongs to a file in list_unscanned_files()
                   (name prefix matches, e.g. ChkComCtrlLog → ComChk.h/ComChk.c)
  - "extern"     : NOT in index AND NOT matching any unscanned file — external library
  - "unknown"    : cannot determine

Resolution strategy:
  1. Call lookup_function(name) first.
  2. If not found, call list_unscanned_files() once (reuse result for all unknowns).
  3. Match function name prefix/suffix to unscanned file names:
       ChkComCtrlLog  → prefix "Chk" or "Com" → likely ComChk.c / ComChk.h
       SjlInitProc    → prefix "Sjl" → likely SjlInit.c
       ComChkDate     → prefix "Com" + "Chk" → likely ComChk.c
  4. Use heuristic: if function name starts with a token that matches an unscanned file
     stem → kind="unscanned", suggested_file=<best match>

Rules:
1. Call lookup_function before anything else.
2. Call list_unscanned_files() at most ONCE — cache and reuse the result.
3. Do NOT call a tool for the same function twice.
4. After gathering all needed info, return a JSON array:
   [
     {
       "name": "FunctionName",
       "resolved_file": "filename.c or null",
       "resolved_line_start": 123,
       "resolved_line_end": 145,
       "kind": "local | unscanned | extern | unknown",
       "suggested_file": "ComChk.c (unscanned)",
       "confidence": "high | medium | low"
     },
     ...
   ]
5. Return ONLY the JSON array after all tool calls are done. No prose.
"""


def resolve_with_llm(
    ambiguous_names: list[str],
    index: FunctionIndex,
    pattern_context: str = "",
) -> dict[str, dict]:
    """
    Use LLM with tool calling to resolve ambiguous function references.
    Returns dict: name → resolved info.

    LLM gets:
      - ambiguous function names
      - 3 tools: lookup_function, lookup_callers, list_unscanned_files
      - Never receives raw source code
    """
    if not ambiguous_names:
        return {}

    from utils.key_pool import get_aws_config
    from utils.api_client import _parse_bedrock_key, _get_client
    from config import BEDROCK_MODEL_ID

    cfg      = get_aws_config()
    model_id = cfg.get("model_id") or BEDROCK_MODEL_ID
    region   = cfg.get("region_name", "ap-southeast-1")
    api_key  = cfg.get("bedrock_api_key", "")

    if not api_key:
        raise RuntimeError("BEDROCK_API_KEY not configured.")

    creds  = _parse_bedrock_key(api_key)
    client = _get_client(region, creds)

    user_msg = (
        f"Resolve these C function references:\n"
        f"{json.dumps(ambiguous_names, ensure_ascii=False)}\n\n"
        f"Context: {pattern_context[:300] if pattern_context else '(none)'}\n\n"
        "Steps:\n"
        "1. Call lookup_function(name) for each.\n"
        "2. If any are not found, call list_unscanned_files() once to check if "
        "they might belong to an unscanned file.\n"
        "3. Return the JSON array with kind, resolved_file, suggested_file."
    )

    messages = [{"role": "user", "content": [{"text": user_msg}]}]
    resolved: dict[str, dict] = {}

    print(f"  [CallGraph/LLM] Resolving {len(ambiguous_names)} ambiguous functions ...")

    # Agentic loop — LLM may call tools multiple times
    for iteration in range(15):  # hard cap (more iterations since 3 tools now)
        resp = client.converse(
            modelId=model_id,
            system=[{"text": _RESOLVER_SYSTEM}],
            messages=messages,
            inferenceConfig={"maxTokens": 4096, "temperature": 0.0},
            toolConfig={"tools": TOOL_DEFINITIONS},
        )

        content_blocks = resp.get("output", {}).get("message", {}).get("content", [])

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

                print(f"  [CallGraph/LLM] Tool call: {tool_name}"
                      + (f"({func_name!r})" if func_name else "()"))

                if tool_name == "lookup_function":
                    result = index.lookup_function(func_name)

                elif tool_name == "lookup_callers":
                    result = index.lookup_callers(func_name)

                elif tool_name == "list_unscanned_files":
                    # Return all unscanned files + brief hint for matching
                    unscanned = sorted(index._unscanned_files)
                    result = {
                        "unscanned_files": unscanned,
                        "count": len(unscanned),
                        "hint": (
                            "Match function name prefix to file stem. "
                            "E.g. ChkComCtrlLog → ComChk.c/h (prefix 'Chk'+'Com'), "
                            "SjlGetQueue → SjlComFunc.c (prefix 'Sjl')."
                        ),
                    }
                    print(f"  [CallGraph/LLM] Unscanned files: {unscanned}")

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
            continue  # next iteration

        # ── No more tool calls — extract final JSON ───────────────
        text_blocks = [b.get("text", "") for b in content_blocks if b.get("text")]
        final_text  = "\n".join(text_blocks).strip()

        try:
            clean = re.sub(r"^```(?:json)?\s*", "", final_text, flags=re.I)
            clean = re.sub(r"\s*```\s*$", "", clean)
            data  = json.loads(clean.strip())
            if isinstance(data, list):
                for item in data:
                    if isinstance(item, dict) and "name" in item:
                        resolved[item["name"]] = item
        except (json.JSONDecodeError, ValueError) as e:
            print(f"  [CallGraph/LLM] WARNING: Could not parse response: {e}")

        break

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

    # ── Step 1: Build index (tests/ + headers từ #include) ───────
    index = FunctionIndex().build(tests_dir, extra_search_dirs=[tests_dir.parent])

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
                    c["kind"]           = r.get("kind", "extern")
                    c["file"]           = r.get("resolved_file")
                    c["line"]           = r.get("resolved_line_start")
                    c["line_end"]       = r.get("resolved_line_end")
                    c["suggested_file"] = r.get("suggested_file")   # ← NEW: hint về file chưa scan
                    c["confidence"]     = r.get("confidence", "low")
                updated_callees.append(c)
            p["callees"] = updated_callees

    # ── Step 4: Attach callers + resolve pattern_id everywhere ───
    #
    # Build a comprehensive lookup:
    #   func_to_pattern[(file, func_name)] → pattern_id
    #
    # Strategy: for each pattern, try multiple ways to extract func name:
    #   1. raw_type is func_def / func_definition → parse source_snippet
    #   2. line_range overlap with indexed function → use index
    #
    func_to_pattern: dict[tuple[str, str], int] = {}

    for filename, patterns in all_file_patterns.items():
        # Method 1: parse snippet for patterns explicitly tagged as functions
        for p in patterns:
            if p.get("raw_type") in ("func_def", "func_definition", "func_prototype",
                                     "function_def", "function_definition"):
                m = _RE_FUNC_DEF.search(p.get("source_snippet", ""))
                if m:
                    func_to_pattern[(filename, m.group(1))] = p["id"]
                    continue
                # fallback: check prototype regex too
                m2 = _RE_FUNC_PROTO.search(p.get("source_snippet", ""))
                if m2:
                    func_to_pattern[(filename, m2.group(1))] = p["id"]

        # Method 2: match patterns by line_range overlap with index entries
        # Covers cases where raw_type label differs from expected
        file_entries = index._by_file.get(filename, [])
        for entry in file_entries:
            if entry.kind != "definition":
                continue
            key = (filename, entry.name)
            if key in func_to_pattern:
                continue  # already resolved via method 1
            # Find pattern whose line_range contains entry.line_start
            for p in patterns:
                lr = p.get("line_range") or [0, 0]
                if lr[0] <= entry.line_start <= (lr[1] if lr[1] else lr[0] + 1):
                    func_to_pattern[key] = p["id"]
                    break

    print(f"  [CallGraph] func_to_pattern index: {len(func_to_pattern)} entries")

    # ── Step 4b: Resolve pattern_id in existing callees ──────────
    for filename, patterns in all_file_patterns.items():
        for p in patterns:
            resolved_callees = []
            for c in p.get("callees", []):
                callee_name = c["name"]
                callee_file = c.get("file")
                if callee_file and c.get("pattern_id") is None:
                    pid = func_to_pattern.get((callee_file, callee_name))
                    if pid is not None:
                        c["pattern_id"] = pid
                    # Also fill line_start / line_end from index if missing
                    if c.get("line") is None:
                        entries = index.lookup_function(callee_name).get("entries", [])
                        for e in entries:
                            if e["file"] == callee_file:
                                c["line"]     = e["line_start"]
                                c["line_end"] = e["line_end"]
                                break
                # Always carry line_end from index for local/resolved callees
                if c["kind"] == "local" and c.get("line_end") is None:
                    entries = index.lookup_function(callee_name).get("entries", [])
                    for e in entries:
                        if e["file"] == c.get("file"):
                            c["line_end"] = e["line_end"]
                            break
                resolved_callees.append(c)
            p["callees"] = resolved_callees

    # ── Step 4c: Attach callers to func_def patterns ─────────────
    for filename, patterns in all_file_patterns.items():
        for p in patterns:
            # Determine function name for this pattern
            func_name = None
            raw = p.get("raw_type", "")

            # Try from snippet first
            if any(tag in raw for tag in ("func_def", "func_definition",
                                          "function_def", "function_definition")):
                m = _RE_FUNC_DEF.search(p.get("source_snippet", ""))
                if m:
                    func_name = m.group(1)

            # Fallback: match via index line overlap
            if func_name is None:
                lr = p.get("line_range") or [0, 0]
                for entry in index._by_file.get(filename, []):
                    if entry.kind == "definition" and entry.line_start == lr[0]:
                        func_name = entry.name
                        break

            if func_name is None:
                continue

            caller_refs = index.lookup_callers(func_name).get("callers", [])
            callers_out = []
            for ref in caller_refs:
                caller_func = ref.get("caller_func")
                pid = func_to_pattern.get((ref["file"], caller_func or ""))

                # Also enrich with line_start/line_end of the caller function itself
                caller_line_start = None
                caller_line_end   = None
                if caller_func:
                    caller_entries = index.lookup_function(caller_func).get("entries", [])
                    for ce in caller_entries:
                        if ce["file"] == ref["file"]:
                            caller_line_start = ce["line_start"]
                            caller_line_end   = ce["line_end"]
                            break

                callers_out.append({
                    "name":              func_name,         # callee name (this function)
                    "file":              ref["file"],       # file where call happens
                    "line":              ref["line"],       # exact call site line
                    "caller_func":       caller_func,       # enclosing function name
                    "caller_line_start": caller_line_start, # where caller func starts
                    "caller_line_end":   caller_line_end,   # where caller func ends
                    "pattern_id":        pid,               # pattern_id of caller func
                })
            p["callers"] = callers_out

    print("[CallGraph] Dependency analysis complete.\n")
    return all_file_patterns


# ══════════════════════════════════════════════════════════════════
# PUBLIC HELPER — UNSCANNED FILE REPORT
# ══════════════════════════════════════════════════════════════════

def get_unscanned_report(
    all_file_patterns: dict[str, list[dict]],
    tests_dir: Path,
) -> dict:
    """
    Scan tests/ + headers, trả về báo cáo "file nào nên scan thêm".

    Trả về:
    {
      "unscanned_files": [
        {
          "file":        "ComChk.h",         ← tên file chưa scan
          "found_at":    null,               ← null = không tìm thấy trên disk
          "callers": [                       ← ai đang gọi hàm từ file này
            {
              "caller_file":    "SjlComFunc.c",
              "caller_func":    "SjlMainFunc",
              "callee_name":    "ChkComCtrlLog",
              "pattern_id":     12,
              "call_line":      45
            }
          ],
          "unknown_callees": ["ChkComCtrlLog", "ChkComInit"]  ← hàm chưa resolve
        }
      ],
      "total_unscanned": N,
      "total_unknown_calls": N,
    }
    """
    # Build index (no LLM)
    index = FunctionIndex().build(tests_dir, extra_search_dirs=[tests_dir.parent])

    # Collect all callees with kind=unknown or kind=unscanned per source file
    # Map: suggested_file → list of call references
    file_to_unknowns: dict[str, list[dict]] = {}

    for filename, patterns in all_file_patterns.items():
        for p in patterns:
            for c in p.get("callees", []):
                if c.get("kind") not in ("unknown", "unscanned", None):
                    continue
                name = c.get("name", "")
                if not name or name in STDLIB_FUNCTIONS:
                    continue

                # Try to guess which unscanned file this belongs to
                suggested = _guess_file(name, index._unscanned_files)
                key = suggested or "__unknown__"

                file_to_unknowns.setdefault(key, []).append({
                    "caller_file":  filename,
                    "caller_func":  _find_enclosing_func_name(p),
                    "callee_name":  name,
                    "pattern_id":   p.get("id"),
                    "call_line":    c.get("line"),
                    "suggested_by": "prefix_match" if suggested else "no_match",
                })

    # Also add unscanned files from index that have no unknown callees mapped
    all_unscanned = index._unscanned_files.copy()

    report_entries = []
    for fname in sorted(all_unscanned):
        callers = file_to_unknowns.get(fname, [])
        unknown_callees = sorted({c["callee_name"] for c in callers})

        # Check if file actually exists somewhere (was just not in tests/)
        found_at = None
        for search_dir in [tests_dir.parent, tests_dir]:
            candidate = search_dir / fname
            if candidate.exists():
                found_at = str(candidate)
                break

        report_entries.append({
            "file":             fname,
            "found_at":         found_at,
            "callers":          callers,
            "unknown_callees":  unknown_callees,
            "priority":         "high" if callers else "low",
        })

    # Sort: high priority (has callers) first
    report_entries.sort(key=lambda e: (e["priority"] != "high", e["file"]))

    total_unknown_calls = sum(len(e["callers"]) for e in report_entries)
    # Also count truly unknown (no file match)
    orphan_calls = file_to_unknowns.get("__unknown__", [])

    return {
        "unscanned_files":     report_entries,
        "orphan_unknown_calls": orphan_calls,   # unknown với không guess được file nào
        "total_unscanned":     len(report_entries),
        "total_unknown_calls": total_unknown_calls + len(orphan_calls),
    }


def _guess_file(func_name: str, unscanned: set[str]) -> str | None:
    """
    Heuristic: match function name prefix/infix với file stem.
    Ví dụ:
      ChkComCtrlLog  → ComChk.h  (tokens: Chk, Com, Ctrl, Log)
      SjlGetQueue    → SjlComFunc.c (token: Sjl)
      ComChkDate     → ComChk.h (tokens: Com, Chk)
    """
    if not unscanned:
        return None

    # Tokenize CamelCase function name
    tokens = re.findall(r'[A-Z][a-z0-9]*|[a-z0-9]+', func_name)
    tokens_lower = {t.lower() for t in tokens}

    best_match: str | None = None
    best_score = 0

    for fname in unscanned:
        stem = Path(fname).stem
        stem_tokens = set(re.findall(r'[A-Z][a-z0-9]*|[a-z0-9]+', stem))
        stem_lower  = {t.lower() for t in stem_tokens}

        score = len(tokens_lower & stem_lower)
        if score > best_score:
            best_score = score
            best_match = fname

    return best_match if best_score >= 1 else None


def _find_enclosing_func_name(pattern: dict) -> str | None:
    """Extract function name from a func_def pattern."""
    raw = pattern.get("raw_type", "")
    if any(tag in raw for tag in ("func_def", "func_definition",
                                  "function_def", "function_definition")):
        m = _RE_FUNC_DEF.search(pattern.get("source_snippet", ""))
        if m:
            return m.group(1)
    return None
