"""
main.py
CLI entry point — AWS Bedrock / Claude Opus 4.5
"""
import sys
import argparse
from pathlib import Path
from config import OUTPUT_DIR

# ── Bedrock credential init ────────────────────────────────────
from utils.key_pool import init_pool
init_pool()
# ──────────────────────────────────────────────────────────────


def parse_args():
    parser = argparse.ArgumentParser(
        prog="c2csharp",
        description="Migrate C / Pro*C source to C# with pattern analysis (AWS Bedrock / Claude Opus 4.5).",
    )
    parser.add_argument("source", help="Path to the C or Pro*C source file (.c, .h, .pc)")
    parser.add_argument("--output", "-o", default=OUTPUT_DIR,
                        help=f"Output directory (default: {OUTPUT_DIR})")
    return parser.parse_args()


def main():
    args = parse_args()

    source_path = Path(args.source)
    if not source_path.is_file():
        script_dir = Path(__file__).parent
        alt = script_dir / args.source
        if alt.is_file():
            source_path = alt
        else:
            print(f"ERROR: Source file not found: {args.source}")
            sys.exit(1)

    from pipeline import run_pipeline
    run_pipeline(str(source_path), output_dir=args.output)
    return 0


if __name__ == "__main__":
    sys.exit(main())
