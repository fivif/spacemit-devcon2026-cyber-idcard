# -*- coding: utf-8 -*-
"""底图制作工具 —— 把出图裁成与成片同尺寸（1600x900）的底板，并测出人像窗。

为什么要有这一步：出图的比例（1.49）和成片（16:9 = 1.78）对不上，必须裁。
**裁哪儿不能随便**：这张图左上角是标题字、右下角是芯片，只有底部可以割。
所以统一按「按宽度铺满 → 从下往上割」处理，把变换写死在代码里，可复现。

用法：
    python3 tools/make_keyvisual.py <源图> <输出png> [--scan]
"""
from __future__ import annotations

import argparse
import os
import sys

import numpy as np
from PIL import Image

_HERE = os.path.dirname(os.path.abspath(__file__))
_APP = os.path.normpath(os.path.join(_HERE, ".."))
if _APP not in sys.path:
    sys.path.insert(0, _APP)

CANVAS = (1600, 900)          # 与 render.keyvisual.WIDE 一致


def bake(src_path: str, dst_path: str) -> Image.Image:
    """按宽度铺满、顶部对齐、底部裁掉多余 —— 与 render.keyvisual 的成片同尺寸。"""
    img = Image.open(src_path).convert("RGB")
    w, h = img.size
    tw, th = CANVAS
    scale = tw / float(w)
    nh = int(round(h * scale))
    img = img.resize((tw, nh), Image.LANCZOS)
    if nh >= th:                       # 高了：从下往上割（保顶部标题）
        img = img.crop((0, 0, tw, th))
    else:                              # 矮了：左右居中补边（描边复制，不引新颜色）
        pad = Image.new("RGB", (tw, th))
        edge = img.crop((0, 0, 1, nh)).resize((1, th), Image.LANCZOS)
        pad.paste(edge.resize((tw, th), Image.NEAREST), (0, 0))
        pad.paste(img, ((tw - tw) // 2, (th - nh) // 2))
        img = pad
    img.save(dst_path, "PNG", optimize=True)
    return img


def calm_score(a: np.ndarray, box) -> tuple:
    """"这块地方干净吗"：边缘能量 + 亮度标准差。越小越适合放人像。"""
    x0, y0, x1, y1 = (int(round(v)) for v in box)
    b = a[y0:y1, x0:x1]
    gx = np.abs(np.diff(b, axis=1)).mean() if b.shape[1] > 1 else 0.0
    gy = np.abs(np.diff(b, axis=0)).mean() if b.shape[0] > 1 else 0.0
    return float(gx + gy), float(b.std())


def scan(img: Image.Image, ar: float = 0.78, y_min: float = 0.40, y_max: float = 0.985,
         x_min: float = 0.015, x_max: float = 0.44, steps: int = 26) -> dict:
    """在左栏里扫一块宽高比 = ar 的窗口，选最干净的位置。返回归一化坐标。"""
    a = np.asarray(img, np.float32)
    a = a @ np.array([0.299, 0.587, 0.114], np.float32)
    H, W = a.shape
    best = None
    for hf in np.linspace(0.34, y_max - y_min, steps):
        hh = hf * H
        ww = hh * ar
        if ww > (x_max - x_min) * W or ww > x_max * W:
            continue
        for y0f in np.linspace(y_min, y_max - hf, 8):
            for x0f in np.linspace(x_min, x_max - ww / W, 8):
                box = (x0f * W, y0f * H, x0f * W + ww, y0f * H + hh)
                e, s = calm_score(a, box)
                # 越大越优先，但干净度优先
                gain = (hh * ww) / float(W * H) * 30.0
                score = e * 6.0 + s * 0.35 - gain
                if best is None or score < best["score"]:
                    best = {"score": score, "edge": e, "std": s, "box": box,
                            "frac": (box[0] / W, box[1] / H, box[2] / W, box[3] / H)}
    return best


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("src")
    ap.add_argument("dst")
    ap.add_argument("--scan", action="store_true", help="跑一遍人像窗扫描并打印")
    args = ap.parse_args()
    img = bake(args.src, args.dst)
    print("baked -> %s  %s" % (args.dst, img.size))
    if args.scan:
        r = scan(img)
        f = r["frac"]
        print("窗口建议: x %.4f..%.4f  y %.4f..%.4f  (%dx%d, 比例 %.4f)" % (
            f[0], f[2], f[1], f[3], round((f[2]-f[0])*img.size[0]), round((f[3]-f[1])*img.size[1]),
            ((f[2]-f[0])*img.size[0]) / max(1.0, (f[3]-f[1])*img.size[1])))
        print("边缘=%.3f  亮度std=%.2f  score=%.3f" % (r["edge"], r["std"], r["score"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
