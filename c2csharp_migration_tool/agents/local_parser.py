"""
agents/local_parser.py
LOCAL C/Pro*C Pattern Parser — NO API CALLS, NO SOURCE UPLOAD

Extracts patterns using pure regex/heuristics.
Source code never leaves the client.
Only the DESCRIPTION of each pattern is sent to Claude API.
"""

import re
from dataclasses import dataclass, field


@dataclass
class RawPattern:
    id: int
    source_snippet: str
    line_range: list
    raw_type: str
    description: str   # ← this (not source) goes to API


# ── Regex rules ────────────────────────────────────────────────
# Each rule: (raw_type, compiled_pattern, multiline?)
_RULES = [
    # Preprocessor
    ("pragma",          re.compile(r"^#pragma\s+.+", re.M)),
    ("include_sys",     re.compile(r"^#include\s+<[^>]+>", re.M)),
    ("include_local",   re.compile(r'^#include\s+"[^"]+"', re.M)),
    ("define",          re.compile(r"^#define\s+\w+.*", re.M)),
    ("ifdef_block",     re.compile(r"^#ifn?def\s+\w+.*", re.M)),
    ("undef",           re.compile(r"^#undef\s+\w+", re.M)),

    # Pro*C / EXEC SQL
    ("exec_sql",        re.compile(r"EXEC\s+SQL[\s\S]+?;", re.M)),

    # RCS/metadata
    ("rcs_tag",         re.compile(r'static\s+char\s+\w+\[\]\s*=\s*\{[^}]*\$Header[^}]*\}', re.M)),

    # Function prototype (has semicolon, no body)
    ("func_prototype",  re.compile(
        r"^(?:extern\s+)?(?:static\s+)?(?:inline\s+)?"
        r"(?:const\s+)?[\w\*]+\s+[\w\*]+\s*\([^)]*\)\s*;",
        re.M
    )),

    # Function definition (has opening brace)
    ("func_def",        re.compile(
        r"^(?:extern\s+)?(?:static\s+)?(?:inline\s+)?"
        r"(?:const\s+)?[\w\*]+\s+\w+\s*\([^)]*\)\s*\{",
        re.M
    )),

    # Variable declarations (local / global)
    ("var_decl",        re.compile(
        r"^\s*(?:static\s+|extern\s+|const\s+|volatile\s+)*"
        r"(?:unsigned\s+|signed\s+)?"
        r"(?:int|char|long|short|float|double|void)\s*\*?\s*\w+(?:\[\d*\])?\s*(?:=\s*[^;]+)?;",
        re.M
    )),

    # Struct / union / typedef / enum
    ("struct_def",      re.compile(r"\bstruct\s+\w*\s*\{", re.M)),
    ("typedef",         re.compile(r"\btypedef\b.+;", re.M)),
    ("enum_def",        re.compile(r"\benum\s+\w*\s*\{", re.M)),

    # Memory management
    ("malloc_call",     re.compile(r"\bmalloc\s*\([^)]*\)", re.M)),
    ("calloc_call",     re.compile(r"\bcalloc\s*\([^)]*\)", re.M)),
    ("realloc_call",    re.compile(r"\brealloc\s*\([^)]*\)", re.M)),
    ("free_call",       re.compile(r"\bfree\s*\([^)]*\)", re.M)),
    ("memcpy_call",     re.compile(r"\bmemcpy\s*\([^)]*\)", re.M)),
    ("memset_call",     re.compile(r"\bmemset\s*\([^)]*\)", re.M)),
    ("sizeof_expr",     re.compile(r"\bsizeof\s*\([^)]*\)", re.M)),

    # I/O
    ("printf_call",     re.compile(r"\bprintf\s*\([^)]*(?:\([^)]*\)[^)]*)*\)", re.M)),
    ("scanf_call",      re.compile(r"\bscanf\s*\([^)]*\)", re.M)),
    ("fprintf_call",    re.compile(r"\bfprintf\s*\([^)]*(?:\([^)]*\)[^)]*)*\)", re.M)),
    ("sprintf_call",    re.compile(r"\bsprintf\s*\([^)]*(?:\([^)]*\)[^)]*)*\)", re.M)),
    ("fopen_call",      re.compile(r"\bfopen\s*\([^)]*\)", re.M)),
    ("fclose_call",     re.compile(r"\bfclose\s*\([^)]*\)", re.M)),
    ("fread_call",      re.compile(r"\bfread\s*\([^)]*\)", re.M)),
    ("fwrite_call",     re.compile(r"\bfwrite\s*\([^)]*\)", re.M)),

    # String operations
    ("strcpy_call",     re.compile(r"\bstr(?:n?cpy|cat|cmp|len|str|chr)\s*\([^)]*\)", re.M)),

    # Control flow — extract whole blocks
    ("if_else_block",   re.compile(
        r"\bif\s*\([^)]+\)\s*\{[\s\S]*?\}(?:\s*else\s*(?:if\s*\([^)]+\)\s*)?\{[\s\S]*?\})*",
        re.M
    )),
    ("while_loop",      re.compile(r"\bwhile\s*\([^)]+\)\s*\{[\s\S]*?\}", re.M)),
    ("for_loop",        re.compile(r"\bfor\s*\([^)]*;[^)]*;[^)]*\)\s*\{[\s\S]*?\}", re.M)),
    ("do_while",        re.compile(r"\bdo\s*\{[\s\S]*?\}\s*while\s*\([^)]+\)\s*;", re.M)),
    ("switch_block",    re.compile(r"\bswitch\s*\([^)]+\)\s*\{[\s\S]*?\}", re.M)),
    ("goto_stmt",       re.compile(r"\bgoto\s+\w+\s*;", re.M)),
    ("return_stmt",     re.compile(r"\breturn\s+[^;]+;", re.M)),

    # Operators
    ("pointer_deref",   re.compile(r"\*\s*\w+\s*=", re.M)),
    ("increment_op",    re.compile(r"\w+\s*\+\+|\+\+\s*\w+|\w+\s*--", re.M)),
    ("pointer_arith",   re.compile(r"\w+\s*[+\-]\s*\d+\s*\)|\w+\[\w+\]", re.M)),
    ("bitwise_op",      re.compile(r"[&|^~]\s*\w+|\w+\s*[&|^]\s*\w+", re.M)),
    ("modulo_op",       re.compile(r"\w+\s*%\s*\d+", re.M)),

    # Error handling
    ("errno_check",     re.compile(r"\berrno\b", re.M)),
    ("perror_call",     re.compile(r"\bperror\s*\(", re.M)),
    ("assert_call",     re.compile(r"\bassert\s*\([^)]*\)", re.M)),
]


