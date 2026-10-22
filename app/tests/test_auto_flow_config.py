# -*- coding: utf-8 -*-
"""T-11 配置与安全默认值测试。

运行（项目根目录）：
    python3 -m unittest app.tests.test_auto_flow_config -v

约定：
- 只注入环境字典（load_config(env=...)），不读取、不修改机器真实环境变量；
- 不联网、不写盘；路径校验只读本测试文件自身与 app/data/style-catalog.json。
"""

from __future__ import annotations

import unittest
from pathlib import Path

from app.auto_flow import errors
from app.auto_flow.config import DEFAULT_CATALOG_PATH, ConfigError, load_config

PROJECT_ROOT = Path(__file__).resolve().parents[2]
REAL_FILE = Path(__file__).resolve()  # 一定存在的文件，用作合法路径
MISSING_FILE = PROJECT_ROOT / "app" / "auto_flow" / "definitely-missing.onnx"


def _prod(**overrides: str) -> dict[str, str]:
    """生产模式环境：默认给足保留策略，避免无关用例被保留策略门先拦下。"""
    env = {"BOOTH_PHOTO_RETENTION_DAYS": "30"}
    env.update(overrides)
    return env


def _test_mode(**overrides: str) -> dict[str, str]:
    env = {"BOOTH_TEST_MODE": "1"}
    env.update(overrides)
    return env


def _load_prod(**overrides: str):
    return load_config(env=_prod(**overrides))


def _load_test(**overrides: str):
    return load_config(env=_test_mode(**overrides), allow_test_mode=True)


class DefaultsTest(unittest.TestCase):
    """spec Configuration 表的默认值；四扇功能门默认全关。"""

    def test_feature_switches_default_off(self):
        cfg = _load_test()
        self.assertFalse(cfg.queue.enabled)
        self.assertFalse(cfg.aigc.enabled)
        self.assertFalse(cfg.gesture.enabled)
        self.assertFalse(cfg.printer.enabled)

    def test_production_defaults_with_retention(self):
        cfg = _load_prod()
        self.assertFalse(cfg.queue.enabled)
        self.assertFalse(cfg.aigc.enabled)
        self.assertFalse(cfg.gesture.enabled)
        self.assertFalse(cfg.printer.enabled)
        self.assertTrue(cfg.logging.redact_tokens)
        self.assertEqual(cfg.storage.photo_retention_days, 30)
        self.assertFalse(cfg.test_mode)
        self.assertFalse(cfg.test_adapters_allowed)
        self.assertFalse(cfg.testing)

    def test_default_values_follow_spec_table(self):
        cfg = _load_test()
        self.assertEqual(cfg.server.host, "0.0.0.0")
        self.assertEqual(cfg.server.port, 8090)
        self.assertIsNone(cfg.queue.max_active)
        self.assertEqual(cfg.queue.start_timeout_seconds, 120)
        self.assertEqual(cfg.queue.idempotency_ttl_seconds, 86400)
        self.assertEqual(cfg.queue.request_body_max_bytes, 8192)
        self.assertEqual(cfg.queue.optional_fields_allowlist, ())
        self.assertEqual(cfg.capture.countdown_seconds, 8)
        self.assertEqual(cfg.camera.health_timeout_seconds, 2.0)
        self.assertIsNone(cfg.gesture.model_path)
        self.assertEqual(cfg.gesture.stable_frames, 10)
        self.assertAlmostEqual(cfg.gesture.minimum_confidence, 0.90)
        self.assertTrue(cfg.gesture.require_single_person)
        self.assertTrue(cfg.gesture.require_in_frame)
        self.assertIsNone(cfg.aigc.base_url)
        self.assertIsNone(cfg.aigc.api_key_file)
        self.assertEqual(cfg.aigc.model, "gpt-image-1")
        self.assertEqual(cfg.aigc.timeout_seconds, 60)
        self.assertEqual(cfg.aigc.max_concurrency, 1)
        self.assertEqual(cfg.aigc.max_upload_bytes, 20_971_520)
        self.assertFalse(cfg.aigc.terms_approved)
        self.assertEqual(cfg.printer.backend, "none")
        self.assertTrue(cfg.logging.redact_tokens)
        self.assertEqual(cfg.aigc.catalog_path, DEFAULT_CATALOG_PATH)
        self.assertTrue(cfg.aigc.catalog_path.is_file())

    def test_explicit_countdown_configuration_overrides_eight_second_default(self):
        for seconds in (3, 8):
            with self.subTest(seconds=seconds):
                cfg = _load_prod(BOOTH_CAPTURE_COUNTDOWN_SECONDS=str(seconds))
                self.assertEqual(cfg.capture.countdown_seconds, seconds)

    def test_default_load_does_not_read_real_environment(self):
        # 注入空字典：除保留策略门外不受机器真实变量影响
        with self.assertRaises(ConfigError) as ctx:
            load_config(env={})
        self.assertIn("BOOTH_PHOTO_RETENTION_DAYS", str(ctx.exception))


