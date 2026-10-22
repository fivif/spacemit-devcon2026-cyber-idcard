from __future__ import annotations

import io
import sys
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from aigc_client import AIGCError, _request_edit


class RequestErrorTests(unittest.TestCase):
    def request(self):
        return _request_edit(
            b"jpeg", prompt="blue circle", model="test-model", size="1024x1024",
            endpoint_base="https://example.invalid/v1", api_key="private-key", timeout=300)

    def test_socket_and_wrapped_timeouts_are_distinct_from_network_errors(self):
        for failure in (TimeoutError("timed out"), urllib.error.URLError(TimeoutError("timed out"))):
            with self.subTest(failure=type(failure).__name__), patch(
                "aigc_client.urllib.request.urlopen", side_effect=failure
            ) as transport:
                with self.assertRaisesRegex(AIGCError, "请求超时.*300 秒") as raised:
                    self.request()
                self.assertIs(raised.exception.__cause__, failure)
                self.assertTrue(raised.exception.retryable)
                self.assertEqual(transport.call_args.kwargs["timeout"], 300)

    def test_response_read_timeout_is_classified(self):
        with patch("aigc_client.urllib.request.urlopen") as transport:
            transport.return_value.__enter__.return_value.read.side_effect = TimeoutError()
            with self.assertRaisesRegex(AIGCError, "请求超时"):
                self.request()

    def test_other_network_errors_do_not_expose_upstream_details(self):
        with patch("aigc_client.urllib.request.urlopen", side_effect=ConnectionResetError("private-key")):
            with self.assertRaisesRegex(AIGCError, "AIGC 网络请求失败") as raised:
                self.request()
            self.assertNotIn("private-key", str(raised.exception))

    def test_http_errors_keep_the_status_code(self):
        failure = urllib.error.HTTPError("https://example.invalid", 503, "busy", {}, io.BytesIO())
        with patch("aigc_client.urllib.request.urlopen", side_effect=failure):
            with self.assertRaisesRegex(AIGCError, "HTTP 503"):
                self.request()


if __name__ == "__main__":
    unittest.main()
