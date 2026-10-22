"""UVC 抓帧模块（唯一 import cv2 的模块）。

为什么单独成模块：cv2 与 spacemit_ort 同进程会崩（见 02-spec §1.1），
所以抓帧和推理必须在不同进程里。本模块只负责抓帧。

实测设备：DICOTA 4K（1bcf:28c4）。**采集节点号是会变的**：USB 重新枚举一次就换号
（现场见过 video1 → video2），所以一律按 by-id 解析，不要钉死序号。
  * /dev/video0 是 Linlon 编解码 M2M，不是相机；
  * 每个 USB 摄像头会出两个节点，`*-video-index1` 那个是 metadata，打得开但抓不到帧；
  * YUYV 在高分辨率下只有 1 fps（1280x720 也才 5 fps），必须用 MJPG。
"""

from __future__ import annotations

import glob
import os
import subprocess
import threading
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

import numpy as np

# 中国的市电是 50Hz；相机默认 60Hz 会与灯光拍频，画面出现横向滚动条。
POWERLINE_50HZ = 1
# 相机源帧与拍照都用 1920x1080；网页预览由 PreviewHub 缩到 1280x720。
#
# 为什么拍照不用 2592x1944 了：USB2 高带速实测只有 ~35 MB/s，而 MJPG 2592x1944
# 每帧 1~2 MB，相机标称 30fps 就是 30~60 MB/s —— 一档就顶穿链路，切过去的那几秒
# 必然 URB 溢出（现场 dmesg：`Failed to resubmit video URB (-1)`、
# `Failed to set UVC probe control : -71`），随后就是设备重新枚举、预览断流。
# 而 2592 档多出来的像素最后也到不了成片：官方主视觉的人像窗只有约 278x357。
# 要更高档位：BOOTH_PHOTO_SIZE=2592x1944（先确认链路扛得住）。
#
# 预览尺寸也从环境变量取，是为了**把它和 BOOTH_PHOTO_SIZE 设成同一个值** ——
# 那样每次拍照的两次分辨率重协商就彻底消失了（重协商是整条 USB 链路上最重的动作，
# 见 snapshot 的注释）。现场 A/B 只要改两个环境变量，不用动代码。
# 代价是预览流的编解码开销。K3 实测（每帧 编+解，素材是真成片、JPEG q78）：
#   1280x720   17.3 ms · 106 KB/帧 · 1.3 MB/s  ← 12fps 占单核 21%
#   1920x1080  38.6 ms · 202 KB/帧 · 2.4 MB/s  ← 占 46%
#   2592x1944  89.5 ms · 425 KB/帧 · 5.1 MB/s  ← 超过单核，别当预览用
# USB2 实测可用约 35 MB/s，所以 1080p 的 2.4 MB/s 很宽裕 —— 卡的是 CPU、不是链路。
PREVIEW_SIZE = tuple(
    int(x) for x in os.environ.get("BOOTH_PREVIEW_SIZE", "1920x1080").lower().split("x")
)  # type: ignore[assignment]
PHOTO_SIZE = tuple(
    int(x) for x in os.environ.get("BOOTH_PHOTO_SIZE", "1920x1080").lower().split("x")
)  # type: ignore[assignment]
# 连续几次读不到帧，就认定句柄已经废了，强制关掉重开。
# 为什么必须换句柄：USB 重新枚举后 /dev/videoN 会换号，进程手里那条旧 fd
# 变成 "deleted"，read() 从此恒返回 False —— 只重试不换句柄就是永久黑屏，
# 只能人工重启（2026-09-14 18:28 现场实测：相机连掉两次，预览流死在那一刻）。
FAIL_STREAK_LIMIT = 3
# MJPG 协商失败时的重试：相机刚回到总线上的那一两秒，uvcvideo 经常先给一个
# 低频/无色的格式（实测 640x480 GREY）。隔一会儿重开就能要回 MJPG。
MJPG_RETRIES = 3
MJPG_RETRY_GAP_S = 0.8
# 两次自动重开之间至少隔这么久：链路不稳时别把重连打成风暴。
RECOVER_COOLDOWN_S = 6.0
# 「连续读空帧」还不够，得同时满足「确实有一阵子没出过帧」才算相机没了。
# 预览是 12fps，3 次空帧只有 0.25 秒 —— 链路抖一下、URB 重排队一次就能凑满 3 次。
# 为这么一下去 close→open（甚至复位整条总线）是自己给自己制造断流：
# 本机实测一次 close→open 就要重新协商两回，而那正是全链路最重的动作。
STALE_GRACE_S = 1.5


class CameraError(RuntimeError):
    pass


def _v4l_names() -> "list[tuple[str, str]]":
    """列出 (video 节点, 内核里的设备名)。by-id 符号链接还没建好时用它兜底。"""
    found = []
    for node in sorted(glob.glob("/dev/video*")):
        try:
            name = (Path("/sys/class/video4linux") / Path(node).name / "name").read_text()
        except Exception:
            continue
        found.append((node, name.strip()))
    return found


# 相机掉到总线下线以后，只有把控制器 unbind/bind 才能让它回来（见 usb_reset_host）。
# 这条路径要十几秒，比「重开设备节点」贵得多，所以只在节点确实没了的时候才走。
USB_RESET_COOLDOWN_S = 30.0
USB_DRIVER_DIR = "/sys/bus/platform/drivers/xhci-hcd"
# 控制器设备自己在哪 —— 它在平台总线上，不随驱动的绑定状态变化。
USB_DEVICE_GLOBS = ("/sys/devices/platform/*/*/", "/sys/devices/platform/*/")


def usb_host_of(device: str | None = None) -> str | None:
    """这台相机挂在哪条 USB 控制器上（返回 ``xhci-hcd.N.auto``）。

    为什么要知道：相机**从总线上掉下去**时（内核原话
    ``Cannot enable. Maybe the USB cable is bad?`` → ``unable to enumerate``），
    ``/dev/v4l/by-id`` 会整个消失，重开设备节点无从谈起 —— 要复位就得先知道
    复位哪条控制器。
    """
    if not device:
        return None
    try:
        name = os.path.basename(os.path.realpath(device))
        link = os.path.realpath("/sys/class/video4linux/%s" % name)
        for part in link.split(os.sep):
            if part.startswith("xhci-hcd."):
                return part
    except Exception:
        return None
    return None


def hcd_device_dir(host: str) -> str | None:
    """控制器设备在 sysfs 里的目录（不管驱动绑没绑上）。找不到返回 None。"""
    import glob

    if not host:
        return None
    for pat in USB_DEVICE_GLOBS:
        for path in glob.glob(pat + host):
            if os.path.isdir(path):
                return path
    return None


