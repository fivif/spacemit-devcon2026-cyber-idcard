# -*- coding: utf-8 -*-
"""成片渲染 —— SpacemiT DevCon 2026 · 装置5「赛博开发者证件照」· T-3

把现场抓拍按实例分割掩码抠出人像，**原色**贴进官方主视觉的人像窗。

**风格只剩「原色」一档**（现场定的）：证件照就该是本人的颜色。早期的四档强风格化
（highcontrast / mono / dieline / halftone）看着像海报、本人不认，代码已删。
`STYLES` 是唯一风格清单，`booth_server.styles()` 从这里读，不抄第二份（坑 23）。

**原色不做暗调校准。** 下面那套「把整块板子压到全图均值 15–35」的对象是
暗底上的人像板；原色的脸要留在暗底上，进了那个窗口就是剪影。原色单独一条判据：
``natural_metrics_ok()``。

暗调那一套仍然在，现在是 card.py 六张卡与掩码预览的底子
（knowledge/03 §1/§2/§11）
------------------------
**低照度人像的物理规律**：人物受的是轮廓光/踢光，脸在阴影里，只有少量镜面高光。
所以人像调子先做一次低照度压缩：

    v = clip((luma * 单光源方向 - 0.26) / 0.60, 0, 1) ** 1.35

脸落在 v≈0.3（暗中间调），只有 luma>0.83 的镜面才进最亮一档 → 高光稀少而成立。
再叠加三级模糊 glow（芯光 / 近晕 / 远晕），最后 `calibrate_dark()` 闭环校准：

    全图平均亮度 15-35 / 像素值>200 的占比 1%-6% / 细节能量（|图−高斯(σ=3)|>12）> 3%

校准的两个旋钮：曝光 g（钉住均值）与对比度 k（在 [0.8,1.5] 内微调高光占比）。
k 有界是刻意的 —— 无界会把暗调压成灰饼（本任务踩过，见报告）。

约束:
- 只依赖 PIL + numpy，**不 import cv2**（K3 上 cv2 与 spacemit_ort 同进程会崩，§1.1）。
- mask 为 None / 全空 → 退化为整幅风格化，**不得抛异常**（§9「推理超时 → L0」）。
- 只画「人像 + 底」，不画任何文字/编号/水印 —— 那是 card.py / watermark.py 的事。
"""

from __future__ import annotations

import os

import numpy as np
from PIL import Image, ImageFilter

# --------------------------------------------------------------------------
# 冻结接口（docs/spec/系统规格-v2.md §11.2）
# --------------------------------------------------------------------------

# **只有原色一档**。早期还有 highcontrast / mono / dieline / halftone
# 四档，现场判定：证件照就该是本人的颜色，
# 四档反而把人变成了符号；且选项多了影响拍摄节奏。
# 这四档已从代码里删除（旧版资产清单已并入 docs/）。
STYLES = ("natural",)

STYLE_LABELS = {"natural": "原色"}

BRAND = (178, 228, 13)          # #B2E40D 酸柠绿（spacemit.com 品牌色）
BRAND_SOFT = (208, 238, 144)    # #D0EE90

# 抠像成片里，人至少要占窗口这么大一块 —— 低于它就是「框里其实没人」
# （实测：真人是 0.30~0.49，误检碎块是 0.0000；见 knowledge/04 坑 27）
CUTOUT_MIN_COVER = 0.02

MEAN_RANGE = (15.0, 35.0)          # 暗调窗口：card.py 的卡片还在用
HI_RANGE = (1.0, 6.0)
DETAIL_MIN = 3.0

NATURAL_MEAN_RANGE = (12.0, 60.0)  # 原色成片：不是一片黑
NATURAL_HI_MAX = 6.0               # 原色成片：高光没爆

# --------------------------------------------------------------------------
# 四套风格调色板
#
# 规则：**只有真正的高光才允许超过 200**。中间调与辅助色的通道上限压到 <=196，
# 否则"通道最大值 >200"的占比会远超"亮度 >200"的占比，两个口径对不上。
# --------------------------------------------------------------------------

# 算力视图的掩码预览图是**独立于成片风格**的一张图（它只服务大屏），
# 所以自带一套配色，不去借成片的调色板 —— 成片风格收窄时才不会被连带。
MASK_PREVIEW_PAL = {
    "paper": (5, 8, 6), "paper2": (12, 19, 13),
    "glow": (72, 246, 156), "glow2": (178, 228, 13),
}

