"""错误类型与错误信息脱敏。

所有面向前端的错误都经过 :func:`sanitize_message` 处理：
- 不泄露服务器绝对路径（SQLite 报错可能附带 db 文件路径）；
- 不泄露内部配置（DATA_ROOT、环境变量等）。
"""
from __future__ import annotations

import re

from . import config


class SQLError(Exception):
    """用户可理解、可定位到具体语句的查询错误。"""

    def __init__(self, message: str, statement_index: int | None = None,
                 statement_text: str | None = None, stop_reason: str = config.STOP_ERROR):
        super().__init__(message)
        self.message = message
        self.statement_index = statement_index          # 0-based，第几条语句出错
        self.statement_text = statement_text            # 出错语句原文（截断）
        self.stop_reason = stop_reason

    def to_dict(self) -> dict:
        d = {"message": sanitize_message(self.message)}
        if self.statement_index is not None:
            d["statement_index"] = self.statement_index
            d["statement_number"] = self.statement_index + 1
        if self.statement_text:
            d["statement"] = truncate_sql(self.statement_text)
        d["stop_reason"] = self.stop_reason
        return d


class ValidationError(Exception):
    """导入/参数校验错误（纯业务信息，本身不含敏感内容）。"""


# 匹配 *nix 绝对路径与 Windows 盘符路径（要求路径段以点/字母数字下划线/连字符组成，
# 避免把普通句子中的 "/" 误伤成路径）
_PATH_RE = re.compile(
    r"(?:file:)?(?:/[A-Za-z0-9_.\-]+){2,}(?:/[A-Za-z0-9_.\-]*)*"
    r"|[A-Za-z]:[\\/][\w.\\\-]+"
)
_HOME_RE = re.compile(r"/home/[A-Za-z0-9_.\-]+|/root(?![\w])|/tmp/[A-Za-z0-9_.\-/]*")


def sanitize_message(message: str) -> str:
    """剥除错误文本中可能出现的服务端私有路径。"""
    if not message:
        return message
    text = str(message)
    # 先屏蔽数据根目录，给出中性占位
    text = text.replace(str(config.DATA_ROOT), "<data>")
    text = _HOME_RE.sub("<path>", text)
    text = _PATH_RE.sub("<path>", text)
    return text


def truncate_sql(sql: str, limit: int = 300) -> str:
    collapsed = " ".join(sql.split())
    return collapsed if len(collapsed) <= limit else collapsed[:limit] + "…"


def sqlite_error_message(exc: Exception) -> str:
    """从 sqlite3 异常提取稳定、可读的文本。"""
    return sanitize_message(str(exc))