def usb_reset_plan(host: str, driver_dir: str = USB_DRIVER_DIR,
                   device_dir: str | None = None) -> str:
    """复位一条 USB 控制器该走哪条路。

    * ``cycle``  —— 驱动还绑着：unbind 再 bind，等于给这条总线重新上电
    * ``rebind`` —— 驱动**已经被内核解绑**（总线已注销）：直接 bind
    * ``gone``   —— sysfs 里连控制器设备都没了：软件救不了，只能人工插拔

    为什么要分：相机整个从总线上掉下去时，内核把整条总线注销的同时
    **也把驱动解绑了**，这时再 unbind 只会 ENODEV。见 2026-09-15 现场：
    点「USB 复位」报 `unbind xhci-hcd.15.auto 失败：OSError: [Errno 19] No such device`，
    而 bind 一下相机就回来了。
    """
    if not host or not os.path.isdir(driver_dir):
        return "gone"
    if os.path.exists(os.path.join(driver_dir, host)):
        return "cycle"
    if device_dir is None:
        device_dir = hcd_device_dir(host)
    if device_dir and os.path.isdir(device_dir):
        return "rebind"
    return "gone"


def _settle(settle_s: float, until) -> float:
    """等总线回来，返回实际等了几秒。

    没给 until 就老实睡满（保持老行为）；给了就一边等一边探。相机重新枚举通常
    3~7 秒就好了，固定睡满 9 秒是白让大屏多黑几秒 —— 2026-09-17 实测
    09:50:18 复位、09:50:25 已经在出图了。
    """
    if until is None:
        time.sleep(settle_s)
        return settle_s
    waited = 0.0
    while waited < settle_s:
        time.sleep(0.25)
        waited += 0.25
        try:
            if until():
                return waited
        except Exception:
            pass
    return waited


def usb_reset_host(host: str | None, settle_s: float = 9.0,
                   until=None) -> dict:
    """把一条 USB 控制器复位一次 —— 等于给这条总线上的口重新上电。

    注意：这条控制器上的**所有**设备都会跟着下线并重新枚举（本机这条总线上
    只有相机和一个 USB hub，所以代价可以接受）。正因为代价不小，它是
    「节点确实没了」时的最后一招，不是每次读空帧都来一遍。

    走哪条路由 ``usb_reset_plan`` 定：驱动还绑着就 unbind+bind；
    **已经被解绑就直接 bind**（见那个函数的注释）。
    """
    if not os.path.isdir(USB_DRIVER_DIR):
        return {"ok": False, "detail": "这台机器没有 %s（不是 sysfs 那套）" % USB_DRIVER_DIR}
    if not host:
        return {"ok": False, "detail": "不知道相机挂在哪条 USB 控制器上"}

    plan = usb_reset_plan(host)
    if plan == "gone":
        return {"ok": False, "plan": plan,
                "detail": "sysfs 里找不到 USB 控制器 %s —— 请人工重新插拔相机" % host}
    if plan == "cycle":
        try:
            with open(os.path.join(USB_DRIVER_DIR, "unbind"), "w") as fh:
                fh.write(host)
        except Exception as exc:
            return {"ok": False, "plan": plan,
                    "detail": "unbind %s 失败：%s: %s" % (host, type(exc).__name__, exc)}
        time.sleep(3.0)
    try:
        with open(os.path.join(USB_DRIVER_DIR, "bind"), "w") as fh:
            fh.write(host)
    except Exception as exc:
        return {"ok": False, "plan": plan,
                "detail": "bind %s 失败（总线可能没回来）：%s: %s"
                          % (host, type(exc).__name__, exc)}
    waited = _settle(settle_s, until)
    return {"ok": True, "host": host, "plan": plan, "settled_s": round(waited, 2)}


def _host_cache_path() -> str | None:
    try:
        return os.path.join(tempfile.gettempdir(), "devcon-camera-usb-host.txt")
    except Exception:
        return None


def _cached_usb_host() -> str | None:
    """上次成功打开相机时记下的控制器 —— 服务重启后还能知道该复位哪条。"""
    path = _host_cache_path()
    if not path:
        return None
    try:
        with open(path) as fh:
            host = fh.read().strip()
        return host or None
    except Exception:
        return None


def _cache_usb_host(host: str) -> None:
    path = _host_cache_path()
    if not path or not host:
        return
    try:
        with open(path, "w") as fh:
            fh.write(host)
    except Exception:
        pass


def node_exists(path: str | None) -> bool:
    """这个设备节点现在真的能用吗（顺着符号链接看目标在不在）。

    必须顺着链接看一眼目标：相机掉线之后，udev 留下的 by-id 符号链接会先变成
    **悬空链接** —— 链接还在、目标 /dev/videoN 已经没了，而 glob 照样匹配得到它。
    2026-09-17 现场就栽在这上面：应用以为「相机还在总线上」（解析结果非空），
    于是只在原地反复 open 一个死节点 —— 服务日志每 6 秒一条
    `VIDIOC_REQBUFS: errno=19 (No such device)`，连刷 73 秒都没去复位总线，
    大屏就一直冻着，一直冻到人去点「USB 复位」。
    """
    if not path:
        return False
    try:
        return os.path.exists(path)
    except OSError:
        return False


def resolve_device() -> str | None:
    """按 by-id 找 DICOTA，避免 video 序号漂移。

    为什么要三级：USB 重新枚举的瞬间，udev 还没把 by-id 符号链接建回来，
    只靠 glob 会返回 None，调用方就会退到一个已经作废的 /dev/videoN 上死磕。
    所以最后再按 sysfs 里的设备名扫一遍，并避开 index1（那是 metadata 节点，
    打得开但抓不到帧）。
    """
    for pattern in ("/dev/v4l/by-id/usb-*DICOTA*-video-index0",
                    "/dev/v4l/by-id/usb-*video-index0"):
        matches = sorted(glob.glob(pattern))
        for path in matches:
            if node_exists(path):
                return path
        # 匹配到了但目标不存在 = 悬空链接，就是「相机已经不在总线上」。
        # 这里绝不能 return，否则上层会拿它当「相机还在」的证据（见 node_exists）。
    # 兜底：按内核设备名找。index1 是 metadata 节点，必须排除。
    metadata = {os.path.realpath(p)
                for p in glob.glob("/dev/v4l/by-id/*-video-index1")}
    for node, name in _v4l_names():
        if os.path.realpath(node) in metadata:
            continue
        if "DICOTA" in name.upper():
            return node
    return None


@dataclass
class CameraStatus:
    ok: bool
    device: str | None
    real: bool
    width: int
    height: int
    fourcc: str
    fps: float
    detail: str = ""
    # mock 源的细分：False = 空灰合成源（没内容可看，界面藏掉画面）；
    # True  = 静态照片联调源（有真照片，界面照常显示画面 + MOCK 徽章）。
    static_photo: bool = False


