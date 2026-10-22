# -*- coding: utf-8 -*-
"""HTTP 队列处理器纯逻辑层（T-2）：POST /api/queue、GET /api/queue/<queue_id>、
POST /api/queue/<queue_id>/start。

规格依据：`docs/spec/自动流程规格-v1.md` 的 External HTTP and page contracts
（三条路由的成功/错误表）、Error handling、Security（队列访问私有性）与
Configuration（BOOTH_QUEUE_ENABLED）；HTTP 接线在 `app/booth_server.py`。

边界与约定（与 consent_handlers.py 同一套写法）：
- 不 import HTTP server、不读请求对象、不写响应流：只接收已解析的 JSON 载荷、
  路径参数与会话值，返回 ``(http_status, payload)`` 或抛 `errors.ApiError`；
- 成功响应：``{"entry": QueueEntry}``（提交另附 ``session``；开始另附
  ``gesture_enabled``）；错误统一由路由层用 ``ApiError.payload(request_id)``
  输出为 ``{"error": {"code", "message", "request_id"}}``；
- 队列私有性（硬要求，T-2 自主决策）：POST 首次提交与幂等重放的成功响应都附发
  随机 ``session``（24 字节 URL-safe，32 字符——与取图令牌 22 位、同意令牌
  32 位十六进制形状不同，避免混用），服务端在 `QueueSessionRegistry` 中绑定
  session↔queue_id。GET/start 必须带 ``X-Queue-Session`` 头；缺失或不匹配一律
  **404 QUEUE_NOT_FOUND**（隐藏存在性：让仅知道 queue_id 的人无法区分"不存在"
  与"不属于我"，也无法从状态码推断任何条目信息；这对"不得仅凭公开 queue_id
  操作其他预约"是最保守的语义）。session 只存内存：服务重启后失效（与同意令牌
  同契约），参与者需重新提交；
- 提交顺序（对 T-12「队列接入顺序」的落地 + 一处收紧）：结构解析(400) ->
  风格校验(404) -> 同意预检(403，**不产生任何队列状态**) -> QueueService 幂等
  提交(409/429) -> 同意绑定(403，绑定时竞态失败则回滚已建条目) -> 附发 session。
  预检不只查"同意是否有效"，还会查它是否已绑到别的预约（借 queue.get(binding)
  与本请求的 request_id 对比）：能提前确定的"绑走"直接 403，不创建条目、不占
  容量、不留取消僵尸；`authorize_queue_submission` 仍是权威的绑定入口，负责
  处理预检之后才发生的极小竞态（此时若有新建条目则 mark_cancelled 回滚）；
- 开始顺序：会话(404) -> 条目存在(404) -> 同意(400/403) -> 期限判断(仅用于
  决定"是否先查相机") -> 相机新鲜度探针(503) -> QueueService.start(202/409)；
  未到号（queued）等状态不查相机，交由 QueueService.start 统一抛 409 NOT_CALLED /
  QUEUE_EXPIRED——保证"未到号开始 409"不被 503 抢先；
- 相机探针：默认 **None = 保守"不可用"**（T-2 不接真实相机；接线决策见
  booth_server）。测试可注入：可调用对象或带 ``health()`` 的对象，返回值须是
  ``models.ComponentHealth``、带 ``status`` 的 Mapping、状态字符串或布尔；
  只有 ``status == "ok"`` 才算可用（degraded 也按不可用，保守方向）；
- 功能开关：`queue_feature_enabled()` 与 T-11 的布尔词表一致；开关关闭时三条
  路由的行为由 booth_server 决定（404 not_found，"未部署"语义），本模块不参与。

线程模型：处理器自身无状态；`QueueSessionRegistry` 内锁；QueueService /
ConsentService 各自线程安全。全部逻辑可被 T-3 的 unittest 直接调用（不经过
socket），也可经真实 Handler 端到端调用。

T-16 扩展（取图 / 作业查询 / 打印 / 风格目录 / 报名二维码；全部只增，不改上面
已有处理器的行为）：
- `handle_job_get`：新流程作业查询（旧 JobBoard 的查询由路由层先做、逐字保留）；
  响应含 fallback / error_code / pickup_ready；**含私人取图入口（photo_token 与
  取图链接）时必须 X-Queue-Session 授权**，未授权只回公开字段；
- `handle_external_processing_consent`：记录 `{granted}` 意图（`bind_job` 绑定
  作业），默认禁用外置处理端口——只记录、**绝不调用网关**（T-8 替换装配即生效，
  不需要再次编辑路由）；
- `handle_photo_asset` / `handle_photo_delete`：新流程取图与删除；旧数据由路由层
  先走 legacy `Store`（行为逐字不变），令牌旧链路不认识才落到这里；
- `handle_print_submit` / `handle_print_get`：默认 503 PRINT_UNAVAILABLE /
  404 PRINT_JOB_NOT_FOUND；幂等键接口已预置，T-9 装配打印端口即可；
- `public_style_catalog` / `handle_styles`：只读风格契约（只暴露展示字段）；
- `join_qr_png`：报名二维码（编码 `<public_base>/join`），模块级内存缓存，
  `render/qr` 不可用时抛 `QrUnavailableError`（路由层转 503）；
- `BoothCameraAdapter`：现有 `app/camera.py` 的**懒加载**端口适配器（provider
  注入，import 期不触达相机模块，Windows/无相机环境安全）。
"""

from __future__ import annotations

import importlib
import json
import os
import secrets
import threading
import time
import warnings
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.auto_flow import errors
from app.auto_flow.config import DEFAULT_CATALOG_PATH, ConfigError
from app.auto_flow.consent_handlers import authorize_queue_submission, verify_queue_start
from app.auto_flow.consent_service import ConsentService
from app.auto_flow.health_service import camera_freshness_health
from app.auto_flow.models import (
    ComponentHealth,
    QueueEntry,
    QueueStatus,
    QueueSubmission,
    parse_utc_iso,
    to_utc_iso,
)
from app.auto_flow.ports import CapturedFrame
from app.auto_flow.queue_service import QueueService
from app.auto_flow.recovery_service import _read_document, _valid_token, _write_document

