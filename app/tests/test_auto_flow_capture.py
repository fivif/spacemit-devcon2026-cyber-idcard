# -*- coding: utf-8 -*-
"""T-6 相机与手势安全门测试：触发资格判定的全部负例与安全断言。

运行（项目根目录）：
    python3 -m unittest app.tests.test_auto_flow_capture -v

覆盖（对应任务验收标准）：
- 禁用（adapter disabled / coordinator disabled）、未开始（无 session）、无帧/非 OK 手势、
  多人（person_count=2）、画面外（in_frame=False）、低置信度、过期观测、乱序（时间倒退）
  → 一律不触发；
- 稳定帧断裂重置（第 3 帧不符 → 服务端计数回 0），之后可重新累计到资格；
- 重复 OK 只产生一次触发资格（单次触发锁）；
- 浏览器不能直接提交可信观测：coordinator 只暴露服务端 API、拒绝原始 JSON 载荷、
  客户端 stable_frames 数字不被信任；
- 两个模块保持纯逻辑（不 import cv2 / spacemit_ort / onnxruntime / http/socket）。

约定：全部用注入的假时钟推进（毫秒精度），不 sleep；不联网、不写盘、不触碰硬件。
"""

from __future__ import annotations

import re
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from app.auto_flow import capture_coordinator as cc
from app.auto_flow import gesture_adapter as ga
from app.auto_flow.capture_coordinator import (
    REASON_CODES,
    CaptureCoordinator,
    CaptureStatus,
    TriggerDecision,
)
from app.auto_flow.config import GestureConfig
from app.auto_flow.models import GestureResult, parse_utc_iso
from app.auto_flow.ports import GesturePort


def _iso(moment: datetime) -> str:
    """毫秒精度 ISO 8601 UTC（观测需亚秒区分；models.to_utc_iso 是秒精度，不适合本测试）。"""
    return moment.isoformat(timespec="milliseconds").replace("+00:00", "Z")


class _FakeClock:
    """测试假时钟：只向前推进，毫秒精度。"""

    def __init__(self, start: str = "2026-09-30T09:00:00.000Z") -> None:
        self._now = parse_utc_iso(start)

    def now(self) -> datetime:
        return self._now

    def iso(self) -> str:
        return _iso(self._now)

    def advance(self, seconds: float) -> datetime:
        self._now = self._now + timedelta(seconds=seconds)
        return self._now


def _coordinator(**overrides) -> CaptureCoordinator:
    """默认“已启用 + 3 帧阈值 + 全条件开启”的协调器；测试按需覆盖。"""
    params = dict(
        enabled=True,
        stable_frames=3,
        minimum_confidence=0.9,
        require_single_person=True,
        require_in_frame=True,
    )
    params.update(overrides)
    return CaptureCoordinator(**params)


def _result(clock: _FakeClock, **overrides) -> GestureResult:
    """默认全条件合格的观测；测试只改要考察的字段。"""
    payload = dict(
        available=True,
        gesture="ok",
        confidence=0.95,
        person_count=1,
        in_frame=True,
        stable_frames=1,
        observed_at=clock.iso(),
    )
    payload.update(overrides)
    return GestureResult(**payload)


def _feed(
    coordinator: CaptureCoordinator, clock: _FakeClock, session_id: str, *, step: float = 0.1, **overrides
) -> TriggerDecision:
    """推进时钟 step 秒后喂一条观测（默认全条件合格）。"""
    clock.advance(step)
    return coordinator.on_gesture(session_id, _result(clock, **overrides), now=clock.now())


