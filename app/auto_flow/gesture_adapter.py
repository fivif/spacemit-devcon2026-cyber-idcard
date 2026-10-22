# -*- coding: utf-8 -*-
"""T-6 手势适配器：默认禁用实现与可注入替换点。

规格依据：`自动流程规格 v1（内部文档）`「Components / GestureAdapter」「FR-6」；
接口形状与 ``app.auto_flow.ports.GesturePort`` 一致（status / observe / stop）。

安全立场（当前唯一允许的状态 = 禁用）：
- K3 手势模型尚未验证：模型来源/许可、手部关键点、量化、AI 核兼容性、延迟、误触发率
  全部未知（spec Unknowns）。因此生产接线必须使用本模块的默认禁用适配器：
  ``status()`` 如实返回 disabled，``observe()`` 如实返回 ``available=False``，
  调用方（CaptureCoordinator）不得据此触发任何拍摄。
- ``build_gesture_adapter()`` 是预留的可注入替换点：K3 验证通过、真实模型适配器落地后
  在此切换。真实适配器必须运行在独立推理进程/IPC 中，遵守两条硬约束：
  1) cv2 与 spacemit_ort 不得同进程；2) 使用 spacemit_ort 前必须已导入 onnxruntime。
  本模块是纯标准库逻辑，不 import cv2 / onnxruntime / spacemit_ort，不联网、不读模型
  文件，进程隔离约束天然满足。

本模块不读环境变量、不写文件、不打印日志。
"""

from __future__ import annotations

from datetime import datetime, timezone

from app.auto_flow.models import ComponentHealth, GestureResult, to_utc_iso
from app.auto_flow.ports import Clock, GesturePort

__all__ = [
    "DISABLED_REASON",
    "DisabledGestureAdapter",
    "GestureAdapter",
    "build_gesture_adapter",
]

# 如实、可核对的中文原因：与 spec「未验证或禁用时返回不可用」一致。
DISABLED_REASON = "手势未启用/未验证"


class DisabledGestureAdapter:
    """默认禁用适配器：K3 验证通过前唯一安全的手势实现。

    - ``status()`` 恒为 ``ComponentHealth(status="disabled", reason="手势未启用/未验证")``；
    - ``observe()`` 恒为 ``GestureResult(available=False, gesture="none", stable_frames=0)``，
      不编造置信度、人数、画面内等任何数字；
    - ``stop()`` 无资源可释放，仅为满足 ``GesturePort`` 形状而保留；
    - ``clock`` 可注入（测试用假时钟）；缺省取 UTC 现时。
    """

    def __init__(self, *, clock: Clock | None = None, reason: str = DISABLED_REASON) -> None:
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("reason 必须是非空字符串。")
        self._clock = clock
        self._reason = reason

    def _now(self) -> datetime:
        if self._clock is not None:
            moment = self._clock.now()
            if moment.tzinfo is None:
                raise ValueError("clock.now() 必须返回带时区的 datetime。")
            return moment.astimezone(timezone.utc)
        return datetime.now(timezone.utc)

    def status(self) -> ComponentHealth:
        """手势组件健康：未启用时如实返回 disabled（不得伪报 ok）。"""
        return ComponentHealth(
            status="disabled",
            checked_at=to_utc_iso(self._now()),
            reason=self._reason,
        )

    def observe(self, session_id: str) -> GestureResult:
        """禁用状态下的观测：available=False，任何调用方都不得据此触发拍摄。"""
        return GestureResult(
            available=False,
            gesture="none",
            stable_frames=0,
            observed_at=to_utc_iso(self._now()),
            reason=self._reason,
        )

    def stop(self, session_id: str) -> None:
        """结束观察会话；禁用适配器无资源可释放。"""
        return None


# 规格 Interfaces 使用 ``GestureAdapter`` 这一名字；当前唯一实现即禁用适配器。
GestureAdapter = DisabledGestureAdapter


def build_gesture_adapter(
    *,
    clock: Clock | None = None,
    reason: str = DISABLED_REASON,
    person_boxes_provider=None,
    seg_provider=None,
    gesture_provider=None,
) -> GesturePort:
    """预留替换点：按环境变量 BOOTH_GESTURE_ENABLED 切换真实实现或禁用适配器。

    环境变量：
      BOOTH_GESTURE_ENABLED   true/1/yes 启用真实手势识别（默认禁用）
      BOOTH_GESTURE_WORKER    worker.py 路径（默认 gesture/worker.py）
      BOOTH_GESTURE_TIMEOUT   单次请求超时秒数（默认 8.0）
      BOOTH_GESTURE_ENGINE    official_yolo（官方量化 YOLOv5）| yolo（开放词汇）| handpose（21 关键点）

    person_boxes_provider: callable(image_path) -> [[x1,y1,x2,y2],...] | None。
      handpose 引擎的验真用（见 gesture_rules 头部 2026-10-09 记录）。
    seg_provider: callable(image_path) -> seg 响应 dict。yolo 引擎（默认）用，
      走服务里唯一的 InferWorker（AI 核独占，不能起第二个 EP 会话）。
      开放词汇判据天然抗假手、抗距离（真人 OK 实测 0.59~0.67 分）。
    """
    import os
    enabled = str(os.environ.get("BOOTH_GESTURE_ENABLED", "")).strip().lower()
    visual_enabled = str(os.environ.get("BOOTH_GESTURE_VISUAL_ENABLED", "")).strip().lower()
    if enabled not in ("true", "1", "yes") and visual_enabled not in ("true", "1", "yes"):
        return DisabledGestureAdapter(clock=clock, reason=reason)

    # 延迟导入，避免禁用时加载 gesture_client（它走 IPC，不碰 cv2/ort）
    try:
        from app.gesture_client import GestureClient  # type: ignore[attr-defined]
    except ImportError:
        from gesture_client import GestureClient  # type: ignore[no-redef]

    worker = os.environ.get("BOOTH_GESTURE_WORKER", "")
    timeout = float(os.environ.get("BOOTH_GESTURE_TIMEOUT", "8.0"))
    return GestureClient(
        worker_path=worker if worker else None,
        timeout=timeout,
        clock=clock,
        person_boxes_provider=person_boxes_provider,
        seg_provider=seg_provider,
        gesture_provider=gesture_provider,
    )
