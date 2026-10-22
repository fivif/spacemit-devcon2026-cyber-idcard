"""后台图片上传离线回归；运行：python app/tests/test_operator_upload.py -v。"""

from __future__ import annotations

import base64
import io
import sys
import tempfile
import threading
import unittest
from email.message import Message
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import numpy as np
from PIL import Image, ImageDraw, ImageOps

APP_DIR = Path(__file__).resolve().parents[1]
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))

import booth_server
from aigc_catalog import StyleCatalog
from store import Store


def image_bytes(image_format="PNG"):
    buffer = io.BytesIO()
    Image.new("RGB", (12, 18), "navy").save(buffer, image_format)
    return buffer.getvalue()


def upload_handler(booth, fields, raw=None, quoted_boundary=False):
    boundary = "operator-upload-test"
    parts = []
    for name, value in fields.items():
        parts.append(
            ('--%s\r\nContent-Disposition: form-data; name="%s"\r\n\r\n%s\r\n'
             % (boundary, name, value)).encode("utf-8")
        )
    if raw is not None:
        parts.append(
            ('--%s\r\nContent-Disposition: form-data; name="image"; '
             'filename="portrait.png"\r\nContent-Type: image/png\r\n\r\n'
             % boundary).encode("ascii") + raw + b"\r\n"
        )
    parts.append(("--%s--\r\n" % boundary).encode("ascii"))
    payload = b"".join(parts)
    handler = object.__new__(booth_server.Handler)
    handler.booth = booth
    handler.path = "/api/operator/upload"
    handler.headers = Message()
    boundary_value = '"%s"' % boundary if quoted_boundary else boundary
    handler.headers["Content-Type"] = "multipart/form-data; boundary=" + boundary_value
    handler.headers["Content-Length"] = str(len(payload))
    handler.rfile = io.BytesIO(payload)
    handler._json = Mock()
    handler._error = Mock()
    return handler


class OperatorUploadTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="operator-upload-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.booth = object.__new__(booth_server.Booth)
        self.booth.jobs = booth_server.JobBoard()
        self.booth._gesture_seg_lock = threading.Lock()
        self.booth.style_catalog = StyleCatalog(self.root)
        self.raw = image_bytes()

    def test_multipart_route_passes_binary_image_and_text_fields(self):
        for quoted in (False, True):
            with self.subTest(quoted_boundary=quoted):
                job = booth_server.Job("upload-test", "pixel-person")
                submit = Mock(return_value=job)
                handler = upload_handler(SimpleNamespace(submit_operator_upload=submit), {
                    "mode": "aigc", "style": "pixel-person", "orientation": "wide",
                    "portrait_mode": "chibi", "name": "小明", "handle": "@developer",
                    "seed": "19",
                }, self.raw, quoted)
                handler.do_POST()
                handler._error.assert_not_called()
                submit.assert_called_once_with(self.raw, "pixel-person", {
                    "orientation": "wide", "portrait_mode": "chibi", "name": "小明",
                    "handle": "@developer", "seed": 19, "mnemonic": None,
                    "upload_mode": "aigc",
                    "ai_watermark": True,
                })
                handler._json.assert_called_once()
                response, status = handler._json.call_args.args
                self.assertEqual(status, 202)
                self.assertTrue(response["ok"])
                self.assertEqual(response["job"]["job"], job.id)
                self.assertEqual(response["job"]["style"], "pixel-person")
                self.assertEqual(response["record_id"], job.record_id)
                self.assertEqual(response["poll"], "/api/job/upload-test")

    def test_multipart_route_rejects_missing_image(self):
        submit = Mock()
        handler = upload_handler(SimpleNamespace(submit_operator_upload=submit), {"mode": "original"})
        handler.do_POST()
        self.assertEqual(handler._error.call_args.args[:2], (400, "image_required"))
        submit.assert_not_called()
        handler._json.assert_not_called()

    def test_rejects_empty_invalid_and_oversized_images_before_starting_worker(self):
        for raw, message in (
            (b"", "图片不能为空"),
            (b"not an image", "无法读取这张图片"),
            (b"x" * (20 * 1024 * 1024 + 1), "不能超过 20 MB"),
        ):
            with self.subTest(message=message), patch.object(booth_server.threading, "Thread") as worker:
                with self.assertRaisesRegex(ValueError, message):
                    self.booth.submit_operator_upload(raw, None, {"upload_mode": "original"})
                worker.assert_not_called()
                self.assertIsNone(self.booth.jobs.latest())

    def test_accepts_jpeg_and_png_and_preserves_aigc_selection(self):
        for image_format in ("JPEG", "PNG"):
            with self.subTest(image_format=image_format), patch.object(booth_server.threading, "Thread") as worker:
                raw = image_bytes(image_format)
                meta = {"upload_mode": "aigc", "portrait_mode": "chibi"}
                job = self.booth.submit_operator_upload(raw, "pixel-person", meta)
                self.assertEqual(job.style, "pixel-person")
                self.assertEqual(job.portrait_mode, "chibi")
                self.assertEqual(job.source, "operator_upload")
                self.assertIs(self.booth.jobs.get(job.id), job)
                worker.assert_called_once()
                worker.return_value.start.assert_called_once_with()
                submitted_job, style, submitted_meta, photo, submitted_raw = worker.call_args.kwargs["args"]
                self.assertIs(submitted_job, job)
                self.assertEqual(style, "pixel-person")
                self.assertEqual(submitted_meta, {**meta, "source": "operator_upload"})
                self.assertNotIn("source", meta)
                self.assertEqual(photo.mode, "RGB")
                self.assertEqual(photo.size, (12, 18))
                self.assertEqual(submitted_raw, raw)

    def test_aigc_requires_an_enabled_style(self):
        catalog = self.booth.style_catalog.snapshot()
        disabled = catalog["styles"][0]["id"]
        catalog["styles"][0]["enabled"] = False
        self.booth.style_catalog.save(catalog["styles"])
        for style in (None, "", "  ", "no-such-style", disabled):
            with self.subTest(style=style), patch.object(booth_server.threading, "Thread") as worker:
                with self.assertRaisesRegex(ValueError, "请选择一个已启用的 AIGC 风格"):
                    self.booth.submit_operator_upload(self.raw, style, {"upload_mode": "aigc"})
                worker.assert_not_called()
                self.assertIsNone(self.booth.jobs.latest())

    def test_original_pipeline_uses_upload_without_camera_capture_or_aigc(self):
        self.booth.store = Store(self.root)
        self.booth.lock = threading.Lock()
        self.booth.camera = SimpleNamespace(
            status=SimpleNamespace(real=False),
            snapshot=Mock(side_effect=AssertionError("后台上传不得调用相机抓帧")),
        )
        self.booth.infer = SimpleNamespace(seg=Mock(return_value={
            "infer_ms": 1, "person_count": 1, "ep": False,
        }))
        self.booth.ep_threads = 0
        self.booth.note_ep = Mock()
        self.booth.counters = {"captures": 0, "errors": 0, "cpu_infers": 0, "degraded": 0}
        photo = Image.open(io.BytesIO(self.raw)).convert("RGB")

        def run_synchronously(**kwargs):
            return SimpleNamespace(start=lambda: kwargs["target"](*kwargs["args"]))

        with patch.object(booth_server.threading, "Thread", side_effect=run_synchronously), patch.object(
            self.booth, "_aigc_scene", side_effect=AssertionError("原片不得调用 AIGC")
        ) as generate, patch.object(
            self.booth, "_render", return_value=(photo, None, "原片", photo)
        ) as render:
            job = self.booth.submit_operator_upload(
                self.raw, "pixel-person", {"upload_mode": "original"}
            )

        self.assertEqual(job.stage, "done", job.error)
        self.assertTrue(job.ok)
        self.assertFalse(job.degraded, job.degraded_reason)
        self.assertEqual(job.style, "natural")
        self.booth.camera.snapshot.assert_not_called()
        self.booth.infer.seg.assert_not_called()
        self.booth.note_ep.assert_not_called()
        self.assertEqual(self.booth.counters["cpu_infers"], 0)
        infer_step = next(step for step in job.steps if step["key"] == "infer")
        self.assertEqual(infer_step["label"], "原图无需人像分割")
        generate.assert_not_called()
        render.assert_called_once()
        self.assertEqual(render.call_args.args[1].tobytes(), photo.tobytes())
        self.assertEqual(self.booth.store.capture_path(job.record_id).read_bytes(), self.raw)
        record = self.booth.store.by_token(job.record_token)
        self.assertIsNotNone(record)
        self.assertEqual(record.style, "natural")
        self.assertEqual(record.extra["source"], "operator_upload")
        self.assertTrue(self.booth.store.photo_path(job.record_id).is_file())
        self.assertFalse(self.booth.lock.locked())

    def assert_event_identity(self, final, unmarked):
        self.assertEqual(final.size, (1024, 1536))
        pixels = np.asarray(final, dtype=np.int16)
        logo = pixels[110:155, 138:179]
        green = (logo[:, :, 1] > logo[:, :, 0] + 20) & (logo[:, :, 1] > logo[:, :, 2] + 30)
        self.assertGreater(int(green.sum()), 100, "最终成片缺少绿色 Logo")
        title = pixels[110:160, 195:850]
        dark_ink = np.max(np.abs(title - (29, 43, 49)), axis=2) < 20
        self.assertGreater(int(dark_ink.sum()), 100, "最终成片缺少大会标题")
        name = pixels[1177:1240, 166:600]
        self.assertGreater(int((name.min(axis=2) > 235).sum()), 100, "最终成片缺少白色姓名")
        handle = pixels[1254:1295, 168:600]
        self.assertGreater(int((handle.min(axis=2) > 200).sum()), 30, "最终成片缺少账号")
        reference = np.asarray(unmarked, dtype=np.int16)
        self.assertLessEqual(
            float(np.abs(pixels[:80, :100] - reference[:80, :100]).mean()), 1,
            "后台上传不应因相机为 mock 而叠加 MOCK 标签",
        )

    def run_real_render_upload(self, mode, generation_fails=False, infer_failure=None,
                               processing_alpha=False):
        self.booth.store = Store(self.root)
        (self.root / "previews").mkdir()
        (self.root / "masks").mkdir()
        self.booth.lock = threading.Lock()
        self.booth.camera = SimpleNamespace(
            status=SimpleNamespace(real=False),
            snapshot=Mock(side_effect=AssertionError("后台上传不得调用相机抓帧")),
        )
        segmentation = {"ok": True, "infer_ms": 1, "person_count": 1, "ep": False}
        if processing_alpha:
            alpha_buffer = io.BytesIO()
            Image.new("L", (256, 384), 255).save(alpha_buffer, "PNG")
            segmentation["matte_b64"] = base64.b64encode(alpha_buffer.getvalue()).decode("ascii")
            segmentation["matte_source"] = "modnet-roi"
        if isinstance(infer_failure, dict):
            segmentation = infer_failure
        self.booth.infer = SimpleNamespace(
            switch_mode=Mock(return_value={"ok": True}),
            seg=Mock(return_value=segmentation,
                     side_effect=infer_failure if isinstance(infer_failure, Exception) else None),
        )
        self.booth.ep_threads = 0
        self.booth.note_ep = Mock()
        self.booth.counters = {"captures": 0, "errors": 0, "cpu_infers": 0, "degraded": 0}
        source = Image.new("RGB", (768, 1152), (120, 145, 165))
        draw = ImageDraw.Draw(source)
        draw.rectangle((0, 0, 100, 150), fill=(80, 100, 180))
        draw.rectangle((667, 1001, 767, 1151), fill=(180, 100, 80))
        draw.rectangle((300, 400, 468, 750), fill=(100, 130, 160))
        raw_buffer = io.BytesIO()
        source.save(raw_buffer, "PNG")
        scene = Image.new("RGB", (1024, 1536), (165, 145, 120))
        ImageDraw.Draw(scene).rectangle((400, 400, 624, 1000), fill=(160, 130, 100))
        meta = {"upload_mode": mode, "orientation": "vertical", "card": "keyvisual",
                "name": "Ada Lovelace", "handle": "@ada"}

        def run_synchronously(**kwargs):
            return SimpleNamespace(start=lambda: kwargs["target"](*kwargs["args"]))

        generation_error = RuntimeError("test AIGC unavailable") if generation_fails else None
        with patch.object(booth_server.threading, "Thread", side_effect=run_synchronously), patch.object(
            self.booth, "_aigc_scene", return_value=scene, side_effect=generation_error
        ) as generate, patch.dict(booth_server.os.environ, {
            "BOOTH_STYLE_PREVIEW_HOLD_S": "0", "BOOTH_MATTE_PREVIEW_HOLD_S": "0",
        }):
            job = self.booth.submit_operator_upload(raw_buffer.getvalue(), "pixel-person", meta)

        self.assertEqual(job.stage, "done", job.error)
        self.assertTrue(job.ok)
        self.assertEqual(job.source, "operator_upload")
        self.booth.camera.snapshot.assert_not_called()
        record = self.booth.store.by_token(job.record_token)
        self.assertIsNotNone(record)
        if mode == "original":
            generate.assert_not_called()
            self.booth.infer.seg.assert_not_called()
            self.booth.infer.switch_mode.assert_not_called()
            self.booth.note_ep.assert_not_called()
            self.assertFalse(job.degraded, job.degraded_reason)
            self.assertEqual(record.card, "原图")
            expected_clean = source
        elif infer_failure is not None:
            generate.assert_not_called()
            self.booth.infer.switch_mode.assert_called_once_with("seg", timeout=30.0)
            self.booth.infer.seg.assert_called_once()
            self.booth.note_ep.assert_not_called()
            self.assertTrue(job.degraded)
            self.assertIn("infer_failed:", job.degraded_reason)
            self.assertIn("未开始风格生成", job.degraded_reason)
            self.assertNotIn("没有检出人像", job.degraded_reason)
            self.assertEqual(record.card, "原片（AIGC 未完成）")
            expected_clean = ImageOps.fit(source, (1024, 1536), method=Image.Resampling.LANCZOS)
        else:
            generate.assert_called_once()
            if processing_alpha:
                self.assertEqual(generate.call_args.args[2].size, source.size)
                self.assertTrue(job.matte_applied)
            self.booth.infer.seg.assert_called_once()
            self.booth.note_ep.assert_called_once_with(segmentation)
            self.assertEqual(job.degraded, generation_fails, job.degraded_reason)
            if generation_fails:
                self.assertIn("aigc_failed: test AIGC unavailable", job.degraded_reason)
                self.assertEqual(record.card, "原片（AIGC 未完成）")
                expected_clean = ImageOps.fit(source, (1024, 1536), method=Image.Resampling.LANCZOS)
            else:
                self.assertEqual(record.card, self.booth.aigc_style("pixel-person")["title"])
                expected_clean = scene

        if mode == "aigc" and infer_failure is None and not generation_fails:
            from render.ai_label import apply_ai_label

            expected_clean = apply_ai_label(expected_clean)
        expected_buffer = io.BytesIO()
        expected_clean.save(expected_buffer, "JPEG", quality=92, optimize=True)
        with Image.open(io.BytesIO(expected_buffer.getvalue())) as expected, Image.open(
            self.booth.store.clean_photo_path(job.record_id)
        ) as clean, Image.open(self.booth.store.photo_path(job.record_id)) as final:
            self.assertEqual(clean.size, expected_clean.size)
            self.assertEqual(
                clean.tobytes(), expected.tobytes(),
                "clean 应完整保留场景与背景，不含 Logo、标题、姓名或 MOCK",
            )
            unmarked = ImageOps.fit(expected_clean, final.size, method=Image.Resampling.LANCZOS)
            self.assert_event_identity(final, unmarked)
        self.assertTrue(any(step["label"] in ("合成成片", "原图排版") for step in job.steps))
        self.assertNotIn("render_error", job.degraded_reason or "")
        self.assertFalse(self.booth.lock.locked())

    def test_real_render_aigc_success_marks_final_and_keeps_clean_scene(self):
        self.run_real_render_upload("aigc")

    def test_real_render_aigc_failure_marks_final_and_keeps_uploaded_image(self):
        self.run_real_render_upload("aigc", generation_fails=True)

    def test_real_render_original_marks_final_and_preserves_clean_size_and_background(self):
        self.run_real_render_upload("original")

    def test_inference_timeout_keeps_original_without_claiming_no_person(self):
        self.run_real_render_upload("aigc", infer_failure=TimeoutError("seg 超时（20.0s）"))

    def test_inference_error_response_is_not_misreported_as_no_person(self):
        self.run_real_render_upload("aigc", infer_failure={"ok": False, "error": "worker unavailable"})

    def test_processing_alpha_is_restored_to_original_image_size(self):
        self.run_real_render_upload("aigc", processing_alpha=True)

    def test_missed_detection_uses_valid_matte_for_camera_and_upload(self):
        alpha = Image.new("L", (120, 180), 0)
        ImageDraw.Draw(alpha).ellipse((25, 10, 95, 170), fill=255)
        for source in ("camera", "operator_upload"):
            for coarse_mask in (None, Image.new("L", alpha.size, 0)):
                with self.subTest(source=source, coarse_mask=coarse_mask is not None):
                    self.run_missed_detection(alpha, source=source, coarse_mask=coarse_mask)

    def test_missed_detection_rejects_missing_empty_faint_tiny_and_opaque_mattes(self):
        tiny = Image.new("L", (120, 180), 0)
        ImageDraw.Draw(tiny).rectangle((50, 50, 52, 52), fill=255)
        for label, alpha in (
            ("missing", None),
            ("empty", Image.new("L", (120, 180), 0)),
            ("faint", Image.new("L", (120, 180), 32)),
            ("tiny", tiny),
            ("opaque", Image.new("L", (120, 180), 255)),
        ):
            with self.subTest(matte=label):
                self.run_missed_detection(alpha, expected_generation=False)

    def test_missed_detection_does_not_treat_coarse_fallback_as_independent_matte(self):
        alpha = Image.new("L", (120, 180), 0)
        ImageDraw.Draw(alpha).rectangle((25, 10, 95, 170), fill=255)
        self.run_missed_detection(alpha, matte_source="detect-mask", expected_generation=False)

    def run_missed_detection(self, alpha, source="operator_upload", coarse_mask=None,
                             matte_source="modnet", expected_generation=True, photo=None):
        self.booth.store = Store(self.root)
        (self.root / "previews").mkdir(exist_ok=True)
        (self.root / "masks").mkdir(exist_ok=True)
        photo = photo if photo is not None else Image.new("RGB", (120, 180), "navy")
        raw_buffer = io.BytesIO()
        photo.save(raw_buffer, "PNG")
        raw = raw_buffer.getvalue()
        self.booth.camera = SimpleNamespace(
            snapshot=Mock(return_value=raw), last_snapshot_note="",
            status=SimpleNamespace(real=True),
        )
        segmentation = {"ok": True, "infer_ms": 1, "person_count": 0, "ep": False,
                        "matte_source": matte_source}
        for key, image in (("matte_b64", alpha), ("mask_b64", coarse_mask)):
            if image is not None:
                buffer = io.BytesIO()
                image.save(buffer, "PNG")
                segmentation[key] = base64.b64encode(buffer.getvalue()).decode("ascii")
        self.booth.infer = SimpleNamespace(seg=Mock(return_value=segmentation))
        self.booth.ep_threads = 0
        self.booth.note_ep = Mock()
        self.booth.counters = {"captures": 0, "errors": 0, "cpu_infers": 0, "degraded": 0}
        job = booth_server.Job("matte-fallback-test", "pixel-person")
        meta = {"source": source, "ai_watermark": False}
        with patch.object(self.booth, "_infer_uploaded_portrait", return_value=segmentation), patch.object(
            self.booth, "_aigc_scene", return_value=photo
        ) as generate, patch.object(
            self.booth, "_render", return_value=(photo, None, "像素风", photo)
        ), patch.dict(booth_server.os.environ, {
            "BOOTH_STYLE_PREVIEW_HOLD_S": "0", "BOOTH_MATTE_PREVIEW_HOLD_S": "0",
        }):
            self.booth._run(job, "pixel-person", meta,
                            source_photo=photo if source == "operator_upload" else None)
        self.assertTrue(job.ok)
        self.assertEqual(job.person_count, 0)
        record = self.booth.store.by_token(job.record_token)
        self.assertEqual(record.person_count, 0)
        self.assertEqual(record.extra["ai_generated"], expected_generation)
        if expected_generation:
            generate.assert_called_once()
            self.assertIsNotNone(generate.call_args.args[2])
            self.assertTrue(job.matte_applied)
            self.assertFalse(job.degraded, job.degraded_reason)
        else:
            generate.assert_not_called()
            self.assertTrue(job.degraded)
            self.assertIn("未取得有效人像", job.degraded_reason)

    def test_upload_processing_is_bounded_and_boxes_map_back_to_original(self):
        source = Image.new("RGB", (2320, 3088), "navy")
        job = booth_server.Job("bounded-upload", "hand-painted-animation")
        calls = []

        def segment(path, **kwargs):
            calls.append("segment")
            with Image.open(path) as image:
                self.assertEqual(max(image.size), 1920)
                self.assertAlmostEqual(image.width / image.height, source.width / source.height, places=3)
                width, height = image.size
            self.assertEqual(kwargs["timeout"], 20.0)
            return {"ok": True, "width": width, "height": height,
                    "persons": [{"box": [0, 0, width, height]}],
                    "subject": {"box": [0, 0, width, height]},
                    "matte_roi": [0, 0, width, height]}

        def ready(mode, timeout):
            calls.append("ready")
            self.assertEqual(mode, "seg")
            self.assertEqual(timeout, 30.0)
            return {"ok": True}

        self.booth.infer = SimpleNamespace(switch_mode=ready, seg=segment)
        result = self.booth._infer_uploaded_portrait(source, job)
        self.assertEqual(calls, ["ready", "segment"])
        self.assertEqual(source.size, (2320, 3088))
        self.assertEqual((result["width"], result["height"]), source.size)
        for box in (result["persons"][0]["box"], result["subject"]["box"], result["matte_roi"]):
            self.assertEqual(box, [0, 0, 2320, 3088])

    def test_model_preparation_failure_does_not_submit_segmentation(self):
        self.booth.infer = SimpleNamespace(
            switch_mode=Mock(return_value={"ok": False, "error": "model not ready"}),
            seg=Mock(),
        )
        with self.assertRaisesRegex(RuntimeError, "model not ready"):
            self.booth._infer_uploaded_portrait(Image.new("RGB", (16, 24)),
                                                 booth_server.Job("not-ready", "pixel-person"))
        self.booth.infer.seg.assert_not_called()


if __name__ == "__main__":
    unittest.main()
