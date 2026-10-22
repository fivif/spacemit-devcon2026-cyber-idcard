# -*- coding: utf-8 -*-
"""QueueService：排队状态机、幂等提交与拍摄位独占（T-1）。

规格依据：`自动流程规格 v1（内部文档）` 的 Components（QueueService 职责）、
Interfaces（类签名逐字）、Data models、Error handling、Restart and recovery。

核心保证（T-1 验收）：
- FIFO：call_next 取最早 queued；顺序 = 持久化文档中的创建顺序，重启后保持；
- 独占：called/ready/capturing 任一存在时不叫号；占用记录与队列变更、幂等索引
  在同一次原子提交里落盘；
- 幂等：request_id + 规范化载荷指纹（models.payload_fingerprint）。同键同载荷返回
  原条目（不做任何修改），同键异载荷抛 IDEMPOTENCY_CONFLICT；规范化把 optional_fields
  的 None 与 {} 视为等价、键序无关（见 models.canonical_payload）；
- 容量：默认不设人数上限；显式配置 queue.max_active 时才执行容量拒绝，
  拒绝时不产生任何状态变化；
- 线程安全：所有读写都在 threading.RLock 内；每次变更整份文档原子持久化
  （队列有界、文档很小，换取"队列+幂等+占用"一次提交的一致性是值得的）。

重启恢复口径（T-1 自主决策，逐条理由）：
- ready/capturing/processing -> failed（写诊断 note）：
  * ready：开始动作已发生，但同意令牌只存在内存里、重启即失效（规格 Restart and
    recovery），该会话无法继续；留着会永久占住唯一拍摄位（死锁），转 failed 释放；
  * capturing：拍摄是否完成无法在 T-1 层确认，规格要求"不重拍、转可诊断失败"；
  * processing：处理结果未确认，不自动重试（避免重复计费），转 failed 由人工核对；
- called 保持原状：叫号顺序与期限保留，由 T-13 调度器执行期限/故障策略（含冻结），
  T-1 不替它罚弃号；
- queued 与终态（expired/cancelled/failed/ready_for_pickup）原样保留；
- 恢复后若发现多于一条拍摄位占用（文件被手工编辑或异常），保留创建顺序最早的一条，
  其余转 failed —— 保证"任何时刻最多一个拍摄位占用"；
- 全流程不调用任何拍摄逻辑，不存在自动重拍路径。

下游任务接入点（T-2/T-6/T-8/T-13/T-15）：
- submit_result()：区分新提交（HTTP 201）与幂等重放（HTTP 200）；
- start(queue_id, capture_consent_id=None)：规格签名 start(queue_id) 可直接调用，
  此时绑定提交时记录的拍摄同意；T-2 可传路由 body 里的 capture_consent_id 显式绑定；
- mark_capturing / mark_processing / mark_ready_for_pickup / mark_failed /
  mark_cancelled：条目状态机推进（含拍摄位释放），同样原子提交；
- snapshot()：状态计数 + 只读条目视图 + 占用记录（含完整 queue_id，仅限内部使用；
  公共投影必须走 T-20 的脱敏层）。
"""

from __future__ import annotations

import secrets
import threading
from collections.abc import Iterable, Mapping
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from app.auto_flow import errors
from app.auto_flow.config import QueueConfig
from app.auto_flow.models import (
    QueueEntry,
    QueueStatus,
    QueueSubmission,
    PORTRAIT_MODES,
    SLOT_STATUSES,
    canonical_payload,
    legacy_payload_fingerprint,
    occupies_capture_slot,
    parse_utc_iso,
    payload_fingerprint,
    to_utc_iso,
)
from app.auto_flow.queue_store import QueueStore

__all__ = [
    "QueueService",
    "SubmitResult",
    "NormalizedSubmission",
    "InvalidTransitionError",
    "normalize_submission",
]

_UTC = timezone.utc
_MAX_NOTES = 200
_DISPLAY_NUMBER_PREFIX = "SM"

