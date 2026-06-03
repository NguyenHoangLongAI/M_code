"""
server.py — C→C# Migration Local Server  v4
============================================
Endpoints
─────────
GET  /health                   → server + model status, file count
GET  /api/files                → list .c/.pc/.h files in tests/
GET  /api/file?name=X.c        → read raw file content
GET  /api/migrate?name=X.c     → run full pipeline, stream SSE progress
GET  /api/output?name=X.c      → return generated .cs file text
GET  /api/output/csv?name=X.c  → return generated _patterns.csv text

SSE event schema  (data: <JSON>)
─────────────────────────────────
  {"type":"step",  "step":0..5, "label":"..."}          → agent started
  {"type":"log",   "level":"info|warn|error", "msg":"..."} → log line
  {"type":"done",  "patterns":[...], "elapsed":N.N}     → pipeline finished
  {"type":"error", "msg":"...", "trace":"..."}           → fatal error

Usage
─────
    pip install google-genai flask flask-cors
    export GEMINI_API_KEY=AIza...
    python server.py
"""

import os
import sys
import json
import time
import queue
import threading
import traceback
import builtins
from pathlib import Path

from flask import Flask, request, jsonify, Response, stream_with_context
from flask_cors import CORS

# ── App setup ──────────────────────────────────────────────────
app = Flask(__name__)
CORS(app, resources={r"/*": {"origins": "*"}})

BASE_DIR  = Path(__file__).parent
TESTS_DIR = BASE_DIR / "tests"

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
GEMINI_MODEL   = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")

# ── Key pool init ─────────────────────────────────────────────
def _init_keys():
    """Load keys from keys.py (pool), fall back to env GEMINI_API_KEY."""
    from utils.key_pool import init_pool
    try:
        from keys import GEMINI_KEYS
        keys = [k for k in GEMINI_KEYS if k.strip()]
    except ImportError:
        keys = []

    # Also add env key if set and not already in list
    if GEMINI_API_KEY and GEMINI_API_KEY not in keys:
        keys.append(GEMINI_API_KEY)

    if not keys:
        print("ERROR: No Gemini API keys found.")
        print("  Option 1: Add keys to keys.py  (GEMINI_KEYS list)")
        print("  Option 2: export GEMINI_API_KEY=AIza...")
        sys.exit(1)

    init_pool(keys)

_init_keys()


# ══════════════════════════════════════════════════════════════════
# UTILS
# ══════════════════════════════════════════════════════════════════

def _sse(event: dict) -> str:
    """Encode a dict as a single SSE data line."""
    return f"data: {json.dumps(event, ensure_ascii=False)}\n\n"


def _safe_name(name: str) -> str:
    """Strip path components — only allow bare filename."""
    return Path(name).name


def _read_file(fpath: Path) -> str:
    for enc in ("utf-8", "shift_jis", "cp932", "latin-1"):
        try:
            return fpath.read_text(encoding=enc)
        except (UnicodeDecodeError, LookupError):
            continue
    raise ValueError(f"Cannot decode {fpath.name}")


# ══════════════════════════════════════════════════════════════════
# STATIC FILE ENDPOINTS
# ══════════════════════════════════════════════════════════════════

@app.route("/health")
def health():
    files = sorted(
        list(TESTS_DIR.glob("*.c")) +
        list(TESTS_DIR.glob("*.pc")) +
        list(TESTS_DIR.glob("*.pro"))
    )
    from utils.key_pool import status as pool_status
    return jsonify({
        "status":     "ok",
        "model":      GEMINI_MODEL,
        "tests_dir":  str(TESTS_DIR),
        "file_count": len(files),
        "key_pool":   pool_status(),
    })


@app.route("/api/files")
def list_files():
    exts  = ("*.c", "*.pc", "*.pro", "*.h")
    files = []
    for ext in exts:
        for p in sorted(TESTS_DIR.glob(ext)):
            stat = p.stat()
            files.append({
                "name":  p.name,
                "size":  stat.st_size,
                "lines": sum(1 for _ in p.open(encoding="utf-8", errors="replace")),
                "ext":   p.suffix,
            })
    return jsonify({"files": files, "dir": str(TESTS_DIR)})


@app.route("/api/file")
def read_file_ep():
    name  = request.args.get("name", "").strip()
    if not name:
        return jsonify({"error": "Missing ?name="}), 400
    fpath = TESTS_DIR / _safe_name(name)
    if not fpath.exists():
        return jsonify({"error": f"Not found: {name}"}), 404
    try:
        content = _read_file(fpath)
        return jsonify({"name": fpath.name, "content": content,
                        "size": fpath.stat().st_size})
    except ValueError as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/output")
def get_output_cs():
    """Return the generated .cs file for a given source name."""
    name = request.args.get("name", "").strip()
    if not name:
        return jsonify({"error": "Missing ?name="}), 400
    from config import OUTPUT_DIR, CSHARP_FILE_SUFFIX
    stem  = Path(_safe_name(name)).stem
    fpath = Path(OUTPUT_DIR) / f"{stem}{CSHARP_FILE_SUFFIX}"
    if not fpath.exists():
        return jsonify({"error": f"Output not found: {fpath.name}"}), 404
    return Response(fpath.read_text(encoding="utf-8"), mimetype="text/plain")