__all__ = [
    "TRUE_VALUES",
    "FALSE_VALUES",
    "AutoFlowServices",
    "BoothCameraAdapter",
    "QueueSessionRegistry",
    "QrUnavailableError",
    "queue_feature_enabled",
    "enabled_style_ids",
    "public_style_catalog",
    "evaluate_camera_probe",
    "handle_queue_submit",
    "handle_queue_get",
    "handle_queue_start",
    "handle_job_get",
    "handle_external_processing_consent",
    "handle_photo_asset",
    "handle_photo_delete",
    "handle_print_submit",
    "handle_print_get",
    "handle_styles",
    "public_join_url",
    "join_qr_png",
]

_UTC = timezone.utc

# 与 config._parse_bool 同一词表（T-11）；单独列出是为了在"开关关闭"时
# 不依赖 config 模块也能判定，不需要额外语义。
TRUE_VALUES = frozenset({"true", "1", "yes"})
FALSE_VALUES = frozenset({"false", "0", "no"})
_BOOL_HINT = "只接受 true/false/1/0/yes/no（大小写不敏感）"


def queue_feature_enabled(env: Mapping[str, str] | None = None) -> bool:
    """`BOOTH_QUEUE_ENABLED` 功能开关（默认 false）。

    - 未设置 -> False；true/1/yes（大小写不敏感、去两端空白）-> True；
      false/0/no -> False；
    - 其它值（含空串）-> ``ConfigError``：不静默回退，与 T-11 的失败方向一致。
    """
    source = os.environ if env is None else env
    raw = source.get("BOOTH_QUEUE_ENABLED")
    if raw is None:
        return False
    value = str(raw).strip().lower()
    if value in TRUE_VALUES:
        return True
    if value in FALSE_VALUES:
        return False
    raise ConfigError("BOOTH_QUEUE_ENABLED", f"{_BOOL_HINT}，实际值为 {raw!r}")


class QueueSessionRegistry:
    """会话注册表：session ↔ queue_id（T-2，队列私有性的执行点）。

    - ``issue(queue_id)``：返回该条目的会话令牌；同一进程生命周期内同一条目固定
      返回同一令牌（幂等重放 = 原会话，不轮换），保证多次重放拿到的入口一致；
    - ``check(session, queue_id)``：会话必须非空字符串且正好绑定该 queue_id；
      处理器把 False 一律映射为 404 QUEUE_NOT_FOUND（隐藏存在性）；
    - ``forget(queue_id)``：移除绑定（仅供测试/清理用，正常流程不调用）；
    - 默认只存内存；指定 path 后原子保存 token→queue_id，重启仍有效。
      坏文件抛错，写入失败不提交内存变更；同一路径需要单一写者；
    - 线程安全：RLock 保护双向索引，两个方向由同一把锁维护，保持一致。

    令牌形状：``secrets.token_urlsafe(24)`` -> 32 个 URL-safe 字符 + 可能的
    padding 省略，与 22 位取图令牌、32 位十六进制同意令牌形状均不同。
    """

    TOKEN_BYTES = 24

    def __init__(self, path: Path | None = None) -> None:
        self._lock = threading.RLock()
        self._path = Path(path) if path is not None else None
        self._by_queue: dict[str, str] = {}
        self._by_token: dict[str, str] = {}
        if self._path is not None:
            document = _read_document(self._path, {})
            if not isinstance(document, dict):
                raise ValueError("会话存储必须为 JSON 对象。")
            for token, queue_id in document.items():
                if (
                    not _valid_token(token)
                    or not isinstance(queue_id, str)
                    or not queue_id.strip()
                    or queue_id != queue_id.strip()
                    or queue_id in self._by_queue
                ):
                    raise ValueError("会话存储绑定无效。")
                self._by_queue[queue_id] = token
                self._by_token[token] = queue_id

    def issue(self, queue_id: str) -> str:
        if not isinstance(queue_id, str) or not queue_id.strip():
            raise ValueError("queue_id 必须是非空字符串。")
        key = queue_id.strip()
        with self._lock:
            token = self._by_queue.get(key)
            if token is None:
                token = secrets.token_urlsafe(self.TOKEN_BYTES)
                while token in self._by_token:
                    token = secrets.token_urlsafe(self.TOKEN_BYTES)
                if self._path is not None:
                    _write_document(self._path, {**self._by_token, token: key})
                self._by_queue[key] = token
                self._by_token[token] = key
            return token

    def get_existing(self, queue_id: str) -> str | None:
        """只读查询已有控制会话；缺失时不签发、不落盘。"""
        if not isinstance(queue_id, str) or not queue_id.strip():
            return None
        with self._lock:
            return self._by_queue.get(queue_id.strip())

    def issue_volatile(self, queue_id: str) -> str:
        """持久化不可用时仅在内存补发；已有绑定保持不变。"""
        if not isinstance(queue_id, str) or not queue_id.strip():
            raise ValueError("queue_id 必须是非空字符串。")
        key = queue_id.strip()
        with self._lock:
            token = self._by_queue.get(key)
            if token is None:
                token = secrets.token_urlsafe(self.TOKEN_BYTES)
                while token in self._by_token:
                    token = secrets.token_urlsafe(self.TOKEN_BYTES)
                self._by_queue[key] = token
                self._by_token[token] = key
            return token

    def check(self, session: Any, queue_id: str) -> bool:
        """会话是否正好绑定该 queue_id；缺失/非字符串/不匹配均为 False。"""
        if not isinstance(session, str) or not isinstance(queue_id, str):
            return False
        token = session.strip()
        if not token:
            return False
        with self._lock:
            return self._by_token.get(token) == queue_id

    def forget(self, queue_id: str) -> bool:
        """解除该条目的会话绑定；返回是否发生过解除。"""
        if not isinstance(queue_id, str):
            return False
        key = queue_id.strip()
        with self._lock:
            token = self._by_queue.get(key)
            if token is None:
                return False
            if self._path is not None:
                document = dict(self._by_token)
                document.pop(token)
                _write_document(self._path, document)
            self._by_queue.pop(key)
            self._by_token.pop(token, None)
            return True


