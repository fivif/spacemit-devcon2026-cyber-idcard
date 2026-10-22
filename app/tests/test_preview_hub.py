# -*- coding: utf-8 -*-
"""预览分发（PreviewHub）与「断流自愈」测试 —— 不需要真相机。

对应现场踩过的两个坑（knowledge/04）：
  * 坑 10：大屏 + 拍摄位两个页面各拉一路 MJPEG，各自读同一台相机 →
    帧被轮流吃掉，两边都卡；
  * 坑 11：相机 USB 掉线重枚举后旧句柄作废，read() 恒 False，
    流停在最后一帧再也不动，只能人工重启服务。

抓帧器用假的（FakeCamera），验证的是**分发与自愈逻辑本身**，
所以本文件在任何机器上都能跑：python3 app/tests/test_preview_hub.py
"""

from __future__ import annotations

import sys
import os
import threading
import time
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # pragma: no cover
    pass

HERE = Path(__file__).resolve().parent
APP = HERE.parent
sys.path.insert(0, str(APP))
os.environ.setdefault("BOOTH_MEDIA_PUBLISH_ENABLED", "0")

import camera  # noqa: E402

import numpy as np  # noqa: E402

try:  # 编码一帧 JPEG 需要 cv2（K3 自带 4.10.0）
    import cv2  # noqa: F401

    HAS_CV2 = True
except Exception:  # noqa: BLE001
    HAS_CV2 = False
    # 没有 cv2 就用一个假编码器顶替。这一份测的是**分发与自愈逻辑**，
    # 不是 JPEG 编码本身（编码在 test_camera.py 里拿真 cv2 验过）。
    camera.encode_jpeg = lambda frame, quality=88: b"\xff\xd8" + bytes([int(quality) & 0xFF]) * 64

CHECKS = 0
FAILURES: list[str] = []
SKIPS: list[str] = []


def check(cond, name: str, detail: str = "") -> bool:
    global CHECKS
    CHECKS += 1
    ok = bool(cond)
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" | {detail}" if detail else ""))
    if not ok:
        FAILURES.append(name)
    return ok


def skip(name: str, detail: str = "") -> None:
    SKIPS.append(name)
    print(f"  [SKIP] {name}" + (f" | {detail}" if detail else ""))


def info(name: str, detail: str = "") -> None:
    print(f"  [info] {name}" + (f" | {detail}" if detail else ""))


class FakeCamera:
    """假抓帧器：数得清被读了多少次，也能随时「掉线」。"""

    def __init__(self, size=(64, 48)):
        self.size = size
        self.reads = 0
        self.fail = False
        self._lock = threading.Lock()

    def health(self) -> dict:
        """镜像真实 Camera 的健康读数接口（PreviewHub.stats 会带上它）。"""
        return {"device": "fake", "open": True, "fail_streak": int(self.fail),
                "reopens": 0, "last_ok_s": 0.1, "snapshot_note": None}

    def read_bgr(self, warm: int = 0):
        with self._lock:
            self.reads += 1
            failing = self.fail
        if failing:
            # 模拟「USB 重枚举后句柄作废」：read 一直失败
            raise camera.CameraError("读到空帧（假相机）")
        return np.zeros((self.size[1], self.size[0], 3), np.uint8)


def pump(gen, stop, sink, limit=10 ** 9):
    try:
        for jpeg in gen:
            sink.append(jpeg)
            if len(sink) >= limit:
                break
    except Exception:  # noqa: BLE001
        pass
    finally:
        stop.set()


def collect(hub, stop, out, limit=10 ** 9, keepalive=0.3):
    thread = threading.Thread(target=pump, args=(hub.frames(stop, keepalive=keepalive), stop, out, limit),
                              daemon=True)
    thread.start()
    return thread