class DisabledAdapterTest(unittest.TestCase):
    """禁用适配器：未验证前唯一安全状态。"""

    def test_status_is_disabled_with_chinese_reason(self):
        adapter = ga.DisabledGestureAdapter(clock=_FakeClock())
        health = adapter.status()
        self.assertEqual(health.status, "disabled")
        self.assertEqual(health.reason, ga.DISABLED_REASON)
        self.assertIn("未验证", health.reason)
        parse_utc_iso(health.checked_at)  # 时间戳可解析

    def test_observe_returns_unavailable_without_fabricating_numbers(self):
        adapter = ga.DisabledGestureAdapter(clock=_FakeClock())
        result = adapter.observe("session-1")
        self.assertFalse(result.available)
        self.assertEqual(result.gesture, "none")
        self.assertEqual(result.stable_frames, 0)
        self.assertIsNone(result.confidence)
        self.assertIsNone(result.person_count)
        self.assertIsNone(result.in_frame)
        self.assertEqual(result.reason, ga.DISABLED_REASON)
        adapter.stop("session-1")  # 不抛异常

    def test_adapter_conforms_to_gesture_port_and_factory_returns_disabled(self):
        adapter = ga.build_gesture_adapter(clock=_FakeClock())
        self.assertIsInstance(adapter, ga.DisabledGestureAdapter)
        self.assertIsInstance(adapter, GesturePort)
        self.assertEqual(adapter.status().status, "disabled")


class DisabledGateTest(unittest.TestCase):
    """禁用门槛：协调器未启用时，完美观测也绝不触发。"""

    def test_disabled_coordinator_never_eligible(self):
        clock = _FakeClock()
        coordinator = _coordinator(enabled=False)
        coordinator.begin("s-1", now=clock.now())
        for _ in range(5):
            decision = _feed(coordinator, clock, "s-1")
            self.assertFalse(decision.eligible)
            self.assertEqual(decision.reason_code, "gesture_disabled")
        status = coordinator.status("s-1")
        self.assertFalse(status.triggered)
        self.assertEqual(status.stable_frames, 0)

    def test_disabled_adapter_result_rejected_even_when_coordinator_enabled(self):
        clock = _FakeClock()
        coordinator = _coordinator()
        coordinator.begin("s-1", now=clock.now())
        adapter = ga.DisabledGestureAdapter(clock=clock)
        clock.advance(0.1)
        decision = coordinator.on_gesture("s-1", adapter.observe("s-1"), now=clock.now())
        self.assertFalse(decision.eligible)
        self.assertEqual(decision.reason_code, "unavailable")
        self.assertEqual(coordinator.status("s-1").stable_frames, 0)


class SessionGateTest(unittest.TestCase):
    """会话归属：未开始、错会话、开始之前的观测一律不触发。"""

    def test_no_session_never_eligible(self):
        clock = _FakeClock()
        coordinator = _coordinator()
        decision = coordinator.on_gesture("s-1", _result(clock), now=clock.now())
        self.assertFalse(decision.eligible)
        self.assertEqual(decision.reason_code, "no_session")
        self.assertEqual(decision.stable_frames, 0)
        self.assertIsNone(coordinator.status("s-1"))

    def test_wrong_session_rejected(self):
        clock = _FakeClock()
        coordinator = _coordinator()
        coordinator.begin("s-1", now=clock.now())
        decision = _feed(coordinator, clock, "s-2")
        self.assertFalse(decision.eligible)
        self.assertEqual(decision.reason_code, "session_mismatch")
        self.assertIsNone(coordinator.status("s-2"))
        self.assertEqual(coordinator.status("s-1").stable_frames, 0)

    def test_observation_before_session_start_rejected(self):
        clock = _FakeClock()
        coordinator = _coordinator()
        coordinator.begin("s-1", now=clock.now())
        before = _iso(clock.now() - timedelta(seconds=0.5))
        decision = coordinator.on_gesture("s-1", _result(clock, observed_at=before), now=clock.now())
        self.assertFalse(decision.eligible)
        self.assertEqual(decision.reason_code, "before_session_start")
        self.assertEqual(coordinator.status("s-1").stable_frames, 0)

    def test_rebegin_resets_progress_and_invalidates_old_observations(self):
        clock = _FakeClock()
        coordinator = _coordinator()
        coordinator.begin("s-1", now=clock.now())
        _feed(coordinator, clock, "s-1")
        _feed(coordinator, clock, "s-1")
        self.assertEqual(coordinator.status("s-1").stable_frames, 2)
        old_observed = clock.iso()
        clock.advance(1.0)
        coordinator.begin("s-1", now=clock.now())  # 重新开始：计数与触发锁清零
        status = coordinator.status("s-1")
        self.assertEqual(status.stable_frames, 0)
        self.assertFalse(status.triggered)
        decision = coordinator.on_gesture("s-1", _result(clock, observed_at=old_observed), now=clock.now())
        self.assertFalse(decision.eligible)
        self.assertEqual(decision.reason_code, "before_session_start")


