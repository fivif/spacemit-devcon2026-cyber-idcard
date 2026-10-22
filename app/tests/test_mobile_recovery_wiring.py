"""真实装配入口的恢复故障降级测试，仅使用临时存储与回环 HTTP。"""

from __future__ import annotations

import os
from types import SimpleNamespace
from unittest.mock import patch

from app.auto_flow.config import CaptureConfig, QueueConfig
from app.auto_flow.recovery_service import RecoveryRegistry
from app.tests.test_auto_flow_http import AutoFlowHttpTestCase, booth_server, setUpModule


class MobileRecoveryWiringTest(AutoFlowHttpTestCase):
    def assemble(self):
        style_source = self.services.style_source
        config = SimpleNamespace(
            queue=QueueConfig(
                enabled=True,
                max_active=None,
                start_timeout_seconds=120,
                idempotency_ttl_seconds=86400,
                request_body_max_bytes=65536,
                optional_fields_allowlist=("name",),
            ),
            capture=CaptureConfig(countdown_seconds=3),
            gesture=SimpleNamespace(enabled=False),
        )
        with patch.dict(os.environ, {"BOOTH_QUEUE_ENABLED": "true"}):
            with patch("app.auto_flow.config.load_config", return_value=config):
                with patch.object(self.booth, "_start_auto_flow_tick_thread") as start_tick:
                    with patch.object(booth_server, "log") as log:
                        self.booth._init_auto_flow()
        start_tick.assert_called_once()
        self.services = self.booth.auto_flow
        self.services.style_source = style_source
        self.assertIsNotNone(self.services.scheduler)
        self.assertIsNotNone(self.services.countdown)
        self.assertIsNotNone(self.services.bridge)
        return [str(call.args[0]) for call in log.call_args_list]

    def corrupt(self, filename):
        path = self.tmp / "auto_flow" / filename
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = b"{private-corrupt-storage-sentinel"
        path.write_bytes(payload)
        return path, payload

    def assert_original_flow_works(self, request_id):
        consent_id = self.make_consent()
        status, payload = self.submit(request_id, consent_id=consent_id)
        self.assertEqual(status, 201, payload)
        queue_id = payload["entry"]["queue_id"]
        headers = {"X-Queue-Session": payload["session"]}
        self.assertEqual(self.http("GET", "/api/queue/" + queue_id, headers=headers)[0], 200)
        self.services.queue.call_next()
        self.services.camera_probe = lambda: True
        status, started = self.http("POST", "/api/queue/" + queue_id + "/start",
                                    {"capture_consent_id": consent_id}, headers=headers)
        self.assertEqual(status, 202, started)
        self.assertEqual(started["entry"]["status"], "ready")
        return payload

    def assert_warning_is_sanitized(self, logs, prefix, exception_type):
        warnings = [message for message in logs if message.startswith(prefix)]
        self.assertEqual(len(warnings), 1)
        self.assertEqual(warnings[0].rsplit("：", 1)[-1], exception_type)
        self.assertNotIn("private-corrupt-storage-sentinel", warnings[0])
        self.assertNotIn(str(self.tmp), warnings[0])

    def test_corrupt_recovery_preserves_file_and_original_flow(self):
        recovery_path, original = self.corrupt("mobile_recovery.json")
        logs = self.assemble()
        self.assertIsNone(self.services.recovery)
        self.assert_warning_is_sanitized(logs, "备用恢复暂不可用", "JSONDecodeError")
        self.assertEqual(self.http("GET", "/api/recovery/current")[0], 404)
        payload = self.assert_original_flow_works("corrupt-recovery")
        self.assertNotIn("recovery_url", payload)
        self.assertTrue((self.tmp / "auto_flow" / "queue_sessions.json").is_file())
        self.assertEqual(recovery_path.read_bytes(), original)

    def test_corrupt_sessions_do_not_reauthorize_old_browser_and_allow_new_flow(self):
        status, old = self.submit("old-binding", consent_id=self.make_consent())
        self.assertEqual(status, 201)
        recovery = RecoveryRegistry(self.tmp / "auto_flow" / "mobile_recovery.json")
        binding = recovery.bind(old["entry"]["queue_id"])
        session_path, original = self.corrupt("queue_sessions.json")
        logs = self.assemble()
        self.assertIsNotNone(self.services.recovery)
        self.assert_warning_is_sanitized(logs, "拍摄会话暂不能持久保存", "JSONDecodeError")
        cookie = "booth_browser=" + binding["browser_token"]
        with patch.object(self.services.sessions, "issue", side_effect=AssertionError("不得新授权")):
            with patch.object(self.services.sessions, "issue_volatile", side_effect=AssertionError("不得新授权")):
                status, payload = self.http("GET", "/api/recovery/current", headers={"Cookie": cookie})
        self.assertEqual(status, 404, payload)
        self.assertIsNone(self.services.sessions.get_existing(old["entry"]["queue_id"]))
        self.assertFalse(self.services.sessions.check(old["session"], old["entry"]["queue_id"]))
        self.services.queue.mark_cancelled(old["entry"]["queue_id"], reason="隔离测试释放旧预约")
        self.assert_original_flow_works("new-after-corrupt-sessions")
        self.assertEqual(session_path.read_bytes(), original)

    def test_both_corrupt_registries_preserve_files_and_original_flow(self):
        session_path, session_bytes = self.corrupt("queue_sessions.json")
        recovery_path, recovery_bytes = self.corrupt("mobile_recovery.json")
        logs = self.assemble()
        self.assert_warning_is_sanitized(logs, "拍摄会话暂不能持久保存", "JSONDecodeError")
        self.assert_warning_is_sanitized(logs, "备用恢复暂不可用", "JSONDecodeError")
        self.assertIsNone(self.services.recovery)
        payload = self.assert_original_flow_works("both-corrupt")
        self.assertNotIn("recovery_url", payload)
        self.assertEqual(session_path.read_bytes(), session_bytes)
        self.assertEqual(recovery_path.read_bytes(), recovery_bytes)

    def test_unreadable_registry_paths_preserve_original_flow(self):
        session_path = self.tmp / "auto_flow" / "queue_sessions.json"
        recovery_path = self.tmp / "auto_flow" / "mobile_recovery.json"
        session_path.mkdir(parents=True)
        recovery_path.mkdir()
        self.assemble()
        self.assertIsNone(self.services.recovery)
        payload = self.assert_original_flow_works("unreadable-registries")
        self.assertNotIn("recovery_url", payload)
        self.assertTrue(session_path.is_dir())
        self.assertTrue(recovery_path.is_dir())