# 状态机白名单（终态没有出边）。到号/过期/开始这些"规格接口路径"也走同一张表，
# 保证任何入口都不能把条目推进到非法状态。
_ALLOWED_TRANSITIONS: dict[QueueStatus, frozenset[QueueStatus]] = {
    QueueStatus.QUEUED: frozenset(
        {QueueStatus.CALLED, QueueStatus.EXPIRED, QueueStatus.CANCELLED, QueueStatus.FAILED}
    ),
    QueueStatus.CALLED: frozenset(
        {QueueStatus.READY, QueueStatus.EXPIRED, QueueStatus.CANCELLED, QueueStatus.FAILED}
    ),
    QueueStatus.READY: frozenset(
        {QueueStatus.CAPTURING, QueueStatus.PROCESSING, QueueStatus.CANCELLED, QueueStatus.FAILED}
    ),
    QueueStatus.CAPTURING: frozenset(
        {QueueStatus.PROCESSING, QueueStatus.CANCELLED, QueueStatus.FAILED}
    ),
    QueueStatus.PROCESSING: frozenset(
        {QueueStatus.READY_FOR_PICKUP, QueueStatus.CANCELLED, QueueStatus.FAILED}
    ),
    QueueStatus.READY_FOR_PICKUP: frozenset(),
    QueueStatus.EXPIRED: frozenset(),
    QueueStatus.CANCELLED: frozenset(),
    QueueStatus.FAILED: frozenset(),
}

# 重启恢复映射：状态 -> (目标状态, 诊断理由)
_RESTART_RECOVERY: dict[QueueStatus, tuple[QueueStatus, str]] = {
    QueueStatus.READY: (
        QueueStatus.FAILED,
        "重启后拍摄会话失效（同意令牌不持久化），按契约不自动重拍；请重新排队并重新同意。",
    ),
    QueueStatus.CAPTURING: (
        QueueStatus.FAILED,
        "重启时拍摄进行中，无法确认是否已出片；按契约不自动重拍、不重复占位，请人工核对。",
    ),
    QueueStatus.PROCESSING: (
        QueueStatus.FAILED,
        "重启时处理进行中，结果未确认；不自动重试以避免重复计费，请人工核对。",
    ),
}


@dataclass(frozen=True)
class NormalizedSubmission:
    """规范化后的提交内容：幂等比较的唯一口径。"""

    request_id: str
    style_id: str
    optional_fields: dict[str, str]
    capture_consent_id: str
    canonical: str
    fingerprint: str
    portrait_mode: str
    ai_watermark: bool


def normalize_submission(
    request: QueueSubmission | Mapping[str, Any],
    *,
    optional_fields_allowlist: Iterable[str] = (),
) -> NormalizedSubmission:
    """校验并规范化提交；不合法抛 ValueError（QueueService 换算成 INVALID_REQUEST）。

    规则：request_id / style_id / capture_consent_id 非空；optional_fields 只允许
    allowlist 内的键（未配置字段不得收集）、值必须是字符串；缺省 optional_fields
    与 {} 等价；指纹覆盖 style_id + portrait_mode + optional_fields。
    """
    if isinstance(request, QueueSubmission):
        submission = request
    elif isinstance(request, Mapping):
        submission = QueueSubmission.from_dict(request)
    else:
        raise ValueError(f"submit 需要 QueueSubmission，得到 {type(request).__name__}")

    allowed = {str(key) for key in optional_fields_allowlist}
    optional_fields = dict(submission.optional_fields or {})
    for key, value in optional_fields.items():
        if not isinstance(key, str):
            raise ValueError(f"可选字段名必须是字符串，得到 {key!r}。")
        if key not in allowed:
            raise ValueError(f"可选字段 {key!r} 不在允许清单内（未配置字段不得收集）。")
        if not isinstance(value, str):
            raise ValueError(f"可选字段 {key!r} 的值必须是字符串，得到 {value!r}。")

    request_id = submission.request_id.strip()
    if not request_id:
        raise ValueError("request_id 不能为空。")
    style_id = submission.style_id.strip()
    if not style_id:
        raise ValueError("style_id 不能为空。")
    consent_id = submission.capture_consent_id.strip()
    if not consent_id:
        raise ValueError("capture_consent_id 不能为空。")
    portrait_mode = submission.portrait_mode
    if not isinstance(portrait_mode, str) or portrait_mode not in PORTRAIT_MODES:
        raise ValueError("portrait_mode 必须是 standard 或 chibi。")
    if not isinstance(submission.ai_watermark, bool):
        raise ValueError("ai_watermark 必须是布尔值。")

    return NormalizedSubmission(
        request_id=request_id,
        style_id=style_id,
        optional_fields=optional_fields,
        capture_consent_id=consent_id,
        portrait_mode=portrait_mode,
        ai_watermark=submission.ai_watermark,
        canonical=canonical_payload(style_id=style_id, optional_fields=optional_fields,
                                    portrait_mode=portrait_mode, ai_watermark=submission.ai_watermark),
        fingerprint=payload_fingerprint(style_id=style_id, optional_fields=optional_fields,
                                        portrait_mode=portrait_mode, ai_watermark=submission.ai_watermark),
    )


