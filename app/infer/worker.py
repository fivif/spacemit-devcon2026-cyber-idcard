#!/usr/bin/env python3
"""装置 5 · 推理 worker（独立进程）

为什么必须是独立进程：
  1. `cv2` 与 `spacemit_ort` 同进程会让 ORT 日志管理器崩溃（import-order crash）；
  2. `onnxruntime` 必须先于 `spacemit_ort` 被 import，反之会话创建直接失败。
因此本文件**绝不 import cv2**；抓帧由 booth_server.py 负责。

协议：stdin/stdout，一行一个 JSON。
  请求  {"id":1,"op":"seg","image":"/tmp/a.jpg","prompts":["person"],"conf":0.20}
  响应  {"id":1,"op":"seg","ok":true,"ms":176.5,"ep":true,"persons":[...],"mask_b64":"..."}
错误不会杀死进程：返回 {"ok":false,"error":"..."} 后继续读下一行。

模型常驻内存，只加载一次。
"""

from __future__ import annotations

import base64
import io
import json
import os
import re
import signal
import sys
import time
import gc
from pathlib import Path

import numpy as np
from PIL import Image, ImageFilter

# ---- 路径 ----------------------------------------------------------------
HERE = Path(__file__).resolve().parent
APP = HERE.parent
sys.path.insert(0, str(HERE))
# 主体选择规则放在 app/subject_rules.py（两个进程共用一份配置）；本文件在
# app/infer/ 下，够不到上一层，所以补一条路径。名字用别名避免与下面的
# DEFAULTS（模型路径表）撞名。
sys.path.insert(0, str(APP))

from tokenizer import ClipTokenizer  # noqa: E402

try:
    from subject_rules import rules as _subject_rules      # noqa: E402
except Exception:      # 规则模块缺失/写坏绝不能拖垮推理 worker
    def _subject_rules(force: bool = False) -> dict:       # type: ignore[misc]
        return {
            "anchor": [0.5, 0.5],
            "weights": {"area": 0.444, "center": 0.333, "completeness": 0.222},
            "area_full": 0.45,
            "min_box_h": 0.22,
            "pad": 0.06,
        }

DEFAULTS = {
    "seg_model": os.environ.get("BOOTH_SEG_MODEL", "/opt/devcon/assets/models/yoloe-v8s-seg.dynq.onnx"),
    "clip_model": os.environ.get("BOOTH_CLIP_MODEL", "/opt/devcon/assets/models/mobileclip.q.onnx"),
    "det_model": os.environ.get("BOOTH_DET_MODEL", "/usr/share/yolo-demo/yolov8/yolov8n.q.onnx"),
    "gesture_model": os.environ.get("BOOTH_GESTURE_MODEL", "/opt/devcon/assets/models/gesture/yolov5_gesture.q.onnx"),
    "matte_model": os.environ.get("BOOTH_MATTE_MODEL",
                                       "/opt/devcon/assets/models/modnet.onnx"),
    "bpe": os.environ.get("BOOTH_BPE", str(APP / "assets" / "clip" / "bpe_merges.txt")),
    "max_nc": 80,
    "imgsz": 640,
}

# AI 核线程预算。实测（2026-09-14，K3 Pico-ITX）：
#  * 全板共 **8 个 AI 核线程槽**，进程内**多个 EP 会话共享**这个池子；
#  * 会话一旦创建就**一直占着核**，直到进程退出（不是按次释放）；
#  * 申请数超过余量时，EP 报 "Not enough available AI cores"
#    并**静默退回 CPU**（yoloe-seg 174ms → 1160ms，慢 6.6 倍）；
#  * 只有 `session.get_providers()` 能事后分辨用了谁 —— 所以必须复核。
# 实测最优切分（干净进程）：seg=4 / det=4 → seg 193ms、det 14ms；
# 留 2 个槽余量更稳，故默认 seg=4 / det=2 → seg 194ms、det 23ms。
EP_THREADS = {
    "seg": int(os.environ.get("BOOTH_EP_THREADS_SEG", "4")),
    "det": int(os.environ.get("BOOTH_EP_THREADS_DET", "2")),
    "gesture": int(os.environ.get("BOOTH_EP_THREADS_GESTURE", "2")),
}
# 拿不到目标线程数时逐级降档，而不是直接掉到 CPU
EP_THREAD_LADDER = tuple(
    int(x) for x in os.environ.get("BOOTH_EP_THREADS", "8,4,2,1").split(",") if x.strip()
)

# 协议哨兵：EP 失败时会往 stdout 打 "EP Error ..." 噪声，客户端只认带前缀的行。
PROTOCOL_PREFIX = "@@J "

GESTURE_LABELS = (
    "ok", "stop", "palm", "like", "dislike", "no_gesture", "call", "fist",
    "four", "mute", "one", "peace", "peace_inverted", "rock", "stop_inverted",
    "three", "three2", "two_up", "two_up_inverted",
)

_state: dict = {}


def _release_session(name: str) -> None:
    """释放一个 ORT 会话，让 SpaceMIT EP 归还它占用的 AI 核槽。"""
    session = _state.pop(name, None)
    if session is not None:
        del session
        gc.collect()


def _switch_mode(mode: str) -> None:
    """在 seg / gesture 两个 EP 会话之间做独占切换。"""
    current = _state.get("mode")
    if current == mode:
        return
    if mode == "gesture":
        _release_session("seg")
        _state["seg"] = None
        _state["seg_img_input"] = None
        _state["seg_text_input"] = None
        _state["gesture"], load_ms, info = _session(
            DEFAULTS["gesture_model"],
            cpu_only=os.environ.get("BOOTH_GESTURE_DEVICE", "ep").strip().lower() == "cpu",
            prefer_threads=EP_THREADS["gesture"],
        )
        _state["load_ms"]["gesture_switch_ms"] = round(load_ms, 1)
        _state["gesture_ep"] = bool(info.get("ep"))
        _state["gesture_ep_threads"] = info.get("ep_threads")
        _state["gesture_providers"] = info.get("providers", [])
        _state["gesture_ep_fallback"] = info.get("fallback")
        _state["gesture_device"] = "ep" if info.get("ep") else "cpu"
        _state["gesture_input"] = _state["gesture"].get_inputs()[0].name
    elif mode == "seg":
        _release_session("gesture")
        _state["gesture"] = None
        _state["gesture_input"] = None
        _state["seg"], load_ms, info = _session(
            DEFAULTS["seg_model"], prefer_threads=EP_THREADS["seg"]
        )
        _state["load_ms"]["seg_switch_ms"] = round(load_ms, 1)
        _state["seg_ep_threads"] = info.get("ep_threads")
        _state["providers"] = info.get("providers", [])
        _state["ep"] = bool(info.get("ep"))
        _state["ep_threads"] = info.get("ep_threads")
        _state["ep_fallback"] = info.get("fallback")
        seg_inputs = [item.name for item in _state["seg"].get_inputs()]
        _state["seg_img_input"] = seg_inputs[0]
        _state["seg_text_input"] = next(name for name in seg_inputs if "txt" in name or "text" in name)
    else:
        raise ValueError("未知推理模式：%s" % mode)
    _state["mode"] = mode


# ---- ONNX 会话 ------------------------------------------------------------
def _ep_available() -> bool:
    """spacemit_ort 是否可用。import 顺序不可调换。"""
    try:
        import spacemit_ort  # noqa: F401  必须在 onnxruntime 之后、建会话之前
        return True
    except Exception:
        return False


def release_stale_tcm(core_id: int = 0) -> bool:
    """释放上一个 EP 进程崩溃后残留的 Spine TCM 块（否则 AI 核一直显示被占）。"""
    try:
        import ctypes

        lib = ctypes.CDLL("libspine_tcm.so.3")
        lib.spine_tcm_runtime_mem_force_release(ctypes.c_int(core_id))
        return True
    except Exception:
        return False


_CORE_RE = re.compile(r"core id\s+(\d+)")


def _is_runtime_ep_error(exc: BaseException) -> bool:
    """判断是不是「EP 运行期」故障（区别于「建会话时拿不到核」）。"""
    text = str(exc).lower()
    return any(key in text for key in ("tcm buffer", "spinesubgraph", "spacemitexecutionprovider",
                                       "not enough available ai cores"))


