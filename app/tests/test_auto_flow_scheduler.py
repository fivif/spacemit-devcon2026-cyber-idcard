# -*- coding: utf-8 -*-
"""T-13 队列调度测试（QueueScheduler.tick / capture_completed）。

运行（项目根目录）：
    python3 -m unittest app.tests.test_auto_flow_scheduler -v

约定：
- 全部离线、无 sleep：时间推进一律用 T-14 的 FakeClock，存储用 MemoryQueueStore；
- 覆盖验收：queued→called→ready→expired 可观察转换；同一时刻最多一个拍摄位占用；
  相机故障暂停叫号 + 故障期间不弃号（期限冻结）+ 恢复后继续叫号且不重复叫；
  超时 / capture_completed(成功) / capture_completed(失败) 三条拍摄位释放路径。
"""

from __future__ import annotations

import unittest

from app.auto_flow import errors
from app.auto_flow.models import QueueStatus, QueueSubmission, occupies_capture_slot
from app.auto_flow.queue_scheduler import QueueScheduler
from app.auto_flow.queue_service import QueueService
from app.auto_flow.queue_store import MemoryQueueStore
from app.tests.auto_flow_fixtures import FakeClock

EVENT_YEAR = 2026


class CameraSwitch:
    """可脚本化的相机探针：ok=True 表示画面新鲜、可拍。"""

    def __init__(self, ok: bool = True) -> None:
        self.ok = ok
        self.probe_calls = 0

    def __call__(self) -> bool:
        self.probe_calls += 1
        return self.ok


def _boom():
    raise RuntimeError("probe boom")


