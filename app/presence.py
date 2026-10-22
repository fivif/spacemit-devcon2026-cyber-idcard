# -*- coding: utf-8 -*-
"""低频人形在场检测：给大屏「有人靠近自动唤醒」用的粗判据。

为什么这样做：
- AI 核槽位紧张（8 槽，seg=4 / det=2 已占 6），不可能再为一个「有没有人」
  加载新模型。检测模型（yolov8n）本来就随 worker 加载并预热（见
  booth_server.Booth.preload），det 单帧实测 ~25ms。复用它，每 ~1.2s 对
  **预览帧**跑一次检测，就是零新增槽位、零新模型的在场判据。
- 输入取 PreviewHub 的最新预览 JPEG（单生产者分发的那一份），**不**直接读
  相机 —— 相机只有预览线程一个读者，这是 camera.PreviewHub 存在的理由。

纪律（与 booth_server 同一套）：
- 本模块**不 import cv2**（booth_server 进程纪律，cv2 只属于 camera.py）：
  JPEG 解码用 PIL，缩放用 PIL 自带方法。
- 检测走既有 infer worker 子进程：cv2 / spacemit_ort 的进程隔离不受影响。
- 拿不到检测（worker 未就绪、模型缺失、开发机没有 onnxruntime）时如实报
  unknown，**绝不**把「测不了」当成「没人」——大屏拿到 unknown 就维持当前
  画面，不做状态切换。
- 判据是「画面里有人形，而且够大」：box 高度不足画面 min_box_h 的路人不算
  在场 —— 大屏要的是「有人走到拍摄位附近」，不是「画面里有个远处的人影」。

平滑：单帧检测会抖（遮挡、低头），所以连续 2 次阳性才算「在场」、
连续 3 次阴性才算「离开」。连续出错按指数退避拉长探测间隔（封顶 30s），
避免开发机上每秒拉起一个必死的 worker。
"""

from __future__ import annotations

import io
import os
import tempfile
import threading
import time

__all__ = ["PresenceSensor"]


