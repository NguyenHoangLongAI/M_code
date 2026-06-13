"""
utils/neo4j_store.py — Neo4j Persistence Layer  v2.0
=====================================================
v2.0 changes (on top of v1.1):
  - Pattern nodes gain two new fields:
      func_calls   : JSON array of function names where func_kind == "call"
      func_creates : JSON array of function names where func_kind == "create"
  - CALLS relationship gains:
      func_kind    : "call" | "create" | "system"
  - _save_call_edges: stores func_kind on each edge
  - load_call_graph: returns func_kind from relationship properties
  - _pattern_to_props: includes func_calls / func_creates
  - search_patterns: can filter by func_kind on edges

v1.1 changes:
  - _save_call_edges: lưu thêm definition_pattern_id + definition_file vào CALLS edge
  - load_call_graph: trả về definition fields từ relationship properties

Schema:
  (MigrationFile)-[:HAS_PATTERN]->(Pattern)
  (MigrationFile)-[:HAS_OUTPUT]->(CSharpOutput)
  (Pattern)-[:CALLS {kind, func_kind, callee_name, call_line,
                     definition_pattern_id, definition_file}]->(Pattern)
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

try:
    from neo4j import GraphDatabase, exceptions as neo4j_exc
    NEO4J_AVAILABLE = True
except ImportError:
    NEO4J_AVAILABLE = False


# ══════════════════════════════════════════════════════════════════
# CONFIG
# ══════════════════════════════════════════════════════════════════

def _get_neo4j_config() -> dict:
    cfg = {
        "uri":      "bolt://localhost:7687",
        "user":     "neo4j",
        "password": "migration123",
    }
    try:
        import keys as k  # type: ignore
        if hasattr(k, "NEO4J_URI"):      cfg["uri"]      = k.NEO4J_URI
        if hasattr(k, "NEO4J_USER"):     cfg["user"]     = k.NEO4J_USER
        if hasattr(k, "NEO4J_PASSWORD"): cfg["password"] = k.NEO4J_PASSWORD
    except ImportError:
        pass
    if os.environ.get("NEO4J_URI"):      cfg["uri"]      = os.environ["NEO4J_URI"]
    if os.environ.get("NEO4J_USER"):     cfg["user"]     = os.environ["NEO4J_USER"]
    if os.environ.get("NEO4J_PASSWORD"): cfg["password"] = os.environ["NEO4J_PASSWORD"]
    return cfg


# ══════════════════════════════════════════════════════════════════
# DRIVER SINGLETON
# ══════════════════════════════════════════════════════════════════

_driver = None
_driver_cfg: dict = {}


def get_driver():
    global _driver, _driver_cfg
    if not NEO4J_AVAILABLE:
        return None
    cfg = _get_neo4j_config()
    if _driver is None or _driver_cfg != cfg:
        if _driver:
            _driver.close()
        try:
            _driver = GraphDatabase.driver(
                cfg["uri"],
                auth=(cfg["user"], cfg["password"]),
                max_connection_lifetime=3600,
                connection_acquisition_timeout=10,
                notifications_min_severity="WARNING",
                notifications_disabled_categories=["UNRECOGNIZED"],
            )
            _driver_cfg = cfg
        except Exception as e:
            print(f"[Neo4j] WARNING: Cannot connect — {e}")
            _driver = None
    return _driver


def check_connection() -> dict:
    if not NEO4J_AVAILABLE:
        return {"ok": False, "reason": "neo4j driver not installed (pip install neo4j)"}
    drv = get_driver()
    if drv is None:
        cfg = _get_neo4j_config()
        return {"ok": False, "reason": f"Cannot connect to {cfg['uri']}"}
    try:
        with drv.session() as s:
            result = s.run("RETURN 1 AS ping")
            result.single()
        return {"ok": True, "uri": _driver_cfg.get("uri", "?")}
    except Exception as e:
        return {"ok": False, "reason": str(e)}


# ══════════════════════════════════════════════════════════════════
# SCHEMA INIT
# ══════════════════════════════════════════════════════════════════

def ensure_schema() -> None:
    drv = get_driver()
    if not drv:
        return
    constraints = [
        "CREATE CONSTRAINT mf_path IF NOT EXISTS FOR (f:MigrationFile) REQUIRE f.path IS UNIQUE",
        "CREATE CONSTRAINT pat_key IF NOT EXISTS FOR (p:Pattern) REQUIRE (p.file_path, p.pattern_id) IS UNIQUE",
    ]
    indexes = [
        "CREATE INDEX mf_name IF NOT EXISTS FOR (f:MigrationFile) ON (f.name)",
        "CREATE INDEX pat_raw_type IF NOT EXISTS FOR (p:Pattern) ON (p.raw_type)",
        "CREATE INDEX pat_pattern_type IF NOT EXISTS FOR (p:Pattern) ON (p.pattern_type)",
        # v2.0: index on func_kind edge property (via node lookup)
        "CREATE INDEX pat_func_calls IF NOT EXISTS FOR (p:Pattern) ON (p.func_calls)",
    ]
    with drv.session() as s:
        for stmt in constraints + indexes:
            try:
                s.run(stmt)
            except Exception:
                pass
    print("[Neo4j] Schema ensured.")


# ══════════════════════════════════════════════════════════════════
# HELPERS
# ══════════════════════════════════════════════════════════════════

def _serialize(val: Any) -> Any:
    if isinstance(val, (list, dict)):
        return json.dumps(val, ensure_ascii=False)
    if val is None:
        return ""
    return val


def _deserialize_pattern(props: dict) -> dict:
    p = dict(props)
    for field in ("sub_types", "pattern_group", "callees", "callers", "line_range",
                  "func_calls", "func_creates"):           # ← v2.0 added
        if field in p and isinstance(p[field], str) and p[field]:
            try:
                p[field] = json.loads(p[field])
            except Exception:
                pass
    p["id"] = p.get("pattern_id", p.get("id"))
    for k in ("file_path",):
        p.pop(k, None)
    return p


def _pattern_to_props(file_path: str, p: dict) -> dict:
    return {
        "file_path":              file_path,
        "pattern_id":             int(p.get("id", 0)),
        "source_snippet":         str(p.get("source_snippet", ""))[:2000],
        "line_range":             _serialize(p.get("line_range", [0, 0])),
        "cs_line_start":          int(p.get("cs_line_start") or 0),
        "cs_line_end":            int(p.get("cs_line_end") or 0),
        "raw_type":               str(p.get("raw_type", "")),
        "pattern_type":           str(p.get("pattern_type", "")),
        "sub_types":              _serialize(p.get("sub_types", [])),
        "pattern_group":          _serialize(p.get("pattern_group", [])),
        "summary_vi":             str(p.get("summary_vi", "")),
        "csharp_snippet":         str(p.get("csharp_snippet", ""))[:4000],
        "difficulty":             str(p.get("difficulty", "")),
        "migration_strategy":     str(p.get("migration_strategy", "")),
        "risk_level":             str(p.get("risk_level", "")),
        "risk_strategy":          str(p.get("risk_strategy", "")),
        "csharp_popularity":      int(p.get("csharp_popularity", 3)),
        "needs_review":           bool(p.get("needs_review", False)),
        "source_file":            str(p.get("source_file", "")),
        "callees":                _serialize(p.get("callees", [])),
        "callers":                _serialize(p.get("callers", [])),
        # v2.0: func_kind summary fields
        "func_calls":             _serialize(p.get("func_calls", [])),
        "func_creates":           _serialize(p.get("func_creates", [])),
    }


# ══════════════════════════════════════════════════════════════════
# SAVE
# ══════════════════════════════════════════════════════════════════

def save_migration_result(
    file_path: str,
    file_name: str,
    source_content: str,
    patterns: list[dict],
    cs_text: str,
    csv_rows: list[dict] | None = None,
    elapsed: float = 0.0,
) -> dict:
    drv = get_driver()
    if not drv:
        return {"ok": False, "reason": "Neo4j not available"}

    now_iso = datetime.now(timezone.utc).isoformat()

    with drv.session() as s:
        s.run(
            """
            MERGE (f:MigrationFile {path: $path})
            SET f.name          = $name,
                f.updated_at    = $now,
                f.elapsed_s     = $elapsed,
                f.pattern_count = $cnt,
                f.source_size   = $src_size
            """,
            path=file_path, name=file_name, now=now_iso,
            elapsed=elapsed, cnt=len(patterns),
            src_size=len(source_content),
        )

        s.run(
            """
            MERGE (o:CSharpOutput {file_path: $path})
            SET o.cs_text    = $cs_text,
                o.char_count = $cc,
                o.updated_at = $now
            WITH o
            MATCH (f:MigrationFile {path: $path})
            MERGE (f)-[:HAS_OUTPUT]->(o)
            """,
            path=file_path, cs_text=cs_text[:500000],
            cc=len(cs_text), now=now_iso,
        )

        for p in patterns:
            props = _pattern_to_props(file_path, p)
            s.run(
                """
                MERGE (pat:Pattern {file_path: $file_path, pattern_id: $pattern_id})
                SET pat += $props
                WITH pat
                MATCH (f:MigrationFile {path: $file_path})
                MERGE (f)-[:HAS_PATTERN]->(pat)
                """,
                file_path=file_path,
                pattern_id=props["pattern_id"],
                props=props,
            )

        _save_call_edges(s, file_path, patterns)

    return {"ok": True, "file_path": file_path, "patterns_saved": len(patterns)}


def _save_call_edges(session, file_path: str, patterns: list[dict]) -> None:
    """
    Create CALLS relationships between Pattern nodes.
    v2.0: stores func_kind on each edge so the UI can distinguish
          call vs create relationships in the graph.
    """
    for p in patterns:
        src_id = int(p.get("id", 0))
        for c in p.get("callees", []):
            def_pid  = c.get("definition_pattern_id") or c.get("pattern_id")
            def_file = c.get("definition_file") or c.get("file") or file_path

            if def_pid is None:
                continue

            # func_kind: "call" | "create" | "system"
            func_kind = c.get("func_kind", "call")

            try:
                session.run(
                    """
                    MATCH (src:Pattern {file_path: $sf, pattern_id: $sid})
                    MATCH (dst:Pattern {file_path: $df, pattern_id: $did})
                    MERGE (src)-[r:CALLS {kind: $kind, func_kind: $func_kind}]->(dst)
                    SET r.callee_name           = $name,
                        r.call_line             = $line,
                        r.definition_pattern_id = $def_pid,
                        r.definition_file       = $def_file,
                        r.func_kind             = $func_kind
                    """,
                    sf=file_path,   sid=src_id,
                    df=def_file,    did=int(def_pid),
                    kind=c.get("kind", "local"),
                    func_kind=func_kind,
                    name=c.get("name", ""),
                    line=c.get("line") or 0,
                    def_pid=int(def_pid),
                    def_file=def_file,
                )
            except Exception:
                pass


# ══════════════════════════════════════════════════════════════════
# LOAD — FILE LIST
# ══════════════════════════════════════════════════════════════════

def list_migrated_files() -> list[dict]:
    drv = get_driver()
    if not drv:
        return []
    with drv.session() as s:
        result = s.run(
            """
            MATCH (f:MigrationFile)
            RETURN f.path AS path, f.name AS name,
                   f.updated_at AS updated_at, f.elapsed_s AS elapsed_s,
                   f.pattern_count AS pattern_count, f.source_size AS source_size
            ORDER BY f.updated_at DESC
            """
        )
        return [dict(r) for r in result]


# ══════════════════════════════════════════════════════════════════
# LOAD — SINGLE FILE
# ══════════════════════════════════════════════════════════════════

def load_file_result(file_path: str) -> Optional[dict]:
    drv = get_driver()
    if not drv:
        return None

    with drv.session() as s:
        r = s.run(
            "MATCH (f:MigrationFile {path: $path}) RETURN f",
            path=file_path,
        ).single()
        if not r:
            return None
        file_node = dict(r["f"])

        cs_r = s.run(
            """
            MATCH (f:MigrationFile {path: $path})-[:HAS_OUTPUT]->(o:CSharpOutput)
            RETURN o.cs_text AS cs_text
            """,
            path=file_path,
        ).single()
        cs_text = cs_r["cs_text"] if cs_r else ""

        pat_r = s.run(
            """
            MATCH (f:MigrationFile {path: $path})-[:HAS_PATTERN]->(p:Pattern)
            RETURN p
            ORDER BY p.pattern_id ASC
            """,
            path=file_path,
        )
        patterns = [_deserialize_pattern(dict(r["p"])) for r in pat_r]

    return {
        "file_path":  file_path,
        "file_name":  file_node.get("name", Path(file_path).name),
        "patterns":   patterns,
        "cs_text":    cs_text,
        "updated_at": file_node.get("updated_at", ""),
        "elapsed_s":  file_node.get("elapsed_s", 0),
    }


# ══════════════════════════════════════════════════════════════════
# LOAD — CALL GRAPH  (v2.0: includes func_kind from edge)
# ══════════════════════════════════════════════════════════════════

def load_call_graph(file_path: str):
    drv = get_driver()
    if not drv:
        return None

    with drv.session() as s:
        result = s.run(
            """
            MATCH (f:MigrationFile {path: $path})-[:HAS_PATTERN]->(p:Pattern)
            OPTIONAL MATCH (p)-[ce:CALLS]->(callee:Pattern)
            OPTIONAL MATCH (caller_pat:Pattern)-[cr:CALLS]->(p)
            RETURN p,
                   collect(DISTINCT {
                     name:                  ce.callee_name,
                     kind:                  ce.kind,
                     func_kind:             ce.func_kind,
                     file:                  callee.file_path,
                     line:                  ce.call_line,
                     pattern_id:            callee.pattern_id,
                     definition_pattern_id: ce.definition_pattern_id,
                     definition_file:       ce.definition_file
                   }) AS callees_rel,
                   collect(DISTINCT {
                     name:                  cr.callee_name,
                     func_kind:             cr.func_kind,
                     file:                  caller_pat.file_path,
                     line:                  cr.call_line,
                     pattern_id:            caller_pat.pattern_id,
                     caller_func:           caller_pat.source_file,
                     definition_pattern_id: cr.definition_pattern_id,
                     definition_file:       cr.definition_file
                   }) AS callers_rel
            ORDER BY p.pattern_id
            """,
            path=file_path,
        )

        patterns = []
        total_callees = total_callers = 0
        total_calls = total_creates = 0
        for r in result:
            p = _deserialize_pattern(dict(r["p"]))
            callees = [c for c in r["callees_rel"] if c.get("name")]
            callers = [c for c in r["callers_rel"] if c.get("name")]
            if callees:
                p["callees"] = callees
                total_callees += len(callees)
                total_calls   += sum(1 for c in callees if c.get("func_kind") == "call")
                total_creates += sum(1 for c in callees if c.get("func_kind") == "create")
            if callers:
                p["callers"] = callers
                total_callers += len(callers)
            patterns.append(p)

    if not patterns:
        return None

    return {
        "file": Path(file_path).name,
        "patterns": patterns,
        "summary": {
            "patterns_with_deps": sum(1 for p in patterns if p.get("callees") or p.get("callers")),
            "total_callees":      total_callees,
            "total_callers":      total_callers,
            "total_func_calls":   total_calls,     # ← v2.0
            "total_func_creates": total_creates,   # ← v2.0
        },
    }


# ══════════════════════════════════════════════════════════════════
# SAVE CALL GRAPH (from auto call graph after pipeline)
# ══════════════════════════════════════════════════════════════════

def save_call_graph_result(file_name: str, enriched_patterns: list[dict]) -> dict:
    drv = get_driver()
    if not drv:
        return {"ok": False, "reason": "Neo4j not available"}

    with drv.session() as s:
        r = s.run(
            "MATCH (f:MigrationFile {name: $name}) RETURN f.path AS path LIMIT 1",
            name=file_name,
        ).single()
        if not r:
            return {"ok": False, "reason": f"File '{file_name}' not found in DB"}
        file_path = r["path"]

        for p in enriched_patterns:
            pid = int(p.get("id", 0))
            callees_json  = _serialize(p.get("callees", []))
            callers_json  = _serialize(p.get("callers", []))
            func_calls_j  = _serialize(p.get("func_calls", []))
            func_creates_j = _serialize(p.get("func_creates", []))
            s.run(
                """
                MATCH (pat:Pattern {file_path: $fp, pattern_id: $pid})
                SET pat.callees      = $callees,
                    pat.callers      = $callers,
                    pat.source_file  = $sf,
                    pat.func_calls   = $func_calls,
                    pat.func_creates = $func_creates
                """,
                fp=file_path, pid=pid,
                callees=callees_json,
                callers=callers_json,
                sf=p.get("source_file", ""),
                func_calls=func_calls_j,
                func_creates=func_creates_j,
            )

        _save_call_edges(s, file_path, enriched_patterns)

    return {"ok": True, "file_path": file_path, "patterns_updated": len(enriched_patterns)}


# ══════════════════════════════════════════════════════════════════
# DELETE
# ══════════════════════════════════════════════════════════════════

def delete_file_result(file_path: str) -> dict:
    drv = get_driver()
    if not drv:
        return {"ok": False, "reason": "Neo4j not available"}
    with drv.session() as s:
        s.run(
            """
            MATCH (f:MigrationFile {path: $path})-[:HAS_PATTERN]->(p:Pattern)
            DETACH DELETE p
            """,
            path=file_path,
        )
        s.run(
            """
            MATCH (f:MigrationFile {path: $path})-[:HAS_OUTPUT]->(o:CSharpOutput)
            DETACH DELETE o
            """,
            path=file_path,
        )
        s.run(
            "MATCH (f:MigrationFile {path: $path}) DETACH DELETE f",
            path=file_path,
        )
    return {"ok": True, "deleted": file_path}


# ══════════════════════════════════════════════════════════════════
# STATS
# ══════════════════════════════════════════════════════════════════

def get_db_stats() -> dict:
    drv = get_driver()
    if not drv:
        return {"ok": False, "reason": "Neo4j not available"}
    try:
        with drv.session() as s:
            r = s.run(
                """
                MATCH (f:MigrationFile)
                WITH count(f) AS files
                MATCH (p:Pattern)
                WITH files, count(p) AS patterns
                OPTIONAL MATCH ()-[c:CALLS]->()
                WITH files, patterns, count(c) AS edges
                OPTIONAL MATCH ()-[cc:CALLS {func_kind: 'call'}]->()
                WITH files, patterns, edges, count(cc) AS call_edges
                OPTIONAL MATCH ()-[cr:CALLS {func_kind: 'create'}]->()
                WITH files, patterns, edges, call_edges, count(cr) AS create_edges
                RETURN files, patterns, edges, call_edges, create_edges
                """
            ).single()
            return {
                "ok":           True,
                "files":        r["files"]        if r else 0,
                "patterns":     r["patterns"]     if r else 0,
                "edges":        r["edges"]        if r else 0,
                "call_edges":   r["call_edges"]   if r else 0,   # ← v2.0
                "create_edges": r["create_edges"] if r else 0,   # ← v2.0
                "uri":          _driver_cfg.get("uri", "?"),
            }
    except Exception as e:
        return {"ok": False, "reason": str(e)}


# ══════════════════════════════════════════════════════════════════
# SEARCH PATTERNS
# ══════════════════════════════════════════════════════════════════

def search_patterns(
    query: str = "",
    raw_type: str = "",
    risk_level: str = "",
    func_kind: str = "",      # ← v2.0: filter by func_kind on callees
    limit: int = 100,
) -> list[dict]:
    drv = get_driver()
    if not drv:
        return []

    filters = []
    params: dict = {"limit": limit}

    if query:
        filters.append(
            "(toLower(p.source_snippet) CONTAINS toLower($q) OR "
            " toLower(p.summary_vi) CONTAINS toLower($q) OR "
            " toLower(p.csharp_snippet) CONTAINS toLower($q))"
        )
        params["q"] = query

    if raw_type:
        filters.append("p.raw_type = $raw_type")
        params["raw_type"] = raw_type

    if risk_level:
        filters.append("p.risk_level = $risk_level")
        params["risk_level"] = risk_level

    if func_kind == "call":
        # Patterns that have at least one "call"-kind callee
        filters.append("p.func_calls <> '[]' AND p.func_calls <> ''")
    elif func_kind == "create":
        filters.append("p.func_creates <> '[]' AND p.func_creates <> ''")

    where_clause = ("WHERE " + " AND ".join(filters)) if filters else ""

    cypher = f"""
        MATCH (f:MigrationFile)-[:HAS_PATTERN]->(p:Pattern)
        {where_clause}
        RETURN p, f.path AS file_path, f.name AS file_name
        ORDER BY p.pattern_id
        LIMIT $limit
    """

    with drv.session() as s:
        result = s.run(cypher, **params)
        out = []
        for r in result:
            p = _deserialize_pattern(dict(r["p"]))
            p["_file_path"] = r["file_path"]
            p["_file_name"] = r["file_name"]
            out.append(p)
    return out
