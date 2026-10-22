#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""手势识别客户端：通过 IPC 调用独立的 gesture worker 进程。

设计立场（与 infer_client 对齐）：
- **独立进程**：worker 用 cv2 + onnxruntime，不能和 spacemit_ort 同进程。
- **哨兵协议**：stdin/stdout 上的行分隔 JSON，回包带 ``@@J `` 前缀。
- **按需启动**：第一次 observe 时才起进程；stop 时结束它。
- **看门狗**：单次请求超时（默认 8s）时杀掉进程并重启；连续失败 3 次进入降级态。

环境变量（由 Booth 注入）：
  BOOTH_GESTURE_WORKER   worker.py 的路径（默认 gesture/worker.py）
  BOOTH_GESTURE_TIMEOUT  单次请求超时秒数（默认 8.0）

★★ 检测引擎（2026-10-11 更新）：默认使用 YOLOE 开放词汇分支 ★★
  handpose 在空画面会拟合出假手，且当前项目里的独立 YOLO 权重类别并不是
  HaGRID 的 OK/点赞类别，不能把它当成已验证的 HaGRID 模型。默认分支因此只
  使用现有 YOLOE 的语义框，并保留手动拍摄作为可靠回退。
  引擎选择由 BOOTH_GESTURE_ENGINE 控制：
    "handpose" 走 gesture/worker.py，MediaPipe 21 关键点
    "yolo"      走 seg_provider 的开放词汇分割（prompts 方案）
    "official_yolo" 走唯一 InferWorker 中的官方量化 YOLOv5 手势模型
  手势类型由 BOOTH_GESTURE_TYPE 控制：
    "thumbs_up" 竖大拇指
    "ok"        OK 捏合手势

★★ 人形验真（仅 handpose 路线需要）★★
  handpose 喂没有手的图会**强行拟合关键点**：K3 实测空办公室 20/20 帧都被报成
  "一只手"，捏合比低到 0.0219（比真 OK 还像 OK）。所以 handpose 路线的 observe()
  必须把人形框一起发给 worker 做验真（person_boxes_provider 注入）；yolo 路线
  没有这个问题（认不出 OK 手势就不出框），不依赖人形框。
