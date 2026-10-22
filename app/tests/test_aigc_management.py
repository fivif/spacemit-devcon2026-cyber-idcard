"""风格管理、六款预设迁移与真实 HTTP 路由的离线回归。"""

from __future__ import annotations

import importlib
import base64
import io
import json
import os
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from PIL import Image

APP_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP_DIR))

from aigc_catalog import CLOTHING_LOCK, PRESET_VERSION, CatalogError, CatalogOperationError, StyleCatalog, default_catalog
from aigc_channels import AIGCChannels
from app.auto_flow.consent_service import ConsentService, MemoryConsentStore
from app.auto_flow.http_handlers import AutoFlowServices, QueueSessionRegistry
from app.auto_flow.models import QueueStatus, QueueSubmission
from app.auto_flow.queue_service import QueueService
from app.auto_flow.queue_store import MemoryQueueStore


EXPECTED_STYLES = [
    ("hand-painted-animation", "晴空手绘动画"),
    ("american-comic", "高能漫画封面"),
    ("hand-doodle", "开发者手账速写"),
    ("professional-suit", "未来职业肖像"),
    ("chinese-elegance", "东方新潮国风"),
    ("pixel-person", "复古像素头像"),
]
EXPECTED_IDS = [style_id for style_id, _title in EXPECTED_STYLES]


class CatalogManagementTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="aigc-management-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.catalog = StyleCatalog(self.root)

    def test_v4_to_v9_migration_refreshes_six_presets_and_retires_korean(self):
        seeds = {item["id"]: item for item in default_catalog()["styles"]}
        for preset_version in range(4, PRESET_VERSION):
            with self.subTest(preset_version=preset_version):
                legacy = default_catalog()
                legacy.update(preset_version=preset_version, version=41, onsite_note="keep metadata")
                for index, item in enumerate(legacy["styles"]):
                    item.update(title="现场风格-%s" % item["id"], description="现场旧说明",
                                model="onsite-%s" % item["id"], sizes=["1280x720", "720x1280"],
                                default_orientation="wide", backdrop_id="onsite-backdrop", revision=9,
                                updated_at=1, onsite_setting={"keep": index},
                                enabled=True, is_default=index == 0,
                                wide_prompt="Create a cute chibi with visible feet." + CLOTHING_LOCK,
                                vertical_prompt="Create a portrait preserving body proportions." + CLOTHING_LOCK,
                                negative_prompt="No realistic adult proportions.",
                                history=[{"revision": 8, "wide_prompt": "Original historical prompt"}])
                korean = dict(legacy["styles"][1], id="korean-pop", title="韩式流行风",
                              description="退役兼容", revision=5, enabled=True, is_default=False,
                              history=[{"revision": 4, "title": "旧韩式"}])
                custom = dict(legacy["styles"][2], id="onsite-copy", title="现场副本", enabled=False,
                              is_default=False)
                legacy["styles"].extend((korean, custom))
                self.catalog.path.write_text(json.dumps(legacy), encoding="utf-8")
                catalog = StyleCatalog(self.root)
                migrated = catalog.snapshot()
                self.assertEqual(migrated["preset_version"], PRESET_VERSION)
                self.assertGreater(migrated["version"], 41)
                self.assertEqual(migrated["onsite_note"], "keep metadata")
                self.assertEqual(migrated["styles"][-1], custom)
                self.assertEqual([item["id"] for item in migrated["styles"][:6]], EXPECTED_IDS)
                for updated, original in zip(migrated["styles"][:6], legacy["styles"][:6]):
                    seed = seeds[original["id"]]
                    for key in original:
                        if key not in ("title", "description", "wide_prompt", "vertical_prompt", "negative_prompt",
                                       "backdrop_id",
                                       "history", "revision", "updated_at"):
                            self.assertEqual(updated[key], original[key], (original["id"], key))
                    for key in ("title", "description", "wide_prompt", "vertical_prompt", "negative_prompt",
                                "backdrop_id"):
                        self.assertEqual(updated[key], seed[key], (original["id"], key))
                    self.assertGreater(updated["revision"], original["revision"])
                    self.assertEqual(updated["history"][0], original["history"][0])
                    self.assertTrue(any(entry.get("title") == original["title"]
                                        for entry in updated["history"]))
                    self.assertGreater(updated["updated_at"], original["updated_at"])
                retired = next(item for item in migrated["styles"] if item["id"] == "korean-pop")
                self.assertFalse(retired["enabled"])
                self.assertFalse(retired["is_default"])
                self.assertEqual(retired["history"][0], korean["history"][0])
                self.assertTrue(any(entry.get("enabled") for entry in retired["history"]))
                self.assertEqual([(item["id"], item["title"]) for item in catalog.public()],
                                 EXPECTED_STYLES)
                persisted = catalog.path.read_bytes()
                self.assertEqual(StyleCatalog(self.root).snapshot(), migrated)
                self.assertEqual(catalog.path.read_bytes(), persisted)

    def test_migration_refreshes_all_builtin_titles_and_preserves_old_titles(self):
        legacy = default_catalog()
        legacy["preset_version"] = PRESET_VERSION - 1
        for index, item in enumerate(legacy["styles"]):
            item["title"] = "现场旧标题-%d" % index
        self.catalog.path.write_text(json.dumps(legacy), encoding="utf-8")
        updated = StyleCatalog(self.root).snapshot()["styles"]
        self.assertEqual([(item["id"], item["title"]) for item in updated], EXPECTED_STYLES)
        for index, item in enumerate(updated):
            self.assertTrue(any(entry.get("title") == "现场旧标题-%d" % index
                                for entry in item["history"]))

    def test_delete_default_replaces_it_and_never_resurrects(self):
        before = self.catalog.snapshot()
        default_id = EXPECTED_IDS[0]
        result = self.catalog.delete(default_id)
        self.assertEqual(result["version"], before["version"] + 1)
        self.assertNotIn(default_id, {item["id"] for item in result["styles"]})
        defaults = [item for item in result["styles"] if item["is_default"]]
        self.assertEqual(len(defaults), 1)
        self.assertTrue(defaults[0]["enabled"])
        self.assertEqual([item["id"] for item in result["styles"]],
                         [item["id"] for item in before["styles"][1:]])
        self.assertTrue(result["styles"][0]["is_default"])
        self.assertEqual(result["styles"][1:], before["styles"][2:])
        result = self.catalog.save(result["styles"])
        self.assertEqual(StyleCatalog(self.root).snapshot(), result)
        result["preset_version"] = 4
        self.catalog.path.write_text(json.dumps(result), encoding="utf-8")
        upgraded = StyleCatalog(self.root).snapshot()
        self.assertNotIn(default_id, {item["id"] for item in upgraded["styles"]})
        upgraded["preset_version"] = 3
        self.catalog.path.write_text(json.dumps(upgraded), encoding="utf-8")
        self.assertNotIn(default_id, {item["id"] for item in StyleCatalog(self.root).snapshot()["styles"]})

    def test_v4_to_v9_missing_presets_and_tombstones_are_not_resurrected(self):
        for preset_version in range(4, PRESET_VERSION):
            with self.subTest(preset_version=preset_version):
                legacy = default_catalog()
                legacy.update(preset_version=preset_version,
                              deleted_style_ids=["pixel-person", "professional-suit", "deleted-copy"])
                legacy["styles"] = legacy["styles"][1:3]
                for item in legacy["styles"]:
                    item.update(enabled=False, is_default=False)
                custom = dict(default_catalog()["styles"][4], id="onsite-copy", title="现场默认",
                              is_default=True)
                legacy["styles"].append(custom)
                self.catalog.path.write_text(json.dumps(legacy), encoding="utf-8")
                catalog = StyleCatalog(self.root)
                migrated = catalog.snapshot()
                expected_builtin = [style_id for style_id in EXPECTED_IDS
                                    if style_id not in legacy["deleted_style_ids"]]
                self.assertEqual([item["id"] for item in migrated["styles"]],
                                 expected_builtin + ["onsite-copy"])
                self.assertEqual(migrated["deleted_style_ids"], legacy["deleted_style_ids"])
                self.assertEqual(migrated["styles"][-1], custom)
                public = catalog.public()
                self.assertEqual([item["id"] for item in public],
                                 [style_id for style_id in expected_builtin
                                  if next(item for item in migrated["styles"]
                                          if item["id"] == style_id)["enabled"]] + ["onsite-copy"])
                self.assertTrue(next(item for item in public if item["id"] == "onsite-copy")["is_default"])
                self.assertEqual(StyleCatalog(self.root).snapshot(), migrated)

    def test_custom_enabled_public_default_and_delete(self):
        before = self.catalog.snapshot()
        custom = dict(before["styles"][2], id="comic-copy", title="漫画副本", is_default=False)
        result = self.catalog.upsert(custom)
        self.assertTrue(result["styles"][-1]["enabled"])
        self.assertEqual([item["id"] for item in self.catalog.public()],
                         [item["id"] for item in before["styles"]] + ["comic-copy"])
        for item in result["styles"]:
            item["is_default"] = item["id"] == "comic-copy"
        self.catalog.save(result["styles"])
        self.assertEqual([item["id"] for item in self.catalog.public() if item["is_default"]], ["comic-copy"])
        result = self.catalog.delete("comic-copy")
        self.assertTrue(result["styles"][0]["is_default"])
        self.assertNotIn("comic-copy", {item["id"] for item in StyleCatalog(self.root).snapshot()["styles"]})

    def test_delete_disabled_custom_and_last_enabled_or_missing_are_atomic(self):
        before = self.catalog.snapshot()
        custom = dict(before["styles"][0], id="disabled-copy", enabled=False, is_default=False)
        self.catalog.upsert(custom)
        self.catalog.delete("disabled-copy")
        styles = self.catalog.snapshot()["styles"]
        for item in styles[1:]:
            item["enabled"] = False
        self.catalog.save(styles)
        persisted = self.catalog.path.read_bytes()
        for style_id, code, status in ((styles[0]["id"], "last_enabled_style", 409),
                                      ("missing-style", "style_not_found", 404)):
            with self.subTest(style=style_id), self.assertRaises(CatalogOperationError) as raised:
                self.catalog.delete(style_id)
            self.assertEqual((raised.exception.code, raised.exception.http_status), (code, status))
            self.assertEqual(self.catalog.path.read_bytes(), persisted)
        self.catalog.delete(styles[1]["id"])

    def test_write_failure_does_not_change_catalog(self):
        before = self.catalog.snapshot()
        persisted = self.catalog.path.read_bytes()
        with patch("aigc_catalog.os.replace", side_effect=OSError("write failed")):
            with self.assertRaises(OSError):
                self.catalog.delete("pixel-person")
        self.assertEqual(self.catalog.snapshot(), before)
        self.assertEqual(self.catalog.path.read_bytes(), persisted)