class TestModeContractTest(unittest.TestCase):
    """测试模式只允许测试启动器打开；生产配置必须拒绝。"""

    def test_production_rejects_test_mode(self):
        with self.assertRaises(ConfigError) as ctx:
            load_config(env={"BOOTH_TEST_MODE": "1"})
        message = str(ctx.exception)
        self.assertIn("BOOTH_TEST_MODE", message)
        self.assertIn("拒绝", message)

    def test_production_rejects_fake_adapters(self):
        with self.assertRaises(ConfigError) as ctx:
            load_config(env={"BOOTH_ALLOW_TEST_ADAPTERS": "1"})
        self.assertIn("BOOTH_ALLOW_TEST_ADAPTERS", str(ctx.exception))

    def test_production_rejects_test_flags_even_with_retention(self):
        env = {"BOOTH_PHOTO_RETENTION_DAYS": "30", "BOOTH_ALLOW_TEST_ADAPTERS": "yes"}
        with self.assertRaises(ConfigError):
            load_config(env=env)

    def test_test_launcher_may_open_test_mode(self):
        env = {"BOOTH_TEST_MODE": "1", "BOOTH_ALLOW_TEST_ADAPTERS": "1"}
        cfg = load_config(env=env, allow_test_mode=True)
        self.assertTrue(cfg.test_mode)
        self.assertTrue(cfg.test_adapters_allowed)
        self.assertTrue(cfg.testing)

    def test_allow_test_mode_alone_keeps_production_semantics(self):
        # 不开测试开关时仍是生产语义：缺保留策略必须报错
        with self.assertRaises(ConfigError) as ctx:
            load_config(env={}, allow_test_mode=True)
        self.assertIn("BOOTH_PHOTO_RETENTION_DAYS", str(ctx.exception))

    def test_test_mode_does_not_relax_type_validation(self):
        with self.assertRaises(ConfigError):
            load_config(env=_test_mode(BOOTH_QUEUE_MAX_ACTIVE="-3"), allow_test_mode=True)


class RetentionPolicyTest(unittest.TestCase):
    """照片保留策略：生产必须有，测试模式（临时存储）可省。"""

    def test_missing_retention_blocks_production_startup(self):
        with self.assertRaises(ConfigError) as ctx:
            load_config(env={})
        message = str(ctx.exception)
        self.assertIn("BOOTH_PHOTO_RETENTION_DAYS", message)
        self.assertIn("保留", message)

    def test_missing_retention_allowed_in_test_mode(self):
        cfg = _load_test()
        self.assertIsNone(cfg.storage.photo_retention_days)

    def test_retention_positive_days_parsed(self):
        self.assertEqual(_load_prod(BOOTH_PHOTO_RETENTION_DAYS="7").storage.photo_retention_days, 7)

    def test_retention_zero_or_negative_rejected(self):
        for raw in ("0", "-1"):
            with self.subTest(raw=raw), self.assertRaises(ConfigError) as ctx:
                _load_prod(BOOTH_PHOTO_RETENTION_DAYS=raw)
            self.assertIn("BOOTH_PHOTO_RETENTION_DAYS", str(ctx.exception))


