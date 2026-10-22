# -*- coding: utf-8 -*-
"""T-15 倒计时与授权拍摄测试：默认 8 秒、显式 3 秒、取消条件与失败不签发令牌。

运行（项目根目录）：
    python3 -m unittest app.tests.test_auto_flow_countdown -v

覆盖（对应任务验收标准）：
- 默认 8 秒与显式 3 秒均仅调用一次授权拍摄（FakeCamera.calls 计数 == 1）；
- 相机帧失效 / 同意撤回 / 会话取消 / 人数条件失效 → 取消且快门 0 次（四条独立用例）；
- 拍摄失败（FakeCamera 抛错）与提交失败（PhotoStore 抛错）→ 作业 failed、
  无成功图片、无取图令牌；
- 重复 begin / tick 不重启倒计时、不重复拍摄；已完成后不再触发。

约定：全部用注入的假时钟推进，不 sleep；相机与相册写临时目录（T-14 夹具强制），
不联网、不触碰硬件、不修改任何生产文件。
"""

from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.auto_flow import errors
from app.auto_flow.capture_bridge import CaptureBridge, CaptureFailedError
from app.auto_flow.capture_coordinator import CaptureCoordinator
from app.auto_flow.consent_handlers import verify_queue_start
from app.auto_flow.consent_service import ConsentService, MemoryConsentStore
from app.auto_flow.countdown_service import (
    CANCEL_REASON_CODES,
    DEFAULT_COUNTDOWN_SECONDS,
    CountdownCanceledError,
    CountdownService,
    CountdownTick,
)
from app.auto_flow.models import GestureResult, QueueStatus, QueueSubmission, is_valid_photo_token
from app.auto_flow.queue_service import QueueService
from app.auto_flow.queue_store import MemoryQueueStore
from app.auto_flow.state_projection import public_capture_fields
from app.tests.auto_flow_fixtures import FakeCamera, FakeClock, FakePhotoStore, new_photo_token


class _FailingStore:
    """PhotoStore 提交失败的替身（其余接口与端口一致）。"""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        self.attempts = 0

    def commit(self, job_id: str, source_path: str, variant: str):  # noqa: ANN201 - 测试替身
        self.attempts += 1
        raise RuntimeError(self.reason)


class _AsyncStore:
    """只模拟“接管终帧后后台处理”的存储端口。"""

    def __init__(self) -> None:
        self.queue = None
        self.calls = []

    def commit_async(self, job_id: str, source_path: str, variant: str) -> None:
        self.calls.append((job_id, source_path, variant))

    def commit(self, job_id: str, source_path: str, variant: str):
        raise AssertionError("异步适配器不应走同步提交")

    def finish(self, job_id: str) -> None:
        self.queue.mark_ready_for_pickup(job_id)


