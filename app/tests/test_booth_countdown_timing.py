from __future__ import annotations

import io
import os
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from PIL import Image

from app.auto_flow.consent_service import ConsentService
from app.auto_flow.queue_service import QueueService
from app.tests.test_auto_flow_runtime import booth_server
from app.tests.test_auto_flow_countdown import _Harness


class GesturePreviewSchedulingTest(unittest.TestCase):
    def test_legacy_mode_keeps_preview_detection(self):
        booth = SimpleNamespace(auto_flow=None)
        self.assertTrue(booth_server._gesture_preview_needed(booth))

    def test_queue_mode_only_detects_after_user_starts(self):
        queue = Mock()
        booth = SimpleNamespace(auto_flow=SimpleNamespace(queue=queue))
        for status, expected in (("queued", False), ("called", False),
                                 ("ready", True), ("capturing", False),
                                 ("processing", False)):
            with self.subTest(status=status):
                queue.snapshot.return_value = {"entries": [{"status": status}]}
                self.assertEqual(booth_server._gesture_preview_needed(booth), expected)

    def test_queue_read_failure_does_not_spin_inference(self):
        queue = Mock()
        queue.snapshot.side_effect = RuntimeError("store unavailable")
        booth = SimpleNamespace(auto_flow=SimpleNamespace(queue=queue))
        self.assertFalse(booth_server._gesture_preview_needed(booth))


