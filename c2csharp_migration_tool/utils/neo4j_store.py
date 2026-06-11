"""
utils/neo4j_store.py — Neo4j Persistence Layer  v1.0
=====================================================
Lưu trữ toàn bộ kết quả migration vào Neo4j:
  - MigrationFile  node: thông tin file nguồn
  - Pattern        node: từng pattern (migration_data item)
  - CSharpOutput   node: nội dung .cs file
  - CallEdge       relationship: callee/caller giữa các Pattern

Schema:
  (MigrationFile)-[:HAS_PATTERN]->(Pattern)
  (MigrationFile)-[:HAS_OUTPUT]->(CSharpOutput)
  (Pattern)-[:CALLS]->(Pattern)        # callee edge
  (Pattern)-[:CALLED_BY]->(Pattern)    # caller edge (reverse)

Endpoints tương ứng trong server.py:
  POST /api/db/save          — lưu toàn bộ kết quả migration
  GET  /api/db/files         — danh sách files đã migrate
  GET  /api/db/file?name=X   — load lại patterns + cs cho 1 file
  GET  /api/db/callgraph?name=X — load lại call graph
  DELETE /api/db/file?name=X — xoá file khỏi DB
  GET  /api/db/stats         — thống kê DB
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

# neo4j driver — pip install neo4j
try:
    from neo4j import GraphDatabase, exceptions as neo4j_exc
    NEO4J_AVAILABLE = True
except ImportError:
    NEO4J_AVAILABLE = False


# ══════════════════════════════════════════════════════════════════
# CONFIG — đọc từ env hoặc keys.py
# ══════════════════════════════════════════════════════════════════

def _get_neo4j_config() -> dict:
    """Đọc Neo4j config theo thứ tự ưu tiên: env > keys.py > default."""
    cfg = {
        "uri":      "bolt://localhost:7687",
        "user":     "neo4j",
        "password": "migration123",
    }
    # Thử đọc từ keys.py
    try:
        import keys as k  # type: ignore
        if hasattr(k, "NEO4J_URI"):      cfg["uri"]      = k.NEO4J_URI
        if hasattr(k, "NEO4J_USER"):     cfg["user"]     = k.NEO4J_USER
        if hasattr(k, "NEO4J_PASSWORD"): cfg["password"] = k.NEO4J_PASSWORD
    except ImportError:
        pass
    # Env vars override everything
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
    """Lazy-init driver singleton. Returns None nếu Neo4j không khả dụng."""
    global _driver, _driver_cfg
    if not NEO4J_AVAILABLE:
        return None
    cfg = _get_neo4j_config()
    # Reconnect nếu config thay đổi
    if _driver is None or _driver_cfg != cfg:
        if _driver:
            _driver.close()
        try:
            _driver = GraphDatabase.driver(
                cfg["uri"],
                auth=(cfg["user"], cfg["password"]),
                max_connection_lifetime=3600,
                connection_acquisition_timeout=10,
            )
            _driver_cfg = cfg
        except Exception as e:
            print(f"[Neo4j] WARNING: Cannot connect — {e}")
            _driver = None
    return _driver


def check_connection() -> dict:
    """Kiểm tra kết nối Neo4j. Trả về status dict."""
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
    """Tạo indexes/constraints nếu chưa có."""
    drv = get_driver()
    if not drv:
        return
    constraints = [
        # MigrationFile unique by path
        "CREATE CONSTRAINT mf_path IF NOT EXISTS FOR (f:MigrationFile) REQUIRE f.path IS UNIQUE",
        # Pattern unique by composite key
        "CREATE CONSTRAINT pat_key IF NOT EXISTS FOR (p:Pattern) REQUIRE (p.file_path, p.pattern_id) IS UNIQUE",
    ]
    indexes = [
        "CREATE INDEX mf_name IF NOT EXISTS FOR (f:MigrationFile) ON (f.name)",
        "CREATE INDEX pat_raw_type IF NOT EXISTS FOR (p:Pattern) ON (p.raw_type)",
        "CREATE INDEX pat_pattern_type IF NOT EXISTS FOR (p:Pattern) ON (p.pattern_type)",
    ]
    with drv.session() as s:
        for stmt in constraints + indexes:
            try:
                s.run(stmt)
            except Exception:
                pass  # constraints may already exist on older Neo4j
    print("[Neo4j] Schema ensured.")



