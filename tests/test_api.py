"""端到端 HTTP 测试：真实启动 ThreadingHTTPServer，覆盖完整 API 链路。"""
from __future__ import annotations

import io
import json
import threading
import time
import unittest
import urllib.error
import urllib.request
import uuid
from urllib.parse import urlencode

from tests.base import ROOT, TempDataTestCase


class HttpServerFixture:
    """每个测试类启动一次服务，数据目录仍由用例的 tmpdir 提供。"""

    server = None
    thread = None
    base_url = ""

    @classmethod
    def start(cls, data_root: str):
        import os
        os.environ["SQLAB_DATA_ROOT"] = data_root
        from app import config
        config.DATA_ROOT = __import__("pathlib").Path(data_root)
        config.SPACES_ROOT = config.DATA_ROOT / "spaces"
        config.QUERY_TIMEOUT_MS = 1000  # 端到端测试用较短超时
        from app.server import build_server
        cls.server = build_server("127.0.0.1", 0)
        cls.base_url = f"http://127.0.0.1:{cls.server.server_address[1]}"
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def stop(cls):
        if cls.server:
            cls.server.shutdown()
            cls.server.server_close()


class ApiEndToEndTests(TempDataTestCase):
    @classmethod
    def setUpClass(cls):
        cls._http = HttpServerFixture()

    def setUp(self):
        super().setUp()
        # 让 server 使用本用例的临时目录
        self._http.start(self.tmpdir.name)

    def tearDown(self):
        self._http.stop()
        super().tearDown()

    # -- HTTP 工具 ----------------------------------------------------------
    def request(self, method: str, path: str, body=None, raw: bytes | None = None,
                content_type: str = "application/json"):
        url = self._http.base_url + path
        data = None
        headers = {}
        if raw is not None:
            data = raw
            headers["Content-Type"] = content_type
        elif body is not None:
            data = json.dumps(body, ensure_ascii=False).encode()
            headers["Content-Type"] = content_type
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                payload = json.loads(resp.read().decode())
                return resp.status, payload, dict(resp.headers)
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read().decode()), dict(e.headers)

    def multipart(self, path: str, filename: str, content: bytes,
                  table_name: str | None = None):
        boundary = "----test" + uuid.uuid4().hex
        parts = []
        if table_name:
            parts.append(
                f'--{boundary}\r\nContent-Disposition: form-data; name="table_name"'
                f'\r\n\r\n{table_name}\r\n'.encode()
            )
        parts.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="file"; '
            f'filename="{filename}"\r\nContent-Type: text/csv\r\n\r\n'.encode()
            + content + b"\r\n"
        )
        parts.append(f"--{boundary}--\r\n".encode())
        return self.request("POST", path, raw=b"".join(parts),
                            content_type=f"multipart/form-data; boundary={boundary}")

    def poll(self, space_id: str, query_id: str, timeout: float = 10.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            _, payload, _ = self.request(
                "GET", f"/api/spaces/{space_id}/queries/{query_id}")
            if payload["data"]["status"] != "running":
                return payload["data"]
            time.sleep(0.1)
        self.fail("查询轮询超时")

    # -- 用例 ---------------------------------------------------------------
    def test_full_workflow_space_import_query_page_download(self):
        # 1. 新建空间
        status, payload, _ = self.request("POST", "/api/spaces",
                                          {"name": "端到端空间"})
        self.assertEqual(status, 201)
        sid = payload["data"]["id"]

        # 2. 上传 CSV
        csv_content = "id,region,amount\n1,华东,10.5\n2,华北,20\n3,华东,30.5\n".encode()
        status, payload, _ = self.multipart(
            f"/api/spaces/{sid}/import", "sales.csv", csv_content, "sales")
        self.assertEqual(status, 201)
        self.assertEqual(payload["data"]["table"], "sales")

        # 3. 表结构
        _, payload, _ = self.request("GET", f"/api/spaces/{sid}/tables")
        self.assertEqual(payload["data"][0]["name"], "sales")
        self.assertEqual(payload["data"][0]["row_count"], 3)

        # 4. 提交查询并轮询
        _, payload, _ = self.request(
            "POST", f"/api/spaces/{sid}/query",
            {"sql": "SELECT region, SUM(amount) AS total FROM sales GROUP BY region "
                    "ORDER BY total DESC"})
        self.assertEqual(status, 201)
        qid = payload["data"]["query_id"]
        result = self.poll(sid, qid)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["columns"], ["region", "total"])
        self.assertEqual(result["page"]["rows"][0], ["华东", 41.0])

        # 5. 分页
        rid = result["result_id"]
        _, payload, _ = self.request(
            "GET", f"/api/spaces/{sid}/results/{rid}?offset=1&limit=1")
        page = payload["data"]
        self.assertEqual(page["offset"], 1)
        self.assertEqual(len(page["rows"]), 1)
        # 共 2 个分组，最后一页没有更多
        self.assertFalse(page["has_more"])

        # 6. 下载 CSV
        with urllib.request.urlopen(
                f"{self._http.base_url}/api/spaces/{sid}/results/{rid}/download",
                timeout=10) as resp:
            body = resp.read().decode("utf-8-sig")
            self.assertEqual(resp.headers["X-Result-Truncated"], "0")
        self.assertIn("region,total", body)
        self.assertIn("华东,41.0", body)

    def test_sample_endpoint_and_complex_query(self):
        _, payload, _ = self.request("POST", "/api/spaces", {"name": "样本空间"})
        sid = payload["data"]["id"]
        status, payload, _ = self.request("POST", f"/api/spaces/{sid}/sample")
        self.assertEqual(status, 200)
        self.assertEqual(len(payload["data"]["imported"]), 4)

        _, payload, _ = self.request(
            "POST", f"/api/spaces/{sid}/query",
            {"sql": "WITH oa AS (SELECT c.region, SUM(oi.quantity*p.unit_price) amt "
                    "FROM orders o JOIN customers c ON c.customer_id=o.customer_id "
                    "JOIN order_items oi ON oi.order_id=o.order_id "
                    "JOIN products p ON p.product_id=oi.product_id "
                    "WHERE o.status<>'cancelled' GROUP BY c.region, o.order_id) "
                    "SELECT region, ROUND(SUM(amt),2) FROM oa GROUP BY region "
                    "ORDER BY 2 DESC"})
        result = self.poll(sid, payload["data"]["query_id"])
        self.assertEqual(result["status"], "completed")
        self.assertTrue(len(result["page"]["rows"]) >= 2)

    def test_attack_queries_blocked_over_http(self):
        _, payload, _ = self.request("POST", "/api/spaces", {"name": "攻击空间"})
        sid = payload["data"]["id"]
        self.request("POST", f"/api/spaces/{sid}/sample")
        attacks = [
            ("-- x\nDROP TABLE customers;", "denied"),
            ("WITH t AS (SELECT 1 x) INSERT INTO customers DEFAULT VALUES;", "denied"),
            ("SELECT 1; DELETE FROM customers;", "denied"),
            ("ATTACH DATABASE '/tmp/x.db' AS x;", "denied"),
            ("SELECT load_extension('/tmp/x.so');", "denied"),
            # 标准构建无 readfile：白名单拦截或解析失败，但必须失败
            ("SELECT readfile('/etc/passwd');", None),
            ("PRAGMA database_list;", "denied"),
        ]
        for sql, expect_reason in attacks:
            _, payload, _ = self.request(
                "POST", f"/api/spaces/{sid}/query", {"sql": sql})
            result = self.poll(sid, payload["data"]["query_id"])
            self.assertEqual(result["status"], "failed", sql)
            if expect_reason:
                self.assertEqual(result["error"]["stop_reason"], expect_reason, sql)
        # customers 表仍在且数据完好
        _, payload, _ = self.request(
            "POST", f"/api/spaces/{sid}/query",
            {"sql": "SELECT COUNT(*) FROM customers"})
        result = self.poll(sid, payload["data"]["query_id"])
        self.assertEqual(result["page"]["rows"][0][0], 8)

    def test_cancel_over_http(self):
        from app import config
        config.QUERY_TIMEOUT_MS = 30_000   # 保证取消测试不是超时触发
        _, payload, _ = self.request("POST", "/api/spaces", {"name": "取消空间"})
        sid = payload["data"]["id"]
        _, payload, _ = self.request("POST", f"/api/spaces/{sid}/sample")
        heavy = (
            "WITH RECURSIVE r(n, s) AS ("
            "SELECT 1, '" + "ab" * 200 + "' UNION ALL "
            "SELECT n+1, substr(s || '" + "ab" * 200 + "', 1, 400) "
            "FROM r WHERE n < 100000000) "
            "SELECT count(*) FROM r "
            "WHERE s GLOB '*a*b*a*b*a*b*a*b*a*b*a*b*a*b*'"
        )
        _, payload, _ = self.request(
            "POST", f"/api/spaces/{sid}/query", {"sql": heavy})
        qid = payload["data"]["query_id"]
        time.sleep(0.3)
        status, payload, _ = self.request(
            "POST", f"/api/spaces/{sid}/cancel", {"query_id": qid})
        self.assertEqual(status, 200)
        result = self.poll(sid, qid)
        self.assertEqual(result["error"]["stop_reason"], "cancelled")

        # 历史可查
        _, payload, _ = self.request("GET", f"/api/spaces/{sid}/history")
        self.assertTrue(any(
            h.get("stop_reason") == "cancelled" for h in payload["data"]))

    def test_truncated_download_is_marked(self):
        from app import config
        config.QUERY_MAX_ROWS = 3
        _, payload, _ = self.request("POST", "/api/spaces", {"name": "截断空间"})
        sid = payload["data"]["id"]
        self.request("POST", f"/api/spaces/{sid}/sample")
        _, payload, _ = self.request(
            "POST", f"/api/spaces/{sid}/query",
            {"sql": "SELECT * FROM customers CROSS JOIN products"})
        result = self.poll(sid, payload["data"]["query_id"])
        self.assertTrue(result["truncated"])
        rid = result["result_id"]
        with urllib.request.urlopen(
                f"{self._http.base_url}/api/spaces/{sid}/results/{rid}/download",
                timeout=10) as resp:
            body = resp.read().decode("utf-8-sig")
            self.assertEqual(resp.headers["X-Result-Truncated"], "1")
        self.assertIn("结果已被截断", body)

    def test_spaces_are_isolated(self):
        _, p1, _ = self.request("POST", "/api/spaces", {"name": "空间A"})
        _, p2, _ = self.request("POST", "/api/spaces", {"name": "空间B"})
        a, b = p1["data"]["id"], p2["data"]["id"]
        self.multipart(f"/api/spaces/{a}/import", "t.csv", b"v\n1\n", "only_in_a")
        _, tables_a, _ = self.request("GET", f"/api/spaces/{a}/tables")
        _, tables_b, _ = self.request("GET", f"/api/spaces/{b}/tables")
        self.assertEqual(len(tables_a["data"]), 1)
        self.assertEqual(tables_b["data"], [])
        # B 空间查询 A 的表必须报 no such table，且错误不带路径
        _, payload, _ = self.request(
            "POST", f"/api/spaces/{b}/query", {"sql": "SELECT * FROM only_in_a"})
        result = self.poll(b, payload["data"]["query_id"])
        self.assertEqual(result["status"], "failed")
        self.assertIn("no such table", result["error"]["message"])
        self.assertNotIn("/", result["error"]["message"])

    def test_history_and_unknown_space(self):
        _, payload, _ = self.request("GET", "/api/spaces/nonexistent-id/tables")
        self.assertEqual(payload["ok"], False)

    def test_static_index_served(self):
        with urllib.request.urlopen(self._http.base_url + "/", timeout=10) as resp:
            html = resp.read().decode()
        self.assertIn("SQL 数据空间分析台", html)


if __name__ == "__main__":
    unittest.main()
