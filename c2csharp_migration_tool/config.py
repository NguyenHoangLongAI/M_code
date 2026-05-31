"""
C to C# Migration Tool - Configuration
"""

import os

# ─────────────────────────────────────────────
# Google Gemini API
# ─────────────────────────────────────────────
GEMINI_API_KEY = ""
GEMINI_MODEL   = "gemini-2.5-flash"          # hoặc gemini-1.5-pro
MAX_TOKENS     = 8192

# ─────────────────────────────────────────────
# Output paths
# ─────────────────────────────────────────────
OUTPUT_DIR         = "output"
PATTERN_CSV        = "patterns.csv"
CSHARP_FILE_SUFFIX = "_migrated.cs"

# ─────────────────────────────────────────────
# CSV columns
# ─────────────────────────────────────────────
CSV_COLUMNS = [
    "No",
    "Pattern_C_ProC",
    "Pattern_Type",
    "Pattern_SubType",
    "Pattern_Group",
    "Summary",
    "Pattern_CSharp",
    "Difficulty",
    "Migration_Strategy",
    "Risk_Level",
    "Risk_Strategy",
    "CSharp_Popularity",
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

DIFFICULTY_LEVELS = ["Dễ", "Trung bình", "Khó", "Rất khó"]

RISK_STRATEGIES = [
    "Auto convert",
    "Rules theo bối cảnh",
    "AI chủ động suggest",
    "Làm thủ công",
]

POPULARITY_SCALE = "x/5"