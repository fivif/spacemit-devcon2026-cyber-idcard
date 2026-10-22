#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""装置 5 · 开场前预检（preflight）。

一句话：把「今天能不能开机就用」拆成一条条 PASS / FAIL / WARN。
**有 FAIL 就非零退出**，供彩排留档、值班 SOP、自动化脚本直接判断。

零 pip 依赖：只用标准库 + 真机上已装好的 numpy / PIL / cv2 / onnxruntime / spacemit_ort。

★ 进程纪律（02-spec §1.1）：本文件**绝不在同一个进程里同时 import cv2 与 spacemit_ort**。
  OpenCV 的共享库会破坏 ORT 的日志管理器，两者同进程必崩。所以：
    · 相机检查：本进程 import cv2（本进程不碰 spacemit_ort）；
    · AI 核检查：起**子进程** infer/worker.py，读它回话的 health；
    · 依赖导入检查：拆成三个子进程（worker 依赖 / 相机依赖 / 应用模块），互不污染。

用法：
    /usr/bin/python3 /opt/devcon/app/tools/preflight.py            # 人读
    /usr/bin/python3 /opt/devcon/app/tools/preflight.py --json     # 机读（彩排留档）
    /usr/bin/python3 /opt/devcon/app/tools/preflight.py --json > /opt/devcon/app/data/preflight.json
    /usr/bin/python3 /opt/devcon/app/tools/preflight.py --skip-ai   # 跳过 AI 核探测（几秒出结果）
    /usr/bin/python3 /opt/devcon/app/tools/preflight.py --probe-ai  # 服务在跑也另起 worker 复核

退出码：0 = 无 FAIL（允许有 WARN）；1 = 至少一项 FAIL；2 = 用法错误。

★ 一条判据上的取舍：「服务已在跑」时，端口 8090 一定被占，相机也一定被占 ——
  这两件都不是故障。所以本脚本会先认人（进程 cmdline 里有没有 booth_server.py）：
    · 被**别的**东西占（例如跑飞的 camd.py）→ FAIL，并点名 PID 与命令行；
    · 被**本服务自己**占 → PASS，并顺手改用 http://127.0.0.1:8090/api/state 读相机与
      AI 核的**实时读数**（不再二次抢相机、不再多开一个占 AI 核的会话）。
"""

from __future__ import annotations

import argparse
import glob
import inspect
import json
import os
import socket
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

# --------------------------------------------------------------------------
# 路径与环境：与 booth_server.py / infer/worker.py 同源，避免「预检过的」和
# 「真跑起来的」不是同一套东西（这类偏差最坑）。
# --------------------------------------------------------------------------
APP_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.environ.get("BOOTH_DATA", APP_DIR / "data"))
PORT = int(os.environ.get("BOOTH_PORT", "8090"))
PYTHON = os.environ.get("BOOTH_PYTHON") or "/usr/bin/python3"
WORKER_PY = APP_DIR / "infer" / "worker.py"

# 模型：默认值与 infer/worker.py 的 DEFAULTS 逐字一致；环境变量优先（systemd 单元里可覆盖）。
MODEL_DEFAULTS = {
    "seg": "/opt/devcon/assets/models/yoloe-v8s-seg.dynq.onnx",
    "clip": "/opt/devcon/assets/models/mobileclip.q.onnx",
    "det": "/usr/share/yolo-demo/yolov8/yolov8n.q.onnx",
    "matte": "/opt/devcon/assets/models/modnet.onnx",
}
MODEL_ENV = {"seg": "BOOTH_SEG_MODEL", "clip": "BOOTH_CLIP_MODEL", "det": "BOOTH_DET_MODEL",
             "matte": "BOOTH_MATTE_MODEL"}
MODEL_LABELS = {"seg": "分割（开放词汇实例分割）", "clip": "文本编码器（提示词）",
                "det": "检测（人形框，预览/兜底）",
                "matte": "抠像（MODNet，把人贴进主视觉人像窗）"}
MODEL_MIN_BYTES = 1_000_000  # 小于 1 MB 基本是截断/占位文件
BPE = os.environ.get("BOOTH_BPE", str(APP_DIR / "assets" / "clip" / "bpe_merges.txt"))
RETENTION_SPEC_HOURS = 24
PROTOCOL_PREFIX = "@@J "

# 逐个点名的关键文件（其余按集合校验，见 REQUIRED_SETS）
REQUIRED_FILES = (
    "booth_server.py", "camera.py", "infer_client.py", "store.py",
    "infer/worker.py", "infer/tokenizer.py",
    "render/qr.py", "render/stylize.py", "render/card.py", "render/watermark.py",
    "static/index.html", "static/stage.html", "static/operator.html", "static/pick.html",
    "assets/clip/bpe_merges.txt", "assets/brand/spacemit-logo.svg",
)
# 集合校验：(glob, 最少个数, 说明)
REQUIRED_SETS = (
    ("static/css/*.css", 6, "样式表"),
    ("static/js/*.js", 5, "前端脚本"),
    ("assets/cards/*.png", 6, "成片卡片底图"),
    ("assets/bg/*.png", 2, "成片底图（DevCon 2026 主视觉，暗调 + 亮调两套）"),
)

# --------------------------------------------------------------------------
# 小工具
# --------------------------------------------------------------------------
def _utf8_streams() -> None:
    """现场终端 locale 可能是 C/POSIX；显式用 UTF-8，免得中文输出炸在编码上。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def _kb(num: float) -> str:
    if num >= 1 << 30:
        return "%.2f GB" % (num / float(1 << 30))
    if num >= 1 << 20:
        return "%.1f MB" % (num / float(1 << 20))
    if num >= 1 << 10:
        return "%.1f KB" % (num / float(1 << 10))
    return "%d B" % int(num)


def _size_of(path) -> str:
    try:
        return _kb(Path(path).stat().st_size)
    except OSError:
        return "-"


