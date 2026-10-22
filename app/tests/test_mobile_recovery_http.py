from __future__ import annotations

import json
import urllib.request
import warnings
from unittest.mock import patch

from app.tests.test_auto_flow_http import AutoFlowHttpTestCase, booth_server
from app.auto_flow.http_handlers import QueueSessionRegistry
from app.auto_flow.recovery_service import RecoveryRegistry
from app.auto_flow.models import QueueStatus
from store import Record


class MobileRecoveryHttpTest(AutoFlowHttpTestCase):
    def setUp(self):
        super().setUp()
        self.enable_recovery()

    def enable_recovery(self):
        self.services.sessions = QueueSessionRegistry(self.tmp / "auto_flow" / "queue_sessions.json")
        self.services.recovery = RecoveryRegistry(self.tmp / "auto_flow" / "mobile_recovery.json")
        self.services.photo_service = booth_server._BoothPhotoServiceAdapter(self.booth, {})

    def submit_with_cookie(self, request_id="recovery-request", cookie=None):
        consent_id = self.make_consent()
        self.submitted_consent_id = consent_id
        body = {"request_id": request_id, "style_id": "neon", "capture_consent_id": consent_id}
        headers = {"Content-Type": "application/json", "X-Forwarded-Proto": "https"}
        if cookie:
            headers["Cookie"] = cookie
        request = urllib.request.Request(self.base_url + "/api/queue", method="POST",
                                         data=json.dumps(body).encode(), headers=headers)
        with urllib.request.urlopen(request, timeout=10) as response:
            payload = json.load(response)
            cookie_header = response.headers.get("Set-Cookie", "")
            self.assertIn("HttpOnly", cookie_header)
            self.assertIn("SameSite=Lax", cookie_header)
            self.assertIn("Secure", cookie_header)
            self.assertEqual(response.headers["Referrer-Policy"], "no-referrer")
        return payload, cookie_header.split(";", 1)[0]

    def test_original_flow_and_cookie_restore_use_same_authorization(self):
        payload, cookie = self.submit_with_cookie()
        queue_id = payload["entry"]["queue_id"]
        self.assertRegex(payload["recovery_url"], r"^/join#restore=[A-Za-z0-9_-]{32}$")
        status, restored = self.http("GET", "/api/recovery/current", headers={"Cookie": cookie})
        self.assertEqual(status, 200)
        self.assertEqual(restored["entry"]["queue_id"], queue_id)
        self.assertEqual(restored["session"], payload["session"])
        self.assertEqual(restored["consent_id"], self.submitted_consent_id)
        self.services.queue.call_next()
        self.services.camera_probe = lambda: True
        status, started = self.http("POST", "/api/queue/" + queue_id + "/start",
                                    {"capture_consent_id": restored["consent_id"]},
                                    headers={"X-Queue-Session": payload["session"]})
        self.assertEqual(status, 202)
        self.assertEqual(started["entry"]["status"], "ready")

    def test_link_is_read_only_and_does_not_rebind_other_browser(self):
        payload, cookie = self.submit_with_cookie()
        token = payload["recovery_url"].split("=", 1)[1]
        status, restored = self.http("GET", "/api/recovery/" + token)
        self.assertEqual(status, 200)
        self.assertTrue(restored["read_only"])
        self.assertNotIn("session", restored)
        self.assertNotIn("consent_id", restored)
        self.assertNotIn("queue_id", restored["entry"])
        self.assertNotIn("request_id", restored["entry"])
        self.assertEqual(self.http("GET", "/api/recovery/current")[0], 404)
        queue_id = payload["entry"]["queue_id"]
        self.assertEqual(self.http("POST", "/api/queue/" + queue_id + "/start",
                                    {"capture_consent_id": self.submitted_consent_id},
                                    headers={"X-Queue-Session": token})[0], 404)
        self.assertEqual(self.services.queue.get(queue_id).status, QueueStatus.QUEUED)

    def test_session_and_recovery_survive_registry_restart(self):
        payload, cookie = self.submit_with_cookie()
        self.enable_recovery()
        status, restored = self.http("GET", "/api/recovery/current", headers={"Cookie": cookie})
        self.assertEqual(status, 200)
        self.assertEqual(restored["session"], payload["session"])
        self.assertEqual(self.http("GET", "/api/queue/" + payload["entry"]["queue_id"],
                                   headers={"X-Queue-Session": payload["session"]})[0], 200)

    def test_new_registration_keeps_old_link(self):
        first, cookie = self.submit_with_cookie("first")
        second, reused_cookie = self.submit_with_cookie("second", cookie)
        self.assertEqual(cookie, reused_cookie)
        status, restored = self.http("GET", "/api/recovery/current", headers={"Cookie": cookie})
        self.assertEqual(status, 200)
        self.assertEqual(restored["entry"]["queue_id"], second["entry"]["queue_id"])
        first_token = first["recovery_url"].split("=", 1)[1]
        status, historical = self.http("GET", "/api/recovery/" + first_token)
        self.assertEqual(status, 200)
        self.assertEqual(historical["entry"]["display_number"], first["entry"]["display_number"])

    def test_recovery_storage_failure_does_not_block_registration(self):
        with patch.object(self.services.recovery, "bind", side_effect=OSError("disk error")):
            status, payload = self.submit("disk-failure", consent_id=self.make_consent())
        self.assertEqual(status, 201)
        self.assertNotIn("recovery_url", payload)
        self.assertTrue(self.services.sessions.check(payload["session"], payload["entry"]["queue_id"]))

    def test_session_write_failure_keeps_registration_replay_and_start_working(self):
        consent_id = self.make_consent()
        with warnings.catch_warnings(record=True) as captured:
            warnings.simplefilter("always", RuntimeWarning)
            with patch("app.auto_flow.http_handlers._write_document", side_effect=OSError("private-storage-sentinel")):
                status, payload = self.submit("session-write-failure", consent_id=consent_id)
                self.assertEqual(status, 201)
                replay_status, replay = self.submit("session-write-failure", consent_id=consent_id)
        self.assertEqual(replay_status, 200)
        self.assertEqual(replay["session"], payload["session"])
        self.assertIn("recovery_url", payload)
        session_path = self.tmp / "auto_flow" / "queue_sessions.json"
        self.assertFalse(session_path.exists())
        self.assertEqual(len(captured), 1)
        warning = str(captured[0].message)
        self.assertNotIn(payload["session"], warning)
        self.assertNotIn("private-storage-sentinel", warning)
        queue_id = payload["entry"]["queue_id"]
        headers = {"X-Queue-Session": payload["session"]}
        self.assertEqual(self.http("GET", "/api/queue/" + queue_id, headers=headers)[0], 200)
        self.services.queue.call_next()
        self.services.camera_probe = lambda: True
        status, started = self.http("POST", "/api/queue/" + queue_id + "/start",
                                    {"capture_consent_id": consent_id}, headers=headers)
        self.assertEqual(status, 202)
        self.assertEqual(started["entry"]["status"], "ready")
        self.assertFalse(session_path.exists())

    def test_session_write_failure_does_not_drop_previously_persisted_sessions(self):
        first, cookie = self.submit_with_cookie("durable-first")
        session_path = self.tmp / "auto_flow" / "queue_sessions.json"
        original = session_path.read_bytes()
        with warnings.catch_warnings(record=True) as captured:
            warnings.simplefilter("always", RuntimeWarning)
            with patch("app.auto_flow.http_handlers._write_document", side_effect=OSError("failed")):
                status, second = self.submit("volatile-second", consent_id=self.make_consent())
        self.assertEqual(status, 201)
        self.assertEqual(len(captured), 1)
        self.assertEqual(session_path.read_bytes(), original)
        self.assertTrue(self.services.sessions.check(first["session"], first["entry"]["queue_id"]))
        self.assertTrue(self.services.sessions.check(second["session"], second["entry"]["queue_id"]))
        self.assertEqual(QueueSessionRegistry(session_path).get_existing(first["entry"]["queue_id"]), first["session"])
        self.assertIsNone(QueueSessionRegistry(session_path).get_existing(second["entry"]["queue_id"]))
        status, restored = self.http("GET", "/api/recovery/current", headers={"Cookie": cookie})
        self.assertEqual(status, 200)
        self.assertEqual(restored["session"], first["session"])

    def test_old_session_attach_requires_control_session(self):
        payload, cookie = self.submit_with_cookie()
        queue_id = payload["entry"]["queue_id"]
        self.assertEqual(self.http("POST", "/api/recovery/attach", {"queue_id": queue_id})[0], 404)
        status, attached = self.http("POST", "/api/recovery/attach", {"queue_id": queue_id},
                                      headers={"X-Queue-Session": payload["session"]})
        self.assertEqual(status, 200)
        self.assertEqual(attached["recovery_url"], payload["recovery_url"])

    def test_completed_photo_mapping_reads_disk_after_restart(self):
        payload, cookie = self.submit_with_cookie()
        queue_id = payload["entry"]["queue_id"]
        self.services.queue.call_next()
        self.services.queue.start(queue_id, self.submitted_consent_id)
        self.services.queue.mark_capturing(queue_id)
        self.services.queue.mark_processing(queue_id)
        self.services.queue.mark_ready_for_pickup(queue_id)
        record = Record(id="SM-2026-0001", created_at="2026-10-13T10:00:00",
                        prompt="", ep=False, infer_ms=0, person_count=1, degraded=False,
                        seed=1, card="test", style="neon", token="abcdefghijklmnopqrstuv",
                        extra={"queue_id": queue_id, "job": "different-job-id"})
        self.booth.store.append(record)
        photo_path = self.booth.store.photo_path(record.id)
        photo_path.write_bytes(b"isolated-test-image")
        self.install_services()
        self.enable_recovery()
        status, restored = self.http("GET", "/api/recovery/current", headers={"Cookie": cookie})
        self.assertEqual(status, 200)
        self.assertEqual(restored["pickup_url"], "/p/" + record.token)
        self.assertEqual(restored["entry"]["status"], "ready_for_pickup")
        status, job = self.http("GET", "/api/job/" + queue_id,
                                headers={"X-Queue-Session": payload["session"]})
        self.assertEqual(status, 200)
        self.assertEqual(job["job"]["photo_token"], record.token)
        with patch.object(self.booth.store, "photo_path", return_value=self.tmp / "missing.jpg"):
            token = payload["recovery_url"].split("=", 1)[1]
            self.assertEqual(self.http("GET", "/api/recovery/" + token)[0], 404)

    def test_unknown_and_expired_links_do_not_reveal_queue(self):
        payload, cookie = self.submit_with_cookie()
        self.assertEqual(self.http("GET", "/api/recovery/" + "z" * 32)[0], 404)
        self.services.recovery._clock = lambda: 99999999999
        token = payload["recovery_url"].split("=", 1)[1]
        self.assertEqual(self.http("GET", "/api/recovery/" + token)[0], 404)
        self.assertEqual(self.http("GET", "/api/recovery/current", headers={"Cookie": cookie})[0], 404)

    def test_temporary_recovery_read_error_is_retryable_without_internal_details(self):
        payload, cookie = self.submit_with_cookie()
        with patch.object(self.services.recovery, "current", side_effect=OSError("private-read-sentinel")):
            with patch.object(booth_server, "log") as captured:
                status, response = self.http("GET", "/api/recovery/current", headers={"Cookie": cookie})
        self.assertEqual(status, 503)
        self.assertEqual(response["error"], "recovery_unavailable")
        self.assertNotIn("private-read-sentinel", json.dumps(response))
        self.assertNotIn("private-read-sentinel", str(captured.call_args_list))
        status, recovered = self.http("GET", "/api/recovery/current", headers={"Cookie": cookie})
        self.assertEqual(status, 200)
        self.assertEqual(recovered["session"], payload["session"])

    def test_photo_read_error_does_not_misreport_expired_recovery_link(self):
        payload, cookie = self.submit_with_cookie()
        queue_id = payload["entry"]["queue_id"]
        self.services.queue.call_next()
        self.services.queue.start(queue_id, self.submitted_consent_id)
        self.services.queue.mark_capturing(queue_id)
        self.services.queue.mark_processing(queue_id)
        self.services.queue.mark_ready_for_pickup(queue_id)
        token = payload["recovery_url"].split("=", 1)[1]
        with patch.object(self.booth.store, "all", side_effect=OSError("private-photo-sentinel")):
            for path, headers in (("/api/recovery/current", {"Cookie": cookie}),
                                  ("/api/recovery/" + token, {})):
                status, response = self.http("GET", path, headers=headers)
                self.assertEqual(status, 503)
                self.assertEqual(response["error"], "recovery_unavailable")
                self.assertNotIn("private-photo-sentinel", json.dumps(response))
        self.assertEqual(self.services.queue.get(queue_id).status, QueueStatus.READY_FOR_PICKUP)
