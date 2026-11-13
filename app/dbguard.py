"""SQL 安全护栏（安全核心模块）。

三道防线，全部基于 SQLite 真实的解析/执行机制，而非字符串前缀判断：

1. 连接以 ``mode=ro`` URI 打开，物理上无法写入主库；
2. :meth:`GuardedAuthorizer` 注册到 SQLite 的授权回调，在 statement
   *prepare 阶段*（真正执行之前）对每个动作做裁决。注释、WITH、子查询、
   多语句都会经过同一条路径，因此无法绕过；
3. progress handler 周期性检查截止时间与取消事件，长查询可被打断，
   打断后连接立即归还/关闭，不占用其他查询的资源。

Authorizer 的动作码与返回码均显式断言，避免不同版本 sqlite3 模块常量
缺失时静默放行。
"""
from __future__ import annotations

import sqlite3
import threading
import time
from dataclasses import dataclass, field

from . import config
from .errors import SQLError

# ---------------------------------------------------------------------------
# 显式校验 sqlite3 模块暴露的常量（模块导入期就失败，好过运行时静默放行）
# ---------------------------------------------------------------------------
for _name in (
    "SQLITE_OK", "SQLITE_DENY", "SQLITE_IGNORE",
    "SQLITE_CREATE_INDEX", "SQLITE_CREATE_TABLE", "SQLITE_CREATE_TEMP_INDEX",
    "SQLITE_CREATE_TEMP_TABLE", "SQLITE_CREATE_TEMP_TRIGGER",
    "SQLITE_CREATE_TEMP_VIEW", "SQLITE_CREATE_TRIGGER", "SQLITE_CREATE_VIEW",
    "SQLITE_DELETE", "SQLITE_DROP_INDEX", "SQLITE_DROP_TABLE",
    "SQLITE_DROP_TEMP_INDEX", "SQLITE_DROP_TEMP_TABLE",
    "SQLITE_DROP_TEMP_TRIGGER", "SQLITE_DROP_TEMP_VIEW", "SQLITE_DROP_TRIGGER",
    "SQLITE_DROP_VIEW", "SQLITE_INSERT", "SQLITE_PRAGMA", "SQLITE_READ",
    "SQLITE_SELECT", "SQLITE_TRANSACTION", "SQLITE_UPDATE", "SQLITE_ATTACH",
    "SQLITE_DETACH", "SQLITE_ALTER_TABLE", "SQLITE_REINDEX", "SQLITE_ANALYZE",
    "SQLITE_CREATE_VTABLE", "SQLITE_DROP_VTABLE", "SQLITE_FUNCTION",
    "SQLITE_SAVEPOINT", "SQLITE_RECURSIVE",
):
    assert hasattr(sqlite3, _name), f"当前 sqlite3 模块缺少常量 {_name}"

_OK, _DENY, _IGNORE = sqlite3.SQLITE_OK, sqlite3.SQLITE_DENY, sqlite3.SQLITE_IGNORE

# 写/DDL 类动作 → 人类可读的中文动作名
_WRITE_ACTIONS: dict[int, str] = {
    sqlite3.SQLITE_CREATE_INDEX: "创建索引",
    sqlite3.SQLITE_CREATE_TABLE: "创建表",
    sqlite3.SQLITE_CREATE_TEMP_INDEX: "创建临时索引",
    sqlite3.SQLITE_CREATE_TEMP_TABLE: "创建临时表",
    sqlite3.SQLITE_CREATE_TEMP_TRIGGER: "创建临时触发器",
    sqlite3.SQLITE_CREATE_TEMP_VIEW: "创建临时视图",
    sqlite3.SQLITE_CREATE_TRIGGER: "创建触发器",
    sqlite3.SQLITE_CREATE_VIEW: "创建视图",
    sqlite3.SQLITE_CREATE_VTABLE: "创建虚表",
    sqlite3.SQLITE_DELETE: "DELETE 删除",
    sqlite3.SQLITE_DROP_INDEX: "删除索引",
    sqlite3.SQLITE_DROP_TABLE: "删除表",
    sqlite3.SQLITE_DROP_TEMP_INDEX: "删除临时索引",
    sqlite3.SQLITE_DROP_TEMP_TABLE: "删除临时表",
    sqlite3.SQLITE_DROP_TEMP_TRIGGER: "删除临时触发器",
    sqlite3.SQLITE_DROP_TEMP_VIEW: "删除临时视图",
    sqlite3.SQLITE_DROP_TRIGGER: "删除触发器",
    sqlite3.SQLITE_DROP_VIEW: "删除视图",
    sqlite3.SQLITE_DROP_VTABLE: "删除虚表",
    sqlite3.SQLITE_INSERT: "INSERT 写入",
    sqlite3.SQLITE_UPDATE: "UPDATE 更新",
    sqlite3.SQLITE_ALTER_TABLE: "ALTER TABLE 改表",
    sqlite3.SQLITE_REINDEX: "REINDEX",
    sqlite3.SQLITE_ANALYZE: "ANALYZE",
    sqlite3.SQLITE_TRANSACTION: "开启事务（BEGIN/COMMIT/ROLLBACK）",
    sqlite3.SQLITE_SAVEPOINT: "SAVEPOINT",
}