# ── Human-readable descriptions (used in API prompts instead of code) ──
_DESCRIPTIONS = {
    "pragma":         "C #pragma preprocessor directive (e.g. PACK alignment)",
    "include_sys":    "C system #include <header.h>",
    "include_local":  'C local #include "header.h"',
    "define":         "C #define macro or constant definition",
    "ifdef_block":    "C conditional compilation #ifdef / #ifndef",
    "undef":          "C #undef directive",
    "exec_sql":       "Pro*C embedded SQL EXEC SQL block",
    "rcs_tag":        "RCS/SCCS version control metadata tag (static char array with $Header)",
    "func_prototype": "C function prototype declaration with parameter types and semicolon",
    "func_def":       "C function definition with body",
    "var_decl":       "C variable declaration (int/char/float/etc, possibly with initializer)",
    "struct_def":     "C struct definition",
    "typedef":        "C typedef declaration",
    "enum_def":       "C enum definition",
    "malloc_call":    "C dynamic memory allocation: malloc()",
    "calloc_call":    "C dynamic memory allocation: calloc()",
    "realloc_call":   "C dynamic memory reallocation: realloc()",
    "free_call":      "C dynamic memory deallocation: free()",
    "memcpy_call":    "C memory copy: memcpy()",
    "memset_call":    "C memory set: memset()",
    "sizeof_expr":    "C sizeof() expression",
    "printf_call":    "C formatted output: printf()",
    "scanf_call":     "C formatted input: scanf()",
    "fprintf_call":   "C file formatted output: fprintf()",
    "sprintf_call":   "C string formatted output: sprintf()",
    "fopen_call":     "C file open: fopen()",
    "fclose_call":    "C file close: fclose()",
    "fread_call":     "C file read: fread()",
    "fwrite_call":    "C file write: fwrite()",
    "strcpy_call":    "C string operation (strcpy/strcat/strcmp/strlen/etc)",
    "if_else_block":  "C if/else conditional control flow block",
    "while_loop":     "C while loop",
    "for_loop":       "C for loop",
    "do_while":       "C do-while loop",
    "switch_block":   "C switch/case statement",
    "goto_stmt":      "C goto statement",
    "return_stmt":    "C return statement",
    "pointer_deref":  "C pointer dereference assignment (*ptr = value)",
    "increment_op":   "C increment/decrement operator (++ or --)",
    "pointer_arith":  "C pointer arithmetic or array indexing",
    "bitwise_op":     "C bitwise operator (&, |, ^, ~)",
    "modulo_op":      "C modulo operator (%)",
    "errno_check":    "C errno error variable check",
    "perror_call":    "C perror() error message print",
    "assert_call":    "C assert() runtime check",
}


