# -*- coding: utf-8 -*-
"""auto_flow 可替换端口协议与抓帧结果（T-14）。

规格依据：`自动流程规格 v1（内部文档）`「Interfaces」「Testing strategy」「File and module layout」。

- 本模块只定义端口（typing.Protocol）与抓帧结果值对象 ``CapturedFrame``；组件健康、手势结果、
  照片记录、打印任务等值对象的权威定义在 ``app/auto_flow/models.py``，这里只 import，不重复定义。
- 端口职责约定：同意校验属于调用方——拍摄处理器必须先完成同意检查，再把已经校验过的
  ``consent_id`` 传给 :meth:`CameraPort.capture_authorized`；相机端口不自行判断同意语义，
  只记录收到的 ``consent_id``（便于测试断言「未同意时相机从未被调用」）。
- 生产适配器包装既有相机路径（``app/camera.py`` 的 open/read_bgr/snapshot；生产接线由
  T-2/T-16 负责，T-14 不改 ``booth_server.py``）；测试假实现见 ``app/tests/auto_flow_fixtures.py``，
  两边都实现这些端口。
- 网关端口约定：超时抛 ``errors.ApiError(AIGC_TIMEOUT)``（504），失败抛
  ``errors.ApiError(AIGC_FAILED)``（502）；请求只允许携带图像与风格字段（见 spec Security）。
- 命名兼容：任务书写 ``Clock``/``PrintPort``，spec 正文写 ``ClockPort``/``PrinterPort``；
  本模块以任务书为准定义主名，并保留等价别名供后续任务 import。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol, runtime_checkable

try:
    from app.auto_flow.models import ComponentHealth, GestureResult, PhotoRecord, PrintJob
except ImportError:  # pragma: no cover - 取决于启动方式
    from auto_flow.models import ComponentHealth, GestureResult, PhotoRecord, PrintJob  # type: ignore[no-redef]

__all__ = [
    "CapturedFrame",
    "CameraPort",
    "Clock",
    "ClockPort",
    "GesturePort",
    "GatewayPort",
    "PrintPort",
    "PrinterPort",
    "PhotoStorePort",
]


@dataclass(frozen=True)
class CapturedFrame:
    """一次已获同意的抓帧结果。

    source_path: 图像（合成或实拍）落盘路径；测试夹具一律写临时目录。
    captured_at: 拍摄时刻，ISO 8601 UTC 字符串；测试里由注入时钟给出，可脚本化。
    """

    source_path: str
    captured_at: str


@runtime_checkable
class CameraPort(Protocol):
    """相机端口：健康探测 + 已获同意后的抓帧。"""

    def health(self) -> ComponentHealth:
        """相机组件健康；不得因探测失败抛异常，失败计入 status/reason。"""
        ...

    def capture_authorized(self, consent_id: str) -> CapturedFrame:
        """执行一次抓帧。调用方必须已完成同意校验；consent_id 仅用于记录/追溯。"""
        ...


@runtime_checkable
class Clock(Protocol):
    """时钟端口：所有时间戳都从这里取，测试用假时钟推进。"""

    def now(self) -> datetime:
        """当前时刻（带时区；夹具统一 UTC）。"""
        ...


@runtime_checkable
class GesturePort(Protocol):
    """手势端口：与 spec ``GestureAdapter`` 接口一致；手势默认关闭。"""

    def status(self) -> ComponentHealth:
        """手势组件健康；未启用时必须如实返回 disabled。"""
        ...

    def observe(self, session_id: str) -> GestureResult:
        """观察一次手势（或取最近一次稳定结果）；不得阻塞拍摄主流程。"""
        ...

    def stop(self, session_id: str) -> None:
        """结束观察会话，释放资源。"""
        ...


@runtime_checkable
class GatewayPort(Protocol):
    """外发风格化网关端口；只允许传图像与风格字段，禁止携带个人信息/令牌。"""

    def health(self) -> ComponentHealth:
        """网关组件健康。"""
        ...

    def edit_image(self, source_path: str, style_id: str, output_path: str) -> str:
        """按 style_id 风格化 source_path 的图像，写入 output_path 并返回该路径。

        超时抛 ``errors.ApiError(AIGC_TIMEOUT)``；失败抛 ``errors.ApiError(AIGC_FAILED)``。
        """
        ...


@runtime_checkable
class PrintPort(Protocol):
    """打印端口：与 spec ``PrintAdapter`` 接口一致；打印默认关闭。"""

    def status(self) -> ComponentHealth:
        """打印组件健康；未启用时必须如实返回 disabled。"""
        ...

    def submit(self, photo_token: str, idempotency_key: str) -> PrintJob:
        """提交打印；未启用/未验收时抛 ``errors.ApiError(PRINT_UNAVAILABLE)``（503）。"""
        ...

    def get(self, print_job_id: str) -> PrintJob | None:
        """查询打印任务；未知返回 None。"""
        ...


@runtime_checkable
class PhotoStorePort(Protocol):
    """照片存储端口：把已拍摄/已风格化的图像提交为可取图记录。"""

    def commit(self, job_id: str, source_path: str, variant: str) -> PhotoRecord:
        """落盘并返回 PhotoRecord（variant ∈ {"L0", "styled"}）；photo_token 22 位随机。"""
        ...


# 任务书与 spec 正文的命名差异：保留等价别名（同一对象，不是第二份定义）。
ClockPort = Clock
PrinterPort = PrintPort
