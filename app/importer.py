"""CSV 导入：先全量校验，再在单事务内建表 + 插入。

安全/正确性要点：
- 表名、列名走严格标识符白名单，拒绝关键字、重名、空名；
- 类型基于全部数据推断（不是只看前几行），仅允许 INTEGER/REAL/TEXT；
- 校验阶段不触碰数据库；校验全部通过后才开启事务，
  任何一步失败立即 ROLLBACK，磁盘上不会留下半张表；
- 插入全部使用参数化语句，CSV 内容不可能注入 SQL；
- 行数、列数、文件大小有硬性上限。
"""
from __future__ import annotations

import csv
import io
import re
import sqlite3

from . import config
from .errors import ValidationError

_TYPE_INTEGER = "INTEGER"
_TYPE_REAL = "REAL"
_TYPE_TEXT = "TEXT"
_TYPE_RANK = {_TYPE_INTEGER: 0, _TYPE_REAL: 1, _TYPE_TEXT: 2}

_INT_RE = re.compile(r"^[+-]?\d+$")
_FLOAT_RE = re.compile(r"^[+-]?(\d+\.\d*|\.\d+|\d+)([eE][+-]?\d+)?$")


# ---------------------------------------------------------------------------
# 类型推断（导入与样本生成共用）
# ---------------------------------------------------------------------------
def infer_scalar(raw: str) -> tuple[str, int | float | str | None]:
    """把单个 CSV 字段转为 (推断类型, 转换后的值)。空字段 → NULL。"""
    if raw is None or raw == "":
        return ("NULL", None)
    if _INT_RE.match(raw):
        try:
            return (_TYPE_INTEGER, int(raw))
        except ValueError:
            pass
    if _FLOAT_RE.match(raw):
        try:
            f = float(raw)
            if f == f and f not in (float("inf"), float("-inf")):
                return (_TYPE_REAL, f)
        except ValueError:
            pass
    return (_TYPE_TEXT, raw)


def merge_type(current: str, new: str) -> str:
    if new == "NULL":
        return current
    if current == "NULL":
        return new
    # 整型 + 浮点 → 浮点；任何一边是文本 → 文本（按提升级别取较高者）
    return current if _TYPE_RANK[current] >= _TYPE_RANK[new] else new


def validate_identifier(name: str, kind: str = "名称") -> str:
    name = (name or "").strip()
    if not name:
        raise ValidationError(f"{kind}不能为空")
    if len(name) > config.IMPORT_TABLE_NAME_MAX:
        raise ValidationError(f"{kind}过长（最长 {config.IMPORT_TABLE_NAME_MAX} 字符）：{name}")
    if not config.IDENT_RE.match(name):
        raise ValidationError(
            f"{kind}只能包含字母、数字、下划线且以字母/下划线开头：{name}"
        )
    if name.lower() in config.SQLITE_KEYWORDS:
        raise ValidationError(f"{kind}不能使用 SQL 保留字：{name}")
    return name


# ---------------------------------------------------------------------------
# CSV 解析与全量校验
# ---------------------------------------------------------------------------
class ParsedCSV:
    def __init__(self, table_name: str, columns: list[str],
                 col_types: list[str], rows: list[tuple]):
        self.table_name = table_name
        self.columns = columns
        self.col_types = col_types
        self.rows = rows