def set_powerline_50hz(device: str) -> str:
    """把抗频闪设成 50Hz。返回 "" 表示设上了，否则返回一句可写进状态的原因。

    为什么不用 ``cv2.CAP_PROP_POWERLINE_FREQUENCY``：实测本板上 OpenCV 的
    ``cap.get()`` 恒返回 -1，设没设上都读不出来 —— 无法验证的「已设置」等于没设。
    ``v4l2-ctl`` 能设也能读回（实测 default=2/60Hz → 1/50Hz），所以用它。
    """
    try:
        import subprocess

        subprocess.run(["v4l2-ctl", "-d", device,
                        "--set-ctrl=power_line_frequency=%d" % POWERLINE_50HZ],
                       capture_output=True, timeout=5.0, check=True)
        out = subprocess.run(["v4l2-ctl", "-d", device, "--get-ctrl=power_line_frequency"],
                             capture_output=True, timeout=5.0)
        value = (out.stdout or b"").decode("utf-8", "replace").strip().split(":")[-1].strip()
        if value == str(POWERLINE_50HZ):
            return ""
        return "power_line_frequency=%s（期望 50Hz）" % (value or "?")
    except FileNotFoundError:
        return "power_line_frequency 未设（无 v4l2-ctl）"
    except Exception as exc:
        return "power_line_frequency 未设（%s）" % type(exc).__name__


