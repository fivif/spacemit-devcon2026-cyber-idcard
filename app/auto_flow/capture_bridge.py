# -*- coding: utf-8 -*-
"""T-15 授权拍摄桥：触发 -> 抓帧 -> PhotoStore 提交 -> T-1 状态机推进。

规格依据：`自动流程规格 v1（内部文档）`（「Components / CaptureCoordinator」
「Data models / CaptureJob」「Error handling」）与任务 T-15。

职责边界：
- 只负责“按下快门之后的编排”：调用注入的相机端口抓帧、经 PhotoStore 端口提交、
  再按 T-1 的状态机推进队列条目；不做倒计时（`countdown_service.py`）、不做 HTTP
  （T-16 接线）。
- **改动现状范围**：本模块不改 ``booth_server.py``。生产接线（T-16）注入的拍摄适配器
  来自现有 ``app/camera.py``（``Camera.snapshot()`` 返回 JPEG 字节，落盘后再交给
  ``PhotoStorePort.commit``）；测试注入 ``app/tests/auto_flow_fixtures.py`` 的
  ``FakeCamera``。两者都实现 `app.auto_flow.ports.CameraPort`。
- **令牌唯一来源**：可用的 ``PhotoRecord``（含 22 位 ``photo_token``）只能由
  ``PhotoStorePort.commit`` 产出；本模块没有任何其它签发入口。任何一步失败都不会
  走到 commit，因此失败路径上既没有照片、也没有取图令牌。
- 按 T-1 状态机推进（单一权威顺序）：``ready -> capturing -> processing ->
  ready_for_pickup``。**不能**从 ``ready`` 直接跳 ``ready_for_pickup``
  （`queue_service._ALLOWED_TRANSITIONS` 里没有这条边）；L0 是“无外置风格化”的产出，
  不是无处理流程。任一步失败统一走 ``mark_failed``（终态，释放拍摄位）。

异常口径：拍摄/提交/推进失败一律抛 ``errors.ApiError(CAPTURE_FAILED)``（503，
「拍摄失败，未生成照片；请重试或找工作人员。」）；作业已落 failed、无图片、无令牌。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from app.auto_flow import errors
from app.auto_flow.models import PhotoRecord, to_utc_iso
from app.auto_flow.ports import CameraPort, Clock, PhotoStorePort
from app.auto_flow.queue_service import InvalidTransitionError

__all__ = [
    "DEFAULT_VARIANT",
    "CaptureResult",
    "CaptureFailedError",
    "CaptureBridge",
]

# 默认提交变体：L0 = 未做外置风格化的原图（spec PHOTO_VARIANTS）。
DEFAULT_VARIANT = "L0"


class CaptureFailedError(errors.ApiError):
    """拍摄链路失败（相机/存储/状态机）；对外统一 CAPTURE_FAILED。"""

    def __init__(self, message: str) -> None:
        super().__init__(errors.CAPTURE_FAILED, message)


@dataclass(frozen=True)
class CaptureResult:
    """一次成功拍摄的结果；同样可作“已处理过、未重拍”的返回。"""

    job_id: str
    queue_id: str
    queue_status: str
    photo: PhotoRecord | None
    captured_at: str | None
    source_path: str | None
    duplicate: bool = False

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "job_id": self.job_id,
            "queue_id": self.queue_id,
            "queue_status": self.queue_status,
            "duplicate": self.duplicate,
        }
        if self.photo is not None:
            payload["photo"] = self.photo.to_dict()
        if self.captured_at is not None:
            payload["captured_at"] = self.captured_at
        if self.source_path is not None:
            payload["source_path"] = self.source_path
        return payload


def _entry_status(entry: Any) -> str:
    """队列条目状态 -> 字符串（兼容枚举与字符串两种来源）。"""
    status = getattr(entry, "status", None)
    return str(getattr(status, "value", status))


class CaptureBridge:
    """触发 -> 拍摄 -> 提交 的桥（T-15）。

    构造参数：
    - ``queue_service``：T-1 的 ``QueueService``（唯一状态机入口）；
    - ``camera``：实现 ``CameraPort`` 的抓帧端口（生产 = `app/camera.py` 适配器，
      测试 = ``FakeCamera``）；
    - ``photo_store``：实现 ``PhotoStorePort`` 的提交端口（唯一签发取图令牌的地方）；
    - ``clock`` / ``now_fn``：可注入时钟；仅用于记录 ``captured_at``（抓帧端口自身
      也会带拍摄时间戳，这里作为兜底）。

    ``run()`` 是唯一执行入口；倒计时归零必须携带 ``capture_due=True`` 的 tick 才调用，
    且 ``ready_for_pickup`` 条目再次调用会返回 ``duplicate=True`` 且**不重拍**。
    """

    def __init__(
        self,
        *,
        queue_service: Any,
        camera: CameraPort,
        photo_store: PhotoStorePort,
        clock: Clock | None = None,
        now_fn: Any | None = None,
    ) -> None:
        if now_fn is not None and clock is not None:
            raise ValueError("now_fn 与 clock 只能给一个（等价写法）。")
        for name, port, required in (
            ("queue_service", queue_service, ("get", "mark_capturing", "mark_failed")),
            ("camera", camera, ("capture_authorized",)),
            ("photo_store", photo_store, ("commit",)),
        ):
            for method in required:
                if not callable(getattr(port, method, None)):
                    raise TypeError(f"{name} 必须实现 {method}()。")
        if now_fn is not None:
            if not callable(now_fn):
                raise ValueError("now_fn 必须是可调用对象。")
            self._now_fn = now_fn
        elif clock is not None:
            self._now_fn = clock.now
        else:
            self._now_fn = lambda: datetime.now(timezone.utc)

        self._queue = queue_service
        self._camera = camera
        self._store = photo_store

    def run(
        self,
        *,
        queue_id: str,
        capture_consent_id: str | None = None,
        job_id: str | None = None,
        variant: str = DEFAULT_VARIANT,
        capture_due: bool = False,
    ) -> CaptureResult:
        """执行一次授权拍摄。

        - ``capture_due`` 必须为 True（由倒计时服务的 tick 给出）；否则直接拒绝——
          没有安全门的“应拍摄”决定，绝不按快门（T-15 合同）；
        - 队列条目必须是 ``ready``；``ready_for_pickup`` 已出片时返回
          ``duplicate=True`` 且不重拍；终态或未到号抛 ``CAPTURE_FAILED``；
        - 成功：``camera.capture_authorized`` -> ``store.commit(job_id, source_path,
          variant)`` -> ``mark_capturing`` -> ``mark_processing`` -> ``mark_ready_for_pickup``；
          生产适配器可提供可选的 ``commit_async``：此时 ``mark_processing`` 会在
          抓帧后立即发生，后台适配器在生成完成后负责 ``mark_ready_for_pickup``；
        - 失败：统一 ``mark_failed`` 并抛 ``errors.ApiError(CAPTURE_FAILED)``；不产生
          ``PhotoRecord``、不签发取图令牌。
        """
        if not capture_due:
            raise CaptureFailedError("缺少倒计时给出的“应拍摄”决定，拒绝按快门。")
        if not isinstance(queue_id, str) or not queue_id.strip():
            raise errors.ApiError(errors.INVALID_REQUEST, "queue_id 必须是非空字符串。")
        if variant not in ("L0", "styled"):
            raise errors.ApiError(errors.INVALID_REQUEST, f"variant 非法：{variant!r}。")
        entry = self._queue.get(queue_id)
        if entry is None:
            raise errors.ApiError(errors.QUEUE_NOT_FOUND)
        status = _entry_status(entry)

        if status == "ready_for_pickup":
            # 刷新/重放不得重拍：条目的终态本身就是幂等证据，直接返回现状。
            return CaptureResult(
                job_id=job_id or self._job_id_of(entry),
                queue_id=queue_id,
                queue_status=status,
                photo=None,
                captured_at=None,
                source_path=None,
                duplicate=True,
            )
        if status != "ready":
            raise CaptureFailedError(f"队列条目状态为 {status}，不在可拍摄的 ready 状态。")

        capture_consent_id = self._require_consent(capture_consent_id, entry)
        job_key = job_id or self._job_id_of(entry, fallback="job-unknown")

        try:
            # ① 取号：进入 capturing（同时接入 T-1 的拍摄位状态）。
            self._queue.mark_capturing(queue_id)
            # ② 抓帧：只能用调用方给的已校验 consent_id 换取一次授权抓帧。
            frame = self._camera.capture_authorized(capture_consent_id)
            source_path = getattr(frame, "source_path", None)
            captured_at = getattr(frame, "captured_at", None)
            if not isinstance(source_path, str) or not source_path:
                raise ValueError("相机端口返回的 CapturedFrame 缺少 source_path。")
            # ③ 提交：生产队列使用异步提交。它只接管已经落盘的终帧，
            #    不把 AIGC 等待时间放在拍摄位上；测试和普通适配器仍走同步契约。
            commit_async = getattr(self._store, "commit_async", None)
            if callable(commit_async):
                # 先切到 processing，再启动线程，避免极快的后台任务在
                # capturing 状态下抢先完成，造成 ready_for_pickup 状态竞争。
                status_after = self._queue.mark_processing(queue_id)
                commit_async(job_key, source_path, variant)
                return CaptureResult(
                    job_id=job_key,
                    queue_id=queue_id,
                    queue_status=_entry_status(status_after),
                    photo=None,
                    captured_at=(
                        captured_at if isinstance(captured_at, str) and captured_at
                        else to_utc_iso(self._now_fn())
                    ),
                    source_path=source_path,
                )
            # 同步适配器：唯一产生 PhotoRecord（与取图令牌）的一步。
            photo = self._store.commit(job_key, source_path, variant)
        except Exception as exc:  # noqa: BLE001 - 任何失败都必须落到 failed 且无令牌
            self._mark_failed(queue_id, exc)
            raise CaptureFailedError(f"拍摄失败：{type(exc).__name__}: {exc}") from exc

        # ④ 推进：capturing -> processing -> ready_for_pickup（processing 释放拍摄位，
        #    让下一位可以到号；不能从 ready 直跳 ready_for_pickup，见模块 docstring）。
        status_after = self._advance(queue_id, exc_reason=None)
        return CaptureResult(
            job_id=job_key,
            queue_id=queue_id,
            queue_status=status_after,
            photo=photo,
            captured_at=(
                captured_at if isinstance(captured_at, str) and captured_at else to_utc_iso(self._now_fn())
            ),
            source_path=source_path,
        )

    # ------------------------------------------------------------------ #
    # 内部
    # ------------------------------------------------------------------ #

    @staticmethod
    def _require_consent(capture_consent_id: str | None, entry: Any) -> str:
        """拍摄同意：显式传入优先，否则取队列条目上绑定的同意（T-12 绑定产物）。"""
        consent = capture_consent_id or getattr(entry, "consent_id", None)
        if not isinstance(consent, str) or not consent.strip():
            raise errors.ApiError(errors.CONSENT_REQUIRED)
        return consent.strip()

    @staticmethod
    def _job_id_of(entry: Any, fallback: str = "job-unknown") -> str:
        """作业号：从队列条目派生（request_id 是幂等键，适合当作业名）；缺失用兜底值。

        说明：``job_id`` 只用于 PhotoRecord 归属，不是取图令牌；T-2/T-16 可显式传入
        真正的作业号覆盖本默认值。
        """
        request_id = getattr(entry, "request_id", None)
        if isinstance(request_id, str) and request_id.strip():
            return f"job-{request_id.strip()}"
        return fallback

    def _mark_failed(self, queue_id: str, exc: Exception) -> None:
        """失败路径：把作业落 failed；清理用的另一次失败不再覆盖第一现场。"""
        try:
            self._queue.mark_failed(queue_id, reason=f"capture_failed: {type(exc).__name__}: {exc}")
        except InvalidTransitionError:
            # 已经在终态（并发下另一条路径已落 failed/cancelled）：不重复推进。
            pass

    def _advance(self, queue_id: str, exc_reason: str | None) -> str:
        """capturing -> processing -> ready_for_pickup；推进失败同样落 failed。"""
        try:
            self._queue.mark_processing(queue_id)
            entry = self._queue.mark_ready_for_pickup(queue_id)
        except Exception as exc:  # noqa: BLE001 - 推进失败按拍摄失败处理
            self._mark_failed(queue_id, exc)
            raise CaptureFailedError(f"照片已提交但状态推进失败：{type(exc).__name__}: {exc}") from exc
        return _entry_status(entry)
