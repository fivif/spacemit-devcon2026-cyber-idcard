# -*- coding: utf-8 -*-
"""ConsentService：用途隔离的拍摄/外置同意签发、持久化、撤回与预约绑定（T-12）。

规格依据：
- `docs/spec/自动流程规格-v1.md`：Interfaces（``validate`` / ``record`` 签名）、
  Data models（``ConsentRecord``）、Security（外置同意独立、同意绑定）、
  Restart and recovery；
- `docs/spec/仓库契约-v1.md` §4「同意令牌链」：现有
  ``ConsentBook.grant(kind)`` 签发 ``uuid.uuid4().hex``（32 位十六进制）令牌、
  内存字典、TTL ``BOOTH_CONSENT_TTL`` 默认 600s。本模块延续同一令牌格式与默认
  TTL（环境变量同名，见 ``default_ttl_seconds``），但改为可持久化、可绑定。

设计要点：
- 用途严格隔离：``capture`` / ``external_processing`` 是两套记录，``validate``
  逐字比对 purpose；一个用途的同意不能当另一个用；
- 主体与绑定：每条同意带一个可选主体 ``subject_id``（留空 = 匿名同意）。
  capture 同意在首次有效入队时用 ``bind(consent_id, queue_id)`` 原子绑定到唯一
  预约；external_processing 同意用 ``bind_job(consent_id, job_id)`` 绑定作业。
  绑定后改绑其它主体一律拒绝（重复绑定同一主体 = 幂等重放，允许）；
- ``validate`` 语义（只返回 bool，不抛业务错误）：
    * 未知 / 已拒绝（granted=False）/ 已撤回 / 已过期 / 用途不符 -> False；
    * 已绑定：``subject_id`` 必须等于绑定的 queue_id（capture）或 job_id（外置）；
    * 未绑定：只接受 ``subject_id`` 留空（直接拍摄 / 旧令牌桥接场景）；带主体
      的校验一律 False，必须先绑定；
- 持久化：``<BOOTH_DATA>/auto_flow/consent.json``，同目录临时文件 + fsync +
  ``os.replace`` 原子写；只新增这一个文件，绝不覆盖 records.jsonl、counter.json、
  settings.json 等既有文件（与 ``queue_store.py`` 同一套存储契约）；
- 重启可恢复：新进程读同一文件即可 validate 通过（T-12 验收）；已撤回/已绑定
  状态同样保留；
- 重试语义：绑定是幂等的——同一 consent 重复绑定同一 queue 直接返回原记录；
  绑到另一个 queue 抛 ``ConsentBindingError(reason="bound_elsewhere")``；
- 线程安全：进程内 RLock 保护全部状态；「校验 + 绑定」在一次持锁内完成，
  并发下同一同意的首次绑定只有一个赢家；跨进程并发不在本契约内（systemd 单实例）。

隐私口径：只存同意元数据（谁在何时同意了什么用途、绑定到哪个预约）；不存人脸、
不做人脸识别；``subject_id`` 只作为不透明主体键使用，公共投影（stats）不返回。
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
import uuid
from collections.abc import Callable, Mapping
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from app.auto_flow.models import CONSENT_PURPOSES, ConsentRecord, parse_utc_iso, to_utc_iso

__all__ = [
    "DEFAULT_TTL_SECONDS",
    "LEGACY_TTL_ENV",
    "default_ttl_seconds",
    "ConsentError",
    "ConsentBindingError",
    "ConsentStore",
    "ConsentStoreError",
    "MemoryConsentStore",
    "ConsentService",
]

_UTC = timezone.utc

# 与 booth_server.py:45 的 BOOTH_CONSENT_TTL 同名、同默认值：两套机制共享一把 TTL 开关
LEGACY_TTL_ENV = "BOOTH_CONSENT_TTL"
DEFAULT_TTL_SECONDS = 600


def default_ttl_seconds(env: Mapping[str, str] | None = None) -> int:
    """同意 TTL 默认值：优先 ``BOOTH_CONSENT_TTL``（旧 ConsentBook 同一开关），否则 600s。

    非法值（非整数、<=0）直接报 ValueError——失败方向与 config.py 一致：启动期
    报错，不静默回退。
    """
    source: Mapping[str, str] = os.environ if env is None else env
    raw = source.get(LEGACY_TTL_ENV)
    if raw is None or not str(raw).strip():
        return DEFAULT_TTL_SECONDS
    try:
        value = int(str(raw).strip())
    except ValueError as exc:
        raise ValueError(f"{LEGACY_TTL_ENV} 必须是正整数秒，得到 {raw!r}") from exc
    if value < 1:
        raise ValueError(f"{LEGACY_TTL_ENV} 必须大于 0，得到 {raw!r}")
    return value


def _env_ttl_if_set() -> int | None:
    """环境变量设了才返回其值；没设返回 None（用于「显式参数 > 环境 > 文件 > 默认」）。"""
    if LEGACY_TTL_ENV not in os.environ:
        return None
    return default_ttl_seconds()


def _require_purpose(purpose: object) -> str:
    if not isinstance(purpose, str) or purpose not in CONSENT_PURPOSES:
        raise ValueError(
            f"purpose 必须是 {sorted(CONSENT_PURPOSES)} 之一，得到 {purpose!r}"
        )
    return purpose


def _normalize_subject(subject_id: object) -> str:
    if subject_id is None:
        return ""
    if not isinstance(subject_id, str):
        raise ValueError(f"subject_id 必须是字符串或 None，得到 {type(subject_id).__name__}")
    return subject_id.strip()


class ConsentError(RuntimeError):
    """同意服务业务错误基类；``reason`` 为稳定小写码，供处理器映射。"""

    def __init__(self, reason: str, message: str) -> None:
        self.reason = reason
        self.message = message
        super().__init__(f"{reason}: {message}")


class ConsentBindingError(ConsentError):
    """绑定失败：unknown / invalid_request / wrong_purpose / invalid / bound_elsewhere。"""


class ConsentStoreError(RuntimeError):
    """同意状态文件不可用：不可读取、非法 JSON、或版本不支持。"""


class ConsentStore:
    """JSON 文件持久化适配：``<data_root>/auto_flow/consent.json``。

    与 ``queue_store.QueueStore`` 同一套契约：缺失即空文档（不自动创建文件）、
    原子写（mkstemp + fsync + os.replace）、解析失败抛错不自动覆盖、只清理本次
    自己写出的临时文件。
    """

    SCHEMA_VERSION = 1
    SUBDIR = "auto_flow"
    FILENAME = "consent.json"

    def __init__(self, data_root: str | Path) -> None:
        self.root = Path(data_root)
        self.directory = self.root / self.SUBDIR
        self.path = self.directory / self.FILENAME
        self._lock = threading.RLock()

    @classmethod
    def empty_state(cls) -> dict[str, Any]:
        """缺省文档：没有任何同意记录。"""
        return {"version": cls.SCHEMA_VERSION, "updated_at": "", "issued": 0, "records": []}

    @classmethod
    def _validate(cls, data: object, *, source: str) -> dict[str, Any]:
        if not isinstance(data, dict):
            raise ConsentStoreError(f"同意状态必须是 JSON 对象：{source}")
        version = data.get("version")
        if version != cls.SCHEMA_VERSION:
            raise ConsentStoreError(
                f"同意状态文件版本不支持：version={version!r}，本程序支持 {cls.SCHEMA_VERSION}。"
                f"拒绝按未知格式解释，也不会覆盖该文件，请人工处理：{source}"
            )
        records = data.get("records")
        if not isinstance(records, list):
            raise ConsentStoreError(f"同意状态缺少必要字段 records（或类型不对）：{source}")
        return data

    def load(self) -> dict[str, Any]:
        """读取状态文档；文件不存在返回空文档，其余错误抛 ConsentStoreError。"""
        with self._lock:
            if not self.path.is_file():
                return self.empty_state()
            try:
                text = self.path.read_text(encoding="utf-8")
            except OSError as exc:
                raise ConsentStoreError(f"读取同意状态文件失败：{self.path}（{exc}）") from exc
            try:
                data = json.loads(text)
            except ValueError as exc:
                raise ConsentStoreError(
                    f"同意状态文件不是合法 JSON：{self.path}。"
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
                raise ConsentStoreError(f"准备同意状态目录失败：{self.directory}（{exc}）") from exc
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


class MemoryConsentStore(ConsentStore):
    """内存适配：接口与 ConsentStore 相同、不落盘，供单元测试与假适配器使用。"""

    def __init__(self, initial_state: Mapping[str, Any] | None = None) -> None:
        self.root: Path | None = None
        self.directory: Path | None = None
        self.path: Path | None = None
        self._lock = threading.RLock()
        self._state: dict[str, Any] = (
            deepcopy(dict(initial_state)) if initial_state is not None else self.empty_state()
        )
        self._validate(self._state, source="MemoryConsentStore")

    def load(self) -> dict[str, Any]:
        with self._lock:
            return deepcopy(self._state)

    def save(self, state: Mapping[str, Any]) -> None:
        document = self._validate(dict(state), source="MemoryConsentStore")
        with self._lock:
            self._state = deepcopy(document)


@dataclass
class _StoredConsent:
    """内部存储视图：公共记录 + 仅内部使用的元数据（主体、过期、撤回）。"""

    record: ConsentRecord
    subject_id: str = ""
    expires_at: str = ""
    revoked_at: str | None = None


class ConsentService:
    """同意服务：签发 / 校验 / 绑定 / 撤回 / 持久化。

    构造：``ConsentService(store, ttl_seconds=None, clock=None)``
    - store：``ConsentStore`` / ``MemoryConsentStore``（数据根下的 ``auto_flow/consent.json``）；
    - ttl_seconds：显式 TTL（秒）；缺省按 环境 ``BOOTH_CONSENT_TTL`` > 状态文件里的
      ``ttl_seconds`` > ``DEFAULT_TTL_SECONDS``(600) 取值；
    - clock：可注入时钟（可调用对象，或带 ``now()`` 的对象）；缺省 UTC 现在。
    """

    def __init__(
        self,
        store: ConsentStore | MemoryConsentStore,
        *,
        ttl_seconds: int | None = None,
        clock: Callable[[], datetime] | Any | None = None,
    ) -> None:
        for name in ("load", "save"):
            if not callable(getattr(store, name, None)):
                raise TypeError(f"store 必须实现 {name}()（ConsentStore / MemoryConsentStore）。")
        self._store = store
        self._clock = self._resolve_clock(clock)
        self._lock = threading.RLock()
        self._records: dict[str, _StoredConsent] = {}
        self._issued = 0

        document = self._store.load()
        if ttl_seconds is None:
            ttl_seconds = _env_ttl_if_set()
        if ttl_seconds is None:
            stored_ttl = document.get("ttl_seconds")
            if isinstance(stored_ttl, int) and not isinstance(stored_ttl, bool) and stored_ttl > 0:
                ttl_seconds = stored_ttl
        if ttl_seconds is None:
            ttl_seconds = DEFAULT_TTL_SECONDS
        if isinstance(ttl_seconds, bool) or not isinstance(ttl_seconds, int) or ttl_seconds < 1:
            raise ValueError(f"ttl_seconds 必须是正整数秒，得到 {ttl_seconds!r}")
        self.ttl_seconds: int = ttl_seconds
        self._restore(document)

    # ------------------------------------------------------------------ #
    # 规格接口（02-spec Interfaces）
    # ------------------------------------------------------------------ #

    def validate(self, consent_id: str, purpose: str, subject_id: str = "") -> bool:
        """这条同意在该用途、该主体下是否仍然有效（见模块 docstring 的语义表）。"""
        purpose_norm = _require_purpose(purpose)
        subject = _normalize_subject(subject_id)
        if not isinstance(consent_id, str):
            return False
        key = consent_id.strip()
        if not key:
            return False
        with self._lock:
            stored = self._records.get(key)
            if stored is None:
                return False
            if stored.record.purpose != purpose_norm or not stored.record.granted:
                return False
            if stored.revoked_at is not None:
                return False
            if self._is_expired(stored, self._now()):
                return False
            if stored.subject_id:
                return stored.subject_id == subject
            # 未绑定：只接受空主体（直接拍摄 / 旧令牌桥接）；带主体必须先 bind
            return subject == ""

    def record(self, purpose: str, granted: bool, subject_id: str = "") -> ConsentRecord:
        """落一条同意元数据；granted=True 时该 consent_id 即可用于 validate。

        subject_id 非空时同时写入记录的 queue_id（capture）或 job_id（外置处理），
        等价于签发即绑定该主体；留空 = 匿名、待首次入队/绑定。
        """
        purpose_norm = _require_purpose(purpose)
        if not isinstance(granted, bool):
            raise ValueError(f"granted 必须是布尔值，得到 {type(granted).__name__}")
        subject = _normalize_subject(subject_id)
        with self._lock:
            moment = self._now()
            consent_id = uuid.uuid4().hex  # 延续旧 ConsentBook.grant 的 32 位十六进制格式
            item = ConsentRecord(
                consent_id=consent_id,
                purpose=purpose_norm,
                granted=granted,
                created_at=to_utc_iso(moment),
                queue_id=subject if (subject and purpose_norm == "capture") else None,
                job_id=subject if (subject and purpose_norm == "external_processing") else None,
            )
            self._records[consent_id] = _StoredConsent(
                record=item,
                subject_id=subject,
                expires_at=to_utc_iso(moment + timedelta(seconds=self.ttl_seconds)),
                revoked_at=None,
            )
            self._issued += 1
            self._commit_locked()
            return deepcopy(item)

    # ------------------------------------------------------------------ #
    # 管理动作（T-12 任务要求；命名与拒绝语义向集成方汇报）
    # ------------------------------------------------------------------ #

    def bind(self, consent_id: str, queue_id: str) -> ConsentRecord:
        """把 capture 同意原子绑定到唯一预约 queue_id（首次有效入队时调用）。

        - 未绑定 -> 绑定并落盘，返回绑定后的记录；
        - 已绑定同一 queue（含幂等重放）-> 原样返回，不重复写盘；
        - 已绑定其它 queue / 用途不符 / 已撤回 / 已过期 / 不存在 ->
          抛 ``ConsentBindingError``（reason 见类 docstring），调用方不得继续拍。
        """
        return self._bind(consent_id, queue_id, purpose="capture")

    def bind_job(self, consent_id: str, job_id: str) -> ConsentRecord:
        """把 external_processing 同意原子绑定到唯一作业 job_id（T-8 接入用）。"""
        return self._bind(consent_id, job_id, purpose="external_processing")

    def release_binding(self, consent_id: str, queue_id: str) -> bool:
        """补偿回滚：只有该同意正好绑定到该 queue 时才解除，返回是否解除。

        仅用于「提交后续步骤失败、预约条目已取消」的清理；正常流程不要调用。
        """
        if not isinstance(consent_id, str) or not consent_id.strip():
            return False
        if not isinstance(queue_id, str) or not queue_id.strip():
            return False
        key = consent_id.strip()
        target = queue_id.strip()
        with self._lock:
            stored = self._records.get(key)
            if stored is None or stored.record.purpose != "capture":
                return False
            if stored.subject_id != target:
                return False
            stored.subject_id = ""
            stored.record.queue_id = None
            self._commit_locked()
            return True

    def revoke(self, consent_id: str) -> bool:
        """撤回一条同意；撤回后 validate 一律 False。返回是否发生状态变化。"""
        if not isinstance(consent_id, str) or not consent_id.strip():
            return False
        key = consent_id.strip()
        with self._lock:
            stored = self._records.get(key)
            if stored is None or stored.revoked_at is not None:
                return False
            stored.revoked_at = self._now_iso()
            self._commit_locked()
            return True

    def revoke_all(self) -> int:
        """撤回全部尚未撤回的同意（对应旧 ConsentBook.revoke_all / 彩排复位）。"""
        with self._lock:
            moment = self._now_iso()
            count = 0
            for stored in self._records.values():
                if stored.revoked_at is None:
                    stored.revoked_at = moment
                    count += 1
            if count:
                self._commit_locked()
            return count

    def get(self, consent_id: str) -> ConsentRecord | None:
        """按 id 取公共记录（不含主体等内部元数据）；不存在返回 None。"""
        if not isinstance(consent_id, str) or not consent_id.strip():
            return None
        with self._lock:
            stored = self._records.get(consent_id.strip())
            return deepcopy(stored.record) if stored is not None else None

    def binding_of(self, consent_id: str) -> str | None:
        """当前绑定主体（capture -> queue_id；外置 -> job_id）；未绑定返回 None。"""
        if not isinstance(consent_id, str) or not consent_id.strip():
            return None
        with self._lock:
            stored = self._records.get(consent_id.strip())
            if stored is None or not stored.subject_id:
                return None
            return stored.subject_id

    def status(self, consent_id: str) -> str:
        """诊断用状态：missing / denied / revoked / expired / valid（不用于对外文案）。"""
        if not isinstance(consent_id, str) or not consent_id.strip():
            return "missing"
        with self._lock:
            stored = self._records.get(consent_id.strip())
            if stored is None:
                return "missing"
            if not stored.record.granted:
                return "denied"
            if stored.revoked_at is not None:
                return "revoked"
            if self._is_expired(stored, self._now()):
                return "expired"
            return "valid"

    def stats(self) -> dict[str, int]:
        """聚合计数（公共投影安全：不含任何 consent_id / 主体 / 用途内容）。"""
        with self._lock:
            now = self._now()
            summary: dict[str, int] = {
                "issued": self._issued,
                "live": 0,
                "revoked": 0,
                "expired": 0,
                "live_capture": 0,
                "live_external": 0,
                "ttl_s": self.ttl_seconds,
            }
            for stored in self._records.values():
                if stored.revoked_at is not None:
                    summary["revoked"] += 1
                    continue
                if self._is_expired(stored, now):
                    summary["expired"] += 1
                    continue
                if not stored.record.granted:
                    continue
                summary["live"] += 1
                if stored.record.purpose == "capture":
                    summary["live_capture"] += 1
                else:
                    summary["live_external"] += 1
            return summary

    # ------------------------------------------------------------------ #
    # 内部实现
    # ------------------------------------------------------------------ #

    @staticmethod
    def _resolve_clock(clock: Callable[[], datetime] | Any | None) -> Callable[[], datetime]:
        if clock is None:
            return lambda: datetime.now(_UTC)
        if callable(clock):
            return clock
        now_attr = getattr(clock, "now", None)
        if callable(now_attr):
            return now_attr
        raise TypeError("clock 必须是可调用对象，或带 now() 的对象。")

    def _now(self) -> datetime:
        value = self._clock()
        if not isinstance(value, datetime):
            raise TypeError(f"clock 必须返回 datetime，得到 {type(value).__name__}")
        if value.tzinfo is None:
            value = value.replace(tzinfo=_UTC)
        return value.astimezone(_UTC)

    def _now_iso(self) -> str:
        return to_utc_iso(self._now())

    def _is_expired(self, stored: _StoredConsent, moment: datetime) -> bool:
        if not stored.expires_at:
            return False
        try:
            expires = parse_utc_iso(stored.expires_at)
        except ValueError:
            return True  # 坏时间戳按失效处理（保守方向）
        return moment >= expires

    def _bind(self, consent_id: str, target_id: str, *, purpose: str) -> ConsentRecord:
        if not isinstance(consent_id, str) or not consent_id.strip():
            raise ConsentBindingError("invalid_request", "consent_id 必须是非空字符串。")
        if not isinstance(target_id, str) or not target_id.strip():
            raise ConsentBindingError("invalid_request", "绑定目标必须是非空字符串。")
        key = consent_id.strip()
        target = target_id.strip()
        with self._lock:
            stored = self._records.get(key)
            if stored is None:
                raise ConsentBindingError("unknown", "不存在这条同意记录。")
            if stored.record.purpose != purpose:
                raise ConsentBindingError("wrong_purpose", "该同意的用途与本操作不匹配。")
            if not stored.record.granted:
                raise ConsentBindingError("invalid", "该同意已被拒绝。")
            if stored.revoked_at is not None:
                raise ConsentBindingError("invalid", "该同意已被撤回。")
            if self._is_expired(stored, self._now()):
                raise ConsentBindingError("invalid", "该同意已过期。")
            if stored.subject_id:
                if stored.subject_id == target:
                    return deepcopy(stored.record)  # 幂等重放：同一主体允许
                raise ConsentBindingError(
                    "bound_elsewhere", "该同意已绑定到其它预约/作业，不能复用。"
                )
            # 首次绑定：持锁内改状态 + 原子落盘，保证并发下只有一个赢家
            stored.subject_id = target
            if purpose == "capture":
                stored.record.queue_id = target
            else:
                stored.record.job_id = target
            self._commit_locked()
            return deepcopy(stored.record)

    def _record_doc(self, stored: _StoredConsent) -> dict[str, Any]:
        document = stored.record.to_dict()
        # 内部元数据与公共字段同文档保存；subject_id 不出现在 ConsentRecord 投影里
        document["subject_id"] = stored.subject_id
        document["expires_at"] = stored.expires_at
        document["revoked_at"] = stored.revoked_at
        return document

    def _commit_locked(self) -> None:
        self._store.save(
            {
                "version": ConsentStore.SCHEMA_VERSION,
                "updated_at": self._now_iso(),
                "ttl_seconds": self.ttl_seconds,
                "issued": self._issued,
                "records": [self._record_doc(item) for item in self._records.values()],
            }
        )

    def _restore(self, document: Mapping[str, Any]) -> None:
        raw_records = document.get("records") or []
        source = str(getattr(self._store, "path", "ConsentStore"))
        for raw in raw_records:
            try:
                record = ConsentRecord.from_dict(raw)
            except ValueError as exc:
                raise ConsentStoreError(
                    f"同意状态文件中有无法解析的记录：{exc}。"
                    f"服务不会自动覆盖或删除该文件，请人工处理：{source}"
                ) from exc
            if record.consent_id in self._records:
                raise ConsentStoreError(
                    f"同意状态文件存在重复 consent_id：{record.consent_id}。"
                    f"服务不会自动覆盖或删除该文件，请人工处理：{source}"
                )
            subject = raw.get("subject_id")
            subject = subject.strip() if isinstance(subject, str) else ""
            expires_at = raw.get("expires_at")
            if not isinstance(expires_at, str) or not expires_at:
                try:
                    expires_at = to_utc_iso(
                        parse_utc_iso(record.created_at)
                        + timedelta(seconds=self.ttl_seconds)
                    )
                except ValueError:
                    expires_at = ""
            revoked_at = raw.get("revoked_at")
            if not isinstance(revoked_at, str) or not revoked_at:
                revoked_at = None
            self._records[record.consent_id] = _StoredConsent(
                record=record,
                subject_id=subject,
                expires_at=expires_at,
                revoked_at=revoked_at,
            )
        issued = document.get("issued")
        if isinstance(issued, int) and not isinstance(issued, bool) and issued >= 0:
            self._issued = issued
