"""限额、截断、超时与取消测试。"""
from __future__ import annotations

import time

from tests.base import TempDataTestCase


# 固定长度字符串上的高成本 GLOB 回溯递归：单行成本高、深度不受 1000 限制、
# 可被 progress handler 在超时/取消时稳定中断。
_HEAVY_RECURSION = (
    "WITH RECURSIVE r(n, s) AS ("
    "SELECT 1, '" + "ab" * 200 + "' UNION ALL "
    "SELECT n+1, substr(s || '" + "ab" * 200 + "', 1, 400) "
    "FROM r WHERE n < 100000000) "
    "SELECT count(*) FROM r "
    "WHERE s GLOB '*a*b*a*b*a*b*a*b*a*b*a*b*a*b*'"
)


class LimitAndCancelTests(TempDataTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.load_sample()

    def test_row_limit_truncation(self):
        # 把上限临时调小，验证截断标志与行数
        self.config.QUERY_MAX_ROWS = 5
        st = self.run_sql("SELECT * FROM customers CROSS JOIN products")
        self.assertEqual(st["status"], "completed")
        self.assertTrue(st["truncated"])
        self.assertEqual(st["row_count"], 5)
        self.assertEqual(st["stop_reason"], "row_limit")
        # 历史中也应记录截断
        from app import history
        records = history.list_history(self.space)
        self.assertTrue(any(r["truncated"] for r in records))

    def test_snapshot_pagination(self):
        st = self.run_sql("SELECT * FROM customers")
        rid = st["result_id"]
        page1 = self.svc.page(self.space.id, rid, 0, 3)
        self.assertEqual(len(page1["rows"]), 3)
        self.assertTrue(page1["has_more"])
        page2 = self.svc.page(self.space.id, rid, 3, 3)
        self.assertEqual(len(page2["rows"]), 3)
        page3 = self.svc.page(self.space.id, rid, 6, 3)
        self.assertEqual(len(page3["rows"]), 2)   # customers 共 8 行
        self.assertFalse(page3["has_more"])
        # 两页数据不重复
        ids1 = {r[0] for r in page1["rows"]}
        ids2 = {r[0] for r in page2["rows"]}
        self.assertTrue(ids1.isdisjoint(ids2))

    def test_timeout_stops_long_recursive_query(self):
        self.config.QUERY_TIMEOUT_MS = 800
        start = time.monotonic()
        st = self.run_sql(_HEAVY_RECURSION)
        elapsed = time.monotonic() - start
        self.assertEqual(st["status"], "failed")
        self.assertEqual(st["error"]["stop_reason"], "timeout")
        # 中断粒度受单条 VM 指令（GLOB 回溯）长度影响，要求在预算的
        # 若干倍内停住，而不是无限运行；正常应在数秒内。
        self.assertLess(elapsed, 8.0)
        self.assertGreaterEqual(elapsed, 0.7)

    def test_user_cancel_stops_query(self):
        # 超时给足，排除超时干扰，验证用户取消
        self.config.QUERY_TIMEOUT_MS = 60_000
        qid = self.svc.submit(self.space, _HEAVY_RECURSION)["query_id"]
        # 等待查询真正进入运行态（progress handler 已挂上）
        self._wait_running(qid)
        cancelled = self.svc.cancel(self.space.id, qid)
        self.assertTrue(cancelled)
        final = self._wait_terminal(qid)
        self.assertEqual(final["status"], "failed")
        self.assertEqual(final["error"]["stop_reason"], "cancelled")

    def _wait_running(self, qid: str, timeout: float = 5.0) -> None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if qid in self.svc._running:
                return
            time.sleep(0.01)
        self.fail("查询未进入 running 状态")

    def _wait_terminal(self, qid: str, timeout: float = 10.0) -> dict:
        deadline = time.time() + timeout
        while time.time() < deadline:
            st = self.svc.get_status(self.space.id, qid)
            if st["status"] != "running":
                return st
            time.sleep(0.02)
        self.fail("查询未在预期时间内结束")

    def test_cancel_releases_connection_other_queries_unaffected(self):
        """取消后连接释放：另一个查询应能立刻正常运行。"""
        self.config.QUERY_TIMEOUT_MS = 60_000
        qid = self.svc.submit(self.space, _HEAVY_RECURSION)["query_id"]
        self._wait_running(qid)
        self.svc.cancel(self.space.id, qid)
        self._wait_terminal(qid)
        # 立刻发起新查询 —— 不应被阻塞
        started = time.monotonic()
        st = self.run_sql("SELECT COUNT(*) FROM customers")
        self.assertEqual(st["status"], "completed")
        self.assertLess(time.monotonic() - started, 2.0)

    def test_cancel_unknown_query_returns_false(self):
        self.assertFalse(self.svc.cancel(self.space.id, "nonexistent-id"))

    def test_cross_space_snapshot_access_denied(self):
        other = self.registry.create("另一个空间")
        st = self.run_sql("SELECT 1 AS x")
        rid = st["result_id"]
        with self.assertRaises(Exception):
            self.svc.page(other.id, rid, 0, 10)

    def test_sql_size_limit(self):
        # 64KB 上限：大小检查发生在拆语句之前，填充空白即可
        huge = "SELECT 1" + " " * (self.config.QUERY_SQL_MAX_BYTES + 100)
        from app.errors import SQLError
        with self.assertRaises(SQLError) as ctx:
            self.svc.submit(self.space, huge)
        self.assertIn("KB", str(ctx.exception))
