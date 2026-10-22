from __future__ import annotations

import base64
import io
import json
import sys
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import aigc_client
from aigc_channels import AIGCChannels


class Response:
    def __init__(self, value):
        self.raw = value if isinstance(value, bytes) else json.dumps(value).encode()

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, limit):
        return self.raw[:limit]

    def geturl(self):
        return "https://output.example/result.png"


class ModelScopeTests(unittest.TestCase):
    def test_raw_long_prompt_is_rejected_before_submission(self):
        with patch("aigc_client.urllib.request.urlopen") as opened:
            with self.assertRaisesRegex(aigc_client.AIGCError, "4000"):
                aigc_client._request_modelscope(b"jpeg", prompt="a" * 4001, model="model",
                    endpoint_base="https://api-inference.modelscope.cn/v1", api_key="test", timeout=300)
        opened.assert_not_called()

    def request(self, responses, endpoint="https://api-inference.modelscope.cn/v1"):
        with patch("aigc_client.urllib.request.urlopen", side_effect=responses) as opened:
            with patch("aigc_client.time.sleep"):
                image = aigc_client._request_edit(
                    b"source-jpeg", prompt="keep the person", model="Qwen/Qwen-Image-2.1",
                    size="1024x1536", endpoint_base=endpoint, api_key="test-secret", timeout=120)
        return image, [entry.args[0] for entry in opened.call_args_list]

    def png(self):
        buffer = io.BytesIO()
        Image.new("RGB", (32, 48), "navy").save(buffer, "PNG")
        return buffer.getvalue()

    def test_async_submission_polling_and_download_without_key(self):
        image, requests = self.request([
            Response({"task_id": "test/task"}),
            Response({"task_status": "PENDING"}),
            Response({"task_status": "RUNNING"}),
            Response({"task_status": "SUCCEED", "output_images": ["https://output.example/result.png"]}),
            Response(self.png()),
        ])
        self.assertEqual(image.size, (32, 48))
        self.assertEqual(requests[0].full_url, "https://api-inference.modelscope.cn/v1/images/generations")
        self.assertEqual(requests[0].get_header("X-modelscope-async-mode"), "true")
        payload = json.loads(requests[0].data)
        self.assertEqual(payload["model"], "Qwen/Qwen-Image-2.1")
        self.assertEqual(payload["image_url"], ["data:image/jpeg;base64," + base64.b64encode(b"source-jpeg").decode()])
        self.assertNotIn("size", payload)
        self.assertEqual(requests[1].full_url, "https://api-inference.modelscope.cn/v1/tasks/test%2Ftask")
        self.assertEqual(requests[1].get_header("X-modelscope-task-type"), "image_generation")
        self.assertEqual(requests[1].get_header("Authorization"), "Bearer test-secret")
        self.assertIsNone(requests[-1].get_header("Authorization"))

    def test_root_url_is_normalized(self):
        _, requests = self.request([
            Response({"task_id": "test"}),
            Response({"task_status": "SUCCEED", "output_images": ["https://output.example/result.png"]}),
            Response(self.png()),
        ], endpoint="https://api-inference.modelscope.cn/")
        self.assertEqual(requests[0].full_url, "https://api-inference.modelscope.cn/v1/images/generations")
        raw = {"id": "mod", "name": "mod", "base_url": "https://api-inference.modelscope.cn/",
               "api_key": "test-secret", "models": ["Qwen/Qwen-Image-2.1"]}
        saved = AIGCChannels._validate({"channels": [raw]}, {"channels": []})
        self.assertEqual(saved["channels"][0]["base_url"], "https://api-inference.modelscope.cn/v1")
        self.assertEqual(saved["channels"][0]["timeout"], 300)

    def test_failure_and_cancellation_are_reported(self):
        for status in ("FAILED", "CANCELED", "CANCELLED", "unexpected"):
            with self.subTest(status=status), self.assertRaises(aigc_client.AIGCError):
                self.request([Response({"task_id": "test"}), Response({"task_status": status})])

    def test_missing_or_malformed_results_are_rejected(self):
        for result in ({}, [], {"task_id": 123}, {"task_id": ""}):
            with self.subTest(result=result), self.assertRaises(aigc_client.AIGCError):
                self.request([Response(result)])
        for urls in ([], ["http://output.example/result.png"], ["file:///tmp/photo.png"], [123]):
            with self.subTest(urls=urls), self.assertRaises(aigc_client.AIGCError):
                self.request([Response({"task_id": "test"}),
                              Response({"task_status": "SUCCEED", "output_images": urls})])

    def test_pending_task_has_total_timeout(self):
        with patch("aigc_client.time.monotonic", side_effect=[0, 1, 2, 121]):
            with self.assertRaisesRegex(aigc_client.AIGCError, "超时"):
                self.request([Response({"task_id": "test"}), Response({"task_status": "RUNNING"})])

    def test_http_error_is_sanitized(self):
        error = urllib.error.HTTPError("https://api-inference.modelscope.cn/v1/images/generations",
                                       429, "secret provider body", {}, None)
        with self.assertRaisesRegex(aigc_client.AIGCError, "HTTP 429") as caught:
            self.request([error])
        self.assertNotIn("secret", str(caught.exception))
        self.assertTrue(caught.exception.retryable)

    def test_broken_download_is_reported(self):
        with self.assertRaisesRegex(aigc_client.AIGCError, "下载或读取失败"):
            self.request([Response({"task_id": "test"}),
                          Response({"task_status": "SUCCEED", "output_images": ["https://output.example/result.png"]}),
                          Response(b"not an image")])


if __name__ == "__main__":
    unittest.main()
