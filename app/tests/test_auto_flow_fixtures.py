# -*- coding: utf-8 -*-
"""T-14 测试注入与夹具启动器测试。

运行（项目根目录）::

    python3 -m unittest app.tests.test_auto_flow_fixtures -v

覆盖 T-14 三条验收：
a) 假相机输出可解码合成 JPEG；未经同意的请求 → 403（容器与夹具服务两层，且相机零调用）；
b) 临时存储销毁不涉及生产路径；生产语义（默认 allow_test_mode=False）拒绝测试配置；
c) 启动器只监听回环地址、输出实际 URL，端口能干净释放（线程与子进程两种方式）。
"""

from __future__ import annotations

import json
import queue
import socket
import subprocess
import sys
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from PIL import Image

from app.auto_flow import errors
from app.auto_flow.config import ConfigError, load_config
from app.auto_flow.models import COMPONENT_STATUSES, PHOTO_VARIANTS, is_valid_photo_token
from app.auto_flow.ports import (
    CameraPort,
    CapturedFrame,
    Clock,
    GatewayPort,
    GesturePort,
    PhotoStorePort,
    PrintPort,
)
from app.tests.auto_flow_fixtures import (
    HEALTH_FIXTURE_PATH,
    PROJECT_ROOT,
    SCENARIO_STEPS,
    AutoFlowFixtures,
    FakeClock,
    FakePhotoStore,
    fixture_env,
)
from app.tests.run_auto_flow_fixture_server import FixtureServer, require_loopback

LAUNCHER_PATH = PROJECT_ROOT / "app" / "tests" / "run_auto_flow_fixture_server.py"
LOOPBACK = "127.0.0.1"


def http_json(url: str, method: str = "GET", payload: dict | None = None, headers: dict | None = None, timeout: float = 15.0):
    """极简 HTTP 客户端：返回 (状态码, JSON)；HTTPError 也按 JSON 解析。"""
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={"Content-Type": "application/json; charset=utf-8", **(headers or {})},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


