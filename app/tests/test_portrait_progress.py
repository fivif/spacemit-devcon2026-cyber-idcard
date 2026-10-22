import base64
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import numpy as np
from PIL import Image, ImageFilter

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import aigc_client
from booth_server import Booth, Job, _BoothCapturePhotoStore
from infer_client import InferError, InferWorker, PROTOCOL_PREFIX
from store import Store


class PortraitExtremaTest(unittest.TestCase):
    def test_identical_to_existing_filters_including_borders(self):
        random = np.random.default_rng(321)
        for shape in ((1, 1), (2, 3), (31, 24), (213, 139)):
            source = Image.fromarray(random.integers(0, 256, shape, dtype=np.uint8))
            for size in (3, 5, 7, 9, 15, 31):
                for maximum in (False, True):
                    with self.subTest(shape=shape, size=size, maximum=maximum):
                        original = source.filter((ImageFilter.MaxFilter if maximum else ImageFilter.MinFilter)(size))
                        updated = aigc_client._portrait_extrema(source, size, maximum)
                        np.testing.assert_array_equal(np.asarray(original), np.asarray(updated))

    def test_full_protection_output_is_pixel_identical(self):
        random = np.random.default_rng(99)
        alpha = Image.fromarray(random.integers(0, 256, (431, 283), dtype=np.uint8))
        coarse = Image.fromarray(random.integers(0, 256, (61, 41), dtype=np.uint8))
        updated = aigc_client.constrain_alpha(alpha, coarse)

        def original_filter(image, size, maximum=True):
            return image.filter((ImageFilter.MaxFilter if maximum else ImageFilter.MinFilter)(size))

        with patch.object(aigc_client, "_portrait_extrema", original_filter):
            original = aigc_client.constrain_alpha(alpha, coarse)
        np.testing.assert_array_equal(np.asarray(updated), np.asarray(original))


class PortraitProgressTest(unittest.TestCase):
    def test_processing_releases_countdown_placeholder(self):
        previous = Job("queue-test", "comic")
        previous.stage = "capturing"
        updated = Job("processing-test", "comic")
        booth = SimpleNamespace(
            auto_flow=SimpleNamespace(queue=SimpleNamespace(get=lambda identity: SimpleNamespace(style_id="comic"))),
            queue_jobs={"queue-test": previous}, active_countdown_job=previous,
            jobs=SimpleNamespace(create=lambda style: updated),
        )
        job, style, meta = _BoothCapturePhotoStore(booth)._prepare_job("queue-test")
        self.assertIsNone(booth.active_countdown_job)
        self.assertIs(job, updated)
        self.assertIs(booth.queue_jobs["queue-test"], updated)

    def worker(self, lines):
        worker = InferWorker()
        worker._proc = Mock()
        worker._proc.stdin = io.BytesIO()
        worker._proc.poll.return_value = None
        worker.start = Mock()
        worker._switch_mode_process = Mock()
        worker._read_line = Mock(side_effect=[PROTOCOL_PREFIX + json.dumps(line) for line in lines])
        return worker

    def test_progress_precedes_final_and_ignores_other_request(self):
        worker = self.worker([
            {"id": 999, "event": "segmentation", "ok": True},
            {"id": 1, "event": "segmentation", "ok": True},
            {"id": 1, "ok": True, "matte_ms": 123},
        ])
        progress = Mock()
        final = worker.seg("capture.jpg", on_progress=progress)
        self.assertEqual(final["matte_ms"], 123)
        progress.assert_called_once()
        request = json.loads(worker._proc.stdin.getvalue())
        self.assertTrue(request["progress"])

    def test_display_error_does_not_break_photo_processing(self):
        worker = self.worker([
            {"id": 1, "event": "segmentation", "ok": True},
            {"id": 1, "ok": True},
        ])
        self.assertTrue(worker.seg("capture.jpg", on_progress=Mock(side_effect=ValueError))["ok"])

    def test_progress_does_not_extend_timeout(self):
        worker = self.worker([{"id": 1, "event": "segmentation", "ok": True}])
        worker._kill_wedged = Mock()
        with patch("infer_client.time.time", side_effect=[0, 0.5, 1.1]):
            with self.assertRaises(InferError):
                worker.seg("capture.jpg", timeout=1, on_progress=Mock())
        worker._kill_wedged.assert_called_once()

    def test_selected_mask_published_before_fine_matte(self):
        with tempfile.TemporaryDirectory() as directory:
            booth = Booth.__new__(Booth)
            booth.store = Store(directory)
            job = Job("test", "comic")
            job.record_id = "SM-2026-test"
            job.stage = "inferring"
            source = Image.new("L", (32, 48), 0)
            source.paste(255, (8, 4, 22, 47))
            output = io.BytesIO()
            source.save(output, "PNG")
            result = {"ok": True, "subject_mask_b64": base64.b64encode(output.getvalue()).decode(),
                      "width": 320, "height": 480, "ep": True, "infer_ms": 275, "person_count": 2}
            booth._publish_segmentation(job, result)
            self.assertEqual(job.stage, "matting")
            self.assertIsNone(job.cutout_url)
            self.assertFalse(job.matte_applied)
            self.assertTrue(job.segmentation["ep"])
            with Image.open(booth.store.root / "masks" / (job.record_id + "-subject.png")) as actual:
                np.testing.assert_array_equal(np.asarray(actual), np.asarray(source))
            booth._publish_segmentation(job, {**result, "infer_ms": 999})
            self.assertEqual(job.infer_ms, 275)


if __name__ == "__main__":
    unittest.main()