class Camera:
    """线程安全的 V4L2 抓帧器；相机掉线时按退避重连。"""

    def __init__(self, device: str | None = None, mock: bool | None = None):
        self.requested_device = device or os.environ.get("BOOTH_CAMERA_DEVICE") or None
        self.mock = mock if mock is not None else os.environ.get("BOOTH_CAMERA", "").lower() == "mock"
        self._cap = None
        self._lock = threading.Lock()
        self._size = PREVIEW_SIZE
        self._status = CameraStatus(False, None, not self.mock, 0, 0, "", 0.0, "未打开")
        self._retry_after = 0.0
        self._mock_phase = 0.0
        # 自愈用的账本：连续失败几次、上一次读成功是何时、重开了几回。
        self._fail_streak = 0
        self._last_ok_at = 0.0
        self._reopens = 0
        self._last_recover_at = 0.0
        self._last_recover_note = ""
        self._recover_after = 0.0
        self._recover_lock = threading.Lock()
        self._open_lock = threading.Lock()
        self._device: str | None = None
        self._snapshot_note = ""
        # USB 总线复位账本：掉到总线下线时用得上，现场也得看得见复位过几次
        self._open_fail_streak = 0
        self._usb_host: str | None = None
        self._usb_resets = 0
        self._usb_reset_fails = 0
        self._last_usb_reset_at = 0.0
        self._last_usb_reset_note = ""

    # ---- 打开 / 关闭 --------------------------------------------------
    def open(self, size: tuple[int, int] | None = None) -> CameraStatus:
        """打开（或重新打开）相机。同一时刻只允许一个线程在开关它。

        为什么要串行：预览线程（自愈重开）和拍照线程（切分辨率）都会调 open()，
        两个线程同时开关同一个 V4L2 节点会互相撞成 EBUSY —— 在本就不稳的链路上，
        撞一次就是「相机不可用」，参会者当场拿不到照片。
        """
        with self._open_lock:
            return self._open_locked(size)

    def _open_locked(self, size: tuple[int, int] | None = None) -> CameraStatus:
        size = size or PREVIEW_SIZE
        # 已经在这个尺寸上开着，就直接复用 —— 不要去 close 再 open。
        # close→open 是整场里对 USB 链路最重的动作（见 snapshot 的注释），而
        # 「尺寸没变」恰恰是最常走的一条路：预览线程每秒都在抓帧、拍照前后各
        # 换一次分辨率。白做一次重新协商，换来的只有掉线风险。
        if (not self.mock and self._cap is not None and self._status.ok
                and tuple(self._size) == (int(size[0]), int(size[1]))):
            return self._status
        self.close()
        if self.mock:
            self._size = size
            self._status = CameraStatus(True, "mock", False, size[0], size[1], "MOCK", 30.0,
                                        "静态照片源" if self._static_photo() else "开发用合成源",
                                        static_photo=self._static_photo())
            return self._status
        # 合成源不需要 cv2；只有真的开 V4L2 才需要。缺了要说人话，
        # 别让现场看见一个 ModuleNotFoundError 还得去猜是相机坏了还是没装包。
        try:
            import cv2
        except Exception as exc:
            raise CameraError("这台机器没有 cv2，打不开相机：%s" % exc) from exc

        # 设备打哪来，分两种，规矩不一样：
        #
        # 1) 操作员**显式钉死**了路径（BOOTH_CAMERA_DEVICE 或 Camera(device=...)）：
        #    那是他的判断，就按他的用。路径不在就如实报错，**绝不**偷偷换成按名字
        #    解析出来的另一台相机再回报成功 —— 那种「装作成功」比直接失败更难查，
        #    现场会变成「配置是错的，但没人知道，画面还是另一路」。
        # 2) 没钉死：按 by-id / 内核设备名解析（USB 重新枚举后 video 序号会漂移，
        #    现场实测 video1 -> video2），解析不到再退到上次开成功过的那个节点。
        #
        # 「钉死的序号因重新枚举而失效」这种救场，交给 recover() 显式解绑后再解析
        # （见那里），不走这里的静默替换 —— 重连是明说的事，要留痕。
        if self.requested_device:
            device = self.requested_device
            if not os.path.exists(device):
                self._status = CameraStatus(False, device, True, 0, 0, "", 0.0,
                                            "指定的设备不存在：%s" % device)
                raise CameraError(self._status.detail)
        else:
            device = resolve_device() or self._device
            if not device or not os.path.exists(device):
                # 报错，而不是去开一个碰巧存在的别的节点
                self._status = CameraStatus(False, device, True, 0, 0, "", 0.0,
                                            "找不到可用相机：by-id 与内核设备名都没解析到")
                raise CameraError(self._status.detail)
        # 抗频闪要在 cv2 拿到设备之前设：现场是 50Hz 市电，默认 60Hz 会在灯下
        # 压出横向滚动条纹。放在开流前是因为此时设备还没有被 cv2 占住。
        early_notes = []
        power_note = set_powerline_50hz(device)
        if power_note:
            early_notes.append(power_note)

        cap = self._open_capture(device)
        if cap is None:
            self._status = CameraStatus(False, device, True, 0, 0, "", 0.0, f"打不开 {device}")
            raise CameraError(self._status.detail)

        cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, size[0])
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, size[1])
        cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)

        # 自适应：自动曝光 + 自动对焦（失败不致命，只记录）
        notes = list(early_notes)
        for prop, value, label in (
            (cv2.CAP_PROP_AUTO_EXPOSURE, 3, "auto_exposure"),
            (getattr(cv2, "CAP_PROP_AUTOFOCUS", None), 1, "autofocus"),
        ):
            if prop is None:
                continue
            try:
                if not cap.set(prop, value):
                    notes.append(label)
            except Exception:
                notes.append(label)

        actual_w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        actual_h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        raw_fourcc = int(cap.get(cv2.CAP_PROP_FOURCC))
        fourcc = "".join(chr((raw_fourcc >> (8 * i)) & 0xFF) for i in range(4))
        fps = float(cap.get(cv2.CAP_PROP_FPS))

        # 协商失败会掉到 YUYV（高分辨率只有 1fps）或 GREY（直接没颜色），两个都
        # 不能拿去给参会者拍照。实测相机从总线上抖回来之后第一次协商就是
        # 640x480 GREY —— 隔一会儿重开就能要回 MJPG，所以这里重试几次，
        # 而不是将就着拿一个灰的 640x480 出片。
        for _ in range(MJPG_RETRIES):
            if fourcc.strip("\x00") in ("MJPG", "MJPG".lower()):
                break
            try:
                cap.release()
            except Exception:
                pass
            time.sleep(MJPG_RETRY_GAP_S)
            cap = self._open_capture(device)
            if cap is None:
                self._status = CameraStatus(False, device, True, 0, 0, "", 0.0,
                                            "重试协商 MJPG 时打不开 %s" % device)
                raise CameraError(self._status.detail)
            cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))
            cap.set(cv2.CAP_PROP_FRAME_WIDTH, size[0])
            cap.set(cv2.CAP_PROP_FRAME_HEIGHT, size[1])
            cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
            raw_fourcc = int(cap.get(cv2.CAP_PROP_FOURCC))
            fourcc = "".join(chr((raw_fourcc >> (8 * i)) & 0xFF) for i in range(4))
            actual_w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
            actual_h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
            fps = float(cap.get(cv2.CAP_PROP_FPS))

        if fourcc.strip("\x00") not in ("MJPG", "MJPG".lower()):
            notes.append("fourcc=%r（期望 MJPG，重试 %d 次仍不行）" % (fourcc, MJPG_RETRIES))

        self._cap = cap
        self._device = device
        # 记住这条相机挂在哪条控制器上：等它掉下去的时候，这是唯一能复位回来的线索
        host = usb_host_of(device)
        if host:
            self._usb_host = host
            _cache_usb_host(host)
        self._size = (actual_w, actual_h)
        self._fail_streak = 0
        self._last_ok_at = time.time()
        self._status = CameraStatus(
            True, device, True, actual_w, actual_h, fourcc.strip("\x00"), fps,
            "; ".join(notes) if notes else "ok",
        )
        return self._status

    def _open_capture(self, device: str, attempts: int = 3, gap: float = 0.35):
        """打开 V4L2 设备，给瞬时 EBUSY 留几次机会。

        拍一张要先 close 再 open 切到拍照分辨率、拍完再切回预览，大屏那边还在同时拉
        MJPEG。刚从 close 放开的 V4L2 节点偶尔还会短暂被内核标成忙 —— 一次开不上就
        raise，整台装置就卡在「相机不可用」，只能人工重启（现场实测踩过）。
        """
        import cv2

        for index in range(max(1, attempts)):
            cap = cv2.VideoCapture(device, cv2.CAP_V4L2)
            if cap.isOpened():
                return cap
            # 开失败的句柄必须显式放掉：留着不 release，下次重试照样是忙。
            try:
                cap.release()
            except Exception:
                pass
            if index + 1 < attempts:
                time.sleep(gap)
        return None

    def close(self) -> None:
        with self._lock:
            if self._cap is not None:
                try:
                    self._cap.release()
                except Exception:
                    pass
                self._cap = None

    # ---- 自愈 ---------------------------------------------------------
    def usb_reset(self, reason: str = "") -> dict:
        """整条 USB 控制器复位（相机从总线上掉下去时的最后一招）。

        什么时候轮到它：``/dev/v4l/by-id`` 整个消失 —— 这时重开设备节点、
        重试、hub 重新授权都没用，只有让内核重新枚举这条总线。
        控制器是哪条：优先用上次开成功时记下的，其次现场从节点倒推，
        最后读上次落盘的缓存。都拿不到就如实说「请人工重新插拔」。
        """
        host = self._usb_host or usb_host_of(resolve_device()) or _cached_usb_host()
        if not host:
            note = ("相机不在总线上，而且不知道它挂在哪条 USB 控制器上"
                    "（本次开机还没成功打开过它）—— 请人工重新插拔相机")
            self._last_usb_reset_note = note
            return {"ok": False, "detail": note}
        self.close()
        # 只认「真的动过 sysfs」的复位：sysfs 里连控制器都没有时 usb_reset_host
        # 什么都没做就返回失败，那种不该记进「已复位 N 次」—— 否则操作页上的数字
        # 会涨成一个查不出原因的假象（现场见过 1536 次，而内核侧只重新注册过 87 回）。
        out = usb_reset_host(host, until=lambda: resolve_device() is not None)
        self._last_usb_reset_at = time.time()
        if out.get("ok"):
            self._usb_resets += 1
            self._usb_host = host
            # 重新枚举后节点会换号（现场实测 video2 -> video1），旧节点必须清掉
            self._device = None
            self._last_usb_reset_note = "%s -> %s 复位成功（%s）" % (
                reason or "手工复位", host, out.get("plan") or "cycle")
        else:
            self._usb_reset_fails += 1
            self._last_usb_reset_note = "%s -> %s 复位失败：%s" % (
                reason or "手工复位", host, out.get("detail"))
        return dict(out, resets=self._usb_resets)

    def recover(self, reason: str = "") -> bool:
        """把相机句柄整个换掉（关掉、按名字重新解析、再开）。返回是否重开成功。

        触发场景（现场判据）：dmesg 出现
            ``usb 7-1.1: device descriptor read/64, error -71``
            ``uvcvideo 7-1.1:1.0: probe with driver uvcvideo failed with error -71``
        → 设备重新枚举 → video 节点换号 → 进程里的旧句柄跟着旧节点一起消失。
        这时 read() 恒为 False，**重试多少次都没用**，只有关掉重开。

        重开走 resolve_device()，所以节点换号也能找回来。
        冷却期内直接返回 False，避免链路一抖就把重连打成风暴。
        """
        with self._recover_lock:
            now = time.time()
            if now < self._recover_after:
                return False
            self._recover_after = now + RECOVER_COOLDOWN_S
            size = self._size
            # 这一句才是 recover() 存在的理由：节点换号之后，钉死的序号和上次开
            # 成功过的路径都已经作废，必须解绑、重新按名字解析。解绑只在「明确要
            # 重连」时发生，而且会写进 last_recover_note（见下），不是静默替换。
            self.requested_device = None
            # 相机整个从总线上掉了的时候，重开设备节点是没有意义的 —— 节点不存在。
            # 判据要**两个都成立**才算掉线：by-id 解析不出来，而且我们上次用的那个
            # 节点也不在了。只看一个的话，udev 还没把软链建回来的那一瞬间会被误判，
            # 白白把整条总线复位一次（那条总线上的设备都会跟着抖一下）。
            if not self.mock and resolve_device() is None and not (
                    self._device and os.path.exists(self._device)):
                if self._device is not None or self._usb_host or _cached_usb_host():
                    # 复位整条总线很贵（这条路上的设备全下线重枚举，实测 7~10 秒），
                    # 先把 30 秒冷却排上，免得链路抖一下就打一轮风暴。
                    self._recover_after = time.time() + USB_RESET_COOLDOWN_S
                    out = self.usb_reset("重连：相机不在总线上（%s）" % (reason or "无原因"))
                    if not out.get("ok"):
                        self._last_recover_note = "USB 复位失败：%s" % out.get("detail")
                        # 复位救不了**也不能就此撒手**：节点可能已经被内核自己修好了
                        # （重枚举完毕、或者别人先复位过）。原来这里直接 return False，
                        # 等于「复位这一招不管用」时连开都不试一次 —— 现场表现就是
                        # 永久黑屏，只能人工重启。继续往下走一次普通重开，成本只有一个
                        # 开不上的设备节点（ENODEV 立刻返回）。
                        self._retry_after = time.time() + RECOVER_COOLDOWN_S
                    else:
                        # 成功了：贵的部分（重新上电、重新枚举）已经付过，剩下的只是
                        # 等它把节点建出来，按普通冷却再试就行，不必再等 30 秒。
                        self._recover_after = time.time() + RECOVER_COOLDOWN_S
            try:
                self.close()
                status = self.open(size)
            except Exception as exc:
                self._last_recover_note = "%s: %s" % (type(exc).__name__, exc)
                # 设备还没枚举完：退避久一点，等 udev 把节点建出来
                self._retry_after = time.time() + RECOVER_COOLDOWN_S
                return False
            self._reopens += 1
            self._last_recover_at = time.time()
            self._last_recover_note = "%s -> %s %dx%d %s" % (
                reason or "recover", status.device, status.width, status.height,
                status.detail or "ok")
            return True

    def health(self) -> dict:
        """相机健康读数，进 /api/state：现场查断流就看这里。"""
        now = time.time()
        return {
            "device": self._device or self.requested_device,
            "resolved": resolve_device(),
            "open": bool(self._cap is not None or self.mock),
            "fail_streak": self._fail_streak,
            "last_ok_s": (round(now - self._last_ok_at, 1) if self._last_ok_at else None),
            "reopens": self._reopens,
            "last_recover_s": (round(now - self._last_recover_at, 1)
                               if self._last_recover_at else None),
            "last_recover_note": self._last_recover_note or None,
            "snapshot_note": self._snapshot_note or None,
            "open_fail_streak": self._open_fail_streak,
            "usb_host": self._usb_host,
            "usb_resets": self._usb_resets,
            "usb_reset_fails": self._usb_reset_fails,
            "last_usb_reset_s": (round(now - self._last_usb_reset_at, 1)
                                 if self._last_usb_reset_at else None),
            "last_usb_reset_note": self._last_usb_reset_note or None,
        }

    # ---- 抓帧 ---------------------------------------------------------
    def _ensure(self) -> None:
        if self._cap is None and not self.mock:
            if time.time() < self._retry_after:
                raise CameraError("相机重连退避中")
            try:
                self.open(self._size)
                self._open_fail_streak = 0
            except Exception as exc:
                self._retry_after = time.time() + 2.0
                # 这里是「设备节点整个没了」时唯一会被反复撞到的入口：_cap 一直是
                # None，read_bgr 里那套「连续读空帧」的计数永远涨不起来 ——
                # 不在这儿补计数，相机掉线后预览线程就会永远 raise、永远不触发
                # 自愈（现场实测踩到：节点消失之后一直没人去复位总线，相机再没回来）。
                self._open_fail_streak += 1
                if self._open_fail_streak >= FAIL_STREAK_LIMIT:
                    self._open_fail_streak = 0
                    self.recover("连续 %d 次打不开相机：%s" % (FAIL_STREAK_LIMIT, exc))
                raise CameraError(f"相机不可用：{exc}") from exc

    def read_bgr(self, warm: int = 0) -> np.ndarray:
        """抓一帧 BGR。warm>0 时丢弃前 warm 帧（等自动曝光/对焦稳定）。

        连续 FAIL_STREAK_LIMIT 次读不到帧，就认定句柄废了并强制重开 —— 见 recover()。
        """
        self._ensure()
        if self.mock:
            return self._synthetic()
        frame = None
        stale = False
        with self._lock:
            # _ensure() 是在锁外查的，这里必须复查：拍照会把相机切到高分辨率再切回来
            # （snapshot → open → close → open），中间的 close 会把句柄置空。
            # 不复查就是 `NoneType has no attribute 'read'`，而那不是相机错误、
            # 是竞态 —— 预览线程接不住就永久黑屏。
            if self._cap is None:
                raise CameraError("相机正在切换分辨率，稍后重试")
            for _ in range(max(1, warm + 1)):
                ok, candidate = self._cap.read()
                if ok and candidate is not None:
                    frame = candidate
            if frame is None:
                self._fail_streak += 1
                self._retry_after = time.time() + 1.0
                # 光数够次数不算：预览 12fps 下 3 次空帧只要 0.25 秒，链路抖一下
                # 就能凑满。必须「确实有一阵子没出过帧」才认定相机没了 ——
                # 否则每抖一下就 close→open 一次，等于自己把断流做实了。
                gap = time.time() - (self._last_ok_at or 0.0)
                stale = (self._fail_streak >= FAIL_STREAK_LIMIT
                         and gap >= STALE_GRACE_S)
            else:
                self._fail_streak = 0
                self._last_ok_at = time.time()
        if frame is not None:
            return frame
        if stale:
            # recover() 内部要拿 self._lock（close 会拿），所以只能在锁外调。
            self.recover("连续 %d 次读到空帧" % FAIL_STREAK_LIMIT)
            raise CameraError("相机无响应，已触发自动重连")
        raise CameraError("读到空帧")

    def snapshot(self, size: tuple[int, int] = PHOTO_SIZE, warm: int = 3) -> bytes:
        """切到拍照分辨率抓一帧，编码成 JPEG 字节。

        close→open 换分辨率是整场里对 USB 链路最重的动作（现场 -71 就出现在
        这个时刻），所以三级来：
          ① 切到拍照分辨率抓一帧；
          ② 失败先自愈重开一次再试；
          ③ 还不行就用当前分辨率直接抓一帧 —— 宁可少几个像素，
             也不能让参会者空手离开（L0 底线）。
        降级了不算「成功」，会写进 last_snapshot_note，由上层标 degraded。
        """
        note = ""
        frame = None
        try:
            self.open(size)
            frame = self.read_bgr(warm=warm)
        except Exception as exc:
            note = "%s: %s" % (type(exc).__name__, exc)
        if frame is None:
            self.recover("拍照失败（%s）" % note)
            try:
                frame = self.read_bgr(warm=1)
                note = "退回当前分辨率：%s" % note
            except Exception as exc:
                note = "%s / 重试仍失败：%s" % (note, exc)
        if frame is None:
            self._snapshot_note = note
            raise CameraError("拍照失败：%s" % note)
        data = encode_jpeg(frame, quality=95)
        self._snapshot_note = note
        try:
            self.open(PREVIEW_SIZE)  # 回到预览分辨率
        except Exception as exc:
            # 回不去不致命：预览线程下一次抓帧失败就会自愈重开
            self._snapshot_note = ("%s 回预览失败：%s" % (note, exc)).strip()
        return data

    def _static_photo(self) -> str:
        """静态照片源路径；没配置返回 ""（走空灰合成源）。"""
        value = os.environ.get("BOOTH_CAMERA_STATIC", "").strip()
        if value and Path(value).is_file():
            return value
        # BOOTH_CAMERA=mock,<路径> 的写法也收：逗号后面当路径
        mock_value = os.environ.get("BOOTH_CAMERA", "")
        if mock_value.lower().startswith("mock,"):
            candidate = mock_value[5:].strip()
            if candidate and Path(candidate).is_file():
                return candidate
        return ""

    def _synthetic(self) -> np.ndarray:
        """合成源：给开发机/无相机环境提供中性空画面。

        BOOTH_CAMERA=mock,<图片路径>（或 BOOTH_CAMERA_STATIC 指定路径）时，
        不画空灰底，而是把**固定照片**当取景源 —— 相机硬件故障期间的整机联调
        用：推理、抠像、合成、AIGC 全链路都能吃真照片，只有「按快门」这一步
        是回放。照片按需缩放到当前预览/拍照尺寸，BGR 返回（与 UVC 同口径）。
        缓存按「路径+尺寸」算，换尺寸（预览↔拍照）时重切一次，之后零开销。
        """
        static = self._static_photo()
        if not static:
            width, height = self._size
            return np.full((height, width, 3), 18, np.uint8)
        key = (static, int(self._size[0]), int(self._size[1]))
        cached = getattr(self, "_static_cache", None)
        if cached is not None and cached[0] == key:
            return cached[1]
        from PIL import Image

        with Image.open(static) as image:
            rgb = image.convert("RGB")
            # cover：铺满目标画幅再居中裁（取景是 object-fit: cover 的语义，
            # 直接 resize 拉伸会让人像比例失真，抠像与合成跟着遭殃）。
            scale = max(self._size[0] / rgb.width, self._size[1] / rgb.height)
            resized = rgb.resize(
                (max(1, round(rgb.width * scale)), max(1, round(rgb.height * scale))))
            left = (resized.width - self._size[0]) // 2
            top = (resized.height - self._size[1]) // 2
            cropped = resized.crop((left, top,
                                    left + self._size[0], top + self._size[1]))
        frame = np.asarray(cropped, dtype=np.uint8)[:, :, ::-1]  # RGB -> BGR
        self._static_cache = (key, frame)
        return frame

    @property
    def status(self) -> CameraStatus:
        return self._status

    @property
    def is_open(self) -> bool:
        # bool() 不能省：`a and b` 返回的是操作数，属性一旦漏出非布尔值，
        # 调用方写 `is True`（或塞进 JSON）就会露馅。
        return bool(self._status.ok and (self._cap is not None or self.mock))

    @property
    def last_snapshot_note(self) -> str:
        """上一次拍照的降级说明；"" 表示按拍照分辨率正常拍到。"""
        return self._snapshot_note


