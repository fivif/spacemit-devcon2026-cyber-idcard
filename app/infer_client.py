"""推理 worker 客户端（跑在抓帧/渲染进程里）。

协议：行式 JSON，响应行带哨兵前缀 @@J（见 02-spec §1.1 / §5）。
哨兵不是洁癖：EP 拿不到 AI 核时会往 stdout 打 "EP Error ..." 噪声，
实测一次冷启动能打出 48 行 —— 不设前缀就会把协议打碎。
"""

from __future__ import annotations

import json
import os
import selectors
import subprocess
import sys
import threading
import time
from pathlib import Path
PROTOCOL_PREFIX = "@@J "
HERE = Path(__file__).resolve().parent

# 单次等待可读的上限（秒）。真正的总超时由 call(timeout=...) 控制，
# 这个只是让循环有机会回去比较 deadline。
_READ_POLL_S = 0.5


def _decode_line(raw: bytes) -> str | None:
    """字节行 → str；解不出 UTF-8 返回 None（EP 噪声里有非 UTF-8 字节）。"""
    try:
        return raw.decode("utf-8", errors="strict").rstrip("\r\n")
    except UnicodeDecodeError:
        return None


class InferError(RuntimeError):
    pass


class InferWorker:
    """常驻推理子进程；崩溃自动重启（退避 1/2/5s，最多 5 次）。"""

    def __init__(self, script: str | Path | None = None, python: str | None = None,
                 start_timeout: float = 120.0):
        self.script = str(script or HERE / "infer" / "worker.py")
        self.python = python or os.environ.get("BOOTH_PYTHON") or sys.executable
        self.start_timeout = start_timeout
        self._proc: subprocess.Popen | None = None
        self._lock = threading.RLock()
        self._busy = threading.Event()   # set while a call is in flight; callers may check before blocking
        self._seq = 0
        self._restarts = 0
        self._noise_lines = 0
        self._last_error: str | None = None
        self._started_at = 0.0
        self._line_buf = b""      # _read_line 的跨次残留（没凑够一行的尾巴）
        # K3 的 EP 会话必须随进程释放，不能只删除 ORT 对象。
        self._mode = "seg"

    # ---- 生命周期 ------------------------------------------------------
    @property
    def running(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def start(self) -> None:
        with self._lock:
            if self.running:
                return
            # 二进制模式（不是 text=True）：文本模式下 readline() 会在 Python
            # 自己的缓冲区里预读，selectors 看到的「可读」与「有整行」不是一回事，
            # 带超时的读会误判。协议是行式 UTF-8，这里自己解码。
            # stderr 用 PIPE 是安全的：worker 死亡时下面会 read() 清空它；
            # 活着时 EP 噪声走 stdout（被当噪声行丢弃），不写 stderr。
            self._proc = subprocess.Popen(
                [self.python, self.script],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                cwd=str(HERE),
                env={**os.environ, "BOOTH_INFER_MODE": self._mode},
            )
            self._started_at = time.time()

    def _restart(self) -> None:
        with self._lock:
            self._restarts += 1
            if self._restarts > 5:
                raise InferError(f"推理 worker 连续重启超过 5 次：{self._last_error}")
            delay = {1: 1.0, 2: 2.0}.get(self._restarts, 5.0)
            try:
                if self._proc is not None:
                    self._proc.kill()
            except Exception:
                pass
            self._line_buf = b""     # 换进程了：丢掉上一个 worker 的半行残留
            self._proc = None
            time.sleep(delay)
            self.start()

    def stop(self) -> None:
        with self._lock:
            if self._proc is not None:
                try:
                    self._proc.stdin and self._proc.stdin.close()
                    self._proc.terminate()
                    self._proc.wait(timeout=5)
                except Exception:
                    try:
                        self._proc.kill()
                    except Exception:
                        pass
                self._proc = None

    def _switch_mode_process(self, mode: str) -> None:
        """销毁当前 worker，再以目标模式启动下一阶段。"""
        mode = str(mode).strip().lower()
        if mode not in ("seg", "gesture"):
            raise ValueError("未知推理模式：%s" % mode)
        if mode == self._mode and (self._proc is None or self.running):
            return
        if self._proc is not None:
            try:
                self._proc.stdin and self._proc.stdin.close()
                self._proc.terminate()
                self._proc.wait(timeout=3)
            except Exception:
                try:
                    self._proc.kill()
                except Exception:
                    pass
        self._proc = None
        self._line_buf = b""
        self._mode = mode

    # ---- 调用 ----------------------------------------------------------
    def call(self, op: str, timeout: float = 60.0, on_progress=None, **payload) -> dict:
        with self._lock:
            self._busy.set()
            try:
                desired_mode = {
                    "gesture": "gesture",
                    "seg": "seg",
                    "matte": "seg",
                    "det": "seg",
                }.get(op)
                if desired_mode is not None:
                    self._switch_mode_process(desired_mode)
                self.start()
                self._seq += 1
                request = {"id": self._seq, "op": op, **payload}
                try:
                    self._proc.stdin.write((json.dumps(request) + "\n").encode("utf-8"))
                    self._proc.stdin.flush()
                except Exception as exc:
                    self._last_error = f"写入失败：{exc}"
                    self._restart()
                    raise InferError(self._last_error) from exc

                deadline = time.time() + timeout
                while True:
                    remaining = deadline - time.time()
                    if remaining <= 0:
                        self._last_error = f"{op} 超时（{timeout}s）"
                        self._kill_wedged()
                        raise InferError(self._last_error)
                    # 必须带读超时：worker 是单线程逐个处理请求，某个 op 卡在
                    # ONNX Runtime 里就再也不回包。裸 readline 会永久阻塞，于是
                    # deadline 永远轮不到比较 —— 实测卡过 900 秒，拍摄线程占着
                    # 拍摄位不放，后面排队的人全部叫不到号。
                    try:
                        line = self._read_line(min(remaining, _READ_POLL_S))
                    except Exception as exc:
                        self._last_error = f"读取失败：{exc}"
                        self._restart()
                        raise InferError(self._last_error) from exc
                    if line is None:
                        continue  # 这一轮没读到，回去看 deadline
                    if line == "":
                        if self._proc.poll() is None:
                            continue  # EOF 但进程还在：等一下再看
                        tail = ""
                        try:
                            raw_tail = self._proc.stderr.read()
                            if raw_tail:
                                # 二进制模式下 stderr 给的是 bytes
                                tail = (raw_tail.decode("utf-8", "replace")
                                        if isinstance(raw_tail, bytes) else str(raw_tail))[-400:]
                        except Exception:
                            pass
                        self._last_error = f"worker 退出：{tail.strip()}"
                        self._restart()
                        raise InferError(self._last_error)
                    # _read_line 已经解好码了（返回 str），不要再 decode 一次
                    if not line.startswith(PROTOCOL_PREFIX):
                        self._noise_lines += 1   # EP 噪声，丢弃
                        continue
                    response = json.loads(line[len(PROTOCOL_PREFIX):])
                    if response.get("event") == "segmentation":
                        if response.get("id") == request["id"] and on_progress is not None:
                            try:
                                on_progress(response)
                            except Exception as exc:
                                self._last_error = "进度显示失败：%s" % type(exc).__name__
                        continue
                    if not response.get("ok", False) and response.get("error"):
                        self._last_error = response["error"]
                    return response
            finally:
                self._busy.clear()

    def _read_line(self, wait_s: float) -> str | None:
        """最多等 wait_s 秒读一行（协议是行式 UTF-8）。

        为什么不用 proc.stdout.readline()：那是 BufferedReader，它会在 Python
        层预读 8KB。于是 fd 上「无数据可读」时缓冲区里其实已经躺着整行，
        selectors 会一直等一个永远不会来的事件 —— 越是响应快、一次吐多行的
        场景越容易踩。所以这里直接在 fd 上读字节、自己按换行拼行。

        返回：读到一行 → 该行（不含换行）；等到超时 → None；EOF 且无数据 → ""。
        """
        proc = self._proc
        if proc is None or proc.stdout is None:
            return ""

        # 缓冲区里已有整行就直接给，不必等 fd
        buf = self._line_buf
        nl = buf.find(b"\n")
        if nl >= 0:
            self._line_buf = buf[nl + 1:]
            return _decode_line(buf[:nl + 1])

        fd = proc.stdout.fileno()
        try:
            sel = selectors.DefaultSelector()
        except Exception:
            return None
        try:
            sel.register(fd, selectors.EVENT_READ)
            if not sel.select(wait_s):
                return None
        except (ValueError, OSError):
            return ""          # fd 已关闭：当 EOF
        finally:
            try:
                sel.close()
            except Exception:
                pass

        try:
            chunk = os.read(fd, 65536)
        except (OSError, ValueError):
            return ""
        if not chunk:
            return ""
        buf = self._line_buf + chunk
        nl = buf.find(b"\n")
        if nl < 0:
            self._line_buf = buf      # 还没凑够一行，留着等下一次
            return None
        self._line_buf = buf[nl + 1:]
        return _decode_line(buf[:nl + 1])

    def _kill_wedged(self) -> None:
        """超时后杀掉卡住的 worker 并重启。

        不能只 kill 不重启：下一次 call 会走到 start()，而 _proc 仍指向那个
        已死的进程、poll() 也还没刷新，于是 self.running 判 true，新请求写进
        一个没人读的管道 —— 表现就是「每次超时后所有请求都超时」。
        """
        try:
            if self._proc is not None:
                self._proc.kill()
                self._proc.wait(timeout=5)
        except Exception:
            pass
        self._proc = None
        self._restart()

    # ---- 便利封装 ------------------------------------------------------
    def health(self, timeout: float = 180.0) -> dict:
        return self.call("health", timeout=timeout)

    def seg(self, image: str, prompts: list[str] | None = None, conf: float = 0.20,
            mask: bool = True, mask_max_side: int = 640, matte: bool = True,
            matte_max_side: int = 0, timeout: float = 90.0, on_progress=None) -> dict:
        """mask_max_side 管二值掩码（数人 / 画框），matte_max_side 管抠像 alpha。

        matte=False 完全跳过 MODNet 抠像（手势探测用，节省 ~1300ms）。
        alpha 默认 0 = **不缩**：它决定成片上人的边，缩一次再放大等于白抠。
        """
        return self.call("seg", timeout=timeout, on_progress=on_progress,
                         progress=on_progress is not None, image=image, prompts=prompts or ["person"],
                         conf=conf, mask=mask, mask_max_side=mask_max_side,
                         matte=matte, matte_max_side=matte_max_side)

    def matte(self, image: str, mask_max_side: int = 640, matte_max_side: int = 0,
              timeout: float = 90.0) -> dict:
        return self.call("matte", timeout=timeout, image=image, mask_max_side=mask_max_side,
                         matte_max_side=matte_max_side)

    def det(self, image: str, conf: float = 0.25, timeout: float = 60.0) -> dict:
        return self.call("det", timeout=timeout, image=image, conf=conf)

    def gesture(self, image: str, conf: float = 0.45, iou: float = 0.45,
                timeout: float = 30.0) -> dict:
        return self.call("gesture", timeout=timeout, image=image,
                         conf=conf, iou=iou)

    def switch_mode(self, mode: str, timeout: float = 30.0) -> dict:
        """切换 seg / gesture 独占会话，释放上一阶段占用的 AI 核。"""
        with self._lock:
            self._switch_mode_process(mode)
            return self.call("health", timeout=timeout)

    def stats(self) -> dict:
        return {"running": self.running, "restarts": self._restarts,
                "noise_lines_dropped": self._noise_lines, "last_error": self._last_error,
                "uptime_s": round(time.time() - self._started_at, 1) if self._started_at else 0,
                "busy": self._busy.is_set()}
