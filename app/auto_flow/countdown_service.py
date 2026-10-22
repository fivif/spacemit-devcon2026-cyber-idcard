# -*- coding: utf-8 -*-
"""T-15 服务端倒计时：安全门之后的 8 秒倒计时、取消判定与“应拍摄”决定。

规格依据：`docs/spec/自动流程规格-v1.md`（「Components / CaptureCoordinator」、
`GET /api/state` 的 capture 小节）与任务 T-15。当前产品默认已调整为 8 秒。

职责边界（逐条）：
- 只做倒计时与取消判定；**不**按下快门、**不**调用相机、**不**提交存储——那是
  :mod:`app.auto_flow.capture_bridge`（T-15）；HTTP 接线属 T-16。
- 触发资格由 :class:`app.auto_flow.capture_coordinator.CaptureCoordinator`（T-6）判定；
  本模块只登记“会话已开始”并读它的状态，不重复实现手势门槛。
- 同意校验复用 :meth:`app.auto_flow.consent_service.ConsentService.validate`
  （T-12）；会话存在与终态检测复用 T-1 的 ``QueueService.get()``。
- 纯逻辑 + 标准库：不 import cv2 / onnxruntime / spacemit_ort / http / socket，
  不联网、不写盘、不读环境变量、不打印日志。

取消条件（全部服务端检查，探针由调用方注入；任一成立 → 取消，绝不触发快门）：
1. ``camera_probe`` —— 相机帧失效 / 断流；
2. ``consent_probe`` —— 同意被撤回（缺省用 T-12 的 ``ConsentService.validate``）；
3. ``session_probe`` —— 会话被取消（缺省看 T-1 条目是否缺失或进入终态）；
4. ``person_probe`` —— 人数条件失效（多人 / 无人 / 主体离开画面）。

**不要求 OK 手势持续保持**（T-15 合同逐字）：倒计时开始后不再复检手势是否仍是 OK；
这是产品决定，避免参与者比完 OK 又放下手时白等 8 秒。

防重启 / 防重复（T-15 合同逐字「客户端刷新不得重启倒计时或重复拍摄」）：
- 同一 session_id 重复 :meth:`CountdownService.begin` **不重置**倒计时，原样返回在跑的
  状态对象（同一个 dict，供状态投影直接读）；
- :meth:`CountdownService.tick` 只在计时归零的那一次 tick 给出一次
  ``capture_due=True``；进入 capturing / completed / cancelled 后不再给；
- 状态机单向：countdown -> capturing -> completed，或 countdown -> cancelled（终态无出边）。

时间全部由调用方注入带时区的 ``now``（或构造时注入 ``clock``）；测试用假时钟推进，
不 sleep。状态对象内部用锁保护（HTTP 服务可能多线程）。
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Literal

from app.auto_flow.capture_coordinator import CaptureCoordinator
from app.auto_flow.consent_service import ConsentService
from app.auto_flow.models import to_utc_iso
from app.auto_flow.ports import Clock

__all__ = [
    "DEFAULT_COUNTDOWN_SECONDS",
    "CANCEL_REASON_CODES",
    "CountdownCanceledError",
    "CancelDecision",
    "CountdownState",
    "CountdownTick",
    "CountdownService",
]

DEFAULT_COUNTDOWN_SECONDS = 8

# 取消原因码取值域（与 CaptureCoordinator.REASON_CODES 同风格，供 T-16 映射文案/日志）。
CANCEL_REASON_CODES: frozenset[str] = frozenset(
    {
        "countdown_camera_invalid",
        "countdown_consent_revoked",
        "countdown_session_cancelled",
        "countdown_person_condition_invalid",
    }
)

# 计时归零的判定容差（秒）：假时钟推进 3.0 秒必须恰好归零；真实时钟的浮点毛刺不应
# 把 3 秒变成 4 秒。
_DUE_EPSILON_S = 1e-9

# 探针名 -> 默认取消原因码（探针未自带原因时补齐）
_REASON_CODE_BY_PROBE: dict[str, str] = {
    "camera_frame_valid": "countdown_camera_invalid",
    "consent_valid": "countdown_consent_revoked",
    "session_cancelled": "countdown_session_cancelled",
    "person_condition_valid": "countdown_person_condition_invalid",
}

CountdownPhase = Literal["countdown", "capturing", "completed", "cancelled"]

# 探针统一内部签名：(会话记录, 当前时刻) -> 判定
_ProbeFn = Callable[["_SessionRecord", datetime], Any]


class CountdownCanceledError(ValueError):
    """构造参数不合法（未知会话、非法取消原因等）；不是运行时取消。"""


@dataclass(frozen=True)
class CancelDecision:
    """一次取消探针的判定结果（探针返回二值或本对象）。"""

    canceled: bool
    reason_code: str = ""
    reason: str = ""


def _canceled(reason_code: str, reason: str) -> CancelDecision:
    return CancelDecision(canceled=True, reason_code=reason_code, reason=reason)


def _normalize_probe_result(name: str, result: object) -> CancelDecision:
    """探针返回值 -> CancelDecision（判据语义与探针名一致：探针回答“条件是否仍成立”）。

    探针契约（与 ``ConsentService.validate`` 同一习惯）：
    - ``True`` = 条件仍成立 -> **不取消**；
    - ``False`` / ``None`` / 空串 = 条件不再成立 -> **取消**；
    - :class:`CancelDecision` = 直接采用（可自带原因码与中文原因）；
    - 其它类型（dict / 数字 / 任意对象）= 不可解析 -> 安全方向按“条件不成立（取消）”处理。
    """
    if isinstance(result, CancelDecision):
        if result.canceled and result.reason_code and result.reason_code not in CANCEL_REASON_CODES:
            raise CountdownCanceledError(
                f"取消原因码必须是 {sorted(CANCEL_REASON_CODES)} 之一，得到 {result.reason_code!r}。"
            )
        return result
    if result is True:
        return CancelDecision(canceled=False)
    if result is False or result is None or result == "":
        return CancelDecision(canceled=True)
    return _canceled(
        "",
        f"取消探针 {name!r} 返回了不可解析的值 {result!r}（安全方向：按条件不成立处理）。",
    )


@dataclass
class _SessionRecord:
    """会话内部状态。state 是唯一对外可见的状态对象。"""

    session_id: str
    queue_id: str
    capture_consent_id: str
    duration_seconds: float
    started_at: datetime
    state: dict[str, Any]


class CountdownService:
    """服务端倒计时（T-15）。

    构造参数：
    - ``queue_service``：T-1 的 ``QueueService``（``begin`` 时读条目确认会话存在）；
    - ``coordinator``：可选 ``CaptureCoordinator``（T-6）；给了就在 ``begin`` 时复检
      “未触发过闸门”，并把 ``stable_frames`` 写进状态供展示；
    - ``consent_service``：可选 ``ConsentService``（T-12）；作为缺省同意探针，
      按 ``validate(consent_id, "capture", queue_id)`` 复检（撤回即取消）；
    - ``now_fn`` / ``clock``：注入时钟（等价写法）；都不给则用系统 UTC 现在；
    - ``countdown_seconds``：默认 8 秒；
    - ``camera_probe`` / ``consent_probe`` / ``session_probe`` / ``person_probe``：
      四个取消探针，签名 ``(now: datetime) -> object``；返回 ``True`` = 条件仍成立
      （不取消），``False`` / ``None`` = 条件不成立（取消）。未注入的探针取内置缺省
      （同意看 T-12、会话看 T-1 条目；相机与人数据实数必须由调用方提供）。
    """

    def __init__(
        self,
        *,
        queue_service: Any | None = None,
        coordinator: CaptureCoordinator | None = None,
        consent_service: ConsentService | None = None,
        now_fn: Callable[[], datetime] | None = None,
        clock: Clock | None = None,
        countdown_seconds: int | float | None = None,
        camera_probe: Callable[[datetime], Any] | None = None,
        consent_probe: Callable[[datetime], Any] | None = None,
        session_probe: Callable[[datetime], Any] | None = None,
        person_probe: Callable[[datetime], Any] | None = None,
    ) -> None:
        if now_fn is not None and clock is not None:
            raise CountdownCanceledError("now_fn 与 clock 只能给一个（等价写法）。")
        if now_fn is not None:
            if not callable(now_fn):
                raise CountdownCanceledError("now_fn 必须是可调用对象。")
            self._now_fn = now_fn
        elif clock is not None:
            if not callable(getattr(clock, "now", None)):
                raise CountdownCanceledError("clock 必须实现 now()。")
            self._now_fn = clock.now
        else:
            self._now_fn = lambda: datetime.now(timezone.utc)

        seconds = DEFAULT_COUNTDOWN_SECONDS if countdown_seconds is None else countdown_seconds
        if isinstance(seconds, bool) or not isinstance(seconds, (int, float)) or float(seconds) <= 0:
            raise CountdownCanceledError(f"countdown_seconds 必须是 >0 的秒数，得到 {seconds!r}。")

        self._queue = queue_service
        self._coordinator = coordinator
        self._consent = consent_service
        self._countdown_seconds = float(seconds)
        self._sessions: dict[str, _SessionRecord] = {}
        self._lock = threading.Lock()

        # 四个取消条件：显式注入优先（包装成统一内部签名），缺省走内置实现。
        self._probes: dict[str, _ProbeFn] = {
            "camera_frame_valid": (
                _wrap_probe(camera_probe, "camera_valid") if camera_probe is not None else _always_valid
            ),
            "consent_valid": (
                _wrap_probe(consent_probe, "consent_valid")
                if consent_probe is not None
                else self._default_consent_probe
            ),
            "session_cancelled": (
                _wrap_probe(session_probe, "session_valid")
                if session_probe is not None
                else self._default_session_probe
            ),
            "person_condition_valid": (
                _wrap_probe(person_probe, "person_condition_valid")
                if person_probe is not None
                else _always_valid
            ),
        }

    # ------------------------------------------------------------------ #
    # 只读属性
    # ------------------------------------------------------------------ #

    @property
    def countdown_seconds(self) -> float:
        """本次倒计时时长（秒）；默认 8.0。"""
        return self._countdown_seconds

    # ------------------------------------------------------------------ #
    # 公共 API
    # ------------------------------------------------------------------ #

    def status(self, session_id: str) -> dict[str, Any] | None:
        """状态查询：返回状态对象副本；未知会话返回 None。"""
        with self._lock:
            record = self._sessions.get(session_id if isinstance(session_id, str) else "")
            if record is None:
                return None
            return dict(record.state)

    def begin(
        self,
        session_id: str,
        *,
        queue_id: str | None = None,
        capture_consent_id: str | None = None,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        """开始倒计时并返回状态对象。

        - 重复调用同一 session_id **不重置倒计时**：原样返回在跑的状态对象（同一个
          dict；客户端刷新不会重启倒计时，也不会造成第二次拍摄）；
        - ``begin`` 时立即评估四个取消条件：已失效则直接进入 cancelled 终态（不拍摄）；
        - ``capture_consent_id`` 缺省从 ``queue_service.get(queue_id)`` 的绑定同意读取。
        """
        session = _require_nonempty(session_id, "session_id")
        queue_key = _require_nonempty(queue_id, "queue_id") if queue_id is not None else session
        moment = _coerce_now(now if now is not None else self._now_fn())
        with self._lock:
            existing = self._sessions.get(session)
            if existing is not None:
                return existing.state

            entry = self._queue.get(queue_key) if self._queue is not None else None
            if entry is None:
                raise CountdownCanceledError(f"队列条目不存在：{queue_key!r}（会话未开始）。")
            coordinator_status = (
                self._coordinator.status(queue_key) if self._coordinator is not None else None
            )
            if coordinator_status is not None and getattr(coordinator_status, "triggered", False):
                raise CountdownCanceledError(
                    "该会话的安全门已给出过触发资格（单次触发锁），不得再次倒计时。"
                )
            consent_id = capture_consent_id
            if consent_id is None:
                consent_id = getattr(entry, "consent_id", None) or ""
            consent_key = _require_nonempty(consent_id, "capture_consent_id")

            state: dict[str, Any] = {
                "session_id": session,
                "queue_id": queue_key,
                "capture_consent_id": consent_key,
                "phase": "countdown",
                "countdown_seconds": self._countdown_seconds,
                "started_at": to_utc_iso(moment),
                "updated_at": to_utc_iso(moment),
                "countdown_remaining": self._countdown_seconds,
                "stable_frames": getattr(coordinator_status, "stable_frames", None),
                "cancel_reason_code": None,
                "cancel_reason": None,
                "completed_at": None,
            }
            record = _SessionRecord(
                session_id=session,
                queue_id=queue_key,
                capture_consent_id=consent_key,
                duration_seconds=self._countdown_seconds,
                started_at=moment,
                state=state,
            )
            # begin 也评估探针：相机/同意/会话/人数在开始那一刻就已失效时，绝不进倒计时。
            decision = self._evaluate_cancel(record, moment)
            if decision.canceled:
                self._enter_cancelled(record, decision, moment)
            self._sessions[session] = record
            return record.state

    def tick(self, session_id: str, *, now: datetime | None = None) -> CountdownTick:
        """推进倒计时；返回一次判定（``capture_due`` 只在归零那一次为 True）。

        已取消 / 已拍摄 / 已完成 / 未知会话 → 安全 no-op（``capture_due=False``）。
        """
        session = _require_nonempty(session_id, "session_id")
        moment = _coerce_now(now if now is not None else self._now_fn())
        with self._lock:
            record = self._sessions.get(session)
            if record is None:
                return _tick("idle", session, "countdown_no_session",
                             "会话未开始倒计时，无操作。", None, False, moment)
            state = record.state
            phase = state["phase"]
            if phase != "countdown":
                # 已取消的会话要**把当初的取消原因一起回传**，不能只说一句
                # "已离开倒计时"。原因：begin() 里也会评估探针，"开始那一刻就
                # 不满足条件"（例如镜头前没人）会当场取消；此时调用方第一次
                # tick 拿到的是本分支，如果只报 countdown_noop，真实的
                # countdown_person_condition_invalid 就被丢了 —— 现场日志里
                # 只剩一句无意义的 noop，没法判断为什么没拍（实测踩过）。
                if phase == "cancelled":
                    return _tick(
                        phase, session,
                        state.get("cancel_reason_code") or "countdown_cancelled",
                        state.get("cancel_reason") or "倒计时已取消。",
                        0.0, False, moment,
                    )
                return _tick(phase, session, "countdown_noop",
                             "会话已离开倒计时（已取消/已拍摄/已完成），不再给应拍摄决定。",
                             state["countdown_remaining"], False, moment)

            decision = self._evaluate_cancel(record, moment)
            if decision.canceled:
                self._enter_cancelled(record, decision, moment)
                return _tick("cancelled", session, decision.reason_code, decision.reason,
                             0.0, False, moment)

            elapsed = (moment - record.started_at).total_seconds()
            if elapsed + _DUE_EPSILON_S < record.duration_seconds:
                remaining = max(0.0, record.duration_seconds - elapsed)
                state["countdown_remaining"] = remaining
                state["updated_at"] = to_utc_iso(moment)
                return _tick("countdown", session, "countdown_pending",
                             f"倒计时进行中，剩余 {remaining:.1f} 秒。", remaining, False, moment)

            return self._enter_capture_due(record, moment)

    def cancel(
        self,
        session_id: str,
        *,
        reason_code: str = "countdown_session_cancelled",
        reason: str | None = None,
        now: datetime | None = None,
    ) -> CountdownTick:
        """显式取消会话（“会话取消”条件的手动入口）；已终态时安全 no-op。"""
        session = _require_nonempty(session_id, "session_id")
        if reason_code not in CANCEL_REASON_CODES:
            raise CountdownCanceledError(
                f"取消原因码必须是 {sorted(CANCEL_REASON_CODES)} 之一，得到 {reason_code!r}。"
            )
        moment = _coerce_now(now if now is not None else self._now_fn())
        with self._lock:
            record = self._sessions.get(session)
            if record is None:
                return _tick("idle", session, "countdown_no_session",
                             "会话未开始倒计时，无操作。", None, False, moment)
            if record.state["phase"] != "countdown":
                return _tick(record.state["phase"], session, "countdown_noop",
                             "会话已离开倒计时，取消无操作。", record.state["countdown_remaining"],
                             False, moment)
            return self._enter_cancelled(
                record,
                CancelDecision(
                    canceled=True,
                    reason_code=reason_code,
                    reason=reason or "会话被取消。",
                ),
                moment,
            )

    # ------------------------------------------------------------------ #
    # 内部：缺省探针与状态机
    # ------------------------------------------------------------------ #

    def _default_consent_probe(self, record: _SessionRecord, now: datetime) -> CancelDecision:
        """同意撤回检测：复用 T-12 ``ConsentService.validate``（撤回/过期/用途不符 → 取消）。"""
        if self._consent is None:
            return CancelDecision(canceled=False)
        # 主体 = queue_id；T-12 的绑定语义是 capture 同意必须正好绑到本预约。
        valid = self._consent.validate(record.capture_consent_id, "capture", record.queue_id)
        if valid:
            return CancelDecision(canceled=False)
        return _canceled("countdown_consent_revoked", "拍摄同意已失效（撤回/过期/用途不符），取消拍摄。")

    def _default_session_probe(self, record: _SessionRecord, now: datetime) -> CancelDecision:
        """会话取消检测：队列条目必须仍存在、且未进入终态。"""
        if self._queue is None:
            return CancelDecision(canceled=False)
        entry = self._queue.get(record.queue_id)
        if entry is None:
            return _canceled("countdown_session_cancelled", "排队记录已不存在，取消拍摄。")
        status = getattr(entry, "status", None)
        status_value = getattr(status, "value", status)
        if status_value in ("cancelled", "failed", "expired"):
            return _canceled(
                "countdown_session_cancelled", f"排队条目已进入终态（{status_value}），取消拍摄。"
            )
        return CancelDecision(canceled=False)

    def _evaluate_cancel(self, record: _SessionRecord, now: datetime) -> CancelDecision:
        """按固定顺序评估四个取消条件；第一个成立即返回。"""
        for name, probe in self._probes.items():
            decision = self._invoke_probe(name, probe, record, now)
            if decision.canceled:
                if not decision.reason_code:
                    decision = CancelDecision(
                        canceled=True,
                        reason_code=_REASON_CODE_BY_PROBE[name],
                        reason=decision.reason or f"取消条件不成立：{name}。",
                    )
                return decision
        return CancelDecision(canceled=False)

    def _invoke_probe(
        self, name: str, probe: _ProbeFn, record: _SessionRecord, now: datetime
    ) -> CancelDecision:
        """调用一个探针；探针抛异常一律按“条件不成立”取消（安全方向）。"""
        try:
            result = probe(record, now)
        except Exception as exc:  # noqa: BLE001 - 探针故障按取消处理
            return _canceled(
                _REASON_CODE_BY_PROBE[name],
                f"取消探针 {name} 执行失败（{type(exc).__name__}: {exc}），取消拍摄。",
            )
        return _normalize_probe_result(name, result)

    def _enter_cancelled(
        self, record: _SessionRecord, decision: CancelDecision, now: datetime
    ) -> CountdownTick:
        """countdown -> cancelled（终态）：不触发快门。"""
        state = record.state
        state["phase"] = "cancelled"
        state["countdown_remaining"] = None
        state["updated_at"] = to_utc_iso(now)
        state["cancel_reason_code"] = decision.reason_code
        state["cancel_reason"] = decision.reason
        return _tick(
            "cancelled", record.session_id, decision.reason_code or "countdown_cancelled",
            decision.reason or "倒计时被取消。", 0.0, False, now,
        )

    def _enter_capture_due(self, record: _SessionRecord, now: datetime) -> CountdownTick:
        """countdown -> capturing：给出唯一的“应拍摄”决定，之后不再给。"""
        state = record.state
        state["phase"] = "capturing"
        state["countdown_remaining"] = 0.0
        state["updated_at"] = to_utc_iso(now)
        return _tick(
            "capturing", record.session_id, "capture_due",
            "倒计时归零，应拍摄（本次仅一次）。", 0.0, True, now,
        )


def _wrap_probe(probe: Callable[..., Any], semantic_name: str) -> _ProbeFn:
    """把调用方注入的探针 ``(now)`` 包装成统一内部签名 ``(record, now)``。

    只接受 ``(now)`` 一种写法（不给同一个能力两套参数形状，避免误用）；签名不符时
    直接报错，绝不静默换一种调用方式。
    """
    if not callable(probe):
        raise CountdownCanceledError(f"取消探针 {semantic_name} 必须是可调用对象。")

    def _wrapped(record: _SessionRecord, now: datetime) -> Any:
        try:
            return probe(now)
        except TypeError as exc:
            raise CountdownCanceledError(
                f"取消探针 {semantic_name} 的签名必须是 (now: datetime) -> object；"
                f"调用失败：{exc}"
            ) from exc

    return _wrapped


def _always_valid(record: _SessionRecord, now: datetime) -> bool:
    """缺省探针：未注入检查时按“条件仍成立”处理（不自造判定）。

    与探针契约一致：返回 ``True`` = 条件成立（不取消）。需要检查相机帧新鲜度 /
    人数条件的调用方必须注入对应探针；这里返回“通过”不是对硬件的判断，而是
    “该条件下没有可复检的证据，交回给安全门与上层”。
    """
    return True


def _tick(
    phase: str,
    session_id: str,
    reason_code: str,
    reason: str,
    remaining: float | None,
    capture_due: bool,
    now: datetime,
) -> CountdownTick:
    """构造一次 tick 判定。

    不查服务状态：全部调用点都在持锁的临界区内（`_lock` 是不可重入锁），再次取锁会
    自死锁；状态查询只能在锁外做（`status()` 自己取锁）。
    """
    return CountdownTick(
        capture_due=capture_due,
        phase=phase,
        session_id=session_id,
        remaining_seconds=remaining,
        reason_code=reason_code,
        reason=reason,
        decided_at=to_utc_iso(now),
    )


def _coerce_now(value: object) -> datetime:
    """规范化注入时间：必须带时区；统一转 UTC。"""
    if not isinstance(value, datetime):
        raise CountdownCanceledError("now 必须是 datetime（请从注入的时钟取时间）。")
    if value.tzinfo is None:
        raise CountdownCanceledError("now 必须带时区（测试请注入带时区的假时钟）。")
    return value.astimezone(timezone.utc)


def _require_nonempty(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise CountdownCanceledError(f"{field_name} 必须是非空字符串。")
    return value.strip()


@dataclass(frozen=True)
class CountdownState:
    """倒计时状态快照（``status()`` 的强类型视图；接线方也可直接读 dict）。"""

    session_id: str
    queue_id: str
    phase: CountdownPhase
    remaining_seconds: float | None
    started_at: str
    updated_at: str
    cancel_reason_code: str | None = None
    cancel_reason: str | None = None
    completed_at: str | None = None

    @classmethod
    def from_dict(cls, state: Mapping[str, Any]) -> "CountdownState":
        raw_remaining = state.get("countdown_remaining")
        return cls(
            session_id=str(state.get("session_id") or ""),
            queue_id=str(state.get("queue_id") or ""),
            phase=state.get("phase") or "countdown",  # type: ignore[arg-type]
            remaining_seconds=(
                raw_remaining
                if isinstance(raw_remaining, (int, float)) and not isinstance(raw_remaining, bool)
                else None
            ),
            started_at=str(state.get("started_at") or ""),
            updated_at=str(state.get("updated_at") or ""),
            cancel_reason_code=state.get("cancel_reason_code"),
            cancel_reason=state.get("cancel_reason"),
            completed_at=state.get("completed_at"),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "queue_id": self.queue_id,
            "phase": self.phase,
            "countdown_remaining": self.remaining_seconds,
            "started_at": self.started_at,
            "updated_at": self.updated_at,
            "cancel_reason_code": self.cancel_reason_code,
            "cancel_reason": self.cancel_reason,
            "completed_at": self.completed_at,
        }


@dataclass(frozen=True)
class CountdownTick:
    """一次 :meth:`CountdownService.tick` 的判定结果。

    - ``capture_due=True`` 表示“现在应当拍摄”，且**整个会话只会出现一次**；
    - ``phase`` 直接可进 ``GET /api/state`` 的 capture 小节（T-20 投影白名单值）；
    - ``remaining_seconds`` 与状态对象里的 ``countdown_remaining`` 同值。
    """

    capture_due: bool
    phase: str
    session_id: str
    remaining_seconds: float | None
    reason_code: str
    reason: str
    decided_at: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "capture_due": self.capture_due,
            "phase": self.phase,
            "session_id": self.session_id,
            "countdown_remaining": self.remaining_seconds,
            "reason_code": self.reason_code,
            "reason": self.reason,
            "decided_at": self.decided_at,
        }
