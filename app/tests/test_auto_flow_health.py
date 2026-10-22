# -*- coding: utf-8 -*-
"""T-20 健康服务与公共状态投影测试。

运行（项目根目录）：
    python3 -m unittest app.tests.test_auto_flow_health -v

覆盖：
- 相机健康以帧新鲜度判定（无帧 / 超时 / 设备枚举不算可用 / 探针异常）；
- 六组件固定夹具与 SystemHealth 字段契约（models.py 权威值对象）；
- 公共状态投影字段契约（counts / called_label / phase / countdown_remaining /
  capabilities）与脱敏保证（敏感值放进内部状态，输出不得回显）；
- /api/state 与 /api/health 载荷 ≤ 32 KiB。

约定：不联网、不写盘、不触碰硬件；时钟与探针全部注入。
"""

from __future__ import annotations

import json
import unittest
from datetime import datetime, timezone

from app.auto_flow import models
from app.auto_flow.health_service import (
    COMPONENT_NAMES,
    DEFAULT_CAMERA_TIMEOUT_SECONDS,
    FIXTURE_CHECKED_AT,
    HealthService,
    camera_freshness_health,
    demo_snapshot,
)
from app.auto_flow.state_projection import (
    COMPONENTS,
    PUBLIC_CAPTURE_PHASES,
    QUEUE_STATUSES,
    called_label,
    public_capture_fields,
    public_capabilities,
    public_overlay_fields,
    public_queue_fields,
    public_state,
    queue_counts,
)

MAX_PAYLOAD_BYTES = 32 * 1024
NOW = 1_756_000_000.0  # 固定 Unix 时间戳，时钟与帧时间戳同源


def _component(health, name):
    if isinstance(health, dict):
        return health[name]
    return getattr(health, name)


def _to_plain(obj):
    """dataclass（带 to_dict）-> 纯 JSON 结构；dict/list 递归。"""
    if hasattr(obj, "to_dict"):
        return obj.to_dict()
    if isinstance(obj, dict):
        return {key: _to_plain(value) for key, value in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_to_plain(value) for value in obj]
    return obj


def _json_text(obj) -> str:
    return json.dumps(_to_plain(obj), ensure_ascii=False, sort_keys=True)


def _json_bytes(obj) -> int:
    return len(_json_text(obj).encode("utf-8"))


def _collect_keys(obj, out: set) -> None:
    if isinstance(obj, dict):
        for key, value in obj.items():
            out.add(key)
            _collect_keys(value, out)
    elif isinstance(obj, (list, tuple)):
        for value in obj:
            _collect_keys(value, out)


def _ok_component(checked_at: str = FIXTURE_CHECKED_AT, reason: str | None = None):
    return models.ComponentHealth(status="ok", checked_at=checked_at, reason=reason)


class CameraFreshnessTest(unittest.TestCase):
    """相机健康只看帧新鲜度：设备枚举 / 能打开一律不算可用。"""

    def test_no_frame_ever_is_unavailable_with_chinese_reason(self):
        health = camera_freshness_health(None, NOW, DEFAULT_CAMERA_TIMEOUT_SECONDS)
        self.assertIsInstance(health, models.ComponentHealth)
        self.assertEqual(health.status, "unavailable")
        self.assertTrue(health.reason)
        self.assertIn("相机", health.reason)
        self.assertIn("画面", health.reason)
        self.assertNotEqual(health.reason, "unavailable")

    def test_stale_frame_past_timeout_is_unavailable(self):
        health = camera_freshness_health(NOW - 5.0, NOW, 2.0)
        self.assertEqual(health.status, "unavailable")
        self.assertIn("没有新画面", health.reason)

    def test_fresh_frame_is_ok(self):
        health = camera_freshness_health(NOW - 0.5, NOW, 2.0)
        self.assertEqual(health.status, "ok")
        self.assertIsNone(health.reason)

    def test_age_exactly_at_timeout_is_still_ok(self):
        health = camera_freshness_health(NOW - 2.0, NOW, 2.0)
        self.assertEqual(health.status, "ok")

    def test_nan_or_invalid_timestamps_are_unavailable(self):
        for last_frame_at, now, timeout in (
            (float("nan"), NOW, 2.0),
            (float("inf"), NOW, 2.0),
            (NOW, NOW, float("nan")),
            (NOW, NOW, 0.0),
            (NOW, NOW, -1.0),
            ("not-a-time", NOW, 2.0),
        ):
            with self.subTest(last_frame_at=last_frame_at, timeout=timeout):
                self.assertEqual(
                    camera_freshness_health(last_frame_at, now, timeout).status,
                    "unavailable",
                )

    def test_checked_at_override_is_used_for_fixture_timestamps(self):
        health = camera_freshness_health(None, NOW, 2.0, checked_at=FIXTURE_CHECKED_AT)
        self.assertEqual(health.checked_at, FIXTURE_CHECKED_AT)