class ObservationConditionTest(unittest.TestCase):
    """单帧条件：无帧/非 OK、多人、画面外、低置信度都不计入连续段。"""

    def test_no_gesture_frame_never_eligible(self):
        clock = _FakeClock()
        coordinator = _coordinator()
        coordinator.begin("s-1", now=clock.now())
        for gesture in ("none", "unknown"):
            decision = _feed(coordinator, clock, "s-1", gesture=gesture)
            self.assertFalse(decision.eligible)
            self.assertEqual(decision.reason_code, "gesture_not_ok")
        self.assertEqual(coordinator.status("s-1").stable_frames, 0)

    def test_multiple_people_never_eligible(self):
        clock = _FakeClock()
        coordinator = _coordinator()
        coordinator.begin("s-1", now=clock.now())
        decision = _feed(coordinator, clock, "s-1", person_count=2)
        self.assertFalse(decision.eligible)
        self.assertEqual(decision.reason_code, "person_count_invalid")
        self.assertEqual(coordinator.status("s-1").stable_frames, 0)

    def test_missing_person_count_rejected_when_required_but_accepted_when_lax(self):
        clock = _FakeClock()
        strict = _coordinator()
        strict.begin("s-1", now=clock.now())
        decision = _feed(strict, clock, "s-1", person_count=None)
        self.assertEqual(decision.reason_code, "person_count_invalid")

        lax = _coordinator(require_single_person=False)
        lax.begin("s-2", now=clock.now())
        decision = _feed(lax, clock, "s-2", person_count=None)
        self.assertEqual(decision.reason_code, "collecting")
        self.assertEqual(lax.status("s-2").stable_frames, 1)

    def test_out_of_frame_never_eligible(self):
        clock = _FakeClock()
        coordinator = _coordinator()
        coordinator.begin("s-1", now=clock.now())
        for value in (False, None):
            decision = _feed(coordinator, clock, "s-1", in_frame=value)
            self.assertFalse(decision.eligible)
            self.assertEqual(decision.reason_code, "out_of_frame")
        self.assertEqual(coordinator.status("s-1").stable_frames, 0)

    def test_low_confidence_never_eligible(self):
        clock = _FakeClock()
        coordinator = _coordinator()
        coordinator.begin("s-1", now=clock.now())
        for value in (0.89, None):
            decision = _feed(coordinator, clock, "s-1", confidence=value)
            self.assertFalse(decision.eligible)
            self.assertEqual(decision.reason_code, "low_confidence")
        self.assertEqual(coordinator.status("s-1").stable_frames, 0)

    def test_confidence_threshold_boundary_counts(self):
        clock = _FakeClock()
        coordinator = _coordinator(minimum_confidence=0.9)
        coordinator.begin("s-1", now=clock.now())
        decision = _feed(coordinator, clock, "s-1", confidence=0.9)  # 恰好等于阈值 → 计入
        self.assertFalse(decision.eligible)
        self.assertEqual(decision.reason_code, "collecting")
        self.assertEqual(decision.stable_frames, 1)