def _catalog_document(source: Any = None) -> Mapping[str, Any]:
    """解析风格目录文档（enabled_style_ids 与 public_style_catalog 共用）。

    source 允许四种形态（按序判定）：
    - None：读默认目录 ``app/data/style-catalog.json``（config.DEFAULT_CATALOG_PATH）；
    - 可调用对象：调用一次，返回值当目录文档；
    - 带 ``snapshot()`` 的对象：生产接 Booth.style_catalog（StyleCatalog 实例），
      snapshot() 取当前目录，跟着操作员在 /api/operator 的编辑走；
    - Mapping：直接当目录文档（T-3/T-16 可注入内存目录）。
    目录不可读/不是合法 JSON/形状不对：抛 ValueError（诚实失败；路由层转
    500 INTERNAL_ERROR），绝不静默放行任意 style_id。
    """
    if source is None:
        path = Path(DEFAULT_CATALOG_PATH)
        try:
            document: Any = json.loads(path.read_text(encoding="utf-8"))
        except OSError as exc:
            raise ValueError(f"风格目录不可读：{path}（{exc}）") from exc
        except ValueError as exc:
            raise ValueError(f"风格目录不是合法 JSON：{path}（{exc}）") from exc
    elif callable(source):
        document = source()
    elif callable(getattr(source, "snapshot", None)):
        document = source.snapshot()
    elif isinstance(source, Mapping):
        document = source
    else:
        raise ValueError("风格目录来源必须是 Mapping、可调用对象或带 snapshot() 的对象。")
    if not isinstance(document, Mapping):
        raise ValueError(f"风格目录必须是 JSON 对象，得到 {type(document).__name__}。")
    return document


def enabled_style_ids(source: Any = None) -> frozenset[str]:
    """解析风格目录中"当前可选"的 style_id 集合（T-2 的 style_id 校验数据源）。

    目录文档形状：``{"styles": [{"id": ..., "enabled": true, ...}, ...]}``；
    只有 ``enabled`` 为真值的条目算可选风格，id 必须是非空字符串。
    目录不可读/不是合法 JSON/形状不对：抛 ValueError（诚实失败；路由层转
    500 INTERNAL_ERROR），绝不静默放行任意 style_id。解析规则见 `_catalog_document`。
    """
    document = _catalog_document(source)
    items = document.get("styles")
    if not isinstance(items, list):
        raise ValueError("风格目录缺少 styles 列表。")
    ids: set[str] = set()
    for item in items:
        if not isinstance(item, Mapping) or not item.get("enabled"):
            continue
        style_id = item.get("id")
        if isinstance(style_id, str) and style_id.strip():
            ids.add(style_id.strip())
    return frozenset(ids)


def public_style_catalog(source: Any = None) -> list[dict[str, Any]]:
    """风格目录的「公开展示」视图（T-16，``GET /api/styles`` 的数据源）。

    只暴露 T-4 前端需要的展示字段：``id / title / description / sizes /
    default_orientation / is_default``；**不外泄**提示词、模型名、修订历史与任何
    文件路径。过滤规则与 enabled_style_ids 完全一致（只列 enabled 且 id 非空），
    字段缺失时用空串/默认值补齐，前端不因缺字段报错；目录不可读抛 ValueError。
    """
    document = _catalog_document(source)
    items = document.get("styles")
    if not isinstance(items, list):
        raise ValueError("风格目录缺少 styles 列表。")
    result: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, Mapping) or not item.get("enabled"):
            continue
        style_id = item.get("id")
        if not isinstance(style_id, str) or not style_id.strip():
            continue
        sizes = [
            size.strip()
            for size in (item.get("sizes") or [])
            if isinstance(size, str) and size.strip()
        ]
        orientation = item.get("default_orientation")
        result.append(
            {
                "id": style_id.strip(),
                "title": item.get("title") if isinstance(item.get("title"), str) else "",
                "description": (
                    item.get("description") if isinstance(item.get("description"), str) else ""
                ),
                "sizes": sizes or ["1024x1536"],
                "default_orientation": (
                    orientation if orientation in ("wide", "vertical") else "vertical"
                ),
                "is_default": bool(item.get("is_default")),
            }
        )
    return result


def evaluate_camera_probe(probe: Any) -> tuple[bool, str]:
    """求值相机新鲜度探针：返回 ``(是否可用, 诊断)``，不抛异常。

    probe 形态：
    - None：T-2 的保守默认 —— 视为不可用（503 CAMERA_UNAVAILABLE），
      真实帧探针由 T-15/T-21 接入后注入；
    - 可调用对象：``probe()``；
    - 带 ``health()`` 的对象（T-14 CameraPort 形状）：``probe.health()``。
    返回值接受 models.ComponentHealth、带 ``status`` 的 Mapping、状态字符串或
    布尔；只有 ``status == "ok"`` / ``True`` 才算可用，其余（含 degraded、
    异常、不合契约的返回值）一律不可用 —— 保守方向，绝不把坏探针当成健康。
    """
    if probe is None:
        return False, "未接入相机帧探针（T-2 保守默认：视为不可用）。"
    try:
        if callable(probe):
            value = probe()
        elif callable(getattr(probe, "health", None)):
            value = probe.health()
        else:
            return False, "相机探针形态不符合契约（需要可调用对象或 health() 方法）。"
    except Exception as exc:  # noqa: BLE001 - 探针异常必须收敛为不可用
        return False, f"相机探针异常（{type(exc).__name__}: {exc}）。"

    if isinstance(value, ComponentHealth):
        status: Any = value.status
    elif isinstance(value, bool):
        return (value, "探针报告可用。" if value else "探针报告不可用。")
    elif isinstance(value, Mapping):
        status = value.get("status")
    elif isinstance(value, str):
        status = value
    else:
        return False, f"相机探针返回值不符合契约：{type(value).__name__}。"
    if status == "ok":
        return True, "相机帧新鲜。"
    return False, f"相机不可用（探针状态 {status!r}）。"


