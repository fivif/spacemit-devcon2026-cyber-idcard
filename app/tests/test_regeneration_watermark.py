from __future__ import annotations

import io
import json
import sys
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from PIL import Image, ImageChops

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import booth_server
from aigc_catalog import StyleCatalog
from render.ai_label import apply_ai_label
from store import Record, Store
from app.auto_flow import errors
from app.auto_flow.models import QueueSubmission, payload_fingerprint
from app.auto_flow.queue_service import QueueService
from app.auto_flow.queue_store import QueueStore


class QueueWatermarkTests(unittest.TestCase):
    def test_default_and_opt_out_are_durable_and_idempotent(self):
        with tempfile.TemporaryDirectory() as directory:
            service = QueueService(QueueStore(Path(directory)))
            base = {"request_id": "request", "style_id": "pixel-person", "capture_consent_id": "consent"}
            entry = service.submit(base)
            self.assertTrue(entry.ai_watermark)
            self.assertEqual(service.submit({**base, "ai_watermark": True}).queue_id, entry.queue_id)
            with self.assertRaises(errors.ApiError):
                service.submit({**base, "ai_watermark": False})
            opted_out = service.submit({**base, "request_id": "second", "ai_watermark": False})
            reloaded = QueueService(QueueStore(Path(directory)))
            self.assertFalse(reloaded.get(opted_out.queue_id).ai_watermark)
            self.assertFalse(reloaded.get(opted_out.queue_id).to_dict()["ai_watermark"])
            self.assertEqual(reloaded.submit({**base, "request_id": "second", "ai_watermark": False}).queue_id,
                             opted_out.queue_id)

    def test_rejects_non_booleans_and_keeps_previous_default_fingerprint(self):
        for value in (None, 0, 1, "false", [], {}):
            with self.subTest(value=value), self.assertRaises(ValueError):
                QueueSubmission.from_dict({"request_id": "r", "style_id": "s",
                                           "capture_consent_id": "c", "ai_watermark": value})
        import hashlib

        old = json.dumps({"style_id": "s", "optional_fields": {}, "portrait_mode": "chibi"},
                         sort_keys=True, separators=(",", ":"))
        self.assertEqual(payload_fingerprint(style_id="s", portrait_mode="chibi"),
                         "sha256:" + hashlib.sha256(old.encode()).hexdigest())


class RegenerationTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        root = Path(self.directory.name)
        self.booth = object.__new__(booth_server.Booth)
        self.booth.store = Store(root)
        (root / "previews").mkdir()
        (root / "masks").mkdir()
        self.booth.jobs = booth_server.JobBoard()
        self.booth.lock = threading.Lock()
        self.booth.style_catalog = StyleCatalog(root)
        self.booth.active_countdown_job = None
        self.booth.auto_flow = None
        self.booth.camera = SimpleNamespace(snapshot=Mock(side_effect=AssertionError("不允许拍摄")),
                                            status=SimpleNamespace(real=False))
        self.booth._infer_uploaded_portrait = Mock(return_value={"ok": True, "person_count": 1, "ep": True})
        self.booth.ep_threads = 0
        self.booth.note_ep = Mock()
        self.booth.counters = {"captures": 0, "errors": 0, "cpu_infers": 0, "degraded": 0}
        self.photo = Image.new("RGB", (512, 768), "navy")
        raw = io.BytesIO()
        self.photo.save(raw, "JPEG")
        self.raw = raw.getvalue()
        self.record = Record(id=self.booth.store.next_id(), token=self.booth.store.new_token(),
                             created_at=datetime.now(timezone.utc).isoformat(), prompt="person",
                             ep=True, infer_ms=1, person_count=1, degraded=True, seed=7,
                             card="原片", style="pixel-person",
                             extra={"name": "Ada", "handle": "@ada", "portrait_mode": "standard"})
        self.booth.store.append(self.record)
        self.booth.store.capture_path(self.record.id).write_bytes(self.raw)
        self.photo.save(self.booth.store.photo_path(self.record.id))

    def synchronous_thread(self, **kwargs):
        return SimpleNamespace(start=lambda: kwargs["target"](*kwargs["args"], **kwargs.get("kwargs", {})))

    def generate(self, watermark=True, failure=False, original=False):
        scene = Image.new("RGB", self.photo.size, (98, 120, 150))
        with patch.object(booth_server.threading, "Thread", side_effect=self.synchronous_thread), patch.object(
            self.booth, "_aigc_scene", return_value=scene,
            side_effect=RuntimeError("test generation failure") if failure else None
        ) as generate, patch.object(self.booth, "_render", return_value=(scene, None, "测试风格", scene)), patch.dict(
            booth_server.os.environ, {"BOOTH_STYLE_PREVIEW_HOLD_S": "0", "BOOTH_MATTE_PREVIEW_HOLD_S": "0"}
        ):
            if original:
                job = self.booth.submit_operator_upload(self.raw, "pixel-person", {"upload_mode": "original"})
            else:
                job = self.booth.submit_operator_regeneration(self.record.token, "pixel-person", "chibi", watermark)
        self.assertEqual(job.stage, "done", job.error)
        self.assertFalse(self.booth.lock.locked())
        return job, self.booth.store.by_token(job.record_token), generate

    def test_retry_uses_original_and_selected_configuration_and_keeps_old_photo(self):
        before = self.booth.store.photo_path(self.record.id).read_bytes()
        job, record, generate = self.generate()
        self.assertEqual(job.source, "operator_regenerate")
        self.assertNotEqual(record.id, self.record.id)
        self.assertEqual(record.extra["regenerated_from"], self.record.id)
        self.assertEqual(record.extra["name"], "Ada")
        self.assertEqual(record.extra["handle"], "@ada")
        self.assertEqual(record.extra["portrait_mode"], "chibi")
        self.assertTrue(record.extra["ai_generated"])
        self.assertTrue(record.extra["ai_watermark_applied"])
        self.assertEqual(self.booth.store.capture_path(record.id).read_bytes(), self.raw)
        self.assertEqual(self.booth.store.photo_path(self.record.id).read_bytes(), before)
        self.assertEqual(generate.call_args.args[0], "pixel-person")
        self.assertEqual(generate.call_args.args[-1], "chibi")
        self.booth.camera.snapshot.assert_not_called()
        final = Image.open(self.booth.store.photo_path(record.id)).convert("RGB")
        clean = Image.open(self.booth.store.clean_photo_path(record.id)).convert("RGB")
        self.assertGreater(final.getpixel((468, 40))[0], 180)
        self.assertEqual(final.tobytes(), clean.tobytes())

    def test_opt_out_and_failed_or_original_images_have_no_ai_label(self):
        for watermark, failure, original in ((False, False, False), (True, True, False), (True, False, True)):
            with self.subTest(watermark=watermark, failure=failure, original=original):
                job, record, _ = self.generate(watermark=watermark, failure=failure, original=original)
                self.assertFalse(record.extra["ai_watermark_applied"])
                self.assertEqual(record.extra["ai_generated"], not (failure or original))
                self.assertEqual(record.degraded, failure)

    def test_busy_or_repeated_submission_does_not_create_another_job(self):
        with patch.object(booth_server.threading, "Thread"):
            self.booth.submit_operator_regeneration(self.record.token, "pixel-person", "standard")
            with self.assertRaisesRegex(RuntimeError, "正在拍摄或生成"):
                self.booth.submit_operator_regeneration(self.record.token, "pixel-person", "standard")
            self.assertEqual(len(self.booth.jobs._jobs), 1)
        self.booth.lock.release()

    def test_missing_expired_deleted_or_wrong_source_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "不存在"):
            self.booth.submit_operator_regeneration("invalid", "pixel-person", "standard")
        with patch.object(self.booth.store, "by_token", return_value=None), self.assertRaises(ValueError):
            self.booth.submit_operator_regeneration(self.record.token, "pixel-person", "standard")
        with patch.object(Path, "is_file", return_value=False), self.assertRaises(ValueError):
            self.booth.submit_operator_regeneration(self.record.token, "pixel-person", "standard")
        for style, mode, watermark in (("missing", "standard", True), ("pixel-person", "bad", True),
                                       ("pixel-person", "standard", "false")):
            with self.subTest(style=style, mode=mode), self.assertRaises(ValueError):
                self.booth.submit_operator_regeneration(self.record.token, style, mode, watermark)
        self.assertIsNone(self.booth.jobs.latest())

    def test_waiting_for_gesture_does_not_allow_regeneration_to_take_capture_slot(self):
        self.booth.auto_flow = SimpleNamespace(queue=SimpleNamespace(
            snapshot=lambda: {"occupancy": {"status": "ready"}}))
        with self.assertRaisesRegex(RuntimeError, "正在拍摄或生成"):
            self.booth.submit_operator_regeneration(self.record.token, "pixel-person", "standard")
        self.assertIsNone(self.booth.jobs.latest())
        self.assertFalse(self.booth.lock.locked())

    def test_source_deleted_during_generation_does_not_publish_new_record(self):
        available = self.booth.store.by_token
        with patch.object(self.booth.store, "by_token", side_effect=[self.record, self.record, None]):
            with patch.object(booth_server.threading, "Thread", side_effect=self.synchronous_thread), patch.object(
                self.booth, "_aigc_scene", return_value=self.photo
            ), patch.object(self.booth, "_render", return_value=(self.photo, None, "测试风格", self.photo)), patch.dict(
                booth_server.os.environ, {"BOOTH_STYLE_PREVIEW_HOLD_S": "0"}
            ):
                job = self.booth.submit_operator_regeneration(self.record.token, "pixel-person", "standard")
        self.assertEqual(job.stage, "failed")
        self.assertIn("删除或过期", job.error)
        self.assertIsNone(available(job.record_token))
        self.assertFalse(self.booth.lock.locked())

    def test_regeneration_http_route_and_busy_response(self):
        handler = object.__new__(booth_server.Handler)
        handler.booth = self.booth
        handler.path = "/api/operator/photos/regenerate"
        handler.headers = {}
        handler._json = Mock()
        handler._error = Mock()
        handler._body = Mock(return_value={"token": self.record.token, "style": "pixel-person",
                                          "portrait_mode": "chibi", "ai_watermark": False})
        with patch.object(booth_server.threading, "Thread"):
            handler.do_POST()
            self.assertEqual(handler._json.call_args.args[1], 202)
            self.assertFalse(handler._json.call_args.args[0]["job"]["ai_watermark"])
            handler.do_POST()
            self.assertEqual(handler._error.call_args.args[:2], (409, "busy"))
        self.booth.lock.release()


class AiLabelTests(unittest.TestCase):
    def test_visible_top_right_and_safe_from_crop_without_touching_brand_area(self):
        for size in ((1024, 1536), (1600, 900)):
            with self.subTest(size=size):
                original = Image.new("RGB", size, "white")
                result = apply_ai_label(original)
                bbox = ImageChops.difference(result, original).getbbox()
                self.assertIsNotNone(bbox)
                self.assertGreater(bbox[0], size[0] * 0.75)
                self.assertGreater(bbox[1], min(size) * 0.06)
                self.assertLess(bbox[2], size[0] - min(size) * 0.06)
                self.assertLess(bbox[3], size[1] * 0.15)
                self.assertEqual(original.getpixel((size[0] - 1, 0)), (255, 255, 255))


if __name__ == "__main__":
    unittest.main()
