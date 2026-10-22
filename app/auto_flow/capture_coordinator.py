# -*- coding: utf-8 -*-
"""T-6 触发资格判定：会话开始登记 + 服务端连续稳定帧累计 + 单次触发锁。

规格依据：`docs/spec/自动流程规格-v1.md`「Components / CaptureCoordinator」
「FR-5、FR-6」「Testing strategy」（禁用/多人/画面外/不稳定/低置信度负例序列）。

本任务边界（逐条，超出即越界）：
- 只做“触发资格判定”；不做倒计时（T-15）、不调用拍摄（T-15/T-2）、不做真实手势模型
  推理（K3 验证未过，手势禁用是当前唯一安全状态）。
- 同意校验不在本模块（路由层职责，T-16）；``begin()`` 只登记“会话已开始”。
- 不 import cv2 / spacemit_ort / onnxruntime，不联网、不写盘：纯逻辑 + 标准库，
  cv2 与 spacemit_ort 的进程隔离约束天然满足。
- 时间全部由调用方注入 ``now``（或构造时注入 ``clock``）；测试用假时钟推进，不 sleep。

安全设计（对应验收标准）：
- **不信任客户端**：``GestureResult.stable_frames`` 仅作展示/参考，连续帧计数完全由本
  协调器维护；``on_gesture()`` 只接受服务端 ``GestureResult`` 对象，拒绝原始 JSON 载荷，
  且本模块不注册任何 HTTP 路由——浏览器无法把自己的数字直接变成触发资格。
- **观测归属**：session_id 必须与当前会话一致；观测时间不得早于会话开始时刻；
  重新 ``begin()`` 会重置计数与触发锁，旧观测一律失效（before_session_start）。
- **新鲜且递增**：过期（距 now 超过 freshness）、来自未来、乱序或重复的时间戳，
  连续计数一律重置为 0。
- **间隔合理**：连续段内相邻两次合格观测的间隔必须落在
  [min_interval_seconds, max_interval_seconds]；过小视为不可信回放（重置为 0），
  过大视为连续性断裂（本次观测作为新连续段的第 1 帧）。
- **单次触发锁**：一个会话最多给出一次触发资格；之后重复 OK 返回 ``already_triggered``，
  绝不第二次 eligible（重给资格须由路由层重新 ``begin()``）。

真实适配器启用前的集成约束：观测时间戳需带亚秒精度（秒级时间戳在 ≥1 观测/s 的节奏下
会退化为“时间未严格递增”而被判乱序）；该要求写入 T-22 的验收项。
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from app.auto_flow.config import GestureConfig
from app.auto_flow.models import GestureResult, parse_utc_iso, to_utc_iso
from app.auto_flow.ports import Clock

__all__ = [
    "CaptureSession",
    "TriggerDecision",
    "CaptureStatus",
    "CaptureCoordinator",
    "REASON_CODES",
    "DEFAULT_STABLE_FRAMES",
    "DEFAULT_MINIMUM_CONFIDENCE",
    "DEFAULT_FRESHNESS_SECONDS",
    "DEFAULT_MIN_INTERVAL_SECONDS",
    "DEFAULT_MAX_INTERVAL_SECONDS",
]

# 与 config.py 的 BOOTH_GESTURE_* 默认值对齐（spec 标注为 provisional 候选值，
# T-22 实测后按结果校准；本模块只做工程默认，不把候选门槛当既成事实）。
DEFAULT_STABLE_FRAMES = 10
DEFAULT_MINIMUM_CONFIDENCE = 0.90

# 以下三个时间窗为 T-6 自定的合理默认（spec 未给数字，只给了 ≥10 观测/s 的候选预算）：
# - 新鲜度：观测距判定时刻超过 0.5s 视为过期；
# - 最小间隔：小于 0.02s（>50 观测/s）视为不可信回放；
# - 最大间隔：大于 0.5s（<2 观测/s）视为连续性断裂。
DEFAULT_FRESHNESS_SECONDS = 0.5
DEFAULT_MIN_INTERVAL_SECONDS = 0.02
DEFAULT_MAX_INTERVAL_SECONDS = 0.5

# TriggerDecision.reason_code 的完整取值域（T-15/T-16 可据此做映射/测试断言）。
REASON_CODES: frozenset[str] = frozenset(
    {
        "eligible",
        "collecting",
        "no_session",
        "session_mismatch",
        "gesture_disabled",
        "already_triggered",
        "invalid_observation",
        "before_session_start",
        "future_observation",
        "stale_observation",
        "out_of_order",
        "unavailable",
        "gesture_not_ok",
        "person_count_invalid",
        "out_of_frame",
        "low_confidence",
        "interval_too_small",
        "continuity_broken",
    }
)


@dataclass(frozen=True)
class CaptureSession:
    """一次“已开始”的拍摄会话（``begin()`` 的返回值）。

    只包含会话标识与开始时间；倒计时、拍摄等语义属于 T-15/T-2，不在本任务。
    """

    session_id: str
    started_at: str  # ISO 8601 UTC（秒精度，展示用；比较用内部保留的原始 datetime）

    def to_dict(self) -> dict[str, Any]:
        return {"session_id": self.session_id, "started_at": self.started_at}


@dataclass(frozen=True)
class TriggerDecision:
    """一次手势观测的触发资格判定（只表态“资格”，不代表已拍摄/将拍摄）。

    stable_frames 是服务端自己维护的连续合格计数（判定后的值），与观测里客户端
    声称的 stable_frames 无关。
    """

    eligible: bool
    session_id: str
    stable_frames: int
    required_frames: int
    reason_code: str  # 取值见 REASON_CODES
    reason: str  # 中文可读说明（面向操作员日志/后续 UI）
    decided_at: str  # ISO 8601 UTC

    def to_dict(self) -> dict[str, Any]:
        return {
            "eligible": self.eligible,
            "session_id": self.session_id,
            "stable_frames": self.stable_frames,
            "required_frames": self.required_frames,
            "reason_code": self.reason_code,
            "reason": self.reason,
            "decided_at": self.decided_at,
        }


@dataclass(frozen=True)
class CaptureStatus:
    """``status(session_id)`` 的返回值：当前计数 / 最后观测 / 是否已触发。"""

    session_id: str
    started_at: str
    stable_frames: int
    required_frames: int
    last_observed_at: str | None
    last_result: GestureResult | None
    triggered: bool
    triggered_at: str | None
    last_reason_code: str | None

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "session_id": self.session_id,
            "started_at": self.started_at,
            "stable_frames": self.stable_frames,
            "required_frames": self.required_frames,
            "triggered": self.triggered,
        }
        if self.last_observed_at is not None:
            payload["last_observed_at"] = self.last_observed_at
        if self.last_result is not None:
            payload["last_result"] = self.last_result.to_dict()
        if self.triggered_at is not None:
            payload["triggered_at"] = self.triggered_at
        if self.last_reason_code is not None:
            payload["last_reason_code"] = self.last_reason_code
        return payload


@dataclass(frozen=True)
class _ActiveSession:
    """内部会话状态：除展示用字符串外，保留原始 datetime 以保证比较口径一致。"""

    session_id: str
    started_at: str
    started_dt: datetime


def _coerce_now(value: datetime) -> datetime:
    """规范化调用方注入的时间：必须是带时区的 datetime。"""
    if not isinstance(value, datetime):
        raise TypeError("now 必须是 datetime（请从注入的时钟取时间）。")
    if value.tzinfo is None:
        raise ValueError("now 必须是带时区的 datetime（测试请注入带时区的假时钟）。")
    return value.astimezone(timezone.utc)


class CaptureCoordinator:
    """到号开始 + 触发资格判定（T-6 裁减版：不做倒计时、不拍摄、不做模型推理）。

    默认关闭（``enabled=False``，与 config 的安全默认一致）：即使灌入完美观测也绝不
    给出触发资格；启用需由调用方显式打开，且真实部署前必须完成 K3 手势验证。

    典型用法（T-15/T-16 接线）：
        coordinator = CaptureCoordinator(gesture_config=cfg.gesture, clock=clock)
        coordinator.begin(queue_id, now=clock.now())
        result = adapter.observe(queue_id)
        decision = coordinator.on_gesture(queue_id, result, now=clock.now())
        if decision.eligible: ...  # 交给 T-15 的倒计时

    线程安全：内部用一把可重入无关的互斥锁保护全部会话状态（HTTP 服务可能多线程）。
    """

    def __init__(
        self,
        *,
        gesture_config: GestureConfig | None = None,
        enabled: bool | None = None,
        stable_frames: int | None = None,
        minimum_confidence: float | None = None,
        require_single_person: bool | None = None,
        require_in_frame: bool | None = None,
        freshness_seconds: float = DEFAULT_FRESHNESS_SECONDS,
        min_interval_seconds: float = DEFAULT_MIN_INTERVAL_SECONDS,
        max_interval_seconds: float = DEFAULT_MAX_INTERVAL_SECONDS,
        clock: Clock | None = None,
    ) -> None:
        """显式参数优先于 ``gesture_config``；都不给时用安全默认（禁用 + spec 候选门槛）。"""
        config = gesture_config
        resolved_enabled = enabled if enabled is not None else (config.enabled if config is not None else False)
        resolved_frames = (
            stable_frames
            if stable_frames is not None
            else (config.stable_frames if config is not None else DEFAULT_STABLE_FRAMES)
        )
        resolved_confidence = (
            minimum_confidence
            if minimum_confidence is not None
            else (config.minimum_confidence if config is not None else DEFAULT_MINIMUM_CONFIDENCE)
        )
        resolved_single = (
            require_single_person
            if require_single_person is not None
            else (config.require_single_person if config is not None else True)
        )
        resolved_in_frame = (
            require_in_frame
            if require_in_frame is not None
            else (config.require_in_frame if config is not None else True)
        )

        if not isinstance(resolved_frames, int) or isinstance(resolved_frames, bool) or resolved_frames < 1:
            raise ValueError(f"stable_frames 必须是 ≥1 的整数，得到 {resolved_frames!r}。")
        if isinstance(resolved_confidence, bool) or not isinstance(resolved_confidence, (int, float)):
            raise ValueError(f"minimum_confidence 必须是 0..1 的数字，得到 {resolved_confidence!r}。")
        if not 0.0 <= float(resolved_confidence) <= 1.0:
            raise ValueError(f"minimum_confidence 必须在 [0, 1] 内，得到 {resolved_confidence!r}。")
        for name, value in (
            ("freshness_seconds", freshness_seconds),
            ("min_interval_seconds", min_interval_seconds),
            ("max_interval_seconds", max_interval_seconds),
        ):
            if isinstance(value, bool) or not isinstance(value, (int, float)) or float(value) <= 0.0:
                raise ValueError(f"{name} 必须是 >0 的秒数，得到 {value!r}。")
        if float(min_interval_seconds) > float(max_interval_seconds):
            raise ValueError(
                f"min_interval_seconds({min_interval_seconds}) 不得大于 max_interval_seconds({max_interval_seconds})。"
            )

        self._enabled = bool(resolved_enabled)
        self._stable_frames = int(resolved_frames)
        self._minimum_confidence = float(resolved_confidence)
        self._require_single_person = bool(resolved_single)
        self._require_in_frame = bool(resolved_in_frame)
        self._freshness_seconds = float(freshness_seconds)
        self._min_interval_seconds = float(min_interval_seconds)
        self._max_interval_seconds = float(max_interval_seconds)
        self._clock = clock

        self._lock = threading.Lock()
        self._session: _ActiveSession | None = None
        self._streak = 0  # 服务端连续合格计数（唯一可信来源）
        self._last_seen: datetime | None = None  # 已见过的最大观测时刻（防重放）
        self._last_counted: datetime | None = None  # 最近一次计入连续段的观测时刻
        self._last_observed_at: str | None = None
        self._last_result: GestureResult | None = None
        self._triggered = False
        self._triggered_at: str | None = None
        self._last_reason_code: str | None = None

    # ------------------------------------------------------------------ #
    # 只读属性
    # ------------------------------------------------------------------ #

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def required_frames(self) -> int:
        return self._stable_frames

    # ------------------------------------------------------------------ #
    # 公共 API（服务端专用；无 HTTP 绑定，不接受原始客户端载荷）
    # ------------------------------------------------------------------ #

    def begin(self, session_id: str, *, now: datetime | None = None) -> CaptureSession:
        """登记“会话已开始”（同意校验由路由层完成，本方法不判断同意语义）。

        对同一 session_id 再次调用即重新开始：计数、最后观测、触发锁全部清零，
        此前的观测因早于新的开始时刻而失效。
        """
        if not isinstance(session_id, str) or not session_id.strip():
            raise ValueError("session_id 必须是非空字符串。")
        started_dt = _coerce_now(now if now is not None else self._now())
        with self._lock:
            self._session = _ActiveSession(
                session_id=session_id,
                started_at=to_utc_iso(started_dt),
                started_dt=started_dt,
            )
            self._streak = 0
            self._last_seen = None
            self._last_counted = None
            self._last_observed_at = None
            self._last_result = None
            self._triggered = False
            self._triggered_at = None
            self._last_reason_code = None
            return CaptureSession(session_id=session_id, started_at=self._session.started_at)

    def on_gesture(self, session_id: str, result: GestureResult, *, now: datetime | None = None) -> TriggerDecision:
        """判定一次手势观测是否构成触发资格（服务端权威判定）。

        观测的全部条件（OK 手势、可用性、人数、画面内、置信度、新鲜度、时间递增、
        间隔合理）都在这里由服务端检查；任一不满足 → 连续计数重置为 0。
        达到 ``stable_frames`` 且首次 → eligible=True（单次触发锁立即生效）。

        ``result`` 必须是 ``models.GestureResult``（服务端适配器产物）；原始 dict/JSON
        一律拒绝——浏览器不能直接提交可信观测。
        """
        if not isinstance(result, GestureResult):
            raise TypeError(
                "on_gesture() 只接受服务端 GestureResult 对象；原始 JSON/dict 一律拒绝"
                "（浏览器不能直接提交可信手势观测）。"
            )
        now_dt = _coerce_now(now if now is not None else self._now())
        with self._lock:
            session = self._session
            if session is None:
                return self._decide(False, session_id, "no_session", "尚未开始会话，禁止触发。", now_dt, streak=0)
            if session_id != session.session_id:
                return self._decide(
                    False, session_id, "session_mismatch", "观测不属于当前会话，禁止触发。", now_dt, streak=0
                )

            # 透明性：无论如何都记录最后一条观测（便于 operator 排查），但计数只信服务端。
            self._last_result = result
            self._last_observed_at = result.observed_at

            if not self._enabled:
                return self._decide(
                    False, session_id, "gesture_disabled", "手势未启用/未验证，禁止触发。", now_dt
                )
            if self._triggered:
                return self._decide(
                    False, session_id, "already_triggered", "本会话已给出触发资格（单次触发锁），不再重复给资格。", now_dt
                )

            try:
                observed = parse_utc_iso(result.observed_at)
            except ValueError:
                return self._fail("invalid_observation", "观测时间戳非法，连续计数重置。", now_dt)

            if observed < session.started_dt:
                return self._fail("before_session_start", "观测时间早于本次会话开始时刻，连续计数重置。", now_dt)
            if observed > now_dt:
                return self._fail("future_observation", "观测时间来自未来（时钟不可信），连续计数重置。", now_dt)
            age = (now_dt - observed).total_seconds()
            if age > self._freshness_seconds:
                return self._fail(
                    "stale_observation",
                    f"观测已过期（{age:.3f}s > 新鲜窗口 {self._freshness_seconds:.3f}s），连续计数重置。",
                    now_dt,
                )
            if self._last_seen is not None and observed <= self._last_seen:
                return self._fail("out_of_order", "观测时间未严格递增（乱序/重复），连续计数重置。", now_dt)
            self._last_seen = observed

            if not result.available:
                return self._fail("unavailable", "手势组件不可用（未启用或未验证），禁止触发。", now_dt)
            if result.gesture != "ok":
                return self._fail(
                    "gesture_not_ok", f"未检测到 OK 手势（gesture={result.gesture!r}），连续计数重置。", now_dt
                )
            if self._require_single_person and result.person_count != 1:
                return self._fail(
                    "person_count_invalid",
                    f"人数不满足单人条件（person_count={result.person_count!r}），连续计数重置。",
                    now_dt,
                )
            if self._require_in_frame and result.in_frame is not True:
                return self._fail(
                    "out_of_frame", f"主体不在画面内（in_frame={result.in_frame!r}），连续计数重置。", now_dt
                )
            if result.confidence is None or result.confidence < self._minimum_confidence:
                return self._fail(
                    "low_confidence",
                    f"置信度不达阈值（confidence={result.confidence!r} < {self._minimum_confidence}），连续计数重置。",
                    now_dt,
                )

            if self._streak > 0 and self._last_counted is not None:
                delta = (observed - self._last_counted).total_seconds()
                if delta < self._min_interval_seconds:
                    return self._fail(
                        "interval_too_small",
                        f"观测间隔过小（{delta:.3f}s < {self._min_interval_seconds:.3f}s，疑似回放），连续计数重置。",
                        now_dt,
                    )
                if delta > self._max_interval_seconds:
                    self._streak = 1
                    self._last_counted = observed
                    return self._decide(
                        False,
                        session_id,
                        "continuity_broken",
                        f"观测间隔过大（{delta:.3f}s > {self._max_interval_seconds:.3f}s），连续性断裂："
                        "本次观测作为新连续段第 1 帧。",
                        now_dt,
                    )

            self._streak += 1
            self._last_counted = observed
            if self._streak >= self._stable_frames:
                self._triggered = True
                self._triggered_at = to_utc_iso(now_dt)
                return self._decide(
                    True,
                    session_id,
                    "eligible",
                    f"服务端连续稳定帧 {self._streak}/{self._stable_frames}，给出触发资格（单次触发锁生效）。",
                    now_dt,
                )
            return self._decide(
                False, session_id, "collecting", f"连续稳定帧累计中（{self._streak}/{self._stable_frames}）。", now_dt
            )

    def status(self, session_id: str) -> CaptureStatus | None:
        """当前会话状态：计数 / 最后观测 / 是否已触发；未知会话返回 None。"""
        with self._lock:
            session = self._session
            if session is None or session_id != session.session_id:
                return None
            return CaptureStatus(
                session_id=session.session_id,
                started_at=session.started_at,
                stable_frames=self._streak,
                required_frames=self._stable_frames,
                last_observed_at=self._last_observed_at,
                last_result=self._last_result,
                triggered=self._triggered,
                triggered_at=self._triggered_at,
                last_reason_code=self._last_reason_code,
            )

    # ------------------------------------------------------------------ #
    # 内部工具
    # ------------------------------------------------------------------ #

    def _now(self) -> datetime:
        if self._clock is not None:
            return _coerce_now(self._clock.now())
        return datetime.now(timezone.utc)

    def _fail(self, code: str, message: str, now_dt: datetime) -> TriggerDecision:
        """不合格观测：连续计数重置为 0（含乱序/过期），并记录原因。"""
        self._streak = 0
        self._last_counted = None
        return self._decide(False, self._session.session_id, code, message, now_dt)

    def _decide(
        self,
        eligible: bool,
        session_id: str,
        code: str,
        message: str,
        now_dt: datetime,
        *,
        streak: int | None = None,
    ) -> TriggerDecision:
        # 只有针对当前会话的判定才刷新 last_reason_code（不把别的会话的原因串到本会话）。
        if self._session is not None and session_id == self._session.session_id:
            self._last_reason_code = code
        return TriggerDecision(
            eligible=eligible,
            session_id=session_id,
            stable_frames=self._streak if streak is None else streak,
            required_frames=self._stable_frames,
            reason_code=code,
            reason=message,
            decided_at=to_utc_iso(now_dt),
        )