def _det_enabled() -> bool:
    return str(os.environ.get("BOOTH_DET_ENABLED", "false")).strip().lower() in (
        "true", "1", "yes"
    )


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _run(cmd, timeout: float = 30.0, env: dict | None = None, cwd: str | None = None,
         stdin: str | None = None):
    """跑一个外部命令，永不抛异常（预检自己不能崩）。返回 (rc, stdout, stderr)。"""
    try:
        proc = subprocess.run(cmd, input=stdin, capture_output=True, text=True,
                              timeout=timeout, env=env, cwd=cwd)
        return proc.returncode, proc.stdout or "", proc.stderr or ""
    except FileNotFoundError as exc:
        return 127, "", str(exc)
    except subprocess.TimeoutExpired:
        return 124, "", "超时（%.0fs）" % timeout
    except Exception as exc:  # 兜底：任何异常都变成一条 rc，不让预检挂掉
        return 126, "", "%s: %s" % (type(exc).__name__, exc)


def _proc_cmdline(pid: int) -> str:
    try:
        raw = Path("/proc/%d/cmdline" % pid).read_bytes()
    except OSError:
        return ""
    return " ".join(part for part in raw.decode("utf-8", "replace").split("\x00") if part)


def _listening_inodes(port: int) -> set:
    """从 /proc/net/tcp{,6} 里找出监听该端口的 socket inode（纯标准库）。"""
    want = "%04X" % port
    inodes = set()
    for name in ("/proc/net/tcp", "/proc/net/tcp6"):
        try:
            lines = Path(name).read_text(encoding="utf-8", errors="replace").splitlines()[1:]
        except OSError:
            continue
        for line in lines:
            parts = line.split()
            if len(parts) < 10:
                continue
            local, state, inode = parts[1], parts[3], parts[9]
            if state != "0A":  # 0A = LISTEN
                continue
            if local.rsplit(":", 1)[-1].upper() == want:
                inodes.add(inode)
    return inodes


def _has_proc() -> bool:
    """现场是 Linux，/proc 一定在；开发机/容器里没有 —— 缺了要退化，不能崩。"""
    return os.path.isdir("/proc")


def _pids_for_inodes(inodes: set) -> list:
    hits: list[int] = []
    if not inodes or not _has_proc():
        return hits
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        pid = int(entry)
        fd_dir = "/proc/%d/fd" % pid
        try:
            fds = os.listdir(fd_dir)
        except OSError:
            continue
        for fd in fds:
            try:
                target = os.readlink(os.path.join(fd_dir, fd))
            except OSError:
                continue
            if target.startswith("socket:[") and target[len("socket:["):-1] in inodes:
                hits.append(pid)
                break
    return sorted(hits)


def device_holders(device) -> list:
    """谁拿着这个设备节点（相机被别人占时要把名字点出来）。"""
    try:
        node = str(Path(device).resolve())
    except Exception:
        node = str(device)
    hits = []
    if not _has_proc():
        return hits
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        pid = int(entry)
        fd_dir = "/proc/%d/fd" % pid
        try:
            fds = os.listdir(fd_dir)
        except OSError:
            continue
        for fd in fds:
            try:
                if os.readlink(os.path.join(fd_dir, fd)) == node:
                    hits.append((pid, _proc_cmdline(pid)))
                    break
            except OSError:
                continue
    return hits


SELF_SCRIPT = str((APP_DIR / "booth_server.py").resolve())


def _script_of(cmdline: str, pid: int | None = None) -> str | None:
    """从命令行里挑出那个 .py 的绝对路径（认「是不是我们这个服务」用）。

    只看文件名不够 —— 现场踩过：另一份旧原型也叫 booth_server.py，
    于是自检把它当成「我们的服务在跑」，实际访问到的是另一个应用。
    """
    if not cmdline:
        return None
    for token in cmdline.split():
        if not token.endswith(".py"):
            continue
        path = token
        if not os.path.isabs(path) and pid:
            try:
                cwd = os.readlink("/proc/%d/cwd" % pid)
                path = os.path.join(cwd, path)
            except OSError:
                pass
        try:
            return str(Path(path).resolve())
        except Exception:
            return path
    return None


def port_state(port: int) -> dict:
    """端口三态：free / self（本服务自己占着）/ other（别人占着）。"""
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        probe.bind(("0.0.0.0", port))
        return {"state": "free", "holders": []}
    except OSError:
        pass
    finally:
        probe.close()

    holders = []
    for pid in _pids_for_inodes(_listening_inodes(port)):
        cmdline = _proc_cmdline(pid)
        script = _script_of(cmdline, pid)
        holders.append({"pid": pid, "cmdline": cmdline, "script": script,
                        "self": bool(script) and script == SELF_SCRIPT})
    if not holders:
        return {"state": "other", "holders": []}
    return {"state": "self" if all(item["self"] for item in holders) else "other",
            "holders": holders}


def http_json(url: str, timeout: float = 10.0):
    """只读一次本机 HTTP 接口（标准库 urllib）。"""
    import urllib.request

    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8", "replace"))
    except Exception as exc:
        return {"__error__": "%s: %s" % (type(exc).__name__, exc)}

# --------------------------------------------------------------------------
# 报告
# --------------------------------------------------------------------------
class Report:
    """逐项收集检查结果。状态只有四种：PASS / FAIL / WARN / SKIP。"""

    def __init__(self) -> None:
        self.checks: list[dict] = []

    def add(self, group: str, status: str, title: str, detail: str = "", **extra) -> dict:
        item = {"group": group, "status": status, "title": str(title), "detail": detail}
        item.update(extra)
        self.checks.append(item)
        return item

    def counts(self) -> dict:
        out = {"PASS": 0, "FAIL": 0, "WARN": 0, "SKIP": 0}
        for item in self.checks:
            key = item["status"]
            out[key] = out.get(key, 0) + 1
        return out

    def render(self) -> str:
        lines: list[str] = []
        current = None
        for item in self.checks:
            if item["group"] != current:
                current = item["group"]
                pad = max(4, 44 - len(current) * 2)
                lines.append("")
                lines.append("── %s " % current + "─" * pad)
            line = "  [%s] %s" % (item["status"], item["title"])
            if item["detail"]:
                line += "  · %s" % item["detail"]
            lines.append(line)
        return "\n".join(lines)


