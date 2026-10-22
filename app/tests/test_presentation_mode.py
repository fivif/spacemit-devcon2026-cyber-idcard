# -*- coding: utf-8 -*-
"""现场展示模式：完整展示 / AIGC 8 秒后后台交接。"""

from __future__ import annotations

import json
import os
import tempfile
import time
import unittest
from pathlib import Path
import sys
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "app"))
from app import booth_server


class PresentationModeTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.settings_path = Path(self.temp_dir.name) / "settings.json"
        self.old_settings_path = booth_server.SETTINGS_PATH
        self.old_cache = booth_server._SETTINGS_CACHE
        booth_server.SETTINGS_PATH = self.settings_path
        booth_server._SETTINGS_CACHE = None

    def tearDown(self):
        booth_server.SETTINGS_PATH = self.old_settings_path
        booth_server._SETTINGS_CACHE = self.old_cache
        self.temp_dir.cleanup()

    def test_default_handoff_delay_is_eight_seconds(self):
        with patch.dict(os.environ, {
            "BOOTH_PRESENTATION_MODE": "full",
            "BOOTH_AIGC_HANDOFF_DELAY_SECONDS": "",
        }, clear=False):
            mode, delay = booth_server.presentation_runtime_config()
        self.assertEqual(mode, "full")
        self.assertEqual(delay, 8.0)

    def test_operator_setting_persists_and_updates_runtime(self):
        booth = object.__new__(booth_server.Booth)
        booth.public_config = lambda: {
            "presentation_mode": booth.presentation_mode,
            "handoff_enabled": booth.presentation_mode == "handoff",
            "aigc_handoff_delay_seconds": booth.aigc_handoff_delay_seconds,
        }

        result = booth.set_presentation_config(handoff_enabled=True)

        self.assertEqual(booth.presentation_mode, "handoff")
        self.assertEqual(booth.aigc_handoff_delay_seconds, 8.0)
        self.assertTrue(result["handoff_enabled"])
        saved = json.loads(self.settings_path.read_text(encoding="utf-8"))
        self.assertEqual(saved["presentation_mode"], "handoff")
        self.assertEqual(saved["aigc_handoff_delay_seconds"], 8.0)

    def test_handoff_only_becomes_ready_after_eight_seconds(self):
        self.settings_path.write_text(json.dumps({
            "presentation_mode": "handoff",
            "aigc_handoff_delay_seconds": 8.0,
        }), encoding="utf-8")
        booth_server._SETTINGS_CACHE = None
        job = booth_server.Job("job-1", "style")
        job.stage = "stylizing"
        job.aigc_started_at = time.time() - 7.5
        self.assertFalse(job.snapshot()["handoff_ready"])
        job.aigc_started_at = time.time() - 8.5
        self.assertTrue(job.snapshot()["handoff_ready"])


if __name__ == "__main__":
    unittest.main()