class NumericValidationTest(unittest.TestCase):
    def test_negative_capacity_rejected(self):
        with self.assertRaises(ConfigError) as ctx:
            _load_prod(BOOTH_QUEUE_MAX_ACTIVE="-1")
        self.assertIn("BOOTH_QUEUE_MAX_ACTIVE", str(ctx.exception))

    def test_zero_capacity_rejected(self):
        with self.assertRaises(ConfigError) as ctx:
            _load_prod(BOOTH_QUEUE_MAX_ACTIVE="0")
        self.assertIn("BOOTH_QUEUE_MAX_ACTIVE", str(ctx.exception))

    def test_negative_timeout_rejected(self):
        with self.assertRaises(ConfigError) as ctx:
            _load_prod(BOOTH_QUEUE_START_TIMEOUT_SECONDS="-5")
        self.assertIn("BOOTH_QUEUE_START_TIMEOUT_SECONDS", str(ctx.exception))

    def test_zero_body_limit_rejected(self):
        with self.assertRaises(ConfigError):
            _load_prod(BOOTH_REQUEST_BODY_MAX_BYTES="0")

    def test_negative_countdown_rejected(self):
        with self.assertRaises(ConfigError):
            _load_prod(BOOTH_CAPTURE_COUNTDOWN_SECONDS="-1")

    def test_port_range_enforced(self):
        for raw in ("0", "-1", "65536"):
            with self.subTest(raw=raw), self.assertRaises(ConfigError):
                _load_prod(BOOTH_PORT=raw)

    def test_non_integer_rejected(self):
        with self.assertRaises(ConfigError) as ctx:
            _load_prod(BOOTH_PORT="8090.5")
        self.assertIn("整数", str(ctx.exception))

    def test_camera_health_timeout_must_be_positive(self):
        with self.assertRaises(ConfigError):
            _load_prod(BOOTH_CAMERA_HEALTH_TIMEOUT_SECONDS="0")
        cfg = _load_prod(BOOTH_CAMERA_HEALTH_TIMEOUT_SECONDS="0.5")
        self.assertEqual(cfg.camera.health_timeout_seconds, 0.5)

    def test_confidence_range_and_parsing(self):
        for raw in ("1.5", "-0.1", "abc"):
            with self.subTest(raw=raw), self.assertRaises(ConfigError):
                _load_prod(BOOTH_GESTURE_MIN_CONFIDENCE=raw)
        cfg = _load_prod(BOOTH_GESTURE_MIN_CONFIDENCE="0.95")
        self.assertAlmostEqual(cfg.gesture.minimum_confidence, 0.95)

    def test_stable_frames_minimum(self):
        with self.assertRaises(ConfigError):
            _load_prod(BOOTH_GESTURE_STABLE_FRAMES="0")


class BooleanValidationTest(unittest.TestCase):
    def test_all_documented_spellings(self):
        cases = (
            ("true", True), ("TRUE", True), ("1", True), ("yes", True), ("Yes", True),
            ("false", False), ("FALSE", False), ("0", False), ("no", False), ("No", False),
        )
        for raw, expected in cases:
            with self.subTest(raw=raw):
                cfg = _load_prod(BOOTH_REDACT_TOKENS=raw)
                self.assertEqual(cfg.logging.redact_tokens, expected)

    def test_illegal_boolean_rejected(self):
        for raw in ("maybe", "on", "off", "", "2"):
            with self.subTest(raw=raw), self.assertRaises(ConfigError) as ctx:
                _load_prod(BOOTH_REDACT_TOKENS=raw)
            self.assertIn("BOOTH_REDACT_TOKENS", str(ctx.exception))


class PathValidationTest(unittest.TestCase):
    def test_optional_paths_unset_default_none(self):
        cfg = _load_prod()
        self.assertIsNone(cfg.gesture.model_path)
        self.assertIsNone(cfg.aigc.api_key_file)

    def test_existing_path_accepted(self):
        cfg = _load_prod(BOOTH_GESTURE_MODEL_PATH=str(REAL_FILE))
        self.assertEqual(cfg.gesture.model_path, REAL_FILE)

    def test_missing_path_rejected(self):
        with self.assertRaises(ConfigError) as ctx:
            _load_prod(BOOTH_GESTURE_MODEL_PATH=str(MISSING_FILE))
        self.assertIn("BOOTH_GESTURE_MODEL_PATH", str(ctx.exception))

    def test_directory_rejected(self):
        with self.assertRaises(ConfigError):
            _load_prod(BOOTH_GESTURE_MODEL_PATH=str(PROJECT_ROOT / "app"))

    def test_empty_path_rejected(self):
        with self.assertRaises(ConfigError):
            _load_prod(BOOTH_GESTURE_MODEL_PATH="")

    def test_gesture_enabled_does_not_require_legacy_model_path(self):
        cfg = _load_prod(BOOTH_GESTURE_ENABLED="true")
        self.assertTrue(cfg.gesture.enabled)
        self.assertIsNone(cfg.gesture.model_path)

    def test_gesture_enabled_allowed_in_test_mode_without_model(self):
        cfg = _load_test(BOOTH_GESTURE_ENABLED="true")
        self.assertTrue(cfg.gesture.enabled)
        self.assertIsNone(cfg.gesture.model_path)


