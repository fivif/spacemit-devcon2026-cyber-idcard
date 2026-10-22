from __future__ import annotations

import io
import time
import urllib.error
import urllib.request
from types import SimpleNamespace
from unittest.mock import patch

from PIL import Image

from app.tests.test_auto_flow_http import AutoFlowHttpTestCase


class CapturePreviewTest(AutoFlowHttpTestCase):
    with_services = False

    def prepare_capture(self):
        job = self.booth.jobs.create("professional-suit")
        job.record_id = self.booth.store.next_id()
        job.record_token = self.booth.store.new_token()
        job.capture_preview_url = "/api/capture-preview/%s.jpg" % job.record_token
        photo = io.BytesIO()
        Image.new("RGB", (12, 18), "navy").save(photo, "JPEG")
        self.booth.store.capture_path(job.record_id).write_bytes(photo.getvalue())
        return job, photo.getvalue()

    def fetch(self, path):
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            response = opener.open(self.base_url + path, timeout=5)
        except urllib.error.HTTPError as exc:
            response = exc
        with response:
            return response.status, response.headers, response.read()

    def test_preview_serves_exact_capture_and_is_not_cached(self):
        job, photo = self.prepare_capture()
        status, headers, body = self.fetch(job.snapshot()["capture_preview_url"])
        self.assertEqual(status, 200)
        self.assertEqual(body, photo)
        self.assertIn("image/jpeg", headers["Content-Type"])
        self.assertIn("no-store", headers["Cache-Control"])
        self.assertEqual(self.fetch("/api/capture-preview/%s.jpg" % job.record_id)[0], 404)
        self.assertEqual(self.fetch("/api/capture-preview/%s.jpg" % job.id)[0], 404)

    def test_preview_requires_completed_frame_write(self):
        job, _ = self.prepare_capture()
        job.capture_preview_url = None
        self.assertEqual(self.fetch("/api/capture-preview/%s.jpg" % job.record_token)[0], 404)
        self.assertIsNone(job.snapshot()["capture_preview_url"])

    def test_expired_preview_is_unavailable(self):
        job, _ = self.prepare_capture()
        job.finished_at = time.time() - 601
        self.assertEqual(self.fetch(job.capture_preview_url)[0], 404)

    def test_missing_capture_is_unavailable(self):
        job, _ = self.prepare_capture()
        job.record_id = "SM-2026-9999"
        self.assertEqual(self.fetch(job.capture_preview_url)[0], 404)

    def test_pipeline_publishes_frame_before_inference(self):
        job = self.booth.jobs.create("natural")
        photo = io.BytesIO()
        Image.new("RGB", (12, 18), "navy").save(photo, "JPEG")
        raw = photo.getvalue()
        camera = SimpleNamespace(snapshot=lambda **kwargs: (_ for _ in ()).throw(
                                     AssertionError("锁定预览帧后不应再次读取相机")),
                                 last_snapshot_note=None, status=SimpleNamespace(real=False))

        def infer_frame(capture_path, **kwargs):
            self.assertEqual(job.stage, "inferring")
            self.assertTrue(job.capture_preview_url)
            self.assertEqual(self.fetch(job.capture_preview_url)[2], raw)
            return {"infer_ms": 1.0, "person_count": 1, "ep": False}

        with patch.object(self.booth, "camera", camera), \
                patch.object(self.booth.preview_hub, "latest_capture_jpeg", return_value=raw), \
                patch.object(self.booth.infer, "seg", side_effect=infer_frame), \
                patch.object(self.booth, "note_ep"), \
                patch.object(self.booth, "_render", return_value=(Image.open(io.BytesIO(raw)), None, "natural")):
            self.booth._run(job, "natural", {})
        self.assertEqual(job.stage, "done")
        self.assertEqual(self.fetch(job.capture_preview_url)[2], raw)
        self.assertEqual(len(list((self.booth.store.root / "captures").glob("*.jpg"))), 1)
