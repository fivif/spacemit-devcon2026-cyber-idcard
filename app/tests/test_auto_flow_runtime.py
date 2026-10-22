# -*- coding: utf-8 -*-
"""自动流程生产接线的最小真实 HTTP 回归测试。"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parents[2]
import sys

for candidate in (str(PROJECT_ROOT), str(PROJECT_ROOT / "app")):
    if candidate not in sys.path:
        sys.path.insert(0, candidate)

import booth_server  # noqa: E402
from app.auto_flow.http_handlers import QrUnavailableError  # noqa: E402


class AutoFlowRuntimeHttpTest(unittest.TestCase):
    """使用真实 Handler/socket，隔离队列开关与数据目录。"""

    def setUp(self) -> None:
        self._environment = {
            name: os.environ.get(name)
            for name in (
                "BOOTH_QUEUE_ENABLED",
                "BOOTH_PHOTO_RETENTION_DAYS",
                "BOOTH_CAMERA",
                "BOOTH_PUBLIC_HOST",
            )
        }
        os.environ.pop("BOOTH_QUEUE_ENABLED", None)
        os.environ.pop("BOOTH_PHOTO_RETENTION_DAYS", None)
        os.environ["BOOTH_PUBLIC_HOST"] = "https://public.example.test"
        os.environ["BOOTH_CAMERA"] = "mock"
        self.data_dir = Path(tempfile.mkdtemp(prefix="auto-flow-runtime-"))
        self._start_server()
        self.addCleanup(self._restore)

    def _restore(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.server_thread.join(timeout=5)
        shutil.rmtree(self.data_dir, ignore_errors=True)
        for name, value in self._environment.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value

    def _start_server(self) -> None:
        booth_server.Handler.booth = booth_server.Booth(data_dir=self.data_dir)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), booth_server.Handler)
        self.server.daemon_threads = True
        self.server_thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.server_thread.start()
        host, port = self.server.server_address
        self.base_url = "http://%s:%d" % (host, port)

    def _request(self, method: str, path: str, body=None, headers=None):
        data = None if body is None else json.dumps(body).encode("utf-8")
        request = urllib.request.Request(
            self.base_url + path,
            data=data,
            headers={"Content-Type": "application/json", **(headers or {})},
            method=method,
        )
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return response.status, response.headers, response.read()
        except urllib.error.HTTPError as error:
            return error.code, error.headers, error.read()

    def _json_request(self, method: str, path: str, body=None, headers=None):
        status, response_headers, raw = self._request(method, path, body, headers)
        return status, response_headers, json.loads(raw.decode("utf-8"))

    def test_join_page_and_qr_use_current_public_base_with_queue_off(self):
        os.environ.pop("BOOTH_QUEUE_ENABLED", None)
        status, headers, body = self._request("GET", "/join")
        self.assertEqual(status, 200)
        self.assertIn("text/html", headers.get("Content-Type", ""))
        self.assertIn("报名", body.decode("utf-8"))

        captured = []

        def fake_qr(url):
            captured.append(url)
            return b"fake-png"

        with patch("app.auto_flow.http_handlers.join_qr_png", side_effect=fake_qr):
            status, headers, body = self._request("GET", "/api/join-qr.png")
        self.assertEqual(status, 200)
        self.assertEqual(headers.get("Content-Type"), "image/png")
        self.assertEqual(body, b"fake-png")
        self.assertEqual(captured, ["%s/join" % booth_server.public_base()])
        self.assertEqual(captured[0], "https://public.example.test/join")

    def test_qr_failure_is_frontend_readable(self):
        with patch(
            "app.auto_flow.http_handlers.join_qr_png",
            side_effect=QrUnavailableError("测试不可用"),
        ):
            status, headers, payload = self._json_request("GET", "/api/join-qr.png")
        self.assertEqual(status, 503)
        self.assertIn("application/json", headers.get("Content-Type", ""))
        self.assertEqual(payload["ok"], False)
        self.assertEqual(payload["error"], "qr_unavailable")

    def test_queue_routes_stay_404_when_switch_is_off(self):
        os.environ.pop("BOOTH_QUEUE_ENABLED", None)
        self.assertIsNone(booth_server.Handler.booth.auto_flow_services())
        for method, path, body in (
            ("POST", "/api/queue", {}),
            ("GET", "/api/queue/queue-id", None),
            ("POST", "/api/queue/queue-id/start", {}),
        ):
            status, _, payload = self._json_request(method, path, body)
            self.assertEqual(status, 404)
            self.assertEqual(payload["error"], "not_found")

    def test_queue_initializes_and_rejects_missing_consent_or_session(self):
        os.environ["BOOTH_QUEUE_ENABLED"] = "1"
        os.environ["BOOTH_PHOTO_RETENTION_DAYS"] = "30"
        self.server.shutdown()
        self.server.server_close()
        self.server_thread.join(timeout=5)
        self._start_server()
        self.assertIsNotNone(booth_server.Handler.booth.auto_flow_services())

        status, _, payload = self._json_request(
            "POST", "/api/queue", {"request_id": "runtime-missing-consent", "style_id": "neon"}
        )
        self.assertEqual(status, 400)
        self.assertEqual(payload["error"]["code"], "INVALID_REQUEST")

        for method, path, body in (
            ("GET", "/api/queue/unknown", None),
            ("POST", "/api/queue/unknown/start", {"capture_consent_id": "missing"}),
        ):
            status, _, payload = self._json_request(method, path, body)
            self.assertEqual(status, 404)
            self.assertEqual(payload["error"]["code"], "QUEUE_NOT_FOUND")


if __name__ == "__main__":
    unittest.main()