def encode_jpeg(frame: np.ndarray, quality: int = 88) -> bytes:
    import cv2

    ok, buffer = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), int(quality)])
    if not ok:
        raise CameraError("JPEG 编码失败")
    return buffer.tobytes()


class MediaStreamPublisher:
    """把 PreviewHub 的同一份源帧发布成 RTSP，交给 MediaMTX 输出 WebRTC。

    相机只由 PreviewHub 读取一次；发布线程只拿最新帧，编码跟不上时丢旧帧，
    不会反向阻塞抓帧、手势识别或拍照流程。
    """

    def __init__(self, size: tuple[int, int], fps: int):
        self.enabled = os.environ.get("BOOTH_MEDIA_PUBLISH_ENABLED", "1").lower() not in (
            "0", "false", "no", "off")
        self.size = (int(size[0]), int(size[1]))
        self.fps = max(1, int(fps))
        self.ffmpeg = os.environ.get("BOOTH_FFMPEG", "ffmpeg")
        self.url = os.environ.get(
            "BOOTH_MEDIA_RTSP_URL", "rtsp://127.0.0.1:8554/booth")
        self.encoder = os.environ.get("BOOTH_MEDIA_ENCODER", "h264_stcodec")
        self._cond = threading.Condition()
        self._frame: np.ndarray | None = None
        self._seq = 0
        self._sent = 0
        self._process: subprocess.Popen | None = None
        self._thread: threading.Thread | None = None
        self._stop = False
        self._last_error = ""
        self._started_at = 0.0

    def start(self) -> None:
        if not self.enabled:
            return
        with self._cond:
            if self._thread and self._thread.is_alive():
                return
            self._stop = False
            self._started_at = time.time()
            self._thread = threading.Thread(
                target=self._loop, name="media-stream-publisher", daemon=True)
            self._thread.start()

    def submit(self, frame: np.ndarray) -> None:
        if not self.enabled:
            return
        source = frame
        if (source.shape[1], source.shape[0]) != self.size:
            try:
                import cv2

                source = cv2.resize(source, self.size, interpolation=cv2.INTER_AREA)
            except Exception as exc:  # noqa: BLE001
                self._last_error = "发布帧缩放失败：%s" % exc
                return
        with self._cond:
            self._frame = source
            self._seq += 1
            self._cond.notify()

    def _command(self) -> list[str]:
        width, height = self.size
        return [
            self.ffmpeg, "-hide_banner", "-loglevel", "error",
            "-f", "rawvideo", "-pix_fmt", "bgr24",
            "-video_size", "%dx%d" % (width, height),
            "-framerate", str(self.fps), "-i", "pipe:0",
            "-an", "-c:v", self.encoder, "-pix_fmt", "yuv420p",
            "-b:v", os.environ.get("BOOTH_MEDIA_BITRATE", "4M"),
            "-maxrate", os.environ.get("BOOTH_MEDIA_MAXRATE", "5M"),
            "-bufsize", os.environ.get("BOOTH_MEDIA_BUFSIZE", "2M"),
            "-g", str(max(2, self.fps * 2)), "-keyint_min", str(max(2, self.fps * 2)),
            "-bf", "0", "-f", "rtsp", "-rtsp_transport", "tcp", self.url,
        ]

    def _start_process(self) -> bool:
        if self._process is not None and self._process.poll() is None:
            return True
        self._close_process()
        try:
            self._process = subprocess.Popen(
                self._command(), stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                bufsize=0)
            self._last_error = ""
            return True
        except Exception as exc:  # noqa: BLE001
            self._process = None
            self._last_error = "%s：%s" % (type(exc).__name__, exc)
            return False

    def _close_process(self) -> None:
        process = self._process
        self._process = None
        if process is None:
            return
        try:
            if process.stdin:
                process.stdin.close()
        except Exception:
            pass
        try:
            process.terminate()
            process.wait(timeout=1.0)
        except Exception:
            try:
                process.kill()
            except Exception:
                pass

    def _loop(self) -> None:
        retry_at = 0.0
        seen = 0
        while True:
            with self._cond:
                self._cond.wait_for(lambda: self._stop or self._seq != seen, timeout=1.0)
                if self._stop:
                    break
                frame = self._frame
                seen = self._seq
            if frame is None:
                continue
            if time.time() < retry_at:
                continue
            if not self._start_process():
                retry_at = time.time() + 3.0
                continue
            process = self._process
            try:
                if process is None or process.stdin is None:
                    raise BrokenPipeError("ffmpeg stdin 不可用")
                process.stdin.write(frame.tobytes(order="C"))
                process.stdin.flush()
                self._sent += 1
            except Exception as exc:  # noqa: BLE001
                self._last_error = "发布中断：%s" % exc
                self._close_process()
                retry_at = time.time() + 1.0
        self._close_process()

    def stop(self) -> None:
        if not self.enabled:
            return
        with self._cond:
            self._stop = True
            self._cond.notify_all()
        thread = self._thread
        if thread and thread.is_alive():
            thread.join(timeout=2.0)
        self._thread = None

    def stats(self) -> dict:
        process = self._process
        return {
            "enabled": self.enabled,
            "running": bool(self._thread and self._thread.is_alive()),
            "online": bool(process is not None and process.poll() is None),
            "encoder": self.encoder,
            "url": self.url,
            "size": [self.size[0], self.size[1]],
            "fps": self.fps,
            "sent": self._sent,
            "last_error": self._last_error or None,
        }