class FakeCameraDeviceWithNoFrames:
    """设备枚举成功、但没有帧——绝不能判 ok。"""

    device_present = True

    def last_frame_at(self):
        return None


class HealthServiceTest(unittest.TestCase):
    def test_snapshot_uses_models_value_objects(self):
        service = HealthService({}, camera=lambda: NOW - 0.1, clock=lambda: NOW)
        snapshot = service.snapshot()
        self.assertIsInstance(snapshot, models.SystemHealth)
        for name in COMPONENT_NAMES:
            with self.subTest(component=name):
                self.assertIsInstance(_component(snapshot, name), models.ComponentHealth)

    def test_six_component_names_exact(self):
        snapshot = HealthService({}, camera=lambda: NOW, clock=lambda: NOW).snapshot()
        plain = _to_plain(snapshot)
        self.assertEqual(set(plain), set(COMPONENT_NAMES))
        self.assertEqual(list(plain), list(COMPONENT_NAMES))

    def test_fixed_fixture_statuses_match_contract(self):
        snapshot = demo_snapshot()
        self.assertEqual(
            {name: _component(snapshot, name).status for name in COMPONENT_NAMES},
            {
                "camera": "unavailable",
                "gesture": "disabled",
                "queue": "ok",
                "storage": "ok",
                "aigc": "disabled",
                "printer": "disabled",
            },
        )
        for name in COMPONENT_NAMES:
            with self.subTest(component=name):
                component = _component(snapshot, name)
                self.assertEqual(component.checked_at, FIXTURE_CHECKED_AT)
                self.assertIn(component.status, models.COMPONENT_STATUSES)
                self.assertTrue(component.reason, "夹具每个组件都要有可读的中文说明")
        camera = _component(snapshot, "camera")
        self.assertIn("帧", camera.reason)

    def test_probes_are_injected_and_used_once_each(self):
        calls: list[str] = []

        def make_probe(name: str, status: str):
            def probe():
                calls.append(name)
                return models.ComponentHealth(
                    status=status, checked_at=FIXTURE_CHECKED_AT, reason=f"{name} 探针读数"
                )
            return probe

        service = HealthService(
            {
                "gesture": make_probe("gesture", "disabled"),
                "queue": make_probe("queue", "ok"),
                "storage": make_probe("storage", "ok"),
                "aigc": make_probe("aigc", "disabled"),
                "printer": make_probe("printer", "disabled"),
            },
            camera=lambda: NOW,
            clock=lambda: NOW,
        )
        snapshot = service.snapshot()
        self.assertEqual(sorted(calls), ["aigc", "gesture", "printer", "queue", "storage"])
        self.assertEqual(_component(snapshot, "queue").status, "ok")
        self.assertEqual(_component(snapshot, "queue").reason, "queue 探针读数")
        self.assertEqual(_component(snapshot, "printer").status, "disabled")
        self.assertEqual(_component(snapshot, "camera").status, "ok")

    def test_port_style_objects_health_and_status_methods(self):
        class Gate:
            def health(self):
                return _ok_component(reason="网关正常")

        class Gesture:
            def status(self):
                return models.ComponentHealth(
                    status="disabled", checked_at=FIXTURE_CHECKED_AT, reason="手势未启用"
                )

        snapshot = HealthService({"aigc": Gate(), "gesture": Gesture()}, camera=lambda: NOW, clock=lambda: NOW).snapshot()
        self.assertEqual(_component(snapshot, "aigc").status, "ok")
        self.assertEqual(_component(snapshot, "gesture").status, "disabled")

    def test_missing_probes_are_conservative(self):
        snapshot = HealthService(clock=lambda: NOW).snapshot()
        self.assertEqual(_component(snapshot, "camera").status, "unavailable")
        for name in COMPONENT_NAMES:
            if name == "camera":
                continue
            with self.subTest(component=name):
                status = _component(snapshot, name).status
                self.assertEqual(status, "disabled")
                self.assertNotEqual(status, "ok")

    def test_device_enumeration_without_frames_is_not_ok(self):
        service = HealthService({}, camera=FakeCameraDeviceWithNoFrames(), clock=lambda: NOW)
        camera = _component(service.snapshot(), "camera")
        self.assertEqual(camera.status, "unavailable")
        self.assertIn("画面", camera.reason)

    def test_probe_exception_becomes_unavailable_not_raise(self):
        def broken():
            raise RuntimeError("USB 总线复位中")

        service = HealthService({"gesture": broken, "queue": _ok_component}, camera=lambda: NOW, clock=lambda: NOW)
        snapshot = service.snapshot()
        self.assertEqual(_component(snapshot, "gesture").status, "unavailable")
        self.assertIn("gesture", _component(snapshot, "gesture").reason)
        self.assertEqual(_component(snapshot, "queue").status, "ok")

    def test_camera_probe_exception_becomes_unavailable_not_raise(self):
        def broken_camera():
            raise OSError("设备消失")

        snapshot = HealthService({}, camera=broken_camera, clock=lambda: NOW).snapshot()
        self.assertEqual(_component(snapshot, "camera").status, "unavailable")

    def test_probe_returning_garbage_is_unavailable(self):
        service = HealthService(
            {
                "gesture": lambda: {"status": "banana", "checked_at": FIXTURE_CHECKED_AT},
                "queue": lambda: "ok",
            },
            camera=lambda: NOW,
            clock=lambda: NOW,
        )
        snapshot = service.snapshot()
        self.assertEqual(_component(snapshot, "gesture").status, "unavailable")
        self.assertEqual(_component(snapshot, "queue").status, "unavailable")

    def test_probe_returning_dict_is_accepted(self):
        service = HealthService(
            {"storage": lambda: {"status": "ok", "checked_at": FIXTURE_CHECKED_AT, "reason": "盘读写正常"}},
            camera=lambda: NOW,
            clock=lambda: NOW,
        )
        storage = _component(service.snapshot(), "storage")
        self.assertEqual(storage.status, "ok")
        self.assertEqual(storage.reason, "盘读写正常")

    def test_unknown_probe_name_rejected(self):
        with self.assertRaises(ValueError):
            HealthService({"camer": lambda: None})
        with self.assertRaises(ValueError):
            HealthService({"camera": lambda: None})  # 相机走 camera= 参数，防止绕过帧新鲜度
        with self.assertRaises(ValueError):
            HealthService({}, camera_timeout=0)
        with self.assertRaises(TypeError):
            HealthService({}, clock=123)

    def test_clock_drives_checked_at(self):
        service = HealthService({}, camera=lambda: NOW - 0.1, clock=lambda: NOW)
        snapshot = service.snapshot()
        expected = models.to_utc_iso(datetime.fromtimestamp(NOW, tz=timezone.utc))
        for name in COMPONENT_NAMES:
            with self.subTest(component=name):
                self.assertEqual(_component(snapshot, name).checked_at, expected)

    def test_health_payload_under_32kib(self):
        snapshot = HealthService(
            {
                "gesture": _ok_component,
                "queue": _ok_component,
                "storage": _ok_component,
                "aigc": _ok_component,
                "printer": _ok_component,
            },
            camera=lambda: NOW,
            clock=lambda: NOW,
        ).snapshot()
        self.assertLessEqual(_json_bytes(snapshot), MAX_PAYLOAD_BYTES)
        self.assertLessEqual(_json_bytes(demo_snapshot()), MAX_PAYLOAD_BYTES)