class CountdownPersonEvidenceTest(unittest.TestCase):
    def setUp(self):
        self.booth = booth_server.Booth.__new__(booth_server.Booth)
        self.booth._countdown_person_lock = threading.Lock()
        self.check = {"queue_id": "session", "status": "pending", "boxes": None}
        self.booth._countdown_person_check = self.check
        self.booth.queue_jobs = {"session": SimpleNamespace(stage="countdown")}
        self.booth.last_gesture_obs = None

    def observe(self, boxes, moment):
        return self.booth._record_countdown_person_result(self.check, boxes, (480, 640), moment)

    def test_empty_and_single_person_results_complete_display_check(self):
        self.assertTrue(self.observe([], 0.0))
        self.assertEqual(self.check["status"], "done")
        self.assertEqual(self.booth.last_gesture_obs["person_count"], 0)
        self.assertTrue(self.observe([[0, 0, 200, 400]], 0.8))
        self.assertEqual(self.booth.last_gesture_obs["person_count"], 1)

    def test_model_error_is_unknown_and_does_not_replace_display(self):
        self.assertFalse(self.observe(None, 0.0))
        self.assertEqual(self.check["status"], "pending")
        self.assertIsNone(self.booth.last_gesture_obs)
        boxes = [[0, 0, 200, 400], [10, 10, 190, 390]]
        self.assertTrue(self.observe(boxes, 0.8))
        observation = dict(self.booth.last_gesture_obs)
        self.assertFalse(self.observe(None, 1.6))
        self.assertEqual(self.check["boxes"], boxes)
        self.assertEqual(self.booth.last_gesture_obs, observation)

    def test_zero_to_three_boxes_only_update_display(self):
        for boxes in ([], [[0, 0, 200, 400]],
                      [[0, 0, 200, 400], [10, 10, 190, 390]],
                      [[0, 0, 200, 400], [220, 0, 400, 400], [20, 20, 180, 380]]):
            with self.subTest(boxes=boxes):
                self.assertTrue(self.observe(boxes, 0.0))
                self.assertEqual(self.check["status"], "done")
                self.assertEqual(self.check["boxes"], boxes)
                self.assertEqual(self.booth.last_gesture_obs["persons"], boxes)
                self.assertEqual(self.booth.last_gesture_obs["person_count"], len(boxes))
                self.assertEqual(self.booth.last_gesture_obs["img_w"], 480)
                self.assertEqual(self.booth.last_gesture_obs["img_h"], 640)
                self.assertEqual(self.booth.queue_jobs["session"].stage, "countdown")

    def test_changing_box_counts_do_not_change_countdown_stage(self):
        for moment, boxes in ((0.0, []), (0.8, []), (1.6, [[0, 0, 1, 1], [2, 2, 3, 3]]), (2.4, [])):
            self.assertTrue(self.observe(boxes, moment))
        self.assertEqual(self.booth.queue_jobs["session"].stage, "countdown")
        self.assertEqual(self.booth.last_gesture_obs["person_count"], 0)

    def test_person_result_drops_stale_gesture_overlay(self):
        self.booth.last_gesture_obs = {
            "ok_boxes": [[1, 2, 3, 4]], "hand_boxes": [[2, 3, 4, 5]],
            "palm_boxes": [[3, 4, 5, 6]], "hand_points": [[4, 5]],
            "is_ok": True, "available": True,
        }
        self.assertTrue(self.observe([[0, 0, 200, 400]], 0.0))
        observation = self.booth.last_gesture_obs
        self.assertEqual(observation["persons"], [[0, 0, 200, 400]])
        self.assertEqual(observation["ok_boxes"], [])
        self.assertEqual(observation["hand_boxes"], [])
        self.assertEqual(observation["palm_boxes"], [])
        self.assertEqual(observation["hand_points"], [])
        self.assertFalse(observation["is_ok"])

    def test_worker_is_async_and_retries_unknown_results_at_most_four_times(self):
        raw = io.BytesIO()
        Image.new("RGB", (12, 18), "navy").save(raw, "JPEG")
        self.booth.preview_hub = SimpleNamespace(latest_jpeg=lambda: raw.getvalue())
        self.booth.auto_flow = SimpleNamespace(countdown=SimpleNamespace(countdown_seconds=8))
        multiple = [[0, 0, 8, 16], [1, 1, 7, 15], [2, 2, 6, 14]]
        for results in ([None] * 4, [None] * 3 + [multiple], [[]]):
            with self.subTest(results=results):
                self.booth._countdown_person_check = None
                self.booth.last_gesture_obs = None
                self.booth._person_boxes_for = Mock(side_effect=results)
                with patch.object(booth_server.threading, "Thread") as worker_thread, \
                        patch.object(booth_server.time, "sleep") as retry_sleep, \
                        patch.object(booth_server, "log"):
                    self.booth._start_countdown_person_check("session")
                    worker_thread.return_value.start.assert_called_once_with()
                    self.booth._person_boxes_for.assert_not_called()
                    self.booth._start_countdown_person_check("session")
                    worker_thread.assert_called_once()
                    worker_thread.call_args.kwargs["target"]()
                self.assertEqual(self.booth._person_boxes_for.call_count, len(results))
                self.assertEqual(retry_sleep.call_count, len(results) - 1)
                for retry in retry_sleep.call_args_list:
                    self.assertEqual(retry.args, (0.8,))
                self.assertEqual(self.booth.queue_jobs["session"].stage, "countdown")
                if results[-1] is None:
                    self.assertIsNone(self.booth.last_gesture_obs)
                else:
                    self.assertEqual(self.booth._countdown_person_check["status"], "done")
                    self.assertEqual(self.booth.last_gesture_obs["person_count"], len(results[-1]))

    def test_late_result_does_not_overwrite_capture_or_next_session(self):
        self.booth.queue_jobs["session"].stage = "capturing"
        self.assertTrue(self.observe([], 0.0))
        self.assertIsNone(self.booth.last_gesture_obs)
        self.booth.queue_jobs["session"].stage = "countdown"
        self.booth._countdown_person_check = {"queue_id": "next"}
        self.assertTrue(self.observe([], 0.0))
        self.assertIsNone(self.booth.last_gesture_obs)

    def test_confirmed_ok_does_not_wait_for_model_switch(self):
        raw = io.BytesIO()
        Image.new("RGB", (12, 18), "navy").save(raw, "JPEG")
        self.booth.gesture_port = SimpleNamespace(
            _engine="official_yolo",
            observe_visual=Mock(return_value=SimpleNamespace(gesture="ok", available=True, in_frame=True)))
        self.booth.preview_hub = SimpleNamespace(latest_jpeg=lambda: raw.getvalue())
        self.booth.infer = SimpleNamespace(switch_mode=Mock(side_effect=AssertionError("同步切换阻塞倒计时")))
        with patch.dict(os.environ, {"BOOTH_GESTURE_STABLE_FRAMES": "1"}), patch.object(booth_server, "log"):
            self.assertTrue(booth_server._gesture_gate_allows(self.booth, "session", {}, None))
        self.booth.infer.switch_mode.assert_not_called()

    def test_official_gate_reuses_fresh_background_observation(self):
        observe_visual = Mock(side_effect=AssertionError("不应重复推理"))
        self.booth.gesture_port = SimpleNamespace(
            _engine="official_yolo", observe_visual=observe_visual)
        self.booth.last_gesture_obs = {
            "at": time.time(), "is_ok": True, "available": True,
            "ok_boxes": [[1, 2, 3, 4]], "img_w": 480, "img_h": 270,
        }
        with patch.dict(os.environ, {"BOOTH_GESTURE_STABLE_FRAMES": "1"}), \
                patch.object(booth_server, "log"):
            self.assertTrue(booth_server._gesture_gate_allows(
                self.booth, "session", {}, None))
        observe_visual.assert_not_called()