class BoothCameraAdapter:
    """现有 ``app/camera.py`` 的端口适配器（T-16；实现 T-14 的 CameraPort）。

    设计要点（Windows / 无相机环境安全）：
    - **懒加载**：构造时不 import 任何相机模块，也不触达相机对象；`provider`
      在首次使用（抓帧/健康探测）时才被调用（生产传 ``lambda: booth.camera``）；
    - ``capture_authorized(consent_id)``：调用 ``camera.snapshot()`` 拿 JPEG 字节，
      落盘到 ``output_dir``（一帧一文件：时间戳 + 随机后缀，绝不覆盖），返回
      ``CapturedFrame``；任何失败原样抛出（CaptureBridge 统一落 failed，不发令牌）；
    - ``health()`` / ``last_frame_at()``：**帧新鲜度**口径 —— 读相机 ``health()``
      的 ``last_ok_s``（距上次成功取帧的秒数）换算成 Unix 时间戳；拿不到读数、
      读数无效或超过新鲜度阈值一律 unavailable（设备枚举/打开成功不算可用，
      与 T-20 的判定口径一致）。
    """

    def __init__(
        self,
        provider: Any,
        output_dir: Path | str,
        *,
        clock: Any = None,
        freshness_timeout: float = 2.0,
    ) -> None:
        if not callable(provider):
            raise TypeError("provider 必须是无参可调用对象（返回相机对象）。")
        self._provider = provider
        self.output_dir = Path(output_dir)
        self._clock = clock or time.time
        self.freshness_timeout = float(freshness_timeout)
        if not self.freshness_timeout > 0:
            raise ValueError("freshness_timeout 必须为正数（秒）。")
        self._camera: Any = None
        self.calls = 0

    # ---- 内部 ---------------------------------------------------------- #

    def _camera_or_raise(self) -> Any:
        if self._camera is None:
            self._camera = self._provider()
        return self._camera

    def _moment(self) -> datetime:
        return datetime.fromtimestamp(float(self._clock()), tz=_UTC)

    # ---- T-14 CameraPort ------------------------------------------------ #

    def capture_authorized(self, consent_id: str) -> CapturedFrame:
        camera = self._camera_or_raise()
        jpeg = camera.snapshot()
        if not isinstance(jpeg, (bytes, bytearray)) or not jpeg:
            raise RuntimeError("相机返回了空帧，拒绝当作拍摄成功。")
        self.output_dir.mkdir(parents=True, exist_ok=True)
        moment = self._moment()
        path = self.output_dir / (
            "capture-%s-%s.jpg" % (moment.strftime("%Y%m%dT%H%M%S%f"), secrets.token_hex(4))
        )
        path.write_bytes(bytes(jpeg))
        self.calls += 1
        return CapturedFrame(source_path=str(path), captured_at=to_utc_iso(moment))

    def last_frame_at(self) -> float | None:
        """最近一次成功取帧的 Unix 时间戳；拿不到读数诚实返回 None。"""
        camera = self._camera_or_raise()
        probe = getattr(camera, "health", None)
        info = probe() if callable(probe) else None
        age = info.get("last_ok_s") if isinstance(info, Mapping) else None
        if isinstance(age, bool) or not isinstance(age, (int, float)):
            return None
        return float(self._clock()) - float(age)

    def health(self) -> ComponentHealth:
        """帧新鲜度健康；探测失败收敛为 unavailable，绝不抛异常。"""
        checked_at = to_utc_iso(self._moment())
        try:
            last_frame_at = self.last_frame_at()
        except Exception as exc:  # noqa: BLE001 - 探针异常必须收敛
            return ComponentHealth(
                status="unavailable",
                checked_at=checked_at,
                reason="相机探测失败：%s: %s" % (type(exc).__name__, exc),
            )
        return camera_freshness_health(
            last_frame_at,
            float(self._clock()),
            self.freshness_timeout,
            checked_at=checked_at,
        )


@dataclass
class AutoFlowServices:
    """booth_server 与处理器之间的一次性装配（T-2）。

    字段全部可注入（T-3 直接构造即可，不依赖环境变量与真实相机）：
    - queue / consent / sessions：必填；
    - style_source：风格目录来源（Booth 传 StyleCatalog 实例；None = 包内默认目录）；
    - camera_probe：开始路由的帧新鲜度探针；None = 保守不可用；
    - gesture_enabled：开始响应里的 `gesture_enabled`（镜像 config.gesture.enabled，
      默认 false；真实手势会话由 T-6/T-15 接）；
    - clock：截止时间预判用的时钟；应与 QueueService 注入同一个（默认系统 UTC）。

    T-16 端口（全部可空；None / False = 未接，按「默认禁用」处理）。装配契约：
    后续任务**替换端口即可，不要编辑路由**：
    - photo_store / photo_service：T-7 存储与服务；`photo_service.require` 决定取图
      404/410，`photo_service.delete` 决定删除语义（含删除批准门控）；
    - camera / bridge：T-14 相机端口与 T-15 授权拍摄桥（`bridge.run` 是唯一按快门
      入口；本任务只装配，不新增拍摄路由）；
    - print_port：T-9 的 PrintPort；None = 打印默认禁用（503 PRINT_UNAVAILABLE）；
    - gateway：T-8 的 GatewayPort；**T-16 绝不调用它**（外置同意端点只记录意图）；
    - external_processing_enabled：外置处理条款/开关的当前值（默认 False），只影响
      外置同意端点的响应说明，不触发任何外发；
    - health：T-20 的 HealthService（/api/health 六组件与 /api/state capabilities）；
    - capture_state：可选无参 callable，返回当前拍摄阶段（T-20 投影的 capture 输入）。
    """

    queue: QueueService
    consent: ConsentService
    sessions: QueueSessionRegistry
    style_source: Any = None
    camera_probe: Any = None
    gesture_enabled: bool = False
    clock: Any = None
    photo_store: Any = None
    photo_service: Any = None
    camera: Any = None
    bridge: Any = None
    print_port: Any = None
    gateway: Any = None
    health: Any = None
    external_processing_enabled: bool = False
    capture_state: Any = None
    recovery: Any = None


