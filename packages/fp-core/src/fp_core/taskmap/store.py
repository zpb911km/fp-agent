"""TaskMapStore — 任务图存储层

单文件 JSON(.fp/tasks.json, 项目本地)。

- **原子写**: tempfile.mkstemp → os.replace(替换旧的裸 open().write(), 避免
  写入中途崩溃导致文件半截)。
- **容错读**: 解析失败备份 .bak; 单条坏数据跳过并告警。
- **旧格式惰性迁移**: 读到 v1 的 {tasks:[...]} 就地转成 v2 {maps:[...]},
  真正的落盘发生在下一次 save。
- **并发**: 本层**不加文件锁**。安全性依赖调用方的「单写者」纪律——
  编排器是唯一落图者, 自然串行。docstring 在此明示, 不假装线程安全。
"""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
from typing import Any, cast

from fp_core.logger import get_logger

from .models import SCHEMA_VERSION, TERMINAL_MAP_STATUSES, TaskMap

TASKS_FILE = os.path.join(".fp", "tasks.json")


class TaskMapStore:
    """任务图存储(JSON 文件 CRUD)"""

    def __init__(self, file_path: str | None = None):
        self._file_path = file_path or TASKS_FILE

    @property
    def file_path(self) -> str:
        return self._file_path

    # ── 读 ─────────────────────────────────────────

    def load(self) -> tuple[list[TaskMap], int]:
        """加载 (maps, next_id)。

        容错: 文件不存在 → 空; JSON 损坏 → 告警 + 备份 .bak 后返回空
        (不静默清空, 避免任务「凭空消失」); 单条坏数据 → 跳过并告警。
        """
        if not os.path.exists(self._file_path):
            return [], 1

        try:
            with open(self._file_path, encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            get_logger().error(f"[task] ⚠️ tasks.json 解析失败({e}), 已备份为 {self._file_path}.bak")
            with contextlib.suppress(OSError):
                os.replace(self._file_path, self._file_path + ".bak")
            return [], 1

        if not isinstance(data, dict):
            get_logger().warning("[task] ⚠️ tasks.json 顶层结构异常(非对象), 按空处理")
            return [], 1

        data = cast(dict[str, Any], data)

        # v1(线性清单) → 惰性迁移
        if "maps" not in data and "tasks" in data:
            return self._migrate_legacy(data)

        maps: list[TaskMap] = []
        for idx, item in enumerate(data.get("maps", [])):
            try:
                maps.append(TaskMap.from_dict(item))
            except Exception as e:  # noqa: BLE001 — 单条坏数据不应拖垮整份
                get_logger().warning(f"[task] ⚠️ 跳过坏任务图 #{idx}: {e}")

        return maps, int(data.get("next_id", 1))

    def _migrate_legacy(self, data: dict[str, Any]) -> tuple[list[TaskMap], int]:
        maps: list[TaskMap] = []
        for idx, item in enumerate(data.get("tasks", [])):
            try:
                maps.append(TaskMap.from_legacy(item))
            except Exception as e:  # noqa: BLE001
                get_logger().warning(f"[task] ⚠️ 旧任务迁移失败 #{idx}: {e}")
        if maps:
            get_logger().info(f"[task] 旧格式迁移: {len(maps)} 个任务 → 任务图 v{SCHEMA_VERSION}")
        return maps, int(data.get("next_id", 1))

    # ── 写 ─────────────────────────────────────────

    def save(self, maps: list[TaskMap], next_id: int) -> None:
        """整份落盘(原子)"""
        payload = {
            "version": SCHEMA_VERSION,
            "next_id": next_id,
            "maps": [m.to_dict() for m in maps],
        }
        self._atomic_write(payload)

    def _atomic_write(self, payload: dict[str, Any]) -> None:
        d = os.path.dirname(self._file_path) or "."
        os.makedirs(d, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=d, prefix=".tasks_", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
            os.replace(tmp, self._file_path)
        finally:
            if os.path.exists(tmp):
                with contextlib.suppress(OSError):
                    os.unlink(tmp)

    # ── CRUD ───────────────────────────────────────

    @staticmethod
    def _coerce_id(map_id: Any) -> int | None:
        """类型宽容: 2 / "2" / 2.0 → 2(LLM 常把整数序列化成字符串)。"""
        try:
            return int(map_id)
        except (TypeError, ValueError):
            return None

    def create(self, title: str, goal: str = "") -> TaskMap:
        maps, next_id = self.load()
        m = TaskMap.create(next_id, title, goal)
        maps.append(m)
        self.save(maps, next_id + 1)
        return m

    def get(self, map_id: Any) -> TaskMap | None:
        want = self._coerce_id(map_id)
        raw = str(map_id).strip()
        for m in self.list_all():
            if (want is not None and m.id == want) or str(m.id) == raw:
                return m
        return None

    def save_map(self, m: TaskMap) -> bool:
        """按 id 替换一个 map 并落盘(保留其他 map 与 next_id)。"""
        maps, next_id = self.load()
        for i, x in enumerate(maps):
            if x.id == m.id:
                m.touch()
                maps[i] = m
                self.save(maps, next_id)
                return True
        return False

    def list_all(self) -> list[TaskMap]:
        maps, _ = self.load()
        return maps

    def clear_terminal(self) -> int:
        """清除终态 map(completed + superseded), 返回清除数量。

        delivered 不是终态, 不会被清除(它代表「等用户批准」, 清掉会丢交付物)。
        """
        maps, next_id = self.load()
        keep = [m for m in maps if m.status not in TERMINAL_MAP_STATUSES]
        cleared = len(maps) - len(keep)
        if cleared == 0:
            return 0
        self.save(keep, next_id)
        return cleared