class TimingTest(unittest.TestCase):
    """时间窗口：过期、未来、乱序、重复、间隔过小/过大。"""

    def test_stale_observation_resets_streak(self):
        clock = _FakeClock()
        coordinator = _coordinator()
        coordinator.begin("s-1", now=clock.now())
        _feed(coordinator, clock, "s-1")
        _feed(coordinator, clock, "s-1")
        self.assertEqual(coordinator.status("s-1").stable_frames, 2)
        stale_observed = clock.iso()
        clock.advance(2.0)  # 远超默认新鲜窗口 0.5s
        decision = coordinator.on_gesture("s-1", _result(clock, observed_at=stale_observed), now=clock.now())
        self.assertFalse(decision.eligible)
        self.assertEqual(decision.reason_code, "stale_observation")
        self.assertEqual(coordinator.status("s-1").stable_frames, 0)

    def test_future_observation_rejected(self):
        clock = _FakeClock()
        coordinator = _coordinator()
        coordinator.begin("s-1", now=clock.now())
        future = _iso(clock.now() + timedelta(seconds=5))
        decision = coordinator.on_gesture("s-1", _result(clock, observed_at=future), now=clock.now())
        self.assertFalse(decision.eligible)
        self.assertEqual(decision.reason_code, "future_observation")

    def test_out_of_order_observation_resets_streak(self):
        clock = _FakeClock()
        coordinator = _coordinator()
        coordinator.begin("s-1", now=clock.now())
        clock.advance(0.3)
        first = coordinator.on_gesture("s-1", _result(clock), now=clock.now())
        self.assertEqual(first.reason_code, "collecting")
        earlier = _iso(clock.now() - timedelta(seconds=0.1))  # 时间倒退
        replay = coordinator.on_gesture("s-1", _result(clock, observed_at=earlier), now=clock.now())
        self.assertFalse(replay.eligible)
        self.assertEqual(replay.reason_code, "out_of_order")
        self.assertEqual(coordinator.status("s-1").stable_frames, 0)
        recovered = _feed(coordinator, clock, "s-1")
        self.assertEqual(recovered.reason_code, "collecting")
        self.assertEqual(coordinator.status("s-1").stable_frames, 1)

    def test_duplicate_timestamp_rejected(self):
        clock = _FakeClock()
        coordinator = _coordinator()
        coordinator.begin("s-1", now=clock.now())
        _feed(coordinator, clock, "s-1")
        same_time = _result(clock, observed_at=clock.iso())  # 与上一条同时间戳
        decision = coordinator.on_gesture("s-1", same_time, now=clock.now())
        self.assertFalse(decision.eligible)
        self.assertEqual(decision.reason_code, "out_of_order")
        self.assertEqual(coordinator.status("s-1").stable_frames, 0)

    def test_interval_too_small_rejected(self):
        clock = _FakeClock()  # 默认 min_interval=0.02s
        coordinator = _coordinator()
        coordinator.begin("s-1", now=clock.now())
        _feed(coordinator, clock, "s-1", step=0.1)
        decision = _feed(coordinator, clock, "s-1", step=0.005)  # 5ms < 20ms，疑似回放
        self.assertFalse(decision.eligible)
        self.assertEqual(decision.reason_code, "interval_too_small")
        self.assertEqual(coordinator.status("s-1").stable_frames, 0)

    def test_interval_too_large_starts_new_streak(self):
        clock = _FakeClock()  # 默认 max_interval=0.5s
        coordinator = _coordinator()
        coordinator.begin("s-1", now=clock.now())
        _feed(coordinator, clock, "s-1")
        _feed(coordinator, clock, "s-1")
        decision = _feed(coordinator, clock, "s-1", step=1.0)  # 1.0s > 0.5s，连续性断裂
        self.assertFalse(decision.eligible)
        self.assertEqual(decision.reason_code, "continuity_broken")
        self.assertEqual(decision.stable_frames, 1)
        self.assertEqual(coordinator.status("s-1").stable_frames, 1)