# --------------------------------------------------------------------------
# 检查 1/10：代码与资源
# --------------------------------------------------------------------------
def check_files(report: Report) -> int:
    group = "代码与资源"
    missing = 0
    for rel in REQUIRED_FILES:
        path = APP_DIR / rel
        if path.is_file() and path.stat().st_size > 0:
            report.add(group, "PASS", path, _size_of(path))
        else:
            missing += 1
            report.add(group, "FAIL", path, "缺失或为空")
    for pattern, minimum, label in REQUIRED_SETS:
        found = sorted(item for item in glob.glob(str(APP_DIR / pattern)) if Path(item).is_file())
        detail = "%d 个：" % len(found) + "，".join("%s %s" % (Path(item).name, _size_of(item))
                                                   for item in found)
        title = "%s（%s）" % (pattern, label)
        if len(found) >= minimum:
            report.add(group, "PASS", title, detail)
        elif found:
            report.add(group, "WARN", title,
                       "只有 %d 个，期望 ≥ %d；%s" % (len(found), minimum, detail))
        else:
            missing += 1
            report.add(group, "FAIL", title, "一个都没有，期望 ≥ %d 个" % minimum)
    return missing


# --------------------------------------------------------------------------
# 检查 2/10：模型
# --------------------------------------------------------------------------
def check_models(report: Report) -> list:
    group = "模型"
    problems = []
    for key in ("seg", "clip", "det", "matte"):
        if key == "det" and not _det_enabled():
            report.add(group, "SKIP", "检测（人形框，预览/兜底）",
                       "BOOTH_DET_ENABLED=false，按配置跳过检测模型必需检查")
            continue
        env_name = MODEL_ENV[key]
        source = "环境变量 %s" % env_name if os.environ.get(env_name) else "worker 默认路径"
        path = Path(os.environ.get(env_name, MODEL_DEFAULTS[key]))
        title = "%s · %s" % (MODEL_LABELS[key], path.name)
        # 抠像缺失只判 WARN：成片会退回整幅取景，工位照常出图 ——
        # 但它确实丢了「只把人抠进来」这个卖点，所以也不能一声不吭。
        soft = (key == "matte")
        if not path.is_file():
            if not soft:
                problems.append(key)
            report.add(group, "WARN" if soft else "FAIL", title,
                       "不存在：%s（来源：%s）" % (path, source))
            continue
        size = path.stat().st_size
        detail = "%s  %s（来源：%s）" % (path, _size_of(path), source)
        if size < MODEL_MIN_BYTES:
            if not soft:
                problems.append(key)
            report.add(group, "WARN" if soft else "FAIL", title, "疑似截断/占位：" + detail)
        else:
            report.add(group, "PASS", title, detail)
    bpe = Path(BPE)
    if bpe.is_file() and bpe.stat().st_size > 1000:
        report.add(group, "PASS", "CLIP BPE 词表 · %s" % bpe.name,
                   "%s  %s" % (bpe, _size_of(bpe)))
    else:
        problems.append("bpe")
        report.add(group, "FAIL", "CLIP BPE 词表 · %s" % bpe.name, "缺失或过小：%s" % bpe)
    return problems

# --------------------------------------------------------------------------
# 检查 3/10：手势（OK 手势自动开拍）
# --------------------------------------------------------------------------
def check_gesture(report: Report) -> None:
    """如实报出「OK 手势自动开拍」当前能不能用。

    为什么单列一项：这是唯一一个**功能缺失**而不是配置问题的环节。目标里写着
    "检测到有人比出 OK 手势则自动开始拍摄倒计时"，但截至 2026-10-08 实测：

      · 板上**没有任何手部/姿态模型**（全盘 .onnx 只有 yolov8 检测、yoloe 分割、
        mobileclip 文本塔、modnet 抠像，外加机器狗的策略网络）；
      · 开放词汇分割也认不出手 —— 用 12 个提示词（hand / hands / open hand /
        raised hand / finger / fingers / fist / palm / arm / wrist …）逐条试，
        **全部 0 个目标**，最高分 0.069（arm），而同一帧 person 能出 23 个候选，
        所以不是模型没跑起来；
      · mobileclip 是**纯文本编码器**（图内节点全是 /text_encoder/，无视觉塔），
        算不了图像特征，零样本分类那条路也走不通；
      · 板子**上不了网**（github / huggingface 都连不通），不能现场下模型。

    所以这里一律 WARN 而不是 FAIL：装置**照常可用**，只是"开始"要靠参与者
    点屏幕按钮（点开始 → 倒计时 → 拍摄，这条链路实测跑通了）。误报成 FAIL 会让
    值班的人以为装置坏了；一声不吭则会让人在台上等一个永远不会来的手势。
    """
    group = "手势"
    enabled = str(os.environ.get("BOOTH_GESTURE_ENABLED", "")).strip().lower() in ("true", "1", "yes")
    model = str(os.environ.get("BOOTH_GESTURE_MODEL_PATH", "")).strip()
    model_ok = bool(model) and Path(model).is_file()

    if enabled and not model_ok:
        # 配置层其实会拒绝启动（config 里这条是硬门），这里提前点出来更直观。
        report.add(group, "WARN", "已启用手势但模型不可用",
                   "BOOTH_GESTURE_ENABLED=true 但 BOOTH_GESTURE_MODEL_PATH "
                   "缺失或文件不存在：%s" % (model or "(未设置)"))
    elif enabled and model_ok:
        report.add(group, "PASS", "手势已启用", "模型：%s" % model)
        report.add(group, "WARN", "**接线未完成**",
                   "on_gesture() 在生产代码里从未被调用：后台 tick 线程是 "
                   "ready → 倒计时 直连，手势适配器没有接进这条链路。模型到位后仍需接线。")
    else:
        report.add(group, "WARN", "OK 手势自动开拍：当前不可用（不影响装置使用）",
                   "板上无手部/姿态模型，开放词汇分割对 12 个手部提示词全 0，"
                   "mobileclip 无视觉塔，且板子无法联网取模型。")
        report.add(group, "PASS", "替代路径可用",
                   "参与者点屏幕「开始拍摄」→ 倒计时 → 拍摄（2026-10-08 实测端到端通过）。")