def main() -> int:
    print("=== 预览分发（PreviewHub） ===")
    info("JPEG 编码器", "cv2" if HAS_CV2 else "假编码器（本机没有 cv2，逻辑用例照样跑）")

    # ---------------------------------------------------------------- a) 单生产者
    fake = FakeCamera()
    hub = camera.PreviewHub(fake, fps=20, quality=70)
    hub.set_gate(True)
    stops = [threading.Event() for _ in range(3)]
    sinks: list[list] = [[], [], []]
    firsts: list[float] = []
    threads = []
    t0 = time.time()
    for i in range(3):
        threads.append(collect(hub, stops[i], sinks[i]))
        firsts.append(time.time() - t0)
    time.sleep(2.0)
    reads_during = fake.reads
    st = hub.stats()
    for s in stops:
        s.set()
    for t in threads:
        t.join(timeout=2.0)

    info("3 个订阅者时的相机读取次数", "%d 次（20fps × 2s ≈ 40）" % reads_during)
    check(reads_during <= 70,
          "a) 相机只有一条线程在读（3 个页面 ≠ 3 倍读取）",
          "reads=%d，若每个连接各读一份会是 ~120" % reads_during)
    check(reads_during >= 15, "a) 抓帧线程确实在跑", "reads=%d" % reads_during)
    check(all(len(s) > 5 for s in sinks), "a) 3 个订阅者都收到了帧",
          "帧数=%s" % [len(s) for s in sinks])
    check(st["clients"] == 3, "a) stats 里 clients 如实统计", "clients=%s" % st["clients"])
    check(all(isinstance(f, (bytes, bytearray)) and bytes(f[:2]) == b"\xff\xd8" for f in sinks[0]),
          "a) 发出去的是真 JPEG（魔数 FFD8）")
    later = hub.stats()
    check(later["clients"] == 0, "a) 订阅者退出后 clients 归零", "clients=%s" % later["clients"])
    check(set(("seq", "fps", "quality", "running", "last_error", "last_frame_s", "camera"))
          <= set(later), "a) stats 字段齐全（现场排断流就看这几个）", sorted(later))
    probe = hub.latest_probe_jpeg(max_width=480, quality=80)
    check(probe is not None and probe[1] == fake.size,
          "a) 检测小图可直接从最近原始帧生成", str(probe[1] if probe else None))

    if HAS_CV2:
        large_fake = FakeCamera(size=(960, 540))
        large_hub = camera.PreviewHub(
            large_fake, fps=10, quality=70, output_size=(480, 270))
        large_hub.set_gate(True)
        large_hub.start()
        deadline = time.time() + 2.0
        large_probe = None
        while time.time() < deadline and large_probe is None:
            large_probe = large_hub.latest_probe_jpeg(max_width=480, quality=80)
            time.sleep(0.02)
        check(large_probe is not None and large_probe[1] == (480, 270),
              "a) 检测小图按宽度等比缩放", str(large_probe[1] if large_probe else None))
        stream_image = cv2.imdecode(
            np.frombuffer(large_hub.latest_jpeg(), np.uint8), cv2.IMREAD_COLOR)
        capture_image = cv2.imdecode(
            np.frombuffer(large_hub.latest_capture_jpeg(), np.uint8), cv2.IMREAD_COLOR)
        check((stream_image.shape[1], stream_image.shape[0]) == (480, 270),
              "a) 网页预览按输出尺寸下发", str(stream_image.shape[:2]))
        check((capture_image.shape[1], capture_image.shape[0]) == (960, 540),
              "a) 快门锁定未缩小的同一源帧", str(capture_image.shape[:2]))
    else:
        skip("a) 检测小图按宽度等比缩放", "本机无 cv2")

    # ---------------------------------------------------------------- b) 新连接立刻见到画面
    lat = []
    for _ in range(3):
        out: list = []
        stop = threading.Event()
        started = time.time()
        thread = collect(hub, stop, out, limit=1)
        thread.join(timeout=3.0)
        lat.append(time.time() - started)
        stop.set()
    info("新连接拿到第一帧的耗时", " / ".join("%.0f ms" % (v * 1000) for v in lat))
    check(max(lat) < 0.6, "b) 新连接立刻拿到最近一帧（不用等下一个周期）",
          "最慢 %.0f ms" % (max(lat) * 1000))

    # ---------------------------------------------------------------- c) 掉线 → 自愈
    seq_before = hub.stats()["seq"]
    fake.fail = True
    time.sleep(0.7)
    st_down = hub.stats()
    check(st_down["last_error"] is not None,
          "c) 相机掉线时如实记下错误（不是静默黑屏）", str(st_down["last_error"])[:60])
    check(st_down["running"], "c) 掉线时抓帧线程还活着（没有被打死）")
    time.sleep(0.4)
    frozen = hub.stats()["seq"]
    check(frozen <= st_down["seq"] + 1, "c) 掉线期间不再产出新帧",
          "seq %d -> %d" % (st_down["seq"], frozen))

    fake.fail = False
    deadline = time.time() + 3.0
    while time.time() < deadline and hub.stats()["seq"] <= frozen:
        time.sleep(0.05)
    st_up = hub.stats()
    check(st_up["seq"] > frozen, "c) 相机恢复后流自己接回来（自愈）",
          "seq %d -> %d" % (frozen, st_up["seq"]))
    check(st_up["last_error"] is None, "c) 恢复后错误被清掉")

    check(hub.stats()["seq"] > seq_before, "c) 全程 seq 单调递增（没有回退/重复发号）")
    if hasattr(hub, "stop"):
        hub.stop()

    print("\n%d checks, %d fail, %d skip" % (CHECKS, len(FAILURES), len(SKIPS)))
    if FAILURES:
        print("FAIL:", "; ".join(FAILURES))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
