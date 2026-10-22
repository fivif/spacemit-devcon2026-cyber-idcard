# -*- coding: utf-8 -*-
"""组件健康聚合（T-20）：帧新鲜度优先的相机健康 + 六组件系统快照。

规格依据：`docs/spec/自动流程规格-v1.md` 的「Data models」（SystemHealth /
ComponentHealth）、「Error handling」（CAMERA_UNAVAILABLE = 相机健康检查拿不到帧）
与 T-20 任务合同。

硬约束（逐条对应 T-20 合同）：
- 相机健康**只认帧新鲜度**：`camera_freshness_health(last_frame_at, now, timeout)`
  在「从未有过帧」「超过 timeout 没有新帧」「时间戳无效」时判 unavailable。
  设备枚举成功、STREAMON 成功等「能打开」的证据一律不得作为可用依据——
  已知故障场景里相机物理掉线时设备枚举仍可能成功。
- 六个组件（camera/gesture/queue/storage/aigc/printer）全部由注入探针产生：
  探针可以是无参 callable，也可以是带 `.health()` / `.status()` 的小对象
  （兼容 T-14 `ports.py` 的 CameraPort / GesturePort / GatewayPort / PrintPort 形状）。
  本模块不 import 硬件、不做 IO、不读环境变量。
- 值对象一律用 `app/auto_flow/models.py` 的 ComponentHealth / SystemHealth（T-1 权威
  定义），本模块不复制定义。
- 保守失败方向：探针缺失 / 异常 / 返回值不符合契约，一律收敛为「不可用或禁用」，
  绝不让坏探针把系统说成健康。

时钟约定：`clock` 返回 Unix 时间戳（秒），与帧探针返回的 last_frame_at 同源。
"""

from __future__ import annotations

import math
import time
from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from typing import Any

from .models import (
    COMPONENT_STATUSES,
    ComponentHealth,
    SystemHealth,
    parse_utc_iso,
    to_utc_iso,
    utc_now_iso,
)

__all__ = [
    "COMPONENT_NAMES",
    "DEFAULT_CAMERA_TIMEOUT_SECONDS",
    "FIXTURE_CHECKED_AT",
    "HealthService",
    "camera_freshness_health",
    "demo_snapshot",
]

# 六个组件名，与 SystemHealth 字段一一对应
COMPONENT_NAMES: tuple[str, ...] = ("camera", "gesture", "queue", "storage", "aigc", "printer")

# 除相机外的探针名（相机走帧探针专用参数，见 HealthService）
_PROBE_NAMES: tuple[str, ...] = ("gesture", "queue", "storage", "aigc", "printer")

# 与 app/auto_flow/config.py 的 camera.health_timeout_seconds 默认值一致
DEFAULT_CAMERA_TIMEOUT_SECONDS = 2.0

# demo_snapshot() 固定夹具时间戳（可复现、便于断言）
FIXTURE_CHECKED_AT = "2026-09-30T00:00:00Z"


def _checked_at(now: float) -> str:
    """把时钟读数（Unix 秒）转成 models 约定的 ISO 8601 UTC 字符串。"""
    try:
        return to_utc_iso(datetime.fromtimestamp(float(now), tz=timezone.utc))
    except (OverflowError, OSError, ValueError):
        return utc_now_iso()


def camera_freshness_health(
    last_frame_at: float | None,
    now: float,
    timeout: float,
    *,
    checked_at: str | None = None,
) -> ComponentHealth:
    """相机健康：只看「最近一帧有多新」，不看设备能否枚举。

    last_frame_at: 最近一次成功取帧的 Unix 时间戳（秒）；从未取到过传 None。
    now:           当前 Unix 时间戳（秒），与 last_frame_at 同源。
    timeout:       新鲜度阈值（秒）；超过即 unavailable。
    返回 models.ComponentHealth；reason 为中文（给操作员看诊断）。
    """
    at = checked_at if checked_at is not None else _checked_at(now)
    if last_frame_at is None:
        return ComponentHealth(
            status="unavailable",
            checked_at=at,
            reason="相机不可用：尚未收到任何画面（判定以帧新鲜度为准，设备枚举成功不算可用）。",
        )
    numeric = (
        isinstance(last_frame_at, (int, float))
        and not isinstance(last_frame_at, bool)
        and isinstance(now, (int, float))
        and not isinstance(now, bool)
        and isinstance(timeout, (int, float))
        and not isinstance(timeout, bool)
    )
    if (
        not numeric
        or not math.isfinite(float(last_frame_at))
        or not math.isfinite(float(now))
        or not math.isfinite(float(timeout))
        or float(timeout) <= 0
    ):
        return ComponentHealth(
            status="unavailable",
            checked_at=at,
            reason="相机不可用：帧时间戳或健康阈值无效，无法确认画面新鲜度。",
        )
    age = float(now) - float(last_frame_at)
    if age > float(timeout):
        return ComponentHealth(
            status="unavailable",
            checked_at=at,
            reason="相机不可用：已 %.1f 秒没有新画面（阈值 %.1f 秒）。" % (age, float(timeout)),
        )
    return ComponentHealth(status="ok", checked_at=at, reason=None)


def _call_probe(probe: Any) -> Any:
    """调用探针：无参 callable，或带 .health() / .status() 的小对象。"""
    if callable(probe):
        return probe()
    for method_name in ("health", "status"):
        method = getattr(probe, method_name, None)
        if callable(method):
            return method()
    raise TypeError("探针既不可调用，也没有 health()/status() 方法")