"""
from __future__ import annotations

import json
import os
import subprocess
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

try:
    from app.auto_flow.models import ComponentHealth, GestureResult, to_utc_iso
    from app.auto_flow.ports import Clock, GesturePort
except ImportError:  # pragma: no cover
    from auto_flow.models import ComponentHealth, GestureResult, to_utc_iso  # type: ignore[no-redef]
    from auto_flow.ports import Clock, GesturePort  # type: ignore[no-redef]

__all__ = ["GestureClient"]

HERE = Path(__file__).resolve().parent
_UTC = timezone.utc
_REQUEST_TIMEOUT_S = float(os.environ.get("BOOTH_GESTURE_TIMEOUT", "8.0"))
_MAX_CONSECUTIVE_FAILURES = 3


def _now_iso() -> str:
    return to_utc_iso(datetime.now(_UTC))


class GestureClient:
    """手势识别客户端：IPC 调 worker 进程，遵守 GesturePort 契约。

    线程安全：所有方法持锁。
    """

    def __init__(
        self,
        *,
        worker_path: Path | str | None = None,
        timeout: float = _REQUEST_TIMEOUT_S,
        python: str = "python3",
        clock: Clock | None = None,
        person_boxes_provider=None,
        seg_provider=None,
        gesture_provider=None,
    ):
        self.worker_path = Path(worker_path or (HERE / "gesture" / "worker.py"))
        self.timeout = max(1.0, float(timeout))
        self.python = str(python)
        self._clock = clock
        # callable(image_path) -> list[box] | None。返回 None/[] 表示"看不到人"。
        # 由 Booth 注入（走 infer det）。缺省 None = 一律判阴性。
        self._person_boxes_provider = person_boxes_provider
        # ★ yolo 引擎：callable(image_path) -> seg 响应 dict。由 Booth 注入
        #   （走服务里唯一的 InferWorker——AI 核独占，绝不能起第二个 EP 会话）。
        self._seg_provider = seg_provider
        self._gesture_provider = gesture_provider
        self._engine = os.environ.get("BOOTH_GESTURE_ENGINE", "yolo").strip().lower()
        if self._engine not in ("yolo", "official_yolo", "handpose"):
            self._engine = "handpose"
        # 当前产品口径是 OK；thumbs_up 仅作为兼容配置保留。
        self._gesture_type = os.environ.get("BOOTH_GESTURE_TYPE", "ok").strip().lower()
        if self._gesture_type not in ("thumbs_up", "ok"):
            self._gesture_type = "thumbs_up"
        # 开放词汇判定参数
        self._ok_prompt = os.environ.get("BOOTH_GESTURE_OK_PROMPT", "ok hand gesture")
        self._ok_min_score = float(os.environ.get("BOOTH_GESTURE_OK_SCORE", "0.45"))
        self._ok_min_side = float(os.environ.get("BOOTH_GESTURE_OK_MIN_SIDE", "24"))
        self._lock = threading.RLock()
        self._proc: subprocess.Popen | None = None
        self._next_id = 0
        self._line_buf = b""
        self._consecutive_failures = 0
        self._degraded_reason: str | None = None
        self._sessions: set[str] = set()
        self._last_person_ok: bool | None = None
        self._last_seg_result: dict | None = None  # 供 booth 提取框坐标用（绘框调试）
        self._last_gesture_result: dict | None = None
        self._last_hand_result: dict | None = None
        self._log = None  # Booth 注入 log 函数便于调试

    def _person_boxes(self, image_path: str) -> list[list[float]] | None:
        """取人形框；provider 缺失或失败返回 None（调用方据此判阴性）。"""
        if self._person_boxes_provider is None:
            return None
        try:
            boxes = self._person_boxes_provider(image_path)
        except Exception:
            return None
        if not boxes:
            return None
        return boxes

    def _now(self) -> datetime:
        if self._clock:
            return self._clock.now().astimezone(_UTC)
        return datetime.now(_UTC)

    def _ensure_started(self) -> None:
        """按需启动 worker；已启动则直接返回。"""
        if self._proc is not None and self._proc.poll() is None:
            return
        # ★ stderr 必须指向 DEVNULL，绝不能用 PIPE：
        #   PIPE 的读端没有人读（主进程只读 stdout），onnxruntime / OpenCV 的
        #   warning 写满 64KB 管道缓冲后，worker 会**永久阻塞在下一次 stderr
        #   写入**上 —— 对主进程表现为"回包超时"，于是 kill → 重启 → 新 worker
        #   又写满 → 死循环。2026-10-09 现场实测：tick 线程被拖死 15 分钟，
        #   队列完全冻结（叫号/倒计时全部停摆）。
        #   worker 的诊断输出没有保留价值（模型加载失败时 boot 消息会走 stdout
        #   哨兵协议带回 error 字段），丢弃是安全的。
        self._proc = subprocess.Popen(
            [self.python, str(self.worker_path)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            cwd=str(self.worker_path.parent),
        )
        self._line_buf = b""
        # worker 启动时会先发一条 boot 消息（id=0），读掉它
        try:
            _boot = self._read_reply(want_id=0, timeout_s=self.timeout)
        except Exception:
            pass

    def _read_reply(self, want_id: int, timeout_s: float) -> dict:
        """从 worker 读一条回包（按 id 配对）；超时/EOF/进程死亡均抛异常。"""
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            if self._proc is None or self._proc.poll() is not None:
                raise RuntimeError("worker 进程已死")
            line = self._read_line(max(0.1, deadline - time.time()))
            if line == "":
                raise EOFError("worker stdout EOF")
            if line is None:
                continue
            if not line.startswith("@@J "):
                continue
            payload = json.loads(line[4:])
            if payload.get("id") == want_id:
                return payload
        raise TimeoutError("worker 回包超时（%.1fs）" % timeout_s)

    def _read_line(self, wait_s: float) -> str | None:
        """从 proc.stdout 读一行；返回 None=超时 / ""=EOF / 行内容。"""
        import selectors
        deadline = time.time() + wait_s
        sel = selectors.DefaultSelector()
        if self._proc is None or self._proc.stdout is None:
            return ""
        fd = self._proc.stdout.fileno()
        sel.register(fd, selectors.EVENT_READ)
        try:
            while time.time() < deadline:
                remaining = max(0.0, deadline - time.time())
                if remaining <= 0:
                    return None
                events = sel.select(timeout=min(0.5, remaining))
                if not events:
                    continue
                chunk = os.read(fd, 65536)
                if not chunk:
                    return ""
                self._line_buf += chunk
                if b"\n" in self._line_buf:
                    line_b, self._line_buf = self._line_buf.split(b"\n", 1)
                    return line_b.decode("utf-8", "replace")
            return None
        finally:
            sel.close()

    def _kill_and_restart(self) -> None:
        """杀掉当前进程并清零状态；下次 _ensure_started 会重启。"""
        if self._proc is not None:
            try:
                self._proc.kill()
                self._proc.wait(timeout=3.0)
            except Exception:
                pass
            self._proc = None
        self._line_buf = b""
        self._sessions.clear()

    def _send_request(self, op: str, **kwargs) -> dict:
        """发一条请求并等回包；超时/失败会杀进程、记失败数、并重新抛异常。"""
        self._next_id += 1
        req_id = self._next_id
        request = {"id": req_id, "op": op, **kwargs}
        try:
            self._ensure_started()
            if self._proc is None or self._proc.stdin is None:
                raise RuntimeError("进程未启动")
            self._proc.stdin.write((json.dumps(request) + "\n").encode("utf-8"))
            self._proc.stdin.flush()
            reply = self._read_reply(want_id=req_id, timeout_s=self.timeout)
            self._consecutive_failures = 0
            return reply
        except Exception as exc:
            self._consecutive_failures += 1
            self._kill_and_restart()
            if self._consecutive_failures >= _MAX_CONSECUTIVE_FAILURES:
                self._degraded_reason = "连续失败 %d 次：%s" % (
                    self._consecutive_failures, exc)
            raise

    # ---- GesturePort 契约 -----------------------------------------------
    def status(self) -> ComponentHealth:
        """手势组件健康：降级时 degraded，否则 ok。"""
        with self._lock:
            if self._degraded_reason:
                return ComponentHealth(
                    status="degraded",
                    checked_at=_now_iso(),
                    reason=self._degraded_reason,
                )
            return ComponentHealth(status="ok", checked_at=_now_iso())

    def _observe_yolo(self, image_path: str) -> GestureResult:
        """yolo 引擎：开放词汇分割找「ok hand gesture」框。

        判定规则（全部走环境变量可调）：
          - seg(prompts=["person", ok_prompt]) 拿候选；
          - ok 框分数 ≥ BOOTH_GESTURE_OK_SCORE（默认 0.45）；
          - ok 框短边 ≥ BOOTH_GESTURE_OK_MIN_SIDE 像素（默认 24，过滤远处噪点框）；
          - ok 框中心落在某张 person 框内（外扩 15%）—— 手要长在人身上。
        没有合格 ok 框 = 明确阴性（不是漏检：语义级模型认不出就是没有）。
        """
        if self._seg_provider is None:
            return GestureResult(
                available=False, gesture="none", stable_frames=0,
                observed_at=_now_iso(), reason="yolo 引擎未注入 seg_provider")
        try:
            result = self._seg_provider(image_path)
        except Exception as exc:
            self._consecutive_failures += 1
            if self._consecutive_failures >= _MAX_CONSECUTIVE_FAILURES:
                self._degraded_reason = "seg 连续失败 %d 次：%s" % (
                    self._consecutive_failures, exc)
            self._last_seg_result = None
            return GestureResult(
                available=False, gesture="none", stable_frames=0,
                observed_at=_now_iso(), reason="seg 调用失败：%s" % exc)
        # 缓存最新 seg 响应，供 booth._store_gesture_obs 提取框坐标用于竖屏绘框
        self._last_seg_result = result if isinstance(result, dict) else None
        if not isinstance(result, dict) or not result.get("ok"):
            return GestureResult(
                available=False, gesture="none", stable_frames=0,
                observed_at=_now_iso(),
                reason="seg 不可用：%s" % (result.get("error") if isinstance(result, dict) else result))

        persons = []
        ok_boxes = []
        for det in result.get("persons") or []:
            label = str(det.get("label") or "")
            box = det.get("box")
            score = float(det.get("score") or 0)
            if not box or len(box) < 4:
                continue
            if label == "person":
                persons.append(box)
            elif label == self._ok_prompt:
                ok_boxes.append((box, score))

        # ★ yolo 引擎的人形验真策略（2026-10-10 修订）：
        #   YOLOE 开放词汇下 "person" 语义嵌入得分天然偏低（实测 0.14~0.20），
        #   而 "ok hand gesture" 能到 0.45~0.67。强制要求 person 框存在会把
        #   大量真实 OK 帧过滤掉。新策略：
        #   - 有 person 框 → 做中心位置验证（原逻辑，防止画面角落的游离框）
        #   - 无 person 框 → 跳过位置验证，只靠分数+尺寸门槛
        #   handpose 路线的幻觉问题（在无手区域强行拟合关键点）不影响 YOLOE：
        #   YOLOE 语义级判据不会在没有手的地方出 ok hand gesture 框。

        # 挑分数最高的合格 ok 框
        best = None
        for box, score in ok_boxes:
            if score < self._ok_min_score:
                continue
            side = min(box[2] - box[0], box[3] - box[1])
            if side < self._ok_min_side:
                continue
            if best is None or score > best[1]:
                best = (box, score)

        # 有 person 框时做位置验证；无 person 框时直接放行（跳过）
        if best is not None and persons:
            box, score = best
            cx, cy = (box[0] + box[2]) / 2.0, (box[1] + box[3]) / 2.0
            inside = False
            for pb in persons:
                pw, ph = pb[2] - pb[0], pb[3] - pb[1]
                dx, dy = pw * 0.15, ph * 0.15
                if (pb[0] - dx) <= cx <= (pb[2] + dx) and (pb[1] - dy) <= cy <= (pb[3] + dy):
                    inside = True
                    break
            if not inside:
                best = None

        self._consecutive_failures = 0
        if best is not None:
            _, score = best
            log = getattr(self, "_log", None) or print
            log("yolo 手势 OK：score=%.3f（阈值 %.2f）person_boxes=%d" % (
                score, self._ok_min_score, len(persons)))
            return GestureResult(
                available=True, gesture="ok", stable_frames=0,
                confidence=round(best[1], 3), person_count=len(persons),
                in_frame=True, observed_at=_now_iso(), reason=None)
        # 调试输出：有候选框但没达标，打出最高分便于调阈值
        if ok_boxes:
            log = getattr(self, "_log", None) or print
            top_score = max(s for _, s in ok_boxes)
            log("yolo 手势候选未达标：最高 %.3f（阈值 %.2f），候选数 %d，person_boxes=%d" % (
                top_score, self._ok_min_score, len(ok_boxes), len(persons)))
        reason = "未识别到 OK 手势" if ok_boxes or persons else "画面里没有人形或手势"
        return GestureResult(
            available=True, gesture="none", stable_frames=0,
            person_count=len(persons),
            in_frame=len(ok_boxes) > 0,
            observed_at=_now_iso(), reason=reason)

    def _observe_official_yolo(self, image_path: str, *, visual: bool = False) -> GestureResult:
        if self._gesture_provider is None:
            return GestureResult(
                available=False, gesture="none", stable_frames=0,
                observed_at=_now_iso(), reason="官方手势推理未注入")
        try:
            result = self._gesture_provider(image_path)
        except Exception as exc:
            self._last_gesture_result = None
            return GestureResult(
                available=False, gesture="none", stable_frames=0,
                observed_at=_now_iso(), reason="官方手势推理失败：%s" % exc)
        self._last_gesture_result = result if isinstance(result, dict) else None
        if not isinstance(result, dict) or not result.get("ok"):
            return GestureResult(
                available=False, gesture="none", stable_frames=0,
                observed_at=_now_iso(), reason=(
                    result.get("error") if isinstance(result, dict) else "无有效响应"))

        min_score = float(os.environ.get("BOOTH_GESTURE_OK_SCORE", "0.45"))
        persons = [] if visual else (self._person_boxes(image_path) or [])
        ok_boxes = []
        for detection in result.get("detections") or []:
            if str(detection.get("label") or "").strip().lower() != "ok":
                continue
            box = detection.get("box")
            score = float(detection.get("score") or 0.0)
            if (isinstance(box, (list, tuple)) and len(box) >= 4
                    and all(isinstance(value, (int, float)) for value in box[:4])
                    and box[2] > box[0] and box[3] > box[1]):
                if not visual:
                    if len(persons) != 1:
                        continue
                    person = persons[0]
                    center_x, center_y = (box[0] + box[2]) / 2, (box[1] + box[3]) / 2
                    pad_x = (person[2] - person[0]) * 0.15
                    pad_y = (person[3] - person[1]) * 0.15
                    if not (person[0] - pad_x <= center_x <= person[2] + pad_x
                            and person[1] - pad_y <= center_y <= person[3] + pad_y):
                        continue
                ok_boxes.append((box, score))
        best = max(ok_boxes, key=lambda item: item[1], default=None)
        if best is not None and best[1] >= min_score:
            return GestureResult(
                available=True, gesture="ok", stable_frames=0,
                confidence=round(best[1], 4), person_count=len(persons), in_frame=True,
                observed_at=_now_iso())
        return GestureResult(
            available=True, gesture="none", stable_frames=0,
            confidence=round(best[1], 4) if best is not None else None,
            person_count=len(persons), in_frame=False, observed_at=_now_iso(),
            reason="未达到 OK 置信度门槛")

    def observe(self, image_path: str) -> GestureResult:
        """观测手势；降级时返回 available=False。

        引擎由 BOOTH_GESTURE_ENGINE 决定（默认 yolo，见模块头）。
        handpose 路线：人形框取不到就**直接判阴性**——handpose 在没有手的画面上
        会强行拟合关键点（见模块头 2026-10-09 记录）。
        """
        with self._lock:
            if self._degraded_reason:
                return GestureResult(
                    available=False,
                    gesture="none",
                    stable_frames=0,
                    observed_at=_now_iso(),
                    reason=self._degraded_reason,
                )
            if self._engine == "yolo":
                return self._observe_yolo(image_path)
            if self._engine == "official_yolo":
                return self._observe_official_yolo(image_path)
            person_boxes = self._person_boxes(image_path)
            if not person_boxes:
                self._last_person_ok = False
                return GestureResult(
                    available=False,
                    gesture="none",
                    stable_frames=0,
                    person_count=0,
                    in_frame=False,
                    observed_at=_now_iso(),
                    reason="画面里没有人形（或人形检测不可用）：手势一律判阴性",
                )
            try:
                reply = self._send_request(
                    "ok", image=image_path, person_boxes=person_boxes, gesture=self._gesture_type)
                self._last_hand_result = reply
                self._last_person_ok = bool(reply.get("person_ok"))
                hand_count = reply.get("hand_count")
                return GestureResult(
                    available=True,
                    gesture=reply.get("gesture", "none"),
                    stable_frames=0,
                    confidence=reply.get("confidence"),
                    person_count=len(person_boxes),
                    # hand_count=0 = 关键点模型这一帧没找到手（偶发漏检，
                    # 与"画面里没人"是两回事 —— 上游 person_boxes 有货说明
                    # 人是在的）。闸门靠它区分"漏检帧"和"明确阴性"。
                    in_frame=bool(reply.get("person_ok")) and (hand_count != 0),
                    observed_at=_now_iso(),
                    reason=reply.get("error"),
                )
            except Exception as exc:
                self._consecutive_failures += 1
                if self._consecutive_failures >= _MAX_CONSECUTIVE_FAILURES:
                    self._degraded_reason = f"连续失败 {self._consecutive_failures} 次"
                return GestureResult(
                    available=False,
                    gesture="none",
                    stable_frames=0,
                    observed_at=_now_iso(),
                    reason="observe 失败：%s" % exc,
                )

    def observe_visual(self, image_path: str) -> GestureResult:
        """只输出手框和关键点，不参与自动拍摄资格判定。"""
        with self._lock:
            if self._engine == "yolo":
                return self._observe_yolo(image_path)
            if self._engine == "official_yolo":
                return self._observe_official_yolo(image_path, visual=True)
            try:
                reply = self._send_request(
                    "ok", image=image_path, person_boxes=[], gesture=self._gesture_type)
                self._last_hand_result = reply
                hand_found = int(reply.get("hand_count") or 0) > 0
                return GestureResult(
                    available=hand_found,
                    gesture=reply.get("raw_gesture", "none"),
                    stable_frames=0,
                    confidence=reply.get("confidence"),
                    person_count=0,
                    in_frame=hand_found,
                    observed_at=_now_iso(),
                    reason=reply.get("error"),
                )
            except Exception as exc:
                self._last_hand_result = None
                return GestureResult(
                    available=False,
                    gesture="none",
                    stable_frames=0,
                    observed_at=_now_iso(),
                    reason="visual observe 失败：%s" % exc,
                )

    def stop(self, session_id: str) -> None:
        """结束观察会话；当前无会话时杀掉 worker。"""
        with self._lock:
            self._sessions.discard(session_id)
            if not self._sessions:
                self._kill_and_restart()