def recover_runtime(exc: BaseException) -> dict:
    """运行期 EP 故障自救：释放报错的那块 TCM → 重建会话 → 交给调用方重试一次。

    为什么要这么做：会话建好之后如果 TCM 缓冲区被泄漏（典型场景：另一个推理进程
    被 kill -9），旧会话会**永久**返回 "tcm buffer acquire failed"，而 health 依然
    报 ep=true —— 大屏徽章会显示 EP SpaceMIT，实际每一张都在 CPU 上跑。
    与其撒谎，不如自愈一次；自愈不了也要如实上报。
    """
    match = _CORE_RE.search(str(exc))
    cores = [int(match.group(1))] if match else [0]
    released = {core: release_stale_tcm(core) for core in cores}
    _state.clear()
    load_all()
    return {"released": released, "rebuilt": True, "ep": _state.get("ep"),
            "ep_threads": _state.get("ep_threads")}


def _session(path: str, cpu_only: bool = False, prefer_threads: int | None = None) -> tuple[object, float, dict]:
    """建会话：优先 SpaceMIT AI 核，失败则逐级降线程数，最后退回 CPU。

    K3 只允许一个进程独占 4 个 AI 核。当其它进程（如 yolos-box 常驻 worker）占着核时，
    EP 会报 "Not enough available AI cores" 并**静默退回 CPU** —— 实测慢 35 倍。
    所以这里显式指定线程数、逐级降档，并且**用 session.get_providers() 复核**，
    绝不把 CPU 的耗时当成 AI 核的读数报上去。
    """
    import onnxruntime as ort

    if not Path(path).is_file():
        raise FileNotFoundError(path)

    info: dict = {"ep": False, "ep_threads": None, "fallback": None, "providers": []}
    attempts: list = []
    # cpu_only: 文本编码器必须留在 CPU。它很小，而一个 EP 会话就会占住 AI 核，
    # 把核让给分割模型才是正确取舍（实测：先建 clip 的 EP 会话会让 seg 拿不到核）。
    if not cpu_only and _ep_available():
        preferred = prefer_threads or EP_THREADS["seg"]
        shared_threads = os.environ.get("BOOTH_EP_SHARED_THREADS", "").strip()
        if shared_threads:
            preferred = max(1, min(8, int(shared_threads)))
        # 别照单全收：全板只有 8 个槽，申请 99 时 EP 会「答应得下、跑得极慢」，
        # 于是徽章写着 EP、耗时却是 CPU 量级 —— 那正是不诚实的读数。
        preferred = min(preferred, max(EP_THREAD_LADDER))
        ladder = [t for t in (preferred, *EP_THREAD_LADDER) if 1 <= t <= preferred]
        seen: set[int] = set()
        attempts += [("ep", t) for t in ladder if not (t in seen or seen.add(t))]
    attempts.append(("cpu", None))

    last_error = None
    for kind, threads in attempts:
        opts = ort.SessionOptions()
        opts.log_severity_level = 3
        opts.intra_op_num_threads = max(1, int(os.environ.get("BOOTH_ORT_THREADS", "1")))
        opts.inter_op_num_threads = 1
        if kind == "ep":
            providers = ["SpaceMITExecutionProvider", "CPUExecutionProvider"]
            ep_options = {"SPACEMIT_EP_INTRA_THREAD_NUM": str(threads)}
            if os.environ.get("BOOTH_EP_SHARED_THREADS", "").strip():
                ep_options["SPACEMIT_EP_USE_GLOBAL_INTRA_THREAD"] = "1"
            provider_options = [ep_options, {}]
        else:
            providers, provider_options = ["CPUExecutionProvider"], [{}]
        started = time.perf_counter()
        try:
            session = ort.InferenceSession(
                path, sess_options=opts, providers=providers, provider_options=provider_options
            )
        except Exception as exc:
            last_error = f"{type(exc).__name__}: {exc}"
            continue
        load_ms = (time.perf_counter() - started) * 1000.0
        active = list(session.get_providers())
        engaged = any("SpaceMIT" in name for name in active)
        if kind == "ep" and not engaged:
            last_error = f"EP 未生效（实际 {active}），AI 核可能被其它进程占用"
            continue
        info["requested_threads"] = threads
        info.update(ep=engaged, ep_threads=threads if engaged else None, providers=active)
        if last_error:
            info["fallback"] = last_error
        return session, load_ms, info

    raise RuntimeError(last_error or f"无法创建会话：{path}")


def load_all():
    if _state:
        return
    load: dict[str, float] = {}

    # 进程级轮换后，Spine TCM 偶尔会保留上一个已退出 worker 的块。
    # 必须在新 EP 会话创建前清理，否则会出现“health 显示 EP、首个
    # run 永久卡住并报 tcm buffer acquire failed”的假健康状态。
    for core_id in range(8):
        release_stale_tcm(core_id)

    mode = os.environ.get("BOOTH_INFER_MODE", "seg").strip().lower()
    if mode == "gesture":
        # 手势阶段只加载手势模型。进程退出时由 InferWorker 交还全部
        # EP 资源，下一次 seg 请求会启动全新的分割进程。
        _state["mode"] = "gesture"
        _state["tokenizer"] = None
        _state["clip"] = None
        _state["seg"] = None
        _state["det"] = None
        _state["gesture"] = None
        gesture_info = {}
        if (os.environ.get("BOOTH_GESTURE_ENGINE", "") == "official_yolo"
                and Path(DEFAULTS["gesture_model"]).is_file()):
            gesture_cpu = os.environ.get("BOOTH_GESTURE_DEVICE", "ep").strip().lower() == "cpu"
            _state["gesture"], load["gesture_ms"], gesture_info = _session(
                DEFAULTS["gesture_model"], cpu_only=gesture_cpu,
                prefer_threads=EP_THREADS["gesture"]
            )
        _state["gesture_ep"] = bool(gesture_info.get("ep"))
        _state["gesture_ep_threads"] = gesture_info.get("ep_threads")
        _state["gesture_providers"] = gesture_info.get("providers", [])
        _state["gesture_ep_fallback"] = gesture_info.get("fallback")
        _state["gesture_device"] = "ep" if gesture_info.get("ep") else None
        _state["gesture_input"] = (_state["gesture"].get_inputs()[0].name
                                    if _state["gesture"] is not None else None)
        _state["providers"] = []
        _state["ep"] = False
        _state["ep_threads"] = None
        _state["ep_fallback"] = None
        _state["text_cache"] = {}
        _state["load_ms"] = load
        _state["runtime_fail"] = 0
        return

    _state["tokenizer"] = ClipTokenizer(DEFAULTS["bpe"])
    # 文本编码器只跑 CPU：它很小（中位 ~10ms），把 AI 核留给分割模型。
    _state["clip"], load["clip_ms"], load["clip_ep"] = _session(DEFAULTS["clip_model"], cpu_only=True)
    _state["seg"], load["seg_ms"], seg_info = _session(
        DEFAULTS["seg_model"], prefer_threads=EP_THREADS["seg"]
    )
    _state["seg_ep_threads"] = seg_info.get("ep_threads")
    det_enabled = os.environ.get("BOOTH_DET_ENABLED", "false").strip().lower() in (
        "true", "1", "yes"
    )
    try:
        if not det_enabled:
            raise FileNotFoundError("BOOTH_DET_ENABLED=false")
        _state["det"], load["det_ms"], det_info = _session(
            DEFAULTS["det_model"], prefer_threads=EP_THREADS["det"]
        )
    except Exception as exc:  # 检测模型可选
        _state["det"] = None
        det_info = {}
        load["det_ms"] = -1.0
        load["det_error"] = str(exc)[:200]

    # 手势模型按需加载，不能和人像分割会话同时常驻 AI 核。
    gesture_info = {}

    seg_inputs = [i.name for i in _state["seg"].get_inputs()]
    _state["seg_img_input"] = seg_inputs[0]
    _state["seg_text_input"] = next(n for n in seg_inputs if "txt" in n or "text" in n)
    _state["providers"] = seg_info.get("providers", [])
    _state["ep"] = bool(seg_info.get("ep"))
    _state["ep_threads"] = seg_info.get("ep_threads")
    _state["ep_fallback"] = seg_info.get("fallback")
    _state["det_ep"] = bool(det_info.get("ep"))
    _state["det_ep_threads"] = det_info.get("ep_threads")
    _state["gesture"] = None
    _state["gesture_ep"] = False
    _state["gesture_ep_threads"] = None
    _state["gesture_providers"] = []
    _state["gesture_ep_fallback"] = "按需加载，与人像分割独占轮换"
    _state["gesture_device"] = None
    _state["text_cache"] = {}
    _state["load_ms"] = load
    _state["runtime_fail"] = 0

    if not _state["ep"]:
        # 拿不到 AI 核：先尝试清掉死进程残留的 TCM，再重试一次。
        if release_stale_tcm(0):
            try:
                _state["seg"], load["seg_ms_retry"], seg_info = _session(
                    DEFAULTS["seg_model"], prefer_threads=EP_THREADS["seg"]
                )
                _state["providers"] = seg_info.get("providers", [])
                _state["ep"] = bool(seg_info.get("ep"))
                _state["ep_threads"] = seg_info.get("ep_threads")
                _state["ep_fallback"] = seg_info.get("fallback")
            except Exception as exc:
                load["retry_error"] = str(exc)[:200]

    if _state["det"] is not None:
        _state["det_input"] = _state["det"].get_inputs()[0].name
    _state["gesture_input"] = None
    _state["mode"] = "seg"