class SystemHealthContractTest(unittest.TestCase):
    def test_field_contract_status_checked_at_reason(self):
        snapshot = demo_snapshot()
        plain = snapshot.to_dict()
        for name in COMPONENT_NAMES:
            with self.subTest(component=name):
                entry = plain[name]
                self.assertIn(entry["status"], models.COMPONENT_STATUSES)
                self.assertFalse(set(entry) - {"status", "checked_at", "reason"})
                models.parse_utc_iso(entry["checked_at"])
                reason = entry.get("reason")
                self.assertTrue(reason is None or isinstance(reason, str))


class QueueProjectionTest(unittest.TestCase):
    def test_counts_contract(self):
        entries = [
            {"status": "queued"},
            {"status": "queued"},
            {"status": "called"},
            {"status": "ready_for_pickup"},
            {"status": "expired"},
            {"status": "hacked-status"},  # 白名单外：不计数、不进输出
        ]
        counts = queue_counts(entries)
        self.assertEqual(list(counts), list(QUEUE_STATUSES))
        self.assertEqual(counts["queued"], 2)
        self.assertEqual(counts["called"], 1)
        self.assertEqual(counts["ready_for_pickup"], 1)
        self.assertEqual(counts["expired"], 1)
        self.assertEqual(counts["cancelled"], 0)
        self.assertNotIn("hacked-status", counts)
        self.assertEqual(sum(counts.values()), 5)

    def test_empty_counts_are_all_zero(self):
        counts = queue_counts(None)
        self.assertEqual(set(counts), set(QUEUE_STATUSES))
        self.assertTrue(all(value == 0 for value in counts.values()))

    def test_public_queue_fields_shape_and_called_label(self):
        entries = [
            {"queue_id": "qid-9f3a", "status": "queued"},
            {"queue_id": "qid-1a2b", "status": "called", "display_number": "SM-2026-0012"},
        ]
        fields = public_queue_fields(entries)
        self.assertEqual(set(fields), {"counts", "called_label"})
        self.assertEqual(fields["called_label"], "第 12 位")
        self.assertEqual(fields["counts"]["called"], 1)

    def test_called_label_variants_never_echo_raw_values(self):
        self.assertEqual(called_label(12), "第 12 位")
        self.assertEqual(called_label("12"), "第 12 位")
        self.assertEqual(called_label("SM-2026-0007"), "第 7 位")
        self.assertEqual(called_label({"display_number": "SM-2026-0042"}), "第 42 位")
        self.assertEqual(called_label({"queue_id": "qid-secret", "number": 3}), "第 3 位")
        self.assertIsNone(called_label(None))
        label = called_label({"queue_id": "qid-8f3a9c2e1d", "display_number": "SM-2026-0012"})
        self.assertNotIn("qid-8f3a9c2e1d", label)
        self.assertNotIn("SM-2026-0012", label)
        self.assertEqual(called_label("opaque-without-number"), "已叫号")

    def test_called_label_does_not_mine_digits_out_of_opaque_ids(self):
        # 不透明 id 即使尾部带数字，也不得被当成公开位次
        self.assertEqual(called_label("qid-8f3a-1234"), "已叫号")
        self.assertEqual(called_label("8f3a9c2e1d4f"), "已叫号")
        self.assertEqual(called_label("queue-abc-999"), "已叫号")
        self.assertEqual(called_label({"queue_id": "qid-8f3a-1234"}), "已叫号")

    def test_no_called_entry_gives_null_label(self):
        self.assertIsNone(public_queue_fields([{"status": "queued"}])["called_label"])
        self.assertIsNone(public_queue_fields([])["called_label"])