def parse_and_validate(raw_bytes: bytes, proposed_table_name: str) -> ParsedCSV:
    """纯内存校验：不访问数据库，任何问题直接抛 :class:`ValidationError`。"""
    if not raw_bytes:
        raise ValidationError("上传内容为空")
    if len(raw_bytes) > config.IMPORT_MAX_UPLOAD_BYTES:
        raise ValidationError(
            f"CSV 文件超过大小上限 {config.IMPORT_MAX_UPLOAD_BYTES // 1024 // 1024} MB"
        )

    # 兼容带 BOM 的 UTF-8；CSV 统一要求 UTF-8 编码
    try:
        text = raw_bytes.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ValidationError(f"CSV 必须是 UTF-8 编码，解码失败：{exc}") from exc

    reader = csv.reader(io.StringIO(text))
    try:
        header = next(reader)
    except StopIteration:
        raise ValidationError("CSV 没有表头行")

    if not header or all((c or "").strip() == "" for c in header):
        raise ValidationError("CSV 表头不能为空")
    if len(header) > config.IMPORT_MAX_COLUMNS:
        raise ValidationError(f"列数超过上限 {config.IMPORT_MAX_COLUMNS}")

    table_name = validate_identifier(proposed_table_name, "表名")

    # 列名：逐条规则校验 + 冲突检查
    columns: list[str] = []
    seen: dict[str, int] = {}
    for idx, col in enumerate(header):
        col_name = validate_identifier(col, f"第 {idx + 1} 列的列名")
        key = col_name.lower()
        if key in seen:
            raise ValidationError(
                f"列名冲突：'{col_name}' 与第 {seen[key] + 1} 列重名（忽略大小写）"
            )
        seen[key] = idx
        columns.append(col_name)

    col_types = ["NULL"] * len(columns)
    rows: list[tuple] = []
    row_no = 1  # 表头占第 1 行
    for raw_row in reader:
        row_no += 1
        if not raw_row or all((c or "") == "" for c in raw_row):
            continue  # 容忍空行
        if len(raw_row) != len(columns):
            raise ValidationError(
                f"第 {row_no} 行有 {len(raw_row)} 个字段，与表头列数 {len(columns)} 不一致"
            )
        converted: list = []
        for i, field in enumerate(raw_row):
            inferred, value = infer_scalar(field)
            converted.append(value)
            col_types[i] = merge_type(col_types[i], inferred)
        rows.append(tuple(converted))
        if len(rows) >= config.IMPORT_MAX_ROWS:
            # 再 peek 一行判断是否超限
            try:
                extra = next(reader)
                if extra and not all((c or "") == "" for c in extra):
                    raise ValidationError(
                        f"数据行数超过上限 {config.IMPORT_MAX_ROWS} 行"
                    )
            except StopIteration:
                pass

    if not rows:
        raise ValidationError("CSV 没有任何数据行")

    # 全 NULL 的列退化为 TEXT，保证列类型始终确定
    col_types = [t if t != "NULL" else _TYPE_TEXT for t in col_types]
    return ParsedCSV(table_name, columns, col_types, rows)


def existing_tables(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type IN ('table','view') "
        "AND name NOT LIKE 'sqlite_%'"
    ).fetchall()
    return {r[0].lower() for r in rows}


def import_parsed(space, parsed: ParsedCSV) -> dict:
    """在单事务内建表 + 批量插入；失败回滚，不留半张表。"""
    with space.import_lock:
        conn = sqlite3.connect(space.data_db, timeout=30.0)
        try:
            conn.execute("PRAGMA busy_timeout=30000")
            # 表名冲突（与库内已有表，忽略大小写）
            if parsed.table_name.lower() in existing_tables(conn):
                raise ValidationError(f"表 '{parsed.table_name}' 在当前空间已存在")

            col_defs = ", ".join(
                f'"{name}" {ctype}' for name, ctype in zip(parsed.columns, parsed.col_types)
            )
            quoted_cols = ", ".join(f'"{c}"' for c in parsed.columns)
            ddl = f'CREATE TABLE "{parsed.table_name}" ({col_defs})'
            placeholders = ", ".join("?" for _ in parsed.columns)
            insert_sql = (
                f'INSERT INTO "{parsed.table_name}" '
                f"({quoted_cols}) VALUES ({placeholders})"
            )

            # 显式事务：BEGIN 之后任何异常都走 ROLLBACK
            conn.isolation_level = None
            conn.execute("BEGIN IMMEDIATE")
            try:
                conn.execute(ddl)
                conn.executemany(insert_sql, parsed.rows)
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
        finally:
            conn.close()

    return {
        "table": parsed.table_name,
        "columns": [
            {"name": n, "type": t} for n, t in zip(parsed.columns, parsed.col_types)
        ],
        "row_count": len(parsed.rows),
    }


def import_csv(space, raw_bytes: bytes, table_name: str) -> dict:
    """对外入口：校验 + 事务导入。"""
    parsed = parse_and_validate(raw_bytes, table_name)
    return import_parsed(space, parsed)


def list_tables(space) -> list[dict]:
    """浏览当前空间的表结构（走独立的只读连接）。"""
    uri = f"file:{space.data_db}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=10.0)
    try:
        names = [
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name NOT LIKE 'sqlite_%' ORDER BY name"
            ).fetchall()
        ]
        tables = []
        for name in names:
            cols = [
                {"name": row[1], "type": row[2] or "ANY", "notnull": bool(row[3]),
                 "default": row[4], "pk": bool(row[5])}
                for row in conn.execute(f'PRAGMA table_info("{name}")').fetchall()
            ]
            count = conn.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0]
            tables.append({"name": name, "columns": cols, "row_count": count})
        return tables
    finally:
        conn.close()


def drop_table(space, table_name: str) -> None:
    """删除空间内的表（管理操作，非查询通道；严格标识符校验）。"""
    validate_identifier(table_name, "表名")
    with space.import_lock:
        conn = sqlite3.connect(space.data_db, timeout=30.0)
        try:
            if table_name.lower() not in existing_tables(conn):
                raise ValidationError(f"表 '{table_name}' 不存在")
            conn.execute(f'DROP TABLE IF EXISTS "{table_name}"')
        finally:
            conn.close()