class OperatorHttpTests(unittest.TestCase):
    def test_global_model_endpoint_cannot_override_channels(self):
        with patch.object(self.server_module, "read_settings", return_value={}), patch.object(
            self.server_module, "write_settings"
        ) as saved:
            status, payload = self.request_json("/api/operator/aigc/global-model", {
                "model": "Qwen/Qwen-Image-2.1"})
        self.assertEqual(status, 410)
        self.assertEqual(payload["error"], "channel_model_required")
        saved.assert_not_called()

    def test_namespaced_style_model_can_be_saved(self):
        style = dict(self.catalog.snapshot()["styles"][0], model="Qwen/Qwen-Image-2.1")
        result = self.catalog.upsert(style, style["id"])
        selected = next(item for item in result["styles"] if item["id"] == style["id"])
        self.assertEqual(selected["model"], "Qwen/Qwen-Image-2.1")

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="operator-http-")
        self.addCleanup(self.temp.cleanup)
        with patch.dict(os.environ, {"BOOTH_DATA": self.temp.name, "BOOTH_CAMERA": "mock"}):
            self.server_module = importlib.import_module("booth_server")
        self.booth = object.__new__(self.server_module.Booth)
        self.booth.style_catalog = StyleCatalog(self.temp.name)
        self.booth.aigc_channels = AIGCChannels(self.temp.name)
        self.booth.jobs = self.server_module.JobBoard()
        self.booth.lock = threading.Lock()
        self.booth._style_seq = 0
        self.booth.auto_flow = AutoFlowServices(
            queue=QueueService(MemoryQueueStore()),
            consent=ConsentService(MemoryConsentStore()),
            sessions=QueueSessionRegistry(), style_source=self.booth.style_catalog)
        handler = type("TestOperatorHandler", (self.server_module.Handler,), {"booth": self.booth})
        self.http_server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.thread = threading.Thread(target=self.http_server.serve_forever,
                                       kwargs={"poll_interval": 0.01}, daemon=True)
        self.thread.start()
        self.addCleanup(self.close_server)
        self.base = "http://127.0.0.1:%d" % self.http_server.server_port
        self.catalog = self.booth.style_catalog
        self.catalog.snapshot()

    def close_server(self):
        self.http_server.shutdown()
        self.http_server.server_close()
        self.thread.join(timeout=5)

    def request(self, path, payload=None):
        request = urllib.request.Request(self.base + path,
                                         data=None if payload is None else json.dumps(payload).encode(),
                                         headers={"Content-Type": "application/json"},
                                         method="GET" if payload is None else "POST")
        try:
            response = urllib.request.urlopen(request, timeout=5)
        except urllib.error.HTTPError as error:
            response = error
        with response:
            body = response.read()
            return response.status, response.headers, body

    def request_json(self, path, payload=None):
        status, headers, body = self.request(path, payload)
        self.assertIn("application/json", headers["Content-Type"])
        return status, json.loads(body)

    def request_multipart(self, path, fields, image=None):
        boundary = "operator-channel-test"
        parts = []
        for name, value in fields.items():
            parts.append((
                '--%s\r\nContent-Disposition: form-data; name="%s"\r\n\r\n%s\r\n'
                % (boundary, name, value)
            ).encode("utf-8"))
        if image is not None:
            parts.append((
                '--%s\r\nContent-Disposition: form-data; name="image"; '
                'filename="portrait.png"\r\nContent-Type: image/png\r\n\r\n' % boundary
            ).encode("ascii") + image + b"\r\n")
        parts.append(("--%s--\r\n" % boundary).encode("ascii"))
        body = b"".join(parts)
        request = urllib.request.Request(
            self.base + path, data=body, method="POST",
            headers={"Content-Type": "multipart/form-data; boundary=" + boundary})
        try:
            response = urllib.request.urlopen(request, timeout=5)
        except urllib.error.HTTPError as error:
            response = error
        with response:
            payload = response.read()
            self.assertIn("application/json", response.headers["Content-Type"])
            return response.status, json.loads(payload)

    def submit(self, style_id="pixel-person"):
        consent = self.booth.auto_flow.consent.record("capture", True)
        status, payload = self.request_json("/api/queue", {
            "request_id": consent.consent_id, "style_id": style_id,
            "capture_consent_id": consent.consent_id})
        self.assertEqual(status, 201)
        return payload["entry"]["queue_id"]

    def test_all_operator_routes_serve_actual_html_and_unknown_paths_reject(self):
        expected = (self.server_module.STATIC_DIR / "operator.html").read_text("utf-8").replace(
            "__BUILD__", self.server_module.PAGE_BUILD).encode("utf-8")
        for path in ("/operator", "/operator/", "/operator/queue", "/operator/photos",
                     "/operator/styles", "/operator/settings", "/operator/system"):
            with self.subTest(path=path):
                status, headers, body = self.request(path)
                self.assertEqual(status, 200)
                self.assertIn("text/html", headers["Content-Type"])
                self.assertEqual(body, expected)
        for path in ("/operator/unknown", "/operator/styles/unknown", "/operator/queue/extra"):
            with self.subTest(path=path):
                status, payload = self.request_json(path)
                self.assertEqual(status, 404)
                self.assertEqual(payload["error"], "not_found")

    def test_get_create_enable_default_and_delete_contract(self):
        status, first = self.request_json("/api/operator/aigc/styles")
        self.assertEqual(status, 200)
        self.assertTrue(first["ok"])
        custom = dict(first["styles"][2], id="enabled-copy", title="已启用副本", is_default=False)
        status, result = self.request_json("/api/operator/aigc/styles", custom)
        self.assertEqual(status, 201)
        status, public = self.request_json("/api/aigc/styles")
        self.assertEqual(status, 200)
        self.assertEqual(public["styles"][-1]["id"], "enabled-copy")
        for item in result["styles"]:
            item["is_default"] = item["id"] == "enabled-copy"
        status, result = self.request_json("/api/operator/aigc/styles", {"styles": result["styles"]})
        self.assertEqual(status, 200)
        self.assertTrue(result["styles"][-1]["is_default"])
        status, result = self.request_json("/api/operator/aigc/styles/enabled-copy/delete", {})
        self.assertEqual(status, 200)
        self.assertTrue(result["ok"])
        self.assertTrue(result["styles"][0]["is_default"])
        self.assertNotIn("enabled-copy", {item["id"] for item in result["styles"]})
        status, error = self.request_json("/api/operator/aigc/styles/enabled-copy/delete", {})
        self.assertEqual((status, error["error"]), (404, "style_not_found"))
        self.assertFalse(error["ok"])
        self.assertTrue(error["detail"])
        status, error = self.request_json("/api/operator/aigc/styles", {"styles": "invalid"})
        self.assertEqual((status, error["error"]), (400, "invalid_style"))

    def test_busy_queue_states_reject_delete_disable_bulk_and_rollback_without_writes(self):
        self.catalog.upsert({"enabled": False}, "pixel-person")
        self.catalog.upsert({"enabled": True}, "pixel-person")
        queue_id = self.submit()
        queue = self.booth.auto_flow.queue
        transitions = (None, queue.call_next, lambda: queue.start(queue_id),
                       lambda: queue.mark_capturing(queue_id), lambda: queue.mark_processing(queue_id))
        for transition in transitions:
            if transition:
                transition()
            original_queue = queue.snapshot()
            before = self.catalog.snapshot()
            persisted = self.catalog.path.read_bytes()
            candidates = [dict(item, enabled=False) if item["id"] == "pixel-person" else item
                          for item in before["styles"]]
            without_active = [item for item in before["styles"] if item["id"] != "pixel-person"]
            calls = (
                ("/api/operator/aigc/styles/pixel-person/delete", {}),
                ("/api/operator/aigc/styles/pixel-person", {"enabled": False}),
                ("/api/operator/aigc/styles", {"styles": candidates}),
                ("/api/operator/aigc/styles", {"styles": without_active}),
                ("/api/operator/aigc/styles/pixel-person/rollback", {"revision": 2}))
            for path, payload in calls:
                with self.subTest(status=original_queue["entries"][0]["status"], path=path):
                    status, result = self.request_json(path, payload)
                    self.assertEqual((status, result["error"]), (409, "STYLE_IN_USE"))
                    self.assertEqual(self.catalog.path.read_bytes(), persisted)
                    self.assertEqual(queue.snapshot(), original_queue)
            status, result = self.request_json("/api/operator/aigc/styles/pixel-person",
                                               {"description": "排队时可修改说明"})
            self.assertEqual(status, 200)
        queue.mark_ready_for_pickup(queue_id)
        status, result = self.request_json("/api/operator/aigc/styles/pixel-person/delete", {})
        self.assertEqual(status, 200)

    def test_manual_active_jobs_and_last_enabled_conflicts(self):
        style_id = EXPECTED_IDS[0]
        job = self.booth.jobs.create(style_id)
        status, result = self.request_json("/api/operator/aigc/styles/%s/delete" % style_id, {})
        self.assertEqual((status, result["error"]), (409, "STYLE_IN_USE"))
        job.stage = "done"
        job.finished_at = 1
        candidates = self.catalog.snapshot()["styles"]
        for item in candidates[1:]:
            item["enabled"] = False
        self.booth.save_aigc_catalog(candidates)
        status, result = self.request_json("/api/operator/aigc/styles/%s/delete" % style_id, {})
        self.assertEqual((status, result["error"]), (409, "last_enabled_style"))
        status, result = self.request_json("/api/operator/aigc/styles/%s" % style_id, {"enabled": False})
        self.assertEqual((status, result["error"]), (409, "last_enabled_style"))
        self.assertIn("启用风格", result["detail"])

    def test_get_style_failure_returns_json_error(self):
        with patch.object(self.catalog, "snapshot", side_effect=CatalogError("风格目录不可用")), patch.object(
            self.server_module, "log"
        ):
            status, result = self.request_json("/api/operator/aigc/styles")
        self.assertEqual(status, 500)
        self.assertEqual(result, {"ok": False, "error": "internal", "detail": "风格目录不可用"})

    def test_channel_api_saves_secrets_privately_and_reports_health(self):
        status, initial = self.request_json("/api/operator/aigc/channels")
        self.assertEqual(status, 200)
        self.assertEqual(initial["channels"], [])
        channel = {
            "id": "primary", "name": "主渠道", "base_url": "https://api.example/v1",
            "models": ["image-model"], "api_key": "private-test-key", "enabled": True,
            "priority": 1,
        }
        with patch.object(self.booth.aigc_channels, "_ensure_monitor"):
            status, saved = self.request_json("/api/operator/aigc/channels", {"channels": [channel]})
        self.assertEqual(status, 200)
        self.assertTrue(saved["channels"][0]["key_configured"])
        self.assertNotIn("api_key", saved["channels"][0])
        self.assertNotIn("private-test-key", json.dumps(saved))

        with patch.object(self.booth.aigc_channels, "_probe", return_value={
            "health": "available", "detail": "连接正常", "latency_ms": 5,
        }):
            status, checked = self.request_json("/api/operator/aigc/channels/check", {"id": "primary"})
        self.assertEqual(status, 200)
        self.assertEqual(checked["channels"][0]["health"], "available")

        status, invalid = self.request_json("/api/operator/aigc/channels", {
            "channels": [dict(channel, base_url="file:///tmp/key")]
        })
        self.assertEqual((status, invalid["error"]), (400, "invalid_channels"))

    def test_channel_generation_uses_exact_channel_without_creating_photo(self):
        channel = {
            "id": "primary", "name": "主渠道", "base_url": "https://api.example/v1",
            "models": ["image-model"], "api_key": "private-test-key", "enabled": True,
            "priority": 1,
        }
        with patch.object(self.booth.aigc_channels, "_ensure_monitor"):
            self.booth.aigc_channels.save({"channels": [channel]})
        source = io.BytesIO()
        Image.new("RGB", (18, 24), "navy").save(source, "PNG")
        generated = Image.new("RGB", (32, 32), "orange")
        before_job = self.booth.jobs.latest()
        with patch("aigc_client.edit_portrait_with_channel", return_value=generated) as edit:
            status, result = self.request_multipart(
                "/api/operator/aigc/channels/test",
                {"id": "primary"}, source.getvalue())
        self.assertEqual(status, 200)
        self.assertTrue(result["ok"])
        self.assertEqual(result["channel"], {"id": "primary", "name": "主渠道"})
        self.assertEqual(result["model"], "image-model")
        self.assertTrue(result["image"].startswith("data:image/jpeg;base64,"))
        base64.b64decode(result["image"].split(",", 1)[1], validate=True)
        self.assertNotIn("api_key", json.dumps(result))
        self.assertIs(self.booth.jobs.latest(), before_job)
        self.assertEqual(edit.call_count, 1)
        kwargs = edit.call_args.kwargs
        self.assertEqual(kwargs["model"], "image-model")
        self.assertEqual(kwargs["channel"]["id"], "primary")

    def test_channel_generation_validates_image_channel_and_model(self):
        channel = {
            "id": "primary", "name": "主渠道", "base_url": "https://api.example/v1",
            "models": ["image-model"], "api_key": "private-test-key", "enabled": True,
            "priority": 1,
        }
        with patch.object(self.booth.aigc_channels, "_ensure_monitor"):
            self.booth.aigc_channels.save({"channels": [channel]})
        source = io.BytesIO()
        Image.new("RGB", (18, 24), "navy").save(source, "PNG")
        cases = (
            ({"id": "primary", "model": "image-model"}, None, 400, "image_required"),
            ({"id": "missing", "model": "image-model"}, source.getvalue(), 404, "channel_not_found"),
            ({"id": "primary", "model": "missing-model"}, source.getvalue(), 400, "invalid_model"),
            ({"id": "primary", "model": "image-model"}, b"not an image", 400, "invalid_image"),
        )
        for fields, image, expected_status, expected_error in cases:
            with self.subTest(error=expected_error):
                status, result = self.request_multipart(
                    "/api/operator/aigc/channels/test", fields, image)
                self.assertEqual((status, result["error"]),
                                 (expected_status, expected_error))
        from aigc_client import AIGCError
        with patch("aigc_client.edit_portrait_with_channel", side_effect=AIGCError("AIGC 请求失败 HTTP 503")):
            status, result = self.request_multipart(
                "/api/operator/aigc/channels/test",
                {"id": "primary", "model": "image-model"}, source.getvalue())
        self.assertEqual((status, result["error"]), (502, "generation_failed"))
        self.assertNotIn("private-test-key", json.dumps(result))

    def test_active_manual_job_is_not_evicted_from_busy_checks(self):
        self.booth.jobs = self.server_module.JobBoard(keep=1)
        first = self.booth.jobs.create("pixel-person")
        self.booth.jobs.create("hand-doodle")
        self.assertIs(self.booth.jobs.get(first.id), first)
        status, result = self.request_json("/api/operator/aigc/styles/pixel-person/delete", {})
        self.assertEqual((status, result["error"]), (409, "STYLE_IN_USE"))

    def test_deleted_style_is_rejected_on_queue_submit(self):
        self.booth.delete_aigc_style("pixel-person")
        consent = self.booth.auto_flow.consent.record("capture", True)
        status, result = self.request_json("/api/queue", {
            "request_id": consent.consent_id, "style_id": "pixel-person",
            "capture_consent_id": consent.consent_id})
        self.assertEqual(status, 404)
        self.assertEqual(result["error"]["code"], "STYLE_NOT_FOUND")
        self.assertEqual(self.booth.auto_flow.queue.snapshot()["total"], 0)

    def test_submit_and_delete_are_serialized_before_persisting_queue(self):
        entered = threading.Event()
        release = threading.Event()
        delete_started = threading.Event()
        results = []
        failures = []
        original = self.booth.auto_flow.queue.submit_result

        def paused_submit(submission):
            entered.set()
            if not release.wait(4):
                raise RuntimeError("test did not release submit")
            return original(submission)

        def submit_worker():
            try:
                self.submit()
            except Exception as error:
                failures.append(error)

        def delete_worker():
            delete_started.set()
            try:
                results.append(self.request_json("/api/operator/aigc/styles/pixel-person/delete", {}))
            except Exception as error:
                failures.append(error)

        with patch.object(self.booth.auto_flow.queue, "submit_result", side_effect=paused_submit):
            submit_thread = threading.Thread(target=submit_worker)
            delete_thread = threading.Thread(target=delete_worker)
            submit_thread.start()
            try:
                self.assertTrue(entered.wait(3))
                delete_thread.start()
                self.assertTrue(delete_started.wait(3))
                self.assertEqual(results, [])
            finally:
                release.set()
                submit_thread.join(5)
                if delete_thread.ident is not None:
                    delete_thread.join(5)
        self.assertEqual(failures, [])
        self.assertEqual(results[0][0], 409)
        self.assertEqual(results[0][1]["error"], "STYLE_IN_USE")
        self.assertEqual(self.booth.auto_flow.queue.snapshot()["total"], 1)

    def test_restart_invalidates_session_and_changes_ready_to_failed(self):
        queue_id = self.submit()
        services = self.booth.auto_flow
        token = services.sessions.issue(queue_id)
        self.assertTrue(services.sessions.check(token, queue_id))
        services.queue.call_next()
        services.queue.start(queue_id)
        restarted = QueueService(services.queue.store)
        self.assertEqual(restarted.get(queue_id).status, QueueStatus.FAILED)
        self.assertFalse(QueueSessionRegistry().check(token, queue_id))


if __name__ == "__main__":
    unittest.main()