class _Harness:
    """T-15 测试装配：假时钟 + T-1 队列 + T-6 协调器 + T-12 同意 + 假相机/相册 + T-15 服务。"""

    def __init__(self, *, photo_store=None) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="auto-flow-t15-")
        self.root = Path(self._tmp.name)
        self.clock = FakeClock()
        self.queue = QueueService(MemoryQueueStore(), clock=self.clock)
        self.consent = ConsentService(MemoryConsentStore(), clock=self.clock)
        self.coordinator = CaptureCoordinator(enabled=True, stable_frames=10, clock=self.clock)
        self.camera = FakeCamera(self.root, self.clock)
        self.photo_store = photo_store if photo_store is not None else FakePhotoStore(self.root, self.clock)
        self.countdown = None
        self.bridge = CaptureBridge(
            queue_service=self.queue, camera=self.camera, photo_store=self.photo_store, clock=self.clock
        )
        self.queue_id = ""
        self.consent_id = ""

    # ---- 装配 ---------------------------------------------------------- #

    def bind_queue(self, *, consent_valid=True) -> str:
        """走真实 T-12 路径：签发同意 -> 提交 -> 绑定 -> 到号 -> 开始 -> 严格校验。"""
        self.consent_id = self.consent.record("capture", consent_valid).consent_id
        entry = self.queue.submit(
            QueueSubmission(
                request_id="req-t15-1",
                style_id="neon",
                capture_consent_id=self.consent_id,
            )
        )
        self.queue_id = entry.queue_id
        self.consent.bind(self.consent_id, self.queue_id)
        called = self.queue.call_next()
        assert called is not None and called.queue_id == self.queue_id
        self.queue.start(self.queue_id)
        verify_queue_start(self.consent, capture_consent_id=self.consent_id, queue_id=self.queue_id)
        self.coordinator.begin(self.queue_id, now=self.clock.now())
        return self.queue_id

    def make_countdown(self, *, countdown_seconds=3, **overrides):
        """旧用例显式用 3 秒；传 None 验证服务默认；同意探针走真实 ConsentService。"""
        params = dict(
            queue_service=self.queue,
            coordinator=self.coordinator,
            consent_service=self.consent,
            clock=self.clock,
        )
        if countdown_seconds is not None:
            params["countdown_seconds"] = countdown_seconds
        params.update(overrides)
        self.countdown = CountdownService(**params)
        return self.countdown

    def advance(self, seconds: float) -> None:
        self.clock.advance(seconds)

    def status(self) -> str:
        entry = self.queue.get(self.queue_id)
        return str(entry.status.value if hasattr(entry.status, "value") else entry.status)

    def close(self) -> None:
        self._tmp.cleanup()