PALETTES = {
    # 原色：不压暗、不去色。底板是暗的，人物保持现场真实颜色，
    # 贴进去之后的「同光」由 render/keyvisual._into_scene 负责。
    "natural": {
        "paper": (8, 10, 14), "paper2": (20, 26, 34),
        "glow": (168, 214, 255), "glow2": (178, 228, 13),
        "rim": (198, 236, 156),
    },
}


# --------------------------------------------------------------------------
# 基础工具
# --------------------------------------------------------------------------


def _as_array(img) -> np.ndarray:
    return np.asarray(img.convert("RGB"), np.float32)


def luma(arr: np.ndarray) -> np.ndarray:
    """BT.601 亮度（与 PIL convert("L") 同系数，验收指标按这一口径算）。"""
    return arr[..., 0] * 0.299 + arr[..., 1] * 0.587 + arr[..., 2] * 0.114


def _to_image(arr: np.ndarray) -> Image.Image:
    return Image.fromarray(np.clip(arr, 0, 255).astype(np.uint8), "RGB")


def _blur_l(arr: np.ndarray, radius: float) -> np.ndarray:
    if radius <= 0:
        return arr.astype(np.float32)
    im = Image.fromarray(np.clip(arr, 0, 255).astype(np.uint8), "L")
    return np.asarray(im.filter(ImageFilter.GaussianBlur(radius)), np.float32)


def _blur_rgb(arr: np.ndarray, radius: float) -> np.ndarray:
    if radius <= 0:
        return arr.astype(np.float32)
    return np.asarray(_to_image(arr).filter(ImageFilter.GaussianBlur(radius)), np.float32)


def _dilate(m: np.ndarray, k: int = 1) -> np.ndarray:
    out = m.copy()
    for dy in range(-k, k + 1):
        for dx in range(-k, k + 1):
            if dx == 0 and dy == 0:
                continue
            out |= np.roll(np.roll(m, dy, axis=0), dx, axis=1)
    return out


def _erode(m: np.ndarray, k: int = 1) -> np.ndarray:
    return ~_dilate(~m, k)


def _edges(m: np.ndarray) -> np.ndarray:
    f = m.astype(np.float32)
    gx = np.zeros_like(f)
    gy = np.zeros_like(f)
    gx[:, 1:-1] = f[:, 2:] - f[:, :-2]
    gy[1:-1, :] = f[2:, :] - f[:-2, :]
    return np.sqrt(gx * gx + gy * gy) > 0.5


def _trim(m: np.ndarray) -> np.ndarray:
    """去掉贴边像素：掩码在画幅边缘常有整条误检，会让描边糊满边框。"""
    m = m.copy()
    m[0, :] = False
    m[-1, :] = False
    m[:, 0] = False
    m[:, -1] = False
    return m


def _mask_array(mask, shape) -> np.ndarray:
    """任意掩码输入 → 与画幅同尺寸的 bool 数组。None / 空 → 全 False。"""
    h, w = shape
    if mask is None:
        return np.zeros((h, w), bool)
    if isinstance(mask, Image.Image):
        arr = np.asarray(mask.convert("L"))
    else:
        arr = np.asarray(mask)
    if arr.ndim == 3:
        arr = arr[..., 0]
    if arr.dtype == bool:
        arr = arr.astype(np.uint8) * 255
    elif arr.size and float(np.nanmax(arr)) <= 1.0:
        arr = np.clip(arr.astype(np.float32) * 255.0, 0, 255)
    if arr.shape != (h, w):
        img = Image.fromarray((arr > 127).astype(np.uint8) * 255, "L")
        arr = np.asarray(img.resize((w, h), Image.NEAREST))
    return arr > 127


def _lerp3(a, b, t):
    """三色线性插值。a/b 为 (3,) float32，t 为 (...,1) 0..1。"""
    return a[None, None, :] + (b - a)[None, None, :] * t


# --------------------------------------------------------------------------
# 取景：人像在主画幅里的位置（掩码包围盒决定；掩码缺失时中央裁剪）
# --------------------------------------------------------------------------


def _robust_bbox(mask, keep: float = 0.06):
    """抗干扰包围盒：丢掉细碎的误检，只留有分量的行列。

    直接 np.where 取 min/max 会被一小撮游离像素带偏：matting 模型偶尔会在
    背景物体上漏出几十个像素，取景框因此被拉大，人就被挤到卡片一角。
    阈值取「最宽行/列的 6%」：手臂、脖子这类窄但真实的部位远高于这个数。
    """
    rows = mask.sum(axis=1)
    cols = mask.sum(axis=0)
    if not rows.any() or not cols.any():
        return None
    ry = np.where(rows >= max(1.0, float(rows.max()) * keep))[0]
    rx = np.where(cols >= max(1.0, float(cols.max()) * keep))[0]
    if not len(ry) or not len(rx):
        return None
    return float(rx[0]), float(ry[0]), float(rx[-1] + 1), float(ry[-1] + 1)


