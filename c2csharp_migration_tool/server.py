"""
server.py — C→C# Migration Local Server  v5.3
==============================================
Changes vs v5.2:
  - Fix parallel migration log mixing: dùng thread-local queue thay vì
    hook builtins.print globally. Mỗi pipeline thread có queue riêng,
    log được route đúng file, không bị lộn xộn khi chạy song song.
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

# ── Thread-local storage for per-pipeline log routing ─────────
_thread_local = threading.local()

# ── Key pool init ─────────────────────────────────────────────
def _init_keys():
    from utils.key_pool import init_pool
    init_pool()

_init_keys()

# ── Call graph global state ────────────────────────────────────
_call_graph_cache:   dict[str, list[dict]] = {}
_call_graph_pending: set[str]              = set()
_call_graph_lock     = threading.Lock()


# ══════════════════════════════════════════════════════════════════
# UTILS
# ══════════════════════════════════════════════════════════════════

def _sse(event: dict) -> str:
    return f"data: {json.dumps(event, ensure_ascii=False)}\n\n"


def _safe_name(name: str) -> str:
    """
    Allow relative paths within tests/ (e.g. "subdir/File.c").
    Prevents path traversal outside tests/.
    """
    clean    = Path(name).as_posix().lstrip("/")
    resolved = (TESTS_DIR / clean).resolve()
    try:
        resolved.relative_to(TESTS_DIR.resolve())
    except ValueError:
        raise ValueError(f"Path traversal attempt: {name}")
    return clean


def _read_file(fpath: Path) -> str:
    for enc in ("utf-8", "shift_jis", "cp932", "latin-1"):
        try:
            return fpath.read_text(encoding=enc)
        except (UnicodeDecodeError, LookupError):
            continue
    raise ValueError(f"Cannot decode {fpath.name}")


# ══════════════════════════════════════════════════════════════════
# THREAD-LOCAL LOG ROUTER
# ══════════════════════════════════════════════════════════════════

def _set_thread_queue(q: queue.Queue) -> None:
    """Gắn queue vào thread hiện tại."""
    _thread_local.log_queue = q


def _clear_thread_queue() -> None:
    """Xoá queue khỏi thread hiện tại."""
    _thread_local.log_queue = None


def _get_thread_queue() -> "queue.Queue | None":
    """Lấy queue của thread hiện tại (None nếu chưa set)."""
    return getattr(_thread_local, "log_queue", None)


def _thread_print(*args, **kwargs) -> None:
    """
    Hàm print thay thế: ghi vào queue của thread hiện tại.
    Vẫn in ra stdout bình thường.
    Nếu thread không có queue (e.g. call-graph thread) → chỉ in stdout.
    """
    import builtins as _b
    _b._orig_print(*args, **kwargs)   # type: ignore[attr-defined]

    thread_q = _get_thread_queue()
    if thread_q is None:
        return

    msg = " ".join(str(a) for a in args)

    for marker, (step_num, step_label) in _STEP_MARKERS.items():
        if marker in msg:
            thread_q.put({"type": "step", "step": step_num, "label": step_label})
            break

    level = ("error" if "ERROR" in msg or "error" in msg.lower()
             else "warn"  if "WARNING" in msg or "warn" in msg.lower()
             else "info")
    thread_q.put({"type": "log", "level": level, "msg": msg.strip()})


# Patch builtins.print một lần duy nhất khi server khởi động
# (không patch/unpatch lại trong mỗi thread → tránh race condition)
builtins._orig_print = builtins.print   # type: ignore[attr-defined]
builtins.print = _thread_print          # type: ignore[attr-defined]


# ══════════════════════════════════════════════════════════════════
# DIRECTORY TREE BUILDER
# ══════════════════════════════════════════════════════════════════

_SOURCE_EXTS = {".c", ".pc", ".pro", ".h"}

def _count_lines(fpath: Path) -> int:
    try:
        count = 0
        with fpath.open(encoding="utf-8", errors="replace") as f:
            for _ in f:
                count += 1
        return count
    except Exception:
        return 0


def _build_tree(directory: Path, relative_to: Path) -> dict:
    """
    Recursively build a tree node for `directory`.
    Only includes files with extensions in _SOURCE_EXTS.
    Empty directories (no source files anywhere) are omitted.
    """
    children = []

    try:
        entries = sorted(directory.iterdir(), key=lambda p: (p.is_file(), p.name.lower()))
    except PermissionError:
        return None

    for entry in entries:
        if entry.name.startswith(".") or entry.name == "__pycache__":
            continue

        rel_path = entry.relative_to(relative_to).as_posix()

        if entry.is_dir():
            subtree = _build_tree(entry, relative_to)
            if subtree is not None:
                children.append(subtree)

        elif entry.is_file() and entry.suffix.lower() in _SOURCE_EXTS:
            stat = entry.stat()
            children.append({
                "type":  "file",
                "name":  entry.name,
                "path":  rel_path,
                "size":  stat.st_size,
                "lines": _count_lines(entry),
                "ext":   entry.suffix.lower(),
            })

    if not children:
        return None

    return {
        "type":     "dir",
        "name":     directory.name,
        "path":     directory.relative_to(relative_to).as_posix() if directory != relative_to else "",
        "children": children,
    }


def _flatten_tree(node: dict, result: list = None) -> list:
    """Flatten a tree into a list of file nodes only."""
    if result is None:
        result = []
    if node["type"] == "file":
        result.append(node)
    else:
        for child in node.get("children", []):
            _flatten_tree(child, result)
    return result


# ══════════════════════════════════════════════════════════════════
# AUTO CALL GRAPH
# ══════════════════════════════════════════════════════════════════

def _run_auto_callgraph(filename: str, patterns: list[dict]) -> None:
    from config import OUTPUT_DIR

    safe = Path(filename).name

    with _call_graph_lock:
        if safe in _call_graph_pending:
            return
        _call_graph_pending.add(safe)

    # Call-graph thread không set thread_q → print chỉ ra stdout
    builtins._orig_print(f"[AutoCallGraph] Starting background analysis for {safe} ...")  # type: ignore
    try:
        from utils.call_graph import analyze_dependencies

        all_file_patterns: dict[str, list[dict]] = {safe: patterns}
        enriched = analyze_dependencies(all_file_patterns, TESTS_DIR)

        with _call_graph_lock:
            _call_graph_cache.update(enriched)

        for fname, pats in enriched.items():
            stem     = Path(fname).stem
            out_path = Path(OUTPUT_DIR) / f"_debug_callgraph_{stem}.json"
            out_path.write_text(
                json.dumps(pats, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            builtins._orig_print(f"[AutoCallGraph] Saved → {out_path.name}")  # type: ignore

    except Exception as exc:
        builtins._orig_print(f"[AutoCallGraph] WARNING for {safe}: {exc}")  # type: ignore
        traceback.print_exc()
    finally:
        with _call_graph_lock:
            _call_graph_pending.discard(safe)

    builtins._orig_print(f"[AutoCallGraph] Done for {safe}.")  # type: ignore


# ══════════════════════════════════════════════════════════════════
# STATIC FILE ENDPOINTS
# ══════════════════════════════════════════════════════════════════

@app.route("/health")
def health():
    tree = _build_tree(TESTS_DIR, TESTS_DIR)
    flat = _flatten_tree(tree) if tree else []
    from utils.key_pool import status as pool_status
    from config import BEDROCK_MODEL_ID
    return jsonify({
        "status":     "ok",
        "model":      BEDROCK_MODEL_ID,
        "tests_dir":  str(TESTS_DIR),
        "file_count": len(flat),
        "key_pool":   pool_status(),
    })


@app.route("/api/files")
def list_files():
    tree = _build_tree(TESTS_DIR, TESTS_DIR)
    if tree is None:
        tree = {"type": "dir", "name": "tests", "path": "", "children": []}

    flat = _flatten_tree(tree)

    return jsonify({
        "tree": tree,
        "flat": flat,
        "dir":  str(TESTS_DIR),
    })


@app.route("/api/file")
def read_file_ep():
    name = request.args.get("name", "").strip()
    if not name:
        return jsonify({"error": "Missing ?name="}), 400
    try:
        rel = _safe_name(name)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

    fpath = TESTS_DIR / rel
    if not fpath.exists():
        return jsonify({"error": f"Not found: {name}"}), 404
    try:
        content = _read_file(fpath)
        return jsonify({"name": fpath.name, "path": rel,
                        "content": content, "size": fpath.stat().st_size})
    except ValueError as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/output")
def get_output_cs():
    name = request.args.get("name", "").strip()
    if not name:
        return jsonify({"error": "Missing ?name="}), 400
    from config import OUTPUT_DIR, CSHARP_FILE_SUFFIX
    stem  = Path(name).stem
    fpath = Path(OUTPUT_DIR) / f"{stem}{CSHARP_FILE_SUFFIX}"
    if not fpath.exists():
        return jsonify({"error": f"Output not found: {fpath.name}"}), 404
    return Response(fpath.read_text(encoding="utf-8"), mimetype="text/plain")


@app.route("/api/output/csv")
def get_output_csv():
    name = request.args.get("name", "").strip()
    if not name:
        return jsonify({"error": "Missing ?name="}), 400
    from config import OUTPUT_DIR
    stem  = Path(name).stem
    fpath = Path(OUTPUT_DIR) / f"{stem}_patterns.csv"
    if not fpath.exists():
        return jsonify({"error": f"CSV not found: {fpath.name}"}), 404
    return Response(fpath.read_text(encoding="utf-8-sig"), mimetype="text/csv")


# ══════════════════════════════════════════════════════════════════
# /api/migrate  — SSE pipeline streaming
# ══════════════════════════════════════════════════════════════════

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
    name = request.args.get("name", "").strip()
    if not name:
        return jsonify({"error": "Missing ?name="}), 400

    try:
        rel = _safe_name(name)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

    fpath = TESTS_DIR / rel
    if not fpath.exists():
        return jsonify({"error": f"File not found: {name}"}), 404

    q: "queue.Queue[dict]" = queue.Queue()

    def run():
        # ── Gắn queue riêng vào thread này ────────────────────
        _set_thread_queue(q)
        try:
            from pipeline import run_pipeline
            t0      = time.time()
            result  = run_pipeline(str(fpath))
            elapsed = round(time.time() - t0, 1)

            safe_fname = Path(rel).name
            threading.Thread(
                target=_run_auto_callgraph,
                args=(safe_fname, result["patterns"]),
                daemon=True,
            ).start()

            q.put({
                "type":     "done",
                "patterns": result["patterns"],
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
            # ── Xoá queue → thread này không nhận log nữa ─────
            _clear_thread_queue()

    threading.Thread(target=run, daemon=True).start()

    def stream():
        while True:
            try:
                event = q.get(timeout=180)
            except queue.Empty:
                yield _sse({"type": "error", "msg": "Pipeline timeout (180s)"})
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
# /api/call-graph  — cross-file dependency analysis (SSE)
# ══════════════════════════════════════════════════════════════════

@app.route("/api/call-graph", methods=["GET", "POST"])
def run_call_graph():
    body            = request.get_json(silent=True) or {}
    requested_files = body.get("files", [])

    q: "queue.Queue[dict]" = queue.Queue()

    def run():
        # Call-graph cũng set thread_q để log xuất hiện ở UI
        _set_thread_queue(q)
        try:
            from utils.call_graph import analyze_dependencies
            from config import OUTPUT_DIR

            stem_to_file: dict[str, str] = {}
            for ext in _SOURCE_EXTS:
                for p in TESTS_DIR.rglob(f"*{ext}"):
                    stem_to_file[p.stem] = p.name

            if requested_files:
                scan_files = [Path(f).name for f in requested_files]
            else:
                scan_files = []
                for p in Path(OUTPUT_DIR).glob("_debug_3_*.json"):
                    stem   = p.name.replace("_debug_3_", "").replace(".json", "")
                    actual = stem_to_file.get(stem, stem + ".c")
                    scan_files.append(actual)

            if not scan_files:
                q.put({"type": "error",
                       "msg": "No extracted patterns found. Run migration pipeline first."})
                return

            print(f"[CallGraph] Loading patterns for {len(scan_files)} file(s) ...")

            all_file_patterns: dict[str, list[dict]] = {}
            for fname in scan_files:
                stem       = Path(fname).stem
                debug_path = Path(OUTPUT_DIR) / f"_debug_3_{stem}.json"
                if not debug_path.exists():
                    print(f"[CallGraph] Skipping {fname}: no debug JSON found")
                    continue
                patterns = json.loads(debug_path.read_text(encoding="utf-8"))
                if patterns:
                    all_file_patterns[fname] = patterns
                    print(f"[CallGraph] Loaded {len(patterns)} patterns from {fname}")

            if not all_file_patterns:
                q.put({"type": "error",
                       "msg": "All specified files missing debug JSONs. Run pipeline first."})
                return

            print("[CallGraph] Starting dependency analysis ...")

            enriched = analyze_dependencies(all_file_patterns, TESTS_DIR)

            with _call_graph_lock:
                _call_graph_cache.update(enriched)
                for f in enriched:
                    _call_graph_pending.discard(f)

            for fname, patterns in enriched.items():
                stem     = Path(fname).stem
                out_path = Path(OUTPUT_DIR) / f"_debug_callgraph_{stem}.json"
                out_path.write_text(
                    json.dumps(patterns, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
                print(f"[CallGraph] Saved → {out_path.name}")

            total_deps = sum(
                len(p.get("callees", [])) + len(p.get("callers", []))
                for pats in enriched.values()
                for p in pats
            )

            q.put({
                "type":                  "done",
                "files":                 list(enriched.keys()),
                "total_dependency_refs": total_deps,
            })

        except Exception as exc:
            q.put({"type": "error", "msg": str(exc), "trace": traceback.format_exc()})
        finally:
            _clear_thread_queue()

    threading.Thread(target=run, daemon=True).start()

    def stream():
        while True:
            try:
                event = q.get(timeout=300)
            except queue.Empty:
                yield _sse({"type": "error", "msg": "Call graph timeout (300s)"})
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
# /api/call-graph/file
# ══════════════════════════════════════════════════════════════════

@app.route("/api/call-graph/file")
def get_call_graph_file():
    name = request.args.get("name", "").strip()
    if not name:
        return jsonify({"error": "Missing ?name="}), 400

    safe = Path(name).name

    with _call_graph_lock:
        patterns   = _call_graph_cache.get(safe)
        is_pending = safe in _call_graph_pending

    if patterns is None and is_pending:
        return jsonify({"status": "pending", "file": safe}), 202

    if patterns is None:
        from config import OUTPUT_DIR
        stem       = Path(safe).stem
        debug_path = Path(OUTPUT_DIR) / f"_debug_callgraph_{stem}.json"
        if debug_path.exists():
            patterns = json.loads(debug_path.read_text(encoding="utf-8"))
            with _call_graph_lock:
                _call_graph_cache[safe] = patterns
        else:
            pipeline_json = Path(OUTPUT_DIR) / f"_debug_3_{stem}.json"
            if not pipeline_json.exists():
                return jsonify({
                    "error": f"No pipeline output for '{safe}'. Run migration pipeline first."
                }), 404

            pipeline_patterns = json.loads(pipeline_json.read_text(encoding="utf-8"))
            with _call_graph_lock:
                already_pending = safe in _call_graph_pending

            if not already_pending:
                threading.Thread(
                    target=_run_auto_callgraph,
                    args=(safe, pipeline_patterns),
                    daemon=True,
                ).start()

            return jsonify({"status": "pending", "file": safe}), 202

    summary = {
        "patterns_with_deps": sum(1 for p in patterns if p.get("callees") or p.get("callers")),
        "total_callers":  sum(len(p.get("callers",  [])) for p in patterns),
        "total_callees":  sum(len(p.get("callees", [])) for p in patterns),
    }

    return jsonify({"file": safe, "patterns": patterns, "summary": summary})


# ══════════════════════════════════════════════════════════════════
# ENTRY POINT
# ══════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    from config import BEDROCK_MODEL_ID

    tree = _build_tree(TESTS_DIR, TESTS_DIR)
    flat = _flatten_tree(tree) if tree else []

    builtins._orig_print("=" * 60)                                          # type: ignore
    builtins._orig_print(f"  C→C# Migration Server  v5.3  [Bedrock: {BEDROCK_MODEL_ID}]")  # type: ignore
    builtins._orig_print(f"  Listening  : http://127.0.0.1:5005")           # type: ignore
    builtins._orig_print(f"  Tests dir  : {TESTS_DIR}")                     # type: ignore
    builtins._orig_print(f"  Files found: {len(flat)}")                     # type: ignore
    for f in flat:
        indent = "  " * (f["path"].count("/"))
        builtins._orig_print(f"    {indent}• {f['path']}")                  # type: ignore
    builtins._orig_print()                                                   # type: ignore
    builtins._orig_print("  Endpoints:")
    builtins._orig_print("    GET  /health")
    builtins._orig_print("    GET  /api/files")
    builtins._orig_print("    GET  /api/file?name=subdir/X.c")
    builtins._orig_print("    GET  /api/migrate?name=subdir/X.c  ← SSE pipeline stream")
    builtins._orig_print("    GET  /api/output?name=X.c")
    builtins._orig_print("    GET  /api/output/csv?name=X.c")
    builtins._orig_print("    POST /api/call-graph               ← SSE cross-file analysis")
    builtins._orig_print("    GET  /api/call-graph/file?name=X.c")
    builtins._orig_print()
    builtins._orig_print("  Log routing: thread-local queue (v5.3) — parallel-safe.")
    builtins._orig_print("=" * 60)                                          # type: ignore

    app.run(host="127.0.0.1", port=5005, debug=False, threade
