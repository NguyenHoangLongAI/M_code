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