def _frame_box(mask, size, photo_size, pad: float = 0.34):
    """取景框：以人像包围盒为中心，扩 pad 倍并裁成 size 的宽高比，最后夹进画面。

    **比例是硬约束**：夹取时必须等比缩，不能分别夹宽和高（见下面的注释）。
    """
    W, H = photo_size
    w, h = size
    ar = w / float(h)
    bbox = _robust_bbox(mask) if mask is not None and mask.any() else None
    if bbox is not None:
        x0, y0, x1, y1 = bbox
        bw, bh = x1 - x0, y1 - y0
        cx, cy = 0.5 * (x0 + x1), 0.5 * (y0 + y1)
        if bw / max(bh, 1e-3) < ar:
            box_h = bh * (1.0 + pad)
            box_w = box_h * ar
        else:
            box_w = bw * (1.0 + pad)
            box_h = box_w / ar
        cy -= 0.05 * box_h
        box = (cx - box_w / 2, cy - box_h / 2, cx + box_w / 2, cy + box_h / 2)
    else:
        if W / float(H) > ar:
            box_h, box_w = float(H), float(H) * ar
        else:
            box_w, box_h = float(W), float(W) / ar
        cx, cy = W / 2.0, H * 0.44
        box = (cx - box_w / 2, cy - box_h / 2, cx + box_w / 2, cy + box_h / 2)

    x0, y0, x1, y1 = box
    bw, bh = x1 - x0, y1 - y0
    # 画面装不下就**整体等比缩**。绝不能把宽、高各自夹到画面里 —— 那样比例就没了：
    # 实测一张贴脸抓拍，目标是 0.78 的画布，裁切框被夹成 1.475，
    # 人被横向压到 53%（这就是「人像被压窄了」）。人越满画面压得越狠。
    shrink = min(1.0, float(W) / max(bw, 1e-6), float(H) / max(bh, 1e-6))
    bw, bh = max(2.0, bw * shrink), max(2.0, bh * shrink)
    cx = min(max(0.5 * (x0 + x1), bw / 2.0), W - bw / 2.0)
    cy = min(max(0.5 * (y0 + y1), bh / 2.0), H - bh / 2.0)
    return (cx - bw / 2.0, cy - bh / 2.0, cx + bw / 2.0, cy + bh / 2.0)


def _frame_inputs(photo, mask, size):
    """原片与掩码按同一取景框归一到 size。返回 (float 数组, bool 掩码, 框)。"""
    W, H = photo.size
    m_src = None
    if mask is not None:
        m_src = _mask_array(mask, (H, W))
        if not m_src.any():
            m_src = None
    box = _frame_box(m_src, size, (W, H))
    x0, y0, x1, y1 = (int(round(v)) for v in box)
    x1 = max(x1, x0 + 2)
    y1 = max(y1, y0 + 2)
    crop = photo.convert("RGB").crop((x0, y0, x1, y1)).resize(size, Image.LANCZOS)
    out = _as_array(crop)
    if m_src is None:
        return out, np.zeros((size[1], size[0]), bool), (x0, y0, x1, y1)
    mcrop = Image.fromarray((m_src * 255).astype(np.uint8), "L").crop((x0, y0, x1, y1))
    m = np.asarray(mcrop.resize(size, Image.LANCZOS), np.float32) > 127.0
    return out, _trim(m), (x0, y0, x1, y1)


# --------------------------------------------------------------------------
# 暗底：单一光源 + 网格 + 材质
# --------------------------------------------------------------------------


def _light_field(w: int, h: int) -> np.ndarray:
    """单一光源（左上）的亮度场 0..1。"""
    yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)
    lx, ly = 0.16 * w, 0.06 * h
    r = np.sqrt(((xx - lx) / (w * 0.92)) ** 2 + ((yy - ly) / (h * 0.92)) ** 2)
    return np.clip(1.0 - r, 0.0, 1.0) ** 2.1


