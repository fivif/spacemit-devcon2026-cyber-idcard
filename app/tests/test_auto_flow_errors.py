# -*- coding: utf-8 -*-
"""T-20 错误契约测试：六类指定错误的 HTTP 映射、中文文案与响应形状。

运行（项目根目录）：
    python3 -m unittest app.tests.test_auto_flow_errors -v

约定：
- 后端映射与文案的权威来源是 `app/auto_flow/errors.py`（T-11 已落地），
  本测试消费它的实际结构做一致性断言，不重复实现映射；
- 前端文案层 `app/static/auto-flow-messages.js`（T-20）按文本包含关系断言；
- 不联网、不写盘。
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

from app.auto_flow import errors

PROJECT_ROOT = Path(__file__).resolve().parents[2]
MESSAGES_JS = PROJECT_ROOT / "app" / "static" / "auto-flow-messages.js"

# T-20 合同点名的六类错误：错误码 -> (HTTP, 中文文案)
REQUIRED_ERRORS: dict[str, tuple[int, str]] = {
    "CAMERA_UNAVAILABLE": (503, "相机暂时不可用，请找现场工作人员。"),
    "QUEUE_EXPIRED": (409, "排队已超时，请重新提交。"),
    "CAPTURE_FAILED": (503, "拍摄失败，未生成照片；请重试或找工作人员。"),
    "AIGC_TIMEOUT": (504, "风格化处理超时，已保留原图结果。"),
    "PHOTO_DELETED": (410, "照片已被删除。"),
    "IDEMPOTENCY_CONFLICT": (409, "该请求编号已用于另一份提交，请刷新后重试。"),
}

CJK_RE = re.compile(r"[\u4e00-\u9fff]")


class RequiredErrorsTest(unittest.TestCase):
    """六类指定错误：HTTP 映射 + 中文文案，直接对 errors.py 断言。"""

    def test_six_codes_present_in_authoritative_table(self):
        for code in REQUIRED_ERRORS:
            with self.subTest(code=code):
                self.assertIn(code, errors.ERROR_SPECS)
                self.assertIn(code, errors.HTTP_STATUS_BY_CODE)
                self.assertIn(code, errors.MESSAGE_BY_CODE)

    def test_six_http_mappings(self):
        for code, (http_status, _) in REQUIRED_ERRORS.items():
            with self.subTest(code=code):
                self.assertEqual(errors.http_status_for(code), http_status)
                self.assertEqual(errors.spec_for(code).http_status, http_status)

    def test_six_chinese_messages_exact(self):
        for code, (_, message) in REQUIRED_ERRORS.items():
            with self.subTest(code=code):
                self.assertEqual(errors.default_message_for(code), message)
                self.assertEqual(errors.spec_for(code).message, message)
                self.assertTrue(CJK_RE.search(message), "文案必须是中文")
                self.assertNotEqual(message, code, "文案不得回显错误码本身")

    def test_error_codes_are_stable_uppercase(self):
        for code in REQUIRED_ERRORS:
            with self.subTest(code=code):
                self.assertRegex(code, r"^[A-Z][A-Z_]+$")


class ErrorPayloadShapeTest(unittest.TestCase):
    """错误响应形状固定为 {"error": {code, message, request_id}}。"""

    def test_error_payload_shape(self):
        payload = errors.error_payload("QUEUE_EXPIRED", "req-0001")
        self.assertEqual(set(payload), {"error"})
        body = payload["error"]
        self.assertEqual(set(body), {"code", "message", "request_id"})
        self.assertEqual(body["code"], "QUEUE_EXPIRED")
        self.assertEqual(body["message"], REQUIRED_ERRORS["QUEUE_EXPIRED"][1])
        self.assertEqual(body["request_id"], "req-0001")

    def test_error_payload_message_override(self):
        payload = errors.error_payload("CAPTURE_FAILED", "req-0002", message="自定义说明")
        self.assertEqual(payload["error"]["message"], "自定义说明")
        self.assertEqual(payload["error"]["code"], "CAPTURE_FAILED")

    def test_api_error_carries_status_and_payload(self):
        for code, (http_status, message) in REQUIRED_ERRORS.items():
            with self.subTest(code=code):
                exc = errors.ApiError(code, request_id="req-0003")
                self.assertEqual(exc.http_status, http_status)
                self.assertEqual(exc.message, message)
                payload = exc.payload()
                self.assertEqual(payload["error"]["code"], code)
                self.assertEqual(payload["error"]["message"], message)
                self.assertEqual(payload["error"]["request_id"], "req-0003")

    def test_unknown_code_is_rejected_loudly(self):
        with self.assertRaises(KeyError):
            errors.spec_for("NOT_A_REAL_CODE")


class FrontendMessagesTest(unittest.TestCase):
    """前端文案层覆盖检查：六类必查，全文与 errors.py 对齐。"""

    @classmethod
    def setUpClass(cls):
        cls.js_text = MESSAGES_JS.read_text(encoding="utf-8")

    def test_js_file_exists_and_is_plain_script(self):
        self.assertTrue(MESSAGES_JS.is_file())
        self.assertIn("messageFor", self.js_text)
        self.assertIn("AutoFlowMessages", self.js_text)
        self.assertIn("FALLBACK", self.js_text)

    def test_js_has_no_external_resources(self):
        lowered = self.js_text.lower()
        self.assertNotIn("http://", lowered)
        self.assertNotIn("https://", lowered)
        # 具体的外部加载模式：CDN 域名、ES module import、网络请求 API
        for token in ("cdnjs", "unpkg", "jsdelivr", "import ", "fetch(", "xmlhttprequest", "require("):
            with self.subTest(token=token):
                self.assertNotIn(token, lowered if token.islower() else self.js_text)

    def test_js_covers_six_required_codes_and_core_messages(self):
        for code, (_, message) in REQUIRED_ERRORS.items():
            with self.subTest(code=code):
                self.assertIn(code, self.js_text)
                self.assertIn(message, self.js_text)

    def test_js_stays_in_sync_with_errors_py(self):
        """JS 覆盖 errors.py 全部错误码，且每条中文文案逐字一致。"""
        for code in errors.ERROR_CODES:
            with self.subTest(code=code):
                self.assertIn(f"{code}:", self.js_text)
                self.assertIn(errors.default_message_for(code), self.js_text)


if __name__ == "__main__":
    unittest.main()
