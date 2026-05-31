"""
server.py — C→C# Migration Local Proxy Server
Gemini backend + file serving từ thư mục tests/

Endpoints:
  GET  /health                    → server status
  GET  /api/files                 → list .c/.pc files in tests/
  GET  /api/file?name=X.c         → read file content
  POST /api/chat                  → proxy to Gemini API (legacy / direct use)
  GET  /api/migrate?name=X.c      → run full Python pipeline, SSE progress stream

Cách dùng:
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
from pathlib import Path

from flask import Flask, request, jsonify, Response, stream_with_context
from flask_cors import CORS
from google import genai
from google.genai import types

app = Flask(__name__)
CORS(app)

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
GEMINI_MODEL   = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")

BASE_DIR  = Path(__file__).parent
TESTS_DIR = BASE_DIR / "tests"

if not GEMINI_API_KEY:
    print("ERROR: GEMINI_API_KEY chưa được set.")
    exit(1)

client = genai.Client(api_key=GEMINI_API_KEY)


# ── File endpoints ─────────────────────────────────────────────

@app.route("/api/files", methods=["GET"])
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


@app.route("/api/file", methods=["GET"])
def read_file():
    name = request.args.get("name", "").strip()
    if not name:
        return jsonify({"error": "Missing ?name= parameter"}), 400

    safe  = Path(name).name
    fpath = TESTS_DIR / safe

    if not fpath.exists():
        return jsonify({"error": f"File not found: {safe}"}), 404

    for enc in ("utf-8", "shift_jis", "cp932", "latin-1"):
        try:
            content = fpath.read_text(encoding=enc)
            return jsonify({
                "name": safe, "content": content,
                "encoding": enc, "size": fpath.stat().st_size,
            })
        except (UnicodeDecodeError, LookupError):
            continue

    return jsonify({"error": "Cannot decode file"}), 500


# ── Gemini direct proxy (legacy) ───────────────────────────────

@app.route("/api/chat", methods=["POST"])
def chat():
    try:
        body       = request.get_json()
        system     = body.get("system", "")
        user_msg   = body.get("user", "")
        max_tokens = body.get("max_tokens", 8192)

        if not user_msg:
            for m in body.get("messages", []):
                if m.get("role") == "user":
                    user_msg = m.get("content", "")

        response = client.models.generate_content(
            model=GEMINI_MODEL,
            contents=user_msg,
            config=types.GenerateContentConfig(
                system_instruction=system if system else None,
                max_output_tokens=max_tokens,
                temperature=0.2,
            ),
        )
        return jsonify({"text": response.text})

    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ── Migration pipeline endpoint (SSE) ─────────────────────────

@app.route("/api/migrate", methods=["GET"])
def migrate():
    """
    Run the full Python pipeline for a file.
    Streams Server-Sent Events:

      data: {"type": "step",    "step": 1, "msg": "..."}
      data: {"type": "log",     "msg": "..."}
      data: {"type": "done",    "patterns": [...], "cs": "..."}
      data: {"type": "error",   "msg": "..."}
    """
    name = request.args.get("name", "").strip()
    if not name:
        return jsonify({"error": "Missing ?name= parameter"}), 400

    safe  = Path(name).name
    fpath = TESTS_DIR / safe
    if not fpath.exists():
        return jsonify({"error": f"File not found: {safe}"}), 404

    # Queue for pipeline thread → SSE generator
    q: queue.Queue = queue.Queue()

    def pipeline_thread():
        """Run pipeline in background thread, push events onto queue."""
        try:
            # ── Dynamically import pipeline so server starts even if
            #    dependencies aren't installed yet.
            try:
                from pipeline import run_pipeline
            except ImportError as ie:
                q.put({"type": "error", "msg": f"Import error: {ie}"})
                return

            source_path = str(fpath)

            # Monkey-patch print to also emit log SSE events
            original_print = __builtins__["print"] if isinstance(__builtins__, dict) else print

            import builtins
            _orig_print = builtins.print

            def _sse_print(*args, **kwargs):
                msg = " ".join(str(a) for a in args)
                q.put({"type": "log", "msg": msg})
                _orig_print(*args, **kwargs)          # keep terminal output

            builtins.print = _sse_print

            # ── Step progress markers ──────────────────────────
            STEP_MARKERS = {
                "[Step 0]": (0, "Reading source file"),
                "[Step 1]": (1, "Pattern Extraction"),
                "[Step 2]": (2, "Pattern Classification"),
                "[Step 3]": (3, "C# Translation"),
                "[Step 4]": (4, "C# File Generation"),
                "[Step 5]": (5, "Building Pattern Report"),
            }

            # Wrap run_pipeline with step-detection via queue interceptor
            original_q_put = q.put

            def _smart_put(event):
                if event.get("type") == "log":
                    msg = event["msg"]
                    for marker, (step_num, step_name) in STEP_MARKERS.items():
                        if marker in msg:
                            original_q_put({
                                "type": "step",
                                "step": step_num,
                                "msg":  step_name,
                            })
                            break
                original_q_put(event)

            q.put = _smart_put

            try:
                result = run_pipeline(source_path)
            finally:
                builtins.print = _orig_print   # always restore
                q.put = original_q_put          # restore queue.put

            # ── Send final result ──────────────────────────────
            q.put({
                "type":     "done",
                "patterns": result["patterns"],
                "cs":       result["cs_path"],   # path on disk
                "csv":      result["csv_path"],
            })

        except Exception as exc:
            import traceback
            q.put({"type": "error", "msg": str(exc), "trace": traceback.format_exc()})

    # Start pipeline in background
    t = threading.Thread(target=pipeline_thread, daemon=True)
    t.start()

    def event_stream():
        """Generator: pull from queue, yield SSE lines."""
        while True:
            try:
                event = q.get(timeout=120)   # 2-min hard timeout per event
            except queue.Empty:
                yield "data: {\"type\": \"error\", \"msg\": \"Pipeline timeout\"}\n\n"
                return

            payload = json.dumps(event, ensure_ascii=False)
            yield f"data: {payload}\n\n"

            if event.get("type") in ("done", "error"):
                return

    return Response(
        stream_with_context(event_stream()),
        mimetype="text/event-stream",
        headers={
            "Cache-Control":               "no-cache",
            "X-Accel-Buffering":           "no",      # nginx: disable buffering
            "Access-Control-Allow-Origin": "*",
        },
    )


# ── C# file download ───────────────────────────────────────────

@app.route("/api/output/cs", methods=["GET"])
def download_cs():
    """Return generated .cs file content."""
    name = request.args.get("name", "").strip()
    if not name:
        return jsonify({"error": "Missing ?name="}), 400

    from config import OUTPUT_DIR, CSHARP_FILE_SUFFIX
    stem   = Path(name).stem
    fpath  = Path(OUTPUT_DIR) / f"{stem}{CSHARP_FILE_SUFFIX}"

    if not fpath.exists():
        return jsonify({"error": f"Not found: {fpath}"}), 404

    return Response(
        fpath.read_text(encoding="utf-8"),
        mimetype="text/plain",
        headers={"Content-Disposition": f'attachment; filename="{fpath.name}"'},
    )


# ── Health ─────────────────────────────────────────────────────

@app.route("/health", methods=["GET"])
def health():
    files = list(TESTS_DIR.glob("*.c")) + list(TESTS_DIR.glob("*.pc"))
    return jsonify({
        "status":     "ok",
        "model":      GEMINI_MODEL,
        "tests_dir":  str(TESTS_DIR),
        "file_count": len(files),
    })


# ── Entry point ────────────────────────────────────────────────

if __name__ == "__main__":
    c_files = list(TESTS_DIR.glob("*.c")) + list(TESTS_DIR.glob("*.pc"))
    print("=" * 60)
    print(f"  C→C# Migration Server  [Gemini: {GEMINI_MODEL}]")
    print(f"  Listening  : http://127.0.0.1:5005")
    print(f"  Tests dir  : {TESTS_DIR}")
    print(f"  Files found: {len(c_files)}")
    for f in c_files:
        print(f"    • {f.name}")
    print("=" * 60)
    app.run(host="127.0.0.1", port=5005, debug=False, threaded=True)