def _dark_base(w: int, h: int, pal: dict, seed: int = 7, mask=None) -> np.ndarray:
    """暗底：光源场 + 人像身后聚光 + 细网格 + 材质噪点 + 每 3 行扫描线。"""
    rng = np.random.default_rng(seed)
    field = _light_field(w, h)
    yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)
    if mask is not None and mask.any():
        ys, xs = np.where(mask)
        cx, cy = float(xs.mean()), float(ys.mean())
        rx = max(60.0, float(xs.max() - xs.min()) * 0.85)
        ry = max(60.0, float(ys.max() - ys.min()) * 0.75)
    else:
        cx, cy, rx, ry = w * 0.5, h * 0.42, w * 0.55, h * 0.5
    spot = np.clip(1.0 - np.sqrt(((xx - cx) / rx) ** 2 + ((yy - cy) / ry) ** 2), 0.0, 1.0) ** 2.4

    paper = np.array(pal["paper"], np.float32)
    paper2 = np.array(pal["paper2"], np.float32)
    level = np.clip(field * 0.85 + spot * 0.55, 0.0, 1.0)
    base = paper[None, None, :] + (paper2 - paper)[None, None, :] * (level * 0.75)[..., None]

    step = max(22, int(round(w / 34.0)))
    grid = np.zeros((h, w), np.float32)
    grid[::step, :] = 1.0
    grid[:, ::step] += 0.85
    tick = np.zeros((h, w), np.float32)
    tick[::step * 5, :] = 1.0
    tick[:, ::step * 5] += 1.0
    base += ((grid * 11.0 + tick * 9.0) * (0.35 + 0.65 * field))[..., None] * np.array([0.85, 0.95, 1.0], np.float32)

    base += rng.normal(0.0, 2.6, (h, w))[..., None]                 # 材质细噪点
    base += (np.arange(h) % 3 == 0)[:, None, None] * -2.2           # 每 3 行一条极轻扫描线
    return base


# --------------------------------------------------------------------------
# 三级模糊 glow（芯光 / 近晕 / 远晕）
# --------------------------------------------------------------------------


# 抠像软边的去背景色污染。软 alpha 的过渡带里，原片像素**本身就是混合色**
# （C = a·F + (1-a)·B），直接贴上去，背景亮的时候人周围就留一圈浅雾边
# —— 现场是办公室，头后面就是亮窗，这一圈最明显。这里用 alpha 加权的邻域均值
# 估出真正的前景色 F（哪边是人的权重就大），过渡带改用 F 上色。
MATTE_DECON = os.environ.get("BOOTH_MATTE_DECON", "1") not in ("0", "", "false", "False")
MATTE_DECON_R = float(os.environ.get("BOOTH_MATTE_DECON_R", "3"))


def _decontaminate(rgb: np.ndarray, alpha: np.ndarray, radius: float = 0.0) -> np.ndarray:
    """把人像层的过渡带颜色从「人+旧背景」换成「人自己」的颜色。"""
    r = MATTE_DECON_R if not radius else radius
    a = np.clip(np.asarray(alpha, np.float32), 0.0, 1.0)[..., None]
    num = _blur_rgb(np.asarray(rgb, np.float32) * a, r)
    den = _blur_l(a[..., 0] * 255.0, r)[..., None] / 255.0
    fg = num / np.maximum(den, 1e-3)
    return np.clip(np.asarray(rgb, np.float32) * a + fg * (1.0 - a), 0.0, 255.0)


def _apply_glow(base: np.ndarray, glow: np.ndarray, w: int, pal: dict) -> np.ndarray:
    out = base
    for frac, gain, tint in ((0.010, 0.50, pal["glow"]),
                             (0.030, 0.30, pal["glow2"]),
                             (0.075, 0.18, pal["glow"])):
        soft = _blur_l(glow, max(1.2, w * frac)) / 255.0
        add = soft[..., None] * np.array(tint, np.float32)[None, None, :] * gain
        out = 255.0 - (255.0 - out) * (255.0 - np.clip(add, 0, 255)) / 255.0
    return out


def _finish(base: np.ndarray) -> np.ndarray:
    h, w = base.shape[:2]
    yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)
    r = np.sqrt(((xx - w / 2) / (w / 2)) ** 2 + ((yy - h / 2) / (h / 2)) ** 2)
    vig = np.clip(1.0 - 0.30 * np.clip(r - 0.45, 0, None) ** 1.35, 0.55, 1.0)
    return base * vig[..., None]


# --------------------------------------------------------------------------
# 人像调子（低照度 + 单一光源）
# --------------------------------------------------------------------------


