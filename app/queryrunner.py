"""查询执行引擎（异步执行 + 轮询 + 取消）。

执行模型：
  1. ``submit`` 生成 query_id、写入 running 历史、启动后台线程后立即返回，
     因此前端在查询仍在运行时就持有 query_id，可以随时请求取消；
  2. 工作线程打开 *mode=ro* 只读连接（挂载 authorizer），逐条 prepare
     （安全裁决在执行前发生），再通过 progress handler 周期性检查
     超时/取消标志，最后把结果物化为快照；
  3. 轮询 ``get_status`` 读取终态；分页/下载只访问已物化的快照；
  4. 无论成功、失败、超时、取消，连接都在 finally 中关闭释放，
     不会拖住同一空间的其他查询（只读连接之间互不阻塞）。
"""
from __future__ import annotations

import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass

from . import config, history
from .dbguard import (
    QueryBudget,
    describe_interrupt,
    interrupted_reason,
    open_readonly_connection,
    prepare_guarded,
    split_statements,
)
from .errors import SQLError, sanitize_message


@dataclass
class _RunningEntry:
    space_id: str
    budget: QueryBudget


@dataclass
class _Job:
    query_id: str
    space_id: str
    sql_text: str
    done: threading.Event
    outcome: dict | None = None       # 成功时的结果描述
    error: SQLError | None = None     # 失败时的结构化错误


@dataclass
class Snapshot:
    result_id: str
    space_id: str
    columns: list[str]
    rows: list[tuple]
    truncated: bool
    created_at: float
    sql_text: str

    def is_fresh(self) -> bool:
        return time.time() - self.created_at < config.RESULT_TTL_SECONDS


