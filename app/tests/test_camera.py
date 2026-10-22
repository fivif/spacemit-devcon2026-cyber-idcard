# -*- coding: utf-8 -*-
"""T-2 UVC 抓帧测试：by-id 解析 / 双分辨率 / 明确报错 / mock 降级。

对应 系统规格 v2（内部文档） 里 T-2 的四条 acceptance：
  a) 不硬编码设备号，按 by-id 找到采集节点（camera.resolve_device）；
  b) 预览与拍照两种分辨率（PREVIEW_SIZE / PHOTO_SIZE）都能抓到非空 JPEG；
  c) 设备不存在时 open() 不崩溃、返回明确错误（BOOTH_CAMERA_DEVICE=/dev/video99）；
  d) 无相机时可切 mock 源（BOOTH_CAMERA=mock）。

实测设备：DICOTA 4K（usb 1bcf:28c4）。**节点号会漂**：USB 重新枚举一次就换号
（现场见过 video1 → video2），所以「按 by-id 找」不是洁癖。
/dev/video0 是 Linlon 编解码 M2M（不是相机），`*-video-index1` 是 metadata 节点。

本测试进程会 import cv2（那是 camera.py 的唯一职责），但**绝不** import spacemit_ort，
不违反 spec §1.1 的进程隔离。

运行：``python3 app/tests/test_camera.py``
"""

from __future__ import annotations

import io
import os
import sys
import threading
import time
from contextlib import contextmanager
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # pragma: no cover
    pass

HERE = Path(__file__).resolve().parent
APP = HERE.parent
sys.path.insert(0, str(APP))

import camera  # noqa: E402

try:  # camera.py 的执行依赖（K3 自带 cv2 4.10.0）；本测试进程不 import spacemit_ort
    import cv2  # noqa: F401

    HAS_CV2 = True
except Exception:  # noqa: BLE001
    HAS_CV2 = False

CHECKS = 0
FAILURES: list[str] = []
SKIPS: list[str] = []


def camera_holders() -> str:
    """谁正开着摄像头。UVC 是独占设备，被占住时测试必然抓不到帧。

    现场最常见的就是我们自己的 booth_server：跑预检 / 测试前要先把它停掉。
    这种情况下抓帧失败不是被测代码的毛病，要报 SKIP 并点名占用者 ——
    否则每次预检都红一片，真出问题的时候反而没人当回事。
    """
    found = set()
    for pid in os.listdir("/proc"):
        if not pid.isdigit():
            continue
        fd_dir = f"/proc/{pid}/fd"
        try:
            targets = [os.readlink(os.path.join(fd_dir, fd)) for fd in os.listdir(fd_dir)]
        except OSError:
            continue
        if not any("video" in t for t in targets):
            continue
        try:
            with open(f"/proc/{pid}/cmdline", "rb") as fh:
                cmd = fh.read().decode("utf-8", "replace").replace(chr(0), " ").strip()
        except OSError:
            cmd = "?"
        found.add("pid %s (%s)" % (pid, cmd[:60] or "?"))
    return "; ".join(sorted(found))


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


@contextmanager
def env(**values):
    saved = {k: os.environ.get(k) for k in values}
    os.environ.update({k: str(v) for k, v in values.items()})
    try:
        yield
    finally:
        for key, old in saved.items():
            if old is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = old


def jpeg_ok(data: bytes) -> tuple[bool, object]:
    """JPEG 魔数 + 能被 PIL 解码（PIL 与 cv2 无关，是真解一遍）。"""
    from PIL import Image

    if not data or not data.startswith(b"\xff\xd8") or not data.endswith(b"\xff\xd9"):
        return False, None
    try:
        image = Image.open(io.BytesIO(data))
        image.load()
    except Exception as err:  # noqa: BLE001
        return False, f"{type(err).__name__}: {err}"
    return True, image.size


