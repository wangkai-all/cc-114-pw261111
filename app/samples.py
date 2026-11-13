"""本地样本数据 + 可直接运行的示例查询。

样本是一个迷你电商库：customers / products / orders / order_items，
通过 :mod:`importer` 的同一条事务通道导入（不吃特殊待遇）。

预置查询分三组：
- join：可实际运行的复杂关联查询（CTE、窗口函数、多表 JOIN）；
- attack：越权/绕过尝试，全部应被安全策略拦截，供页面验证；
- stress：长递归 CTE，用于验证超时与取消。
"""
from __future__ import annotations

import io
import csv

from .importer import parse_and_validate, import_parsed

SAMPLE_TABLES = ("customers", "products", "orders", "order_items")


def _csv_text(header: list[str], rows: list[tuple]) -> bytes:
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(header)
    writer.writerows(rows)
    return buf.getvalue().encode("utf-8")


def _customers_csv() -> bytes:
    rows = [
        (1, "张伟", "华东", "2026-01-05", "gold"),
        (2, "李娜", "华北", "2026-02-11", "silver"),
        (3, "王芳", "华南", "2026-02-20", "gold"),
        (4, "刘洋", "西南", "2026-03-02", "bronze"),
        (5, "陈静", "华东", "2026-03-18", "silver"),
        (6, "赵强", "华北", "2026-04-01", "bronze"),
        (7, "孙丽", "华南", "2026-04-12", "gold"),
        (8, "周杰", "西南", "2026-05-06", "silver"),
    ]
    return _csv_text(
        ["customer_id", "name", "region", "signup_date", "tier"], rows
    )


def _products_csv() -> bytes:
    rows = [
        (101, "机械键盘", "外设", 299.0, 120),
        (102, "无线鼠标", "外设", 89.5, 300),
        (103, "4K 显示器", "显示", 1299.0, 45),
        (104, "USB-C 扩展坞", "配件", 199.0, 210),
        (105, "降噪耳机", "音频", 699.0, 80),
        (106, "摄像头", "外设", 259.0, 60),
    ]
    return _csv_text(
        ["product_id", "product_name", "category", "unit_price", "stock"], rows
    )


def _orders_csv() -> bytes:
    rows = [
        (1001, 1, "2026-05-02", "shipped"),
        (1002, 2, "2026-05-04", "delivered"),
        (1003, 1, "2026-05-07", "delivered"),
        (1004, 3, "2026-05-09", "cancelled"),
        (1005, 4, "2026-05-11", "delivered"),
        (1006, 5, "2026-05-15", "shipped"),
        (1007, 2, "2026-05-18", "delivered"),
        (1008, 7, "2026-05-21", "delivered"),
        (1009, 8, "2026-05-24", "shipped"),
        (1010, 3, "2026-05-27", "delivered"),
        (1011, 6, "2026-06-01", "cancelled"),
        (1012, 7, "2026-06-03", "delivered"),
    ]
    return _csv_text(
        ["order_id", "customer_id", "order_date", "status"], rows
    )


def _order_items_csv() -> bytes:
    rows = [
        (1, 1001, 101, 1),
        (2, 1001, 102, 2),
        (3, 1002, 103, 1),
        (4, 1003, 105, 1),
        (5, 1003, 104, 1),
        (6, 1004, 102, 3),
        (7, 1005, 106, 2),
        (8, 1006, 101, 1),
        (9, 1006, 104, 2),
        (10, 1007, 105, 1),
        (11, 1008, 103, 2),
        (12, 1008, 102, 1),
        (13, 1009, 106, 1),
        (14, 1010, 101, 2),
        (15, 1010, 105, 1),
        (16, 1011, 104, 1),
        (17, 1012, 103, 1),
        (18, 1012, 101, 1),
        (19, 1012, 102, 2),
    ]
    return _csv_text(
        ["item_id", "order_id", "product_id", "quantity"], rows
    )