def mjpeg_stream(camera: Camera, stop: threading.Event, fps: int = 25, quality: int = 80):
    """无限产出 multipart/x-mixed-replace 风格的 JPEG 帧，供预览用（不推理）。"""
    interval = 1.0 / max(1, fps)
    while not stop.is_set():
        started = time.time()
        try:
            frame = camera.read_bgr()
            yield encode_jpeg(frame, quality)
        except Exception:
            # 抓帧失败只该让这一帧跳过。以前这里只接 CameraError，
            # 于是拍照瞬间的竞态异常直接把整个预览生成器打死 —— 大屏和拍摄位
            # 就一直黑着，直到有人手动刷新页面。
            time.sleep(0.25)
            continue
        remaining = interval - (time.time() - started)
        if remaining > 0:
            time.sleep(remaining)


class PreviewHub:
    """一路抓帧、N 路分发。

    **为什么必须这样**：大屏（/stage）和拍摄位（/）会**同时**拉 /api/preview.mjpg。
    以前每个连接都自己读相机、自己编 JPEG —— 两个连接就是两个线程抢同一个 V4L2
    句柄：帧被轮流吃掉，两边都卡；CPU 还要编两份。现场表现就是「老是断流」。

    改成单生产者之后：
      * 相机只有一条线程在读（不再互相偷帧）；
      * JPEG 只编一次，N 个连接共享；
      * 新连接**立刻**拿到最近一帧，不用等下一帧（不再黑屏几秒）；
      * 慢连接自动丢帧，只发最新的，不会把服务端拖住。
    """

    def __init__(self, camera: "Camera", fps: int = 12, quality: int = 78,
                 output_size: tuple[int, int] | None = None):
        self.camera = camera
        self.interval = 1.0 / max(1, int(fps))
        self.quality = int(quality)
        self.output_size = tuple(output_size) if output_size else None
        self._cond = threading.Condition()
        self._jpeg: bytes | None = None
        self._frame: np.ndarray | None = None
        self._seq = 0
        self._last_at = 0.0
        self._error: str | None = None
        self._clients = 0
        self._thread: threading.Thread | None = None
        self._started_at = 0.0
        self.publisher = MediaStreamPublisher(self.output_size or PREVIEW_SIZE, fps)
        # 待机门（2026-10-09）：没人排队/拍摄时**不读相机**。
        # 现场纪律：无人预约排队摄像头不用开 —— 省电、省 USB 带宽、
        # 也避免"没人也在拍"的观感。门开着（True）= 照常抓帧；
        # 关上（False）= 循环空转 sleep，不清已有的最后一帧
        # （大屏取景还在显示它，清了就是闪黑）。
        # 门由 Booth 的 tick 循环按「队列有无活动条目 + active_job」驱动，
        # 状态变更后由等待线程自己感知（wait_for 已带超时轮询）。
        self._gate_open = False

    # ---- 待机门 ----
    def set_gate(self, open_: bool) -> None:
        """开/关抓帧门，并让待机状态真正释放相机与编码器。"""
        open_ = bool(open_)
        with self._cond:
            if self._gate_open == open_:
                return
            self._gate_open = open_
            self._cond.notify_all()

        if open_:
            # 相机仍由抓帧线程在首帧时打开；这里只恢复 WebRTC 发布线程。
            self.publisher.start()
        else:
            # 关闭顺序固定：先停止外部编码器，再释放 V4L2 句柄。
            # Camera.close() 自带锁，会等待正在完成的读帧，避免并发释放。
            self.publisher.stop()
            close_camera = getattr(self.camera, "close", None)
            if callable(close_camera):
                close_camera()

    def gate_open(self) -> bool:
        with self._cond:
            return self._gate_open


    # ---- 生命周期 ----
    def start(self) -> None:
        with self._cond:
            if self._thread is not None and self._thread.is_alive():
                return
            self._started_at = time.time()
            self._thread = threading.Thread(target=self._loop, name="preview-hub", daemon=True)
            self._thread.start()

    def _loop(self) -> None:
        while True:
            # 待机门关着：完全不碰相机。interval 节奏睡，醒来先看门——
            # 门开的那一刻（notify_all）立刻恢复抓帧，恢复延迟 ≤ 一个 interval。
            if not self.gate_open():
                with self._cond:
                    self._cond.wait_for(self.gate_open, timeout=self.interval)
                continue
            started = time.time()
            try:
                frame = self.camera.read_bgr()
                self.publisher.submit(frame)
                stream_frame = frame
                if self.output_size and (frame.shape[1], frame.shape[0]) != self.output_size:
                    import cv2

                    stream_frame = cv2.resize(
                        frame, self.output_size, interpolation=cv2.INTER_AREA)
                jpeg = encode_jpeg(stream_frame, self.quality)
                with self._cond:
                    self._frame = frame
                    self._jpeg = jpeg
                    self._seq += 1
                    self._last_at = time.time()
                    self._error = None
                    self._cond.notify_all()
            except Exception as exc:
                # 抓帧失败只跳过这一帧：拍一张要先切到高分辨率、拍完再切回来，
                # 那几百毫秒里读不到帧是正常的，不能因此把整条流断掉。
                with self._cond:
                    self._error = "%s: %s" % (type(exc).__name__, str(exc)[:120])
                    self._cond.notify_all()
                time.sleep(0.2)
            remaining = self.interval - (time.time() - started)
            if remaining > 0:
                time.sleep(remaining)

    def stop(self) -> None:
        """停止外部编码进程；抓帧线程由进程退出时自然回收。"""
        self.publisher.stop()
        close_camera = getattr(self.camera, "close", None)
        if callable(close_camera):
            close_camera()

    # ---- 订阅 ----
    def frames(self, stop: threading.Event, keepalive: float = 10.0):
        """产出最新的 JPEG 字节。

        只发最新一帧、不排队 —— 慢连接自然丢帧。相机长时间没出帧时，
        `keepalive` 秒补发一次上一次的帧，好让调用方的 write 有机会发现
        「对面已经关页面了」，不至于把线程吊死。
        """
        self.start()
        seen = -1
        with self._cond:
            self._clients += 1
        try:
            while not stop.is_set():
                with self._cond:
                    self._cond.wait_for(lambda: self._seq != seen, keepalive)
                    seen = self._seq
                    jpeg = self._jpeg
                # 有新帧就发新帧；等 keepalive 秒还是没新帧（相机在切分辨率或者
                # 掉线了）就把上一帧再发一次 —— 这一写才有机会撞上 BrokenPipe，
                # 否则对面早关页面了，这个线程还吊着。
                if jpeg is not None:
                    yield jpeg
        finally:
            with self._cond:
                self._clients -= 1

    def stats(self) -> dict:
        """给 /api/state 用。现场查「断流」就看这里：clients 是不是意外归零、
        seq 有没有在涨、last_frame_s 是不是卡住了。"""
        now = time.time()
        with self._cond:
            return {
                "clients": self._clients,
                "seq": self._seq,
                "fps": round(1.0 / self.interval, 1) if self.interval else None,
                "quality": self.quality,
                "source_size": ([int(self._frame.shape[1]), int(self._frame.shape[0])]
                                if self._frame is not None else None),
                "stream_size": list(self.output_size) if self.output_size else None,
                "running": bool(self._thread and self._thread.is_alive()),
                # 待机门：false = 无人时段不读相机（前端据此显示"待机省电"）
                "gate_open": self._gate_open,
                "last_error": self._error,
                "uptime_s": round(now - self._started_at, 1) if self._started_at else None,
                "last_frame_s": (round(now - self._last_at, 1)
                                 if self._last_at else None),
                # 相机的自愈账本：断流时先看 reopens 有没有在涨、last_ok_s 卡在哪
                # 相机对象没实现 health() 时也不能把 /api/state 打成 500：
                # 那会把整个界面拖垮，比少一行读数严重得多。
                "camera": (self.camera.health()
                           if hasattr(self.camera, "health") else {}),
                "webrtc": self.publisher.stats(),
            }

    def latest_jpeg(self) -> bytes | None:
        """最新一帧的 JPEG（不递增 seq、不算 client）。

        给在场检测（presence.py）用：它只需要「当前画面长什么样」，不该为了
        拿画面去注册成一条 MJPEG 连接。返回 None = 还没出过帧。
        """
        with self._cond:
            return self._jpeg

    def latest_capture_jpeg(self, quality: int = 95, max_age: float = 0.75) -> bytes | None:
        """将当前预览源帧原样锁定为拍摄帧，不重新打开相机。"""
        with self._cond:
            if self._frame is None or not self._last_at:
                return None
            if time.time() - self._last_at > max(0.0, float(max_age)):
                return None
            frame = self._frame.copy()
        return encode_jpeg(frame, quality)

    def latest_probe_jpeg(self, max_width: int = 480, quality: int = 80):
        """从最近原始帧直接生成检测小图，避免解码 1080p 预览 JPEG。"""
        with self._cond:
            frame = self._frame
        if frame is None:
            return None
        height, width = frame.shape[:2]
        target_width = max(1, int(max_width))
        if width > target_width:
            target_height = max(1, round(height * target_width / width))
            import cv2

            probe = cv2.resize(frame, (target_width, target_height), interpolation=cv2.INTER_AREA)
        else:
            probe = frame
        probe_height, probe_width = probe.shape[:2]
        return encode_jpeg(probe, quality), (probe_width, probe_height)