class CountdownDefaultsEightSecondsTest(unittest.TestCase):
    def setUp(self):
        self.h = _Harness()
        self.addCleanup(self.h.close)
        self.queue_id = self.h.bind_queue()
        self.service = self.h.make_countdown(countdown_seconds=None)

    def test_default_eight_seconds_triggers_one_authorized_shutter(self):
        self.assertEqual(DEFAULT_COUNTDOWN_SECONDS, 8)
        self.assertEqual(self.service.countdown_seconds, 8.0)
        state = self.service.begin(self.queue_id, now=self.h.clock.now())
        self.assertEqual(state["countdown_remaining"], 8.0)
        for elapsed in range(8):
            with self.subTest(elapsed=elapsed):
                tick = self.service.tick(self.queue_id, now=self.h.clock.now())
                self.assertFalse(tick.capture_due)
                self.assertEqual(tick.remaining_seconds, 8.0 - elapsed)
                self.assertEqual(self.h.camera.calls, [])
                self.h.advance(1.0)
        due = self.service.tick(self.queue_id, now=self.h.clock.now())
        self.assertTrue(due.capture_due)
        self.assertEqual(due.remaining_seconds, 0.0)
        self.assertFalse(self.service.tick(self.queue_id, now=self.h.clock.now()).capture_due)
        result = self.h.bridge.run(queue_id=self.queue_id, capture_due=due.capture_due,
                                   job_id="job-default-eight")
        self.assertEqual(result.queue_status, "ready_for_pickup")
        self.assertTrue(is_valid_photo_token(result.photo.photo_token))
        self.assertEqual(self.h.camera.calls, [self.h.consent_id])
        self.service.begin(self.queue_id, now=self.h.clock.now())
        self.h.advance(8.0)
        self.assertFalse(self.service.tick(self.queue_id, now=self.h.clock.now()).capture_due)
        duplicate = self.h.bridge.run(queue_id=self.queue_id, capture_due=True,
                                      job_id="job-default-eight")
        self.assertTrue(duplicate.duplicate)
        self.assertEqual(self.h.camera.calls, [self.h.consent_id])
        self.assertEqual(len(self.h.photo_store.records()), 1)

    def test_microsecond_start_waits_full_eight_seconds_before_one_shutter(self):
        self.h.advance(0.9)
        self.assertEqual(self.h.clock.now().microsecond, 900000)
        first = self.service.begin(self.queue_id, now=self.h.clock.now())
        initial = self.service.tick(self.queue_id, now=self.h.clock.now())
        self.assertEqual(initial.remaining_seconds, 8.0)
        self.assertFalse(initial.capture_due)

        self.h.advance(7.9)
        self.assertIs(self.service.begin(self.queue_id, now=self.h.clock.now()), first)
        pending = self.service.tick(self.queue_id, now=self.h.clock.now())
        self.assertEqual(pending.phase, "countdown")
        self.assertFalse(pending.capture_due)
        self.assertAlmostEqual(pending.remaining_seconds, 0.1)
        self.assertEqual(self.h.camera.calls, [])

        self.h.advance(0.099999)
        self.assertFalse(self.service.tick(self.queue_id, now=self.h.clock.now()).capture_due)
        self.h.advance(0.000001)
        due = self.service.tick(self.queue_id, now=self.h.clock.now())
        self.assertTrue(due.capture_due)
        self.assertEqual(due.remaining_seconds, 0.0)
        self.h.bridge.run(queue_id=self.queue_id, capture_due=due.capture_due,
                          job_id="job-microsecond-eight")
        self.assertFalse(self.service.tick(self.queue_id, now=self.h.clock.now()).capture_due)
        self.assertEqual(self.h.camera.calls, [self.h.consent_id])
        self.assertEqual(len(self.h.photo_store.records()), 1)

    def test_repeated_begin_keeps_original_eight_second_deadline(self):
        first = self.service.begin(self.queue_id, now=self.h.clock.now())
        started_at = first["started_at"]
        self.h.advance(3.0)
        self.assertFalse(self.service.tick(self.queue_id, now=self.h.clock.now()).capture_due)
        repeated = self.service.begin(self.queue_id, now=self.h.clock.now())
        self.assertIs(repeated, first)
        self.assertEqual(repeated["started_at"], started_at)
        self.assertEqual(repeated["countdown_remaining"], 5.0)
        self.h.advance(4.0)
        self.service.begin(self.queue_id, now=self.h.clock.now())
        pending = self.service.tick(self.queue_id, now=self.h.clock.now())
        self.assertEqual(pending.remaining_seconds, 1.0)
        self.assertFalse(pending.capture_due)
        self.assertEqual(self.h.camera.calls, [])
        self.h.advance(1.0)
        due = self.service.tick(self.queue_id, now=self.h.clock.now())
        self.assertTrue(due.capture_due)
        self.h.bridge.run(queue_id=self.queue_id, capture_due=due.capture_due,
                          job_id="job-repeat-eight")
        self.assertIs(self.service.begin(self.queue_id, now=self.h.clock.now()), first)
        self.assertFalse(self.service.tick(self.queue_id, now=self.h.clock.now()).capture_due)
        self.assertEqual(self.h.camera.calls, [self.h.consent_id])
        self.assertEqual(len(self.h.photo_store.records()), 1)


class AsyncCaptureBridgeTest(unittest.TestCase):
    def test_capture_releases_slot_before_background_processing_finishes(self):
        store = _AsyncStore()
        harness = _Harness(photo_store=store)
        self.addCleanup(harness.close)
        store.queue = harness.queue
        queue_id = harness.bind_queue()

        result = harness.bridge.run(queue_id=queue_id, capture_due=True, job_id=queue_id)

        self.assertEqual(result.queue_status, "processing")
        self.assertIsNone(result.photo)
        self.assertEqual(harness.status(), "processing")
        self.assertEqual(len(store.calls), 1)
        self.assertEqual(len(harness.camera.calls), 1)

        store.finish(queue_id)
        self.assertEqual(harness.status(), "ready_for_pickup")


