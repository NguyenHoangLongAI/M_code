"""
C to C# Migration Tool - Configuration
"""

import os

# ─────────────────────────────────────────────
# Google Gemini API
# ─────────────────────────────────────────────
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
GEMINI_MODEL   = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
MAX_TOKENS     = 32768

# ─────────────────────────────────────────────
# Output paths
# ─────────────────────────────────────────────
OUTPUT_DIR         = "output"
PATTERN_CSV        = "patterns.csv"
CSHARP_FILE_SUFFIX = "_migrated.cs"

# ─────────────────────────────────────────────
# CSV columns (Japanese)
# ─────────────────────────────────────────────
CSV_COLUMNS = [
    "番号",               # No
    "C_ProCパターン",     # Pattern_C_ProC
    "パターン種別",       # Pattern_Type
    "サブタイプ",         # Pattern_SubType
    "パターングループ",   # Pattern_Group
    "概要",               # Summary
    "C#パターン",         # Pattern_CSharp
    "難易度",             # Difficulty
    "移行戦略",           # Migration_Strategy
    "リスクレベル",       # Risk_Level
    "リスク戦略",         # Risk_Strategy
    "C#普及度",           # CSharp_Popularity
]

# ─────────────────────────────────────────────
# Pattern taxonomy
# ─────────────────────────────────────────────
PATTERN_TYPES = {
    "variable":     ["int", "char", "float", "double", "long", "short", "void", "static", "const", "extern", "volatile"],
    "array":        ["array", "pointer", "string", "buffer"],
    "struct":       ["struct", "union", "typedef", "enum"],
    "memory":       ["malloc", "calloc", "realloc", "free", "memcpy", "memset", "sizeof"],
    "io":           ["printf", "scanf", "fopen", "fclose", "fread", "fwrite", "fprintf", "fscanf", "sprintf", "sscanf"],
    "control_flow": ["if", "else", "switch", "case", "while", "for", "do", "break", "continue", "goto"],
    "function":     ["function_def", "function_call", "prototype", "return", "pointer_to_func"],
    "preprocessor": ["include", "define", "ifdef", "pragma", "undef"],
    "sql_proc":     ["EXEC SQL", "EXEC ORACLE", "cursor", "fetch", "commit", "rollback"],
    "error":        ["errno", "perror", "assert", "setjmp", "longjmp"],
    "operator":     ["arithmetic", "bitwise", "logical", "comparison", "pointer_op", "increment"],
    "string":       ["strcpy", "strcat", "strcmp", "strlen", "strncpy"],
    "metadata":     ["rcs_tag", "pragma_pack", "comment_block"],
}

# ─────────────────────────────────────────────
# Difficulty levels (Japanese)
# ─────────────────────────────────────────────
DIFFICULTY_LEVELS = ["易しい", "普通", "難しい", "非常に難しい"]

# ─────────────────────────────────────────────
# Risk levels (Japanese)
# ─────────────────────────────────────────────
RISK_LEVELS = ["低", "中", "高", "非常に高い"]

# ─────────────────────────────────────────────
# Risk strategies (Japanese)
# ─────────────────────────────────────────────
RISK_STRATEGIES = [
    "自動変換",         # Auto convert
    "コンテキスト依存", # Rules by context
    "AI提案",           # AI suggest
    "手動対応",         # Manual
]

POPULARITY_SCALE = "x/5"