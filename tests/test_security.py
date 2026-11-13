"""安全策略测试：注释/WITH/子查询/多语句等绕过手法全部按真实权限拦截。"""
from __future__ import annotations

import sqlite3

from tests.base import TempDataTestCase
from app.errors import SQLError


class SecurityGuardTests(TempDataTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.load_sample()

    def assert_denied(self, sql: str, fragment: str = ""):
        status = self.run_sql(sql)
        self.assertEqual(status["status"], "failed", f"应被拒绝: {sql}")
        self.assertEqual(status["error"]["stop_reason"], "denied")
        if fragment:
            self.assertIn(fragment, status["error"]["message"])

    # -- 正常读查询放行 -----------------------------------------------------
    def test_plain_select_allowed(self):
        st = self.run_sql("SELECT COUNT(*) AS c FROM customers")
        self.assertEqual(st["status"], "completed")
        self.assertEqual(st["page"]["rows"][0][0], 8)

    def test_with_cte_and_subquery_allowed(self):
        st = self.run_sql(
            "WITH t AS (SELECT region FROM customers) "
            "SELECT region, COUNT(*) FROM t GROUP BY region ORDER BY region"
        )
        self.assertEqual(st["status"], "completed")
        self.assertTrue(len(st["page"]["rows"]) >= 1)

    def test_join_query_runs(self):
        st = self.run_sql(
            "SELECT c.name, COUNT(o.order_id) FROM customers c "
            "LEFT JOIN orders o ON o.customer_id = c.customer_id "
            "GROUP BY c.name ORDER BY c.name"
        )
        self.assertEqual(st["status"], "completed")
        # 8 位客户；没有订单的客户 COUNT 为 0，行仍然保留
        self.assertEqual(len(st["page"]["rows"]), 8)
        self.assertTrue(all(row[1] >= 0 for row in st["page"]["rows"]))

    # -- 写操作：各种伪装都拦截 ---------------------------------------------
    def test_drop_with_leading_comments(self):
        self.assert_denied("-- 看起来是注释\n-- SELECT 1;\nDROP TABLE customers;",
                           "DROP")
        # 表必须还在
        st = self.run_sql("SELECT COUNT(*) FROM customers")
        self.assertEqual(st["status"], "completed")

    def test_insert_with_cte(self):
        self.assert_denied(
            "WITH t AS (SELECT 1 AS x) INSERT INTO customers DEFAULT VALUES;",
            "INSERT",
        )

    def test_update_hidden_in_subquery(self):
        # 标准 SQLite 不允许把 UPDATE 放进 IN 子查询，解析期即失败；
        # 无论如何必须是失败且表数据不变。
        st = self.run_sql(
            "SELECT * FROM customers WHERE customer_id IN "
            "(UPDATE customers SET tier='x' RETURNING customer_id)"
        )
        self.assertEqual(st["status"], "failed")
        st2 = self.run_sql("SELECT COUNT(*) FROM customers WHERE tier='x'")
        self.assertEqual(st2["page"]["rows"][0][0], 0)

    def test_delete_second_statement(self):
        # 多语句：第一条 SELECT 正常 prepare，第二条 DELETE 必须拦截，整条失败
        self.assert_denied("SELECT COUNT(*) FROM customers; DELETE FROM customers;",
                           "DELETE")
        st = self.run_sql("SELECT COUNT(*) FROM customers")
        self.assertEqual(st["page"]["rows"][0][0], 8)

    def test_create_table_denied(self):
        self.assert_denied("CREATE TABLE evil (x INTEGER);", "CREATE")

    def test_alter_table_denied(self):
        self.assert_denied("ALTER TABLE customers ADD COLUMN hack TEXT;", "ALTER")

    def test_begin_transaction_denied(self):
        self.assert_denied("BEGIN; SELECT 1;", "事务")

    def test_additional_ddl_and_write_surfaces_denied(self):
        # 虚表/触发器/视图/维护语句/保存点/变体写法，全部拒绝且无副作用
        cases = [
            "CREATE VIRTUAL TABLE evil USING fts5(content);",
            "CREATE TRIGGER t AFTER INSERT ON customers BEGIN SELECT 1; END;",
            "CREATE VIEW v AS SELECT 1;",
            "REINDEX;",
            "ANALYZE;",
            "SAVEPOINT sp1;",
            "REPLACE INTO customers (customer_id) VALUES (99);",
            "INSERT INTO customers(customer_id) VALUES (1) ON CONFLICT DO NOTHING;",
        ]
        for sql in cases:
            st = self.run_sql(sql)
            self.assertEqual(st["status"], "failed", sql)
            self.assertEqual(st["error"]["stop_reason"], "denied", sql)
        # 没有任何新对象被创建，原表行数不变
        st = self.run_sql("SELECT COUNT(*) FROM customers")
        self.assertEqual(st["page"]["rows"][0][0], 8)

    # -- ATTACH / PRAGMA / 危险函数 -----------------------------------------
    def test_attach_denied(self):
        self.assert_denied("ATTACH DATABASE '/tmp/evil_xyz.db' AS evil;", "ATTACH")

    def test_detach_denied(self):
        self.assert_denied("DETACH DATABASE main;", "DETACH")

    def test_pragma_denied(self):
        self.assert_denied("PRAGMA database_list;", "PRAGMA")
        self.assert_denied("PRAGMA journal_mode;", "PRAGMA")

    def test_only_recursive_triggers_pragma_allowed(self):
        # 唯一白名单 PRAGMA：不返回结果集也不泄露环境，仅解除递归深度
        st = self.run_sql("PRAGMA recursive_triggers = ON; SELECT 1 AS ok;")
        self.assertEqual(st["status"], "completed")
        self.assertEqual(st["page"]["rows"][0][0], 1)
        # 其他 PRAGMA 仍拒绝
        self.assert_denied("PRAGMA table_info(customers);", "PRAGMA")

    def test_load_extension_denied(self):
        self.assert_denied("SELECT load_extension('/tmp/evil.so');",
                           "load_extension")

    def test_readfile_denied(self):
        # 标准构建无 readfile 函数：无论被白名单拦截还是解析期失败，
        # 都必须失败且不会触达文件系统。
        st = self.run_sql("SELECT readfile('/etc/passwd');")
        self.assertEqual(st["status"], "failed")
        self.assertIn("readfile", st["error"]["message"])

    def test_unknown_function_denied(self):
        st = self.run_sql("SELECT nonexistent_func(1);")
        self.assertEqual(st["status"], "failed")
        self.assertIn("nonexistent_func", st["error"]["message"])

    def test_allowed_builtin_functions(self):
        st = self.run_sql(
            "SELECT UPPER(name), ROUND(1.234, 2), COALESCE(NULL, 'x') FROM customers LIMIT 1"
        )
        self.assertEqual(st["status"], "completed")

    # -- 只读连接的物理保证 --------------------------------------------------
    def test_connection_is_physically_readonly(self):
        from app.dbguard import open_readonly_connection
        conn, _ = open_readonly_connection(self.space.data_db)
        try:
            with self.assertRaises(sqlite3.OperationalError):
                conn.execute("CREATE TABLE physically_blocked (x INT)")
        finally:
            conn.close()

    # -- 错误定位与脱敏 ------------------------------------------------------
    def test_error_points_to_statement_index(self):
        st = self.run_sql("SELECT 1; SELECT * FROM no_such_table;")
        self.assertEqual(st["status"], "failed")
        self.assertEqual(st["error"]["statement_number"], 2)
        self.assertIn("no_such_table", st["error"]["message"])

    def test_error_message_has_no_server_path(self):
        st = self.run_sql("SELECT * FROM no_such_table")
        msg = st["error"]["message"]
        self.assertNotIn("/", msg)
        self.assertNotIn(self.tmpdir.name, msg)

    def test_comment_only_rejected(self):
        with self.assertRaises(SQLError):
            self.svc.submit(self.space, "-- only a comment\n   -- another")

    def test_multi_select_statements_last_result_returned(self):
        # 前置 SELECT 允许但不返回；最终结果取最后一条
        st = self.run_sql("SELECT 1; SELECT 2 AS last_value;")
        self.assertEqual(st["status"], "completed")
        self.assertEqual(st["columns"], ["last_value"])
        self.assertEqual(st["page"]["rows"][0][0], 2)