# ---- 图像处理（只用 PIL + numpy，不用 cv2）--------------------------------
def _letterbox(rgb: np.ndarray, size: int) -> tuple[np.ndarray, float, tuple[float, float]]:
    height, width = rgb.shape[:2]
    gain = min(size / width, size / height)
    new_w, new_h = max(1, round(width * gain)), max(1, round(height * gain))
    pad_x, pad_y = (size - new_w) / 2, (size - new_h) / 2
    canvas = np.full((size, size, 3), 114, dtype=np.uint8)
    ys = np.minimum((np.arange(new_h) * height) // new_h, height - 1)
    xs = np.minimum((np.arange(new_w) * width) // new_w, width - 1)
    top, left = int(round(pad_y)), int(round(pad_x))
    canvas[top : top + new_h, left : left + new_w] = rgb[np.ix_(ys, xs)]
    return canvas, gain, (pad_x, pad_y)


def _sigmoid(x: np.ndarray) -> np.ndarray:
    return 1.0 / (1.0 + np.exp(-np.clip(x, -60.0, 60.0)))


# 检测头的类别数（COCO 80）。用来判断输出张量哪一维是"特征维"：
# 真正的特征是 4 框 + 80 类 = 84，另一个维度是候选框数（通常 8400）。
# 只用来做形状判定，不参与阈值计算。
DET_CLASSES = 80


def _nms(boxes: np.ndarray, scores: np.ndarray, iou_threshold: float) -> list[int]:
    order = np.argsort(-scores)
    keep: list[int] = []
    while order.size:
        i = int(order[0])
        keep.append(i)
        if order.size == 1:
            break
        rest = order[1:]
        x1 = np.maximum(boxes[i, 0], boxes[rest, 0])
        y1 = np.maximum(boxes[i, 1], boxes[rest, 1])
        x2 = np.minimum(boxes[i, 2], boxes[rest, 2])
        y2 = np.minimum(boxes[i, 3], boxes[rest, 3])
        inter = np.maximum(0.0, x2 - x1) * np.maximum(0.0, y2 - y1)
        a1 = (boxes[i, 2] - boxes[i, 0]) * (boxes[i, 3] - boxes[i, 1])
        a2 = (boxes[rest, 2] - boxes[rest, 0]) * (boxes[rest, 3] - boxes[rest, 1])
        iou = inter / (a1 + a2 - inter + 1e-9)
        order = rest[iou <= iou_threshold]
    return keep


def _drop_nested_persons(persons: list[dict], containment: float = 0.88,
                         max_area_ratio: float = 0.40,
                         masks: dict[int, np.ndarray] | None = None,
                         mask_containment: float = 0.55) -> tuple[list[dict], int]:
    """移除同一人物内部的重复小框，不把它误算成第二个人。

    开放词汇分割偶尔会同时给出「整个人」和「脸/上半身」两个 person 框。
    两框的普通 IoU 不高，所以常规 NMS 留不掉；但小框几乎完全落在大框内部。
    这里只合并面积明显更小且被大框高度包住的结果，两个并排或相交的人不受影响。
    """
    if len(persons) < 2:
        return persons, 0

    def area(item: dict) -> float:
        x0, y0, x1, y1 = (float(value) for value in item["box"])
        return max(0.0, x1 - x0) * max(0.0, y1 - y0)

    ordered = sorted(enumerate(persons), key=lambda pair: area(pair[1]), reverse=True)
    kept: list[tuple[int, dict]] = []
    removed = 0
    for original_index, candidate in ordered:
        cx0, cy0, cx1, cy1 = (float(value) for value in candidate["box"])
        candidate_area = area(candidate)
        nested = False
        if candidate_area > 0:
            for _kept_index, outer in kept:
                outer_area = area(outer)
                if outer_area <= 0 or candidate_area > outer_area * max_area_ratio:
                    continue
                ox0, oy0, ox1, oy1 = (float(value) for value in outer["box"])
                intersection = (max(0.0, min(cx1, ox1) - max(cx0, ox0))
                                * max(0.0, min(cy1, oy1) - max(cy0, oy0)))
                if intersection / candidate_area < containment:
                    continue
                if masks is not None:
                    candidate_mask = masks.get(original_index)
                    outer_mask = masks.get(_kept_index)
                    if candidate_mask is not None and outer_mask is not None:
                        candidate_pixels = int(np.count_nonzero(candidate_mask))
                        if candidate_pixels > 0:
                            overlap = int(np.count_nonzero(candidate_mask & outer_mask))
                            if overlap / candidate_pixels < mask_containment:
                                continue
                nested = True
                break
        if nested:
            removed += 1
        else:
            kept.append((original_index, candidate))
    kept.sort(key=lambda pair: pair[0])
    return [item for _index, item in kept], removed


def _read_rgb(path: str) -> np.ndarray:
    with Image.open(path) as image:
        return np.asarray(image.convert("RGB"), dtype=np.uint8)


# ---- 主体选择（多人画面下挑一个人来成像） --------------------------------
# 目标：画面里站着好几个人时，挑出「最完整、最近、在拍摄位上」的那一个，
# 只对他抠像与风格化；其余人保留在画面里但不参与成片。
#
# 诚实的边界：**模型只给出人框，测不出"正对镜头"**。真正的朝向判定需要头部
# 姿态/人脸关键点，而本装置的合规红线是"只做人像分割、不做人脸识别"，所以这里
# 用几何近似代替：
#   * 面积（最近、最大）—— 主办者站在拍摄位时通常最靠前；
#   * 居中（框中心离**拍摄位锚点**的距离）—— 锚点默认画面正中，可现场标定；
#   * 完整（框没被画面边缘切掉）—— 被切掉的人多半是路过的、贴着边的。
# 这三项都只是"更像主办者"的**代理指标**，不是朝向检测；选错了也只影响谁被
# 风格化（成片仍然是真实拍摄的本人），不会伪造任何信息。
#
# 曾经还有第四项「框宽高比接近直立人形」，**已删**：拿真机帧量过，本装置是人
# 站定后的半身/近景，框宽/高普遍在 0.6~1.2，而站远处的瘦框反而接近 0.42 ——
# 那一项等于反向偏好远处的人，和「最近、居中」直接打架，是在制造错误偏好。
#
# 所有阈值都来自 subject_rules（settings.json 的 subject 段 / BOOTH_SUBJECT_*
# 环境变量），**不写死在这里**：拍摄位在哪、人站多远是现场量出来的。


def _select_subject(persons: list[dict], width: int, height: int,
                    frame: tuple[int, int] | None = None,
                    rules: dict | None = None) -> dict | None:
    """从若干 person 里挑一个主体，返回 {"index","box","score","detail"}；没人返回 None。

    frame 是 (宽, 高) 的画面尺寸，用于判断框是否被边缘切掉；缺省用入参 width/height。
    rules 缺省取当前生效配置（现场改完最多 2 秒生效）。
    """
    if not persons:
        return None
    cfg = rules if rules is not None else _subject_rules()
    weights = cfg["weights"]
    anchor = cfg["anchor"]
    area_full = max(1e-6, float(cfg["area_full"]))

    fw = float(frame[0] if frame else width) or float(width) or 1.0
    fh = float(frame[1] if frame else height) or float(height) or 1.0
    # 锚点是"拍摄位在画面里的位置"（比例），不是画面正中 —— 相机装偏了也能标定
    ax, ay = fw * anchor[0], fh * anchor[1]
    diag = max(1.0, (fw ** 2 + fh ** 2) ** 0.5)

    scored: list[tuple[float, int, dict]] = []
    for index, person in enumerate(persons):
        box = person.get("box")
        if not box or len(box) < 4:
            continue
        x0, y0, x1, y1 = (float(v) for v in box[:4])
        box_w = max(0.0, x1 - x0)
        box_h = max(0.0, y1 - y0)
        if box_w <= 0 or box_h <= 0:
            continue

        area_score = min(1.0, (box_w * box_h) / (fw * fh) / area_full)
        center_dist = (((x0 + x1) / 2 - ax) ** 2 + ((y0 + y1) / 2 - ay) ** 2) ** 0.5
        center_score = max(0.0, 1.0 - center_dist / (diag * 0.5))

        # 被画面四边切掉多少（按周长比例），恰好贴边不算全切
        cut = 0.0
        for edge, over in (("l", -x0), ("t", -y0), ("r", x1 - fw), ("b", y1 - fh)):
            if over > 0:
                span = box_h if edge in ("l", "r") else box_w
                cut += min(1.0, over / max(1.0, span))
        completeness = max(0.0, 1.0 - cut / 2.0)

        ratio = box_w / box_h
        score = (weights["area"] * area_score
                 + weights["center"] * center_score
                 + weights["completeness"] * completeness)
        scored.append((score, index, {
            "area": round(area_score, 4), "center": round(center_score, 4),
            "completeness": round(completeness, 4), "aspect_ratio": round(ratio, 4),
        }))

    if not scored:
        return None
    scored.sort(key=lambda item: (-item[0], item[1]))   # 平手取框更靠前的那个
    score, index, detail = scored[0]
    return {
        "index": index,
        "box": list(persons[index]["box"]),
        "score": round(float(score), 4),
        "detail": detail,
        "weights": dict(weights),
        "anchor": list(anchor),
    }


def _encode_mask(binary: np.ndarray, max_side: int) -> tuple[str, int, int]:
    """二值掩码 -> PNG(base64)，按 max_side 缩到最长边。"""
    height, width = binary.shape
    scale = min(1.0, max_side / max(height, width)) if max_side else 1.0
    if scale < 1.0:
        size = (max(1, round(width * scale)), max(1, round(height * scale)))
        image = Image.fromarray((binary * 255).astype(np.uint8), mode="L").resize(size, Image.BILINEAR)
    else:
        image = Image.fromarray((binary * 255).astype(np.uint8), mode="L")
    buffer = io.BytesIO()
    image.save(buffer, format="PNG", optimize=True)
    return base64.b64encode(buffer.getvalue()).decode("ascii"), image.width, image.height


# ---- 文本编码 -------------------------------------------------------------
def text_features(prompts: list[str]) -> np.ndarray:
    """提示词 -> (1, 80, 512) 的 txt_feats；同一词表只算一次。"""
    load_all()
    tokenizer = _state["tokenizer"]
    clip = _state["clip"]
    cache: dict[str, list[float]] = _state["text_cache"]
    input_name = clip.get_inputs()[0].name
    rows = []
    for prompt in prompts:
        key = prompt.strip()
        if key not in cache:
            ids = np.asarray(tokenizer.encode_padded(key), dtype=np.int64).reshape(1, 77)
            vector = clip.run(None, {input_name: ids})[0]
            cache[key] = np.asarray(vector, dtype=np.float32).reshape(-1, 512)[0].tolist()
        rows.append(cache[key])
    features = np.zeros((1, DEFAULTS["max_nc"], 512), dtype=np.float32)
    for index, row in enumerate(rows[: DEFAULTS["max_nc"]]):
        features[0, index] = row
    return features


# ---- 抠像（专门的 matting 模型，不用检测框）--------------------------------
# 为什么单独一个模型：YOLOE 给的是「框 + 框内多边形掩码」，边是折线、发丝全丢，
# 拿它当抠像用，人就是从一块方框里挖出来的。MODNet 是专门做人像 matting 的，
# 输入整幅图，输出整幅的软 alpha —— 没有框，头发丝和半透明边缘都在。
#
# ★ 只能 CPU：MODNet 的 BatchNormalization 让 SpaceMIT EP 编译失败，而且是硬崩
#   （`terminate called after throwing ... operator compile failed at node
#   (BatchNormalization_197)`），整个 worker 进程直接死。所以绝不能让它进 EP 阶梯。
#   CPU 实测 266 ms（256x256 输入），端到端预算里放得下。
#
# 为什么是 MODNet 而不是更大的抠像模型（本机同一张 500x375 人像实测）：
#   MODNet(256)           298 ms   coverage 0.268
#   u2net_human_seg(320) 1203 ms   coverage 0.274
#   rmbg14(1024)         3059 ms   coverage 0.272
#   三者出图边在同一观感档，贵的两个没换来更好的边，只换来 4~10 倍等待。
# 输入分辨率：模型的 height/width 是**动态维度**（不是固定 256 —— 早期按 256 写死
# 是白丢精度）。现场实测同一张 1920x1080 抓拍、同一个扩充后的人像裁切（1579x1080）：
#   256 -> 326 ms     384 -> 764 ms     512 -> 1340 ms     640 -> 2088 ms
# 256 的 alpha 放到裁切尺寸要放大 6 倍，边是糊的、发丝是一团；512 只放大 3 倍，
# 配合下面的边缘细化，观感差一个档。多花 1 秒，端到端预算（≤35 s/人）里放得下。
MATTE_SIDE = int(os.environ.get("BOOTH_MATTE_SIDE", "512"))
MATTE_MEAN = 0.5
MATTE_STD = 0.5
# 边缘细化（guided filter，以原片灰度为导向图）。模型输出是「小图放大」来的，
# 轮廓在像素级一定是糊的；guided filter 把它吸附回原片真实的边缘上，发丝和肩线
# 才立得住。r 是作用半径，eps 是「多大反差才算边」。设 0 可关掉。
MATTE_REFINE_R = int(os.environ.get("BOOTH_MATTE_REFINE_R", "12"))
MATTE_REFINE_EPS = float(os.environ.get("BOOTH_MATTE_REFINE_EPS", "0.0001"))
# 人框要放大到「占裁切画面多大」。MODNet 是拿「头像式特写」训出来的，人在画面里
# 越大越准；原样整幅喂进去，站位稍偏就整幅判空（实测：办公室抓拍覆盖率 0.00002）。
# 0.80（≈ 人框外扩 1.25 倍）是扫出来的最优点。同一张人像按「占画面高」六档合成到
# 2592x1944 画布上量 IoU：外扩 1.25 倍得 0.87/0.91/0.91/0.63/0.76/0.54，
# 外扩 1.0/1.6/2.2 倍则各有一档掉到 0.5 以下。
MATTE_FILL = float(os.environ.get("BOOTH_MATTE_FILL", "0.80"))
# 裁切用竖构图宽高比（宽/高）。证件照本来就是竖的，MODNet 也是竖构图最稳。
MATTE_ASPECT = float(os.environ.get("BOOTH_MATTE_ASPECT", "0.80"))
# 裁切内 alpha 的合格线。低于它就说明 MODNet 在这个裁切上根本没找到人，
# 改用检测掩码兜底 —— 边糊一点，但绝不至于交一张空白。
MATTE_MIN_FILL = 0.01
# 抠像结果必须和检测掩码对得上（IoU）。实测过 MODNet 在某些裁切上会
# 「碰巧给出一堆不知道是什么」的 alpha（非空，但全错），只靠覆盖率阈值拦不住
# —— 这道闸门就是为了拦它。设得宽松（0.30），宁可放过也不要因为检测掩码本身
# 粗糙而误杀一张好 alpha。
MATTE_MIN_IOU = 0.30


def matte_available() -> bool:
    return Path(DEFAULTS["matte_model"]).is_file()


def _matte_session():
    session = _state.get("matte")
    if session is None:
        session, load_ms, info = _session(DEFAULTS["matte_model"], cpu_only=True)
        _state["matte"] = session
        _state["matte_load_ms"] = round(load_ms, 1)
        _state["matte_input"] = session.get_inputs()[0].name
    return session


def _matte_roi(box, size) -> tuple[int, int, int, int]:
    """把检测框扩成居中的竖构图裁切（像素坐标，已夹进画面）。

    检测框在这里**只用来决定「看哪儿」**，不作为最终的边 —— 最终的边仍然逐
    像素由 MODNet 给。这就是 detect -> crop -> matte 的标准做法：MODNet 要有足够大的
    人才工作，而把人框放大到占满画面正是给它创造条件。
    """
    width, height = int(size[0]), int(size[1])
    x0, y0, x1, y1 = (float(v) for v in box)
    bw, bh = max(1.0, x1 - x0), max(1.0, y1 - y0)
    ch = max(bh / MATTE_FILL, bw / (MATTE_FILL * MATTE_ASPECT))
    cw = ch * MATTE_ASPECT
    cx, cy = (x0 + x1) / 2.0, (y0 + y1) / 2.0
    # 竖向偏上留白：头顶要留空间，脚下常常已经出画（半身照＋人被下边缘切断）
    nx0 = int(round(cx - cw / 2.0))
    ny0 = int(round(cy - ch * 0.45))
    return (
        max(0, min(nx0, width - 1)),
        max(0, min(ny0, height - 1)),
        max(1, min(int(round(nx0 + cw)), width)),
        max(1, min(int(round(ny0 + ch)), height)),
    )


def _matte_alpha(rgb: np.ndarray, box=None):
    """人像 alpha matte。返回 (全幅 (H,W) float32 0..1, 耗时 ms, roi, 裁切内覆盖率)。

    ``box`` 是检测到的人框（x0,y0,x1,y1）；给了就在那块裁切里跑 MODNet，
    再把结果贴回全幅坐标系。不给就整幅跑（老行为，站位正的话也能用）。
    """
    session = _matte_session()
    height, width = rgb.shape[:2]
    roi = _matte_roi(box, (width, height)) if box is not None else (0, 0, width, height)
    rx0, ry0, rx1, ry1 = roi
    crop = rgb[ry0:ry1, rx0:rx1]
    side = max(64, MATTE_SIDE)
    small = np.asarray(Image.fromarray(crop).resize((side, side), Image.BILINEAR), np.float32)
    x = (small / 255.0 - MATTE_MEAN) / MATTE_STD
    blob = x.transpose(2, 0, 1)[None].astype(np.float32)

    started = time.perf_counter()
    outputs = session.run(None, {_state["matte_input"]: blob})
    elapsed = (time.perf_counter() - started) * 1000.0

    alpha = np.asarray(outputs[0], np.float32)
    while alpha.ndim > 2:
        alpha = alpha[0]
    alpha = np.clip(alpha, 0.0, 1.0)
    # 256 的 matte 直接最近邻放大会有台阶，双线性放大后边缘才是软的
    big = np.asarray(
        Image.fromarray((alpha * 255).astype(np.uint8)).resize((rx1 - rx0, ry1 - ry0),
                                                               Image.BILINEAR),
        np.float32,
    ) / 255.0
    if MATTE_REFINE_R > 0:
        big = _refine_alpha(big, crop, MATTE_REFINE_R, MATTE_REFINE_EPS)
    full = np.zeros((height, width), np.float32)
    full[ry0:ry1, rx0:rx1] = big
    return full, elapsed, roi, float((big > 0.5).mean())


def _box(arr: np.ndarray, radius: int) -> np.ndarray:
    """方框滤波（积分图，边界按边缘复制）。guided filter 的每一步都用它。"""
    if radius <= 0:
        return arr.astype(np.float32)
    x = arr.astype(np.float32, copy=False)
    k = 2 * radius + 1
    h, w = x.shape
    padded = np.pad(x, ((radius + 1, radius), (radius + 1, radius)), mode="edge")
    ii = padded.cumsum(0).cumsum(1)
    total = (ii[k:k + h, k:k + w] - ii[0:h, k:k + w]
             - ii[k:k + h, 0:w] + ii[0:h, 0:w])
    return total / float(k * k)


def _refine_alpha(alpha: np.ndarray, rgb: np.ndarray, radius: int, eps: float) -> np.ndarray:
    """guided filter（He et al.）：拿原片当导向图，把糊掉的 alpha 吸附到真实边缘上。

    模型给的是 512 的 alpha 放大到裁切尺寸，轮廓天然发虚。这里用原片的灰度做引导，
    在 radius 窗口内做局部线性拟合：窗口里没有边缘的地方（平坦背景）拟合结果就是原值，
    有边缘的地方 alpha 会跟着亮度跳变走 —— 于是发丝、肩线、衣领这些边回到像素级。
    """
    x = np.asarray(rgb, np.float32)
    guide = np.clip((x[..., 0] * 0.299 + x[..., 1] * 0.587 + x[..., 2] * 0.114) / 255.0,
                    0.0, 1.0).astype(np.float32)
    src = np.clip(alpha, 0.0, 1.0).astype(np.float32)
    mean_g, mean_s = _box(guide, radius), _box(src, radius)
    mean_gs = _box(guide * src, radius)
    mean_gg = _box(guide * guide, radius)
    cov = mean_gs - mean_g * mean_s
    var = np.maximum(mean_gg - mean_g * mean_g, 0.0)
    a = cov / (var + eps)
    b = mean_s - a * mean_g
    out = _box(a, radius) * guide + _box(b, radius)
    return np.clip(out, 0.0, 1.0)


def _agree(alpha: np.ndarray, mask: np.ndarray) -> float:
    """抠像与检测掩码的一致度（IoU）。低了就是模型认错人了。"""
    a = alpha > 0.5
    b = mask if mask.dtype == bool else mask > 0.5
    union = int(np.count_nonzero(a | b))
    if not union:
        return 0.0
    return float(np.count_nonzero(a & b)) / float(union)


def _mask_alpha(binary: np.ndarray) -> np.ndarray:
    """检测掩码 -> 软 alpha（兜底用）。只做一点羽化，不改形状。"""
    height, width = binary.shape[:2]
    gray = Image.fromarray((binary * 255).astype(np.uint8), mode="L")
    radius = max(1.0, min(height, width) * 0.006)
    return np.asarray(gray.filter(ImageFilter.GaussianBlur(radius)), np.float32) / 255.0


def _encode_alpha(alpha: np.ndarray, max_side: int) -> tuple[str, int, int]:
    """0..1 连续 alpha -> PNG(base64)。和 _encode_mask 同一套编码，区别是这里允许软边。"""
    return _encode_mask(np.clip(alpha, 0.0, 1.0), max_side)


def _attach_matte(response: dict, rgb: np.ndarray, request: dict,
                  box=None, fallback_mask=None) -> None:
    """给响应挂上精细 alpha。抠像失败只记原因，绝不让整个请求失败。

    顺序是「先按人裁切，再整幅」，和早期版本正好相反 —— 这是量出来的，不是猜的：
    把同一张人像按「占画面高 20%/30%/40%/55%/75%/95%」合成到 2592x1944 展台画布上，
    按人裁切一路的 IoU 是 0.87/0.91/0.91/0.63/0.76/0.54，整幅直出只有
    0.00/0.20/0.61/0.39/0.70/0.54。人越小差距越大，而展台本来就是「人站着、
    镜头包一大片背景」的场景 —— 归到成片里只有一小格，整幅喂进去等于把人缩没了。

    三道保险：
      1. 有检测框 -> 按人裁切跑 MODNet（主路，实测命中率最高）；
      2. 这一路没找到人 / 和检测掩码对不上 -> 整幅再跑一次（人贴很近时它更好）；
      3. 都不行 -> 退回检测掩码羽化版（边糊但有人）；
      4. 连检测都没人 -> 不挂 alpha，调用方整幅取景。
    """
    if not request.get("matte", True) or not matte_available():
        return
    width, height = int(response["width"]), int(response["height"])
    fallback_ok = fallback_mask is not None and bool(fallback_mask.any())
    attempts = ([("modnet-roi", box)] if box is not None else []) + [("modnet", None)]

    alpha = None
    roi = (0, 0, width, height)
    fill = 0.0
    elapsed = 0.0
    source = None
    tried: list = []
    errors: list = []
    for name, candidate_box in attempts:
        # 单次失败不该拖垮整条链：这一路抛了就记一笔，继续试下一路
        try:
            cand, ms, cand_roi, cand_fill = _matte_alpha(rgb, candidate_box)
        except Exception as exc:
            errors.append("%s: %s" % (name, str(exc)[:120]))
            continue
        elapsed += ms
        agree = _agree(cand, fallback_mask) if fallback_ok else None
        tried.append({"source": name, "fill": round(cand_fill, 5),
                      "iou": None if agree is None else round(agree, 4)})
        if cand_fill < MATTE_MIN_FILL:
            continue
        if agree is not None and agree < MATTE_MIN_IOU:
            continue
        alpha, roi, fill, source = cand, cand_roi, cand_fill, name
        break

    if alpha is None:
        if fallback_ok:
            alpha = _mask_alpha(fallback_mask)
            fill = float((alpha > 0.5).mean())
            roi = (0, 0, width, height)
            source = "detect-mask"
        else:
            # 画面里本来就没有人：不挂 alpha，交回调用方整幅取景
            if errors:
                response["matte_error"] = " / ".join(errors)
            if tried:
                response["matte_tried"] = tried
            return

    # 抠像 alpha 默认**不缩**：它是往成片上贴的边，缩一次再放大等于白抠。
    # 数人 / 画框用的二值掩码走 mask_max_side（640 足够），两者互不影响。
    encoded, mw, mh = _encode_alpha(alpha, int(request.get("matte_max_side", 0)))
    response["matte_b64"] = encoded
    response["matte_width"] = mw
    response["matte_height"] = mh
    response["matte_ms"] = round(elapsed, 1)
    response["matte_coverage"] = round(float((alpha > 0.5).mean()), 5)
    response["matte_fill"] = round(fill, 5)
    if fallback_ok:
        response["matte_iou"] = round(_agree(alpha, fallback_mask), 4)
    response["matte_roi"] = [int(v) for v in roi]
    response["matte_source"] = source
    if len(tried) > 1:
        # 多试了一路才成 —— 现场排「抠像不对」时看这个
        response["matte_tried"] = tried
    if errors:
        response["matte_error"] = " / ".join(errors)


def op_matte(request: dict) -> dict:
    """单独抠一张：优先按人框裁切抠，给不出框就整幅抠。"""
    load_all()
    rgb = _read_rgb(request["image"])
    height, width = rgb.shape[:2]
    response = {"ok": True, "op": "matte", "width": width, "height": height,
                "model": DEFAULTS["matte_model"]}
    box = request.get("box")
    _attach_matte(response, rgb, request, box=tuple(box) if box else None)
    return response


# ---- 各 op ---------------------------------------------------------------
def op_seg(request: dict) -> dict:
    load_all()
    _switch_mode("seg")
    rgb = _read_rgb(request["image"])
    height, width = rgb.shape[:2]
    prompts = [str(p) for p in request.get("prompts") or ["person"]]
    conf_threshold = float(request.get("conf", 0.20))
    iou_threshold = float(request.get("iou", 0.45))
    mask_max_side = int(request.get("mask_max_side", 640))

    canvas, gain, pad = _letterbox(rgb, DEFAULTS["imgsz"])
    blob = canvas.astype(np.float32).transpose(2, 0, 1)[None] / 255.0

    started = time.perf_counter()
    outputs = _state["seg"].run(
        None, {_state["seg_img_input"]: blob, _state["seg_text_input"]: text_features(prompts)}
    )
    infer_ms = (time.perf_counter() - started) * 1000.0

    prediction = np.asarray(outputs[0], dtype=np.float32)[0].T
    proto = np.asarray(outputs[1], dtype=np.float32)[0]  # (mask_dim, mh, mw)
    mask_dim = int(proto.shape[0])
    output_features = int(prediction.shape[1])
    n_output_classes = output_features - 4 - mask_dim
    if n_output_classes < len(prompts):
        raise ValueError(
            "seg 输出类别维度不足：features=%d mask_dim=%d prompts=%d"
            % (output_features, mask_dim, len(prompts))
        )
    n_cls = len(prompts)
    boxes = prediction[:, :4]
    class_scores = prediction[:, 4:4 + n_cls]
    mask_coeffs = prediction[:, 4 + n_output_classes:]

    confidences = class_scores.max(axis=1)
    labels = class_scores.argmax(axis=1)
    selected = confidences > conf_threshold
    boxes, confidences, labels, mask_coeffs = (
        boxes[selected], confidences[selected], labels[selected], mask_coeffs[selected]
    )

    persons: list[dict] = []
    fused = np.zeros((height, width), dtype=bool)
    # 每个人各自的掩码：fused 是**所有人**的并集，用来画大屏的框；
    # 主体那一份单独留着 —— 只抠一个人时不能把并集当外限，否则旁边的同伴
    # 会被 constrain_alpha 一起保留下来。
    per_person_masks: dict[int, np.ndarray] = {}
    target = prompts.index("person") if "person" in prompts else 0

    if boxes.size:
        xyxy = np.stack(
            [
                boxes[:, 0] - boxes[:, 2] / 2,
                boxes[:, 1] - boxes[:, 3] / 2,
                boxes[:, 0] + boxes[:, 2] / 2,
                boxes[:, 1] + boxes[:, 3] / 2,
            ],
            axis=1,
        )
        keep = _nms(xyxy, confidences, iou_threshold)
        xyxy, confidences, labels, mask_coeffs = (
            xyxy[keep], confidences[keep], labels[keep], mask_coeffs[keep]
        )

        proto_h, proto_w = proto.shape[1], proto.shape[2]
        flat = proto.reshape(proto.shape[0], -1)
        grid_y = np.clip(((np.arange(DEFAULTS["imgsz"]) + 0.5) * proto_h / DEFAULTS["imgsz"]).astype(int), 0, proto_h - 1)
        grid_x = np.clip(((np.arange(DEFAULTS["imgsz"]) + 0.5) * proto_w / DEFAULTS["imgsz"]).astype(int), 0, proto_w - 1)

        order = np.argsort(-confidences)
        for index in order:
            label = int(labels[index])
            box = xyxy[index]
            person = {
                "score": round(float(confidences[index]), 4),
                "label": prompts[label] if label < len(prompts) else f"class_{label}",
                "box": [
                    round(float(np.clip((box[0] - pad[0]) / gain, 0, width - 1)), 1),
                    round(float(np.clip((box[1] - pad[1]) / gain, 0, height - 1)), 1),
                    round(float(np.clip((box[2] - pad[0]) / gain, 0, width - 1)), 1),
                    round(float(np.clip((box[3] - pad[1]) / gain, 0, height - 1)), 1),
                ],
            }
            if label == target:
                coefficients = mask_coeffs[index]
                small = _sigmoid(coefficients @ flat).reshape(proto_h, proto_w)
                at_full = small[np.ix_(grid_y, grid_x)]
                x1 = int(np.clip(np.floor(box[0]), 0, DEFAULTS["imgsz"] - 1))
                y1 = int(np.clip(np.floor(box[1]), 0, DEFAULTS["imgsz"] - 1))
                x2 = int(np.clip(np.ceil(box[2]), 1, DEFAULTS["imgsz"]))
                y2 = int(np.clip(np.ceil(box[3]), 1, DEFAULTS["imgsz"]))
                patch = at_full[y1:y2, x1:x2]
                if patch.size:
                    ox1 = int(np.clip(round((x1 - pad[0]) / gain), 0, width - 1))
                    oy1 = int(np.clip(round((y1 - pad[1]) / gain), 0, height - 1))
                    ox2 = int(np.clip(round((x2 - pad[0]) / gain), ox1 + 1, width))
                    oy2 = int(np.clip(round((y2 - pad[1]) / gain), oy1 + 1, height))
                    resized = np.asarray(
                        Image.fromarray((patch * 255).astype(np.uint8)).resize(
                            (ox2 - ox1, oy2 - oy1), Image.BILINEAR
                        ),
                        dtype=np.float32,
                    ) / 255.0
                    region = np.zeros((height, width), dtype=np.float32)
                    region[oy1:oy2, ox1:ox2] = resized
                    this_mask = region > 0.5
                    fused |= this_mask
                    person["mask_area"] = round(float(this_mask.mean()), 5)
                    per_person_masks[len(persons)] = this_mask
            persons.append(person)

    raw_person_count = len(persons)
    mask_by_person = {
        id(persons[index]): mask
        for index, mask in per_person_masks.items()
        if 0 <= index < len(persons)
    }
    persons, nested_suppressed = _drop_nested_persons(persons, masks=per_person_masks)
    per_person_masks = {
        index: mask_by_person[id(person)]
        for index, person in enumerate(persons)
        if id(person) in mask_by_person
    }
    response = {
        "ok": True,
        "op": "seg",
        "width": width,
        "height": height,
        "infer_ms": round(infer_ms, 1),
        "providers": _state["providers"],
        "ep": _state["ep"],
        "prompts": prompts,
        "persons": persons,
        "person_count": len(persons),
        "raw_person_count": raw_person_count,
        "nested_suppressed": nested_suppressed,
        "foreground_area": round(float(fused.mean()), 5),
    }
    if request.get("mask", True):
        encoded, mask_w, mask_h = _encode_mask(fused, mask_max_side)
        response["mask_b64"] = encoded
        response["mask_width"] = mask_w
        response["mask_height"] = mask_h
    # 框内多边形掩码只用来数人、画大屏框、以及在抠像失手时兜底；
    # 真正往成片上贴的边来自 MODNet —— 但它需要先被告知「看哪儿」。
    #
    # 多人时先**选一个主体**（最完整 / 最近 / 居中 / 接近直立），然后只抠他：
    # 抠像框与兜底掩码都只用主体那一份。以前这里取所有人框的并集，那是"多人站
    # 一起要一起成像"时代的口径；现在成片只要一个人，并集会把旁边的同伴一起抠
    # 出来糊进风格化输入里。
    cfg = _subject_rules()
    subject = _select_subject(persons, width, height, rules=cfg)
    if subject is not None:
        response["subject"] = {
            "index": subject["index"], "box": subject["box"],
            "score": subject["score"], "detail": subject["detail"],
            "weights": subject["weights"], "anchor": subject["anchor"],
        }
    matte_box = None
    if subject is not None:
        # 抠像前把主体框往外扩一点：分割框常贴着头发/肩膀切，MODNet 在裁切边
        # 上会把那一点当背景丢掉。扩多少来自配置（pad，画面比例）。
        pad_x = float(cfg["pad"]) * width
        pad_y = float(cfg["pad"]) * height
        x0, y0, x1, y1 = (float(v) for v in subject["box"][:4])
        matte_box = [max(0.0, x0 - pad_x), max(0.0, y0 - pad_y),
                     min(float(width), x1 + pad_x), min(float(height), y1 + pad_y)]
    subject_mask = None
    if subject is not None:
        # 主体掩码可能为空（分割只给了框、没给多边形），那时退回并集，
        # 至少不让人像整个丢掉。
        candidate = per_person_masks.get(subject["index"])
        if candidate is not None and bool(candidate.any()):
            subject_mask = candidate
            encoded, mask_w, mask_h = _encode_mask(subject_mask, mask_max_side)
            response["subject_mask_b64"] = encoded
            response["subject_mask_width"] = mask_w
            response["subject_mask_height"] = mask_h
    if request.get("progress"):
        progress = {**response, "event": "segmentation", "id": request.get("id")}
        sys.stdout.write(PROTOCOL_PREFIX + json.dumps(progress, ensure_ascii=False) + "\n")
        sys.stdout.flush()
    _attach_matte(response, rgb, request, box=matte_box,
                  fallback_mask=subject_mask if subject_mask is not None else fused)
    return response


def op_gesture(request: dict) -> dict:
    load_all()
    _switch_mode("gesture")
    if _state.get("gesture") is None:
        return {
            "ok": False,
            "op": "gesture",
            "error": "官方手势模型未加载",
            "model": DEFAULTS["gesture_model"],
        }
    rgb = _read_rgb(request["image"])
    height, width = rgb.shape[:2]
    conf_threshold = float(request.get("conf", 0.45))
    iou_threshold = float(request.get("iou", 0.45))
    canvas, gain, pad = _letterbox(rgb, 640)
    blob = canvas.astype(np.float32).transpose(2, 0, 1)[None] / 255.0

    started = time.perf_counter()
    raw = np.asarray(
        _state["gesture"].run(None, {_state["gesture_input"]: blob})[0],
        dtype=np.float32,
    )
    infer_ms = (time.perf_counter() - started) * 1000.0
    if raw.ndim == 3:
        raw = raw[0]
    features = 5 + len(GESTURE_LABELS)
    if raw.ndim != 2:
        raise ValueError("gesture 输出维度异常：%s" % (raw.shape,))
    if raw.shape[-1] == features:
        prediction = raw
    elif raw.shape[0] == features:
        prediction = raw.T
    else:
        raise ValueError("gesture 输出形状异常：%s" % (raw.shape,))

    objectness = np.clip(prediction[:, 4], 0.0, 1.0)
    class_scores = np.clip(prediction[:, 5:features], 0.0, 1.0)
    labels = class_scores.argmax(axis=1)
    scores = objectness * class_scores[np.arange(class_scores.shape[0]), labels]
    selected = scores >= conf_threshold
    boxes = prediction[selected, :4]
    scores = scores[selected]
    labels = labels[selected]
    detections: list[dict] = []
    if boxes.size:
        xyxy = np.stack(
            [
                boxes[:, 0] - boxes[:, 2] / 2,
                boxes[:, 1] - boxes[:, 3] / 2,
                boxes[:, 0] + boxes[:, 2] / 2,
                boxes[:, 1] + boxes[:, 3] / 2,
            ],
            axis=1,
        )
        xyxy[:, [0, 2]] = np.clip(xyxy[:, [0, 2]], 0, 640)
        xyxy[:, [1, 3]] = np.clip(xyxy[:, [1, 3]], 0, 640)
        keep = _nms(xyxy, scores, iou_threshold)
        for index in keep:
            box = xyxy[index]
            detections.append(
                {
                    "label": GESTURE_LABELS[int(labels[index])],
                    "label_id": int(labels[index]),
                    "score": round(float(scores[index]), 4),
                    "box": [
                        round(float(np.clip((box[0] - pad[0]) / gain, 0, width - 1)), 1),
                        round(float(np.clip((box[1] - pad[1]) / gain, 0, height - 1)), 1),
                        round(float(np.clip((box[2] - pad[0]) / gain, 0, width - 1)), 1),
                        round(float(np.clip((box[3] - pad[1]) / gain, 0, height - 1)), 1),
                    ],
                }
            )
    detections.sort(key=lambda item: -item["score"])
    return {
        "ok": True,
        "op": "gesture",
        "width": width,
        "height": height,
        "infer_ms": round(infer_ms, 1),
        "providers": _state.get("gesture_providers", []),
        "ep": bool(_state.get("gesture_ep")),
        "detections": detections,
    }


def op_switch(request: dict) -> dict:
    """显式切换独占模型，供 OK 确认后立即交还人像分割。"""
    load_all()
    mode = str(request.get("mode") or "").strip().lower()
    if mode not in ("seg", "gesture"):
        return {"ok": False, "op": "switch", "error": "不支持的推理模式"}
    _switch_mode(mode)
    return {
        "ok": True,
        "op": "switch",
        "mode": mode,
        "ep": bool(_state.get("ep")) if mode == "seg" else bool(_state.get("gesture_ep")),
        "providers": (_state.get("providers", []) if mode == "seg"
                      else _state.get("gesture_providers", [])),
    }


def op_det(request: dict) -> dict:
    load_all()
    if _state["det"] is None:
        return {"ok": False, "op": "det", "error": "检测模型未加载", "load": _state.get("load_ms")}
    rgb = _read_rgb(request["image"])
    height, width = rgb.shape[:2]
    conf_threshold = float(request.get("conf", 0.25))
    iou_threshold = float(request.get("iou", 0.45))
    canvas, gain, pad = _letterbox(rgb, DEFAULTS["imgsz"])
    blob = canvas.astype(np.float32).transpose(2, 0, 1)[None] / 255.0

    started = time.perf_counter()
    raw = np.asarray(_state["det"].run(None, {_state["det_input"]: blob})[0], dtype=np.float32)[0]
    infer_ms = (time.perf_counter() - started) * 1000.0

    # 输出方向：真实形状是 (1, 84, 8400) = (1, 4框+80类, 候选数)。
    # 判据必须用**特征维**（== 4 + 类别数），不能用"谁大谁小"：
    # 8400 恒大于 84，`shape[0] < shape[1]` 在 (84, 8400) 上恰好成立、在
    # (8400, 84) 上不成立，看着能用，但它判的是"候选比类多"而不是"这是特征维"。
    # 这里显式按特征维判，两种导出都能吃。
    features = 4 + DET_CLASSES
    if raw.ndim == 3:
        raw = raw[0]
    if raw.shape[-1] == features:
        prediction = raw                       # (8400, 84)
    elif raw.shape[0] == features:
        prediction = raw.T                     # (84, 8400) -> (8400, 84)
    else:
        raise ValueError(
            "det 输出形状既不是 (N,%d) 也不是 (%d,N)：%s" % (features, features, raw.shape))

    # 类别列**已经是 sigmoid 后的概率**，不能再套一次。
    # 这个量化导出（yolov8n.q.onnx）实测：max=0.657、median=0.000、
    # 没有任何负 logit；再套 sigmoid 会把 0 抬到 0.5，于是 8400 个候选全部
    # 越过 0.25 阈值 —— person 从真实的 1 个变成 633 个（实测踩过）。
    scores = np.clip(prediction[:, 4:features], 0.0, 1.0)
    confidences = scores.max(axis=1)
    labels = scores.argmax(axis=1)
    selected = confidences > conf_threshold
    boxes, confidences, labels = prediction[selected, :4], confidences[selected], labels[selected]

    people = []
    if boxes.size:
        xyxy = np.stack(
            [
                boxes[:, 0] - boxes[:, 2] / 2,
                boxes[:, 1] - boxes[:, 3] / 2,
                boxes[:, 0] + boxes[:, 2] / 2,
                boxes[:, 1] + boxes[:, 3] / 2,
            ],
            axis=1,
        )
        keep = _nms(xyxy, confidences, iou_threshold)
        for index in keep:
            person = int(labels[index]) == 0
            people.append(
                {
                    "person": person,
                    "score": round(float(confidences[index]), 4),
                    "box": [
                        round(float(np.clip((xyxy[index, 0] - pad[0]) / gain, 0, width - 1)), 1),
                        round(float(np.clip((xyxy[index, 1] - pad[1]) / gain, 0, height - 1)), 1),
                        round(float(np.clip((xyxy[index, 2] - pad[0]) / gain, 0, width - 1)), 1),
                        round(float(np.clip((xyxy[index, 3] - pad[1]) / gain, 0, height - 1)), 1),
                    ],
                }
            )
        people.sort(key=lambda item: (not item["person"], -item["score"]))

    return {
        "ok": True,
        "op": "det",
        "width": width,
        "height": height,
        "infer_ms": round(infer_ms, 1),
        "providers": _state["providers"],
        "ep": _state["ep"],
        "detections": people,
        "person_count": sum(1 for p in people if p["person"]),
    }


def op_health(request: dict) -> dict:
    load_all()
    return {
        "ok": True,
        "op": "health",
        "providers": _state["providers"],
        "ep": _state["ep"],
        "ep_threads": _state["ep_threads"],
        "ep_fallback": _state["ep_fallback"],
        # 运行期真话：EP 会话在，但刚报过错就不能算「有效」
        "ep_effective": bool(_state.get("ep")) and not _state.get("runtime_fail"),
        "runtime_fail": _state.get("runtime_fail", 0),
        "last_runtime_error": _state.get("last_runtime_error"),
        "last_recovery": _state.get("last_recovery"),
        "det_ep": _state.get("det_ep"),
        "det_enabled": os.environ.get("BOOTH_DET_ENABLED", "false").strip().lower()
        in ("true", "1", "yes"),
        "det_requested_threads": _state.get("det_ep_threads"),
        "gesture_ep": _state.get("gesture_ep"),
        "gesture_requested_threads": _state.get("gesture_ep_threads"),
        "gesture_providers": _state.get("gesture_providers", []),
        "gesture_ep_fallback": _state.get("gesture_ep_fallback"),
        "gesture_device": _state.get("gesture_device"),
        "shared_ep_threads": os.environ.get("BOOTH_EP_SHARED_THREADS") or None,
        "thread_budget": dict(EP_THREADS),
        "load_ms": _state["load_ms"],
        "seg_model": DEFAULTS["seg_model"],
        "det_model": DEFAULTS["det_model"] if _state["det"] is not None else None,
        "gesture_model": DEFAULTS["gesture_model"] if _state.get("gesture") is not None else None,
        "text_cache": len(_state["text_cache"]),
        "matte_model": DEFAULTS["matte_model"] if matte_available() else None,
        "matte_load_ms": _state.get("matte_load_ms"),
        "matte_device": "cpu",
        "clip_model": DEFAULTS["clip_model"],
        "clip_loaded": _state["clip"] is not None,
        "ep_ladder": list(EP_THREAD_LADDER),
    }


HANDLERS = {"seg": op_seg, "det": op_det, "gesture": op_gesture,
            "switch": op_switch, "matte": op_matte, "health": op_health}


def main() -> None:
    if "--preload" in sys.argv:
        load_all()
        sys.stdout.write(PROTOCOL_PREFIX + json.dumps({"ok": True, "op": "preload", "load_ms": _state["load_ms"]}) + "\n")
        sys.stdout.flush()
        if "--exit-after-preload" in sys.argv:
            return

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
        except Exception as exc:
            sys.stdout.write(PROTOCOL_PREFIX + json.dumps({"ok": False, "error": f"bad json: {exc}"}) + "\n")
            sys.stdout.flush()
            continue

        handler = HANDLERS.get(request.get("op", "seg"))
        started = time.perf_counter()
        try:
            response = handler(request) if handler else {"ok": False, "error": f"unknown op {request.get('op')}"}
        except Exception as exc:  # 保持进程存活
            op = request.get("op", "seg")
            if handler and op in ("seg", "det", "gesture") and _is_runtime_ep_error(exc):
                # 运行期 EP 故障：自愈一次再重试（只重试一次，不掩盖问题）
                _state["runtime_fail"] = _state.get("runtime_fail", 0) + 1
                _state["last_runtime_error"] = f"{type(exc).__name__}: {exc}"[:300]
                try:
                    recovery = recover_runtime(exc)
                    response = handler(request)
                    response["recovered"] = recovery
                    if response.get("ok"):
                        _state["runtime_fail"] = 0
                        _state["last_recovery"] = recovery
                except Exception as retry_exc:
                    response = {"ok": False,
                                "error": f"{type(retry_exc).__name__}: {retry_exc}",
                                "recovery_failed": True,
                                "first_error": f"{type(exc).__name__}: {exc}"[:200]}
            else:
                response = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        response["id"] = request.get("id")
        response["total_ms"] = round((time.perf_counter() - started) * 1000.0, 1)
        sys.stdout.write(PROTOCOL_PREFIX + json.dumps(response, ensure_ascii=False) + "\n")
        sys.stdout.flush()


if __name__ == "__main__":
    signal.signal(signal.SIGINT, lambda *_: sys.exit(0))
    main()