def main() -> int:
    print("=== T-2 UVC 抓帧（camera.py） ===")
    info("camera.py", str(Path(camera.__file__).resolve()))
    info("PREVIEW_SIZE / PHOTO_SIZE", f"{camera.PREVIEW_SIZE} / {camera.PHOTO_SIZE}")

    # -------------------------------------------------------------------- a)
    print("\n=== a) 按 by-id 解析采集节点（不硬编码设备号） ===")
    resolved = None
    try:
        resolved = camera.resolve_device()
    except Exception as err:  # noqa: BLE001
        check(False, "a) resolve_device() 可调用", f"{type(err).__name__}: {err}")
    if resolved is None:
        skip("a) by-id 解析", "当前没有 /dev/v4l/by-id/usb-* 采集节点（相机没插？）")
    else:
        info("resolved", resolved)
        check(resolved.startswith("/dev/v4l/by-id/"),
              "a) 解析结果是 by-id 稳定路径（不是 /dev/videoN 硬编码）", resolved)
        check(os.path.exists(resolved), "a) by-id 路径真实存在（符号链接可解析）",
              f"-> {os.path.realpath(resolved)}")
        check("DICOTA" in resolved, "a) 命中 DICOTA 4K 采集节点（而非 M2M/metadata 节点）",
              resolved)
        real = os.path.realpath(resolved)
        other = [p for p in ("/dev/video0", "/dev/video1", "/dev/video2")
                 if os.path.exists(p) and os.path.realpath(p) != real]
        info("同机其它 video 节点（应被跳过）", f"{other or '无'}")

    if not HAS_CV2:
        print("\n=== b/c/d) 需要 cv2（camera.py 的执行依赖） ===")
        skip("b) 双分辨率抓帧", "本机没有 cv2 —— 是环境缺依赖，不是被测代码的问题")
        skip("c) 设备不存在时的明确错误", "本机没有 cv2")
        skip("d) mock 源", "本机没有 cv2")
        return report(resolved)

    # -------------------------------------------------------------------- b)
    print("\n=== b) 预览 %dx%d + 拍照 %dx%d 都要出非空 JPEG ==="
          % (camera.PREVIEW_SIZE + camera.PHOTO_SIZE))
    cam = camera.Camera()
    busy = camera_holders() if resolved else ""
    if resolved is None:
        skip("b) 真实相机双分辨率抓帧", "没有 by-id 采集节点，无法验证真实相机路径")
    else:
        try:
            status = cam.open(camera.PREVIEW_SIZE)
            info("预览状态", f"{status.width}x{status.height} {status.fourcc} "
                              f"{status.fps:.0f}fps device={status.device}")
            check(status.ok and status.real, "b) 预览打开成功且是真设备（非 mock）",
                  f"ok={status.ok} real={status.real}")
            check(status.device == resolved,
                  "b) 实际使用的设备就是 by-id 路径（不是硬编码序号）",
                  f"device={status.device}")
            check(status.width == camera.PREVIEW_SIZE[0] and
                  status.height == camera.PREVIEW_SIZE[1],
                  "b) 预览分辨率协商成 %dx%d" % camera.PREVIEW_SIZE,
                  f"{status.width}x{status.height}")
            check(status.fourcc.strip("\x00") == "MJPG",
                  "b) 用 MJPG（YUYV 在高分辨率只有 1fps）", f"fourcc={status.fourcc!r}")

            started = time.time()
            frame = cam.read_bgr(warm=2)
            info("预览帧", f"shape={getattr(frame, 'shape', None)} "
                           f"mean={frame.mean():.1f} std={frame.std():.1f} "
                           f"({time.time() - started:.2f}s)")
            preview = camera.encode_jpeg(frame, 85)
            ok, size = jpeg_ok(preview)
            check(ok and len(preview) > 5000, "b) 预览抓到非空 JPEG",
                  f"{len(preview)} B, PIL 解出 {size}")
            if ok:
                check(size == camera.PREVIEW_SIZE,
                      "b) 预览 JPEG 尺寸就是 %dx%d" % camera.PREVIEW_SIZE,
                      f"{size}")

            started = time.time()
            snap = cam.snapshot(camera.PHOTO_SIZE, warm=2)
            snap_seconds = time.time() - started
            ok2, size2 = jpeg_ok(snap)
            check(ok2 and len(snap) > 20000,
                  "b) 拍照抓到非空 JPEG（%dx%d）" % camera.PHOTO_SIZE,
                  f"{len(snap)} B, PIL 解出 {size2}, {snap_seconds:.2f}s")
            if ok2:
                check(size2 == camera.PHOTO_SIZE,
                      "b) 拍照 JPEG 尺寸就是 %dx%d" % camera.PHOTO_SIZE,
                      f"{size2}")
            check(cam.status.width == camera.PREVIEW_SIZE[0],
                  "b) 拍照后自动回到预览分辨率（同一句柄，避免二次打开失败）",
                  f"{cam.status.width}x{cam.status.height}")
            info("POWERLINE_50HZ 常量", f"{camera.POWERLINE_50HZ}（spec §3 要求 =1）")
            if camera.POWERLINE_50HZ != 1:
                check(False, "b) 50Hz 抗频闪常量", f"{camera.POWERLINE_50HZ} != 1")
        except camera.CameraError as err:
            if busy:
                skip("b) 真实相机抓帧", f"相机被占用，先停掉再测：{busy}")
            else:
                check(False, "b) 真实相机抓帧", f"设备在 by-id 上但打开/抓帧失败：{err}")
        except Exception as err:  # noqa: BLE001
            if busy:
                skip("b) 真实相机抓帧", f"相机被占用，先停掉再测：{busy}")
            else:
                check(False, "b) 真实相机抓帧", f"{type(err).__name__}: {err}")
        finally:
            cam.close()

        print("\n=== EXTRA) 预览流（/api/preview.mjpg 走的就是它） ===")
        try:
            stream = camera.Camera()
            stream.open(camera.PREVIEW_SIZE)
            stop = threading.Event()
            frames = []
            for jpeg in camera.mjpeg_stream(stream, stop, fps=25, quality=80):
                frames.append(jpeg)
                if len(frames) >= 3:
                    stop.set()
            stream.close()
            good = all(jpeg_ok(f)[0] for f in frames)
            check(good and len(frames) >= 3, "EXTRA) mjpeg_stream 产出可解码 JPEG 帧",
                  f"{len(frames)} 帧，大小 {[len(f) for f in frames]}")
        except Exception as err:  # noqa: BLE001
            if busy:
                skip("EXTRA) mjpeg_stream", f"相机被占用，先停掉再测：{busy}")
            else:
                check(False, "EXTRA) mjpeg_stream", f"{type(err).__name__}: {err}")

    # -------------------------------------------------------------------- c)
    print("\n=== c) 设备不存在：open() 不崩溃、返回明确错误 ===")
    bad_device = "/dev/video99"
    with env(BOOTH_CAMERA_DEVICE=bad_device):
        bad = camera.Camera()
        started = time.time()
        raised = None
        try:
            bad.open(camera.PREVIEW_SIZE)
        except Exception as err:  # noqa: BLE001
            raised = err
        elapsed = time.time() - started
        check(raised is not None, "c) open() 在不存在的设备上失败（而不是装作成功）")
        check(isinstance(raised, camera.CameraError),
              "c) 抛的是 camera.CameraError（明确、可捕获的领域错误）",
              f"{type(raised).__name__}: {raised}")
        check(raised is not None and bad_device in str(raised),
              "c) 错误信息点名了设备路径", f"{raised}")
        check(bad.status.ok is False, "c) status 如实标 ok=False",
              f"detail={bad.status.detail!r}")
        check(elapsed < 15.0, "c) 不挂死（快速返回错误）", f"{elapsed:.2f}s")
        check(bad.status.device == bad_device, "c) status 记录的是请求的设备",
              f"{bad.status.device}")

        raised2 = None
        try:
            bad.read_bgr()
        except Exception as err:  # noqa: BLE001
            raised2 = err
        check(isinstance(raised2, camera.CameraError),
              "c) 打开失败后 read_bgr() 也走明确错误（不是段错误/静默空帧）",
              f"{type(raised2).__name__}: {raised2}")
        check(bad.is_open is False, "c) is_open 如实为 False")
        bad.close()
        info("环境变量路径", "BOOTH_CAMERA_DEVICE=/dev/video99 生效")

    with env(BOOTH_CAMERA_DEVICE="/dev/video99"):
        via_arg = camera.Camera(device="/dev/video99")
        raised3 = None
        try:
            via_arg.open()
        except Exception as err:  # noqa: BLE001
            raised3 = err
        check(isinstance(raised3, camera.CameraError),
              "c) 构造参数 device= 与环境变量同一条降级路径",
              f"{type(raised3).__name__}")
        via_arg.close()

    # -------------------------------------------------------------------- d)
    print("\n=== d) 无相机时可切 mock 源 ===")
    with env(BOOTH_CAMERA="mock"):
        mock = camera.Camera()
        status = mock.open(camera.PREVIEW_SIZE)
        info("mock 状态", f"{status.width}x{status.height} real={status.real} "
                          f"detail={status.detail}")
        check(status.ok and status.real is False,
              "d) BOOTH_CAMERA=mock 生效且**不冒充真设备**",
              f"ok={status.ok} real={status.real}")
        frame = mock.read_bgr()
        check(frame is not None and getattr(frame, "size", 0) > 0,
              "d) mock 能出帧", f"shape={getattr(frame, 'shape', None)}")
        jpeg = camera.encode_jpeg(frame, 85)
        ok, size = jpeg_ok(jpeg)
        check(ok and len(jpeg) > 1000, "d) mock 帧能编成可解码 JPEG",
              f"{len(jpeg)} B, {size}")
        shot = mock.snapshot(camera.PHOTO_SIZE, warm=1)
        ok2, size2 = jpeg_ok(shot)
        check(ok2 and size2 == camera.PHOTO_SIZE, "d) mock 也支持拍照分辨率",
              f"{len(shot)} B, {size2}")
        check(mock.status.width == camera.PREVIEW_SIZE[0],
              "d) mock 拍照后同样回到预览分辨率", f"{mock.status.width}")
        mock.close()

    with env(BOOTH_CAMERA="mock", BOOTH_CAMERA_DEVICE="/dev/video99"):
        forced = camera.Camera()
        status = forced.open(camera.PREVIEW_SIZE)
        check(status.ok and status.real is False,
              "d) mock 优先于坏设备路径（相机拔了也能开发/降级）",
              f"device={status.device} detail={status.detail}")
        forced.close()

    mock_arg = camera.Camera(mock=True)
    status = mock_arg.open(camera.PREVIEW_SIZE)
    check(status.ok and status.real is False, "d) Camera(mock=True) 显式路径同样可用",
          f"device={status.device}")
    mock_arg.close()

    return report(resolved)


def report(resolved=None) -> int:
    print("\n=== T-2 汇总 ===")
    print(f"  resolved device: {resolved}")
    print(f"  检查项 {CHECKS} 个，失败 {len(FAILURES)} 个，跳过 {len(SKIPS)} 个")
    for name in FAILURES:
        print(f"  - FAILED: {name}")
    for name in SKIPS:
        print(f"  - SKIPPED: {name}")
    if FAILURES:
        print("CAMERA TEST: FAIL")
        return 1
    if CHECKS == 0:
        print("CAMERA TEST: SKIP（本机没有相机或 cv2，未取得任何实测证据）")
        return 0
    if SKIPS:
        print("CAMERA TEST: PASS（含 SKIP，见上）")
        return 0
    print("CAMERA TEST: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())