# --------------------------------------------------------------------------- #
# 处理器
# --------------------------------------------------------------------------- #

def handle_queue_submit(
    payload: Any,
    *,
    queue: QueueService,
    consent: ConsentService,
    sessions: QueueSessionRegistry,
    style_source: Any = None,
) -> tuple[int, dict[str, Any]]:
    """处理 ``POST /api/queue``：首次 201、幂等重放 200，均附 ``session``。

    错误与顺序（抛 ApiError）：400 INVALID_REQUEST（非对象/缺必填/未知字段/
    可选字段非法）-> 404 STYLE_NOT_FOUND -> 403 CONSENT_REQUIRED（预检：缺失、
    过期、撤回、用途不符、已绑到别的预约；**不产生任何队列状态**）-> 409
    IDEMPOTENCY_CONFLICT / 429 QUEUE_FULL（QueueService）-> 403 CONSENT_REQUIRED
    （预检之后的绑定竞态；若条目是本次新建的，已 mark_cancelled 回滚，重放时
    绝不取消原条目）。
    """
    if not isinstance(payload, Mapping):
        raise errors.ApiError(errors.INVALID_REQUEST, "请求体必须是 JSON 对象。")
    try:
        submission = QueueSubmission.from_dict(payload)
    except ValueError as exc:
        raise errors.ApiError(errors.INVALID_REQUEST, str(exc)) from exc

    style_id = submission.style_id.strip()
    if style_id not in enabled_style_ids(style_source):
        raise errors.ApiError(errors.STYLE_NOT_FOUND)

    consent_id = submission.capture_consent_id.strip()
    _require_capture_consent(queue, consent, consent_id, submission.request_id.strip())

    result = queue.submit_result(submission)
    entry = result.entry
    try:
        authorize_queue_submission(
            consent, capture_consent_id=consent_id, queue_id=entry.queue_id
        )
    except errors.ApiError as exc:
        if result.created:
            # 预检之后才发生的极小竞态（同意被撤回/绑走）：回滚本次新建条目，
            # 不让它继续占容量与幂等索引；重放（created=False）保持原条目不动。
            queue.mark_cancelled(
                entry.queue_id, reason=f"提交同意绑定失败（{exc.code}），条目已回滚取消。"
            )
        raise
    try:
        session = sessions.issue(entry.queue_id)
    except OSError:
        session = sessions.issue_volatile(entry.queue_id)
        warnings.warn("拍摄会话暂未持久保存，请保留当前页面。", RuntimeWarning, stacklevel=2)
    status = 201 if result.created else 200
    return status, {"entry": entry.to_dict(), "session": session}


def handle_queue_get(
    queue_id: str,
    *,
    queue: QueueService,
    sessions: QueueSessionRegistry,
    session: Any = None,
) -> tuple[int, dict[str, Any]]:
    """处理 ``GET /api/queue/<queue_id>``：200 ``{"entry": QueueEntry}``。

    会话缺失/不匹配 -> 404 QUEUE_NOT_FOUND（隐藏存在性）；条目不存在同样 404。
    """
    if not sessions.check(session, queue_id):
        raise errors.ApiError(errors.QUEUE_NOT_FOUND)
    entry = queue.get(queue_id)
    if entry is None:
        raise errors.ApiError(errors.QUEUE_NOT_FOUND)
    return 200, {"entry": entry.to_dict()}


def handle_queue_start(
    queue_id: str,
    payload: Any,
    *,
    queue: QueueService,
    consent: ConsentService,
    sessions: QueueSessionRegistry,
    session: Any = None,
    camera_probe: Any = None,
    gesture_enabled: bool = False,
    clock: Any = None,
) -> tuple[int, dict[str, Any]]:
    """处理 ``POST /api/queue/<queue_id>/start``：202
    ``{"entry": QueueEntry, "gesture_enabled": bool}``。

    顺序：会话(404) -> 条目(404) -> 同意校验(400/403，verify_queue_start 要求
    同意正好绑定本队列) -> 状态/期限 -> 相机探针(503) -> QueueService.start(202)。
    未到号、已过期、重复开始等状态错误由 QueueService.start 统一抛出（409），
    且发生在相机检查之前，保证"未到号开始 409"不被 503 抢先；相机不可用时条目
    保持 called 原状（不占 ready 位、可重试）。
    """
    if not sessions.check(session, queue_id):
        raise errors.ApiError(errors.QUEUE_NOT_FOUND)
    entry = queue.get(queue_id)
    if entry is None:
        raise errors.ApiError(errors.QUEUE_NOT_FOUND)
    if not isinstance(payload, Mapping):
        raise errors.ApiError(errors.INVALID_REQUEST, "请求体必须是 JSON 对象。")
    capture_consent_id = payload.get("capture_consent_id")
    verify_queue_start(
        consent, capture_consent_id=capture_consent_id, queue_id=queue_id
    )

    if entry.status == QueueStatus.CALLED and not _deadline_passed(entry, clock):
        available, _diagnosis = evaluate_camera_probe(camera_probe)
        if not available:
            raise errors.ApiError(errors.CAMERA_UNAVAILABLE)

    started = queue.start(queue_id, capture_consent_id)
    return 202, {"entry": started.to_dict(), "gesture_enabled": bool(gesture_enabled)}


# --------------------------------------------------------------------------- #
# T-16 处理器：作业查询 / 外置同意 / 取图与删除 / 打印 / 风格目录 / 报名二维码
# --------------------------------------------------------------------------- #

def _entry_field(entry: Any, name: str, default: Any = None) -> Any:
    """从 QueueEntry（dataclass）或 Mapping 里读一个字段；缺失给 default。"""
    if isinstance(entry, Mapping):
        return entry.get(name, default)
    return getattr(entry, name, default)