def _grade_natural(photo: np.ndarray) -> np.ndarray:
    """原色分级：白平衡 + 轻微对比/饱和，**不改变人物本来的颜色**。

    现场是办公室顶灯，色温不定。灰世界白平衡能把偏色拉回来，但画面本来就偏色时
    它会矫枉过正 —— 所以只施加一半校正，并把增益夹在 [0.86, 1.16]。
    另外暗底上人脸容易糊成一团，暗部抬一点、高光压一点，脸才立得住。
    """
    x = np.clip(np.asarray(photo, np.float32), 0.0, 255.0) / 255.0
    mean = x.reshape(-1, 3).mean(axis=0)
    gain = np.clip(float(mean.mean()) / np.maximum(mean, 1e-4), 0.86, 1.16)
    x = np.clip(x * (0.5 + 0.5 * gain)[None, None, :], 0.0, 1.0)
    # S 曲线只混 32% —— 展会现场多是背光，一点反差都没有的话人会糊在墙上；
    # 再重就不是「原像」了。
    s = x * x * (3.0 - 2.0 * x)
    x = 0.68 * x + 0.32 * s
    x = np.clip((x - 0.045) / 0.955, 0.0, 1.0)
    # 饱和度 +10%：白平衡那一步顺手带走了一点色
    gray = (x @ np.array([0.299, 0.587, 0.114], np.float32))[..., None]
    x = np.clip(gray + (x - gray) * 1.10, 0.0, 1.0)
    # 高光软肩：展会灯直打额头，硬切会糊成一块白
    x = np.where(x > 0.90, 0.90 + (x - 0.90) * 0.55, x)
    return np.clip(x * 255.0, 0.0, 255.0)


def _compose_natural(photo, mask, base, pal, seed):
    """原色：人就是照片里的人，只在外轮廓补一圈品牌色光。"""
    h, w = mask.shape
    graded = _grade_natural(photo)
    out = base.copy()
    glow = np.zeros((h, w), np.float32)
    if mask.any():
        out[mask] = graded[mask]
        # rim=None 就是「纯贴图」档：抠像成片把人原样贴到背景板上，不描边、不泛光。
        rim_c = pal.get("rim")
        if rim_c is None:
            return out, glow
        rim = _dilate(_edges(mask), 1) & mask
        rim_c = np.array(rim_c, np.float32)
        out[rim] = graded[rim] * 0.62 + rim_c[None, :] * 0.38
        # 只让最亮的那点高光（额头反光、镜片）泛品牌冷光。原色档若把整张脸
        # 都罩一层光，人就像蒙了层纱 —— 那正是「不是原像」的来源。
        v = luma(graded)
        glow = np.where(mask, np.clip((v - 176.0) / 79.0, 0.0, 1.0) * 70.0, 0.0)
    else:
        out = graded
    return out, glow


_COMPOSERS = {"natural": _compose_natural}


# --------------------------------------------------------------------------
# 暗调校准（闭环：解曝光钉住均值，解对比度微调高光占比）
# --------------------------------------------------------------------------

_PIVOT = 26.0


def _solve_gain(a: np.ndarray, target: float, bounds=(0.05, 9.0), iters: int = 16) -> float:
    lo, hi = bounds
    for _ in range(iters):
        mid = 0.5 * (lo + hi)
        if float(luma(np.clip(a * mid, 0, 255)).mean()) < target:
            lo = mid
        else:
            hi = mid
    return 0.5 * (lo + hi)


def _soft_knee(x: np.ndarray, m: float = 178.0, s: float = 46.0) -> np.ndarray:
    """高光软压缩：只抬不动暗部，用来把"高光占比"压回窗口。"""
    return np.where(x <= m, x, m + (x - m) / (1.0 + (x - m) / s))


