"""测试共用夹具：每个用例独立的临时数据根目录，并保证 config 全局一致。"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


class TempDataTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        os.environ["SQLAB_DATA_ROOT"] = self.tmpdir.name

        # 直接修改真实的 config 模块对象（其他模块都是 `from . import config`
        # 后用 config.XXX 访问，因此改模块属性即可全局生效）
        from app import config
        self.config = config
        self._saved = {
            "DATA_ROOT": config.DATA_ROOT,
            "SPACES_ROOT": config.SPACES_ROOT,
            "QUERY_TIMEOUT_MS": config.QUERY_TIMEOUT_MS,
            "QUERY_MAX_ROWS": config.QUERY_MAX_ROWS,
            "QUERY_SQL_MAX_BYTES": config.QUERY_SQL_MAX_BYTES,
        }
        config.DATA_ROOT = Path(self.tmpdir.name)
        config.SPACES_ROOT = config.DATA_ROOT / "spaces"
        config.SPACES_ROOT.mkdir(parents=True, exist_ok=True)

        from app.spaces import SpaceRegistry
        from app.queryrunner import service as global_service
        self.registry = SpaceRegistry(config.SPACES_ROOT)
        # 统一使用全局单例：取消注册表、快照缓存与 server 运行时行为一致
        self.svc = global_service
        # 清理上一个用例可能遗留在单例里的任务/快照
        self.svc._jobs.clear()
        self.svc._running.clear()
        self.svc._snapshots.clear()
        self.space = self.registry.create("测试空间")

    def tearDown(self) -> None:
        from app import config
        for key, value in self._saved.items():
            setattr(config, key, value)
        self.tmpdir.cleanup()

    def load_sample(self):
        from app import samples
        return samples.load_sample(self.space)

    def run_sql(self, sql: str, wait: bool = True):
        """提交查询并阻塞到终态，返回最终 status 字典。"""
        import time
        submitted = self.svc.submit(self.space, sql)
        qid = submitted["query_id"]
        if not wait:
            return qid
        for _ in range(500):
            status = self.svc.get_status(self.space.id, qid)
            if status["status"] != "running":
                return status
            time.sleep(0.02)
        self.fail("查询未在预期时间内结束")
