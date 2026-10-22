from __future__ import annotations

import io
from types import SimpleNamespace
from unittest.mock import patch

from PIL import Image

from app.tests.test_auto_flow_http import AutoFlowHttpTestCase, booth_server


class PortraitModePipelineTests(AutoFlowHttpTestCase):
    with_services = False

    def test_queue_adapter_passes_mode_to_generation_job(self):
        for mode in ("standard", "chibi"):
            with self.subTest(mode=mode):
                entry = SimpleNamespace(style_id="pixel-person", portrait_mode=mode)
                self.booth.auto_flow = SimpleNamespace(queue=SimpleNamespace(get=lambda queue_id: entry))

                def finish(job, style, meta):
                    self.assertEqual(style, "pixel-person")
                    self.assertEqual(meta["portrait_mode"], mode)
                    self.assertEqual(job.snapshot()["portrait_mode"], mode)
                    job.ok = True
                    job.stage = "done"
                    job.record_token = "test-photo-token"
                    job.record_id = "test-record"

                with patch.object(self.booth, "_run", side_effect=finish):
                    record = booth_server._BoothCapturePhotoStore(self.booth).commit("queue-mode", "unused", "styled")
                self.assertEqual(record.photo_token, "test-photo-token")

    def test_queue_adapter_passes_optional_identity_text_to_rendering(self):
        entry = SimpleNamespace(
            style_id="pixel-person", portrait_mode="standard",
            name="小明", handle="@spacemit",
        )
        self.booth.auto_flow = SimpleNamespace(queue=SimpleNamespace(get=lambda queue_id: entry))

        def finish(job, style, meta):
            self.assertEqual(meta["name"], "小明")
            self.assertEqual(meta["handle"], "@spacemit")
            job.ok = True
            job.stage = "done"
            job.record_token = "test-photo-token"
            job.record_id = "test-record"

        with patch.object(self.booth, "_run", side_effect=finish):
            booth_server._BoothCapturePhotoStore(self.booth).commit(
                "queue-with-identity", "unused", "styled"
            )

    def test_pipeline_persists_selected_mode_with_fixed_capture(self):
        for mode in ("standard", "chibi"):
            with self.subTest(mode=mode):
                photo = Image.new("RGB", (12, 18), "navy")
                buffer = io.BytesIO()
                photo.save(buffer, "JPEG")
                camera = SimpleNamespace(snapshot=lambda **kwargs: buffer.getvalue(),
                                         last_snapshot_note=None, status=SimpleNamespace(real=False))
                job = self.booth.jobs.create("pixel-person")
                with patch.object(self.booth, "camera", camera), patch.object(
                    self.booth.infer, "seg", return_value={"infer_ms": 1, "person_count": 1, "ep": False}
                ), patch.object(self.booth, "note_ep"), patch.object(
                    self.booth, "_aigc_scene", return_value=photo
                ) as generation, patch.object(
                    self.booth, "_render", return_value=(photo, None, "像素风")
                ), patch.dict(booth_server.os.environ, {"BOOTH_STYLE_PREVIEW_HOLD_S": "0"}):
                    self.booth._run(job, "pixel-person", {"portrait_mode": mode})
                self.assertEqual(generation.call_args.args[-1], mode)
                self.assertEqual(job.snapshot()["portrait_mode"], mode)
                record = self.booth.store.by_token(job.record_token)
                self.assertEqual(record.extra["portrait_mode"], mode)
                self.assertEqual(self.booth.photo_meta(job.record_token)["portrait_mode"], mode)
                self.assertTrue(job.capture_preview_url)
                self.assertEqual(job.stage, "done")

    def test_direct_capture_defaults_and_validates_mode(self):
        consent = self.booth.consent.grant("photo")["token"]
        for mode in (None, "standard", "chibi"):
            body = {"consent_token": consent, "style": "pixel-person"}
            if mode is not None:
                body["portrait_mode"] = mode
            with patch.object(self.booth, "_guarded_run"):
                status, payload = self.http("POST", "/api/capture", body)
            self.assertEqual(status, 202)
            self.assertEqual(payload["portrait_mode"], mode or "standard")
            self.assertEqual(self.booth.jobs.get(payload["job"]).snapshot()["portrait_mode"], mode or "standard")
        for invalid in ("cute", 1, None, [], {}):
            with patch.object(self.booth, "capture") as capture:
                status, payload = self.http("POST", "/api/capture", {
                    "consent_token": consent, "portrait_mode": invalid
                })
            self.assertEqual(status, 400)
            capture.assert_not_called()