def _status_value(entry: Any) -> str:
    """条目状态 -> 字符串（枚举与字符串两种来源都吃）。"""
    status = _entry_field(entry, "status", "")
    return str(getattr(status, "value", status))


def _pick_photo_record(photo_service: Any, job_id: str, *, strict: bool = False) -> Any:
    """该作业名下最新的一条可取图记录；没有（或服务未接）返回 None。"""
    store = getattr(photo_service, "store", None)
    records = getattr(store, "records", None)
    if not callable(records):
        return None
    try:
        items = records()
    except Exception:  # noqa: BLE001 - 诊断性读取失败按"没有记录"处理
        if strict:
            raise
        return None
    available = [
        item
        for item in items
        if getattr(item, "job_id", None) == job_id
        and getattr(item, "status", None) == "available"
    ]
    if not available:
        return None
    return max(available, key=lambda item: str(getattr(item, "created_at", "")))


def handle_job_get(
    job_id: str,
    *,
    queue: QueueService,
    sessions: QueueSessionRegistry,
    session: Any = None,
    photo_service: Any = None,
) -> tuple[int, dict[str, Any]]:
    """处理 ``GET /api/job/<job_id>``（T-16 新流程作业查询）。

    旧 JobBoard 的查询由路由层**先做**（逐字保留旧快照与旧扁平 404）；本处理器只
    负责 ``job_id == queue_id`` 形状的新流程作业：200 ``{"ok": true, "job": {...}}``。

    - 作业不存在 -> 404 JOB_NOT_FOUND（嵌套错误体）；
    - **私人字段门（硬要求）**：只有 ``X-Queue-Session`` 正好绑定本作业
      （``sessions.check``）时才返回 queue_id / display_number / photo_token /
      取图链接；未授权版本只回公开字段（status / updated_at / fallback /
      error_code / pickup_ready），拿不到任何可取图入口；
    - pickup_ready：条目已 ``ready_for_pickup`` 且存在可取图照片记录；
    - fallback / error_code：T-16 装配下外置处理默认禁用 -> 正常出图 fallback 恒为
      ``"none"``；条目落 failed 时 error_code 给 ``CAPTURE_FAILED``（拍摄链路唯一
      的失败出口），其他状态为 None。T-8 接入后可按其作业数据扩充（本处理器读的是
      PhotoRecord.variant / 条目状态，不固化假设）。
    """
    if not isinstance(job_id, str) or not job_id.strip():
        raise errors.ApiError(errors.JOB_NOT_FOUND)
    key = job_id.strip()
    entry = queue.get(key)
    if entry is None:
        raise errors.ApiError(errors.JOB_NOT_FOUND)
    status = _status_value(entry)
    record = _pick_photo_record(photo_service, key) if photo_service is not None else None
    job: dict[str, Any] = {
        "job_id": key,
        "status": status,
        "updated_at": _entry_field(entry, "updated_at"),
        "fallback": "none",
        "error_code": "CAPTURE_FAILED" if status == "failed" else None,
        "pickup_ready": bool(status == "ready_for_pickup" and record is not None),
    }
    if sessions.check(session, key):
        token = getattr(record, "photo_token", None) if record is not None else None
        links = (
            {
                "photo": "/api/photo/%s.jpg" % token,
                "thumb": "/api/thumb/%s.jpg" % token,
                "qr": "/api/qr/%s.png" % token,
            }
            if isinstance(token, str) and token
            else {"photo": None, "thumb": None, "qr": None}
        )
        job.update(
            {
                "queue_id": key,
                "portrait_mode": _entry_field(entry, "portrait_mode", "standard"),
                "ai_watermark": _entry_field(entry, "ai_watermark", True),
                "display_number": _entry_field(entry, "display_number"),
                "variant": getattr(record, "variant", None) if record is not None else None,
                "photo_token": token,
                **links,
            }
        )
    return 200, {"ok": True, "job": job}


def handle_external_processing_consent(
    job_id: str,
    payload: Any,
    *,
    queue: QueueService,
    consent: ConsentService,
    sessions: QueueSessionRegistry,
    session: Any = None,
    external_processing_enabled: bool = False,
) -> tuple[int, dict[str, Any]]:
    """处理 ``POST /api/job/<job_id>/external-processing-consent``（T-16 注册）。

    行为（默认禁用外置处理端口，只记录意图）：
    - 会话校验：``X-Queue-Session`` 必须绑定该作业，否则 404 JOB_NOT_FOUND
      （与队列查询同一套隐藏存在性语义，防止仅知道 id 的人替别人做外发选择）；
    - 作业不存在 -> 404 JOB_NOT_FOUND；请求体必须是 ``{"granted": 布尔}``，否则
      400 INVALID_REQUEST；
    - 通过后经 ConsentService 记录 external_processing 同意；``granted=true`` 时用
      ``bind_job`` 把同意绑定到本作业（幂等重放同一作业 OK，绑走抛拒绝）；
    - **绝不调用网关**：响应明确回 ``enabled``（外置处理条款/开关的当前值，默认
      False）与中文 detail；T-8 接上网关服务后复用本端点（替换装配，不编辑路由），
      外发时机由 T-8 服务在"等待窗口"内决定。
    """
    if not sessions.check(session, job_id):
        raise errors.ApiError(errors.JOB_NOT_FOUND)
    entry = queue.get(job_id) if isinstance(job_id, str) else None
    if entry is None:
        raise errors.ApiError(errors.JOB_NOT_FOUND)
    if not isinstance(payload, Mapping) or not isinstance(payload.get("granted"), bool):
        raise errors.ApiError(errors.INVALID_REQUEST, "请求体必须是 {\"granted\": 布尔值}。")
    granted = bool(payload["granted"])
    record = consent.record("external_processing", granted)
    if granted:
        consent.bind_job(record.consent_id, job_id.strip())
        detail = (
            "外置处理未启用：已记录同意，不会外发到任何网关；"
            "待 T-8 装配网关服务后按同意结果处理。"
            if not external_processing_enabled
            else "外置处理已启用：已记录同意并绑定作业，由外置处理服务按状态推进。"
        )
    else:
        detail = "已记录拒绝外置处理：照片只保留 L0 本地结果，零外发。"
    return 200, {
        "consent": record.to_dict(),
        "enabled": bool(external_processing_enabled),
        "detail": detail,
    }