def port_open(host: str, port: int, timeout: float = 0.5) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def wait_port_closed(host: str, port: int, timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not port_open(host, port, timeout=0.3):
            return True
        time.sleep(0.1)
    return False


def assert_decodable_jpeg(test: unittest.TestCase, path: Path) -> None:
    test.assertTrue(path.is_file(), f"图像不存在：{path}")
    with Image.open(path) as image:
        image.load()
        test.assertEqual(image.format, "JPEG")
        test.assertGreaterEqual(image.size[0], 16)
        test.assertGreaterEqual(image.size[1], 16)
    with Image.open(path) as image:
        image.verify()  # 完整解码路径的二次校验


class FakeCameraAcceptanceTest(unittest.TestCase):
    """验收 a 前半：假相机输出可解码合成 JPEG；脚本化行为可用。"""

    def setUp(self) -> None:
        self.fx = AutoFlowFixtures(scenario="skeleton")
        self.addCleanup(self.fx.close)

    def test_capture_authorized_writes_decodable_synthetic_jpeg(self) -> None:
        frame = self.fx.camera.capture_authorized("consent-test")
        self.assertIsInstance(frame, CapturedFrame)
        path = Path(frame.source_path)
        assert_decodable_jpeg(self, path)
        self.assertEqual(frame.captured_at, self.fx.clock.iso())
        self.assertTrue(path.is_relative_to(self.fx.root), f"帧未写临时目录：{path}")

    def test_clock_is_scriptable_and_timestamps_follow_it(self) -> None:
        first = self.fx.camera.capture_authorized("c1")
        self.fx.clock.advance(120)
        second = self.fx.camera.capture_authorized("c2")
        self.assertNotEqual(first.captured_at, second.captured_at)
        self.assertEqual(second.captured_at, self.fx.clock.iso())

    def test_scripts_for_delay_failure_and_specific_frame(self) -> None:
        self.fx.camera.script_delay(0.05)
        started = time.monotonic()
        self.fx.camera.capture_authorized("c1")
        self.assertGreaterEqual(time.monotonic() - started, 0.04)
        self.fx.camera.script_failure("脚本化故障：相机不可用")
        with self.assertRaises(RuntimeError) as ctx:
            self.fx.camera.capture_authorized("c2")
        self.assertIn("脚本化故障", str(ctx.exception))
        reference = self.fx.root / "reference.png"
        Image.new("RGB", (48, 64), (12, 34, 56)).save(reference)
        self.fx.camera.script_frame(copy_from=reference)
        frame = self.fx.camera.capture_authorized("c3")
        with Image.open(frame.source_path) as image:
            self.assertEqual(image.size, (48, 64))

    def test_health_is_scriptable(self) -> None:
        self.assertEqual(self.fx.camera.health().status, "ok")
        self.fx.camera.set_health("unavailable", "无帧")
        health = self.fx.camera.health()
        self.assertIn(health.status, COMPONENT_STATUSES)
        self.assertEqual(health.status, "unavailable")
        self.assertEqual(health.reason, "无帧")
        self.assertEqual(health.checked_at, self.fx.clock.iso())

    def test_fake_adapters_satisfy_ports(self) -> None:
        self.assertIsInstance(self.fx.camera, CameraPort)
        self.assertIsInstance(self.fx.clock, Clock)
        self.assertIsInstance(self.fx.gesture, GesturePort)
        self.assertIsInstance(self.fx.gateway, GatewayPort)
        self.assertIsInstance(self.fx.printer, PrintPort)
        self.assertIsInstance(self.fx.photo_store, PhotoStorePort)


class CaptureConsentOrderTest(unittest.TestCase):
    """验收 a 后半：先同意检查、再调用注入相机；未同意 403 且相机零调用。"""

    def setUp(self) -> None:
        self.fx = AutoFlowFixtures(scenario="skeleton")
        self.addCleanup(self.fx.close)

    def test_capture_without_consent_raises_403_and_never_calls_camera(self) -> None:
        for bad in (None, "", "forged-token"):
            with self.assertRaises(errors.ApiError) as ctx:
                self.fx.capture(bad)
            self.assertEqual(ctx.exception.code, errors.CONSENT_REQUIRED)
            self.assertEqual(ctx.exception.http_status, 403)
        self.assertEqual(self.fx.camera.calls, [], "未同意时相机不应被调用")

    def test_capture_consent_cannot_be_replaced_by_external_consent(self) -> None:
        external = self.fx.grant_consent("external_processing")
        with self.assertRaises(errors.ApiError):
            self.fx.capture(external)
        self.assertEqual(self.fx.camera.calls, [])

    def test_capture_with_valid_token_returns_frame_and_photo_record(self) -> None:
        result = self.fx.capture(self.fx.consent_token)
        self.assertTrue(result["ok"])
        self.assertEqual(self.fx.camera.calls, [self.fx.consent_token])
        assert_decodable_jpeg(self, Path(result["source_path"]))
        self.assertTrue(is_valid_photo_token(result["photo"]["photo_token"]))
        self.assertIn(result["photo"]["variant"], PHOTO_VARIANTS)
        self.assertEqual(result["photo"]["variant"], "L0")
        self.assertIsNotNone(self.fx.photo_store.get(result["photo"]["photo_token"]))

    def test_external_processing_requires_its_own_consent(self) -> None:
        result = self.fx.capture(self.fx.consent_token)
        with self.assertRaises(errors.ApiError) as ctx:
            self.fx.process_external(result["job_id"], result.get("consent_id"))
        self.assertEqual(ctx.exception.http_status, 403)
        self.assertEqual(len(self.fx.gateway.requests), 0)
        external = self.fx.grant_consent("external_processing")
        styled = self.fx.process_external(result["job_id"], external, style_id="neon")
        self.assertEqual(len(self.fx.gateway.requests), 1)
        assert_decodable_jpeg(self, Path(styled["styled_path"]))
        self.assertEqual(styled["photo"]["variant"], "styled")


class FixtureServerHttpTest(unittest.TestCase):
    """HTTP 夹具（线程内启动）：固定健康 JSON、/api/capture 403/200、场景状态。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.server = FixtureServer(scenario="skeleton", host=LOOPBACK, port=0)
        cls.url = cls.server.start()
        assert cls.server.httpd is not None
        cls.port = int(cls.server.httpd.server_address[1])

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.close()
        wait_port_closed(LOOPBACK, cls.port, timeout=10)

    def test_bind_is_loopback_only(self) -> None:
        assert self.server.httpd is not None
        self.assertEqual(self.server.httpd.server_address[0], LOOPBACK)
        self.assertTrue(self.url.startswith(f"http://{LOOPBACK}:"), self.url)
        self.assertTrue(port_open(LOOPBACK, self.port))

    def test_non_loopback_host_is_refused(self) -> None:
        for host in ("0.0.0.0", "192.0.2.10.10", "", "  "):
            with self.assertRaises(ValueError):
                require_loopback(host)
        with self.assertRaises(ValueError):
            FixtureServer(host="0.0.0.0", port=0)

    def test_health_endpoint_serves_fixed_fixture(self) -> None:
        status, payload = http_json(self.url + "/api/health")
        self.assertEqual(status, 200)
        fixed = json.loads(HEALTH_FIXTURE_PATH.read_text(encoding="utf-8"))
        self.assertEqual(payload, fixed)
        self.assertEqual(payload["camera"]["status"], "unavailable")
        self.assertEqual(payload["gesture"]["status"], "disabled")
        self.assertEqual(payload["aigc"]["status"], "disabled")
        self.assertEqual(payload["printer"]["status"], "disabled")
        self.assertEqual(payload["queue"]["status"], "ok")
        self.assertEqual(payload["storage"]["status"], "ok")

    def test_capture_without_consent_returns_403_and_never_calls_camera(self) -> None:
        before = len(self.server.container.camera.calls)
        status, payload = http_json(self.url + "/api/capture", method="POST", payload={})
        self.assertEqual(status, 403)
        self.assertEqual(payload["error"]["code"], "CONSENT_REQUIRED")
        self.assertEqual(len(self.server.container.camera.calls), before)

    def test_capture_with_valid_token_returns_decodable_frame(self) -> None:
        status, issued = http_json(self.url + "/api/consent", method="POST", payload={"purpose": "capture"})
        self.assertEqual(status, 200)
        status, payload = http_json(self.url + "/api/capture", method="POST", payload={"consent_id": issued["consent_id"]})
        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])
        assert_decodable_jpeg(self, Path(payload["source_path"]))
        self.assertTrue(is_valid_photo_token(payload["photo"]["photo_token"]))

    def test_capture_accepts_session_header(self) -> None:
        status, payload = http_json(
            self.url + "/api/capture",
            method="POST",
            payload=None,
            headers={"X-Booth-Session": self.server.container.consent_token},
        )
        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])

    def test_scenario_state_reports_steps_and_counters(self) -> None:
        status, payload = http_json(self.url + "/api/scenario")
        self.assertEqual(status, 200)
        self.assertEqual(payload["scenario"], "skeleton")
        self.assertEqual([step["name"] for step in payload["steps"]], list(SCENARIO_STEPS["skeleton"]))
        self.assertIn("capture_ok", payload["requests"])
        self.assertGreaterEqual(payload["requests"]["capture_ok"], 1)
        steps = {step["name"]: step["status"] for step in payload["steps"]}
        self.assertEqual(steps["capture"], "done")


class TempStorageIsolationTest(unittest.TestCase):
    """验收 b 前半：临时存储销毁不涉及生产路径。"""

    def test_container_writes_only_under_temp_root_and_destroys_it(self) -> None:
        fx = AutoFlowFixtures(scenario="skeleton")
        root = fx.root
        self.assertFalse(root.is_relative_to(PROJECT_ROOT), f"临时根在项目内：{root}")
        try:
            result = fx.capture(fx.consent_token)
            external = fx.grant_consent("external_processing")
            styled = fx.process_external(result["job_id"], external, style_id="neon")
            self.assertEqual(len(fx.gateway.requests), 1)
            written = fx.written_paths()
            self.assertTrue(written, "夹具应当写出了临时文件")
            for path in written:
                self.assertTrue(path.is_relative_to(root), f"写到了临时根之外：{path}")
                self.assertFalse(path.is_relative_to(PROJECT_ROOT), f"写到了项目仓库：{path}")
            self.assertTrue(Path(styled["styled_path"]).is_relative_to(root))
        finally:
            fx.close()
        self.assertFalse(root.exists(), "临时目录应当被销毁")

    def test_photo_store_refuses_project_paths(self) -> None:
        clock = FakeClock()
        with self.assertRaises(ValueError):
            FakePhotoStore(PROJECT_ROOT / "app" / "data", clock)
        with self.assertRaises(ValueError):
            FakePhotoStore(PROJECT_ROOT, clock)


class PortConflictTest(unittest.TestCase):
    """同一端口二次启动必须显式失败（Windows 上不得悄悄双绑定）。"""

    def test_second_instance_refuses_busy_port(self) -> None:
        first = FixtureServer(scenario="skeleton", host=LOOPBACK, port=0)
        first.start()
        assert first.httpd is not None
        port = int(first.httpd.server_address[1])
        second = FixtureServer(scenario="skeleton", host=LOOPBACK, port=port)
        self.addCleanup(second.container.close)
        with self.assertRaises(OSError):
            second.start()
        first.close()
        self.assertTrue(wait_port_closed(LOOPBACK, port, timeout=10), f"端口未释放：{port}")


class ProductionGateTest(unittest.TestCase):
    """验收 b 后半：生产语义拒绝加载测试适配器；夹具 env 只能配测试模式。"""

    def test_production_load_rejects_test_mode(self) -> None:
        with self.assertRaises(ConfigError) as ctx:
            load_config(env={"BOOTH_TEST_MODE": "1"}, allow_test_mode=False)
        self.assertEqual(ctx.exception.env_var, "BOOTH_TEST_MODE")

    def test_production_load_rejects_test_adapter_flag(self) -> None:
        with self.assertRaises(ConfigError) as ctx:
            load_config(env={"BOOTH_ALLOW_TEST_ADAPTERS": "1"}, allow_test_mode=False)
        self.assertEqual(ctx.exception.env_var, "BOOTH_ALLOW_TEST_ADAPTERS")

    def test_fixture_env_is_test_only_and_launcher_loads_it_in_test_mode(self) -> None:
        env = fixture_env(LOOPBACK, 18090)
        self.assertEqual(env["BOOTH_TEST_MODE"], "1")
        self.assertEqual(env["BOOTH_HOST"], LOOPBACK)
        self.assertEqual(env["BOOTH_PORT"], "18090")
        config = load_config(env=env, allow_test_mode=True)
        self.assertTrue(config.testing)
        with self.assertRaises(ConfigError):
            load_config(env=env, allow_test_mode=False)  # 生产语义下同一份 env 被拒

    def test_container_config_is_test_mode(self) -> None:
        fx = AutoFlowFixtures(scenario="entry")
        self.addCleanup(fx.close)
        self.assertTrue(fx.config.testing)
        self.assertEqual(fx.config.server.host, LOOPBACK)
        self.assertEqual(fx.scenario, "entry")


class CliLauncherTest(unittest.TestCase):
    """验收 c：启动器子进程只监听回环、输出实际 URL，端口干净释放。"""

    def _collect_until(self, process: subprocess.Popen, prefixes: tuple[str, ...], timeout: float):
        """持续读 stdout，直到所有 prefix 都出现（返回 {prefix: 值} 与已见行）或超时。"""
        lines: "queue.Queue[str | None]" = queue.Queue()
        seen: list[str] = []

        def reader() -> None:
            assert process.stdout is not None
            for line in process.stdout:
                lines.put(line)
            lines.put(None)

        threading.Thread(target=reader, daemon=True).start()
        found: dict[str, str] = {}
        deadline = time.monotonic() + timeout
        while len(found) < len(prefixes):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                line = lines.get(timeout=remaining)
            except queue.Empty:
                break
            if line is None:
                break
            text = line.rstrip("\n")
            seen.append(text)
            for prefix in prefixes:
                if prefix not in found and text.startswith(prefix):
                    found[prefix] = text[len(prefix):].strip()
        return found, seen

    def test_cli_prints_actual_url_and_releases_port(self) -> None:
        process = subprocess.Popen(
            [sys.executable, "-u", str(LAUNCHER_PATH), "--scenario", "entry", "--host", LOOPBACK, "--port", "0"],
            cwd=str(PROJECT_ROOT),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        port = None
        temp_root = None
        try:
            found, seen = self._collect_until(process, ("FIXTURE_URL: ", "FIXTURE_TEMP_ROOT: "), timeout=30)
            if "FIXTURE_URL: " not in found:
                stderr = process.stderr.read() if process.poll() is not None else ""
                self.fail(f"未在 stdout 找到 FIXTURE_URL；已见输出：{seen}；stderr：{stderr[:1500]}")
            url = found["FIXTURE_URL: "]
            self.assertTrue(url.startswith(f"http://{LOOPBACK}:"), url)
            port = int(url.rsplit(":", 1)[1])
            self.assertTrue(port_open(LOOPBACK, port), f"URL 端口未监听：{url}")
            status, payload = http_json(url + "/api/scenario")
            self.assertEqual(status, 200)
            self.assertEqual(payload["scenario"], "entry")
            temp_root = found.get("FIXTURE_TEMP_ROOT: ")
            self.assertTrue(temp_root, "启动器应打印夹具临时目录（用于验证清理）")
            status, payload = http_json(url + "/api/shutdown", method="POST", payload={})
            self.assertEqual(status, 200)
            self.assertTrue(payload["ok"])
            try:
                code = process.wait(timeout=20)  # 优雅退出：finally 已跑清理
            except subprocess.TimeoutExpired:
                self.fail("shutdown 后进程未退出")
            self.assertEqual(code, 0)
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=15)
            for stream in (process.stdout, process.stderr):
                if stream is not None and not stream.closed:
                    stream.close()
        if port is not None:
            self.assertTrue(wait_port_closed(LOOPBACK, port, timeout=10), f"端口未释放：{port}")
        if temp_root:
            self.assertFalse(Path(temp_root).exists(), f"临时目录未清理：{temp_root}")

    def test_cli_refuses_non_loopback_host(self) -> None:
        completed = subprocess.run(
            [sys.executable, "-u", str(LAUNCHER_PATH), "--host", "0.0.0.0", "--port", "0"],
            cwd=str(PROJECT_ROOT),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
        )
        self.assertEqual(completed.returncode, 2)
        self.assertIn("回环", completed.stdout + completed.stderr)


if __name__ == "__main__":
    unittest.main(verbosity=2)