class ProductionCountdownWiringTest(unittest.TestCase):
    def make_production_countdown(self):
        harness = _Harness()
        self.addCleanup(harness.close)
        booth = booth_server.Booth.__new__(booth_server.Booth)
        booth.store = SimpleNamespace(root=harness.root)
        booth.camera = SimpleNamespace(status=SimpleNamespace(real=True))
        booth.preview_hub = SimpleNamespace(stats=Mock(return_value={"last_frame_s": 0.05}))
        booth.style_catalog = {"styles": [{"id": "neon", "enabled": True}]}
        booth.queue_jobs = {}
        booth._countdown_person_lock = threading.Lock()
        booth._countdown_person_check = None
        booth.last_gesture_obs = None
        queue_init = QueueService.__init__
        consent_init = ConsentService.__init__

        def initialize_queue(service, *args, **kwargs):
            queue_init(service, *args, clock=harness.clock, **kwargs)

        def initialize_consent(service, *args, **kwargs):
            consent_init(service, *args, clock=harness.clock, **kwargs)

        with patch.dict(os.environ, {"BOOTH_QUEUE_ENABLED": "1",
                                     "BOOTH_PHOTO_RETENTION_DAYS": "30",
                                     "BOOTH_CAPTURE_COUNTDOWN_SECONDS": "8"}, clear=True), \
                patch.object(QueueService, "__init__", new=initialize_queue), \
                patch.object(ConsentService, "__init__", new=initialize_consent), \
                patch.object(booth_server, "_NoOpCameraAdapter", return_value=harness.camera), \
                patch.object(booth_server, "_BoothCapturePhotoStore", return_value=harness.photo_store), \
                patch.object(booth, "_start_auto_flow_tick_thread") as start_tick, \
                patch.object(booth_server, "log"):
            booth._init_auto_flow()
        start_tick.assert_called_once_with()
        self.assertTrue(booth.camera.status.real)
        self.assertEqual(booth.auto_flow.countdown.countdown_seconds, 8)
        self.assertTrue(booth.auto_flow.camera_probe())
        harness.queue = booth.auto_flow.queue
        harness.consent = booth.auto_flow.consent
        queue_id = harness.bind_queue()
        booth.queue_jobs[queue_id] = SimpleNamespace(stage="countdown")
        booth._countdown_person_check = {"queue_id": queue_id, "status": "pending"}
        return harness, booth, queue_id

    def test_real_camera_wiring_ignores_zero_single_overlapping_and_multiple_boxes(self):
        scenarios = {
            "no_person": [],
            "single_person": [[0, 0, 200, 400]],
            "overlapping": [[0, 0, 200, 400], [10, 10, 190, 390]],
            "two_people": [[0, 0, 200, 400], [220, 0, 400, 400]],
            "three_boxes": [[0, 0, 200, 400], [220, 0, 400, 400], [20, 20, 180, 380]],
        }
        for scenario, boxes in scenarios.items():
            with self.subTest(scenario=scenario):
                harness, booth, queue_id = self.make_production_countdown()
                countdown = booth.auto_flow.countdown
                harness.advance(0.9)
                first = countdown.begin(queue_id, now=harness.clock.now())
                for elapsed in range(8):
                    self.assertTrue(booth._record_countdown_person_result(
                        booth._countdown_person_check, boxes, (480, 640), float(elapsed)))
                    self.assertIs(countdown.begin(queue_id, now=harness.clock.now()), first)
                    pending = countdown.tick(queue_id, now=harness.clock.now())
                    self.assertFalse(pending.capture_due)
                    self.assertEqual(pending.remaining_seconds, 8.0 - elapsed)
                    self.assertEqual(harness.camera.calls, [])
                    if elapsed < 7:
                        harness.advance(1.0)
                harness.advance(0.9)
                self.assertFalse(countdown.tick(queue_id, now=harness.clock.now()).capture_due)
                self.assertEqual(harness.camera.calls, [])
                harness.advance(0.1)
                due = countdown.tick(queue_id, now=harness.clock.now())
                self.assertTrue(due.capture_due)
                result = booth.auto_flow.bridge.run(
                    queue_id=queue_id, capture_due=due.capture_due, job_id=queue_id)
                self.assertEqual(result.queue_status, "ready_for_pickup")
                self.assertFalse(countdown.tick(queue_id, now=harness.clock.now()).capture_due)
                self.assertEqual(harness.camera.calls, [harness.consent_id])
                self.assertEqual(len(harness.photo_store.records()), 1)

    def test_frame_loss_after_begin_still_blocks_capture(self):
        for age in (None, 2.0, 3.0):
            with self.subTest(age=age):
                harness, booth, queue_id = self.make_production_countdown()
                countdown = booth.auto_flow.countdown
                countdown.begin(queue_id, now=harness.clock.now())
                harness.advance(2.0)
                booth.preview_hub.stats.return_value = {"last_frame_s": age}
                self.assertFalse(booth.auto_flow.camera_probe())
                cancelled = countdown.tick(queue_id, now=harness.clock.now())
                self.assertFalse(cancelled.capture_due)
                self.assertEqual(cancelled.phase, "cancelled")
                self.assertEqual(cancelled.reason_code, "countdown_camera_invalid")
                harness.advance(6.0)
                self.assertFalse(countdown.tick(queue_id, now=harness.clock.now()).capture_due)
                self.assertEqual(harness.camera.calls, [])
                self.assertEqual(harness.photo_store.records(), [])

    def test_consent_revocation_after_begin_still_blocks_capture(self):
        harness, booth, queue_id = self.make_production_countdown()
        countdown = booth.auto_flow.countdown
        countdown.begin(queue_id, now=harness.clock.now())
        harness.advance(4.0)
        self.assertTrue(harness.consent.revoke(harness.consent_id))
        cancelled = countdown.tick(queue_id, now=harness.clock.now())
        self.assertEqual(cancelled.phase, "cancelled")
        self.assertFalse(cancelled.capture_due)
        self.assertEqual(cancelled.reason_code, "countdown_consent_revoked")
        harness.advance(4.0)
        self.assertFalse(countdown.tick(queue_id, now=harness.clock.now()).capture_due)
        self.assertEqual(harness.camera.calls, [])
        self.assertEqual(harness.photo_store.records(), [])

    def test_active_queue_cancellation_after_begin_still_blocks_capture(self):
        harness, booth, queue_id = self.make_production_countdown()
        countdown = booth.auto_flow.countdown
        countdown.begin(queue_id, now=harness.clock.now())
        harness.advance(4.0)
        harness.queue.mark_cancelled(queue_id, reason="参会者主动取消")
        cancelled = countdown.tick(queue_id, now=harness.clock.now())
        self.assertEqual(cancelled.phase, "cancelled")
        self.assertFalse(cancelled.capture_due)
        self.assertEqual(cancelled.reason_code, "countdown_session_cancelled")
        harness.advance(4.0)
        self.assertFalse(countdown.tick(queue_id, now=harness.clock.now()).capture_due)
        self.assertEqual(harness.camera.calls, [])
        self.assertEqual(harness.photo_store.records(), [])