class AllowlistJsonTest(unittest.TestCase):
    def test_default_empty(self):
        self.assertEqual(_load_prod().queue.optional_fields_allowlist, ())

    def test_valid_list_parsed_deduped_and_trimmed(self):
        cfg = _load_prod(BOOTH_OPTIONAL_FIELDS_ALLOWLIST='[" name ", "company", "name"]')
        self.assertEqual(cfg.queue.optional_fields_allowlist, ("name", "company"))

    def test_invalid_json_rejected(self):
        with self.assertRaises(ConfigError) as ctx:
            _load_prod(BOOTH_OPTIONAL_FIELDS_ALLOWLIST="[name")
        self.assertIn("BOOTH_OPTIONAL_FIELDS_ALLOWLIST", str(ctx.exception))

    def test_non_array_rejected(self):
        with self.assertRaises(ConfigError):
            _load_prod(BOOTH_OPTIONAL_FIELDS_ALLOWLIST='{"name": true}')

    def test_non_string_item_rejected(self):
        with self.assertRaises(ConfigError):
            _load_prod(BOOTH_OPTIONAL_FIELDS_ALLOWLIST='["name", 7]')

    def test_empty_item_rejected(self):
        with self.assertRaises(ConfigError):
            _load_prod(BOOTH_OPTIONAL_FIELDS_ALLOWLIST='["name", ""]')


class AigcGateTest(unittest.TestCase):
    """AIGC 外发：默认关；生产启用要求条款批准 + 网关配置。"""

    def test_production_requires_terms_approval(self):
        env = _prod(
            BOOTH_AIGC_ENABLED="true",
            AIGC_ENDPOINT="https://gw.example.com",
            AIGC_API_KEY_FILE=str(REAL_FILE),
        )
        with self.assertRaises(ConfigError) as ctx:
            load_config(env=env)
        self.assertIn("条款", str(ctx.exception))

    def test_production_full_approval_accepted(self):
        cfg = _load_prod(
            BOOTH_AIGC_ENABLED="true",
            BOOTH_AIGC_TERMS_APPROVED="1",
            AIGC_ENDPOINT="https://gw.example.com",
            AIGC_API_KEY_FILE=str(REAL_FILE),
        )
        self.assertTrue(cfg.aigc.enabled)
        self.assertTrue(cfg.aigc.terms_approved)

    def test_production_requires_endpoint_and_key_file(self):
        with self.assertRaises(ConfigError) as ctx:
            _load_prod(
                BOOTH_AIGC_ENABLED="1",
                BOOTH_AIGC_TERMS_APPROVED="1",
                AIGC_API_KEY_FILE=str(REAL_FILE),
            )
        self.assertIn("AIGC_ENDPOINT", str(ctx.exception))
        with self.assertRaises(ConfigError) as ctx2:
            _load_prod(
                BOOTH_AIGC_ENABLED="1",
                BOOTH_AIGC_TERMS_APPROVED="1",
                AIGC_ENDPOINT="https://gw.example.com",
            )
        self.assertIn("AIGC_API_KEY_FILE", str(ctx2.exception))

    def test_test_mode_allows_aigc_without_approval_or_endpoint(self):
        cfg = _load_test(BOOTH_AIGC_ENABLED="1")
        self.assertTrue(cfg.aigc.enabled)
        self.assertFalse(cfg.aigc.terms_approved)

    def test_invalid_endpoint_rejected(self):
        with self.assertRaises(ConfigError) as ctx:
            _load_prod(AIGC_ENDPOINT="gw.example.com")
        self.assertIn("AIGC_ENDPOINT", str(ctx.exception))

    def test_legacy_aigc_variables_do_not_open_new_gate(self):
        # 旧 AIGC_ENABLED/AIGC_BACKEND 控制 v1 拍摄内联外发，与本轮新门互不替代
        cfg = _load_test(AIGC_ENABLED="1", AIGC_BACKEND="public_api")
        self.assertFalse(cfg.aigc.enabled)


