"""执行历史（写入独立的 history.db，用户查询连接打不开它）。"""
from __future__ import annotations

import json
import sqlite3
import time
from typing import Any

from . import config

_SCHEMA = """
CREATE TABLE IF NOT EXISTS query_history (
    id              TEXT PRIMARY KEY,
    sql_text        TEXT NOT NULL,
    status          TEXT NOT NULL,           -- running/completed/failed
    stop_reason     TEXT,                    -- 见 config.STOP_*
    error           TEXT,
    statement_index INTEGER,
    statement       TEXT,
    row_count       INTEGER,
    truncated       INTEGER NOT NULL DEFAULT 0,
    result_id       TEXT,
    duration_ms     INTEGER,
    created_at      REAL NOT NULL
);
"""


def _connect(space) -> sqlite3.Connection:
    conn = sqlite3.connect(space.history_db, timeout=10.0)
    conn.row_factory = sqlite3.Row
    return conn


def init_history_db(space) -> None:
    with _connect(space) as conn:
        conn.executescript(_SCHEMA)


def insert_running(space, query_id: str, sql_text: str) -> None:
    with space.history_lock, _connect(space) as conn:
        conn.execute(
            "INSERT INTO query_history (id, sql_text, status, created_at) "
            "VALUES (?, ?, 'running', ?)",
            (query_id, sql_text, time.time()),
        )
        _trim(conn)


def finish(space, query_id: str, *, status: str, stop_reason: str,
          error: dict | None = None, row_count: int = 0, truncated: bool = False,
          result_id: str | None = None, duration_ms: int = 0) -> None:
    with space.history_lock, _connect(space) as conn:
        conn.execute(
            """
            UPDATE query_history
               SET status = ?, stop_reason = ?, error = ?, statement_index = ?,
                   statement = ?, row_count = ?, truncated = ?, result_id = ?,
                   duration_ms = ?
             WHERE id = ?
            """,
            (
                status,
                stop_reason,
                json.dumps(error, ensure_ascii=False) if error else None,
                (error or {}).get("statement_index"),
                (error or {}).get("statement"),
                row_count,
                1 if truncated else 0,
                result_id,
                duration_ms,
                query_id,
            ),
        )


def list_history(space, limit: int = 100) -> list[dict[str, Any]]:
    with _connect(space) as conn:
        rows = conn.execute(
            "SELECT * FROM query_history ORDER BY created_at DESC LIMIT ?",
            (min(limit, config.HISTORY_MAX_ROWS_PER_SPACE),),
        ).fetchall()
    result = []
    for r in rows:
        d = dict(r)
        d["truncated"] = bool(d.get("truncated"))
        if d.get("error"):
            try:
                d["error"] = json.loads(d["error"])
            except json.JSONDecodeError:
                pass
        result.append(d)
    return result


def get_history(space, query_id: str) -> dict | None:
    with _connect(space) as conn:
        row = conn.execute(
            "SELECT * FROM query_history WHERE id = ?", (query_id,)
        ).fetchone()
    if row is None:
        return None
    d = dict(row)
    d["truncated"] = bool(d.get("truncated"))
    if d.get("error"):
        try:
            d["error"] = json.loads(d["error"])
        except json.JSONDecodeError:
            pass
    return d


def _trim(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        DELETE FROM query_history
         WHERE id NOT IN (
               SELECT id FROM query_history
                ORDER BY created_at DESC
                LIMIT ?
         )
        """,
        (config.HISTORY_MAX_ROWS_PER_SPACE,),
    )