# --------------------------------------------------------------------------
# 检查 4/10：相机
# --------------------------------------------------------------------------
def check_camera(report: Report, service_state: dict | None) -> None:
    group = "相机"
    # 与 camera.resolve_device() 同口径：先认已知的 DICOTA，再收通用
    # `usb-*video-index0`。**别只认 DICOTA** —— 现场这台是 HHWei 4K USB 相机
    # (`usb-HHWei_Technology_Co.__Ltd._4K_USB_Camera_HHW001-video-index0`)，
    # 只 glob DICOTA 会让预检报一条"没找到相机"的假 WARN，而服务其实跑得好好的
    # （实测踩过：预检 WARN、/api/state 里 real=true 1920x1080，两者自相矛盾）。
    order = ("/dev/v4l/by-id/usb-*DICOTA*-video-index0",
             "/dev/v4l/by-id/usb-*video-index0")
    by_id: list[str] = []
    for pattern in order:
        hits = [p for p in sorted(glob.glob(pattern)) if Path(p).exists()]
        if hits:
            by_id = hits
            break
    fallback = Path("/dev/video1")
    if by_id:
        report.add(group, "PASS", "by-id 解析",
                   "%s → %s" % (by_id[0], os.path.realpath(by_id[0])))
    elif fallback.exists():
        report.add(group, "WARN", "by-id 解析",
                   "没有 /dev/v4l/by-id/usb-*video-index0 链接，将退回 %s（采集序号可能漂移）"
                   % fallback)
    else:
        report.add(group, "FAIL", "by-id 解析",
                   "既没有 USB 相机的 by-id 链接，也没有 /dev/video1")

    # 服务在跑：直接用它的实时读数，不二次占用相机（也避免把它踢下线）
    if service_state:
        cam = service_state.get("camera") or {}
        fourcc = str(cam.get("fourcc") or "").strip("\x00")
        note = "读数来自运行中的服务 /api/state（相机句柄归它持有）"
        if cam.get("ok"):
            report.add(group, "PASS", "能否打开",
                       "device=%s %sx%s %s %.0ffps  · %s"
                       % (cam.get("device"), cam.get("width"), cam.get("height"),
                          fourcc or "-", cam.get("fps") or 0, note))
        else:
            report.add(group, "FAIL", "能否打开",
                       "服务报告相机不可用：%s  · %s" % (cam.get("detail"), note))
        if fourcc == "MJPG":
            report.add(group, "PASS", "采集格式 MJPG", "规格 §3 必需（YUYV 高分辨率只有 1fps）")
        else:
            report.add(group, "FAIL", "采集格式 MJPG", "实际 fourcc=%r，必须 MJPG" % fourcc)
        report.add(group, "SKIP", "抓一帧（本进程）",
                   "服务持有相机，跳过二次占用；要看画面就开 /api/preview.mjpg")
        _check_powerline(report, cam.get("device"))
        return

    # 服务没跑：本进程按 camera.py 的同一条路径真开一次
    try:
        sys.path.insert(0, str(APP_DIR))
        import numpy  # noqa: F401
        from camera import PHOTO_SIZE, PREVIEW_SIZE, Camera
    except Exception as exc:
        report.add(group, "FAIL", "导入 camera.py", "%s: %s" % (type(exc).__name__, exc))
        return

    cam = Camera()
    try:
        status = cam.open(PREVIEW_SIZE)
    except Exception as exc:
        holders = device_holders(fallback)
        holder_txt = ("；占用者：" + ", ".join("pid=%d %s" % (pid, cmd or "(cmdline 读不到)")
                                              for pid, cmd in holders)) if holders else ""
        report.add(group, "FAIL", "能否打开", "%s: %s%s" % (type(exc).__name__, exc, holder_txt))
        return

    fourcc = str(status.fourcc or "").strip("\x00")
    report.add(group, "PASS", "能否打开", "device=%s %dx%d %s %.0ffps"
               % (status.device, status.width, status.height, fourcc or "-", status.fps))
    if status.detail and status.detail != "ok":
        report.add(group, "WARN", "打开时的提示", status.detail)
    if fourcc == "MJPG":
        report.add(group, "PASS", "采集格式 MJPG", "规格 §3 必需（YUYV 高分辨率只有 1fps）")
    else:
        report.add(group, "FAIL", "采集格式 MJPG", "实际 fourcc=%r，必须 MJPG" % fourcc)
    if (status.width, status.height) != tuple(PREVIEW_SIZE):
        report.add(group, "WARN", "实际预览分辨率", "%dx%d（请求 %dx%d，驱动给了别的）"
                   % (status.width, status.height, PREVIEW_SIZE[0], PREVIEW_SIZE[1]))
    try:
        started = time.perf_counter()
        frame = cam.read_bgr(warm=2)
        elapsed = (time.perf_counter() - started) * 1000.0
        report.add(group, "PASS", "抓一帧（预览 %dx%d）" % tuple(PREVIEW_SIZE),
                   "shape=%s mean=%.1f %.0fms" % (frame.shape, float(frame.mean()), elapsed))
    except Exception as exc:
        report.add(group, "FAIL", "抓一帧（预览）", "%s: %s" % (type(exc).__name__, exc))
    try:
        started = time.perf_counter()
        jpeg = cam.snapshot(PHOTO_SIZE, warm=2)
        elapsed = (time.perf_counter() - started)
        ok = elapsed < 4.0  # MJPG 实测 ~0.9s；掉到 YUYV 是 ~1fps 量级
        report.add(group, "PASS" if ok else "FAIL", "拍一张 %dx%d" % tuple(PHOTO_SIZE),
                   "%d bytes %.2fs%s" % (len(jpeg), elapsed,
                                         "" if ok else "（>4s，像掉到 YUYV 了）"))
    except Exception as exc:
        report.add(group, "FAIL", "拍一张 %dx%d" % tuple(PHOTO_SIZE),
                   "%s: %s" % (type(exc).__name__, exc))
    finally:
        cam.close()
    _check_powerline(report, status.device)


