# -*- coding: utf-8 -*-
"""相机自愈的判定逻辑测试 —— 不需要真相机。

验证四组路径：连续读空帧触发重开、冷却期内不重复重开、拍照换分辨率失败
退回当前分辨率出图（参会者不空手）、**USB 复位该走 unbind+bind 还是直接 bind**。

运行：python3 app/tests/test_camera_heal.py
"""
import os
import pathlib
import sys
import tempfile
import time

HERE = pathlib.Path(__file__).resolve().parent
APP = HERE.parent
sys.path.insert(0, str(APP))

import camera  # noqa: E402

ok = []
def chk(cond, label, extra=""):
    ok.append(bool(cond))
    print("  [%s] %s %s" % ("PASS" if cond else "FAIL", label, extra))

class FakeCap:
    def __init__(self, good): self.good = good
    def read(self): return (self.good, object() if self.good else None)
    def release(self): self.released = True

def make_cam(fail_photo=False):
    cam = camera.Camera()
    cam.mock = False
    cam._cap = FakeCap(False)            # 一开始就是「读到空帧」的状态
    cam._size = camera.PREVIEW_SIZE
    opens = []
    def fake_open(size=None):
        opens.append(size)
        if fail_photo and size and tuple(size) == tuple(camera.PHOTO_SIZE):
            cam._status = camera.CameraStatus(False, "/dev/videoX", True, 0, 0, "", 0.0, "打不开")
            raise camera.CameraError("打不开 /dev/videoX")
        cam._cap = FakeCap(True)
        cam._fail_streak = 0
        cam._last_ok_at = time.time()
        cam._device = "/dev/videoX"
        cam._size = tuple(size) if size else cam._size
        cam._status = camera.CameraStatus(True, "/dev/videoX", True,
                                          cam._size[0], cam._size[1], "MJPG", 30.0, "stub")
        return cam._status
    cam._open_locked = fake_open
    cam._opens = opens
    return cam

print("=== A) 连续 3 次读空帧 -> 自动重开 ===")
cam = make_cam()
errs = []
for i in range(3):
    try:
        cam.read_bgr()
    except camera.CameraError as exc:
        errs.append(str(exc))
chk(errs == ["读到空帧", "读到空帧", "相机无响应，已触发自动重连"], "三次报错依次为 空/空/触发重连", errs)
chk(cam._reopens == 1, "重开了一次", "reopens=%d" % cam._reopens)
frame = cam.read_bgr()
chk(frame is not None, "重开后立刻能读到帧")
chk(cam._fail_streak == 0, "成功一帧后失败计数归零")

print("=== B) 冷却期内不重复重开（防重连风暴）===")
cam._cap = FakeCap(False)
for _ in range(3):
    try: cam.read_bgr()
    except camera.CameraError: pass
chk(cam._reopens == 1, "6s 冷却内没有第二次重开", "reopens=%d" % cam._reopens)
time.sleep(6.2)
cam._cap = FakeCap(False)
for _ in range(3):
    try: cam.read_bgr()
    except camera.CameraError: pass
chk(cam._reopens == 2, "冷却过后允许再重开一次", "reopens=%d" % cam._reopens)

print("=== C) 拍照换分辨率失败 -> 退回当前分辨率出图 ===")
cam2 = make_cam(fail_photo=True)
camera.encode_jpeg = lambda frame, quality=88: b"JPEG-BYTES"
data = cam2.snapshot(camera.PHOTO_SIZE, warm=1)
chk(data == b"JPEG-BYTES", "降级后仍然交出 JPEG（参会者不空手）")
chk("退回当前分辨率" in cam2.last_snapshot_note, "降级原因被记下（供上层标 degraded）",
    cam2.last_snapshot_note)
chk(cam2._cap is not None and cam2._cap.good, "降级出图后相机是可用状态")
chk(tuple(cam2._size) == tuple(camera.PREVIEW_SIZE), "预览尺寸已复位", str(cam2._size))

print("=== D) 健康读数带上了自愈账本 ===")
h = cam2.health()
chk({"reopens", "last_ok_s", "fail_streak", "snapshot_note"} <= set(h), "health 字段齐全", sorted(h))

print("=== E) USB 复位走哪条路：绑着 / 已被内核解绑 / 设备没了 ===")
tmp = tempfile.mkdtemp(prefix="devcon-usb-")
# 假装出一套 sysfs：drv = 驱动目录，dev = 控制器设备目录
drv = os.path.join(tmp, "xhci-hcd")
os.makedirs(drv)
open(os.path.join(drv, "unbind"), "w").close()
open(os.path.join(drv, "bind"), "w").close()
dev = os.path.join(tmp, "platform", "soc", "c0a00000.usb2", "xhci-hcd.15.auto")
os.makedirs(dev)
host = "xhci-hcd.15.auto"

saved_globs = camera.USB_DEVICE_GLOBS
camera.USB_DEVICE_GLOBS = (os.path.join(tmp, "platform", "*", "*") + "/",)
try:
    # 这一条就是 2026-09-15 现场那个状态：总线被注销，驱动同时被解绑
    plan = camera.usb_reset_plan(host, drv)
    chk(plan == "rebind", "驱动已被内核解绑 -> 直接 bind（不再先 unbind）", plan)

    os.makedirs(os.path.join(drv, host))          # 同一条控制器，这次驱动绑着
    plan = camera.usb_reset_plan(host, drv)
    chk(plan == "cycle", "驱动还绑着 -> unbind+bind（给总线重新上电）", plan)

    plan = camera.usb_reset_plan("xhci-hcd.9.auto", drv)
    chk(plan == "gone", "控制器设备都没了 -> 如实说救不了（人工插拔）", plan)
finally:
    camera.USB_DEVICE_GLOBS = saved_globs

chk(camera.usb_reset_plan(host, os.path.join(tmp, "nope")) == "gone",
    "没有 sysfs 驱动目录 -> gone")
chk(camera.usb_reset_plan("", drv) == "gone", "host 为空 -> gone")

# usb_reset_host 在「救不了」时要给出可执行的下一步，而不是抛异常或假装成功
saved_dir = camera.USB_DRIVER_DIR
camera.USB_DRIVER_DIR = drv
try:
    r = camera.usb_reset_host("xhci-hcd.9.auto")
    r2 = camera.usb_reset_host(None)
finally:
    camera.USB_DRIVER_DIR = saved_dir
chk(r.get("ok") is False and "人工" in (r.get("detail") or ""),
    "救不了时如实报「请人工重新插拔相机」", r.get("detail"))
chk(r2.get("ok") is False, "不知道相机挂哪条时如实失败", r2.get("detail"))

print("\n%d/%d PASS" % (sum(ok), len(ok)))
sys.exit(0 if all(ok) else 1)