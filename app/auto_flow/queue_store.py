# -*- coding: utf-8 -*-
"""队列持久化适配（T-1）。

规格依据：`docs/spec/自动流程规格-v1.md`「Restart and recovery」「File and module
layout」与 `docs/spec/仓库契约-v1.md` §3 存储契约：

- 状态只写 `BOOTH_DATA` 下的**新**子目录/新文件：`<BOOTH_DATA>/auto_flow/queue.json`
  （测试传临时目录即为数据根）。绝不触碰 `records.jsonl`、`counter.json`、
  `settings.json`、既有照片目录或任何其它既有文件；
- 原子写：同目录临时文件 + fsync + `os.replace`，读者永远看到完整文档；
- 缺失即空队列（不自动创建文件；首次成功写入才落盘）；
- 线程安全：进程内 RLock 保护 load/save；跨进程并发不在本适配契约内（现场只有
  一个服务进程写队列，systemd 单实例）；
- 不删除任何文件：解析失败或版本不符时抛 QueueStoreError，让启动方明确报错，
  由人工处理，绝不自动覆盖、清空或"修复"原文件。

文档形状（QueueService 负责内容语义，本模块只管存取与最小结构校验）::

    {
      "version": 1,
      "updated_at": "...Z",
      "counter": 3,
      "entries": [QueueEntry.to_dict(), ...],
      "idempotency": {request_id: {...}},
      "occupancy": {"queue_id": "...", "acquired_at": "...Z"} | null,
      "notes": [ {...}, ... ]
    }
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
from collections.abc import Mapping
from copy import deepcopy
from pathlib import Path
from typing import Any

__all__ = ["QueueStoreError", "QueueStore", "MemoryQueueStore"]


class QueueStoreError(RuntimeError):
    """队列状态文件不可用：不可读取、非法 JSON、或版本不支持。"""


class QueueStore:
    """JSON 文件持久化适配：`<data_root>/auto_flow/queue.json`。"""

    SCHEMA_VERSION = 1
    SUBDIR = "auto_flow"
    FILENAME = "queue.json"

    def __init__(self, data_root: str | Path) -> None:
        self.root = Path(data_root)
        self.directory = self.root / self.SUBDIR
        self.path = self.directory / self.FILENAME
        self._lock = threading.RLock()

    @classmethod
    def empty_state(cls) -> dict[str, Any]:
        """缺省文档：空队列、无幂等索引、无占用、无诊断记录。"""
        return {
            "version": cls.SCHEMA_VERSION,
            "updated_at": "",
            "counter": 0,
            "entries": [],
            "idempotency": {},
            "occupancy": None,
            "notes": [],
        }

    @classmethod
    def _validate(cls, data: object, *, source: str) -> dict[str, Any]:
        if not isinstance(data, dict):
            raise QueueStoreError(f"队列状态必须是 JSON 对象：{source}")
        version = data.get("version")
        if version != cls.SCHEMA_VERSION:
            raise QueueStoreError(
                f"队列状态文件版本不支持：version={version!r}，本程序支持 {cls.SCHEMA_VERSION}。"
                f"拒绝按未知格式解释，也不会覆盖该文件，请人工处理：{source}"
            )
        missing = sorted({"entries", "idempotency", "counter"} - set(data))
        if missing:
            raise QueueStoreError(f"队列状态缺少必要字段 {missing}：{source}")
        return data

    def load(self) -> dict[str, Any]:
        """读取状态文档；文件不存在返回空状态，其余错误抛 QueueStoreError。"""
        with self._lock:
            if not self.path.is_file():
                return self.empty_state()
            try:
                text = self.path.read_text(encoding="utf-8")
            except OSError as exc:
                raise QueueStoreError(f"读取队列状态文件失败：{self.path}（{exc}）") from exc
            try:
                data = json.loads(text)
            except ValueError as exc:
                raise QueueStoreError(
                    f"队列状态文件不是合法 JSON：{self.path}。"
                    f"服务不会自动覆盖或删除该文件，请人工检查。（{exc}）"
                ) from exc
            return self._validate(data, source=str(self.path))

    def save(self, state: Mapping[str, Any]) -> None:
        """原子写入状态文档：同目录临时文件 -> fsync -> os.replace。"""
        document = dict(state)
        self._validate(document, source="（内存文档）")
        payload = json.dumps(document, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        with self._lock:
            try:
                self.directory.mkdir(parents=True, exist_ok=True)
                handle_fd, tmp_name = tempfile.mkstemp(
                    prefix=self.FILENAME + ".", suffix=".tmp", dir=str(self.directory)
                )
            except OSError as exc:
                raise QueueStoreError(f"准备队列状态目录失败：{self.directory}（{exc}）") from exc
            tmp_path = Path(tmp_name)
            try:
                with os.fdopen(handle_fd, "wb") as handle:
                    handle.write(payload)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(tmp_path, self.path)
            except BaseException:
                # 只清理本次失败写出的临时文件；数据目录里的其它文件一概不动
                try:
                    tmp_path.unlink()
                except OSError:
                    pass
                raise


class MemoryQueueStore(QueueStore):
    """内存适配：接口与 QueueStore 相同、不落盘，供单元测试与假适配器使用。"""

    def __init__(self, initial_state: Mapping[str, Any] | None = None) -> None:
        self.root: Path | None = None
        self.directory: Path | None = None
        self.path: Path | None = None
        self._lock = threading.RLock()
        self._state: dict[str, Any] = (
            deepcopy(dict(initial_state)) if initial_state is not None else self.empty_state()
        )
        self._validate(self._state, source="MemoryQueueStore")

    def load(self) -> dict[str, Any]:
        with self._lock:
            return deepcopy(self._state)

    def save(self, state: Mapping[str, Any]) -> None:
        document = self._validate(dict(state), source="MemoryQueueStore")
        with self._lock:
            self._state = deepcopy(document)