def calibrate_dark(arr: np.ndarray, *, mean_target: float = 25.0, hi_lo: float = 1.5,
                   hi_hi: float = 5.5, k_range=(0.80, 1.50), passes: int = 7) -> np.ndarray:
    """把一张暗底画面校准进硬指标窗口。

    两个旋钮：曝光 g（把均值钉在 mean_target）与对比度 k（绕 _PIVOT 拉伸，
    影响"像素值 > 200"的占比）。k 有界 —— 无界会把暗调压成灰饼。
    """
    a = np.clip(np.asarray(arr, np.float32), 0.0, 255.0)
    small = a[::3, ::3]
    k = 1.0
    out = a
    for _ in range(passes):
        b = np.clip((a - _PIVOT) * k + _PIVOT, 0, 255)
        g = _solve_gain(np.clip((small - _PIVOT) * k + _PIVOT, 0, 255), mean_target)
        out = np.clip(b * g, 0, 255)
        for _ in range(2):                     # 全分辨率精修均值
            cur = float(luma(out).mean())
            if abs(cur - mean_target) < 0.5:
                break
            g = float(np.clip(g * (mean_target / max(cur, 1e-3)) ** 0.95, 0.02, 12.0))
            out = np.clip(b * g, 0, 255)
        hi_l = float((luma(out) > 200).mean() * 100.0)
        hi_m = float((out.max(2) > 200).mean() * 100.0)
        if hi_m > hi_hi and k > k_range[0]:
            k = max(k_range[0], k * 0.90)
            continue
        if hi_l < hi_lo and k < k_range[1]:
            k = min(k_range[1], k * 1.12)
            continue
        break
    hi_m = float((out.max(2) > 200).mean() * 100.0)
    if hi_hi and hi_m > hi_hi:                 # 仍超标 → 只压高光，整体曝光再解一次
        b = _soft_knee(out, 176.0, 52.0)
        g = _solve_gain(b[::3, ::3], mean_target)
        out = np.clip(b * g, 0, 255)
    return out


def dark_metrics(image: Image.Image) -> dict:
    """验收指标（knowledge/03 §11）。细节能量用高频 |L − 高斯(σ=3)|，不是裸标准差。"""
    rgb = np.asarray(image.convert("RGB"), np.float32)
    L = luma(rgb)
    Lp = np.asarray(image.convert("L"), np.float32)
    soft = np.asarray(image.convert("L").filter(ImageFilter.GaussianBlur(3)), np.float32)
    return {
        "size": (int(image.size[0]), int(image.size[1])),
        "mean_luma": round(float(L.mean()), 2),
        "hi_luma_pct": round(float((L > 200).mean() * 100.0), 2),
        "hi_max_pct": round(float((rgb.max(2) > 200).mean() * 100.0), 2),
        "detail_pct": round(float((np.abs(Lp - soft) > 12).mean() * 100.0), 2),
    }


def natural_metrics_ok(mt: dict) -> bool:
    """原色成片的判据：不是一片黑、高光没爆。

    旧判据是暗调窗口（``MEAN_RANGE`` 15–35 / ``HI_RANGE`` 1–6%），
    那是给**已删除的四档**钉的 —— 原色刻意不进那个窗口（把脸压到 30 就成了剪影）。
    放在这里而不是抄进每个调用方 —— 抄过就会分弆（坑 23）。

    注意它只算「没出事」，不算「好不好看」—— 那个要人眼看。
    上下界提成 ``NATURAL_MEAN_RANGE`` / ``NATURAL_HI_MAX``：汇总页要引它，
    印在纸上的判据和代码里的判据必须是同一份（坑 23）。
    """
    return bool(NATURAL_MEAN_RANGE[0] <= mt["mean_luma"] <= NATURAL_MEAN_RANGE[1]
                and mt["hi_luma_pct"] <= NATURAL_HI_MAX)


def check_dark(image: Image.Image) -> bool:
    """成片是否满足三条硬指标。"""
    m = dark_metrics(image)
    return bool(MEAN_RANGE[0] <= m["mean_luma"] <= MEAN_RANGE[1]
                and HI_RANGE[0] <= m["hi_luma_pct"] <= HI_RANGE[1]
                and m["detail_pct"] > DETAIL_MIN)


# --------------------------------------------------------------------------
# 对外入口（冻结签名）
# --------------------------------------------------------------------------


def render(style: str, photo, mask, *, size=(1000, 1250), seed: int = 7) -> Image.Image:
    """按风格渲染人像板。  style: natural（当前只有这一档）

    photo: 原片（任意尺寸，内部按掩码取景后 cover 到 size）
    mask:  人像掩码（PIL L 图或 (H,W) 数组）。None / 全空 → 整幅风格化，不抛异常。
    """
    if style not in _COMPOSERS:
        raise ValueError(f"unknown style: {style!r} (expected one of {STYLES})")
    w, h = int(size[0]), int(size[1])
    pal = PALETTES[style]

    photo_arr, mask_b, _box = _frame_inputs(photo, mask, (w, h))
    base = _dark_base(w, h, pal, seed=seed, mask=mask_b if mask_b.any() else None)
    out, glow = _COMPOSERS[style](photo_arr, mask_b, base, pal, seed)
    out = _apply_glow(out, glow, w, pal)
    out = _finish(out)
    # 原色刻意不进暗调窗口：它存在的意义就是把人拍成他本来的样子。
    return _to_image(out)