class CaptureProjectionTest(unittest.TestCase):
    def test_capture_shape_exact_keys(self):
        for payload in (None, {"status": "countdown", "countdown_remaining": 1.2}):
            with self.subTest(payload=payload):
                fields = public_capture_fields(payload)
                self.assertEqual(set(fields), {"phase", "countdown_remaining"})
                self.assertIn(fields["phase"], PUBLIC_CAPTURE_PHASES)

    def test_idle_and_status_mapping(self):
        self.assertEqual(public_capture_fields(None), {"phase": "idle", "countdown_remaining": None})
        self.assertEqual(public_capture_fields({})["phase"], "unknown")
        self.assertEqual(public_capture_fields({"status": "created"})["phase"], "waiting")
        self.assertEqual(public_capture_fields({"status": "capturing"})["phase"], "capturing")
        self.assertEqual(public_capture_fields({"status": "processing"})["phase"], "processing")
        self.assertEqual(public_capture_fields({"phase": "bogus"})["phase"], "unknown")

    def test_countdown_remaining_is_ceil_non_negative(self):
        self.assertEqual(public_capture_fields({"status": "countdown", "countdown_remaining": 1.2})["countdown_remaining"], 2)
        self.assertEqual(public_capture_fields({"status": "countdown", "countdown_remaining": 2.0})["countdown_remaining"], 2)
        self.assertEqual(public_capture_fields({"status": "countdown", "countdown_remaining": -1})["countdown_remaining"], 0)
        self.assertIsNone(public_capture_fields({"status": "capturing", "countdown_remaining": 1.0})["countdown_remaining"])
        self.assertIsNone(public_capture_fields({"status": "countdown"})["countdown_remaining"])
        self.assertIsNone(public_capture_fields({"status": "countdown", "countdown_remaining": "3"})["countdown_remaining"])

    def test_capture_never_leaks_internal_ids(self):
        fields = public_capture_fields(
            {"status": "processing", "job_id": "job-4d5e", "queue_id": "qid-8f3a", "photo_token": "AbCdEfGhIjKlMnOpQrSt12"}
        )
        self.assertEqual(set(fields), {"phase", "countdown_remaining"})


