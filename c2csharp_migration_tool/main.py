"""
main.py
CLI entry point — Gemini backend.

Usage:
    python main.py <source_file.c>  [--output <dir>]

Examples:
    python main.py tests/SjlComFunc.c
    python main.py tests/SjlComFunc.c --output results/

Requirements:
    pip install google-genai flask flask-cors
    export GEMINI_API_KEY=AIza...
"""
import sys
import argparse
from pathlib import Path
from config import OUTPUT_DIR, GEMINI_API_KEY


def parse_args():
    parser = argparse.ArgumentParser(
        prog="c2csharp",
        description="Migrate C / Pro*C source to C# with pattern analysis (Gemini).",
    )
    parser.add_argument("source", help="Path to the C or Pro*C source file (.c, .h, .pc)")
    parser.add_argument("--output", "-o", default=OUTPUT_DIR,
                        help=f"Output directory (default: {OUTPUT_DIR})")
    return parser.parse_args()


def main():
    args = parse_args()

    if not GEMINI_API_KEY:
        print("ERROR: GEMINI_API_KEY environment variable is not set.")
        print("  export GEMINI_API_KEY=AIza...")
        sys.exit(1)

    source_path = Path(args.source)
    if not source_path.is_file():
        script_dir = Path(__file__).parent
        alt = script_dir / args.source
        if alt.is_file():
            source_path = alt
        else:
            print(f"ERROR: Source file not found: {args.source}")
            print(f"  CWD       : {Path.cwd()}")
            print(f"  Tried     : {alt}")
            print(f"  Hint      : python main.py tests/SjlComFunc.c")
            sys.exit(1)

    from pipeline import run_pipeline
    run_pipeline(str(source_path), output_dir=args.output)
    return 0


if __name__ == "__main__":
    sys.exit(main())