class SchedulerTestCase(unittest.TestCase):
    """公共夹具：FakeClock + MemoryQueueStore + 默认 120 秒开始期限。"""

    def setUp(self) -> None:
        self.clock = FakeClock()
        self.service = self.make_service()
        self.camera = CameraSwitch(True)
        self.scheduler = QueueScheduler(self.service, camera_ok=self.camera)

    def make_service(self) -> QueueService:
        return QueueService(
            MemoryQueueStore(),
            max_active=8,
            start_timeout_seconds=120,
            idempotency_ttl_seconds=86400,
            optional_fields_allowlist=("note",),
            clock=self.clock,
            event_year=EVENT_YEAR,
        )

    # ---- 小工具 ----------------------------------------------------------

    def submit(self, service: QueueService, request_id: str) -> str:
        entry = service.submit(
            QueueSubmission(
                request_id=request_id,
                style_id="style-a",
                capture_consent_id=f"consent-{request_id}",
            )
        )
        return entry.queue_id

    def tick(self, scheduler: QueueScheduler | None = None) -> dict:
        return (scheduler or self.scheduler).tick(self.clock.now())

    def advance(self, seconds: float, scheduler: QueueScheduler | None = None) -> dict:
        self.clock.advance(seconds)
        return self.tick(scheduler)

    def snapshot(self, service: QueueService | None = None) -> dict:
        return (service or self.service).snapshot()

    def status_of(self, queue_id: str, service: QueueService | None = None) -> QueueStatus:
        entry = (service or self.service).get(queue_id)
        assert entry is not None, f"queue_id 不存在：{queue_id}"
        return entry.status

    def assert_single_slot(self, service: QueueService | None = None) -> list[str]:
        """不变量：任意时刻最多一个条目占用拍摄位，且占用记录与状态一致。"""
        snap = self.snapshot(service)
        occupants = [
            entry["queue_id"]
            for entry in snap["entries"]
            if occupies_capture_slot(entry["status"])
        ]
        self.assertLessEqual(len(occupants), 1, f"出现多个拍摄位占用：{occupants}")
        occupancy = snap["occupancy"]
        if occupancy is not None:
            self.assertIn(occupancy["queue_id"], occupants)
        return occupants

    # ---- 1. 基本叫号与期限 ------------------------------------------------

    def test_tick_calls_first_queued_with_120s_deadline_and_no_repeat(self) -> None:
        qa = self.submit(self.service, "r-1")
        qb = self.submit(self.service, "r-2")
        self.assertEqual(self.status_of(qa), QueueStatus.QUEUED)

        result = self.tick()
        # tick 返回契约（T-15/T-16 接线依赖的键）
        for key in (
            "now", "camera_ok", "camera_reason", "frozen", "frozen_since",
            "frozen_seconds_total", "just_frozen", "just_recovered",
            "auto_trigger_eligible", "expire_checked_at", "expired", "called",
            "active_call", "occupancy",
        ):
            self.assertIn(key, result)
        self.assertTrue(result["camera_ok"])
        self.assertFalse(result["frozen"])
        self.assertEqual(result["expired"], [])
        self.assertIsNotNone(result["called"])
        self.assertEqual(result["called"]["queue_id"], qa)
        self.assertEqual(result["called"]["called_at"], "2026-09-30T00:00:00Z")
        self.assertEqual(result["called"]["start_deadline"], "2026-09-30T00:02:00Z")
        self.assertTrue(result["called"]["display_number"].startswith("SM-2026-"))

        entry = self.service.get(qa)
        self.assertIs(entry.status, QueueStatus.CALLED)
        self.assertEqual(entry.start_deadline, "2026-09-30T00:02:00Z")

        # 再 tick 不重复叫号：仍是同一条 called、一个占用
        again = self.tick()
        self.assertIsNone(again["called"])
        self.assertEqual(again["active_call"]["queue_id"], qa)
        self.assertEqual(self.status_of(qb), QueueStatus.QUEUED)
        self.assertEqual(self.assert_single_slot(), [qa])

    def test_start_timeout_is_configurable(self) -> None:
        service = QueueService(
            MemoryQueueStore(),
            max_active=4,
            start_timeout_seconds=45,
            optional_fields_allowlist=(),
            clock=self.clock,
            event_year=EVENT_YEAR,
        )
        qa = self.submit(service, "r-a")
        scheduler = QueueScheduler(service, camera_ok=lambda: True)

        result = self.tick(scheduler)
        self.assertEqual(result["called"]["start_deadline"], "2026-09-30T00:00:45Z")

        self.clock.advance(44)
        result = self.tick(scheduler)
        self.assertEqual(result["expired"], [])
        self.assertEqual(self.assert_single_slot(service), [qa])

        self.clock.advance(1)
        result = self.tick(scheduler)
        self.assertEqual(result["expired"], [qa])
        self.assertIsNone(result["occupancy"])

    def test_queued_called_ready_processing_expired_transitions(self) -> None:
        qa = self.submit(self.service, "r-a")
        qb = self.submit(self.service, "r-b")
        statuses_seen = {self.status_of(qa), self.status_of(qb)}  # queued

        self.tick()  # a: queued -> called
        statuses_seen.add(self.status_of(qa))

        self.service.start(qa)  # 手动开始路径：called -> ready
        statuses_seen.add(self.status_of(qa))
        self.assertIs(self.status_of(qa), QueueStatus.READY)

        self.tick()  # ready 仍占拍摄位，b 不会被叫
        self.assertEqual(self.status_of(qb), QueueStatus.QUEUED)

        out = self.scheduler.capture_completed(qa, source_saved=True)  # -> processing（释放）
        statuses_seen.add(self.status_of(qa))
        self.assertEqual(out["path"], "success")
        self.assertEqual(out["status"], "processing")
        self.assertTrue(out["released"])
        self.assertIsNone(self.snapshot()["occupancy"])

        self.tick()  # b: queued -> called
        statuses_seen.add(self.status_of(qb))

        result = self.advance(120)  # b: called -> expired（>= 期限口径）
        statuses_seen.add(self.status_of(qb))
        self.assertEqual(result["expired"], [qb])
        self.assertIsNone(result["called"])
        self.assertIsNone(self.snapshot()["occupancy"])

        self.assertTrue(
            {
                QueueStatus.QUEUED,
                QueueStatus.CALLED,
                QueueStatus.READY,
                QueueStatus.PROCESSING,
                QueueStatus.EXPIRED,
            }
            <= statuses_seen
        )

    # ---- 2. 独占：同一时刻最多一个占用 ------------------------------------

    def test_at_most_one_capture_slot_across_repeated_ticks(self) -> None:
        queue_ids = [self.submit(self.service, f"r-{i}") for i in range(3)]
        first = self.tick()
        self.assertEqual(first["called"]["queue_id"], queue_ids[0])
        for _ in range(5):
            result = self.tick()
            self.assertIsNone(result["called"])  # 已有占用，不再叫号
            self.assertEqual(self.assert_single_slot(), [queue_ids[0]])
        counts = self.snapshot()["counts"]
        self.assertEqual(counts["called"], 1)
        self.assertEqual(counts["queued"], 2)

        # 超时弃号释放后，下一位在同一个 tick 被叫到；仍是一个占用
        result = self.advance(120)
        self.assertEqual(result["expired"], [queue_ids[0]])
        self.assertEqual(result["called"]["queue_id"], queue_ids[1])
        self.assertEqual(self.assert_single_slot(), [queue_ids[1]])

    # ---- 3. 相机故障：暂停叫号 + 期限冻结 + 恢复不重复叫 ------------------

    def test_camera_freeze_pauses_calling_and_freezes_deadline(self) -> None:
        qa = self.submit(self.service, "r-a")
        qb = self.submit(self.service, "r-b")
        self.tick()  # 00:00:00 a 叫号，原始期限 00:02:00

        self.camera.ok = False
        r = self.advance(20)  # 00:00:20 首次观测无帧 -> 冻结起点
        self.assertTrue(r["frozen"])
        self.assertTrue(r["just_frozen"])
        self.assertIsNone(r["called"])
        self.assertEqual(r["expired"], [])
        self.assertEqual(r["frozen_since"], "2026-09-30T00:00:20Z")

        self.advance(20)  # 00:00:40
        r = self.advance(60)  # 00:01:40 -> 顺延 80 秒
        self.assertEqual(r["active_call"]["frozen_shift_seconds"], 80.0)
        self.assertEqual(
            r["active_call"]["effective_start_deadline"], "2026-09-30T00:03:20Z"
        )

        self.advance(299)  # 00:06:39
        r = self.advance(1)  # 00:06:40：已远超原始期限，仍不弃号
        self.assertEqual(r["frozen_seconds_total"], 380.0)
        self.assertEqual(r["expired"], [])
        self.assertIs(self.status_of(qa), QueueStatus.CALLED)
        self.assertEqual(self.service.get(qa).start_deadline, "2026-09-30T00:02:00Z")
        self.assertIs(self.status_of(qb), QueueStatus.QUEUED)

        self.camera.ok = True
        r = self.tick()  # 00:06:40 恢复：继续叫号但不重复叫
        self.assertTrue(r["camera_ok"])
        self.assertTrue(r["just_recovered"])
        self.assertFalse(r["frozen"])
        self.assertIsNone(r["called"])
        self.assertEqual(r["active_call"]["queue_id"], qa)
        self.assertEqual(
            r["active_call"]["effective_start_deadline"], "2026-09-30T00:08:20Z"
        )
        self.assertEqual(self.assert_single_slot(), [qa])

        r = self.advance(100)  # 00:08:20：a 的非冻结等待满 120 秒 -> 弃号，b 补位
        self.assertEqual(r["expired"], [qa])
        self.assertEqual(r["called"]["queue_id"], qb)
        self.assertEqual(r["called"]["start_deadline"], "2026-09-30T00:10:20Z")
        self.assertIs(self.status_of(qa), QueueStatus.EXPIRED)
        self.assertEqual(self.assert_single_slot(), [qb])

    def test_camera_down_blocks_calling_and_recovery_resumes(self) -> None:
        self.camera.ok = False
        qa = self.submit(self.service, "r-a")
        r = self.tick()
        self.assertTrue(r["frozen"])
        self.assertIsNone(r["called"])
        self.assertIs(self.status_of(qa), QueueStatus.QUEUED)

        self.camera.ok = True
        r = self.tick()
        self.assertTrue(r["just_recovered"])
        self.assertEqual(r["called"]["queue_id"], qa)

    def test_waiting_queue_can_be_called_while_camera_gate_is_closed(self) -> None:
        self.camera.ok = False
        qa = self.submit(self.service, "r-a")
        scheduler = QueueScheduler(
            self.service,
            camera_ok=self.camera,
            allow_queue_without_camera=True,
        )

        result = self.tick(scheduler)

        self.assertFalse(result["camera_ok"])
        self.assertEqual(result["called"]["queue_id"], qa)
        self.assertIs(self.status_of(qa), QueueStatus.CALLED)
        self.assertEqual(self.assert_single_slot(), [qa])

    def test_closed_camera_does_not_freeze_called_timeout(self) -> None:
        self.camera.ok = False
        qa = self.submit(self.service, "r-a")
        qb = self.submit(self.service, "r-b")
        scheduler = QueueScheduler(
            self.service,
            camera_ok=self.camera,
            allow_queue_without_camera=True,
        )

        first = self.tick(scheduler)
        self.assertEqual(first["called"]["queue_id"], qa)
        self.assertFalse(first["frozen"])

        result = self.advance(120, scheduler)

        self.assertEqual(result["expired"], [qa])
        self.assertEqual(result["called"]["queue_id"], qb)
        self.assertFalse(result["frozen"])
        self.assertEqual(result["active_call"]["queue_id"], qb)
        self.assertEqual(self.status_of(qa), QueueStatus.EXPIRED)
        self.assertEqual(self.assert_single_slot(), [qb])

    def test_multiple_freeze_episodes_accumulate_shift(self) -> None:
        qa = self.submit(self.service, "r-a")
        qb = self.submit(self.service, "r-b")
        self.tick()  # 00:00:00 a 叫号，原始期限 00:02:00

        self.camera.ok = False
        self.advance(10)  # 00:00:10 第一段冻结开始
        self.camera.ok = True
        r = self.advance(30)  # 00:00:40 恢复，第一段顺延 30 秒
        self.assertEqual(r["active_call"]["frozen_shift_seconds"], 30.0)

        self.camera.ok = False
        self.advance(10)  # 00:00:50 第二段冻结开始
        self.camera.ok = True
        r = self.advance(40)  # 00:01:30 恢复，累计顺延 70 秒
        self.assertEqual(r["frozen_seconds_total"], 70.0)
        self.assertEqual(r["active_call"]["frozen_shift_seconds"], 70.0)
        self.assertEqual(
            r["active_call"]["effective_start_deadline"], "2026-09-30T00:03:10Z"
        )

        r = self.advance(99)  # 00:03:09：逻辑时刻 119 秒，仍在期限内
        self.assertEqual(r["expired"], [])
        self.assertIs(self.status_of(qa), QueueStatus.CALLED)

        r = self.advance(1)  # 00:03:10：逻辑时刻满 120 秒（= 有效期限）-> 弃号，b 补位
        self.assertEqual(r["expired"], [qa])
        self.assertEqual(r["called"]["queue_id"], qb)
        self.assertEqual(self.assert_single_slot(), [qb])

    # ---- 4. 释放路径：超时 / 成功 / 失败 ---------------------------------

    def test_expire_releases_slot_and_next_is_called_in_same_tick(self) -> None:
        qa = self.submit(self.service, "r-a")
        qb = self.submit(self.service, "r-b")
        self.tick()
        result = self.advance(120)
        self.assertEqual(result["expired"], [qa])
        self.assertIs(self.status_of(qa), QueueStatus.EXPIRED)
        self.assertEqual(result["called"]["queue_id"], qb)
        self.assertEqual(self.assert_single_slot(), [qb])

    def test_capture_completed_releases_and_aigc_print_do_not_occupy(self) -> None:
        qa = self.submit(self.service, "r-a")
        qb = self.submit(self.service, "r-b")
        self.tick()
        self.service.start(qa)
        self.service.mark_capturing(qa)
        self.assertEqual(self.assert_single_slot(), [qa])

        out = self.scheduler.capture_completed(qa, source_saved=True)
        self.assertEqual(
            out,
            {
                "queue_id": qa,
                "path": "success",
                "status": "processing",
                "released": True,
                "source_saved": True,
                "reason": None,
            },
        )
        self.assertIsNone(self.snapshot()["occupancy"])
        self.assertEqual(self.assert_single_slot(), [])

        self.tick()  # processing（AIGC 阶段）不占位，下一位可被叫
        self.assertEqual(self.status_of(qb), QueueStatus.CALLED)

        self.service.mark_ready_for_pickup(qa)  # 打印取图阶段同样不占位
        self.assertIs(self.status_of(qa), QueueStatus.READY_FOR_PICKUP)
        self.assertEqual(self.assert_single_slot(), [qb])

    def test_capture_failure_releases_slot_as_failed(self) -> None:
        qa = self.submit(self.service, "r-a")
        qb = self.submit(self.service, "r-b")
        self.tick()
        self.service.start(qa)
        self.service.mark_capturing(qa)

        out = self.scheduler.capture_completed(qa, source_saved=False)
        self.assertEqual(out["path"], "failure")
        self.assertEqual(out["status"], "failed")
        self.assertTrue(out["released"])
        self.assertIs(self.status_of(qa), QueueStatus.FAILED)
        self.assertIsNone(self.snapshot()["occupancy"])
        self.assertEqual(self.assert_single_slot(), [])
        notes = self.snapshot()["notes"]
        self.assertTrue(any("原图未落盘" in (note.get("reason") or "") for note in notes))

        self.tick()  # failed 释放后队列继续
        self.assertEqual(self.status_of(qb), QueueStatus.CALLED)

    # ---- 5. 手势门只影响自动触发资格 --------------------------------------

    def test_gesture_gate_blocks_auto_trigger_only_not_calling(self) -> None:
        qa = self.submit(self.service, "r-a")
        r = self.tick()
        self.assertFalse(r["auto_trigger_eligible"])  # 默认手势禁用 -> 自动触发 no-go
        self.assertEqual(r["called"]["queue_id"], qa)  # 叫号不受手势影响（手动路径保留）
        self.assertIs(self.status_of(qa), QueueStatus.CALLED)

        self.camera.ok = False
        with_gesture = QueueScheduler(
            self.service, camera_ok=self.camera, gesture_enabled=True
        )
        r = self.tick(with_gesture)
        self.assertFalse(r["auto_trigger_eligible"])  # 无帧时自动触发同样不可用

        service = self.make_service()
        self.submit(service, "r-g")
        enabled = QueueScheduler(
            service, camera_ok=lambda: True, gesture_enabled=True
        )
        self.assertTrue(self.tick(enabled)["auto_trigger_eligible"])
        not_required = QueueScheduler(
            service, camera_ok=lambda: True, gesture_enabled=False,
            gesture_required_for_auto_trigger=False,
        )
        self.assertTrue(self.tick(not_required)["auto_trigger_eligible"])

    # ---- 6. 相机探针：缺省 / 异常 / 帧时间戳 -------------------------------

    def test_missing_or_failing_probe_pauses_calling_conservatively(self) -> None:
        qa = self.submit(self.service, "r-a")
        no_probe = QueueScheduler(self.service)
        r = self.tick(no_probe)
        self.assertFalse(r["camera_ok"])
        self.assertTrue(r["frozen"])
        self.assertIsNone(r["called"])
        self.assertIn("未接入相机探针", r["camera_reason"])
        self.assertIs(self.status_of(qa), QueueStatus.QUEUED)

        service = self.make_service()
        self.submit(service, "r-b")
        broken = QueueScheduler(service, camera_ok=_boom)
        r = self.tick(broken)
        self.assertFalse(r["camera_ok"])
        self.assertIn("探针异常", r["camera_reason"])

    def test_frame_timestamp_probe_uses_freshness(self) -> None:
        service = self.make_service()
        qa = self.submit(service, "r-a")
        last_frame = [self.clock.now().timestamp()]
        scheduler = QueueScheduler(
            service, camera_last_frame_at=lambda: last_frame[0], camera_timeout_seconds=2.0
        )

        r = self.tick(scheduler)
        self.assertTrue(r["camera_ok"])
        self.assertEqual(r["called"]["queue_id"], qa)

        self.clock.advance(1)  # 帧龄 1 秒 <= 2 秒阈值，仍可拍
        r = self.tick(scheduler)
        self.assertTrue(r["camera_ok"])
        self.assertFalse(r["frozen"])

        self.clock.advance(2)  # 帧龄 3 秒 > 2 秒阈值 -> 暂停叫号
        r = self.tick(scheduler)
        self.assertFalse(r["camera_ok"])
        self.assertTrue(r["frozen"])
        self.assertIn("没有新画面", r["camera_reason"])

        last_frame[0] = self.clock.now().timestamp()
        r = self.tick(scheduler)
        self.assertTrue(r["camera_ok"])
        self.assertTrue(r["just_recovered"])

        last_frame[0] = None  # 从未取到帧
        r = self.tick(scheduler)
        self.assertFalse(r["camera_ok"])
        self.assertIn("尚未收到任何画面", r["camera_reason"])

    # ---- 7. 构造与入参校验 ------------------------------------------------

    def test_constructor_and_argument_validation(self) -> None:
        with self.assertRaises(ValueError):
            QueueScheduler(
                self.service,
                camera_ok=lambda: True,
                camera_last_frame_at=lambda: 0.0,
            )
        with self.assertRaises(TypeError):
            QueueScheduler(self.service, camera_ok=123)  # type: ignore[arg-type]
        with self.assertRaises(ValueError):
            QueueScheduler(self.service, camera_ok=lambda: True, camera_timeout_seconds=0)
        with self.assertRaises(TypeError):
            QueueScheduler(self.service, camera_ok=lambda: True, gesture_enabled="yes")  # type: ignore[arg-type]
        with self.assertRaises(ValueError):
            self.scheduler.tick("now")  # type: ignore[arg-type]
        qa = self.submit(self.service, "r-a")
        with self.assertRaises(ValueError):
            self.scheduler.capture_completed(qa, source_saved="yes")  # type: ignore[arg-type]
        with self.assertRaises(errors.ApiError):
            self.scheduler.capture_completed("missing-queue", source_saved=True)


if __name__ == "__main__":
    unittest.main()