class QueryService:
    def __init__(self):
        self._running: dict[str, _RunningEntry] = {}
        self._running_lock = threading.Lock()
        self._jobs: dict[str, _Job] = {}
        self._jobs_lock = threading.Lock()
        self._snapshots: dict[str, Snapshot] = {}
        self._snapshot_lock = threading.Lock()

    # ------------------------------------------------------------------ #
    # 取消
    # ------------------------------------------------------------------ #
    def cancel(self, space_id: str, query_id: str) -> bool:
        with self._running_lock:
            entry = self._running.get(query_id)
            if not entry or entry.space_id != space_id:
                return False
            entry.budget.request_cancel()
            return True

    def _register_running(self, query_id: str, space_id: str,
                          budget: QueryBudget) -> None:
        with self._running_lock:
            self._running[query_id] = _RunningEntry(space_id, budget)

    def _unregister_running(self, query_id: str) -> None:
        with self._running_lock:
            self._running.pop(query_id, None)

    # ------------------------------------------------------------------ #
    # 快照（分页 / 下载共用）
    # ------------------------------------------------------------------ #
    def get_snapshot(self, space_id: str, result_id: str) -> Snapshot:
        with self._snapshot_lock:
            snap = self._snapshots.get(result_id)
        if snap is None or not snap.is_fresh():
            raise SQLError("结果已过期或不存在，请重新执行查询")
        if snap.space_id != space_id:
            # 跨空间访问按不存在处理，不泄露其他空间的存在性
            raise SQLError("结果已过期或不存在，请重新执行查询")
        return snap

    def _store_snapshot(self, snap: Snapshot) -> None:
        with self._snapshot_lock:
            same_space = [
                s for s in self._snapshots.values()
                if s.space_id == snap.space_id and s.is_fresh()
            ]
            if len(same_space) >= config.RESULT_MAX_SNAPSHOTS_PER_SPACE:
                same_space.sort(key=lambda s: s.created_at)
                self._snapshots.pop(same_space[0].result_id, None)
            expired = [k for k, s in self._snapshots.items() if not s.is_fresh()]
            for k in expired:
                self._snapshots.pop(k, None)
            self._snapshots[snap.result_id] = snap

    # ------------------------------------------------------------------ #
    # 提交 / 状态轮询
    # ------------------------------------------------------------------ #
    def submit(self, space, sql_text: str) -> dict:
        # 长度检查必须在 strip 之前，否则填充空白可绕过限额
        raw_bytes = len((sql_text or "").encode("utf-8"))
        if raw_bytes > config.QUERY_SQL_MAX_BYTES:
            raise SQLError(
                f"SQL 文本超过 {config.QUERY_SQL_MAX_BYTES // 1024} KB 上限"
            )
        sql_text = (sql_text or "").strip()
        if not sql_text:
            raise SQLError("SQL 不能为空")
        # 语句拆分在请求线程内完成：语法层面的低级错误直接返回
        statements = split_statements(sql_text)
        if not statements:
            raise SQLError("没有可执行的语句（纯注释/空白）")

        query_id = uuid.uuid4().hex
        job = _Job(query_id=query_id, space_id=space.id,
                   sql_text=sql_text, done=threading.Event())
        with self._jobs_lock:
            self._jobs[query_id] = job
        history.insert_running(space, query_id, sql_text)

        thread = threading.Thread(
            target=self._run_job,
            args=(space, job, statements),
            name=f"query-{query_id[:8]}",
            daemon=True,
        )
        thread.start()
        return {"query_id": query_id, "status": "running"}

    def get_status(self, space_id: str, query_id: str) -> dict:
        with self._jobs_lock:
            job = self._jobs.get(query_id)
        if job is None or job.space_id != space_id:
            raise SQLError("查询任务不存在（可能已过期），请重新执行")
        if not job.done.is_set():
            return {"query_id": query_id, "status": "running"}
        if job.error is not None:
            return {
                "query_id": query_id,
                "status": "failed",
                "error": job.error.to_dict(),
            }
        out = dict(job.outcome)
        out["status"] = "completed"
        # 附带第一页，省一次往返
        snap = self.get_snapshot(space_id, out["result_id"])
        out["page"] = self._page(snap, 0, config.QUERY_PAGE_SIZE_DEFAULT)
        return out

    # ------------------------------------------------------------------ #
    # 工作线程主体
    # ------------------------------------------------------------------ #
    def _run_job(self, space, job: _Job, statements: list[str]) -> None:
        query_id = job.query_id
        budget = QueryBudget()
        self._register_running(query_id, space.id, budget)
        started = time.monotonic()
        conn = None
        current_index: int | None = None
        current_stmt: str | None = None
        try:
            conn, _authorizer = open_readonly_connection(space.data_db)
            conn.set_progress_handler(budget.tick, config.QUERY_PROGRESS_TICK_OPS)

            last_cursor = None
            last_stmt_index = len(statements) - 1
            for idx, stmt in enumerate(statements):
                current_index, current_stmt = idx, stmt
                # prepare 阶段即经过 authorizer；任何写操作/ATTACH/PRAGMA
                # 在这里被拒绝，整条请求失败且无副作用。
                cursor = prepare_guarded(conn, _authorizer, stmt, idx)
                if idx != last_stmt_index:
                    # 前置语句不消费结果集（SELECT 允许但不返回给用户，
                    # 写语句已在上面的 prepare 中被拦截，不可能走到这里）。
                    cursor.close()
                else:
                    last_cursor = cursor

            if last_cursor is None or last_cursor.description is None:
                raise SQLError(
                    "最后一条语句必须是 SELECT 查询（写操作在本系统中不被允许）",
                    statement_index=last_stmt_index,
                    statement_text=statements[last_stmt_index],
                    stop_reason=config.STOP_DENIED,
                )

            columns = [d[0] for d in last_cursor.description]
            rows, truncated = self._fetch_capped(last_cursor)

            snap = Snapshot(
                result_id=uuid.uuid4().hex,
                space_id=space.id,
                columns=columns,
                rows=rows,
                truncated=truncated,
                created_at=time.time(),
                sql_text=job.sql_text,
            )
            self._store_snapshot(snap)
            duration_ms = int((time.monotonic() - started) * 1000)
            stop_reason = config.STOP_LIMIT if truncated else config.STOP_COMPLETED
            history.finish(
                space, query_id,
                status="completed",
                stop_reason=stop_reason,
                row_count=len(rows),
                truncated=truncated,
                result_id=snap.result_id,
                duration_ms=duration_ms,
            )
            job.outcome = {
                "query_id": query_id,
                "result_id": snap.result_id,
                "columns": columns,
                "row_count": len(rows),
                "truncated": truncated,
                "max_rows": config.QUERY_MAX_ROWS,
                "timeout_ms": config.QUERY_TIMEOUT_MS,
                "duration_ms": duration_ms,
                "stop_reason": stop_reason,
            }
        except SQLError as exc:
            self._record_failure(space, query_id, started, exc)
            job.error = exc
        except sqlite3.OperationalError as exc:
            if "interrupt" in str(exc).lower():
                reason = interrupted_reason(budget)
                s_exc = describe_interrupt(reason, current_index, current_stmt)
            else:
                s_exc = SQLError(
                    f"SQL 执行失败：{sanitize_message(str(exc))}",
                    statement_index=current_index,
                    statement_text=current_stmt,
                )
            self._record_failure(space, query_id, started, s_exc)
            job.error = s_exc
        except sqlite3.DatabaseError as exc:
            s_exc = SQLError(
                f"SQL 执行失败：{sanitize_message(str(exc))}",
                statement_index=current_index,
                statement_text=current_stmt,
            )
            self._record_failure(space, query_id, started, s_exc)
            job.error = s_exc
        finally:
            self._unregister_running(query_id)
            if conn is not None:
                try:
                    conn.set_progress_handler(None, 0)
                    conn.close()
                except sqlite3.Error:
                    pass
            job.done.set()

    # ------------------------------------------------------------------ #
    # 分页
    # ------------------------------------------------------------------ #
    def page(self, space_id: str, result_id: str, offset: int, limit: int) -> dict:
        snap = self.get_snapshot(space_id, result_id)
        return self._page(snap, offset, limit)

    def _page(self, snap: Snapshot, offset: int, limit: int) -> dict:
        limit = min(max(1, limit), config.QUERY_PAGE_SIZE_MAX)
        offset = max(0, offset)
        total = len(snap.rows)
        page_rows = snap.rows[offset:offset + limit]
        return {
            "offset": offset,
            "limit": limit,
            "total": total,
            "has_more": offset + len(page_rows) < total,
            "columns": snap.columns,
            "rows": [self._json_safe(r) for r in page_rows],
        }

    # ------------------------------------------------------------------ #
    def _fetch_capped(self, cursor) -> tuple[list[tuple], bool]:
        """最多物化 MAX_ROWS + 1 行：多出的 1 行仅用于判断截断。"""
        cap = config.QUERY_MAX_ROWS
        rows: list[tuple] = []
        truncated = False
        for row in cursor:
            rows.append(tuple(row))
            if len(rows) > cap:
                truncated = True
                break
        if truncated:
            rows = rows[:cap]
        return rows, truncated

    def _record_failure(self, space, query_id: str, started: float,
                        exc: SQLError) -> None:
        duration_ms = int((time.monotonic() - started) * 1000)
        history.finish(
            space, query_id,
            status="failed",
            stop_reason=exc.stop_reason,
            error=exc.to_dict(),
            duration_ms=duration_ms,
        )

    @staticmethod
    def _json_safe(row: tuple) -> list:
        out = []
        for v in row:
            if isinstance(v, bytes):
                out.append(v.hex())
            elif isinstance(v, float) and v != v:
                out.append(None)  # NaN → null，保证 JSON 合法
            else:
                out.append(v)
        return out


# 全局单例
service = QueryService()
