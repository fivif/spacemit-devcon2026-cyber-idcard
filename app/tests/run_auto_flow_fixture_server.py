# -*- coding: utf-8 -*-
"""T-14 夹具服务启动器：仅回环 127.0.0.1:18090 的最小 HTTP 夹具。

用法（项目根目录）::

    python3 app/tests/run_auto_flow_fixture_server.py --scenario skeleton
    python3 app/tests/run_auto_flow_fixture_server.py --scenario external-consent --port 18090

约束（规格 execution_contract：fixture_port=18090、fixture_bind=127.0.0.1）：
- 只能以测试方式启动：``load_config(env, allow_test_mode=True)`` + ``BOOTH_TEST_MODE=1``；
  生产语义（默认 allow_test_mode=False）下同一份配置会被拒绝（见 test_auto_flow_fixtures.py）。
- 只允许绑定回环地址（127.0.0.1 / localhost / ::1），其它 host 一律拒绝启动。
- 启动成功在 stdout 打印 ``FIXTURE_URL: http://127.0.0.1:<实际端口>`` 与测试同意令牌。
- 固定健康 JSON 读取 ``app/tests/fixtures/auto_flow_health.json``。
- HTTP 用标准库最小 handler 实现，不复用生产路由；只服务本机测试，请勿用于生产。

端点：
    GET  /                        纯文本端点清单
    GET  /api/health              固定健康 JSON（SystemHealth 六组件）
    GET  /api/scenario            场景名、步骤状态、请求计数
    POST /api/consent             {"purpose":"capture"|"external_processing"} -> 发测试同意令牌
    POST /api/capture             {"consent_id": ...} 或 X-Booth-Session 头；未同意 403；
                                  有效令牌 -> 假相机合成帧 + PhotoStore.commit 结果
    POST /api/external-processing {"job_id","external_consent_id","style_id"} -> 假网关风格化
    POST /api/shutdown            夹具专用优雅停机（测试结束后清理临时目录）
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_PROJECT_ROOT = _HERE.parents[1]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from app.auto_flow import errors
from app.auto_flow.config import Config, ConfigError, load_config
from app.tests.auto_flow_fixtures import AutoFlowFixtures, HEALTH_FIXTURE_PATH, SCENARIO_STEPS, fixture_env

__all__ = ["DEFAULT_HOST", "DEFAULT_PORT", "ALLOWED_HOSTS", "require_loopback", "load_fixture_config", "FixtureServer", "main"]

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 18090
ALLOWED_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
MAX_BODY_BYTES = 8192


def require_loopback(host: str) -> str:
    """只允许回环地址；其它（含 0.0.0.0 与空串）直接拒绝。"""
    text = (host or "").strip()
    if text not in ALLOWED_HOSTS:
        raise ValueError(
            f"夹具服务只允许绑定回环地址 {sorted(ALLOWED_HOSTS)}，收到：{host!r}"
        )
    return text


def load_fixture_config(host: str = DEFAULT_HOST, port: int = DEFAULT_PORT) -> Config:
    """仅测试方式加载配置：BOOTH_TEST_MODE=1 + allow_test_mode=True（生产语义下拒绝）。"""
    return load_config(env=fixture_env(host, port), allow_test_mode=True)


def make_handler(container: AutoFlowFixtures, shutdown_callback=None):
    """构造最小 handler（闭包持有容器）；不复用生产路由。"""

    class FixtureHandler(BaseHTTPRequestHandler):
        server_version = "AutoFlowFixture/1.0"

        def log_message(self, format: str, *args) -> None:  # noqa: A002 - 基类签名
            print(f"[fixture] {self.address_string()} {format % args}", file=sys.stderr, flush=True)

        # ---- 工具 -------------------------------------------------------
        def _request_id(self) -> str:
            return self.headers.get("X-Request-Id") or uuid.uuid4().hex[:12]

        def _send_json(self, status: int, payload: object) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _send_text(self, text: str, status: int = 200) -> None:
            body = text.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _not_found(self, request_id: str) -> None:
            self._send_json(
                404,
                {
                    "error": {
                        "code": "FIXTURE_NOT_FOUND",
                        "message": "夹具服务没有该端点。",
                        "request_id": request_id,
                    }
                },
            )

        def _read_json(self, request_id: str) -> dict | None:
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                length = -1
            if length < 0 or length > MAX_BODY_BYTES:
                self._send_json(
                    400,
                    errors.error_payload(errors.INVALID_REQUEST, request_id, message="请求体长度不合法。"),
                )
                return None
            if length == 0:
                return {}
            raw = self.rfile.read(length)
            try:
                payload = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                self._send_json(
                    400,
                    errors.error_payload(errors.INVALID_REQUEST, request_id, message="请求体必须是 JSON。"),
                )
                return None
            if not isinstance(payload, dict):
                self._send_json(
                    400,
                    errors.error_payload(errors.INVALID_REQUEST, request_id, message="请求体必须是 JSON 对象。"),
                )
                return None
            return payload

        # ---- 路由 -------------------------------------------------------
        def do_GET(self) -> None:  # noqa: N802 - 基类命名
            path = self.path.split("?", 1)[0]
            request_id = self._request_id()
            if path == "/":
                self._send_text(
                    "auto_flow 测试夹具服务（仅测试模式，仅回环，请勿用于生产）\n"
                    "GET  /api/health\nGET  /api/scenario\n"
                    "POST /api/consent\nPOST /api/capture\nPOST /api/external-processing\n"
                )
                return
            if path == "/api/health":
                self._send_json(200, container.health_snapshot())
                return
            if path == "/api/scenario":
                self._send_json(200, container.scenario_state())
                return
            self._not_found(request_id)

        def do_POST(self) -> None:  # noqa: N802 - 基类命名
            path = self.path.split("?", 1)[0]
            request_id = self._request_id()
            body = self._read_json(request_id)
            if body is None:
                return
            try:
                if path == "/api/consent":
                    purpose = str(body.get("purpose") or "capture")
                    if purpose not in ("capture", "external_processing"):
                        self._send_json(
                            400,
                            errors.error_payload(errors.INVALID_REQUEST, request_id, message="purpose 只支持 capture / external_processing。"),
                        )
                        return
                    self._send_json(
                        200,
                        {"ok": True, "consent_id": container.grant_consent(purpose), "purpose": purpose},
                    )
                    return
                if path == "/api/capture":
                    consent_id = self.headers.get("X-Booth-Session") or body.get("consent_id")
                    result = container.capture(consent_id, job_id=body.get("job_id"))
                    self._send_json(200, result)
                    return
                if path == "/api/shutdown":
                    # 夹具专用：优雅停机，让 finally 里的临时目录清理真正执行。
                    self._send_json(200, {"ok": True, "message": "夹具服务即将停止。"})
                    if shutdown_callback is not None:
                        threading.Thread(target=shutdown_callback, name="auto-flow-fixture-shutdown", daemon=True).start()
                    return
                if path == "/api/external-processing":
                    result = container.process_external(
                        job_id=str(body.get("job_id") or ""),
                        external_consent_id=body.get("external_consent_id"),
                        style_id=str(body.get("style_id") or "neon"),
                        source_path=body.get("source_path"),
                    )
                    self._send_json(200, result)
                    return
                self._not_found(request_id)
            except errors.ApiError as exc:
                self._send_json(exc.http_status, exc.payload(request_id))

    return FixtureHandler


class _FixtureHTTPServer(ThreadingHTTPServer):
    """关闭 SO_REUSEADDR：同一端口二次启动必须显式失败。

    Windows 上 allow_reuse_address=True（ThreadingHTTPServer 默认）时，第二个进程
    可以悄悄绑上已被占用的端口，导致请求流向不确定——夹具服务不允许这种状态。
    """

    allow_reuse_address = False
    daemon_threads = True


class FixtureServer:
    """可编程启动的夹具 HTTP 服务（线程模式）；CLI main() 复用同一实现。"""

    def __init__(
        self,
        scenario: str = "skeleton",
        host: str = DEFAULT_HOST,
        port: int = DEFAULT_PORT,
        root: Path | None = None,
    ) -> None:
        self.host = require_loopback(host)  # 构造即拒绝非回环，容器尚未创建
        self.requested_port = int(port)
        self.container = AutoFlowFixtures(scenario=scenario, root=root, host=self.host, port=self.requested_port)
        self.httpd: ThreadingHTTPServer | None = None
        self.thread: threading.Thread | None = None
        self.url: str | None = None

    def start(self) -> str:
        """启动服务并返回实际 URL（端口 0 时由系统分配，URL 反映真实端口）。"""
        if self.httpd is not None:
            raise RuntimeError("夹具服务已经启动。")
        self.httpd = _FixtureHTTPServer((self.host, self.requested_port), make_handler(self.container, self.stop))
        self.httpd.daemon_threads = True
        actual_host, actual_port = self.httpd.server_address[:2]
        self.url = f"http://{actual_host}:{actual_port}"
        self.thread = threading.Thread(target=self.httpd.serve_forever, name="auto-flow-fixture-server", daemon=True)
        self.thread.start()
        return self.url

    def stop(self) -> None:
        """停止 HTTP 服务并释放端口（临时目录由 close() 清理）。"""
        if self.httpd is not None:
            self.httpd.shutdown()
            self.httpd.server_close()
            self.httpd = None
        if self.thread is not None:
            self.thread.join(timeout=5)
            self.thread = None
        self.url = None

    def close(self) -> None:
        self.stop()
        self.container.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="auto_flow 测试夹具服务（仅测试模式，仅回环）")
    parser.add_argument("--scenario", choices=sorted(SCENARIO_STEPS), default="skeleton", help="场景（默认 skeleton）")
    parser.add_argument("--host", default=DEFAULT_HOST, help="只允许回环地址（默认 127.0.0.1）")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="端口（默认 18090；0 表示自动分配）")
    # 统一输出编码：管道/子进程读取端按 UTF-8 解析（Windows 区域默认可能是 GBK）
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8")

    args = parser.parse_args(argv)

    try:
        server = FixtureServer(scenario=args.scenario, host=args.host, port=args.port)
    except (ValueError, ConfigError) as exc:
        print(f"启动失败：{exc}", file=sys.stderr)
        return 2

    try:
        url = server.start()
    except OSError as exc:
        print(f"启动失败：端口 {args.port} 不可用（{exc}）", file=sys.stderr)
        server.container.close()
        return 2

    print("auto_flow 测试夹具服务（仅测试模式，仅回环，请勿用于生产）")
    print(f"FIXTURE_URL: {url}")
    print(f"FIXTURE_SCENARIO: {server.container.scenario}")
    print(f"FIXTURE_TEMP_ROOT: {server.container.root}")
    print(f"测试同意令牌（capture）：{server.container.consent_token}")
    print(f"固定健康 JSON：{HEALTH_FIXTURE_PATH}")
    sys.stdout.flush()
    try:
        assert server.thread is not None
        server.thread.join()
    except KeyboardInterrupt:
        print("\n收到中断，正在停止…")
    finally:
        server.close()
        print("夹具服务已停止，临时目录已清理。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())