def handle_photo_asset(
    token: str,
    kind: str,
    *,
    photo_service: Any,
) -> tuple[int, dict[str, Any]]:
    """解析新流程取图资产，成功返回 ``(200, {"path", "content_type"})``。

    kind ∈ ``{"photo", "thumb", "qr"}``；令牌非法 / 不存在 -> 404 INVALID_TOKEN /
    PHOTO_NOT_FOUND，已删除 -> 410 PHOTO_DELETED（嵌套错误体，spec 契约）；记录
    在但文件缺失按 PHOTO_NOT_FOUND 处理 —— 失败方向是"打不开"，绝不回落到别的
    文件。本处理器只做解析，不发响应（路由层负责读文件）。
    """
    if kind not in ("photo", "thumb", "qr"):
        raise ValueError("kind 必须是 photo/thumb/qr。")
    photo_service.require(token)
    getter = {
        "photo": getattr(photo_service, "photo_path", None),
        "thumb": getattr(photo_service, "thumb_path", None),
        "qr": getattr(photo_service, "qr_path", None),
    }[kind]
    path = getter(token) if callable(getter) else None
    if path is None or not Path(path).is_file():
        raise errors.ApiError(errors.PHOTO_NOT_FOUND)
    return 200, {
        "path": str(path),
        "content_type": "image/png" if kind == "qr" else "image/jpeg",
    }


def handle_photo_delete(token: str, *, photo_service: Any) -> tuple[int, dict[str, Any]]:
    """处理新流程 ``DELETE /api/photo/<token>``：200 ``{"deleted": bool}``。

    - 令牌非法 / 不存在 -> 404 INVALID_TOKEN / PHOTO_NOT_FOUND（嵌套）；
    - 并发删除 -> 409 DELETE_IN_PROGRESS；重复删除幂等：200 ``{"deleted": false}``；
    - 生产删除批准门控在 PhotoService 内（未批准抛 DeleteNotApprovedError，路由层
      转 500 嵌套），本处理器不绕过、不放宽。
    """
    deleted = photo_service.delete(token)
    return 200, {"deleted": bool(deleted)}


def handle_print_submit(
    payload: Any,
    *,
    photo_service: Any = None,
    print_port: Any = None,
) -> tuple[int, dict[str, Any]]:
    """处理 ``POST /api/print``（T-16 注册；端口由 T-9 装配替换）。

    顺序：请求形状 400 INVALID_REQUEST -> 端口未接 503 PRINT_UNAVAILABLE（默认
    禁用；端口由 T-9 注入后才有提交能力）-> 照片 404/410 -> 端口 ``submit``（幂等
    键原样下发，同键同载荷返回同作业的语义由端口保证）-> 202 ``{"print_job"}``。
    """
    if not isinstance(payload, Mapping):
        raise errors.ApiError(errors.INVALID_REQUEST, "请求体必须是 JSON 对象。")
    token = payload.get("photo_token")
    idempotency_key = payload.get("idempotency_key")
    if not isinstance(token, str) or not token.strip():
        raise errors.ApiError(errors.INVALID_REQUEST, "photo_token 必须是非空字符串。")
    if not isinstance(idempotency_key, str) or not idempotency_key.strip():
        raise errors.ApiError(errors.INVALID_REQUEST, "idempotency_key 必须是非空字符串。")
    if print_port is None or not callable(getattr(print_port, "submit", None)):
        raise errors.ApiError(errors.PRINT_UNAVAILABLE)
    if photo_service is not None:
        photo_service.require(token.strip())
    job = print_port.submit(token.strip(), idempotency_key.strip())
    return 202, {"print_job": job.to_dict() if callable(getattr(job, "to_dict", None)) else job}


def handle_print_get(print_job_id: str, *, print_port: Any = None) -> tuple[int, dict[str, Any]]:
    """处理 ``GET /api/print/<print_job_id>``：200 ``{"print_job": PrintJob}``。

    端口未接或任务未知一律 404 PRINT_JOB_NOT_FOUND（未接端口时"任务不存在"是
    诚实答案；503 只留给提交动作）。
    """
    job = None
    if print_port is not None and callable(getattr(print_port, "get", None)):
        job = print_port.get(print_job_id)
    if job is None:
        raise errors.ApiError(errors.PRINT_JOB_NOT_FOUND)
    return 200, {"print_job": job.to_dict() if callable(getattr(job, "to_dict", None)) else job}


def handle_gesture_detect(
    payload: Any,
    *,
    gesture_port: Any = None,
) -> tuple[int, dict[str, Any]]:
    """处理 ``POST /api/gesture/detect``：手势识别。

    请求体：``{"frame": <base64-encoded-image>, "format": "jpeg"}``
    成功响应：``{"gesture": str, "confidence": float, "detected_at": str}``
    """
    if not isinstance(payload, Mapping):
        raise errors.ApiError(errors.INVALID_REQUEST, "请求体必须是 JSON 对象。")
    frame_b64 = payload.get("frame")
    format_hint = payload.get("format", "jpeg")
    if not isinstance(frame_b64, str) or not frame_b64.strip():
        raise errors.ApiError(errors.INVALID_REQUEST, "frame 必须是非空字符串（base64）。")
    if gesture_port is None or not callable(getattr(gesture_port, "observe", None)):
        raise errors.ApiError(errors.ApiError("GESTURE_UNAVAILABLE", "手势识别未启用"))

    import base64
    import tempfile
    from pathlib import Path
    try:
        frame_bytes = base64.b64decode(frame_b64)
    except Exception as exc:
        raise errors.ApiError(errors.INVALID_REQUEST, f"frame 不是有效的 base64：{exc}")

    # 写临时文件（gesture worker 协议要求文件路径）
    ext = {"jpeg": ".jpg", "jpg": ".jpg", "png": ".png"}.get(str(format_hint).lower(), ".jpg")
    with tempfile.NamedTemporaryFile(suffix=ext, delete=False) as tmp:
        tmp.write(frame_bytes)
        tmp_path = tmp.name

    try:
        result = gesture_port.observe(tmp_path)
        return 200, {
            "gesture": result.gesture,
            "confidence": result.confidence,
            "observed_at": result.observed_at,
        }
    finally:
        try:
            Path(tmp_path).unlink(missing_ok=True)
        except Exception:
            pass