def _check_powerline(report: Report, device) -> None:
    """规格 §3 要求 power_line_frequency=1（50Hz，中国，防频闪）。"""
    if not device:
        return
    rc, out, err = _run(["v4l2-ctl", "-d", str(device), "-C", "power_line_frequency"], timeout=8)
    if rc == 0 and out.strip():
        value = out.strip().splitlines()[-1]
        detail = "%s（v4l2-ctl 读数）" % value
        if value.endswith("1") or "50 Hz" in value:
            report.add("相机", "PASS", "power_line_frequency=50Hz", detail)
        else:
            report.add("相机", "WARN", "power_line_frequency 不是 50Hz",
                       "%s · 规格 §3 要求 50Hz（防频闪）；camera.py 未设置该项，"
                       "现场可先手动：v4l2-ctl -d %s -c power_line_frequency=1" % (detail, device))
    else:
        report.add("相机", "SKIP", "power_line_frequency 读数",
                   "v4l2-ctl 不可用（%s）" % (err.strip() or rc))

# --------------------------------------------------------------------------
# 检查 5/10：AI 核
# --------------------------------------------------------------------------
def shoot_ai_checks(report: Report, health: dict, source: str) -> None:
    group = "AI 核"
    providers = health.get("providers") or []
    ep = bool(health.get("ep"))
    threads = health.get("ep_threads")
    fallback = health.get("ep_fallback") or health.get("fallback")
    raw = ["来源：%s" % source,
           "ep=%s ep_threads=%s providers=%s" % (ep, threads, providers),
           "fallback=%s" % (fallback or "无"),
           "det_ep=%s seg_model=%s" % (health.get("det_ep"), health.get("seg_model"))]
    load = health.get("load_ms")
    if isinstance(load, dict):
        raw.append("load_ms=%s" % json.dumps(load, ensure_ascii=False))
    report.add(group, "SKIP", "处理器与线程（原始读数）", " · ".join(raw))

    engaged = any("SpaceMIT" in str(name) for name in providers)
    if ep and not engaged:
        report.add(group, "FAIL", "EP 复核",
                   "ep=true 但 providers=%s 里没有 SpaceMIT —— 读数自相矛盾" % providers)
    elif ep:
        report.add(group, "PASS", "拿到 AI 核（SpaceMIT EP）", "providers=%s" % providers)
    else:
        hint = ("这是**降级**，不是崩溃：worker 拿不到核会静默退回 CPU（分割 ~1.16s vs ~0.2s）。"
                "常见原因：① 8080 上的 yolos-box 常驻 worker 先占了核；"
                "② 上一个 EP 进程崩溃后 Spine TCM 没释放；③ 同时开了第二份本服务。"
                "排查：ss -ltnp | grep 8080 · ps -ef | grep model_test_worker")
        report.add(group, "WARN", "拿不到 AI 核，已退回 CPU",
                   "原因：%s · %s" % (fallback or "(worker 未给出原因)", hint))

    if ep:
        if threads and int(threads) != 4:
            report.add(group, "WARN", "分割线程数非默认 4",
                       "ep_threads=%s（降档了；默认预算 seg=4 留 2 槽余量）" % threads)
        else:
            report.add(group, "PASS", "分割线程数", "ep_threads=%s（预算 4，留 2 槽余量）" % threads)
        if not _det_enabled():
            report.add(group, "SKIP", "检测模型",
                       "BOOTH_DET_ENABLED=false，按配置禁用检测模型")
        elif health.get("det_model"):
            report.add(group, "PASS", "检测模型已加载",
                       "%s（det_ep=%s）" % (health.get("det_model"), health.get("det_ep")))
        else:
            report.add(group, "WARN", "检测模型未加载", "预览人形框 / 无分割时的兜底会退化")
    # 抠像模型慢加载（第一次拍摄才建会话），所以这里只认 worker 报的默认路径；
    # 它在 CPU 上是**设计如此**，不是降级 —— 上 AI 核会让进程硬崩（踩坑记录 坑 9）。
    if health.get("matte_model"):
        report.add(group, "PASS", "抠像模型（CPU）",
                   "%s（device=%s）" % (health.get("matte_model"), health.get("matte_device")))
    else:
        report.add(group, "WARN", "抠像模型未配置",
                   "成片会退回整幅取景；「只把人抠进来」失效")
    report.add(group, "SKIP", "线程预算与降档阶梯",
               "thread_budget=%s ep_ladder=%s"
               % (json.dumps(health.get("thread_budget") or {}, ensure_ascii=False),
                  health.get("ep_ladder")))


def check_ai(report: Report, ctx: dict) -> None:
    group = "AI 核"
    if ctx["skip_ai"]:
        report.add(group, "SKIP", "AI 核探测", "按 --skip-ai 跳过")
        return

    if ctx["service"] and not ctx["probe_ai"]:
        state = ctx["service_state"] or {}
        infer = state.get("infer")
        if isinstance(infer, dict) and "ep" in infer:
            if infer.get("ep") is None and not infer.get("providers"):
                # 开机后头几秒：worker 还在预热，此刻 ep 是「未知」而不是「CPU」。
                # 别把未知报成降级（诚实优先：宁可说不知道）。
                report.add(group, "WARN", "服务还在预热、AI 核未知",
                           "worker 尚未回话（preloaded=%s preload_error=%s）；"
                           "等几秒再跑一次预检" % (infer.get("preloaded"),
                                                   infer.get("preload_error")))
                return
            shoot_ai_checks(report, infer,
                            "运行中的服务 /api/state（未另开会话，不抢 AI 核）")
            return
        report.add(group, "WARN", "读服务状态失败",
                   "服务在跑但 /api/state 没给出 infer：%s" % (state.get("__error__") or "(空)"))

    if not WORKER_PY.is_file():
        report.add(group, "FAIL", "worker 脚本", "不存在：%s" % WORKER_PY)
        return
    env = dict(os.environ)
    env.setdefault("PYTHONUNBUFFERED", "1")
    started = time.perf_counter()
    rc, out, err = _run([PYTHON, str(WORKER_PY)], timeout=ctx["ai_timeout"], env=env,
                        cwd=str(APP_DIR), stdin='{"id":1,"op":"health"}\n')
    elapsed = time.perf_counter() - started
    payload = None
    for line in out.splitlines():
        if line.startswith(PROTOCOL_PREFIX):
            try:
                payload = json.loads(line[len(PROTOCOL_PREFIX):])
            except Exception:
                payload = None
    if payload is None:
        tail = (out.strip().splitlines() or [""])[-1]
        report.add(group, "FAIL", "另起 worker 取 health",
                   "rc=%s %.1fs；没读到 %s 协议行；stdout 尾=%r stderr 尾=%r"
                   % (rc, elapsed, PROTOCOL_PREFIX, tail[-200:], err.strip()[-200:]))
        return
    if not payload.get("ok"):
        report.add(group, "FAIL", "另起 worker 取 health", "worker 回：%s" % payload)
        return
    shoot_ai_checks(report, payload, "另起子进程 infer/worker.py（%.1fs）" % elapsed)


