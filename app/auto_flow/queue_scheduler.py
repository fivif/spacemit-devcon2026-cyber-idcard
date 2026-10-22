# -*- coding: utf-8 -*-
"""QueueScheduler：队列自动调度（T-13）。

规格依据：`自动流程规格 v1（内部文档）` T-13 与 `自动流程规格 v1（内部文档）`
（组件职责 / Policy / Restart and recovery）。本模块只做“何时叫号、何时弃号、何时释放
拍摄位”的调度决策，不重复实现队列状态机、也不接触硬件：

- 叫号与期限的权威在 T-1 ``QueueService``：``call_next()`` 写 called_at/start_deadline
  （默认 120 秒，由 ``queue.start_timeout_seconds`` 配置），``expire_due(now)`` 只处理
  called 条目，``mark_processing/mark_failed/...`` 负责状态推进与占用释放；
- “无相机帧”的判定复用 T-20 ``camera_freshness_health``（按帧新鲜度，不看设备能否枚举）。

由服务生命周期驱动：调用方（T-15/T-16 接线）周期性调用 ``tick(now)``。now 必须取自
``QueueService`` 的同一个时钟，保证叫号时间与 tick 时刻同源；测试用 T-14 的假时钟推进。

单步调度顺序（非冻结时）：

1. 相机探针 -> camera_ok；默认无帧 -> 进入/继续 **冻结**：不叫号、不 expire，已叫号条目的
   期限顺延（见下）；
2. 相机 OK -> 结束冻结（若有）；先 ``expire_due(逻辑时刻)``，再在无占用时 ``call_next()``。

按需启动相机时，``allow_queue_without_camera=True``：叫号与超时检查独立于相机帧，
按真实期限继续执行，不累计冻结顺延；相机判定仍约束自动拍摄资格。

冻结口径（故障不罚弃号，T-13 验收口径）：

- 冻结从“本调度器首次观测到无帧”的时刻起计（进程重启会丢失此前的冻结账目，见“限制”）；
- 冻结期间按 tick 间隔累计 ``frozen_seconds_total``；当前 called 条目同步累计自己的顺延
  ``frozen_shift_seconds``（SLOT_STATUSES 保证同一时刻至多一个 called，因此按条目记账是精确的）；
- 期限判定用 **逻辑时刻 = now - 该条目的顺延**；冻结期间从不调用 ``expire_due``，
  因此推进时钟超过原始期限也不会弃号；
- 恢复后不重复叫号：仍在 called 的条目继续占位，直到它的非冻结等待满 120 秒才过期，
  随后下一位才会在同一 tick 被叫到；任何时刻最多一个拍摄位占用。

限制（诚实声明）：QueueService 没有“改写 start_deadline”的公开接口（T-1 只 import 不改），
所以顺延记在调度器内存里，条目自身的 start_deadline 字符串不变；``tick`` 结果给出
``effective_start_deadline`` 供展示/日志。进程重启会清零顺延账目：仅对“重启前已在叫号
且重启后仍在故障窗口”的条目可能少于理想顺延，其余场景只延长不缩短。

手势口径（按 spec：自动触发在 K3 验证通过前 no-go；GESTURE_DISABLED 不自动触发但保留回退）：

手势只影响“自动触发资格” ``auto_trigger_eligible``，**不暂停叫号**——叫号只在无相机帧时
暂停，手动路径（参与者点“开始”）始终保留。默认 ``gesture_required_for_auto_trigger=True``
且 ``gesture_enabled=False``，与配置默认值（BOOTH_GESTURE_ENABLED=false）一致。

拍摄位释放：

- ``capture_completed(source_saved=True)``：原图已落盘 -> ``mark_processing``，脱离拍摄位；
  AIGC（processing）与打印（ready_for_pickup）都不在 SLOT_STATUSES 里，不占拍摄位；
- ``capture_completed(source_saved=False)``：失败路径 -> ``mark_failed``，不进入处理流程；
  失败是终止态，占用随之释放（否则故障会永久占住唯一拍摄位、与“不因系统故障罚弃号/不阻塞
  队列”矛盾）——这里的“不释放”指不按成功路径释放到 processing；
- 超时由 ``expire_due`` 释放；取消/失败由 T-1 的 ``mark_*`` 释放。
"""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping
from datetime import datetime, timedelta, timezone
from typing import Any

from app.auto_flow.health_service import (
    DEFAULT_CAMERA_TIMEOUT_SECONDS,
    camera_freshness_health,
)
from app.auto_flow.models import (
    QueueEntry,
    QueueStatus,
    parse_utc_iso,
    to_utc_iso,
)
from app.auto_flow.queue_service import QueueService

__all__ = ["QueueScheduler"]

