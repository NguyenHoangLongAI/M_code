"""
server.py — C→C# Migration Local Server  v7.0
==============================================
v7 changes vs v6:
  - Agents 1-3 chạy parallel batches bên trong → tự động nhanh hơn N×
  - Thêm endpoint POST /api/migrate/batch — migrate nhiều files cùng lúc
  - Agent 4 = rule-based assembler (no LLM) → cs_line_start/cs_line_end
  - Neo4j persistence (giữ nguyên từ v6)
  - Thread-local log routing (giữ nguyên từ v5.3)
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

# ── Neo4j init ─────────────────────────────────────────────────
def _init_neo4j():
    try:
        from utils.neo4j_store import ensure_schema, check_connection
        status = check_connection()
        if status["ok"]:
            ensure_schema()
            builtins._orig_print(f"  [Neo4j] Connected → {status['uri']}")  # type: ignore
        else:
            builtins._orig_print(f"  [Neo4j] WARNING: {status['reason']}")  # type: ignore
    except Exception as e:
        builtins._orig_print(f"  [Neo4j] Init error: {e}")  # type: ignore

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
    _thread_local.log_queue = q

def _clear_thread_queue() -> None:
    _thread_local.log_queue = None

def _get_thread_queue() -> "queue.Queue | None":
    return getattr(_thread_local, "log_queue", None)

def _thread_print(*args, **kwargs) -> None:
    import builtins as _b
    _b._orig_print(*args, **kwargs)  # type: ignore[attr-defined]
    thread_q = _get_thread_queue()
    if thread_q is None:
        return
    msg = " ".join(str(a) for a in args)
    for marker, (step_num, step_label) in _STEP_MARKERS.items():
        if marker in msg:
            thread_q.put({"type": "step", "step": step_num, "label": step_label})
            break
    if "ERROR" in msg or "error" in msg.lower():
        level, tag = "error", "err"
    elif "WARNING" in msg or "warn" in msg.lower() or "✗" in msg:
        level, tag = "warn", "warn"
    elif any(x in msg for x in ("✓ Batch", "done (attempt", "Parallel done")):
        level, tag = "info", "batch_ok"
    elif "[API] Bedrock" in msg or "[API] Cache" in msg:
        level, tag = "info", "api"
    elif any(x in msg for x in ("[Agent1]", "[Agent2]", "[Agent3]", "[Agent4]", "[Agent4-NoLLM]")):
        level, tag = "info", "agent"
    elif any(x in msg for x in ("[Step ", "Pipeline", "Saved", "Written", "Generated")):
        level, tag = "info", "pipeline"
    elif any(x in msg for x in ("[DB]", "[Neo4j]", "[Batch]")):
        level, tag = "info", "db"
    elif "[CallGraph]" in msg or "[AutoCallGraph]" in msg:
        level, tag = "info", "callgraph"
    else:
        level, tag = "info", "misc"
    thread_q.put({"type": "log", "level": level, "tag": tag, "msg": msg.strip()})

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
            try:
                from utils.neo4j_store import save_call_graph_result
                save_call_graph_result(fname, pats)
            except Exception as neo_e:
                builtins._orig_print(f"[AutoCallGraph] Neo4j warning: {neo_e}")  # type: ignore
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
    try:
        from utils.neo4j_store import check_connection
        neo4j_status = check_connection()
    except Exception:
        neo4j_status = {"ok": False, "reason": "neo4j_store import error"}
    return jsonify({
        "status":     "ok",
        "model":      BEDROCK_MODEL_ID,
        "tests_dir":  str(TESTS_DIR),
        "file_count": len(flat),
        "key_pool":   pool_status(),
        "neo4j":      neo4j_status,
        "parallel":   True,   # v7 flag
    })


@app.route("/api/files")
def list_files():
    tree = _build_tree(TESTS_DIR, TESTS_DIR)
    if tree is None:
        tree = {"type": "dir", "name": "tests", "path": "", "children": []}
    flat = _flatten_tree(tree)
    return jsonify({"tree": tree, "flat": flat, "dir": str(TESTS_DIR)})


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
# DB ENDPOINTS — Neo4j  (unchanged from v6)
# ══════════════════════════════════════════════════════════════════

@app.route("/api/db/status")
def db_status():
    try:
        from utils.neo4j_store import check_connection, get_db_stats
        conn = check_connection()
        stats = get_db_stats() if conn["ok"] else {}
        return jsonify({"connection": conn, "stats": stats})
    except Exception as e:
        return jsonify({"connection": {"ok": False, "reason": str(e)}}), 500

@app.route("/api/db/files")
def db_list_files():
    try:
        from utils.neo4j_store import list_migrated_files
        files = list_migrated_files()
        return jsonify({"files": files, "count": len(files)})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/db/file", methods=["GET", "DELETE"])
def db_file():
    name = request.args.get("name", "").strip()
    if not name:
        return jsonify({"error": "Missing ?name="}), 400
    if request.method == "DELETE":
        try:
            from utils.neo4j_store import delete_file_result
            return jsonify(delete_file_result(name))
        except Exception as e:
            return jsonify({"error": str(e)}), 500
    try:
        from utils.neo4j_store import load_file_result
        result = load_file_result(name)
        if result is None:
            return jsonify({"error": f"Not found in DB: {name}"}), 404
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/db/callgraph")
def db_callgraph():
    name = request.args.get("name", "").strip()
    if not name:
        return jsonify({"error": "Missing ?name="}), 400
    try:
        from utils.neo4j_store import load_call_graph
        result = load_call_graph(name)
        if result is None:
            return jsonify({"error": f"No call graph in DB for: {name}"}), 404
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/db/stats")
def db_stats():
    try:
        from utils.neo4j_store import get_db_stats
        return jsonify(get_db_stats())
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/db/search")
def db_search():
    q          = request.args.get("q", "").strip()
    raw_type   = request.args.get("raw_type", "").strip()
    risk_level = request.args.get("risk_level", "").strip()
    limit      = int(request.args.get("limit", "100"))
    try:
        from utils.neo4j_store import search_patterns
        results = search_patterns(q, raw_type, risk_level, limit)
        return jsonify({"results": results, "count": len(results)})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ══════════════════════════════════════════════════════════════════
# /api/migrate  — SSE pipeline streaming (single file)
# ══════════════════════════════════════════════════════════════════

_STEP_MARKERS = {
    "[Step 0]": (0, "Reading source file"),
    "[Step 1]": (1, "Pattern Extraction"),
    "[Step 2]": (2, "Pattern Classification"),
    "[Step 3]": (3, "C# Translation"),
    "[Step 4]": (4, "C# File Assembly"),
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
        _set_thread_queue(q)
        try:
            from pipeline import run_pipeline
            t0      = time.time()
            result  = run_pipeline(str(fpath))
            elapsed = round(time.time() - t0, 1)
            safe_fname = Path(rel).name

            # Save to Neo4j
            try:
                from utils.neo4j_store import save_migration_result
                from config import OUTPUT_DIR, CSHARP_FILE_SUFFIX
                stem    = Path(safe_fname).stem
                cs_path = Path(OUTPUT_DIR) / f"{stem}{CSHARP_FILE_SUFFIX}"
                cs_text = cs_path.read_text(encoding="utf-8") if cs_path.exists() else ""
                source_content = ""
                try:
                    source_content = _read_file(fpath)
                except Exception:
                    pass
                db_res = save_migration_result(
                    file_path=name, file_name=safe_fname,
                    source_content=source_content,
                    patterns=result["patterns"], cs_text=cs_text, elapsed=elapsed,
                )
                print(f"[DB] Saved to Neo4j: {db_res}")
            except Exception as neo_e:
                print(f"[DB] WARNING: Neo4j save failed: {neo_e}")

            # Background call graph analysis
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
            q.put({"type": "error", "msg": str(exc), "trace": traceback.format_exc()})
        finally:
            _clear_thread_queue()

    threading.Thread(target=run, daemon=True).start()

    def stream():
        while True:
            try:
                event = q.get(timeout=600)   # v7: 10min timeout (parallel is faster)
            except queue.Empty:
                yield _sse({"type": "error", "msg": "Pipeline timeout (600s)"})
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
# /api/migrate/batch  — NEW v7: migrate multiple files, SSE progress
# ══════════════════════════════════════════════════════════════════

@app.route("/api/migrate/batch", methods=["POST"])
def migrate_batch():
    """
    POST /api/migrate/batch
    Body: {"files": ["subdir/A.c", "subdir/B.pc"], "max_concurrent": 3}

    SSE events:
      {"type": "file_start",  "file": "A.c"}
      {"type": "file_done",   "file": "A.c", "patterns": N, "elapsed": T}
      {"type": "file_error",  "file": "A.c", "msg": "..."}
      {"type": "log",         "msg": "..."}
      {"type": "done",        "files": [...], "total_elapsed": T}
      {"type": "error",       "msg": "..."}
    """
    body  = request.get_json(silent=True) or {}
    names = body.get("files", [])
    max_concurrent = min(int(body.get("max_concurrent", 3)), 5)  # cap at 5

    if not names:
        return jsonify({"error": "Missing 'files' list in request body"}), 400

    # Validate all paths first
    validated: list[tuple[str, Path]] = []
    for name in names:
        try:
            rel = _safe_name(name)
        except ValueError as e:
            return jsonify({"error": str(e)}), 400
        fpath = TESTS_DIR / rel
        if not fpath.exists():
            return jsonify({"error": f"File not found: {name}"}), 404
        if fpath.suffix.lower() == ".h":
            continue  # skip headers
        validated.append((name, fpath))

    if not validated:
        return jsonify({"error": "No valid source files found"}), 400

    q: "queue.Queue[dict]" = queue.Queue()

    def run_all():
        _set_thread_queue(q)
        from concurrent.futures import ThreadPoolExecutor, as_completed
        t0 = time.time()
        completed_files = []
        failed_files    = []

        def _migrate_one(name: str, fpath: Path) -> dict:
            """Run pipeline for one file, return result dict."""
            print(f"[Batch] Starting: {Path(name).name}")
            q.put({"type": "file_start", "file": Path(name).name})
            from pipeline import run_pipeline
            t_start = time.time()
            result = run_pipeline(str(fpath))
            elapsed = round(time.time() - t_start, 1)
            safe_fname = Path(name).name

            # Save to Neo4j
            try:
                from utils.neo4j_store import save_migration_result
                from config import OUTPUT_DIR, CSHARP_FILE_SUFFIX
                stem    = Path(safe_fname).stem
                cs_path = Path(OUTPUT_DIR) / f"{stem}{CSHARP_FILE_SUFFIX}"
                cs_text = cs_path.read_text(encoding="utf-8") if cs_path.exists() else ""
                source_content = ""
                try:
                    source_content = _read_file(fpath)
                except Exception:
                    pass
                save_migration_result(
                    file_path=name, file_name=safe_fname,
                    source_content=source_content,
                    patterns=result["patterns"], cs_text=cs_text, elapsed=elapsed,
                )
            except Exception as neo_e:
                print(f"[Batch/DB] Warning for {safe_fname}: {neo_e}")

            # Background call graph
            threading.Thread(
                target=_run_auto_callgraph,
                args=(safe_fname, result["patterns"]),
                daemon=True,
            ).start()

            q.put({
                "type":     "file_done",
                "file":     safe_fname,
                "patterns": len(result["patterns"]),
                "elapsed":  elapsed,
                "cs_path":  result["cs_path"],
                "csv_path": result["csv_path"],
            })
            return {"name": name, "safe_fname": safe_fname, "result": result}

        try:
            with ThreadPoolExecutor(max_workers=max_concurrent) as executor:
                futures = {
                    executor.submit(_migrate_one, name, fpath): name
                    for name, fpath in validated
                }
                for future in as_completed(futures):
                    name = futures[future]
                    try:
                        r = future.result()
                        completed_files.append(r["safe_fname"])
                    except Exception as exc:
                        failed_files.append(Path(name).name)
                        q.put({
                            "type": "file_error",
                            "file": Path(name).name,
                            "msg":  str(exc),
                        })

            total_elapsed = round(time.time() - t0, 1)
            q.put({
                "type":          "done",
                "files":         completed_files,
                "failed":        failed_files,
                "total_elapsed": total_elapsed,
            })
        except Exception as exc:
            q.put({"type": "error", "msg": str(exc), "trace": traceback.format_exc()})
        finally:
            _clear_thread_queue()

    threading.Thread(target=run_all, daemon=True).start()

    def stream():
        while True:
            try:
                event = q.get(timeout=600)
            except queue.Empty:
                yield _sse({"type": "error", "msg": "Batch timeout (600s)"})
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
                q.put({"type": "error", "msg": "No extracted patterns found. Run migration pipeline first."})
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
                q.put({"type": "error", "msg": "All specified files missing debug JSONs."})
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
                out_path.write_text(json.dumps(patterns, ensure_ascii=False, indent=2), encoding="utf-8")
                print(f"[CallGraph] Saved → {out_path.name}")
                try:
                    from utils.neo4j_store import save_call_graph_result
                    save_call_graph_result(fname, patterns)
                except Exception as ne:
                    print(f"[CallGraph] Neo4j update warning: {ne}")
            total_deps = sum(
                len(p.get("callees", [])) + len(p.get("callers", []))
                for pats in enriched.values()
                for p in pats
            )
            q.put({"type": "done", "files": list(enriched.keys()), "total_dependency_refs": total_deps})
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
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Access-Control-Allow-Origin": "*"},
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
                return jsonify({"error": f"No pipeline output for '{safe}'. Run migration first."}), 404
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
    builtins._orig_print("=" * 60)  # type: ignore
    builtins._orig_print(f"  C→C# Migration Server  v7.0  [Parallel Agents]")  # type: ignore
    builtins._orig_print(f"  Bedrock model : {BEDROCK_MODEL_ID}")  # type: ignore
    builtins._orig_print(f"  Listening     : http://0.0.0.0:5005")  # type: ignore
    builtins._orig_print(f"  Tests dir     : {TESTS_DIR}")  # type: ignore
    builtins._orig_print(f"  Files found   : {len(flat)}")  # type: ignore
    for f in flat:
        indent = "  " * (f["path"].count("/"))
        builtins._orig_print(f"    {indent}• {f['path']}")  # type: ignore
    builtins._orig_print()  # type: ignore
    builtins._orig_print("  Endpoints:")
    builtins._orig_print("    GET  /health")
    builtins._orig_print("    GET  /api/files")
    builtins._orig_print("    GET  /api/file?name=X.c")
    builtins._orig_print("    GET  /api/migrate?name=X.c          ← SSE, parallel agents")
    builtins._orig_print("    POST /api/migrate/batch              ← NEW: multi-file SSE")
    builtins._orig_print("    GET  /api/output?name=X.c")
    builtins._orig_print("    GET  /api/db/status | /files | /file | /stats | /search")
    builtins._orig_print("    POST /api/call-graph")
    builtins._orig_print("    GET  /api/call-graph/file?name=X.c")
    builtins._orig_print()  # type: ignore
    _init_neo4j()
    app.run(host="127.0.0.1", port=5005, debug=False, threaded=True)
