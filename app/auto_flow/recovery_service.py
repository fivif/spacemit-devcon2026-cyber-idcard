"""恢复凭证的纯存储模块；不推进队列、不签发控制会话、不存参与者资料。

同一实例内线程安全，跨进程写入需由装配方保证单一写者。历史绑定只追加，
过期仅影响查询。临时文件原子替换成功后才提交内存，失败文件保留供排查。
"""

from __future__ import annotations

import json
import math
import os
import re
import secrets
import tempfile
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

__all__ = ["RecoveryRegistry"]

_TOKEN_PATTERN = re.compile(r"[A-Za-z0-9_-]{32}")
_RECORD_FIELDS = {"queue_id", "token", "browser_token", "expires_at"}


def _valid_token(value: Any) -> bool:
    return isinstance(value, str) and _TOKEN_PATTERN.fullmatch(value) is not None


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    document: dict[str, Any] = {}
    for key, value in pairs:
        if key in document:
            raise ValueError("存储文档含重复字段。")
        document[key] = value
    return document


def _read_document(path: Path, default: Any) -> Any:
    try:
        content = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return default
    return json.loads(content, object_pairs_hook=_unique_object)


def _write_document(path: Path, document: Any) -> None:
    payload = json.dumps(
        document, ensure_ascii=False, allow_nan=False, separators=(",", ":")
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", prefix=path.name + ".", suffix=".tmp",
        dir=path.parent, delete=False,
    ) as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
        temporary_path = Path(handle.name)
    os.replace(temporary_path, path)


def _finite_number(value: Any) -> bool:
    return type(value) in (int, float) and math.isfinite(value)


class RecoveryRegistry:
    """恢复 token 与浏览器 token 独立随机；最近绑定与历史入口分开查询。

    bind 保留已知浏览器凭证；未知或格式无效的凭证重新随机签发。有效队列
    token 与期限保持不变，过期后再次绑定创建新记录，旧记录仍留在文件中。
    get 返回该恢复 token 最近一次绑定；current 返回该浏览器最近一次绑定，
    不会在最近记录过期时自动回退到历史记录。所有返回值都是独立副本。
    """

    TOKEN_BYTES = 24

    def __init__(
        self, path: Path, clock: Callable[[], float] = time.time,
        ttl_seconds: float = 604800,
    ) -> None:
        if not callable(clock):
            raise TypeError("clock 必须可调用。")
        if not _finite_number(ttl_seconds) or ttl_seconds <= 0:
            raise ValueError("ttl_seconds 必须为有限正数。")
        self.path = Path(path)
        self._clock = clock
        self._ttl_seconds = ttl_seconds
        self._lock = threading.RLock()
        document = _read_document(self.path, {"version": 1, "records": []})
        self._records = self._validate(document)
        self._by_token: dict[str, dict[str, Any]] = {}
        self._by_browser: dict[str, dict[str, Any]] = {}
        self._by_queue: dict[str, dict[str, Any]] = {}
        for record in self._records:
            self._index(record)

    @staticmethod
    def _validate(document: Any) -> list[dict[str, Any]]:
        if (
            not isinstance(document, dict)
            or set(document) != {"version", "records"}
            or type(document["version"]) is not int
            or document["version"] != 1
            or not isinstance(document["records"], list)
        ):
            raise ValueError("恢复存储格式或版本无效。")
        tokens: dict[str, tuple[str, float]] = {}
        browsers: set[str] = set()
        for record in document["records"]:
            if (
                not isinstance(record, dict)
                or set(record) != _RECORD_FIELDS
                or not isinstance(record["queue_id"], str)
                or not record["queue_id"].strip()
                or record["queue_id"] != record["queue_id"].strip()
                or not _valid_token(record["token"])
                or not _valid_token(record["browser_token"])
                or not _finite_number(record["expires_at"])
            ):
                raise ValueError("恢复存储记录无效。")
            identity = (record["queue_id"], record["expires_at"])
            previous = tokens.setdefault(record["token"], identity)
            if previous != identity:
                raise ValueError("恢复凭证绑定不一致。")
            browsers.add(record["browser_token"])
        if tokens.keys() & browsers:
            raise ValueError("恢复凭证与浏览器凭证不能混用。")
        return document["records"]

    def _index(self, record: dict[str, Any]) -> None:
        self._by_token[record["token"]] = record
        self._by_browser[record["browser_token"]] = record
        self._by_queue[record["queue_id"]] = record

    def _now(self) -> float:
        value = self._clock()
        if not _finite_number(value):
            raise ValueError("clock 必须返回有限时间戳。")
        return value

    def _new_token(self, reserved: set[str]) -> str:
        while True:
            token = secrets.token_urlsafe(self.TOKEN_BYTES)
            if token not in reserved:
                return token

    def bind(self, queue_id: str, browser_token: Any = None) -> dict[str, Any]:
        if not isinstance(queue_id, str) or not queue_id.strip():
            raise ValueError("queue_id 必须是非空字符串。")
        key = queue_id.strip()
        with self._lock:
            now = self._now()
            reserved = set(self._by_token) | set(self._by_browser)
            existing = self._by_queue.get(key)
            if existing is not None and existing["expires_at"] > now:
                token = existing["token"]
                expires_at = existing["expires_at"]
            else:
                token = self._new_token(reserved)
                expires_at = now + self._ttl_seconds
                if not _finite_number(expires_at):
                    raise ValueError("恢复凭证期限无效。")
            reserved.add(token)
            if not _valid_token(browser_token) or browser_token not in self._by_browser:
                browser_token = self._new_token(reserved)
            record = {
                "queue_id": key, "token": token,
                "browser_token": browser_token, "expires_at": expires_at,
            }
            records = [*self._records, record]
            _write_document(self.path, {"version": 1, "records": records})
            self._records = records
            self._index(record)
            return dict(record)

    def get(self, token: Any) -> dict[str, Any] | None:
        if not _valid_token(token):
            return None
        with self._lock:
            record = self._by_token.get(token)
            if record is None or record["expires_at"] <= self._now():
                return None
            return dict(record)

    def current(self, browser_token: Any) -> dict[str, Any] | None:
        if not _valid_token(browser_token):
            return None
        with self._lock:
            record = self._by_browser.get(browser_token)
            if record is None or record["expires_at"] <= self._now():
                return None
            return dict(record)