_BUILDERS = {
    "customers": _customers_csv,
    "products": _products_csv,
    "orders": _orders_csv,
    "order_items": _order_items_csv,
}


def load_sample(space) -> dict:
    """把四张样本表通过标准导入通道装入指定空间。"""
    imported = []
    for table_name in SAMPLE_TABLES:
        raw = _BUILDERS[table_name]()
        parsed = parse_and_validate(raw, table_name)
        imported.append(import_parsed(space, parsed))
    return {"imported": imported, "tables": list(SAMPLE_TABLES)}


# ---------------------------------------------------------------------------
# 预置查询
# ---------------------------------------------------------------------------
JOIN_QUERIES = [
    {
        "title": "各区域销售额排名（窗口函数 + 多表 JOIN）",
        "sql": """
WITH order_amounts AS (
    SELECT c.region,
           o.order_id,
           SUM(oi.quantity * p.unit_price) AS amount
      FROM orders o
      JOIN customers c    ON c.customer_id = o.customer_id
      JOIN order_items oi ON oi.order_id = o.order_id
      JOIN products p     ON p.product_id = oi.product_id
     WHERE o.status <> 'cancelled'
     GROUP BY c.region, o.order_id
)
SELECT region,
       COUNT(*)            AS order_count,
       ROUND(SUM(amount), 2) AS total_amount,
       RANK() OVER (ORDER BY SUM(amount) DESC) AS region_rank
  FROM order_amounts
 GROUP BY region
 ORDER BY total_amount DESC;
""".strip(),
    },
    {
        "title": "客户复购明细与累计消费（CTE + 窗口累计）",
        "sql": """
WITH customer_orders AS (
    SELECT c.customer_id,
           c.name,
           o.order_id,
           o.order_date,
           SUM(oi.quantity * p.unit_price) AS order_amount
      FROM customers c
      JOIN orders o       ON o.customer_id = c.customer_id
      JOIN order_items oi ON oi.order_id = o.order_id
      JOIN products p     ON p.product_id = oi.product_id
     WHERE o.status <> 'cancelled'
     GROUP BY c.customer_id, c.name, o.order_id, o.order_date
)
SELECT name,
       order_date,
       order_amount,
       COUNT(*) OVER (PARTITION BY customer_id) AS total_orders,
       ROUND(SUM(order_amount) OVER (
           PARTITION BY customer_id ORDER BY order_date
           ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
       ), 2) AS cumulative_spend
  FROM customer_orders
 ORDER BY customer_id, order_date;
""".strip(),
    },
    {
        "title": "品类销量与库存周转（子查询 + 外连接）",
        "sql": """
SELECT p.category,
       p.product_name,
       p.stock,
       COALESCE(sold.qty, 0) AS sold_qty,
       ROUND(COALESCE(sold.revenue, 0), 2) AS revenue,
       ROUND(COALESCE(sold.qty, 0) * 1.0 / NULLIF(p.stock, 0), 3) AS turnover
  FROM products p
  LEFT JOIN (
      SELECT oi.product_id,
             SUM(oi.quantity) AS qty,
             SUM(oi.quantity * pr.unit_price) AS revenue
        FROM order_items oi
        JOIN orders o   ON o.order_id = oi.order_id
        JOIN products pr ON pr.product_id = oi.product_id
       WHERE o.status <> 'cancelled'
       GROUP BY oi.product_id
  ) sold ON sold.product_id = p.product_id
 ORDER BY revenue DESC;
""".strip(),
    },
    {
        "title": "被取消订单涉及的客户与商品（NOT IN 子查询写法）",
        "sql": """
SELECT c.name, o.order_id, o.order_date,
       (SELECT GROUP_CONCAT(p.product_name, '、')
          FROM order_items oi
          JOIN products p ON p.product_id = oi.product_id
         WHERE oi.order_id = o.order_id) AS items
  FROM orders o
  JOIN customers c ON c.customer_id = o.customer_id
 WHERE o.order_id IN (
       SELECT order_id FROM orders WHERE status = 'cancelled'
 )
 ORDER BY o.order_date;
""".strip(),
    },
]