def render_cutout(style: str, photo, alpha, *, size=(780, 1000), seed: int = 7):
    """抠像成片：和 render() 同一套取景与调色，另外把 matting 的软 alpha 对齐到同尺寸。

    和 render() 的区别只有三处，都是为了「只把人带出去」：
      1. 掩码外填原片而不是暗底（软边才对得上）；
      2. 不压暗角、原色档不描边；
      3. alpha 用 matting 自己的包围盒取景、跟着一起缩放。

    返回 ``(RGB 图, (H,W) float32 alpha)``。调用方按这份 alpha 把人贴到背景板上 ——
    背景不进来，边是软的（发丝、半透明边缘都在）。

    取景用 alpha 自己的包围盒，不是检测框：框是矩形的，人是顺着轮廓的。
    """
    if style not in _COMPOSERS:
        raise ValueError(f"unknown style: {style!r} (expected one of {STYLES})")
    w, h = int(size[0]), int(size[1])
    pal = PALETTES[style]
    if style == "natural":
        pal = dict(pal, rim=None)     # 原色档 = 纯贴图（见 _compose_natural）

    arr = alpha.convert("L") if isinstance(alpha, Image.Image) else alpha
    src = np.asarray(arr, np.float32) / 255.0
    if src.ndim == 3:
        src = src[..., 0]
    if src.shape[:2] != (photo.size[1], photo.size[0]):
        src = np.asarray(
            Image.fromarray((src * 255).astype(np.uint8)).resize(photo.size, Image.BILINEAR),
            np.float32) / 255.0

    binary = src > 0.5
    if not binary.any():
        return None                      # 一个人都没有：交给调用方走整幅取景

    photo_arr, mask_b, box = _frame_inputs(photo, binary, (w, h))
    base = _dark_base(w, h, pal, seed=seed, mask=mask_b if mask_b.any() else None)
    out, glow = _COMPOSERS[style](photo_arr, mask_b, base, pal, seed)
    out = _apply_glow(out, glow, w, pal)
    # 这里**不做** _finish()：暗角是给整块人像板做氛围的，粘到背景板上的人再压一圈
    # 暗角，边缘会比周围暗一档，看着像贴纸。
    # 最后一道：掩码外一律填**原片**。抠像的边本来就是软的，合成时过渡带会把垫在下面的
    # 像素按比例混进来 —— 垫的是暗底就混出一圈灰黑描边，那正是「边不干净」的主因，
    # 不是模型抠得不准。垫原片，过渡带混出来的就是人自己的颜色。
    out = np.where(mask_b[..., None], out, _grade_natural(photo_arr))

    # 贴出去用的那份软 alpha：按 matting 包围盒取景、跟着一起缩放，最后掐掉一点灰雾
    # （matting 的边常带一层 5% 的浅灰，留着像蒙了层纱）。
    x0, y0, x1, y1 = (int(round(v)) for v in box)
    cropped = Image.fromarray((src * 255).astype(np.uint8), "L").crop((x0, y0, x1, y1))
    soft = np.asarray(cropped.resize((w, h), Image.BILINEAR), np.float32) / 255.0
    soft = np.clip((soft - 0.06) / 0.88, 0.0, 1.0)

    # **取景框里必须真的有人。** 检测/抠像偶尔会给出一条贴着画面边沿的碎块
    # （人不在镜头里、只有桌角的一小块皮肤时最容易出现），取景框按包围盒中心
    # 取过去正好落在空白处 —— 那时贴出去的就是一份空 alpha：参会者拿到一张
    # 只有背景板的照片，**而且全程不报错**。
    # 判据用「框里占多大面积」，实测分得很开：有人 0.30~0.49，碎块 0.0000。
    if float((soft > 0.5).mean()) < CUTOUT_MIN_COVER:
        return None                     # 和「一个人都没有」同一条退路：走整幅取景

    # 去污染要用**贴出去的那份 alpha**：过渡带在哪儿由它定义，用二值掩码去算等于没算。
    if MATTE_DECON:
        out = _decontaminate(out, soft)
    return _to_image(out), soft