_UTC = timezone.utc
_NO_FRAME_HINT = "未接入相机探针：无法确认画面新鲜度，按无帧处理（暂停叫号）。"
_SOURCE_MISSING_REASON = (
    "原图未落盘：拍摄未产出，不按成功路径释放到 processing；转 failed 并释放拍摄位，"
    "避免故障阻塞后续叫号。"
)


def _normalize_moment(value: datetime) -> datetime:
    """外部传入的 datetime -> 带时区 UTC；naive 按 UTC 解释（与 T-1 同一口径）。"""
    if not isinstance(value, datetime):
        raise ValueError(f"tick(now) 需要 datetime，得到 {type(value).__name__}")
    if value.tzinfo is None:
        value = value.replace(tzinfo=_UTC)
    return value.astimezone(_UTC)


def _find_active_call(snapshot: Mapping[str, Any]) -> dict[str, Any] | None:
    """快照里当前处于 called 的条目（T-1 不变量：至多一个）；没有返回 None。"""
    for entry in snapshot.get("entries", ()):
        if entry.get("status") == QueueStatus.CALLED.value:
            return dict(entry)
    return None


class QueueScheduler:
    """队列调度器：``tick(now)`` 单步调度 + ``capture_completed`` 拍摄位释放。

    构造参数：
    - queue：T-1 ``QueueService`` 实例；
    - 相机探针（二选一，都不接则按“无帧”处理、暂停叫号——保守方向）：
      * ``camera_ok``：无参 callable -> bool，直接表示“画面新鲜/可拍”；
      * ``camera_last_frame_at``：无参 callable -> Unix 秒或 None（最近一帧时间戳），
        用 T-20 ``camera_freshness_health`` + ``camera_timeout_seconds`` 判定；
    - gesture_enabled：手势能力是否启用（config.gesture.enabled，默认 false）；
    - allow_queue_without_camera：按需启动相机时，等待叫号与超时不依赖相机帧；
    - gesture_required_for_auto_trigger：自动触发是否必须手势门（默认 true）；
      两个布尔只影响 ``auto_trigger_eligible``，不影响叫号。
    """

    def __init__(
        self,
        queue: QueueService,
        *,
        camera_ok: Callable[[], bool] | None = None,
        camera_last_frame_at: Callable[[], float | None] | None = None,
        camera_timeout_seconds: float = DEFAULT_CAMERA_TIMEOUT_SECONDS,
        gesture_enabled: bool = False,
        gesture_required_for_auto_trigger: bool = True,
        allow_queue_without_camera: bool = False,
    ) -> None:
        if not isinstance(queue, QueueService):
            raise TypeError("queue 必须是 T-1 的 QueueService 实例")
        if camera_ok is not None and camera_last_frame_at is not None:
            raise ValueError("camera_ok 与 camera_last_frame_at 只能注入一个")
        if camera_ok is not None and not callable(camera_ok):
            raise TypeError("camera_ok 必须是无参 callable")
        if camera_last_frame_at is not None and not callable(camera_last_frame_at):
            raise TypeError("camera_last_frame_at 必须是无参 callable")
        timeout = float(camera_timeout_seconds)
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError(
                f"camera_timeout_seconds 必须是正的有限秒数，得到 {camera_timeout_seconds!r}"
            )
        if not isinstance(gesture_enabled, bool) or not isinstance(
            gesture_required_for_auto_trigger, bool
        ):
            raise TypeError("gesture_enabled 与 gesture_required_for_auto_trigger 必须是 bool")

        self._queue = queue
        self._camera_ok_probe = camera_ok
        self._camera_frame_probe = camera_last_frame_at
        self._camera_timeout_seconds = timeout
        self._gesture_enabled = gesture_enabled
        self._gesture_required = gesture_required_for_auto_trigger
        if not isinstance(allow_queue_without_camera, bool):
            raise TypeError("allow_queue_without_camera 必须是 bool")
        self._allow_queue_without_camera = allow_queue_without_camera

        # 冻结/恢复状态机（进程内；重启清零，见模块 docstring“限制”）
        self._frozen = False
        self._frozen_since: datetime | None = None
        self._frozen_sample_at: datetime | None = None
        self._frozen_seconds_total = 0.0
        # 当前 called 条目的累计顺延（同一时刻至多一个 called，所以单槽记账即可）
        self._shift_queue_id: str | None = None
        self._shift_seconds = 0.0

    # ------------------------------------------------------------------ #
    # 只读状态（日志 / 健康投影 / 测试）
    # ------------------------------------------------------------------ #

    @property
    def frozen(self) -> bool:
        """当前是否因无相机帧处于“暂停叫号 + 期限冻结”。"""
        return self._frozen

    @property
    def frozen_since(self) -> datetime | None:
        """当前冻结起点（未冻结为 None）。"""
        return self._frozen_since

    @property
    def frozen_seconds_total(self) -> float:
        """进程内累计冻结时长（秒，含当前正在进行的部分）。"""
        return self._frozen_seconds_total

    # ------------------------------------------------------------------ #
    # 单步调度
    # ------------------------------------------------------------------ #

    def tick(self, now: datetime) -> dict[str, Any]:
        """单步调度，返回本 tick 的可观察结果（供测试断言与日志）。

        返回键：
        - ``now`` / ``camera_ok`` / ``camera_reason``：本 tick 时刻与相机判定；
        - ``frozen`` / ``frozen_since`` / ``frozen_seconds_total`` / ``just_frozen`` /
          ``just_recovered``：冻结状态机；
        - ``auto_trigger_eligible``：自动触发资格（相机 OK 且手势门允许），不影响叫号；
        - ``expire_checked_at`` / ``expired``：本 tick 的超时检查（冻结时为 None/[]）与被弃号 id；
        - ``called``：本 tick 新叫到的条目（``call_next`` 返回时才非 None）；
        - ``active_call``：本 tick 结束时仍在 called 的条目信息（含原始期限、顺延秒数、
          顺延后的 ``effective_start_deadline``）；
        - ``occupancy``：本 tick 结束时拍摄位占用的 queue_id（无占用为 None）。
        """
        moment = _normalize_moment(now)
        camera_ok, camera_reason = self._probe_camera(moment)
        scheduling_available = camera_ok or self._allow_queue_without_camera

        snapshot = self._queue.snapshot()
        active_entry = _find_active_call(snapshot)
        active_qid = active_entry.get("queue_id") if active_entry else None

        just_frozen = False
        just_recovered = False
        if not scheduling_available:
            if not self._frozen:
                self._frozen = True
                self._frozen_since = moment
                self._frozen_sample_at = moment
                just_frozen = True
            else:
                self._accrue_freeze(moment, active_qid)
        elif self._frozen:
            self._accrue_freeze(moment, active_qid)
            self._frozen = False
            self._frozen_since = None
            self._frozen_sample_at = None
            just_recovered = True

        expired_ids: list[str] = []
        called_info: dict[str, Any] | None = None
        expire_checked_at: str | None = None
        if scheduling_available:
            # 期限判定用逻辑时刻：真实时刻减去当前叫号条目在故障期间获得的顺延
            logical_now = moment - timedelta(seconds=self._shift_for(active_qid))
            expire_checked_at = to_utc_iso(logical_now)
            expired_ids = [entry.queue_id for entry in self._queue.expire_due(logical_now)]
            if self._queue.snapshot()["occupancy"] is None:
                called_entry = self._queue.call_next()
                if called_entry is not None:
                    called_info = self._describe_called_entry(called_entry)

        final = self._queue.snapshot()
        return {
            "now": to_utc_iso(moment),
            "camera_ok": camera_ok,
            "camera_reason": camera_reason,
            "frozen": self._frozen,
            "frozen_since": to_utc_iso(self._frozen_since) if self._frozen_since else None,
            "frozen_seconds_total": round(self._frozen_seconds_total, 3),
            "just_frozen": just_frozen,
            "just_recovered": just_recovered,
            "auto_trigger_eligible": self._auto_trigger_eligible(camera_ok),
            "expire_checked_at": expire_checked_at,
            "expired": expired_ids,
            "called": called_info,
            "active_call": self._describe_snapshot_call(_find_active_call(final)),
            "occupancy": (final.get("occupancy") or {}).get("queue_id"),
        }

    # ------------------------------------------------------------------ #
    # 拍摄位释放
    # ------------------------------------------------------------------ #

    def capture_completed(self, queue_id: str, *, source_saved: bool) -> dict[str, Any]:
        """拍摄完成回调：原图落盘后释放拍摄位。

        - ``source_saved=True``：``mark_processing``（脱离拍摄位；AIGC/打印不占位）；
        - ``source_saved=False``：``mark_failed``（失败路径，不进入处理流程；拍摄位随终止态释放）。

        返回 ``{"queue_id", "path", "status", "released", "source_saved", "reason"}``。
        queue_id 未知或状态不允许推进时，由 T-1 抛 ApiError / InvalidTransitionError，
        交由调用方（T-15 生命周期）记录与降级处理。
        """
        if not isinstance(source_saved, bool):
            raise ValueError(f"source_saved 必须是 bool，得到 {type(source_saved).__name__}")
        if source_saved:
            entry = self._queue.mark_processing(queue_id)
            return {
                "queue_id": entry.queue_id,
                "path": "success",
                "status": entry.status.value,
                "released": True,
                "source_saved": True,
                "reason": None,
            }
        entry = self._queue.mark_failed(queue_id, reason=_SOURCE_MISSING_REASON)
        return {
            "queue_id": entry.queue_id,
            "path": "failure",
            "status": entry.status.value,
            "released": True,
            "source_saved": False,
            "reason": _SOURCE_MISSING_REASON,
        }

    # ------------------------------------------------------------------ #
    # 内部：相机探针与手势门
    # ------------------------------------------------------------------ #

    def _probe_camera(self, moment: datetime) -> tuple[bool, str | None]:
        """相机是否可拍 + 原因；探针异常一律按无帧处理（保守，不误叫号）。"""
        if self._camera_ok_probe is not None:
            try:
                value = self._camera_ok_probe()
            except Exception as exc:  # noqa: BLE001 - 探针故障不得打断服务生命周期
                return False, f"相机探针异常（{type(exc).__name__}: {exc}），按无帧处理。"
            if not isinstance(value, bool):
                return False, f"相机探针必须返回 bool，得到 {type(value).__name__}，按无帧处理。"
            return value, None if value else "相机探针报告画面不可用。"
        if self._camera_frame_probe is not None:
            try:
                last_frame_at = self._camera_frame_probe()
            except Exception as exc:  # noqa: BLE001
                return False, f"相机帧探针异常（{type(exc).__name__}: {exc}），按无帧处理。"
            if isinstance(last_frame_at, bool) or (
                last_frame_at is not None and not isinstance(last_frame_at, (int, float))
            ):
                return False, "相机帧探针必须返回 Unix 秒或 None，按无帧处理。"
            health = camera_freshness_health(
                last_frame_at, moment.timestamp(), self._camera_timeout_seconds
            )
            return health.status == "ok", health.reason
        return False, _NO_FRAME_HINT

    def _auto_trigger_eligible(self, camera_ok: bool) -> bool:
        """自动触发资格：相机可拍 且（不要求手势 或 手势已启用）。与叫号无关。"""
        if not camera_ok:
            return False
        return self._gesture_enabled or not self._gesture_required

    # ------------------------------------------------------------------ #
    # 内部：冻结记账
    # ------------------------------------------------------------------ #

    def _accrue_freeze(self, moment: datetime, active_qid: str | None) -> None:
        """把自上次采样以来的冻结时长计入总账，并顺延当前 called 条目。"""
        sample = self._frozen_sample_at
        if sample is None:
            self._frozen_sample_at = moment
            return
        delta = (moment - sample).total_seconds()
        if delta <= 0:
            if delta < 0:
                # 时钟回退：只重设采样点，不记负时长
                self._frozen_sample_at = moment
            return
        self._frozen_sample_at = moment
        self._frozen_seconds_total += delta
        if active_qid:
            if active_qid != self._shift_queue_id:
                self._shift_queue_id = active_qid
                self._shift_seconds = 0.0
            self._shift_seconds += delta

    def _shift_for(self, queue_id: str | None) -> float:
        """当前叫号条目的顺延秒数；条目切换时清零（新叫号不带旧账）。"""
        if queue_id is None:
            self._shift_queue_id = None
            self._shift_seconds = 0.0
            return 0.0
        if queue_id != self._shift_queue_id:
            self._shift_queue_id = queue_id
            self._shift_seconds = 0.0
        return self._shift_seconds

    # ------------------------------------------------------------------ #
    # 内部：结果描述
    # ------------------------------------------------------------------ #

    def _describe_called_entry(self, entry: QueueEntry) -> dict[str, Any]:
        """新叫号条目的描述（刚叫号，顺延为 0，effective == 原始期限）。"""
        return {
            "queue_id": entry.queue_id,
            "display_number": entry.display_number,
            "called_at": entry.called_at,
            "start_deadline": entry.start_deadline,
            "frozen_shift_seconds": 0.0,
            "effective_start_deadline": entry.start_deadline,
        }

    def _describe_snapshot_call(self, entry: dict[str, Any] | None) -> dict[str, Any] | None:
        """当前仍在 called 的条目描述；含故障顺延与顺延后的期限。"""
        if entry is None:
            return None
        shift = self._shift_for(entry.get("queue_id"))
        deadline = entry.get("start_deadline")
        effective: str | None = None
        if deadline:
            try:
                effective = to_utc_iso(parse_utc_iso(deadline) + timedelta(seconds=shift))
            except ValueError:
                effective = None
        return {
            "queue_id": entry.get("queue_id"),
            "display_number": entry.get("display_number"),
            "called_at": entry.get("called_at"),
            "start_deadline": deadline,
            "frozen_shift_seconds": round(shift, 3),
            "effective_start_deadline": effective,
        }