# --------------------------------------------------------------------------
# 检查 6/10：端口
# --------------------------------------------------------------------------
def check_port(report: Report, ctx: dict) -> None:
    group = "端口"
    port = ctx["port"]
    state = ctx["port_state"]
    if state["state"] == "free":
        report.add(group, "PASS", "%d 端口空闲" % port,
                   "服务当前未启动（systemctl start devcon-booth 即可）")
        return
    names = ", ".join("pid=%d %s" % (item["pid"], item["cmdline"] or "(cmdline 读不到)")
                      for item in state["holders"]) or "占用进程解析不出来"
    foreign = [item for item in state["holders"] if not item.get("self")]
    if state["state"] == "self":
        report.add(group, "PASS", "%d 端口被本服务占用" % port,
                   "%s（devcon-booth 正在跑，属正常；要冷启动就先 systemctl stop）" % names)
        return
    if foreign and any(it.get("script") for it in foreign):
        # 文件名一样、路径不一样（旧原型）也要认出来，否则「服务在跑」是假的
        detail = "；".join("pid=%d 跑的是 %s（不是 %s）" % (it["pid"], it.get("script"), SELF_SCRIPT)
                           for it in foreign if it.get("script"))
        report.add(group, "FAIL", "%d 端口被别的程序占用" % port,
                   "%s —— 先停掉它再开场（本装置固定用 8090；8080 上的 yolos-box 不要动）" % detail)
        return
    report.add(group, "FAIL", "%d 端口被别的进程占用" % port,
               "%s —— 先停掉它再开场（本装置固定用 8090；8080 上的 yolos-box 不要动）" % names)

# --------------------------------------------------------------------------
# 检查 7/10：磁盘与留存
# --------------------------------------------------------------------------
def _avg_record_bytes() -> float | None:
    """用现有素材估一个「每人占多少字节」：captures + photos + thumbs 各一份。"""
    sizes = []
    for sub in ("captures", "photos", "thumbs"):
        for path in glob.glob(str(DATA_DIR / sub / "*.jpg")):
            try:
                sizes.append(Path(path).stat().st_size)
            except OSError:
                continue
    if not sizes:
        return None
    return float(sum(sizes)) / float(len(sizes)) * 3.0