class CountdownCountsThreeSecondsTest(unittest.TestCase):
    """假时钟 3 秒倒计时：恰好一次授权拍摄。"""

    def setUp(self):
        self.h = _Harness()
        self.queue_id = self.h.bind_queue()
        self.service = self.h.make_countdown()

    def tearDown(self):
        self.h.close()

    def test_counts_down_three_seconds_then_single_authorized_capture(self):
        state = self.service.begin(self.queue_id, now=self.h.clock.now())
        self.assertEqual(state["phase"], "countdown")
        self.assertEqual(state["countdown_remaining"], 3.0)

        first = self.service.tick(self.queue_id, now=self.h.clock.now())
        self.assertEqual(first.phase, "countdown")
        self.assertFalse(first.capture_due)
        self.assertEqual(first.remaining_seconds, 3.0)

        self.h.advance(2.0)
        second = self.service.tick(self.queue_id, now=self.h.clock.now())
        self.assertFalse(second.capture_due)
        self.assertEqual(second.remaining_seconds, 1.0)

        self.h.advance(1.0)  # 正好到 3.0 秒：必须归零
        due = self.service.tick(self.queue_id, now=self.h.clock.now())
        self.assertTrue(due.capture_due)
        self.assertEqual(due.phase, "capturing")
        self.assertEqual(due.remaining_seconds, 0.0)
        self.assertEqual(due.reason_code, "capture_due")
        self.assertEqual(self.camera_calls(), 0, "倒计时期间不得按快门")

        # 第三秒之后才允许真实拍摄：由桥执行，且授权同意正确。
        result = self.h.bridge.run(
            queue_id=self.queue_id,
            capture_consent_id=self.h.consent_id,
            job_id="job-t15-1",
            capture_due=True,
        )
        self.assertEqual(self.camera_calls(), 1)
        self.assertEqual(self.h.camera.calls, [self.h.consent_id])
        self.assertEqual(result.queue_status, "ready_for_pickup")
        self.assertIsNotNone(result.photo)
        self.assertTrue(is_valid_photo_token(result.photo.photo_token))
        self.assertEqual(result.photo.variant, "L0")
        self.assertEqual(self.h.status(), "ready_for_pickup")
        self.assertEqual(len(self.h.photo_store.records()), 1)

    def camera_calls(self) -> int:
        return len(self.h.camera.calls)


class CancelConditionsTest(unittest.TestCase):
    """四个取消条件任一条成立：取消、快门 0 次。"""

    def setUp(self):
        self.h = _Harness()
        self.queue_id = self.h.bind_queue()
        self.camera_ok = True

    def tearDown(self):
        self.h.close()

    def test_camera_probe_invalid_cancels_without_shutter(self):
        service = self.h.make_countdown(camera_probe=lambda now: self.camera_ok)
        service.begin(self.queue_id, now=self.h.clock.now())
        self.camera_ok = False

        self.h.advance(3.0)
        tick = service.tick(self.queue_id, now=self.h.clock.now())
        self.assertFalse(tick.capture_due)
        self.assertEqual(tick.phase, "cancelled")
        self.assertEqual(tick.reason_code, "countdown_camera_invalid")
        self.assertEqual(service.status(self.queue_id)["phase"], "cancelled")
        self.assertEqual(self.h.camera.calls, [])
        self.assertEqual(self.h.photo_store.records(), [])
        self.assertEqual(self.h.status(), "ready")  # 队列仍可重新开始（未拍摄）

    def test_consent_revoked_mid_countdown_cancels_without_shutter(self):
        service = self.h.make_countdown()
        service.begin(self.queue_id, now=self.h.clock.now())
        self.h.advance(2.0)
        pending = service.tick(self.queue_id, now=self.h.clock.now())
        self.assertEqual(pending.remaining_seconds, 1.0)

        self.assertTrue(self.h.consent.revoke(self.h.consent_id))
        self.h.advance(1.0)
        tick = service.tick(self.queue_id, now=self.h.clock.now())
        self.assertFalse(tick.capture_due)
        self.assertEqual(tick.phase, "cancelled")
        self.assertEqual(tick.reason_code, "countdown_consent_revoked")
        self.assertEqual(self.h.camera.calls, [])

    def test_explicit_cancel_stops_countdown(self):
        service = self.h.make_countdown()
        service.begin(self.queue_id, now=self.h.clock.now())
        tick = service.cancel(self.queue_id, reason="操作员取消", now=self.h.clock.now())
        self.assertEqual(tick.phase, "cancelled")
        self.assertEqual(tick.reason_code, "countdown_session_cancelled")
        self.assertFalse(tick.capture_due)

        self.h.advance(3.0)
        after = service.tick(self.queue_id, now=self.h.clock.now())
        self.assertFalse(after.capture_due)
        self.assertEqual(after.phase, "cancelled")
        self.assertEqual(self.h.camera.calls, [])

    def test_session_cancelled_via_queue_cancels_without_shutter(self):
        service = self.h.make_countdown()
        service.begin(self.queue_id, now=self.h.clock.now())
        self.h.queue.mark_cancelled(self.queue_id, reason="参会者离场")

        self.h.advance(1.0)
        tick = service.tick(self.queue_id, now=self.h.clock.now())
        self.assertFalse(tick.capture_due)
        self.assertEqual(tick.phase, "cancelled")
        self.assertEqual(tick.reason_code, "countdown_session_cancelled")
        self.assertEqual(self.h.camera.calls, [])

    def test_person_condition_invalid_cancels_without_shutter(self):
        person_count = {"value": 1}
        service = self.h.make_countdown(person_probe=lambda now: person_count["value"] == 1)
        service.begin(self.queue_id, now=self.h.clock.now())

        self.h.advance(1.0)
        person_count["value"] = 2  # 多人：人数条件失效
        tick = service.tick(self.queue_id, now=self.h.clock.now())
        self.assertFalse(tick.capture_due)
        self.assertEqual(tick.phase, "cancelled")
        self.assertEqual(tick.reason_code, "countdown_person_condition_invalid")
        self.assertEqual(self.h.camera.calls, [])

    def test_camera_invalid_at_begin_never_starts_countdown(self):
        service = self.h.make_countdown(camera_probe=lambda now: False)
        state = service.begin(self.queue_id, now=self.h.clock.now())
        self.assertEqual(state["phase"], "cancelled")
        self.assertEqual(state["cancel_reason_code"], "countdown_camera_invalid")
        self.assertEqual(self.h.camera.calls, [])