ATTACK_QUERIES = [
    {
        "title": "注释伪装后的 DROP（不能只查 SQL 开头）",
        "sql": "-- 看起来是注释\n-- SELECT 1;\nDROP TABLE customers;",
        "expect": "应在 prepare 阶段被拦截，customers 表依然存在",
    },
    {
        "title": "WITH 后接写操作",
        "sql": "WITH t AS (SELECT 1 AS x) INSERT INTO customers DEFAULT VALUES;",
        "expect": "INSERT 被拦截，表数据无变化",
    },
    {
        "title": "子查询里夹带 UPDATE",
        "sql": "SELECT * FROM customers WHERE customer_id IN (UPDATE customers SET tier='x' RETURNING customer_id);",
        "expect": "UPDATE 在 prepare 阶段被拦截",
    },
    {
        "title": "多条语句：先 SELECT 再 DELETE",
        "sql": "SELECT COUNT(*) FROM customers; DELETE FROM customers;",
        "expect": "第二条语句被拦截，整条请求失败",
    },
    {
        "title": "ATTACH 附加外部数据库",
        "sql": "ATTACH DATABASE '/tmp/evil.db' AS evil;",
        "expect": "ATTACH 被禁止，无法附加任何数据库",
    },
    {
        "title": "加载扩展 / 读宿主文件",
        "sql": "SELECT load_extension('/tmp/evil.so');",
        "expect": "函数白名单拒绝 load_extension",
    },
    {
        "title": "readfile 读取宿主机文件",
        "sql": "SELECT readfile('/etc/passwd');",
        "expect": "readfile 不在函数白名单，被拒绝",
    },
    {
        "title": "PRAGMA 探测数据库配置",
        "sql": "PRAGMA database_list;",
        "expect": "PRAGMA 一律禁止，无法探测挂载信息",
    },
]

STRESS_QUERIES = [
    {
        "title": "高成本递归 CTE（触发超时 / 可手动取消）",
        "sql": """
WITH RECURSIVE r(n, s) AS (
    SELECT 1, 'ababababababababababababababababababababababababababababababababababababababababababababababababababababababababababababababababababababababababababababababababababababababababababababababababababababababababababababababababababababababababababababababababababababababababababababababababababababababababababababababababababab'
    UNION ALL
    SELECT n + 1, substr(s || 'ababababababababababababababababababababababababababababababababababababababababababababababababababababababababababababababababababababababababababababababababababababababababababababababababababababababababababababababababababababababababababababababababababababababababababababababababababababababababababababababababababab', 1, 400)
      FROM r
     WHERE n < 100000000
)
SELECT count(*)
  FROM r
 WHERE s GLOB '*a*b*a*b*a*b*a*b*a*b*a*b*a*b*';
""".strip(),
        "expect": "运行超过执行时间上限后自动中止，或点击取消立即停止",
    },
    {
        "title": "深递归扫描（验证取消后连接释放，不阻塞其他查询）",
        "sql": """
WITH RECURSIVE r(n, s) AS (
    SELECT 1, 'abababababababababababababababababababababababababababababababababababababababababababababababababababababababababababababababababababababababababababababababababababababababababababababababababababababababababababababababababababababababababababababababababababababababababababababababababababababababababababababababababab'
    UNION ALL
    SELECT n + 1, substr(s || 'ababababababababababababababababababababababababababababababababababababababababababababababababababababababababababababababababababababababababababababababababababababababababababababababababababababababababababababababababababababababababababababababababababababababababababababababababababababababababababababababababababab', 1, 400)
      FROM r
     WHERE n < 100000000
)
SELECT n, length(s) FROM r;
""".strip(),
        "expect": "点击取消后应立即停止，历史中记录 stop_reason=cancelled，其他查询不受影响",
    },
]