class BoothCountdownTimingTest(unittest.TestCase):
    def test_real_tick_loop_waits_full_eight_seconds_after_gate(self):
        harness = _Harness()
        self.addCleanup(harness.close)
        queue_id = harness.bind_queue()
        countdown = harness.make_countdown(countdown_seconds=8)
        booth = booth_server.Booth.__new__(booth_server.Booth)
        booth.queue_jobs = {}
        booth.jobs = SimpleNamespace(get=lambda key: None)
        booth.active_countdown_job = None
        booth._countdown_person_lock = threading.Lock()
        booth._countdown_person_check = None
        booth._gesture_preview_pause = threading.Event()
        booth._start_countdown_person_check = lambda key: None
        captured = threading.Event()
        moments = {}
        numbers = []

        def gate(*args):
            time.sleep(0.3)
            moments["confirmed"] = time.monotonic()
            return True

        def capture(**kwargs):
            moments["capture"] = time.monotonic()
            result = harness.bridge.run(**kwargs)
            booth.auto_flow = None
            captured.set()
            return result

        def project(busy):
            job = booth.queue_jobs.get(queue_id)
            if job and job.stage == "countdown":
                numbers.append(job.countdown_remaining)

        booth.preview_hub = SimpleNamespace(set_gate=project)
        booth.auto_flow = SimpleNamespace(queue=harness.queue, scheduler=None, countdown=countdown,
                                         bridge=SimpleNamespace(run=capture))
        with patch.object(booth_server, "_gesture_gate_allows", side_effect=gate), patch.object(booth_server, "log"):
            try:
                booth._start_auto_flow_tick_thread()
                self.assertTrue(captured.wait(15), "没有按时触发快门")
            finally:
                booth.auto_flow = None
                booth._auto_flow_tick_thread.join(timeout=3)
        self.assertGreaterEqual(moments["capture"] - moments["confirmed"], 8.0)
        self.assertEqual(set(numbers), set(range(1, 9)))
        self.assertEqual(len(harness.camera.calls), 1)
        self.assertEqual(harness.status(), "ready_for_pickup")