class FailureIssuesNoTokenTest(unittest.TestCase):
    """拍摄/提交失败：作业 failed、无成功图片、无令牌。"""

    def setUp(self):
        self.h = _Harness()
        self.queue_id = self.h.bind_queue()
        self.service = self.h.make_countdown()

    def tearDown(self):
        self.h.close()

    def _reach_due(self):
        self.service.begin(self.queue_id, now=self.h.clock.now())
        self.h.advance(3.0)
        tick = self.service.tick(self.queue_id, now=self.h.clock.now())
        self.assertTrue(tick.capture_due)
        return tick

    def test_camera_failure_marks_failed_without_photo_or_token(self):
        self._reach_due()
        self.h.camera.script_failure("脚本化故障：相机不可用", count=1)

        with self.assertRaises(CaptureFailedError) as ctx:
            self.h.bridge.run(queue_id=self.queue_id, capture_due=True, job_id="job-fail-1")
        self.assertEqual(ctx.exception.code, errors.CAPTURE_FAILED)
        self.assertEqual(self.h.status(), "failed")
        self.assertEqual(self.h.photo_store.records(), [])
        self.assertEqual(len(self.h.camera.calls), 1)  # 快门尝试过一次，但没有产出
        self.assertEqual(len(self.h.photo_store.records()), 0)  # 无成功图片
        self.assertEqual(self.h.photo_store.records(), [])  # 无取图令牌（令牌只随记录产生）
        # 队列条目上不得有任何取图令牌字段
        entry = self.h.queue.get(self.queue_id)
        self.assertFalse(hasattr(entry, "photo_token"))

    def test_commit_failure_marks_failed_without_photo_or_token(self):
        failing = _FailingStore("脚本化故障：磁盘写满")
        h = _Harness(photo_store=failing)
        try:
            queue_id = h.bind_queue()
            service = h.make_countdown()
            service.begin(queue_id, now=h.clock.now())
            h.advance(3.0)
            self.assertTrue(service.tick(queue_id, now=h.clock.now()).capture_due)

            with self.assertRaises(CaptureFailedError):
                h.bridge.run(queue_id=queue_id, capture_due=True, job_id="job-fail-2")
            self.assertEqual(failing.attempts, 1)
            status = str(h.queue.get(queue_id).status.value)
            self.assertEqual(status, "failed")
            self.assertEqual(len(h.camera.calls), 1)
        finally:
            h.close()

    def test_bridge_refuses_to_shoot_without_due_decision(self):
        with self.assertRaises(CaptureFailedError):
            self.h.bridge.run(queue_id=self.queue_id, capture_due=False)
        self.assertEqual(self.h.camera.calls, [])
        self.assertEqual(self.h.status(), "ready")
        self.assertEqual(self.h.photo_store.records(), [])


