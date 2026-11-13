"""数据空间（workspace）管理。

每个空间一个独立目录，物理隔离：
    data/spaces/<space_id>/
        meta.json     空间元数据（名称、创建时间）
        space.db      用户 CSV 导入的数据表（只读查询连接只打开这个文件）
        history.db    执行历史（用户 SQL 无法触达，路径不暴露给查询层）
"""
from __future__ import annotations

import json
import threading
import time
import uuid
from pathlib import Path

from . import config
from .errors import ValidationError


class Space:
    def __init__(self, space_id: str, name: str, created_at: float, dirpath: Path):
        self.id = space_id
        self.name = name
        self.created_at = created_at
        self.dir = dirpath
        self.data_db = dirpath / "space.db"
        self.history_db = dirpath / "history.db"
        # 同一空间的导入串行化（SQLite 写锁 + 应用层校验原子性）
        self.import_lock = threading.Lock()
        # 历史写入串行化（短连接写入，锁内快速完成）
        self.history_lock = threading.Lock()

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "created_at": self.created_at,
        }


class SpaceRegistry:
    def __init__(self, root: Path | None = None):
        self.root = root or config.SPACES_ROOT
        self.root.mkdir(parents=True, exist_ok=True)
        # space_id -> Space；进程内锁注册表
        self._spaces: dict[str, Space] = {}
        self._registry_lock = threading.Lock()
        self._load_existing()

    def _load_existing(self) -> None:
        for child in sorted(self.root.iterdir()):
            meta_file = child / "meta.json"
            if child.is_dir() and meta_file.exists():
                try:
                    meta = json.loads(meta_file.read_text(encoding="utf-8"))
                    space = Space(meta["id"], meta["name"], meta["created_at"], child)
                    self._spaces[space.id] = space
                except (KeyError, json.JSONDecodeError):
                    # 损坏的元数据目录跳过，不影响其他空间
                    continue

    def create(self, name: str) -> Space:
        name = (name or "").strip()
        if not name:
            raise ValidationError("空间名称不能为空")
        if len(name) > 64:
            raise ValidationError("空间名称最长 64 个字符")
        space_id = uuid.uuid4().hex
        dirpath = self.root / space_id
        with self._registry_lock:
            dirpath.mkdir(parents=True, exist_ok=False)
            space = Space(space_id, name, time.time(), dirpath)
            (dirpath / "meta.json").write_text(
                json.dumps(space.to_dict(), ensure_ascii=False), encoding="utf-8"
            )
            self._spaces[space_id] = space
        # 初始化两个数据库文件
        self._init_data_db(space)
        self._init_history_db(space)
        return space

    @staticmethod
    def _init_data_db(space: Space) -> None:
        import sqlite3
        conn = sqlite3.connect(space.data_db, timeout=10.0)
        try:
            # WAL 让长读查询不阻塞另一个连接的导入写入
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA foreign_keys=ON")
        finally:
            conn.close()

    @staticmethod
    def _init_history_db(space: Space) -> None:
        from .history import init_history_db
        init_history_db(space)

    def get(self, space_id: str) -> Space:
        space = self._spaces.get(space_id)
        if space is None:
            raise ValidationError(f"数据空间不存在：{space_id}")
        return space

    def list(self) -> list[Space]:
        return sorted(self._spaces.values(), key=lambda s: s.created_at, reverse=True)

    def delete(self, space_id: str) -> None:
        space = self.get(space_id)
        with self._registry_lock:
            # 简单的文件级删除；删除期间不应有进行中的导入（UI 侧控制）
            for suffix in ("", "-wal", "-shm", "-journal"):
                p = Path(str(space.data_db) + suffix)
                if p.exists():
                    p.unlink()
                p = Path(str(space.history_db) + suffix)
                if p.exists():
                    p.unlink()
            meta = space.dir / "meta.json"
            if meta.exists():
                meta.unlink()
            try:
                space.dir.rmdir()
            except OSError:
                pass
            self._spaces.pop(space_id, None)