@app.route("/api/output/csv")
def get_output_csv():
    """Return the generated _patterns.csv for a given source name."""
    name = request.args.get("name", "").strip()
    if not name:
        return jsonify({"error": "Missing ?name="}), 400
    from config import OUTPUT_DIR
    stem  = Path(_safe_name(name)).stem
    fpath = Path(OUTPUT_DIR) / f"{stem}_patterns.csv"
    if not fpath.exists():
        return jsonify({"error": f"CSV not found: {fpath.name}"}), 404
    return Response(fpath.read_text(encoding="utf-8-sig"), mimetype="text/csv")


# ══════════════════════════════════════════════════════════════════
# /api/migrate  — SSE pipeline streaming
# ══════════════════════════════════════════════════════════════════

# Map of print-line keywords → (step_number, step_label)
# Used to auto-detect which pipeline step is running from print() output.
_STEP_MARKERS = {
    "[Step 0]": (0, "Reading source file"),
    "[Step 1]": (1, "Pattern Extraction"),
    "[Step 2]": (2, "Pattern Classification"),
    "[Step 3]": (3, "C# Translation"),
    "[Step 4]": (4, "C# File Generation"),
    "[Step 5]": (5, "Building Pattern Report"),
}


@app.route("/api/migrate")
def migrate():
    """
    Run the full Python pipeline for a file in tests/.
    Streams Server-Sent Events until done or error.
    """
    name = request.args.get("name", "").strip()
    if not name:
        return jsonify({"error": "Missing ?name="}), 400

    fpath = TESTS_DIR / _safe_name(name)
    if not fpath.exists():
        return jsonify({"error": f"File not found: {name}"}), 404

    # Each request gets its own queue
    q: "queue.Queue[dict]" = queue.Queue()

    # ── Pipeline thread ────────────────────────────────────────
    def run():
        # ── Intercept print() to push SSE log events ──────────
        _orig_print = builtins.print

        def _hook(*args, **kwargs):
            msg = " ".join(str(a) for a in args)
            _orig_print(*args, **kwargs)   # keep terminal output

            # Detect step markers first
            for marker, (step_num, step_label) in _STEP_MARKERS.items():
                if marker in msg:
                    q.put({"type": "step", "step": step_num, "label": step_label})
                    break

            # Always also emit as log line (filtered on client)
            level = ("error" if "ERROR" in msg or "error" in msg.lower()
                     else "warn"  if "WARNING" in msg or "warn" in msg.lower()
                     else "info")
            q.put({"type": "log", "level": level, "msg": msg.strip()})

        builtins.print = _hook

        try:
            from pipeline import run_pipeline
            t0     = time.time()
            result = run_pipeline(str(fpath))
            elapsed = round(time.time() - t0, 1)

            q.put({
                "type":     "done",
                "patterns": result["patterns"],   # full migration_data list
                "elapsed":  elapsed,
                "cs_path":  result["cs_path"],
                "csv_path": result["csv_path"],
            })

        except Exception as exc:
            q.put({
                "type":  "error",
                "msg":   str(exc),
                "trace": traceback.format_exc(),
            })
        finally:
            builtins.print = _orig_print   # always restore

    t = threading.Thread(target=run, daemon=True)
    t.start()

    # ── SSE generator ──────────────────────────────────────────
    def stream():
        while True:
            try:
                event = q.get(timeout=180)   # 3-min hard cap per event
            except queue.Empty:
                yield _sse({"type": "error", "msg": "Pipeline timeout (180 s)"})
                return

            yield _sse(event)

            if event["type"] in ("done", "error"):
                return

    return Response(
        stream_with_context(stream()),
        mimetype="text/event-stream",
        headers={
            "Cache-Control":               "no-cache",
            "X-Accel-Buffering":           "no",
            "Access-Control-Allow-Origin": "*",
        },
    )


# ══════════════════════════════════════════════════════════════════
# ENTRY POINT
# ══════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    c_files = (list(TESTS_DIR.glob("*.c")) +
               list(TESTS_DIR.glob("*.pc")) +
               list(TESTS_DIR.glob("*.pro")))

    print("=" * 60)
    print(f"  C→C# Migration Server  [Gemini: {GEMINI_MODEL}]")
    print(f"  Listening  : http://127.0.0.1:5005")
    print(f"  Tests dir  : {TESTS_DIR}")
    print(f"  Files found: {len(c_files)}")
    for f in sorted(c_files):
        print(f"    • {f.name}")
    print()
    print("  Endpoints:")
    print("    GET /health")
    print("    GET /api/files")
    print("    GET /api/file?name=X.c")
    print("    GET /api/migrate?name=X.c    ← SSE pipeline stream")
    print("    GET /api/output?name=X.c     ← generated .cs")
    print("    GET /api/output/csv?name=X.c ← generated CSV")
    print("=" * 60)

    app.run(host="127.0.0.1", port=5005, debug=False, threaded=True)