class NoRestartNoDuplicateTest(unittest.TestCase):
    """重复 begin/tick 不重启倒计时、不重复拍摄；完成后不再触发。"""

    def setUp(self):
        self.h = _Harness()
        self.queue_id = self.h.bind_queue()
        self.service = self.h.make_countdown()

    def tearDown(self):
        self.h.close()

    def test_repeated_begin_does_not_restart_countdown(self):
        first = self.service.begin(self.queue_id, now=self.h.clock.now())
        self.h.advance(1.5)
        second = self.service.begin(self.queue_id, now=self.h.clock.now())
        self.assertIs(second, first, "重复 begin 必须原样返回在跑的状态对象")
        self.assertEqual(second["phase"], "countdown")

        tick = self.service.tick(self.queue_id, now=self.h.clock.now())
        self.assertEqual(tick.remaining_seconds, 1.5)  # 没有被重置回 3.0

        self.h.advance(1.5)  # 距首次 begin 正好 3.0 秒
        due = self.service.tick(self.queue_id, now=self.h.clock.now())
        self.assertTrue(due.capture_due)

        self.h.bridge.run(queue_id=self.queue_id, capture_due=True, job_id="job-t15-2")
        self.assertEqual(len(self.h.camera.calls), 1)

    def test_no_second_capture_after_completion(self):
        self.service.begin(self.queue_id, now=self.h.clock.now())
        self.h.advance(3.0)
        self.assertTrue(self.service.tick(self.queue_id, now=self.h.clock.now()).capture_due)
        first = self.h.bridge.run(queue_id=self.queue_id, capture_due=True, job_id="job-t15-3")
        self.assertEqual(first.queue_status, "ready_for_pickup")

        again = self.service.tick(self.queue_id, now=self.h.clock.now())
        self.assertFalse(again.capture_due)
        self.assertEqual(again.phase, "capturing")
        self.assertEqual(again.reason_code, "countdown_noop")

        duplicate = self.h.bridge.run(queue_id=self.queue_id, capture_due=True, job_id="job-t15-3")
        self.assertTrue(duplicate.duplicate)
        self.assertIsNone(duplicate.photo)
        self.assertEqual(len(self.h.camera.calls), 1, "已完成后不得重复拍摄")
        self.assertEqual(len(self.h.photo_store.records()), 1)