@dataclass(frozen=True)
class SubmitResult:
    """submit_result 的返回值：entry 之外再带 created，供 T-2 选 201/200。"""

    entry: QueueEntry
    created: bool


class InvalidTransitionError(ValueError):
    """条目状态机不允许的转换（内部误用；规格接口路径不会走到这里）。"""

    def __init__(self, *, queue_id: str, from_status: QueueStatus, to_status: QueueStatus) -> None:
        self.queue_id = queue_id
        self.from_status = from_status
        self.to_status = to_status
        super().__init__(
            f"队列条目 {queue_id} 不允许从 {from_status.value} 转换到 {to_status.value}"
        )


def _normalize_moment(value: datetime) -> datetime:
    """外部传入的 datetime -> 带时区 UTC；naive 按 UTC 解释。"""
    if not isinstance(value, datetime):
        raise ValueError(f"需要 datetime，得到 {type(value).__name__}")
    if value.tzinfo is None:
        value = value.replace(tzinfo=_UTC)
    return value.astimezone(_UTC)


def _display_sequence(display_number: object) -> int:
    """从 SM-YYYY-NNNN 提取序号；不匹配返回 0（用于恢复计数器）。"""
    if not isinstance(display_number, str):
        return 0
    parts = display_number.split("-")
    if len(parts) != 3 or parts[0] != _DISPLAY_NUMBER_PREFIX:
        return 0
    try:
        return int(parts[2])
    except ValueError:
        return 0


