"""人物模式模型与旧版持久数据的离线契约测试。"""

from __future__ import annotations

import json
import unittest

from app.auto_flow.models import (
    CaptureJob,
    QueueEntry,
    QueueSubmission,
    canonical_payload,
    payload_fingerprint,
)


class PortraitModeModelTest(unittest.TestCase):
    def model_payloads(self):
        timestamp = "2026-10-12T10:00:00Z"
        return (
            (QueueSubmission, {
                "request_id": "request-1", "style_id": "any-style",
                "capture_consent_id": "consent-1",
            }),
            (QueueEntry, {
                "queue_id": "queue-1", "request_id": "request-1", "status": "queued",
                "style_id": "any-style", "created_at": timestamp, "updated_at": timestamp,
            }),
            (CaptureJob, {
                "job_id": "job-1", "queue_id": "queue-1", "status": "created",
                "capture_consent_id": "consent-1", "style_id": "any-style",
                "created_at": timestamp, "updated_at": timestamp,
            }),
        )

    def test_missing_mode_in_old_records_defaults_to_standard(self):
        for model, old_payload in self.model_payloads():
            with self.subTest(model=model.__name__):
                restored = model.from_dict(json.loads(json.dumps(old_payload)))
                self.assertEqual(restored.portrait_mode, "standard")
                self.assertEqual(restored.to_dict()["portrait_mode"], "standard")
                self.assertEqual(model.from_dict(restored.to_dict()), restored)
                self.assertNotIn("portrait_mode", old_payload)

    def test_explicit_mode_survives_json_roundtrip_for_all_models(self):
        for model, payload in self.model_payloads():
            for mode in ("standard", "chibi"):
                with self.subTest(model=model.__name__, mode=mode):
                    original = model.from_dict({**payload, "portrait_mode": mode})
                    restored = model.from_dict(json.loads(json.dumps(original.to_dict())))
                    self.assertEqual(restored, original)
                    self.assertEqual(restored.portrait_mode, mode)

    def test_explicit_invalid_mode_rejected_by_all_models(self):
        for model, payload in self.model_payloads():
            for mode in (None, "", "CHIBI", " chibi ", "adult", True, 1, [], {}):
                with self.subTest(model=model.__name__, mode=mode):
                    with self.assertRaises(ValueError):
                        model.from_dict({**payload, "portrait_mode": mode})

    def test_capture_job_inherits_queue_mode_without_changing_style(self):
        for mode in ("standard", "chibi"):
            with self.subTest(mode=mode):
                submission = QueueSubmission("request-1", "any-style", "consent-1",
                                             portrait_mode=mode)
                timestamp = "2026-10-12T10:00:00Z"
                entry = QueueEntry.from_dict({
                    **submission.to_dict(), "queue_id": "queue-1", "status": "queued",
                    "created_at": timestamp, "updated_at": timestamp,
                })
                job = CaptureJob.from_dict({
                    **entry.to_dict(), "job_id": "job-1", "status": "created",
                    "capture_consent_id": submission.capture_consent_id,
                })
                self.assertEqual(job.style_id, "any-style")
                self.assertEqual(job.portrait_mode, mode)
                self.assertEqual(CaptureJob.from_dict(job.to_dict()), job)

    def test_fingerprint_includes_mode_with_standard_default(self):
        implicit = payload_fingerprint(style_id="any-style")
        standard = payload_fingerprint(style_id="any-style", portrait_mode="standard")
        chibi = payload_fingerprint(style_id="any-style", portrait_mode="chibi")
        self.assertEqual(implicit, standard)
        self.assertNotEqual(standard, chibi)
        self.assertEqual(json.loads(canonical_payload(style_id="any-style"))["portrait_mode"],
                         "standard")
        with self.assertRaises(ValueError):
            payload_fingerprint(style_id="any-style", portrait_mode=None)


if __name__ == "__main__":
    unittest.main()