class CancelledAtBeginReportsRealReasonTest(unittest.TestCase):
    """begin() 当场取消的会话，后续 tick 必须回传**真实取消原因**。

    为什么单列一条：`begin()` 自己也会评估四个探针 —— "开始那一刻就不满足条件"
    （最典型是镜头前没人）会立刻把会话置为 cancelled。此时调用方第一次 tick 走的是
    "phase 已不是 countdown" 的分支；如果那里只回一句 countdown_noop，真实的
    countdown_person_condition_invalid 就被吞掉了，现场日志里只剩一句无意义的
    noop，没人能判断为什么没拍。**实测踩过**：真机上倒计时 3 秒后取消，日志写的是
    "countdown_noop · 会话已离开倒计时"，排查了很久才定位到是"镜头前没人"。
    """

    def setUp(self):
        self.h = _Harness()
        self.queue_id = self.h.bind_queue()

    def tearDown(self):
        self.h.close()

    def test_begin_cancel_then_tick_reports_person_reason(self):
        from app.auto_flow.countdown_service import CancelDecision

        # 注入探针的签名必须是 (now) —— 见 countdown_service._wrap_probe：
        # 它只接受这一种写法，签名不符会直接抛 CountdownCanceledError。
        service = self.h.make_countdown(
            person_probe=lambda now: CancelDecision(
                canceled=True,
                reason_code="countdown_person_condition_invalid",
                reason="镜头前没有人，取消拍摄。",
            )
        )
        state = service.begin(self.queue_id, now=self.h.clock.now())
        self.assertEqual(state["phase"], "cancelled", "begin 就该当场取消")
        self.assertEqual(state["cancel_reason_code"], "countdown_person_condition_invalid")

        tick = service.tick(self.queue_id, now=self.h.clock.now())
        self.assertEqual(tick.phase, "cancelled")
        self.assertFalse(tick.capture_due)
        self.assertEqual(
            tick.reason_code, "countdown_person_condition_invalid",
            "必须回传真实原因，不能退化成 countdown_noop",
        )
        self.assertIn("没有人", tick.reason or "")
        self.assertEqual(self.h.camera.calls, [], "取消后快门必须 0 次")

    def test_running_countdown_still_reports_pending(self):
        """对照组：没被取消的会话照常报 countdown_pending（别把这条改坏）。"""
        service = self.h.make_countdown()
        service.begin(self.queue_id, now=self.h.clock.now())
        self.h.advance(1.0)
        tick = service.tick(self.queue_id, now=self.h.clock.now())
        self.assertEqual(tick.phase, "countdown")
        self.assertEqual(tick.reason_code, "countdown_pending")
        self.assertFalse(tick.capture_due)


class StatusProjectionTest(unittest.TestCase):
    """状态可查询：phase / remaining 可直接进公共 capture 投影。"""

    def setUp(self):
        self.h = _Harness()
        self.queue_id = self.h.bind_queue()
        self.service = self.h.make_countdown()

    def tearDown(self):
        self.h.close()

    def test_status_exposes_phase_and_remaining_for_projection(self):
        self.service.begin(self.queue_id, now=self.h.clock.now())
        self.h.advance(2.0)
        tick = self.service.tick(self.queue_id, now=self.h.clock.now())
        self.h.advance(1.0)

        state = self.service.status(self.queue_id)
        self.assertEqual(state["phase"], "countdown")
        self.assertEqual(state["countdown_remaining"], 1.0)
        projected = public_capture_fields(state)
        self.assertEqual(projected, {"phase": "countdown", "countdown_remaining": 1})

        due = self.service.tick(self.queue_id, now=self.h.clock.now())
        self.assertTrue(due.capture_due)
        self.assertEqual(due.reason_code, "capture_due")
        self.assertEqual(
            public_capture_fields(self.service.status(self.queue_id)),
            {"phase": "capturing", "countdown_remaining": None},
        )
        self.assertEqual(tick.reason_code, "countdown_pending")


class ModuleBoundaryTest(unittest.TestCase):
    """纯逻辑边界：不 import 硬件/网络库，不引入 HTTP。"""

    def test_modules_are_pure_logic(self):
        import ast
        import io as _io
        import os as _os

        root = Path(__file__).resolve().parents[2]
        for rel in ("app/auto_flow/countdown_service.py", "app/auto_flow/capture_bridge.py"):
            with _io.open(_os.path.join(root, rel), encoding="utf-8") as handle:
                source = handle.read()
            tree = ast.parse(source)
            names: set[str] = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    names.update(alias.name.split(".")[0] for alias in node.names)
                elif isinstance(node, ast.ImportFrom) and node.module:
                    names.add(node.module.split(".")[0])
            forbidden = {"cv2", "onnxruntime", "spacemit_ort", "http", "socket", "requests", "urllib"}
            self.assertEqual(names & forbidden, set(), f"{rel} 引入了禁用模块")
            self.assertTrue(
                names <= {"__future__", "threading", "collections", "dataclasses", "datetime", "typing", "app"},
                f"{rel} 引入了未预期模块：{sorted(names)}",
            )


if __name__ == "__main__":
    unittest.main(verbosity=2)