class StreakAndLockTest(unittest.TestCase):
    """服务端连续计数、断裂重置与单次触发锁。"""

    def test_third_frame_break_resets_to_zero_then_recovers(self):
        clock = _FakeClock()
        coordinator = _coordinator(stable_frames=3)
        coordinator.begin("s-1", now=clock.now())
        self.assertEqual(_feed(coordinator, clock, "s-1").reason_code, "collecting")
        self.assertEqual(_feed(coordinator, clock, "s-1").reason_code, "collecting")
        self.assertEqual(coordinator.status("s-1").stable_frames, 2)
        broken = _feed(coordinator, clock, "s-1", person_count=2)  # 第 3 帧不符
        self.assertFalse(broken.eligible)
        self.assertEqual(broken.reason_code, "person_count_invalid")
        self.assertEqual(coordinator.status("s-1").stable_frames, 0)  # 计数回 0
        self.assertEqual(_feed(coordinator, clock, "s-1").reason_code, "collecting")
        self.assertEqual(_feed(coordinator, clock, "s-1").reason_code, "collecting")
        third = _feed(coordinator, clock, "s-1")
        self.assertTrue(third.eligible)
        self.assertEqual(third.reason_code, "eligible")

    def test_eligible_after_required_stable_frames_and_status(self):
        clock = _FakeClock()
        coordinator = _coordinator(stable_frames=3)
        session = coordinator.begin("s-1", now=clock.now())
        self.assertEqual(session.session_id, "s-1")
        parse_utc_iso(session.started_at)
        for _ in range(2):
            self.assertFalse(_feed(coordinator, clock, "s-1").eligible)
        third = _feed(coordinator, clock, "s-1")
        self.assertTrue(third.eligible)
        self.assertEqual(third.reason_code, "eligible")
        self.assertEqual(third.stable_frames, 3)
        self.assertEqual(third.required_frames, 3)
        status = coordinator.status("s-1")
        self.assertIsInstance(status, CaptureStatus)
        self.assertTrue(status.triggered)
        self.assertEqual(status.stable_frames, 3)
        self.assertEqual(status.last_observed_at, _result(clock).observed_at)  # 展示最后一条观测
        parse_utc_iso(status.triggered_at)
        self.assertEqual(status.last_reason_code, "eligible")
        self.assertIsNone(coordinator.status("other-session"))

    def test_single_trigger_lock_allows_only_one_eligibility(self):
        clock = _FakeClock()
        coordinator = _coordinator(stable_frames=3)
        coordinator.begin("s-1", now=clock.now())
        decisions = [_feed(coordinator, clock, "s-1") for _ in range(3)]
        self.assertEqual(sum(1 for d in decisions if d.eligible), 1)
        first_triggered_at = coordinator.status("s-1").triggered_at
        for _ in range(5):  # 重复 OK：永不给第二次资格
            later = _feed(coordinator, clock, "s-1")
            self.assertFalse(later.eligible)
            self.assertEqual(later.reason_code, "already_triggered")
        status = coordinator.status("s-1")
        self.assertTrue(status.triggered)
        self.assertEqual(status.triggered_at, first_triggered_at)  # 触发时刻不因重复 OK 改变
        self.assertEqual(status.stable_frames, 3)  # 触发后计数冻结


