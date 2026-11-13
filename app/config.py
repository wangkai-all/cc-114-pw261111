"""全局配置：所有限额集中在此，方便审计与调整。"""
from __future__ import annotations

import os
from pathlib import Path

# 运行时数据根目录（可通过环境变量覆盖，便于测试）
DATA_ROOT = Path(os.environ.get("SQLAB_DATA_ROOT", Path(__file__).resolve().parent.parent / "data"))
SPACES_ROOT = DATA_ROOT / "spaces"

# ---- 查询限额 ----
QUERY_TIMEOUT_MS = int(os.environ.get("SQLAB_QUERY_TIMEOUT_MS", 5000))      # 单次查询最长执行时间
QUERY_MAX_ROWS = int(os.environ.get("SQLAB_QUERY_MAX_ROWS", 1000))          # 单次查询最多返回行数
QUERY_SQL_MAX_BYTES = 64 * 1024                                            # SQL 文本大小上限
QUERY_PROGRESS_TICK_OPS = 200                                              # 每 N 条 VM 指令回调一次进度检查
QUERY_PAGE_SIZE_DEFAULT = 50
QUERY_PAGE_SIZE_MAX = 200

# ---- 导入限额 ----
IMPORT_MAX_UPLOAD_BYTES = 10 * 1024 * 1024                                # 单个 CSV 上传大小
IMPORT_MAX_ROWS = 200_000                                                  # 单个 CSV 数据行数
IMPORT_MAX_COLUMNS = 200
IMPORT_TABLE_NAME_MAX = 64

# ---- 结果快照缓存（分页/下载共用同一份物化结果）----
RESULT_TTL_SECONDS = 600
RESULT_MAX_SNAPSHOTS_PER_SPACE = 20
HISTORY_MAX_ROWS_PER_SPACE = 200

# ---- 标识符规则 ----
# 表名/列名只允许字母、数字、下划线，且以字母或下划线开头
IDENT_RE = __import__("re").compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

# SQLite 关键字（导入时用作表名/列名一律拒绝，避免用户后续查询踩坑）
SQLITE_KEYWORDS = {
    "abort", "action", "add", "after", "all", "alter", "always", "analyze", "and",
    "as", "asc", "attach", "autoincrement", "before", "begin", "between", "by",
    "cascade", "case", "cast", "check", "collate", "column", "commit", "conflict",
    "constraint", "create", "cross", "current", "current_date", "current_time",
    "current_timestamp", "database", "default", "deferrable", "deferred", "delete",
    "desc", "detach", "distinct", "do", "drop", "each", "else", "end", "escape",
    "except", "exclude", "exclusive", "exists", "explain", "fail", "filter",
    "first", "following", "for", "foreign", "from", "full", "generated", "glob",
    "group", "groups", "having", "if", "ignore", "immediate", "in", "index",
    "indexed", "initially", "inner", "insert", "instead", "intersect", "into",
    "is", "isnull", "join", "key", "last", "left", "like", "limit", "match",
    "natural", "no", "not", "nothing", "notnull", "null", "nulls", "of", "offset",
    "on", "or", "order", "others", "outer", "over", "partition", "plan", "pragma",
    "preceding", "primary", "query", "raise", "range", "recursive", "references",
    "regexp", "reindex", "release", "rename", "replace", "restrict", "returning",
    "right", "rollback", "row", "rows", "savepoint", "select", "set", "table",
    "temp", "temporary", "then", "ties", "to", "transaction", "trigger", "unbounded",
    "union", "unique", "update", "using", "vacuum", "values", "view", "virtual",
    "when", "where", "window", "with", "without",
}

# 历史记录中停止原因枚举
STOP_COMPLETED = "completed"
STOP_LIMIT = "row_limit"
STOP_TIMEOUT = "timeout"
STOP_CANCELLED = "cancelled"
STOP_DENIED = "denied"
STOP_ERROR = "error"

# 结果快照被 UI 分页使用的页大小可选项
PAGE_SIZE_OPTIONS = (20, 50, 100, 200)