class QueueService:
    """排队服务（规格 Interfaces 的 QueueService，签名逐字 + 明确标注的加法）。

    构造参数：
    - store：QueueStore（或其内存版）持久化适配；
    - queue_config：config.load_config() 的 cfg.queue；也可用命名参数逐项覆盖；
    - clock：返回 datetime 的可调用对象，或带 .now() 的对象（如 T-14 ClockPort）；
      缺省用系统 UTC 时钟。测试注入 FakeClock。
    - event_year：展示编号的年份；缺省用时钟当前年份（测试可注入）。
    """

    def __init__(
        self,
        store: QueueStore,
        queue_config: QueueConfig | None = None,
        *,
        max_active: int | None = None,
        start_timeout_seconds: int | None = None,
        idempotency_ttl_seconds: int | None = None,
        optional_fields_allowlist: Iterable[str] | None = None,
        clock: Callable[[], datetime] | Any | None = None,
        event_year: int | None = None,
    ) -> None:
        config = queue_config
        configured_max = max_active if max_active is not None else (config.max_active if config else None)
        self._max_active = (
            self._positive_int("max_active", configured_max)
            if configured_max is not None else None
        )
        self._start_timeout_seconds = self._positive_int(
            "start_timeout_seconds",
            start_timeout_seconds
            if start_timeout_seconds is not None
            else (config.start_timeout_seconds if config else 120),
        )
        self._idempotency_ttl_seconds = self._positive_int(
            "idempotency_ttl_seconds",
            idempotency_ttl_seconds
            if idempotency_ttl_seconds is not None
            else (config.idempotency_ttl_seconds if config else 86400),
        )
        if optional_fields_allowlist is not None:
            allowlist = tuple(optional_fields_allowlist)
        elif config is not None:
            allowlist = tuple(config.optional_fields_allowlist)
        else:
            allowlist = ()
        self._allowlist = tuple(str(key) for key in allowlist)
        self._event_year = event_year
        self._clock = clock
        self._store = store
        self._lock = threading.RLock()

        with self._lock:
            self._entries: dict[str, QueueEntry] = {}
            self._index: dict[str, dict[str, Any]] = {}
            self._occupancy: dict[str, Any] | None = None
            self._counter = 0
            self._notes: list[dict[str, Any]] = []
            restored = self._restore(self._store.load())
            if restored:
                # 恢复本身（状态降级/占用重建/索引补写）立即原子落盘，
                # 保证下一次重启看到的是恢复后的状态，不重复诊断。
                self._commit()

    # ------------------------------------------------------------------ #
    # 规格接口
    # ------------------------------------------------------------------ #

    def submit(self, request: QueueSubmission) -> QueueEntry:
        """幂等入队（规格签名）。返回条目；重放时返回原条目。"""
        return self.submit_result(request).entry

    def submit_result(self, request: QueueSubmission | Mapping[str, Any]) -> SubmitResult:
        """同 submit，但附带 created 标记（T-2 用：True -> 201，False -> 200）。"""
        try:
            normalized = normalize_submission(
                request, optional_fields_allowlist=self._allowlist
            )
        except ValueError as exc:
            raise errors.ApiError(errors.INVALID_REQUEST, str(exc)) from exc

        with self._lock:
            now = self._now()
            stale_index = False
            existing = self._index.get(normalized.request_id)
            if existing is not None:
                entry = self._entries.get(existing.get("queue_id") or "")
                if entry is not None and not self._index_expired(existing, now):
                    fingerprints = {normalized.fingerprint, None}
                    if normalized.portrait_mode == "standard" and normalized.ai_watermark:
                        fingerprints.add(legacy_payload_fingerprint(
                            style_id=normalized.style_id, optional_fields=normalized.optional_fields
                        ))
                    if (entry.portrait_mode == normalized.portrait_mode
                            and entry.ai_watermark == normalized.ai_watermark
                            and existing.get("fingerprint") in fingerprints):
                        # 同键同载荷（或旧数据指纹未知）：返回原条目，不产生第二条记录
                        return SubmitResult(entry=deepcopy(entry), created=False)
                    raise errors.ApiError(errors.IDEMPOTENCY_CONFLICT)
                # 索引指向不存在的条目 / 超过保留期且原条目已终态：按新提交处理
                stale_index = True

            active = sum(1 for item in self._entries.values() if item.status.is_active)
            if self._max_active is not None and active >= self._max_active:
                raise errors.ApiError(errors.QUEUE_FULL)

            if stale_index:
                self._index.pop(normalized.request_id, None)
                self._note(
                    kind="idempotency_stale",
                    queue_id="",
                    reason=f"请求 {normalized.request_id!r} 的旧幂等记录不可用，已按新提交处理。",
                )

            self._counter += 1
            created_at = to_utc_iso(now)
            entry = QueueEntry(
                queue_id=secrets.token_urlsafe(16),
                request_id=normalized.request_id,
                status=QueueStatus.QUEUED,
                style_id=normalized.style_id,
                portrait_mode=normalized.portrait_mode,
                ai_watermark=normalized.ai_watermark,
                created_at=created_at,
                updated_at=created_at,
                display_number=f"{_DISPLAY_NUMBER_PREFIX}-{self._display_year()}-{self._counter:04d}",
                name=str(normalized.optional_fields.get("display_name") or "").strip()[:24],
                handle=str(normalized.optional_fields.get("developer_id") or "").strip()[:32],
            )
            self._entries[entry.queue_id] = entry
            self._index[normalized.request_id] = {
                "queue_id": entry.queue_id,
                "fingerprint": normalized.fingerprint,
                "consent_id": normalized.capture_consent_id,
                "created_at": created_at,
            }
            self._commit()
            return SubmitResult(entry=deepcopy(entry), created=True)

    def get(self, queue_id: str) -> QueueEntry | None:
        """按 queue_id 查条目；不存在返回 None（未知 id 不抛错）。"""
        with self._lock:
            entry = self._entries.get(queue_id) if isinstance(queue_id, str) else None
            return deepcopy(entry) if entry is not None else None

    def submission_consent_id(self, queue_id: str) -> str | None:
        with self._lock:
            entry = self._entries.get(queue_id)
            if entry is None:
                return None
            consent_id = entry.consent_id or self._index.get(entry.request_id, {}).get("consent_id")
            return consent_id if isinstance(consent_id, str) and consent_id else None

    def call_next(self) -> QueueEntry | None:
        """FIFO 叫号：最早 queued -> called，写 called_at 与 start_deadline。

        独占：已有 called/ready/capturing 条目（或占用记录）时返回 None。
        """
        with self._lock:
            if self._occupancy is not None or any(
                occupies_capture_slot(item.status) for item in self._entries.values()
            ):
                return None
            now = self._now()
            for entry in self._entries.values():
                if entry.status == QueueStatus.QUEUED:
                    called_at = to_utc_iso(now)
                    deadline = parse_utc_iso(called_at) + timedelta(
                        seconds=self._start_timeout_seconds
                    )
                    entry.status = QueueStatus.CALLED
                    entry.called_at = called_at
                    entry.start_deadline = to_utc_iso(deadline)
                    entry.updated_at = called_at
                    self._occupancy = {"queue_id": entry.queue_id, "acquired_at": called_at}
                    self._commit()
                    return deepcopy(entry)
            return None

    def start(self, queue_id: str, capture_consent_id: str | None = None) -> QueueEntry:
        """开始拍摄：必须 called 且在期限内，否则 NOT_CALLED / QUEUE_EXPIRED。

        成功 -> ready，写 started_at 并绑定 consent_id：
        - 显式传入 capture_consent_id（T-2 路由 body）时绑定它；
        - 规格签名 start(queue_id) 直接调用时，绑定提交时记录的拍摄同意。
        期限已过时先把条目标记 expired（规格 Policy: Mark expired），再抛 QUEUE_EXPIRED。
        """
        if capture_consent_id is not None:
            if not isinstance(capture_consent_id, str) or not capture_consent_id.strip():
                raise errors.ApiError(
                    errors.INVALID_REQUEST, "capture_consent_id 必须是非空字符串。"
                )
            capture_consent_id = capture_consent_id.strip()

        with self._lock:
            entry = self._entries.get(queue_id) if isinstance(queue_id, str) else None
            if entry is None:
                raise errors.ApiError(errors.QUEUE_NOT_FOUND)
            if entry.status == QueueStatus.EXPIRED:
                raise errors.ApiError(errors.QUEUE_EXPIRED)
            if entry.status != QueueStatus.CALLED:
                raise errors.ApiError(errors.NOT_CALLED)

            now = self._now()
            if self._deadline_passed(entry, now):
                self._expire_entry(entry, to_utc_iso(now))
                self._commit()
                raise errors.ApiError(errors.QUEUE_EXPIRED)

            consent_id = capture_consent_id or (
                self._index.get(entry.request_id) or {}
            ).get("consent_id")
            entry.status = QueueStatus.READY
            entry.started_at = to_utc_iso(now)
            entry.updated_at = entry.started_at
            if isinstance(consent_id, str) and consent_id:
                entry.consent_id = consent_id
            if self._occupancy is None:
                # 自愈：called->ready 本应已有占用记录；缺失时补上，保证不变量
                self._occupancy = {
                    "queue_id": entry.queue_id,
                    "acquired_at": entry.called_at or entry.started_at,
                }
            self._commit()
            return deepcopy(entry)

    def expire_due(self, now: datetime) -> list[QueueEntry]:
        """把所有超期 called 条目转为 expired，返回被弃号的条目（副本）。"""
        moment = _normalize_moment(now)
        moment_iso = to_utc_iso(moment)
        with self._lock:
            expired: list[QueueEntry] = []
            for entry in self._entries.values():
                if entry.status == QueueStatus.CALLED and self._deadline_passed(entry, moment):
                    self._expire_entry(entry, moment_iso)
                    expired.append(deepcopy(entry))
            if expired:
                self._commit()
            return expired

    def snapshot(self) -> dict[str, Any]:
        """只读快照：全部状态计数 + 容量 + 占用记录 + 条目视图 + 最近诊断。

        **含完整 queue_id 与请求标识，只准内部/授权侧使用**；公共 /api/state、
        /api/overlay.json 必须由 T-20 的脱敏投影生成，不得直接透传本结构。
        """
        with self._lock:
            counts = {status.value: 0 for status in QueueStatus}
            for entry in self._entries.values():
                counts[entry.status.value] += 1
            active = sum(1 for entry in self._entries.values() if entry.status.is_active)
            occupancy = None
            if self._occupancy is not None:
                occupant = self._entries.get(self._occupancy.get("queue_id") or "")
                occupancy = {
                    "queue_id": self._occupancy.get("queue_id"),
                    "acquired_at": self._occupancy.get("acquired_at"),
                    "status": occupant.status.value if occupant else None,
                    "called_at": occupant.called_at if occupant else None,
                    "start_deadline": occupant.start_deadline if occupant else None,
                    "display_number": occupant.display_number if occupant else None,
                }
            return {
                "counts": counts,
                "total": len(self._entries),
                "active": active,
                "capacity": {"max_active": self._max_active, "active": active},
                "occupancy": occupancy,
                "entries": [entry.to_dict() for entry in self._entries.values()],
                "notes": [dict(note) for note in self._notes[-20:]],
            }

    # ------------------------------------------------------------------ #
    # 状态机推进（T-6/T-8/T-15 接入点；非法转换抛 InvalidTransitionError）
    # ------------------------------------------------------------------ #

    def mark_capturing(self, queue_id: str) -> QueueEntry:
        """ready -> capturing（仍在拍摄位内）。"""
        return self._mark(queue_id, QueueStatus.CAPTURING)

    def mark_processing(self, queue_id: str) -> QueueEntry:
        """ready/capturing -> processing 并释放拍摄位（AIGC 与打印不占拍摄位）。"""
        return self._mark(queue_id, QueueStatus.PROCESSING)

    def mark_ready_for_pickup(self, queue_id: str) -> QueueEntry:
        """processing -> ready_for_pickup。"""
        return self._mark(queue_id, QueueStatus.READY_FOR_PICKUP)

    def mark_failed(self, queue_id: str, *, reason: str = "") -> QueueEntry:
        """任意非终态 -> failed，释放拍摄位；reason 进诊断 notes。"""
        return self._mark(queue_id, QueueStatus.FAILED, reason=reason)

    def mark_cancelled(self, queue_id: str, *, reason: str = "") -> QueueEntry:
        """任意非终态（ready_for_pickup 除外）-> cancelled，释放拍摄位。"""
        return self._mark(queue_id, QueueStatus.CANCELLED, reason=reason)

    # ------------------------------------------------------------------ #
    # 内部
    # ------------------------------------------------------------------ #

    @property
    def store(self) -> QueueStore:
        """持久化适配（只读引用，供测试与诊断确认落盘位置）。"""
        return self._store

    @staticmethod
    def _positive_int(name: str, value: int) -> int:
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise ValueError(f"{name} 必须是 >=1 的整数，得到 {value!r}")
        return value

    def _now(self) -> datetime:
        clock = self._clock
        if clock is None:
            return datetime.now(_UTC)
        if hasattr(clock, "now") and callable(getattr(clock, "now")):
            raw = clock.now()
        elif callable(clock):
            raw = clock()
        else:
            raise TypeError("clock 必须是可调用对象或带 .now() 的对象")
        return _normalize_moment(raw)

    def _display_year(self) -> int:
        if self._event_year is not None:
            return self._event_year
        return self._now().year

    def _deadline_passed(self, entry: QueueEntry, moment: datetime) -> bool:
        """期限判定：now >= start_deadline 视为已到/已过（同一口径用于 expire_due 与 start）。"""
        if not entry.start_deadline:
            return False
        try:
            deadline = parse_utc_iso(entry.start_deadline)
        except ValueError:
            return False
        return moment >= deadline

    def _index_expired(self, record: Mapping[str, Any], moment: datetime) -> bool:
        """幂等记录是否超过保留期；仅当引用条目缺失或已终态时才允许回收。"""
        created = record.get("created_at")
        if not isinstance(created, str):
            return False
        try:
            created_at = parse_utc_iso(created)
        except ValueError:
            return False
        if moment < created_at + timedelta(seconds=self._idempotency_ttl_seconds):
            return False
        entry = self._entries.get(record.get("queue_id") or "")
        return entry is None or entry.status.is_terminal

    def _expire_entry(self, entry: QueueEntry, moment_iso: str) -> None:
        entry.status = QueueStatus.EXPIRED
        entry.expired_at = moment_iso
        entry.updated_at = moment_iso
        self._release_slot(entry)

    def _release_slot(self, entry: QueueEntry) -> None:
        if self._occupancy is not None and self._occupancy.get("queue_id") == entry.queue_id:
            self._occupancy = None

    def _mark(self, queue_id: str, target: QueueStatus, *, reason: str = "") -> QueueEntry:
        with self._lock:
            entry = self._entries.get(queue_id) if isinstance(queue_id, str) else None
            if entry is None:
                raise errors.ApiError(errors.QUEUE_NOT_FOUND)
            if target not in _ALLOWED_TRANSITIONS[entry.status]:
                raise InvalidTransitionError(
                    queue_id=entry.queue_id, from_status=entry.status, to_status=target
                )
            now_iso = to_utc_iso(self._now())
            from_status = entry.status
            entry.status = target
            entry.updated_at = now_iso
            if target in SLOT_STATUSES:
                if self._occupancy is None:
                    self._occupancy = {
                        "queue_id": entry.queue_id,
                        "acquired_at": entry.started_at or entry.called_at or now_iso,
                    }
            else:
                self._release_slot(entry)
            if reason:
                self._note(
                    kind="transition",
                    queue_id=entry.queue_id,
                    from_status=from_status.value,
                    to_status=target.value,
                    reason=reason,
                )
            self._commit()
            return deepcopy(entry)

    def _note(
        self,
        *,
        kind: str,
        reason: str,
        queue_id: str = "",
        from_status: str = "",
        to_status: str = "",
    ) -> None:
        self._notes.append(
            {
                "at": to_utc_iso(self._now()),
                "kind": kind,
                "queue_id": queue_id,
                "from_status": from_status,
                "to_status": to_status,
                "reason": reason,
            }
        )
        del self._notes[:-_MAX_NOTES]

    # ---- 重启恢复 ------------------------------------------------------ #

    def _restore(self, document: Mapping[str, Any]) -> bool:
        """把持久化文档装回内存并做恢复；有任何修正返回 True（调用方立即落盘）。"""
        changed = False

        # 1) 条目：逐条解析；无法解析的条目跳过并留诊断（原文件保留，不删数据）
        raw_entries = document.get("entries")
        if not isinstance(raw_entries, list):
            raw_entries = []
        for position, item in enumerate(raw_entries):
            try:
                entry = QueueEntry.from_dict(item)
            except ValueError as exc:
                self._note(
                    kind="entry_parse_error",
                    reason=f"第 {position} 条队列条目无法解析，已跳过（原文件保留）：{exc}",
                )
                changed = True
                continue
            if entry.queue_id in self._entries:
                self._note(
                    kind="duplicate_entry",
                    queue_id=entry.queue_id,
                    reason="恢复时发现重复 queue_id，保留第一条，跳过后续重复条目。",
                )
                changed = True
                continue
            self._entries[entry.queue_id] = entry

        # 2) 重启恢复：ready/capturing/processing -> failed（不自动重拍）
        recovery_at = to_utc_iso(self._now())
        for entry in self._entries.values():
            recovery = _RESTART_RECOVERY.get(entry.status)
            if recovery is None:
                continue
            target, reason = recovery
            self._note(
                kind="restart_recovery",
                queue_id=entry.queue_id,
                from_status=entry.status.value,
                to_status=target.value,
                reason=reason,
            )
            entry.status = target
            entry.updated_at = recovery_at
            changed = True

        # 3) 拍摄位：至多保留创建顺序最早的一条占用（其余转 failed）
        occupants = [item for item in self._entries.values() if occupies_capture_slot(item.status)]
        if occupants:
            keep = occupants[0]
            for extra in occupants[1:]:
                self._note(
                    kind="slot_conflict",
                    queue_id=extra.queue_id,
                    from_status=extra.status.value,
                    to_status=QueueStatus.FAILED.value,
                    reason=(
                        f"恢复时发现多个拍摄位占用，保留较早的 {keep.queue_id}，"
                        "本条转 failed，不自动重拍。"
                    ),
                )
                extra.status = QueueStatus.FAILED
                extra.updated_at = recovery_at
                changed = True
            self._occupancy = {
                "queue_id": keep.queue_id,
                "acquired_at": keep.called_at or keep.started_at or keep.created_at,
            }
        else:
            self._occupancy = None
        stored_occupancy = document.get("occupancy")
        stored_id = (
            stored_occupancy.get("queue_id") if isinstance(stored_occupancy, Mapping) else None
        )
        expected_id = self._occupancy["queue_id"] if self._occupancy is not None else None
        if stored_id != expected_id:
            self._note(
                kind="occupancy_rebuild",
                queue_id=expected_id or "",
                reason=(
                    f"存储的占用记录 {stored_id!r} 与条目状态不一致，"
                    f"已按条目重建为 {expected_id!r}。"
                ),
            )
            changed = True

        # 4) 幂等索引：丢弃指向不存在条目的记录；为缺索引条目补写（指纹未知）
        raw_index = document.get("idempotency")
        if isinstance(raw_index, Mapping):
            for request_id, record in raw_index.items():
                if not isinstance(request_id, str) or not isinstance(record, Mapping):
                    self._note(kind="idempotency_parse_error", reason="幂等索引存在无法解析的记录，已跳过。")
                    changed = True
                    continue
                queue_id = record.get("queue_id")
                if not isinstance(queue_id, str) or queue_id not in self._entries:
                    self._note(
                        kind="idempotency_stale",
                        reason=f"幂等记录 {request_id!r} 指向的条目不存在，已丢弃该索引。",
                    )
                    changed = True
                    continue
                fingerprint = record.get("fingerprint")
                consent_id = record.get("consent_id")
                created_at = record.get("created_at")
                self._index[request_id] = {
                    "queue_id": queue_id,
                    "fingerprint": fingerprint if isinstance(fingerprint, str) else None,
                    "consent_id": consent_id if isinstance(consent_id, str) else None,
                    "created_at": created_at if isinstance(created_at, str) else None,
                }
        missing = [
            entry
            for entry in self._entries.values()
            if entry.request_id not in self._index
        ]
        for entry in missing:
            self._index[entry.request_id] = {
                "queue_id": entry.queue_id,
                "fingerprint": None,
                "consent_id": entry.consent_id,
                "created_at": entry.created_at,
            }
            self._note(
                kind="idempotency_rebuild",
                queue_id=entry.queue_id,
                reason="条目缺少幂等索引，已按 request_id 补写（指纹未知，重放将返回原条目）。",
            )
            changed = True

        # 5) 计数器：不小于任何已用展示编号，避免编号重复
        counter = document.get("counter")
        if isinstance(counter, int) and not isinstance(counter, bool) and counter >= 0:
            self._counter = counter
        else:
            self._counter = 0
        highest = max((_display_sequence(entry.display_number) for entry in self._entries.values()), default=0)
        if self._counter < highest:
            self._note(
                kind="counter_rebuild",
                reason=f"展示编号计数器 {self._counter} 小于已用最大序号 {highest}，已提升避免重号。",
            )
            self._counter = highest
            changed = True

        # 6) 诊断记录：原样带回（截断到上限）
        raw_notes = document.get("notes")
        if isinstance(raw_notes, list):
            stored_notes = [dict(note) for note in raw_notes if isinstance(note, Mapping)]
            # 先前留存的诊断在前，本次恢复新产生的在后（时间顺序）
            self._notes = stored_notes + self._notes
            del self._notes[:-_MAX_NOTES]

        return changed

    def _commit(self) -> None:
        """整份文档原子提交：队列变更 + 幂等索引 + 占用记录 + 计数器 + 诊断。"""
        self._assert_slot_invariant()
        self._store.save(
            {
                "version": QueueStore.SCHEMA_VERSION,
                "updated_at": to_utc_iso(self._now()),
                "counter": self._counter,
                "entries": [entry.to_storage_dict() for entry in self._entries.values()],
                "idempotency": {key: dict(value) for key, value in self._index.items()},
                "occupancy": dict(self._occupancy) if self._occupancy is not None else None,
                "notes": self._notes[-_MAX_NOTES:],
            }
        )

    def _assert_slot_invariant(self) -> None:
        """内部不变量：至多一个拍摄位占用，且占用记录必须指向占用状态条目。"""
        occupants = [
            item.queue_id
            for item in self._entries.values()
            if occupies_capture_slot(item.status)
        ]
        if len(occupants) > 1:
            raise RuntimeError(f"内部不变量被破坏：出现多个拍摄位占用：{occupants}")
        if self._occupancy is not None:
            occupant = self._entries.get(self._occupancy.get("queue_id") or "")
            if occupant is None or not occupies_capture_slot(occupant.status):
                raise RuntimeError("内部不变量被破坏：占用记录指向非占用状态的条目")