class PresenceSensor:
    """每 interval 秒用检测模型看一眼预览帧，输出平滑后的「在场」状态。"""

    def __init__(self, infer, frames, data_dir, interval: float = 1.2,
                 min_box_h: float | None = None, conf: float = 0.35):
        self.infer = infer
        # frames: callable -> 最新预览 JPEG 字节（没有则 None）。由 Booth 注入
        # preview_hub.latest_jpeg，保持「相机单读者」纪律。
        self._frames = frames
        self.interval = max(0.5, float(interval))
        # 框高门槛默认取 subject_rules 的 min_box_h —— 与"谁算主体"共用同一个
        # 现场标定值。以前这里写死 0.22、规则里又有一份，两处会各自漂移：
        # 调了规则里的值，在场判定却还是老门槛，大屏唤醒跟拍摄判定对不上。
        self.min_box_h = float(self._min_box_from_rules() if min_box_h is None
                               else min_box_h)
        self.conf = float(conf)
        # 帧中转文件：worker 的 det 协议收的是图片路径。固定一个文件反复覆写，
        # 而不是每帧一个新文件 —— 现场跑一整天，谁也不能给 data/ 留一地垃圾。
        self._frame_path = os.path.join(str(data_dir), "presence-frame.jpg")
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._pos = 0          # 连续阳性计数
        self._neg = 0          # 连续阴性计数
        self._errors = 0       # 连续出错计数（退避用）
        self._present: bool | None = None
        self._person_count = 0
        self._checked_at = 0.0
        self._error: str | None = None

    # ---- 生命周期 ------------------------------------------------------
    @staticmethod
    def _min_box_from_rules() -> float:
        """从 subject_rules 取框高门槛；拿不到就退回 0.22（与历史默认一致）。"""
        try:
            import sys
            from pathlib import Path
            here = Path(__file__).resolve().parent
            if str(here) not in sys.path:
                sys.path.insert(0, str(here))
            from subject_rules import rules
            return float(rules()["min_box_h"])
        except Exception:
            return 0.22

    def start(self) -> None:
        if os.environ.get("BOOTH_PRESENCE", "1").strip().lower() in ("0", "false", "no"):
            return
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop.clear()
            self._thread = threading.Thread(target=self._loop, name="presence", daemon=True)
            self._thread.start()

    def _loop(self) -> None:
        while not self._stop.is_set():
            started = time.time()
            try:
                self._probe_once()
            except Exception as exc:  # 探测绝不许把线程打死
                with self._lock:
                    self._errors += 1
                    self._error = "%s: %s" % (type(exc).__name__, str(exc)[:120])
            with self._lock:
                errors = self._errors
            # 出错退避：5s 起步、按连续错误翻倍、封顶 30s；正常就按固定间隔。
            if errors:
                sleep_s = min(30.0, 5.0 * (2 ** min(errors - 1, 3)))
            else:
                sleep_s = self.interval
            remaining = sleep_s - (time.time() - started)
            if remaining > 0:
                self._stop.wait(remaining)

    # ---- 单次探测 ------------------------------------------------------
    def _probe_once(self) -> None:
        jpeg = self._frames()
        if not jpeg:
            # 预览还没出帧（相机没起 / 刚掉线）：不算阴性，也不算阳性。
            with self._lock:
                self._checked_at = time.time()
                self._error = None
            return
        # 缩到 480 宽再落盘：seg 输入会 letterbox，原图 1080p 白占带宽和盘。
        # 只用 PIL，不碰 cv2（booth_server 进程纪律）。
        from PIL import Image

        with Image.open(io.BytesIO(jpeg)) as image:
            rgb = image.convert("RGB")
            scale = min(1.0, 480.0 / max(1, rgb.width))
            if scale < 1.0:
                rgb = rgb.resize((max(1, round(rgb.width * scale)),
                                  max(1, round(rgb.height * scale))))
            rgb.save(self._frame_path, "JPEG", quality=80)

        # 改用 seg 的 person 检测（det 在 K3 上超时）
        result = self.infer.seg(
            self._frame_path, prompts=["person"], conf=self.conf,
            mask=False, mask_max_side=64, matte=False, matte_max_side=0,
            timeout=8.0)
        if not result.get("ok"):
            # worker 没就绪 / 模型缺失：这是「测不了」，不是「没人」。
            # 按错误计（触发指数退避），绝不能把失败当成一次阴性观测。
            raise RuntimeError(str(result.get("error") or "seg 不可用")[:120])
        height = int(result.get("height") or 0)
        close_persons = 0
        for det in result.get("persons") or []:
            if det.get("label") != "person":
                continue
            box = det.get("box") or []
            if len(box) < 4:
                continue
            if (box[3] - box[1]) >= self.min_box_h * max(1, height):
                close_persons += 1
        with self._lock:
            self._errors = 0
            self._error = None
            self._checked_at = time.time()
            self._person_count = close_persons
            if close_persons > 0:
                self._pos += 1
                self._neg = 0
                if self._pos >= 2:
                    self._present = True
            else:
                self._neg += 1
                self._pos = 0
                if self._neg >= 3:
                    self._present = False

    # ---- 读数 ----------------------------------------------------------
    def snapshot(self) -> dict:
        """给 /api/state 用。present 三态：True 在场 / False 没人 / None 测不了。

        age_s 是这份读数多久没更新了：超过 10s（worker 卡死、线程死了）前端
        也按 unknown 处理，宁可保持现状也不切错。

        额外给 ``consecutive_negatives``（连续阴性次数）：倒计时那边要靠它
        区分「刚到场、还没测出来」与「真的没人」。只看 present 的话，一个刚站到
        镜头前的人在后端眼里和空椅子一模一样 —— 真人会被当场取消拍摄。
        """
        with self._lock:
            age = (time.time() - self._checked_at) if self._checked_at else None
            return {
                "present": self._present,
                "person_count": self._person_count,
                "consecutive_negatives": self._neg,
                "age_s": round(age, 1) if age is not None else None,
                "error": self._error,
                "interval_s": self.interval,
            }