# 允许在只读查询里调用的函数白名单（全部为确定性、无副作用的内置函数）。
# 不在名单内的函数（load_extension/readfile/writefile/... 及自定义函数）
# 一律拒绝。
_ALLOWED_FUNCTIONS = frozenset({
    # 标量
    "length", "lower", "upper", "trim", "ltrim", "rtrim", "replace",
    "substr", "substring", "instr", "ltrim", "printf", "format", "quote",
    "round", "abs", "min", "max", "coalesce", "ifnull", "nullif", "typeof",
    "cast", "like", "glob", "concat", "concat_ws", "left", "right",
    "lpad", "rpad", "reverse", "hex", "unhex", "unicode", "char", "random",
    "randomblob", "zeroblob", "sign", "mod", "power", "sqrt", "exp", "ln",
    "log", "log10", "log2", "floor", "ceil", "ceiling", "pi", "degrees",
    "radians", "sin", "cos", "tan", "asin", "acos", "atan", "atan2",
    "date", "time", "datetime", "julianday", "unixepoch", "strftime",
    "timediff", "last_insert_rowid",
    # 聚合 / 窗口
    "count", "sum", "total", "avg", "group_concat", "string_agg",
    "every", "some", "offset",
})

# 显式拒绝、并给出针对性提示的危险函数
_DENIED_FUNCTION_HINTS = {
    "load_extension": "加载扩展（load_extension）被禁止",
    "readfile": "读取宿主机文件（readfile）被禁止",
    "writefile": "写入宿主机文件（writefile）被禁止",
    "fsdir": "访问宿主机文件系统（fsdir）被禁止",
    "dbstat": "访问 dbstat 虚表被禁止",
}


@dataclass
class GuardedAuthorizer:
    """SQLite 授权回调的有状态实现（每个连接一个实例）。"""

    _last_denial: str | None = field(default=None, init=False)

    def __call__(self, action: int, arg1: str | None, arg2: str | None,
                 db_name: str | None, trigger_name: str | None) -> int:
        # 1) ATTACH / DETACH：附加外部数据库
        if action == sqlite3.SQLITE_ATTACH:
            return self._deny(f"ATTACH 附加数据库被禁止（目标：{arg1 or '?'}）")
        if action == sqlite3.SQLITE_DETACH:
            return self._deny("DETACH 数据库被禁止")

        # 2) PRAGMA：默认全部拒绝；仅放行 recursive_triggers 这一个
        #    无害设置（它只影响触发器/递归 CTE 的深度限制，不泄露任何
        #    环境信息、不改变文件权限）。其余 PRAGMA（database_list、
        #    journal_mode 等可能探测挂载或改变行为的）一律拒绝。
        if action == sqlite3.SQLITE_PRAGMA:
            if (arg1 or "").lower() == "recursive_triggers":
                return _OK
            return self._deny(f"PRAGMA {arg1 or ''} 被禁止，查询通道仅允许 SELECT")

        # 3) 函数调用：白名单制。
        #    注意 SQLITE_FUNCTION 的参数约定：arg1 为保留位（NULL），
        #    函数名在 arg2，与 SQLITE_READ 的 (表名, 列名) 约定不同。
        if action == sqlite3.SQLITE_FUNCTION:
            fname = (arg2 or arg1 or "").lower()
            if fname in _DENIED_FUNCTION_HINTS:
                return self._deny(_DENIED_FUNCTION_HINTS[fname])
            if fname not in _ALLOWED_FUNCTIONS:
                shown = arg2 or arg1 or "?"
                return self._deny(
                    f"函数 {shown} 不在允许列表中（禁止自定义/文件类函数）"
                )
            return _OK

        # 4) 一切写操作 / DDL / 事务控制
        if action in _WRITE_ACTIONS:
            target = f"（对象：{arg1}）" if arg1 else ""
            label = _WRITE_ACTIONS[action]
            # CREATE/DROP TABLE 在 SQLite 内部表现为对 sqlite_master 的
            # INSERT/DELETE，这里还原成用户视角的 DDL 语义，便于定位。
            if arg1 == "sqlite_master":
                if action == sqlite3.SQLITE_INSERT:
                    label = "CREATE 建表/建视图等 DDL"
                elif action == sqlite3.SQLITE_DELETE:
                    label = "DROP 删表/删视图等 DDL"
                elif action == sqlite3.SQLITE_UPDATE:
                    label = "ALTER 修改表结构等 DDL"
            return self._deny(f"{label} 被禁止，查询通道只读{target}")

        # 5) 只读语义动作：SELECT / READ / 递归 CTE 标记
        if action in (sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ,
                      sqlite3.SQLITE_RECURSIVE):
            return _OK

        # 未知动作：默认拒绝（fail-closed）
        return self._deny(f"未授权的 SQLite 动作码 {action}")

    def _deny(self, reason: str) -> int:
        self._last_denial = reason
        return _DENY

    @property
    def last_denial(self) -> str | None:
        return self._last_denial