def mask_preview_png(mask) -> Image.Image:
    """算力视图用：掩码画成暗调预览图（近黑底 + 酸柠绿轮廓 + 三级 glow）。"""
    arr = None
    if mask is not None:
        a = np.asarray(mask.convert("L") if isinstance(mask, Image.Image) else mask)
        if a.ndim == 3:
            a = a[..., 0]
        if a.shape[0] and a.shape[1]:
            arr = _mask_array(mask, a.shape)
    if arr is None or not arr.any():
        arr = np.zeros((540, 960), bool)
    h, w = arr.shape
    m = _trim(arr)
    pal = MASK_PREVIEW_PAL
    canvas = np.zeros((h, w, 3), np.float32)
    canvas += np.array(pal["paper"], np.float32)[None, None, :]
    canvas[m] = np.array((26, 48, 28), np.float32)
    edge = _dilate(_edges(m), 1)
    glow = np.zeros((h, w), np.float32)
    glow[_dilate(_edges(m), 2)] = 235.0
    canvas = _apply_glow(canvas, glow, w, pal)
    canvas[edge] = np.array(BRAND, np.float32)
    return _to_image(canvas)


def self_test() -> dict:
    """结构自检：原色可渲染且不是剪影、掩码缺失可降级、取景框比例不改。"""
    out = {}
    W, H = 640, 480
    yy, xx = np.mgrid[0:H, 0:W]
    a = np.zeros((H, W, 3), np.float32)
    a[..., 0] = 34 + 40 * (yy / H)
    a[..., 1] = 38 + 44 * (yy / H)
    a[..., 2] = 46 + 52 * (yy / H)
    face = (((xx - 320) ** 2 / 90.0 ** 2 + (yy - 200) ** 2 / 118.0 ** 2) < 1.0)
    a[face] = (226, 190, 170)
    a[(yy > 330) & (yy < 470) & (xx > 200) & (xx < 440)] = (52, 60, 78)
    photo = Image.fromarray(np.clip(a, 0, 255).astype(np.uint8), "RGB")
    mask = np.zeros((H, W), bool)
    mask[90:470, 210:430] = True
    img = render("natural", photo, mask, size=(360, 450), seed=3)
    mt = dark_metrics(img)
    out["natural_size"] = mt["size"]
    out["natural_mean"] = mt["mean_luma"]
    out["natural_hi"] = mt["hi_luma_pct"]
    out["natural_detail"] = mt["detail_pct"]
    rgb = np.asarray(img.convert("RGB"), np.float32)
    out["natural_chroma"] = round(float(np.abs(rgb.max(2) - rgb.min(2)).mean()), 2)

    # 原色这一档**刻意不满足**暗调窗口（均值 15–35 就是把人变成剪影）。
    # 但「不进窗口」不等于「没有判据」：和**同一张图的暗调底板**比 ——
    # 底板均值是这次运行里现成的参照物，阈值跟着它走，换测试图也不会漂。
    # 参照物 = **同一张图走一遍当年四档共用的暗调校准**（均值钉在 30）。
    # 它正是原色被定义为「不该变成的那一档」，而且跟着这张图走，不是钉死阈值。
    dark_ref = _to_image(calibrate_dark(np.asarray(img.convert("RGB"), np.float32),
                                       mean_target=30.0, hi_lo=2.4, hi_hi=5.2))
    out["dark_ref_mean"] = round(float(dark_metrics(dark_ref)["mean_luma"]), 2)
    out["natural_ok"] = bool(out["natural_chroma"] > 10.0
                             and out["natural_mean"] > 1.5 * out["dark_ref_mean"])
    out["mask_none_ok"] = render("natural", photo, None, size=(360, 450), seed=3).size == (360, 450)
    out["mask_empty_ok"] = render("natural", photo, np.zeros((H, W), bool),
                                  size=(360, 450), seed=3).size == (360, 450)
    out["mask_preview_ok"] = mask_preview_png(mask).size == (640, 480)
    # 取景框必须**等比**夹进画面。这里刻意用一个「又宽又满」的掩码复现现场那张贴脸抓拍
    # （人像包围盒 1.18，目标画布 0.78）：夹取后比例一旦被改，成片里的人就是被压扁的。
    wide = np.zeros((H, W), bool)
    wide[60:470, 40:600] = True
    fb = _frame_box(wide, (360, 460), (W, H))
    out["frame_box"] = [round(float(v), 1) for v in fb]
    out["frame_aspect"] = round((fb[2] - fb[0]) / max(1e-6, fb[3] - fb[1]), 4)
    out["frame_aspect_ok"] = abs(out["frame_aspect"] - 360 / 460.0) < 1e-3
    out["mask_preview_none_ok"] = mask_preview_png(None).size == (960, 540)
    out["ok"] = all(v for k, v in out.items() if isinstance(v, bool))
    return out


if __name__ == "__main__":
    import json

    print(json.dumps(self_test(), ensure_ascii=False, indent=2, default=str))