class CapabilitiesProjectionTest(unittest.TestCase):
    def test_capabilities_from_demo_snapshot_only_booleans(self):
        capabilities = public_capabilities(demo_snapshot())
        self.assertEqual(list(capabilities), list(COMPONENTS))
        for name, value in capabilities.items():
            with self.subTest(component=name):
                self.assertIsInstance(value, bool)
        self.assertEqual(
            capabilities,
            {"camera": False, "gesture": False, "queue": True, "storage": True, "aigc": False, "printer": False},
        )

    def test_only_ok_counts_as_capable(self):
        capabilities = public_capabilities(
            {
                "camera": {"status": "degraded"},
                "gesture": "ok",
                "queue": "unavailable",
                "storage": {"status": "ok"},
                "aigc": None,
            }
        )
        self.assertEqual(
            capabilities,
            {"camera": False, "gesture": True, "queue": False, "storage": True, "aigc": False, "printer": False},
        )

    def test_missing_health_is_all_false(self):
        self.assertEqual(public_capabilities(None), {name: False for name in COMPONENTS})


class PublicStateDesensitizationTest(unittest.TestCase):
    """把一批敏感值放进内部状态，输出绝不允许出现。"""

    SENSITIVE_VALUES = {
        "queue_id": "qid-8f3a9c2e1d4b",
        "job_id": "job-4d5e6f708192",
        "request_id": "req-0a1b2c3d4e5f6a7b",
        "idempotency_key": "idem-11223344556677aabb",
        "capture_consent_id": "consent-9c8b7a6d5e4f",
        "photo_token": "AbCdEfGhIjKlMnOpQrSt12",
        "name": "张小明",
        "handle": "@xiaoming",
        "phone": "13800000000",
        "display_number": "SM-2026-0012",
    }

    FORBIDDEN_KEYS = {
        "queue_id",
        "job_id",
        "request_id",
        "idempotency_key",
        "capture_consent_id",
        "external_processing_consent_id",
        "consent_id",
        "photo_token",
        "token",
        "name",
        "handle",
        "phone",
        "wechat",
        "display_number",
    }

    def _internal_state(self):
        sensitive = self.SENSITIVE_VALUES
        entries = [
            dict(sensitive, status="called"),
            dict(sensitive, status="queued"),
            dict(sensitive, status="queued"),
        ]
        capture = dict(sensitive, status="countdown", countdown_remaining=1.5, phase="countdown")
        return entries, capture

    def test_state_projection_has_no_sensitive_values_or_keys(self):
        entries, capture = self._internal_state()
        projected = public_state(queue_entries=entries, capture=capture, health=demo_snapshot())
        text = _json_text(projected)
        for label, value in self.SENSITIVE_VALUES.items():
            with self.subTest(field=label):
                self.assertNotIn(value, text)
        keys: set = set()
        _collect_keys(_to_plain(projected), keys)
        for forbidden in self.FORBIDDEN_KEYS:
            with self.subTest(key=forbidden):
                self.assertNotIn(forbidden, keys)

    def test_overlay_projection_same_guarantee(self):
        entries, capture = self._internal_state()
        projected = public_overlay_fields(queue_entries=entries, capture=capture, health=demo_snapshot())
        self.assertEqual(projected, public_state(queue_entries=entries, capture=capture, health=demo_snapshot()))
        text = _json_text(projected)
        for value in self.SENSITIVE_VALUES.values():
            self.assertNotIn(value, text)

    def test_public_state_shape(self):
        entries, capture = self._internal_state()
        projected = public_state(queue_entries=entries, capture=capture, health=demo_snapshot())
        self.assertEqual(set(projected), {"queue", "capture", "capabilities"})
        self.assertEqual(set(projected["queue"]), {"counts", "called_label"})
        self.assertEqual(set(projected["capture"]), {"phase", "countdown_remaining"})

    def test_payload_under_32kib_with_large_queue(self):
        entries, capture = self._internal_state()
        entries = entries + [dict(self.SENSITIVE_VALUES, status="queued") for _ in range(200)]
        projected = public_state(queue_entries=entries, capture=capture, health=demo_snapshot())
        self.assertLessEqual(_json_bytes(projected), MAX_PAYLOAD_BYTES)

    def test_fixture_queue_reaching_called_shows_anonymous_label_only(self):
        """固定夹具口径：queue 到号，但对外只有 counts 与匿名 called_label。"""
        fields = public_queue_fields(
            [
                {"queue_id": "qid-8f3a9c2e1d4b", "status": "called", "display_number": "SM-2026-0012"},
                {"queue_id": "qid-zzz", "status": "queued"},
            ]
        )
        self.assertEqual(fields["counts"]["called"], 1)
        self.assertEqual(fields["counts"]["queued"], 1)
        self.assertEqual(fields["called_label"], "第 12 位")
        self.assertNotIn("qid-8f3a9c2e1d4b", _json_text(fields))


if __name__ == "__main__":
    unittest.main()