def self_test() -> dict:
    """真机自检：设备解析 + 抓帧 + 分辨率 + 格式。"""
    result: dict = {"resolved": resolve_device(), "mock": False, "steps": []}

    cam = Camera()
    try:
        status = cam.open(PREVIEW_SIZE)
        result["steps"].append(("预览 " + str(PREVIEW_SIZE), True,
                                f"{status.width}x{status.height} {status.fourcc} {status.fps:.0f}fps {status.detail}"))
        frame = cam.read_bgr(warm=2)
        result["steps"].append(("抓帧", frame is not None,
                                f"shape={frame.shape} mean={frame.mean():.1f}"))
        preview = encode_jpeg(frame, 85)
        result["steps"].append(("预览 JPEG", len(preview) > 5000, f"{len(preview)} bytes"))
        snap = cam.snapshot(PHOTO_SIZE, warm=2)
        result["steps"].append(("拍照 JPEG " + str(PHOTO_SIZE), len(snap) > 20000, f"{len(snap)} bytes"))
        result["status"] = cam.status.__dict__
    except Exception as exc:
        result["steps"].append(("相机", False, f"{type(exc).__name__}: {exc}"))
        result["status"] = cam.status.__dict__
    finally:
        cam.close()

    mock = Camera(mock=True)
    mock.open(PREVIEW_SIZE)
    frame = mock.read_bgr()
    result["mock"] = bool(frame is not None and frame.size)
    mock.close()
    return result


if __name__ == "__main__":
    import json

    report = self_test()
    ok = all(step[1] for step in report["steps"]) and report["mock"]
    for name, passed, detail in report["steps"]:
        print(f"  [{'PASS' if passed else 'FAIL'}] {name}: {detail}")
    print(f"  [{'PASS' if report['mock'] else 'FAIL'}] mock 合成源: shape ok")
    print(f"  resolved device: {report['resolved']}")
    print("CAMERA SELF-TEST:", "PASS" if ok else "FAIL")
    raise SystemExit(0 if ok else 1)
