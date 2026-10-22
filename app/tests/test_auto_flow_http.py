# -*- coding: utf-8 -*-
"""T-3 队列 HTTP 集成测试：真实 Handler + 临时存储 + 假适配器。

运行（项目根目录）：
    python3 -m unittest app.tests.test_auto_flow_http -v

规格依据：`docs/spec/自动流程规格-v1.md` 的三条队列路由契约与
`app/auto_flow/http_handlers.py` 的接线语义（错误优先级、会话私有性）。

测试设计（逐条对应 T-3 验收）：
- 真实 HTTP：每个用例起真实 booth_server.Handler（仅回环 127.0.0.1 + 随机端口），
  经 socket 发请求；成功/错误响应都断言 HTTP 状态 + JSON 字段；
- 临时存储：Booth 数据根用 tempfile 临时目录；queue.json / consent.json 都写在
  临时根内（用例内断言）；模块加载前把 BOOTH_DATA 指到临时目录，测试结束清理；
- 假适配器：假时钟（T-14 FakeClock，可推进）、内存风格目录、相机探针 =
  None / 返回 False 的可调用对象 / T-14 FakeCamera；队列子系统按 T-2 的注入点
  `booth.auto_flow = AutoFlowServices(...)` 装配（与 _t2 自测脚本同一入口）；
- 不联网、不碰真实相机与生产数据：进程内禁用代理（只可能访问回环），
  BOOTH_CAMERA=mock 双保险；开关关闭用例断言三条路由 404 且零副作用。

关于「未同意拍摄相机零调用」的诚实说明（T-3 验收第 3 条）：
本用例把 `booth.camera` 替换为计数假相机——任何属性触达都会计数并抛错，
所以「计数为零」证明的是应用层调用点（`Booth.camera`）零触达；它不追踪 V4L2
设备层系统调用，测试进程也从不打开真实设备。辅助断言（响应无 job、无照片
记录）同时成立。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
APP_DIR = PROJECT_ROOT / "app"

# ---- 导入 booth_server 之前先做环境隔离（它 import 时会读这些变量） ----------
# 1) BOOTH_DATA 指到临时目录：即使有代码路径回退到模块常量，也写不进项目里；
# 2) 队列开关清掉：Booth 默认不装配子系统，本测试统一走显式注入；
# 3) BOOTH_CAMERA=mock：万一有路径真的去开相机，也只会拿到合成帧，不碰设备。
_TEMP_BOOTH_DATA = Path(tempfile.mkdtemp(prefix="auto-flow-http-boothdata-"))
os.environ["BOOTH_DATA"] = str(_TEMP_BOOTH_DATA)
os.environ["BOOTH_CAMERA"] = "mock"
os.environ["BOOTH_ACCESS_LOG"] = "0"
os.environ.pop("BOOTH_QUEUE_ENABLED", None)

for _candidate in (str(PROJECT_ROOT), str(APP_DIR)):
    if _candidate not in sys.path:
        sys.path.insert(0, _candidate)


def setUpModule() -> None:
    """把所有 HTTP 客户端请求固定为直连（禁代理）：网络边界只有回环。"""
    urllib.request.install_opener(
        urllib.request.build_opener(urllib.request.ProxyHandler({}))
    )


def tearDownModule() -> None:
    """清理模块级临时目录（只删本次测试自己创建的目录）。"""
    shutil.rmtree(_TEMP_BOOTH_DATA, ignore_errors=True)


import booth_server  # noqa: E402  （必须在环境隔离之后导入）

from app.auto_flow.consent_service import ConsentService, ConsentStore  # noqa: E402
from app.auto_flow.http_handlers import (  # noqa: E402
    AutoFlowServices,
    QueueSessionRegistry,
)
from app.auto_flow.queue_service import QueueService  # noqa: E402
from app.auto_flow.queue_store import QueueStore  # noqa: E402
from app.tests.auto_flow_fixtures import (  # noqa: E402
    DEFAULT_CLOCK_START,
    FakeCamera,
    FakeClock,
)

STYLE_NEON = "neon"
STYLE_CHIBI = "soft-chibi"
STYLE_DISABLED = "ghost-style"      # 目录里存在但 enabled=false
STYLE_UNKNOWN = "no-such-style"
SESSION_RE = re.compile(r"^[A-Za-z0-9_-]{32}$")


def _style_source() -> dict:
    """内存风格目录（enabled_style_ids 的 Mapping 形态）：不读也不写真实目录文件。"""
    return {
        "styles": [
            {"id": STYLE_NEON, "enabled": True},
            {"id": STYLE_CHIBI, "enabled": True},
            {"id": STYLE_DISABLED, "enabled": False},
        ]
    }


class CountingCamera:
    """计数假相机：任何属性触达都计一次「应用层相机调用」，并立即抛错。"""

    def __init__(self) -> None:
        self.calls = 0

    def __getattr__(self, name: str):
        def _touched(*args, **kwargs):
            self.calls += 1
            raise RuntimeError("未同意路径不应调用相机（camera.%s）" % name)

        return _touched


class AutoFlowHttpTestCase(unittest.TestCase):
    """公共夹具：真 Booth + 真 Handler（127.0.0.1 随机端口）+ 临时数据根。"""

    with_services = True
    queue_switch = None          # None = 不设 BOOTH_QUEUE_ENABLED；字符串 = 显式取值
    max_active = None
    start_timeout_seconds = 120
    consent_ttl_seconds = 600
    optional_fields_allowlist = ("name",)

    def setUp(self) -> None:
        self._apply_queue_switch()
        self.tmp = Path(tempfile.mkdtemp(prefix="auto-flow-http-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.clock = FakeClock(DEFAULT_CLOCK_START)
        self.booth = booth_server.Booth(data_dir=self.tmp)
        self.services = None
        if self.with_services:
            self.install_services()
        self._start_server()

    def _apply_queue_switch(self) -> None:
        previous = os.environ.get("BOOTH_QUEUE_ENABLED")

        def _restore() -> None:
            if previous is None:
                os.environ.pop("BOOTH_QUEUE_ENABLED", None)
            else:
                os.environ["BOOTH_QUEUE_ENABLED"] = previous

        self.addCleanup(_restore)
        if self.queue_switch is None:
            os.environ.pop("BOOTH_QUEUE_ENABLED", None)
        else:
            os.environ["BOOTH_QUEUE_ENABLED"] = self.queue_switch

    # ---- 装配 ------------------------------------------------------------
    def install_services(self, **overrides) -> AutoFlowServices:
        """按 T-2 的注入点装配队列子系统（可重复调用以换参数）。"""
        params = {
            "max_active": self.max_active,
            "start_timeout_seconds": self.start_timeout_seconds,
            "consent_ttl_seconds": self.consent_ttl_seconds,
            "optional_fields_allowlist": self.optional_fields_allowlist,
            "camera_probe": None,
            "gesture_enabled": False,
        }
        params.update(overrides)
        services = AutoFlowServices(
            queue=QueueService(
                QueueStore(self.tmp),
                max_active=params["max_active"],
                start_timeout_seconds=params["start_timeout_seconds"],
                idempotency_ttl_seconds=86400,
                optional_fields_allowlist=params["optional_fields_allowlist"],
                clock=self.clock,
                event_year=2026,
            ),
            consent=ConsentService(
                ConsentStore(self.tmp),
                ttl_seconds=params["consent_ttl_seconds"],
                clock=self.clock,
            ),
            sessions=QueueSessionRegistry(),
            style_source=_style_source(),
            camera_probe=params["camera_probe"],
            gesture_enabled=params["gesture_enabled"],
            clock=self.clock,
        )
        self.services = services
        self.booth.auto_flow = services
        return services

    def _start_server(self) -> None:
        booth_server.Handler.booth = self.booth
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), booth_server.Handler)
        self.server.daemon_threads = True
        self.server_thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.server_thread.start()
        self.addCleanup(self._stop_server)
        host, port = self.server.server_address[:2]
        self.assertEqual(host, "127.0.0.1")          # 验收：网络仅回环
        self.base_url = "http://127.0.0.1:%d" % port

    def _stop_server(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.server_thread.join(timeout=5.0)

    # ---- HTTP 工具 --------------------------------------------------------
    def http(self, method: str, path: str, body=None, headers=None, raw_body=None):
        """发一次真 HTTP 请求，返回 (状态码, JSON 载荷)；4xx/5xx 不抛异常。"""
        data = None
        request_headers = dict(headers or {})
        if raw_body is not None:
            data = raw_body
            request_headers.setdefault("Content-Type", "application/json")
        elif body is not None:
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
            request_headers.setdefault("Content-Type", "application/json")
        request = urllib.request.Request(
            self.base_url + path, data=data, headers=request_headers, method=method
        )
        try:
            with urllib.request.urlopen(request, timeout=10.0) as response:
                return self._decode(response.status, response)
        except urllib.error.HTTPError as exc:
            return self._decode(exc.code, exc)

    def _decode(self, status: int, response):
        raw = response.read()
        content_type = response.headers.get("Content-Type", "")
        self.assertIn("application/json", content_type, "响应必须是 JSON：%r" % content_type)
        return status, json.loads(raw.decode("utf-8") or "{}")

    # ---- 断言工具 ----------------------------------------------------------
    def expect_error(self, result, code: str, status: int) -> dict:
        """断言嵌套错误体：HTTP 状态 + {"error": {code, message, request_id}}。"""
        actual_status, payload = result
        self.assertEqual(
            actual_status, status, "期望 HTTP %d，实际 %d：%s" % (status, actual_status, payload)
        )
        self.assertEqual(set(payload), {"error"})
        error = payload["error"]
        self.assertEqual(set(error), {"code", "message", "request_id"})
        self.assertEqual(error["code"], code)
        self.assertTrue(isinstance(error["message"], str) and error["message"])
        self.assertTrue(isinstance(error["request_id"], str) and error["request_id"])
        return payload

    def expect_flat_not_found(self, result, path: str) -> None:
        """断言开关关闭时的「未部署」404：扁平体 {ok, error, detail}。"""
        status, payload = result
        self.assertEqual(status, 404)
        self.assertEqual(set(payload), {"ok", "error", "detail"})
        self.assertIs(payload["ok"], False)
        self.assertEqual(payload["error"], "not_found")
        self.assertEqual(payload["detail"], path)

    # ---- 业务工具 ----------------------------------------------------------
    def make_consent(self, purpose: str = "capture") -> str:
        assert self.services is not None
        return self.services.consent.record(purpose, True).consent_id

    def submit(self, request_id: str, *, consent_id: str, style: str = STYLE_NEON,
               optional_fields: object = "omit", headers=None, portrait_mode: object = "omit"):
        body = {"request_id": request_id, "style_id": style, "capture_consent_id": consent_id}
        if optional_fields != "omit":
            body["optional_fields"] = optional_fields
        if portrait_mode != "omit":
            body["portrait_mode"] = portrait_mode
        return self.http("POST", "/api/queue", body, headers=headers)

    def submit_created(self, request_id: str, *, consent_id: str, **kwargs):
        """提交并断言 201 契约骨架：{entry, session}，session 为 32 字符。"""
        status, payload = self.submit(request_id, consent_id=consent_id, **kwargs)
        self.assertEqual(status, 201, "期望 201：%s" % payload)
        self.assertEqual(set(payload), {"entry", "session"})
        session = payload["session"]
        self.assertEqual(len(session), 32)
        self.assertRegex(session, SESSION_RE)
        return payload["entry"], session


class QueueSubmitHttpTest(AutoFlowHttpTestCase):
    """POST /api/queue：空个人字段、非法风格、同意、幂等、容量。"""

    def test_submit_get_and_job_preserve_mode_for_each_enabled_style(self):
        for style in (STYLE_NEON, STYLE_CHIBI):
            for mode in ("standard", "chibi"):
                with self.subTest(style=style, mode=mode):
                    entry, session = self.submit_created(
                        "req-" + style + "-" + mode, consent_id=self.make_consent(),
                        style=style, portrait_mode=mode,
                    )
                    self.assertEqual(entry["portrait_mode"], mode)
                    status, payload = self.http(
                        "GET", "/api/queue/" + entry["queue_id"],
                        headers={"X-Queue-Session": session},
                    )
                    self.assertEqual(status, 200)
                    self.assertEqual(payload["entry"], entry)
                    status, payload = self.http(
                        "GET", "/api/job/" + entry["queue_id"],
                        headers={"X-Queue-Session": session},
                    )
                    self.assertEqual(status, 200)
                    self.assertEqual(payload["job"]["portrait_mode"], mode)
        persisted = json.loads((self.tmp / "auto_flow" / "queue.json").read_text(encoding="utf-8"))
        self.assertEqual([item["portrait_mode"] for item in persisted["entries"]],
                         ["standard", "chibi", "standard", "chibi"])

    def test_mode_replay_defaults_and_switch_conflicts(self):
        consent = self.make_consent()
        entry, session = self.submit_created("req-mode", consent_id=consent)
        status, payload = self.submit("req-mode", consent_id=consent, portrait_mode="standard")
        self.assertEqual(status, 200)
        self.assertEqual(payload, {"entry": entry, "session": session})
        self.expect_error(
            self.submit("req-mode", consent_id=consent, portrait_mode="chibi"),
            "IDEMPOTENCY_CONFLICT", 409,
        )
        chibi_consent = self.make_consent()
        chibi_entry, _ = self.submit_created("req-mode-chibi", consent_id=chibi_consent,
                                             portrait_mode="chibi")
        status, payload = self.submit("req-mode-chibi", consent_id=chibi_consent,
                                      portrait_mode="chibi")
        self.assertEqual(status, 200)
        self.assertEqual(payload["entry"], chibi_entry)
        self.expect_error(
            self.submit("req-mode-chibi", consent_id=chibi_consent),
            "IDEMPOTENCY_CONFLICT", 409,
        )
        self.assertEqual(self.services.queue.snapshot()["total"], 2)
        self.assertEqual(self.services.queue.get(entry["queue_id"]).portrait_mode, "standard")

    def test_legacy_persisted_standard_request_replays_with_old_fingerprint(self):
        consent = self.make_consent()
        entry, _ = self.submit_created("req-legacy-mode", consent_id=consent,
                                       optional_fields={"name": "测试"})
        store = QueueStore(self.tmp)
        state = store.load()
        state["entries"][0].pop("portrait_mode")
        old_payload = '{"optional_fields":{"name":"测试"},"style_id":"neon"}'
        state["idempotency"]["req-legacy-mode"]["fingerprint"] = (
            "sha256:" + hashlib.sha256(old_payload.encode("utf-8")).hexdigest()
        )
        store.save(state)
        self.install_services()
        for mode in ("omit", "standard"):
            with self.subTest(mode=mode):
                status, payload = self.submit("req-legacy-mode", consent_id=consent,
                                              optional_fields={"name": "测试"}, portrait_mode=mode)
                self.assertEqual(status, 200)
                self.assertEqual(payload["entry"], entry)
                status, fetched = self.http(
                    "GET", "/api/queue/" + entry["queue_id"],
                    headers={"X-Queue-Session": payload["session"]},
                )
                self.assertEqual(status, 200)
                self.assertEqual(fetched["entry"]["portrait_mode"], "standard")
        self.expect_error(
            self.submit("req-legacy-mode", consent_id=consent,
                        optional_fields={"name": "测试"}, portrait_mode="chibi"),
            "IDEMPOTENCY_CONFLICT", 409,
        )
        self.assertEqual(self.services.queue.snapshot()["total"], 1)

    def test_invalid_mode_returns_400_before_style_and_consent_without_writes(self):
        for invalid in (None, "", "CHIBI", " chibi ", "adult", True, 1, [], {}):
            with self.subTest(mode=invalid):
                self.expect_error(
                    self.submit("req-bad-mode", consent_id="missing", style=STYLE_UNKNOWN,
                                portrait_mode=invalid),
                    "INVALID_REQUEST", 400,
                )
        self.assertEqual(self.services.queue.snapshot()["total"], 0)
        self.assertFalse((self.tmp / "auto_flow" / "queue.json").exists())

    def test_submit_empty_personal_fields_201(self):
        entry, session = self.submit_created("req-empty", consent_id=self.make_consent())
        self.assertEqual(
            set(entry),
            {"queue_id", "request_id", "status", "style_id", "created_at",
             "updated_at", "display_number", "portrait_mode", "ai_watermark"},
        )
        self.assertEqual(entry["request_id"], "req-empty")
        self.assertEqual(entry["style_id"], STYLE_NEON)
        self.assertEqual(entry["portrait_mode"], "standard")
        self.assertEqual(entry["status"], "queued")
        self.assertTrue(entry["queue_id"])
        self.assertNotEqual(entry["queue_id"], session)
        self.assertEqual(entry["display_number"], "SM-2026-0001")
        for key in ("optional_fields", "name", "handle", "phone"):
            self.assertNotIn(key, entry)
        # 省略 optional_fields 与显式 {} 等价：都能 201
        entry2, _ = self.submit_created(
            "req-empty-2", consent_id=self.make_consent(), optional_fields={}
        )
        self.assertEqual(set(entry2), set(entry))
        self.assertEqual(entry2["status"], "queued")
        # 队列与同意都只写在临时数据根内
        self.assertTrue((self.tmp / "auto_flow" / "queue.json").is_file())
        self.assertTrue((self.tmp / "auto_flow" / "consent.json").is_file())

    def test_operator_can_list_and_cancel_queued_entry(self):
        entry, _ = self.submit_created("req-operator-cancel", consent_id=self.make_consent())

        status, payload = self.http("GET", "/api/operator/queue")
        self.assertEqual(status, 200)
        self.assertTrue(payload["unlimited"])
        self.assertEqual(payload["max_active"], None)
        self.assertEqual([item["queue_id"] for item in payload["entries"]], [entry["queue_id"]])
        self.assertNotIn("request_id", payload["entries"][0])

        status, payload = self.http(
            "POST", "/api/operator/queue/%s/cancel" % entry["queue_id"], {}
        )
        self.assertEqual(status, 200)
        self.assertEqual(payload["entry"]["status"], "cancelled")
        self.assertEqual(self.services.queue.snapshot()["active"], 0)

    def test_submit_invalid_style_404_before_consent(self):
        consent = self.make_consent()
        # 未知风格 + 无效同意：风格先判 -> 404（不是 403）
        self.expect_error(
            self.submit("req-bad-style", consent_id="no-such-consent", style=STYLE_UNKNOWN),
            "STYLE_NOT_FOUND", 404,
        )
        # 目录里存在但 enabled=false 的风格同样 404
        self.expect_error(
            self.submit("req-off-style", consent_id=consent, style=STYLE_DISABLED),
            "STYLE_NOT_FOUND", 404,
        )
        # 被拒的提交不产生条目、不占容量
        snapshot = self.services.queue.snapshot()
        self.assertEqual(snapshot["total"], 0)
        self.assertEqual(snapshot["active"], 0)

    def test_submit_structure_errors_400(self):
        consent = self.make_consent()
        # 请求体不是 JSON 对象
        self.expect_error(self.http("POST", "/api/queue", raw_body=b"[]"), "INVALID_REQUEST", 400)
        # 空请求体
        self.expect_error(self.http("POST", "/api/queue"), "INVALID_REQUEST", 400)
        # 缺 request_id
        self.expect_error(
            self.http("POST", "/api/queue", {"style_id": STYLE_NEON, "capture_consent_id": consent}),
            "INVALID_REQUEST", 400,
        )
        # 缺 capture_consent_id：结构错误先于同意检查 -> 400（不是 403）
        self.expect_error(
            self.http("POST", "/api/queue", {"request_id": "req-nc", "style_id": STYLE_NEON}),
            "INVALID_REQUEST", 400,
        )
        # 未知字段
        self.expect_error(
            self.http("POST", "/api/queue", {
                "request_id": "req-unknown", "style_id": STYLE_NEON,
                "capture_consent_id": consent, "extra": "x",
            }),
            "INVALID_REQUEST", 400,
        )
        self.assertEqual(self.services.queue.snapshot()["total"], 0)

    def test_submit_consent_missing_invalid_revoked_expired_403(self):
        # 未知 consent_id（记录不存在）
        self.expect_error(
            self.submit("req-c-unknown", consent_id="no-such-consent"),
            "CONSENT_REQUIRED", 403,
        )
        # 已撤回
        revoked = self.make_consent()
        self.assertTrue(self.services.consent.revoke(revoked))
        self.expect_error(self.submit("req-c-revoked", consent_id=revoked), "CONSENT_REQUIRED", 403)
        # 用途不符：外置处理同意不能替代拍摄同意
        external = self.services.consent.record("external_processing", True).consent_id
        self.expect_error(self.submit("req-c-external", consent_id=external), "CONSENT_REQUIRED", 403)
        # 已过期（假时钟推进超过 TTL）
        expiring = self.make_consent()
        self.clock.advance(self.consent_ttl_seconds + 1)
        self.expect_error(self.submit("req-c-expired", consent_id=expiring), "CONSENT_REQUIRED", 403)
        # 全部拒绝都不产生条目、不占容量
        snapshot = self.services.queue.snapshot()
        self.assertEqual(snapshot["total"], 0)
        self.assertEqual(snapshot["active"], 0)

    def test_submit_idempotency_replay_200_and_conflict_409(self):
        consent = self.make_consent()
        entry, session = self.submit_created("req-dup", consent_id=consent)
        # 同 request_id + 同载荷 -> 200，原 entry / 原 session
        status, payload = self.submit("req-dup", consent_id=consent)
        self.assertEqual(status, 200)
        self.assertEqual(set(payload), {"entry", "session"})
        self.assertEqual(payload["entry"], entry)
        self.assertEqual(payload["session"], session)
        self.assertEqual(len(payload["session"]), 32)
        # 同 request_id + 异载荷 -> 409
        self.expect_error(
            self.submit("req-dup", consent_id=consent, style=STYLE_CHIBI),
            "IDEMPOTENCY_CONFLICT", 409,
        )
        # 冲突不产生第二条记录，原条目原样保留
        self.assertEqual(self.services.queue.snapshot()["total"], 1)
        self.assertEqual(self.services.queue.get(entry["queue_id"]).status.value, "queued")

    def test_submit_cross_booking_reuse_403(self):
        consent = self.make_consent()
        entry, _ = self.submit_created("req-a", consent_id=consent)
        # 同一个拍摄同意去提交另一份预约 -> 403，不创建条目
        self.expect_error(self.submit("req-reuse", consent_id=consent), "CONSENT_REQUIRED", 403)
        self.assertEqual(self.services.queue.snapshot()["total"], 1)
        # 原绑定与条目不受影响；同 request_id 重放仍 200
        self.assertEqual(self.services.consent.binding_of(consent), entry["queue_id"])
        status, payload = self.submit("req-a", consent_id=consent)
        self.assertEqual(status, 200)
        self.assertEqual(payload["entry"]["queue_id"], entry["queue_id"])

    def test_submit_capacity_full_429_then_release(self):
        self.install_services(max_active=2)
        c1, c2, c3 = (self.make_consent() for _ in range(3))
        first, _ = self.submit_created("req-1", consent_id=c1)
        self.submit_created("req-2", consent_id=c2)
        # 容量已满 -> 429
        self.expect_error(self.submit("req-3", consent_id=c3), "QUEUE_FULL", 429)
        snapshot = self.services.queue.snapshot()
        self.assertEqual(snapshot["capacity"], {"max_active": 2, "active": 2})
        self.assertEqual(snapshot["total"], 2)
        # 429 不消耗同意：释放容量后可重新提交
        self.services.queue.mark_cancelled(first["queue_id"], reason="集成测试：释放容量")
        entry3, _ = self.submit_created("req-3", consent_id=c3)
        self.assertEqual(entry3["request_id"], "req-3")


class QueueAccessHttpTest(AutoFlowHttpTestCase):
    """GET /api/queue/<id> 与 POST /start：会话门禁、状态与相机门。"""

    def _start_url(self, queue_id: str) -> str:
        return "/api/queue/%s/start" % queue_id

    def test_get_requires_own_session_200_and_404(self):
        entry_a, session_a = self.submit_created("req-a", consent_id=self.make_consent())
        _, session_b = self.submit_created("req-b", consent_id=self.make_consent())
        queue_id = entry_a["queue_id"]
        # 正确会话 -> 200 {entry}
        status, payload = self.http(
            "GET", "/api/queue/%s" % queue_id, headers={"X-Queue-Session": session_a}
        )
        self.assertEqual(status, 200)
        self.assertEqual(set(payload), {"entry"})
        self.assertEqual(payload["entry"]["queue_id"], queue_id)
        self.assertEqual(payload["entry"]["status"], "queued")
        # 缺会话 / 错会话 / 拿别人的会话 -> 一律 404
        missing = self.expect_error(
            self.http("GET", "/api/queue/%s" % queue_id), "QUEUE_NOT_FOUND", 404
        )
        self.expect_error(
            self.http("GET", "/api/queue/%s" % queue_id, headers={"X-Queue-Session": "x" * 32}),
            "QUEUE_NOT_FOUND", 404,
        )
        other = self.expect_error(
            self.http("GET", "/api/queue/%s" % queue_id, headers={"X-Queue-Session": session_b}),
            "QUEUE_NOT_FOUND", 404,
        )
        # 条目不存在（会话有效）-> 404
        ghost = self.services.sessions.issue("ghost-queue-id")
        self.expect_error(
            self.http("GET", "/api/queue/ghost-queue-id", headers={"X-Queue-Session": ghost}),
            "QUEUE_NOT_FOUND", 404,
        )
        # 隐藏存在性：「别人的条目」与「缺会话」响应同形，不泄露条目是否存在
        self.assertEqual(missing["error"]["code"], other["error"]["code"])
        self.assertEqual(missing["error"]["message"], other["error"]["message"])

    def test_start_session_gate_404_before_other_checks(self):
        consent = self.make_consent()
        entry_a, session_a = self.submit_created("req-a", consent_id=consent)
        _, session_b = self.submit_created("req-b", consent_id=self.make_consent())
        url = self._start_url(entry_a["queue_id"])
        # 无会话 -> 404（body 里有有效同意也一样）
        self.expect_error(
            self.http("POST", url, {"capture_consent_id": consent}), "QUEUE_NOT_FOUND", 404
        )
        # 拿别人的会话 -> 404
        self.expect_error(
            self.http("POST", url, {"capture_consent_id": consent},
                      headers={"X-Queue-Session": session_b}),
            "QUEUE_NOT_FOUND", 404,
        )
        # 条目不存在（会话有效）-> 404
        ghost = self.services.sessions.issue("ghost-queue-id")
        self.expect_error(
            self.http("POST", self._start_url("ghost-queue-id"), {"capture_consent_id": consent},
                      headers={"X-Queue-Session": ghost}),
            "QUEUE_NOT_FOUND", 404,
        )
        # 404 优先于 400：无会话 + 空 body -> 404（不是 INVALID_REQUEST）
        self.expect_error(self.http("POST", url, {}), "QUEUE_NOT_FOUND", 404)

    def test_start_not_called_409_and_consent_gate_403(self):
        consent = self.make_consent()
        entry, session = self.submit_created("req-a", consent_id=consent)
        queue_id = entry["queue_id"]
        url = self._start_url(queue_id)
        headers = {"X-Queue-Session": session}
        # 未到号 -> 409 NOT_CALLED，条目保持 queued
        self.expect_error(
            self.http("POST", url, {"capture_consent_id": consent}, headers=headers),
            "NOT_CALLED", 409,
        )
        self.assertEqual(self.services.queue.get(queue_id).status.value, "queued")
        # 400 优先于 403：body 不是 JSON 对象 / 缺 capture_consent_id
        self.expect_error(self.http("POST", url, raw_body=b"[]", headers=headers), "INVALID_REQUEST", 400)
        self.expect_error(self.http("POST", url, {}, headers=headers), "INVALID_REQUEST", 400)
        # 403：同意无效
        self.expect_error(
            self.http("POST", url, {"capture_consent_id": "no-such-consent"}, headers=headers),
            "CONSENT_REQUIRED", 403,
        )
        # 403：同意绑到别的预约（B 的同意不能用来开始 A）
        consent_b = self.make_consent()
        self.submit_created("req-b", consent_id=consent_b)
        self.expect_error(
            self.http("POST", url, {"capture_consent_id": consent_b}, headers=headers),
            "CONSENT_REQUIRED", 403,
        )
        # 403：外置处理同意不能替代拍摄同意
        external = self.services.consent.record("external_processing", True).consent_id
        self.expect_error(
            self.http("POST", url, {"capture_consent_id": external}, headers=headers),
            "CONSENT_REQUIRED", 403,
        )
        # 以上都不推进状态（相机探针仍为 None，从未被查询）
        self.assertEqual(self.services.queue.get(queue_id).status.value, "queued")

    def test_start_camera_503_then_202_then_repeat_409(self):
        consent = self.make_consent()
        entry, session = self.submit_created("req-a", consent_id=consent)
        queue_id = entry["queue_id"]
        url = self._start_url(queue_id)
        headers = {"X-Queue-Session": session}
        called = self.services.queue.call_next()
        self.assertIsNotNone(called)
        self.assertEqual(called.queue_id, queue_id)
        self.assertEqual(called.status.value, "called")
        self.assertTrue(called.start_deadline)
        # 探针 None（T-2 保守默认）-> 503；条目保持 called，可重试
        self.expect_error(
            self.http("POST", url, {"capture_consent_id": consent}, headers=headers),
            "CAMERA_UNAVAILABLE", 503,
        )
        self.assertEqual(self.services.queue.get(queue_id).status.value, "called")
        # 探针返回 False -> 503
        self.services.camera_probe = lambda: False
        self.expect_error(
            self.http("POST", url, {"capture_consent_id": consent}, headers=headers),
            "CAMERA_UNAVAILABLE", 503,
        )
        # 注入可用探针（T-14 FakeCamera）-> 202 {entry, gesture_enabled}
        fake_camera = FakeCamera(self.tmp / "fake-camera", self.clock)
        fake_camera.set_health("ok")
        self.services.camera_probe = fake_camera
        status, payload = self.http("POST", url, {"capture_consent_id": consent}, headers=headers)
        self.assertEqual(status, 202)
        self.assertEqual(set(payload), {"entry", "gesture_enabled"})
        self.assertIs(payload["gesture_enabled"], False)
        started = payload["entry"]
        self.assertEqual(started["status"], "ready")
        self.assertEqual(started["queue_id"], queue_id)
        self.assertTrue(started["started_at"])
        self.assertEqual(started["consent_id"], consent)
        # 重复开始 -> 409 NOT_CALLED（READY 非 CALLED）
        self.expect_error(
            self.http("POST", url, {"capture_consent_id": consent}, headers=headers),
            "NOT_CALLED", 409,
        )
        # 查询也应反映 ready
        status, payload = self.http("GET", "/api/queue/%s" % queue_id, headers=headers)
        self.assertEqual(status, 200)
        self.assertEqual(payload["entry"]["status"], "ready")

    def test_start_expired_409_before_camera_probe(self):
        consent = self.make_consent()
        entry, session = self.submit_created("req-a", consent_id=consent)
        queue_id = entry["queue_id"]
        headers = {"X-Queue-Session": session}
        called = self.services.queue.call_next()
        self.assertIsNotNone(called)
        self.assertTrue(called.start_deadline)
        # 假时钟推进超过开始期限；相机探针仍为 None（顺序错误会得到 503）
        self.clock.advance(self.start_timeout_seconds + 1)
        self.expect_error(
            self.http("POST", self._start_url(queue_id), {"capture_consent_id": consent},
                      headers=headers),
            "QUEUE_EXPIRED", 409,
        )
        stored = self.services.queue.get(queue_id)
        self.assertEqual(stored.status.value, "expired")
        self.assertTrue(stored.expired_at)
        # 查询反映过期；再次开始仍 409 QUEUE_EXPIRED
        status, payload = self.http("GET", "/api/queue/%s" % queue_id, headers=headers)
        self.assertEqual(status, 200)
        self.assertEqual(payload["entry"]["status"], "expired")
        self.expect_error(
            self.http("POST", self._start_url(queue_id), {"capture_consent_id": consent},
                      headers=headers),
            "QUEUE_EXPIRED", 409,
        )

    def test_start_gesture_flag_mirrors_config(self):
        self.install_services(gesture_enabled=True)
        consent = self.make_consent()
        entry, session = self.submit_created("req-g", consent_id=consent)
        self.services.queue.call_next()
        self.services.camera_probe = lambda: True
        status, payload = self.http(
            "POST", self._start_url(entry["queue_id"]),
            {"capture_consent_id": consent},
            headers={"X-Queue-Session": session},
        )
        self.assertEqual(status, 202)
        self.assertEqual(set(payload), {"entry", "gesture_enabled"})
        self.assertIs(payload["gesture_enabled"], True)
        self.assertEqual(payload["entry"]["status"], "ready")


class PublicStatusHttpTest(AutoFlowHttpTestCase):
    """公网报名只读取两个功能开关，不触碰或泄露完整现场状态。"""

    def _set_aigc(self, enabled: bool) -> None:
        previous_enabled = os.environ.get("AIGC_ENABLED")
        previous_backend = os.environ.get("AIGC_BACKEND")

        def _restore() -> None:
            if previous_enabled is None:
                os.environ.pop("AIGC_ENABLED", None)
            else:
                os.environ["AIGC_ENABLED"] = previous_enabled
            if previous_backend is None:
                os.environ.pop("AIGC_BACKEND", None)
            else:
                os.environ["AIGC_BACKEND"] = previous_backend

        self.addCleanup(_restore)
        os.environ["AIGC_ENABLED"] = "1" if enabled else "0"
        os.environ["AIGC_BACKEND"] = "public_api" if enabled else "disabled"

    def test_public_status_is_minimal_and_does_not_call_full_state(self):
        self._set_aigc(True)
        self.booth.state = lambda: (_ for _ in ()).throw(AssertionError("不应读取完整状态"))
        status, payload = self.http("GET", "/api/public/status")
        self.assertEqual(status, 200)
        self.assertEqual(payload, {
            "ok": True,
            "queue_enabled": True,
            "aigc_enabled": True,
        })

    def test_public_status_reports_disabled_features(self):
        self._set_aigc(False)
        self.booth.auto_flow = None
        status, payload = self.http("GET", "/api/public/status")
        self.assertEqual(status, 200)
        self.assertEqual(payload, {
            "ok": True,
            "queue_enabled": False,
            "aigc_enabled": False,
        })


class QueueSwitchOffHttpTest(AutoFlowHttpTestCase):
    """开关显式关闭（BOOTH_QUEUE_ENABLED=false）：三条路由 404，零副作用。"""

    with_services = False
    queue_switch = "false"

    def test_routes_404_flat_body_and_no_auto_flow_dir(self):
        self.assertIsNone(self.booth.auto_flow_services())
        self.expect_flat_not_found(
            self.http("POST", "/api/queue",
                      {"request_id": "r", "style_id": STYLE_NEON, "capture_consent_id": "c"}),
            "/api/queue",
        )
        self.expect_flat_not_found(self.http("GET", "/api/queue/whatever"), "/api/queue/whatever")
        self.expect_flat_not_found(
            self.http("POST", "/api/queue/whatever/start", {"capture_consent_id": "c"}),
            "/api/queue/whatever/start",
        )
        # 三条路由访问后仍不产生 auto_flow 数据目录/文件
        self.assertFalse((self.tmp / "auto_flow").exists())
        self.assertEqual(list(self.tmp.rglob("queue.json")), [])
        self.assertEqual(list(self.tmp.rglob("consent.json")), [])


class QueueSwitchUnsetHttpTest(QueueSwitchOffHttpTest):
    """开关未设置（默认关闭）：行为与显式 false 一致。"""

    queue_switch = None


class ExistingCaptureConsentHttpTest(AutoFlowHttpTestCase):
    """现有 POST /api/capture：未同意一律 403，且应用层相机零调用。"""

    def test_capture_without_consent_403_camera_zero_calls(self):
        counter = CountingCamera()
        self.booth.camera = counter
        for label, headers in (("无令牌", {}), ("无效令牌", {"X-Booth-Session": "bogus-token"})):
            with self.subTest(label=label):
                status, payload = self.http("POST", "/api/capture", {"style": "natural"}, headers=headers)
                self.assertEqual(counter.calls, 0, "未同意路径触碰了相机（%s）" % label)
                self.assertEqual(status, 403)
                self.assertEqual(set(payload), {"ok", "error", "detail"})
                self.assertIs(payload["ok"], False)
                self.assertEqual(payload["error"], "consent_required")
                self.assertTrue(payload["detail"])
                self.assertNotIn("job", payload)
        # 辅助断言：没有作业、没有照片记录（与相机零调用一致）
        self.assertIsNone(self.booth.jobs.latest())
        self.assertEqual(self.booth.store.all(), [])
        self.assertFalse((self.tmp / "records.jsonl").exists())