class ClientTrustTest(unittest.TestCase):
    """浏览器无法直接提交可信观测：服务端 API、无 HTTP 绑定、数字不被信任。"""

    def test_client_stable_frames_field_is_not_trusted(self):
        clock = _FakeClock()
        coordinator = _coordinator(stable_frames=5)
        coordinator.begin("s-1", now=clock.now())
        first = _feed(coordinator, clock, "s-1", stable_frames=10 ** 6)  # 客户端声称已稳定百万帧
        self.assertFalse(first.eligible)
        self.assertEqual(first.stable_frames, 1)  # 服务端只认自己数到的 1 帧
        self.assertEqual(coordinator.status("s-1").stable_frames, 1)
        for _ in range(4):  # 即便客户端一直声称百万帧，资格只由服务端计数决定
            decision = _feed(coordinator, clock, "s-1", stable_frames=10 ** 6)
        self.assertTrue(decision.eligible)
        self.assertEqual(decision.stable_frames, 5)

    def test_raw_json_like_payload_rejected(self):
        clock = _FakeClock()
        coordinator = _coordinator()
        coordinator.begin("s-1", now=clock.now())
        raw = {"available": True, "gesture": "ok", "stable_frames": 999, "observed_at": clock.iso()}
        with self.assertRaises(TypeError):
            coordinator.on_gesture("s-1", raw, now=clock.now())
        with self.assertRaises(TypeError):
            coordinator.on_gesture("s-1", "not-a-result", now=clock.now())  # 任何原始载荷都拒绝

    def test_public_api_has_no_http_surface(self):
        public = {name for name in dir(CaptureCoordinator) if not name.startswith("_")}
        self.assertEqual(public, {"begin", "on_gesture", "status", "enabled", "required_frames"})
        for banned in ("handle", "post", "dispatch", "route", "json", "request"):
            self.assertNotIn(banned, {name.lower() for name in public})

    def test_modules_stay_pure_logic(self):
        forbidden = re.compile(
            r"^\s*(?:import|from)\s+(cv2|spacemit_ort|onnxruntime|http|socketserver|socket)(?:[.\s]|$)",
            re.MULTILINE,
        )
        for module in (ga, cc):
            source = Path(module.__file__).read_text(encoding="utf-8")
            self.assertIsNone(forbidden.search(source), f"{module.__name__} 引入了禁止的模块")


class ValidationTest(unittest.TestCase):
    """输入校验与 config 接线。"""

    def test_begin_validates_inputs(self):
        coordinator = _coordinator()
        for bad in ("", "   ", None):
            with self.assertRaises(ValueError):
                coordinator.begin(bad, now=_FakeClock().now())
        with self.assertRaises(ValueError):
            coordinator.begin("s-1", now=datetime(2026, 9, 30, 9, 0, 0))  # naive（无时区）拒绝

    def test_constructor_validates_parameters(self):
        with self.assertRaises(ValueError):
            CaptureCoordinator(stable_frames=0)
        with self.assertRaises(ValueError):
            CaptureCoordinator(minimum_confidence=1.5)
        with self.assertRaises(ValueError):
            CaptureCoordinator(min_interval_seconds=0.5, max_interval_seconds=0.2)
        with self.assertRaises(ValueError):
            CaptureCoordinator(freshness_seconds=0)

    def test_gesture_config_drives_defaults(self):
        clock = _FakeClock()
        config = GestureConfig(
            enabled=True,
            model_path=None,
            stable_frames=2,
            minimum_confidence=0.5,
            require_single_person=False,
            require_in_frame=False,
        )
        coordinator = CaptureCoordinator(gesture_config=config)
        self.assertTrue(coordinator.enabled)
        self.assertEqual(coordinator.required_frames, 2)
        coordinator.begin("s-1", now=clock.now())
        _feed(coordinator, clock, "s-1", person_count=None, in_frame=None, confidence=0.6)
        second = _feed(coordinator, clock, "s-1", person_count=None, in_frame=None, confidence=0.6)
        self.assertTrue(second.eligible)

    def test_reason_codes_are_stable_contract(self):
        # 所有对外 reason_code 均属于导出的常量集合（T-15/T-16 据此做映射）。
        self.assertIn("eligible", REASON_CODES)
        self.assertIn("already_triggered", REASON_CODES)
        clock = _FakeClock()
        coordinator = _coordinator(enabled=False)
        coordinator.begin("s-1", now=clock.now())
        decision = _feed(coordinator, clock, "s-1")
        self.assertIn(decision.reason_code, REASON_CODES)


if __name__ == "__main__":
    unittest.main()