def parse_source(source_code: str) -> list[RawPattern]:
    """
    Parse C/Pro*C source locally. Returns list of RawPattern.
    Source code is NEVER sent anywhere — only descriptions go to API.
    """
    lines = source_code.splitlines()
    seen_snippets: set[str] = set()
    patterns: list[RawPattern] = []
    pid = 0

    # De-duplicate and find line numbers
    def add(raw_type: str, snippet: str, start_pos: int, end_pos: int):
        nonlocal pid
        snippet = snippet.strip()
        # Truncate very long snippets for dedup key
        key = raw_type + ":" + snippet[:120]
        if key in seen_snippets:
            return
        seen_snippets.add(key)

        # Compute line range
        start_line = source_code[:start_pos].count("\n") + 1
        end_line   = source_code[:end_pos].count("\n") + 1

        pid += 1
        desc = _DESCRIPTIONS.get(raw_type, raw_type)
        patterns.append(RawPattern(
            id=pid,
            source_snippet=snippet[:400],   # kept local only
            line_range=[start_line, end_line],
            raw_type=raw_type,
            description=desc,
        ))

    for raw_type, regex in _RULES:
        for m in regex.finditer(source_code):
            add(raw_type, m.group(0), m.start(), m.end())

    # Sort by line
    patterns.sort(key=lambda p: p.line_range[0])
    # Re-number
    for i, p in enumerate(patterns):
        p.id = i + 1

    return patterns


def patterns_to_api_payload(patterns: list[RawPattern]) -> list[dict]:
    """
    Convert patterns to API-safe dicts.
    Contains ONLY: id, raw_type, description, line_range.
    NO source_snippet — firewall-safe.
    """
    return [
        {
            "id": p.id,
            "raw_type": p.raw_type,
            "description": p.description,
            "line_range": p.line_range,
        }
        for p in patterns
    ]


def patterns_to_full_dicts(patterns: list[RawPattern]) -> list[dict]:
    """Full dicts including source_snippet (local use only)."""
    return [
        {
            "id": p.id,
            "source_snippet": p.source_snippet,
            "line_range": p.line_range,
            "raw_type": p.raw_type,
            "description": p.description,
        }
        for p in patterns
    ]
