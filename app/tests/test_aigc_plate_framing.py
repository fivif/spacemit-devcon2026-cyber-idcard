#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""验证 AIGC 送图画布的人像摆位真的可调（原来写死在 aigc_client.py 里）。

判据：改 plate_scale_y / plate_top 后，画布上人像的上下边界跟着变，
且不改配置时与改造前的取值一致（0.74 / 0.84 / 0.10）。
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
APP = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP))

from PIL import Image  # noqa: E402

results: list[tuple[bool, str, str]] = []


def check(ok: bool, name: str, detail: str = "") -> None:
    results.append((bool(ok), name, detail))
    print(("  ✅ " if ok else "  ❌ ") + name + (("   " + detail) if detail else ""))


def build_plate(data_dir: Path):
    """在指定数据目录下生成一张送图画布，返回人像在画布里的上下边界（比例）。"""
    for name in ("aigc_client", "subject_rules"):
        sys.modules.pop(name, None)
    os.environ["BOOTH_DATA"] = str(data_dir)
    import aigc_client

    # 200x300 的"人像"：不透明的那个人 + 全透明背景
    photo = Image.new("RGB", (200, 300), (10, 200, 10))
    alpha = Image.new("L", (200, 300), 0)
    alpha.paste(255, (40, 60, 160, 280))

    canvas, guide = aigc_client._prepare_portrait_plate(photo, alpha, "1024x1536")
    bbox = guide.getbbox()
    if bbox is None:
        return None
    return {
        "canvas": canvas.size,
        "top": bbox[1] / canvas.size[1],
        "bottom": bbox[3] / canvas.size[1],
        "left": bbox[0] / canvas.size[0],
        "right": bbox[2] / canvas.size[0],
    }


def legacy_plate(data_dir: Path):
    """**照抄改造前 aigc_client.py 的写死逻辑**，作为基准。

    注意 plate_top 是"人像粘贴位置"，人像自身 alpha 在裁切框里还有留白，
    所以可见顶部（guide.getbbox()[1]）不等于 plate_top —— 别拿 0.10 直接比，
    要比的是"和改造前同一套算法算出来的结果一致"。
    """
    photo = Image.new("RGB", (200, 300), (10, 200, 10))
    alpha = Image.new("L", (200, 300), 0)
    alpha.paste(255, (40, 60, 160, 280))

    width, height = 1024, 1536
    rgb = photo.convert("RGB")
    alpha_img = alpha.convert("L")
    bbox = alpha_img.getbbox()
    left, top, right, bottom = bbox
    pad_x = max(8, int((right - left) * 0.10))
    pad_y = max(8, int((bottom - top) * 0.08))
    left = max(0, left - pad_x)
    top = max(0, top - pad_y)
    right = min(rgb.width, right + pad_x)
    bottom = min(rgb.height, bottom + pad_y)
    rgb = rgb.crop((left, top, right, bottom))
    alpha_img = alpha_img.crop((left, top, right, bottom))

    # ↓↓↓ 改造前写死的三个数 ↓↓↓
    scale = min((width * 0.74) / max(1, rgb.width),
                (height * 0.84) / max(1, rgb.height))
    target = (max(1, int(rgb.width * scale)), max(1, int(rgb.height * scale)))
    rgb = rgb.resize(target, Image.Resampling.LANCZOS)
    alpha_img = alpha_img.resize(target, Image.Resampling.LANCZOS)
    canvas = Image.new("RGB", (width, height), (127, 127, 127))
    guide = Image.new("L", (width, height), 0)
    position = ((width - target[0]) // 2, int(height * 0.10))
    canvas.paste(rgb, position, alpha_img)
    guide.paste(alpha_img, position)
    return guide, canvas


print("=== A) 不改配置：必须与改造前的写死逻辑**逐像素一致**")
with tempfile.TemporaryDirectory() as tmp:
    d = Path(tmp)
    base = build_plate(d)
    check(base is not None, "画布生成成功", str(base))
    # 用同样的输入跑一遍改造前的算法，两张 guide 图应当完全相同
    for name in ("aigc_client", "subject_rules"):
        sys.modules.pop(name, None)
    os.environ["BOOTH_DATA"] = str(d)
    import aigc_client

    photo = Image.new("RGB", (200, 300), (10, 200, 10))
    alpha = Image.new("L", (200, 300), 0)
    alpha.paste(255, (40, 60, 160, 280))
    _c, guide_now = aigc_client._prepare_portrait_plate(photo, alpha, "1024x1536")
    guide_old, canvas_old = legacy_plate(d)
    check(guide_now.tobytes() == guide_old.tobytes(),
          "guide 掩码与改造前逐像素一致（行为零变化）",
          f"bbox now={guide_now.getbbox()} old={guide_old.getbbox()}")
    if base:
        check(base["bottom"] <= 1.0, "人像没被画布裁掉",
              f"bottom={base['bottom']:.3f}")

print()
print("=== B) 改 plate_top：人像应整体下移")
with tempfile.TemporaryDirectory() as tmp:
    d = Path(tmp)
    (d / "settings.json").write_text(json.dumps({
        "subject": {"plate_top": 0.30}}), encoding="utf-8")
    moved = build_plate(d)
    check(moved is not None and base is not None and moved["top"] > base["top"] + 0.05,
          "plate_top 调大后人像下移（参数生效）",
          f"默认 top={base and round(base['top'], 3)} → 改后 {moved and round(moved['top'], 3)}")

print()
print("=== C) 改 plate_scale_y：人像应变小")
with tempfile.TemporaryDirectory() as tmp:
    d = Path(tmp)
    (d / "settings.json").write_text(json.dumps({
        "subject": {"plate_scale_y": 0.40}}), encoding="utf-8")
    smaller = build_plate(d)
    if base and smaller:
        base_h = base["bottom"] - base["top"]
        small_h = smaller["bottom"] - smaller["top"]
        check(small_h < base_h - 0.05, "plate_scale_y 调小后人像变矮（参数生效）",
              f"高度 {base_h:.3f} → {small_h:.3f}")
    else:
        check(False, "取值失败", f"{base} / {smaller}")

print()
print("=== D) 坏值退回默认，不能把送图搞崩")
with tempfile.TemporaryDirectory() as tmp:
    d = Path(tmp)
    (d / "settings.json").write_text(json.dumps({
        "subject": {"plate_scale_y": "huge", "plate_top": -5}}), encoding="utf-8")
    try:
        got = build_plate(d)
    except Exception as exc:  # noqa: BLE001
        check(False, "坏值不抛异常", f"{type(exc).__name__}: {exc}")
    else:
        check(got is not None and got["bottom"] <= 1.0,
              "坏值不抛异常且人像仍在画布内", str(got))

failed = [r for r in results if not r[0]]
print()
print(f"共 {len(results)} 项，失败 {len(failed)} 项")
if failed:
    for _, name, detail in failed:
        print(f"  ❌ {name}  {detail}")
    sys.exit(1)
print("✅ AIGC 画布摆位可调，且默认值与改造前一致")