def check_disk(report: Report) -> None:
    group = "磁盘与留存"
    try:
        usage = os.statvfs(str(DATA_DIR if DATA_DIR.exists() else APP_DIR))
    except Exception as exc:
        report.add(group, "FAIL", "磁盘读数", "%s: %s" % (type(exc).__name__, exc))
        return
    total = usage.f_blocks * usage.f_frsize
    free = usage.f_bavail * usage.f_frsize
    detail = "可用 %s / 共 %s（%s）" % (_kb(free), _kb(total), DATA_DIR)
    if free < 1 << 30:
        report.add(group, "FAIL", "可用空间不足 1 GB", detail + " —— 照片会写不进去")
    elif free < 5 << 30:
        report.add(group, "WARN", "可用空间偏低（< 5 GB）", detail)
    else:
        report.add(group, "PASS", "可用空间", detail)

    avg = _avg_record_bytes()
    if avg:
        report.add(group, "PASS", "按留存期估算容量",
                   "实测均值 %.0f KB/人 · 可用 %s → 约 %s 人（%d 小时留存）"
                   % (avg / 1024.0, _kb(free), "{:,}".format(int(free / avg)),
                      RETENTION_SPEC_HOURS))
    else:
        report.add(group, "SKIP", "按留存期估算容量",
                   "data/ 里还没有成片；按 150 KB/人 粗估约 %s 人"
                   % "{:,}".format(int(free // 153600)))

    try:
        sys.path.insert(0, str(APP_DIR))
        import store as store_mod  # store.py 只 import 标准库，安全

        default_hours = inspect.signature(store_mod.Store.__init__).parameters[
            "retention_hours"].default
        if int(default_hours) == RETENTION_SPEC_HOURS:
            report.add(group, "PASS", "留存期 %d 小时（1 天）" % RETENTION_SPEC_HOURS,
                       "store.Store 默认值一致；booth_server 未覆盖该参数")
        else:
            report.add(group, "FAIL", "留存期与规格不一致",
                       "store.Store 默认 %s 小时，规格 §6 要求 %d 小时"
                       % (default_hours, RETENTION_SPEC_HOURS))
    except Exception as exc:
        report.add(group, "FAIL", "留存期复核", "%s: %s" % (type(exc).__name__, exc))

    if os.environ.get("BOOTH_RETENTION_HOURS"):
        report.add(group, "WARN", "BOOTH_RETENTION_HOURS 不被读取",
                   "代码里没人读这个变量（booth_server 用 store.Store 的默认值），"
                   "设了也不生效，别被它骗了")


# --------------------------------------------------------------------------
# 检查 8/10：数据目录
# --------------------------------------------------------------------------
def check_data(report: Report) -> None:
    group = "数据目录"
    if DATA_DIR.is_dir():
        report.add(group, "PASS", "目录存在", "%s" % DATA_DIR)
    else:
        report.add(group, "FAIL", "目录存在", "不存在：%s（Store 会自建，但仍需排查）" % DATA_DIR)

    probe = DATA_DIR / ".preflight-write-test"
    try:
        probe.parent.mkdir(parents=True, exist_ok=True)
        probe.write_text("ok", encoding="utf-8")
        probe.read_text(encoding="utf-8")
        probe.unlink()
        report.add(group, "PASS", "可写", "写入并删除 %s 成功" % probe.name)
    except Exception as exc:
        report.add(group, "FAIL", "可写", "%s: %s（照片会写不进去）" % (type(exc).__name__, exc))

    counter_path = DATA_DIR / "counter.json"
    counter_value = None
    if counter_path.is_file():
        try:
            payload = json.loads(counter_path.read_text(encoding="utf-8"))
            counter_value = int(payload.get("value", 0))
            year = payload.get("year")
            detail = "year=%s value=%s（下一个编号 SM-%s-%04d）" % (year, counter_value,
                                                                   year, counter_value + 1)
            if year != datetime.now().year:
                report.add(group, "WARN", "编号计数器年份不符",
                           detail + " —— 年份对不上会从 1 重新编号")
            else:
                report.add(group, "PASS", "编号计数器", detail)
        except Exception as exc:
            report.add(group, "FAIL", "编号计数器解析", "%s: %s" % (type(exc).__name__, exc))
    else:
        report.add(group, "WARN", "编号计数器",
                   "%s 还不存在（首次拍摄从 SM-XXXX-0001 起）" % counter_path.name)

    records_path = DATA_DIR / "records.jsonl"
    if records_path.is_file():
        text = records_path.read_text(encoding="utf-8")
        lines = [line for line in text.splitlines() if line.strip()]
        bad = 0
        for line in lines:
            try:
                json.loads(line)
            except Exception:
                bad += 1
        detail = "%d 条记录 %s" % (len(lines), _size_of(records_path))
        if bad:
            report.add(group, "WARN", "records.jsonl",
                       detail + "，其中 %d 行解析不了（会被跳过）" % bad)
        else:
            last = json.loads(lines[-1]).get("id") if lines else "-"
            report.add(group, "PASS", "records.jsonl", detail + "，最后一条 %s" % last)
    else:
        report.add(group, "SKIP", "records.jsonl", "%s 不存在（还没拍过）" % records_path.name)

    photos = [item for item in glob.glob(str(DATA_DIR / "photos" / "*.jpg"))
              if not item.endswith("-wide.jpg")]
    detail = "photos/ 里 %d 张成片" % len(photos)
    if counter_value is not None and counter_value < len(photos):
        report.add(group, "WARN", "编号与成片数量不符",
                   detail + "，但计数器只到 %d（可能有历史残留）" % counter_value)
    else:
        report.add(group, "PASS", "成片数量", detail)

# --------------------------------------------------------------------------
# 检查 9/10：零 pip 依赖（★ 分进程，见文件头）
# --------------------------------------------------------------------------
WORKER_IMPORTS = ("numpy", "PIL", "onnxruntime", "spacemit_ort")   # 顺序不可换
CAMERA_IMPORTS = ("numpy", "cv2")
APP_IMPORTS = ("booth_server", "camera", "infer_client", "store",
               "render.qr", "render.stylize", "render.card", "render.watermark")

_IMPORT_PROBE_CODE = """
import importlib, json, sys
names = __MODULES__
out = {}
for name in names:
    try:
        mod = importlib.import_module(name)
        out[name] = {"ok": True, "version": str(getattr(mod, "__version__", "?")),
                     "file": getattr(mod, "__file__", "?")}
    except Exception as exc:
        out[name] = {"ok": False, "error": type(exc).__name__ + ": " + str(exc)}
print(json.dumps({"ok": all(v["ok"] for v in out.values()), "modules": out,
                  "prefix": sys.prefix, "executable": sys.executable},
                 ensure_ascii=False))
"""


def _import_probe(modules, cwd: str) -> dict:
    """在**独立子进程**里导入一组模块，返回结构化结果（不污染本进程）。"""
    code = _IMPORT_PROBE_CODE.replace("__MODULES__", repr(tuple(modules)))
    rc, out, err = _run([PYTHON, "-c", code], timeout=120, cwd=cwd)
    try:
        return json.loads(out.strip().splitlines()[-1])
    except Exception:
        return {"ok": False, "modules": {},
                "error": "rc=%s stderr=%s" % (rc, err.strip()[-300:])}


def check_imports(report: Report) -> None:
    group = "零 pip 依赖"
    rc, out, _ = _run([PYTHON, "-V"], timeout=15)
    version = out.strip() or "?"
    same = os.path.realpath(sys.executable) == os.path.realpath(PYTHON)
    venv = os.environ.get("VIRTUAL_ENV")
    if same and not venv:
        report.add(group, "PASS", "解释器", "%s · %s（无 venv）" % (PYTHON, version))
    else:
        report.add(group, "WARN", "解释器不是 %s" % PYTHON,
                   "当前 sys.executable=%s · %s%s" % (sys.executable, version,
                                                      "（VIRTUAL_ENV 已设置）" if venv else ""))
    if venv:
        report.add(group, "FAIL", "禁止 venv", "VIRTUAL_ENV=%s（规格 §10：不许 venv）" % venv)

    for label, modules in (("worker 侧依赖", WORKER_IMPORTS),
                           ("相机侧依赖", CAMERA_IMPORTS),
                           ("应用模块", APP_IMPORTS)):
        result = _import_probe(modules, str(APP_DIR))
        mods = result.get("modules") or {}
        parts = []
        for name in modules:
            info = mods.get(name) or {}
            parts.append("%s %s" % (name, info.get("version") if info.get("ok")
                                    else "导入失败"))
        detail = "，".join(parts)
        if result.get("ok"):
            report.add(group, "PASS", "%s 全部可导入" % label, detail)
        else:
            broken = "，".join("%s→%s" % (name, (mods.get(name) or {}).get("error", "?"))
                               for name in modules if not (mods.get(name) or {}).get("ok"))
            report.add(group, "FAIL", "%s 导入失败" % label,
                       "%s；%s" % (detail, result.get("error") or broken))


# --------------------------------------------------------------------------
# 检查 10/10：附加（大屏 / 开机自启 / 二维码地址）
# --------------------------------------------------------------------------
def check_extras(report: Report, ctx: dict) -> None:
    group = "附加"
    import shutil

    chromium = shutil.which("chromium") or shutil.which("chromium-browser")
    if chromium:
        rc, out, _ = _run([chromium, "--version"], timeout=20)
        report.add(group, "PASS", "chromium 已安装",
                   "%s · %s" % (chromium, out.strip() or "version 读取失败"))
    else:
        report.add(group, "WARN", "chromium 未安装", "大屏 /stage 起不来（服务本身不受影响）")

    unit = Path("/etc/systemd/system/devcon-booth.service")
    if unit.is_file():
        rc, out, _ = _run(["systemctl", "is-enabled", "devcon-booth.service"], timeout=15)
        enabled = out.strip()
        rc2, out2, _ = _run(["systemctl", "is-active", "devcon-booth.service"], timeout=15)
        if enabled == "enabled":
            report.add(group, "PASS", "devcon-booth 开机自启",
                       "is-enabled=enabled · is-active=%s" % (out2.strip() or "?"))
        else:
            report.add(group, "WARN", "devcon-booth 未 enable",
                       "is-enabled=%s —— 断电重启不会自动恢复；执行 systemctl enable devcon-booth"
                       % (enabled or "?"))
    else:
        report.add(group, "WARN", "systemd 单元未安装",
                   "缺 %s（见 deploy/README.md 的上线步骤）" % unit)

    state = ctx["service_state"] or {}
    link_base = state.get("link_base")
    if link_base:
        ok = "127.0.0.1" not in link_base and "localhost" not in link_base
        report.add(group, "PASS" if ok else "WARN", "二维码取图地址",
                   "服务上报 %s%s" % (link_base, "" if ok else
                                      " —— 指向回环地址，手机扫不到；把网络接好或设 BOOTH_PUBLIC_HOST"))
        return
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect(("10.255.255.255", 1))
        ip = probe.getsockname()[0]
    except Exception:
        ip = "127.0.0.1"
    finally:
        probe.close()
    ok = not ip.startswith("127.")
    report.add(group, "PASS" if ok else "WARN", "二维码取图地址",
               "http://%s:%d（服务未跑，预检自行探测）%s"
               % (ip, ctx["port"], "" if ok else " —— 网络没通，二维码会指向回环地址"))

# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------
def parse_args(argv: list) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="装置 5 · 赛博开发者证件照 —— 开场前预检（有 FAIL 即非零退出）")
    parser.add_argument("--json", action="store_true", help="输出机器可读 JSON（彩排留档用）")
    parser.add_argument("--skip-ai", action="store_true", help="跳过 AI 核探测（几秒出结果）")
    parser.add_argument("--probe-ai", action="store_true",
                        help="即使本服务在跑，也另起一个 worker 复核（会多占 AI 核，慎用）")
    parser.add_argument("--timeout", type=float, default=240.0, help="AI 核探测超时秒数（默认 240）")
    return parser.parse_args(argv)


def main(argv: list | None = None) -> int:
    _utf8_streams()
    args = parse_args(list(sys.argv[1:] if argv is None else argv))
    if args.skip_ai and args.probe_ai:
        print("--skip-ai 与 --probe-ai 不能同时用", file=sys.stderr)
        return 2

    if not _has_proc():
        # 这份清单是给现场那台 K3 用的：要读 /proc、/sys、V4L2 节点、systemd。
        # 在没有 /proc 的机器上继续跑只会得到一堆看不懂的 FAIL，
        # 不如直接说清楚「你跑错机器了」。
        print("这个工具要在现场的 K3 上跑（需要 /proc、/dev/video*、systemd）。")
        print("当前机器不满足：%s" % sys.platform)
        return 2

    started_at = _now()
    report = Report()

    # 先认端口：相机与 AI 核检查需要知道服务是不是已经在跑
    state = port_state(PORT)
    service = state["state"] == "self"
    service_state = None
    if service:
        service_state = http_json("http://127.0.0.1:%d/api/state" % PORT)
    ctx = {"port": PORT, "port_state": state, "service": service,
           "service_state": service_state, "skip_ai": args.skip_ai,
           "probe_ai": args.probe_ai, "ai_timeout": args.timeout}

    check_files(report)
    check_models(report)
    check_gesture(report)
    check_camera(report, service_state if service else None)
    check_ai(report, ctx)
    check_port(report, ctx)
    check_disk(report)
    check_data(report)
    check_imports(report)
    check_extras(report, ctx)

    counts = report.counts()
    failed = counts["FAIL"] > 0
    exit_code = 1 if failed else 0
    finished_at = _now()

    if args.json:
        payload = {
            "ok": not failed, "exit_code": exit_code, "started_at": started_at,
            "finished_at": finished_at, "host": socket.gethostname(),
            "app_dir": str(APP_DIR), "data_dir": str(DATA_DIR), "port": PORT,
            "python": PYTHON, "summary": counts, "checks": report.checks,
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return exit_code

    print("=" * 62)
    print("装置 5 · 赛博开发者证件照 —— 开场前预检")
    print("主机 %s · 解释器 %s · %s" % (socket.gethostname(), PYTHON, finished_at))
    print("应用 %s · 数据 %s · 端口 %d" % (APP_DIR, DATA_DIR, PORT))
    print("=" * 62)
    print(report.render())
    print("")
    print("-" * 62)
    print("汇总：PASS %d · WARN %d · FAIL %d · SKIP %d"
          % (counts["PASS"], counts["WARN"], counts["FAIL"], counts["SKIP"]))
    if failed:
        print("结论：FAIL —— %d 项未通过，修好再开场（退出码 %d）" % (counts["FAIL"], exit_code))
    elif counts["WARN"]:
        print("结论：PASS（有 %d 项 WARN）—— 可以开场；WARN 逐条看上面说明" % counts["WARN"])
    else:
        print("结论：PASS —— 全部通过，可以开场")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