def _normalize_component_health(value: Any) -> ComponentHealth | None:
    """把探针返回值收成合法的 ComponentHealth；不符合契约返回 None。"""
    if isinstance(value, ComponentHealth):
        candidate = value
    elif isinstance(value, Mapping):
        try:
            candidate = ComponentHealth.from_dict(value)
        except ValueError:
            return None
    else:
        return None
    if candidate.status not in COMPONENT_STATUSES:
        return None
    try:
        parse_utc_iso(candidate.checked_at)
    except ValueError:
        return None
    return candidate


class HealthService:
    """六组件健康聚合器。

    用法（T-2/T-16 接线示例）::

        service = HealthService(
            {"gesture": gesture_port, "aigc": gateway_port, "printer": printer_port,
             "queue": queue_service_probe, "storage": storage_probe},
            camera=camera_port,         # 需实现 last_frame_at() -> float | None
            clock=clock.now_seconds,    # 默认 time.time
            camera_timeout=cfg.camera.health_timeout_seconds,
        )
        health = service.snapshot()      # -> models.SystemHealth

    探针契约：
    - 除相机外，探针返回 models.ComponentHealth（或可被其 from_dict 校验的字典）；
    - 相机探针返回「最后一次成功取帧的 Unix 时间戳」或 None——
      相机健康由本模块的帧新鲜度判定，探针不得直接给「ok」；
    - 探针异常不会向上抛：收敛为对应组件的 unavailable（reason 中文）。
    """

    def __init__(
        self,
        probes: Mapping[str, Any] | None = None,
        *,
        camera: Any = None,
        clock: Callable[[], float] = time.time,
        camera_timeout: float = DEFAULT_CAMERA_TIMEOUT_SECONDS,
    ) -> None:
        if not callable(clock):
            raise TypeError("clock 必须是无参 callable（返回 Unix 秒）")
        timeout = float(camera_timeout)
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("camera_timeout 必须是正的有限秒数")
        supplied = dict(probes or {})
        unknown = sorted(set(supplied) - set(_PROBE_NAMES))
        if unknown:
            raise ValueError(
                "未知探针名 %s；可用：%s（相机用 camera= 传帧探针）"
                % (unknown, list(_PROBE_NAMES))
            )
        self._probes = supplied
        self._camera = camera
        self._clock = clock
        self._camera_timeout = timeout

    # ---- 组件 ----------------------------------------------------------

    def _camera_health(self, now: float, checked_at: str) -> ComponentHealth:
        if self._camera is None:
            return ComponentHealth(
                status="unavailable",
                checked_at=checked_at,
                reason="相机不可用：未接入帧探针，无法确认画面新鲜度。",
            )
        try:
            if callable(self._camera):
                last_frame_at = self._camera()
            else:
                method = getattr(self._camera, "last_frame_at", None)
                if not callable(method):
                    raise TypeError("需要 callable 或 last_frame_at() 方法")
                last_frame_at = method()
        except Exception as exc:
            return ComponentHealth(
                status="unavailable",
                checked_at=checked_at,
                reason="相机不可用：帧探针异常（%s: %s）。" % (type(exc).__name__, exc),
            )
        return camera_freshness_health(
            last_frame_at, now, self._camera_timeout, checked_at=checked_at
        )

    def _probe_health(self, name: str, checked_at: str) -> ComponentHealth:
        probe = self._probes.get(name)
        if probe is None:
            return ComponentHealth(
                status="disabled",
                checked_at=checked_at,
                reason="未接入 %s 探针，按禁用处理。" % name,
            )
        try:
            raw = _call_probe(probe)
        except Exception as exc:
            return ComponentHealth(
                status="unavailable",
                checked_at=checked_at,
                reason="%s 探针异常（%s: %s）。" % (name, type(exc).__name__, exc),
            )
        normalized = _normalize_component_health(raw)
        if normalized is None:
            return ComponentHealth(
                status="unavailable",
                checked_at=checked_at,
                reason="%s 探针返回值不符合 ComponentHealth 契约。" % name,
            )
        return normalized

    # ---- 快照 ----------------------------------------------------------

    def snapshot(self) -> SystemHealth:
        """六个组件的当前健康；每个组件一次探测，互不影响。"""
        now = float(self._clock())
        checked_at = _checked_at(now)
        return SystemHealth(
            camera=self._camera_health(now, checked_at),
            gesture=self._probe_health("gesture", checked_at),
            queue=self._probe_health("queue", checked_at),
            storage=self._probe_health("storage", checked_at),
            aigc=self._probe_health("aigc", checked_at),
            printer=self._probe_health("printer", checked_at),
        )


def demo_snapshot(checked_at: str = FIXTURE_CHECKED_AT) -> SystemHealth:
    """固定演示夹具（T-20 合同口径，时间戳固定以便断言与比对）。

    camera 无帧 -> unavailable；gesture 禁用；queue 到号（ok）；storage 正常（ok）；
    aigc 禁用；printer 禁用。
    """
    return SystemHealth(
        camera=ComponentHealth(
            status="unavailable",
            checked_at=checked_at,
            reason="相机不可用：固定夹具为无帧场景（以帧新鲜度为准）。",
        ),
        gesture=ComponentHealth(
            status="disabled",
            checked_at=checked_at,
            reason="手势功能未启用。",
        ),
        queue=ComponentHealth(
            status="ok",
            checked_at=checked_at,
            reason="队列正常：已叫号，等待拍摄。",
        ),
        storage=ComponentHealth(
            status="ok",
            checked_at=checked_at,
            reason="存储读写正常。",
        ),
        aigc=ComponentHealth(
            status="disabled",
            checked_at=checked_at,
            reason="风格化（AIGC）未启用。",
        ),
        printer=ComponentHealth(
            status="disabled",
            checked_at=checked_at,
            reason="打印未启用。",
        ),
    )