def handle_styles(style_source: Any = None) -> tuple[int, dict[str, Any]]:
    """处理 ``GET /api/styles``：200 ``{"ok": true, "count": n, "styles": [...]}``。

    只读契约（T-4 前端依赖）：字段集合见 ``public_style_catalog``；默认目录用
    ``app/data/style-catalog.json``，生产接 Booth.style_catalog（操作员编辑实时可见）。
    """
    styles = public_style_catalog(style_source)
    return 200, {"ok": True, "count": len(styles), "styles": styles}


# ---- 报名二维码（T-4 依赖） ------------------------------------------------ #

class QrUnavailableError(RuntimeError):
    """报名二维码不可用（render/qr 模块缺失或生成失败）；路由层转 503。"""


_JOIN_QR_LOCK = threading.Lock()
_JOIN_QR_CACHE: dict[str, bytes] = {}
_JOIN_QR_CACHE_MAX = 8


def public_join_url(public_base: Any) -> str:
    """报名页 URL（二维码内容）：``<public_base>/join``（去尾斜杠）。"""
    base = str(public_base or "").strip().rstrip("/")
    return "%s/join" % base


def _default_qr_factory(url: str) -> bytes:
    """双路径 import 仓库自带二维码编码器（app.render.qr 或 render.qr）。"""
    module = None
    for name in ("app.render.qr", "render.qr"):
        try:
            module = importlib.import_module(name)
            break
        except ImportError:
            module = None
    if module is None:
        raise QrUnavailableError("二维码模块不可用（app/render/qr.py 未找到）。")
    return module.make_qr_png(url, scale=10)


def join_qr_png(url: str, *, qr_factory: Any = None, cache: Any = None) -> bytes:
    """生成（并按 URL 内存缓存）报名二维码 PNG；不可用时抛 QrUnavailableError。

    cache=None 用模块级缓存（生产中 public_base 固定 -> 只有一份；地址变更时以新
    URL 为键重新生成，旧项按插入序淘汰）。qr_factory 可注入（测试断言编码内容）。
    """
    target = cache if cache is not None else _JOIN_QR_CACHE
    if isinstance(url, str) and url in target:
        return target[url]
    factory = qr_factory or _default_qr_factory
    try:
        png = factory(url)
    except QrUnavailableError:
        raise
    except Exception as exc:  # noqa: BLE001 - 编码器内部错误同样按不可用收敛
        raise QrUnavailableError("二维码生成失败：%s: %s" % (type(exc).__name__, exc)) from exc
    if not isinstance(png, (bytes, bytearray)) or not png:
        raise QrUnavailableError("二维码生成返回空结果，拒绝输出。")
    with _JOIN_QR_LOCK:
        target[url] = bytes(png)
        while len(target) > _JOIN_QR_CACHE_MAX:
            target.pop(next(iter(target)))
    return bytes(png)


# --------------------------------------------------------------------------- #
# 内部辅助
# --------------------------------------------------------------------------- #

def _require_capture_consent(
    queue: QueueService,
    consent: ConsentService,
    consent_id: str,
    request_id: str,
) -> None:
    """提交前的同意预检：有效且"没有绑到别的预约"，否则 403 CONSENT_REQUIRED。

    只做公开查询，不产生任何状态：
    1) 记录必须存在、用途是 capture、状态 valid（覆盖 granted/撤回/过期）；
    2) 若已绑定某预约：只有当那条预约正是本 request_id（幂等重放）才放行，
       否则 403 —— 这样"同一同意提交另一份预约"在创建条目前就被拒绝。
    绑定竞态（预检后、bind 前状态变化）由 authorize_queue_submission 兜底。
    """
    record = consent.get(consent_id)
    if record is None or record.purpose != "capture":
        raise errors.ApiError(errors.CONSENT_REQUIRED)
    if consent.status(consent_id) != "valid":
        raise errors.ApiError(errors.CONSENT_REQUIRED)
    binding = consent.binding_of(consent_id)
    if not binding:
        return
    bound_entry = queue.get(binding)
    if bound_entry is None or bound_entry.request_id != request_id:
        raise errors.ApiError(errors.CONSENT_REQUIRED)


def _deadline_passed(entry: QueueEntry, clock: Any = None) -> bool:
    """``now >= start_deadline`` 视为已到/已过；字段缺失或不可解析 -> False。

    与 QueueService._deadline_passed 同一口径；clock 应与 QueueService 注入同一个
    （生产默认都取系统 UTC）。该判断只决定"该不该先查相机"：即便口径漂移，
    QueueService.start 仍会自己判定期限并抛 QUEUE_EXPIRED，不会把过期条目放行。
    """
    raw = entry.start_deadline
    if not raw:
        return False
    try:
        deadline = parse_utc_iso(raw)
    except ValueError:
        return False
    return _now(clock) >= deadline


def _now(clock: Any = None) -> datetime:
    """取当前时刻（带时区 UTC）；clock 可调用或带 .now()，naive 按 UTC 解释。"""
    if clock is None:
        return datetime.now(_UTC)
    if hasattr(clock, "now") and callable(getattr(clock, "now")):
        moment = clock.now()
    elif callable(clock):
        moment = clock()
    else:
        raise TypeError("clock 必须是可调用对象或带 .now() 的对象。")
    if not isinstance(moment, datetime):
        raise TypeError(f"clock 必须返回 datetime，得到 {type(moment).__name__}。")
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=_UTC)
    return moment.astimezone(_UTC)