class PrinterGateTest(unittest.TestCase):
    def test_printer_enabled_requires_backend(self):
        with self.assertRaises(ConfigError) as ctx:
            _load_prod(BOOTH_PRINTER_ENABLED="true")
        self.assertIn("BOOTH_PRINTER_BACKEND", str(ctx.exception))

    def test_printer_enabled_with_backend_accepted(self):
        cfg = _load_prod(BOOTH_PRINTER_ENABLED="true", BOOTH_PRINTER_BACKEND="ipp")
        self.assertTrue(cfg.printer.enabled)
        self.assertEqual(cfg.printer.backend, "ipp")


class ErrorContractTest(unittest.TestCase):
    """errors.py 的 HTTP 映射与 spec「Error handling」表逐项一致。"""

    SPEC_TABLE = {
        "INVALID_REQUEST": 400,
        "INVALID_TOKEN": 404,
        "CONSENT_REQUIRED": 403,
        "STYLE_NOT_FOUND": 404,
        "IDEMPOTENCY_CONFLICT": 409,
        "QUEUE_FULL": 429,
        "QUEUE_EXPIRED": 409,
        "NOT_CALLED": 409,
        "CAMERA_UNAVAILABLE": 503,
        "GESTURE_DISABLED": 409,
        "CAPTURE_FAILED": 503,
        "AIGC_FAILED": 502,
        "AIGC_TIMEOUT": 504,
        "PHOTO_NOT_FOUND": 404,
        "PHOTO_DELETED": 410,
        "PRINT_UNAVAILABLE": 503,
        "INTERNAL_ERROR": 500,
    }

    def test_code_constants_match_names(self):
        for code in self.SPEC_TABLE:
            with self.subTest(code=code):
                self.assertEqual(getattr(errors, code), code)

    def test_all_spec_codes_have_exact_http_mapping(self):
        for code, status in self.SPEC_TABLE.items():
            with self.subTest(code=code):
                self.assertEqual(errors.http_status_for(code), status)
                self.assertTrue(errors.default_message_for(code).strip())

    def test_error_payload_shape_follows_spec(self):
        payload = errors.error_payload(errors.QUEUE_EXPIRED, "req-123")
        self.assertEqual(set(payload), {"error"})
        self.assertEqual(set(payload["error"]), {"code", "message", "request_id"})
        self.assertEqual(payload["error"]["code"], "QUEUE_EXPIRED")
        self.assertEqual(payload["error"]["request_id"], "req-123")
        self.assertTrue(payload["error"]["message"])

    def test_api_error_carries_status_and_payload(self):
        err = errors.ApiError(errors.PRINT_UNAVAILABLE)
        self.assertEqual(err.http_status, 503)
        payload = err.payload("r-1")
        self.assertEqual(payload["error"]["code"], "PRINT_UNAVAILABLE")
        self.assertEqual(payload["error"]["request_id"], "r-1")

    def test_custom_message_overrides_default(self):
        payload = errors.error_payload(errors.INVALID_REQUEST, "r-2", message="字段缺失。")
        self.assertEqual(payload["error"]["message"], "字段缺失。")

    def test_unknown_code_rejected(self):
        with self.assertRaises(KeyError):
            errors.spec_for("NOT_A_REAL_CODE")

    def test_route_table_codes_present(self):
        for code in (
            "QUEUE_NOT_FOUND",
            "JOB_NOT_FOUND",
            "JOB_NOT_WAITING_FOR_CONSENT",
            "INTERNAL_ONLY",
            "DELETE_IN_PROGRESS",
            "PRINT_JOB_NOT_FOUND",
        ):
            with self.subTest(code=code):
                self.assertIn(code, errors.ERROR_SPECS)


if __name__ == "__main__":
    unittest.main()