@dataclass
class QueryBudget:
    """单次查询的时间预算与取消标志（线程安全）。"""

    timeout_ms: int = config.QUERY_TIMEOUT_MS
    deadline: float = 0.0
    cancel_event: threading.Event = field(default_factory=threading.Event)
    cancelled_by_user: bool = False

    def __post_init__(self) -> None:
        self.deadline = time.monotonic() + self.timeout_ms / 1000.0

    def request_cancel(self) -> None:
        self.cancelled_by_user = True
        self.cancel_event.set()

    def remaining_ms(self) -> int:
        return max(0, int((self.deadline - time.monotonic()) * 1000))

    def tick(self) -> int:
        """progress handler 回调：返回非 0 即中断当前 SQL。"""
        if self.cancel_event.is_set():
            return 1
        if time.monotonic() >= self.deadline:
            return 1
        return 0


def open_readonly_connection(db_path) -> sqlite3.Connection:
    """以只读 URI 模式打开空间数据库连接。

    即使 authorizer 被绕过，文件系统层面也无法写入；且无法通过
    ``ATTACH 'file:...?mode=rwc'`` 新建库（ATTACH 已在 authorizer 拦截，
    同时只读连接本身不允许写附加库）。

    开启 ``recursive_triggers`` 以解除递归 CTE 的默认 1000 深度上限，
    让超时/取消机制真正面对长递归查询时生效。
    """
    uri = f"file:{db_path}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=5.0, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    authorizer = GuardedAuthorizer()
    conn.set_authorizer(authorizer)
    # 该 PRAGMA 已在 authorizer 白名单中；只读连接上它不触碰任何文件
    conn.execute("PRAGMA recursive_triggers = ON")
    return conn, authorizer


def split_statements(sql: str) -> list[str]:
    """把 SQL 文本拆成完整语句列表。

    依赖 ``sqlite3.complete_statement`` 识别语句边界（与 sqlite3 CLI 同一套
    词法规则），能正确处理字符串、标识符引号、注释中的分号。
    注释-only / 纯空白片段会被丢弃；末尾未闭合的语句报错。
    """
    statements: list[str] = []
    buf = ""
    for ch in sql:
        buf += ch
        if ch == ";" and sqlite3.complete_statement(buf):
            stmt = buf.strip()
            if stmt:
                statements.append(stmt)
            buf = ""
    tail = buf.strip()
    if tail:
        # 末尾缺分号但本身是完整语句也接受
        if sqlite3.complete_statement(tail + ";"):
            statements.append(tail)
        else:
            raise SQLError("SQL 语句不完整（可能存在未闭合的引号或括号）",
                           stop_reason=config.STOP_ERROR)
    return statements


def prepare_guarded(conn: sqlite3.Connection, authorizer: GuardedAuthorizer,
                    statement: str, statement_index: int):
    """prepare 一条语句，把 authorizer 的拒绝转成统一的 :class:`SQLError`。

    在 Python 3.11 上使用 ``cursor.execute``：authorizer 回调发生在
    prepare 阶段（任何行被读取/写入之前），因此被拒绝的语句不会产生
    任何副作用；对 SELECT 而言 execute 本身也不会物化全部结果。

    超时/取消引发的 ``interrupted`` OperationalError 必须原样向上抛，
    由调用方区分 stop_reason（不能包装成普通解析错误）。
    """
    try:
        return conn.execute(statement)
    except sqlite3.OperationalError as exc:
        if "interrupt" in str(exc).lower():
            raise
        denial = authorizer.last_denial
        if denial:
            raise SQLError(
                f"第 {statement_index + 1} 条语句被安全策略拦截：{denial}",
                statement_index=statement_index,
                statement_text=statement,
                stop_reason=config.STOP_DENIED,
            ) from exc
        raise SQLError(
            f"第 {statement_index + 1} 条语句无法解析：{exc}",
            statement_index=statement_index,
            statement_text=statement,
        ) from exc
    except sqlite3.DatabaseError as exc:
        denial = authorizer.last_denial
        if denial:
            raise SQLError(
                f"第 {statement_index + 1} 条语句被安全策略拦截：{denial}",
                statement_index=statement_index,
                statement_text=statement,
                stop_reason=config.STOP_DENIED,
            ) from exc
        raise SQLError(
            f"第 {statement_index + 1} 条语句无法解析：{exc}",
            statement_index=statement_index,
            statement_text=statement,
        ) from exc


def interrupted_reason(budget: QueryBudget) -> str:
    """progress handler 中断后判断是用户取消还是超时。"""
    if budget.cancelled_by_user:
        return config.STOP_CANCELLED
    return config.STOP_TIMEOUT


def describe_interrupt(reason: str, statement_index: int | None,
                       statement: str | None) -> SQLError:
    if reason == config.STOP_CANCELLED:
        msg = "查询已被用户取消，连接已释放"
    else:
        msg = f"查询超过执行时间上限（{config.QUERY_TIMEOUT_MS} ms），已被中止"
    return SQLError(msg, statement_index=statement_index,
                    statement_text=statement, stop_reason=reason)
