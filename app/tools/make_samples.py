#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""T-3 的 verify：无参数跑一遍，产出样张 + 拼版，并把像素级数字打出来。

    python3 app/tools/make_samples.py        # 成功 exit 0，任一验收项不过 → exit 1

产出（app/samples/）::

    00-contact-sheet.png            拼版（评审用）
    01-source-synthetic.png         合成人像（**不联网**，本地几何绘制）
    02-mask-preview.png             掩码预览（算力视图的那张）
    10-vertical-<style>.png         1000x1250 竖版成片（主输出）
    20-horizontal-<style>.png       1600x1000 横版成片（cards3 版面）
    30-plate-<style>.png            风格化人像板（stylize.render 的出口，单独可验）

验收（逐条打印 PASS/FAIL + 真实数字）:
    1. 四种风格竖版：平均亮度 15-35 / 像素值>200 占比 1%-6% / 细节能量 > 3%
    2. 四种风格横版：同上
    3. 几何守护：文字/人像包围盒不越画布、不进照片区（违规必须 = 0）
    4. 徽章诚实：badge_text(False) == "CPU"
    5. 姓名不留空：空姓名走 DEVELOPER 占位
"""

from __future__ import annotations

import io
import os
import sys
import time

import numpy as np
from PIL import Image, ImageDraw

_HERE = os.path.dirname(os.path.abspath(__file__))
_APP = os.path.normpath(os.path.join(_HERE, ".."))
_RENDER = os.path.join(_APP, "render")
if _RENDER not in sys.path:
    sys.path.insert(0, _RENDER)

import stylize      # noqa: E402
import card         # noqa: E402
import watermark    # noqa: E402

SAMPLES_DIR = os.path.join(_APP, "samples")

_CJK_OK = watermark.has_cjk_font()


def _t(zh: str, en: str) -> str:
    """目标机可能没装中文字体：缺 CJK 就退英文排版，不留豆腐块。"""
    return zh if _CJK_OK else en

DEMO_NAME = "张伟"
DEMO_HANDLE = "@zhangwei"
DEMO_ID = 471
DEMO_SEED = 7


# --------------------------------------------------------------------------
# 合成人像（离线几何绘制：影棚暗底 + 左上主光 + 轮廓光）
# --------------------------------------------------------------------------


def _soft(img: Image.Image, radius: float) -> Image.Image:
    return img.filter(__import__("PIL.ImageFilter", fromlist=["x"]).GaussianBlur(radius))


def synthetic_portrait(w: int = 1200, h: int = 1500, seed: int = 11):
    """返回 (RGB 人像, L 掩码)。掩码是"真人分割"该给的东西：整条人形剪影。

    刻意做得**不完美**：边缘轻微抖动 + 顶部一小块溢出 —— 用来证明强风格化
    能吸收分割边缘瑕疵（proto 只有 160x160，真机掩码比这脏得多）。
    """
    S = 2
    W, H = w * S, h * S
    rng = np.random.default_rng(seed)
    from PIL import ImageFilter

    def canvas():
        return Image.new("L", (W, H), 0)

    # ── 人体各部件的覆盖率（分开画，便于分块上色）
    torso = canvas()
    d = ImageDraw.Draw(torso)
    d.ellipse([W * 0.13, H * 0.56, W * 0.87, H * 1.55], fill=255)          # 胸/肩
    d.rectangle([0, H * 0.86, W, H], fill=255)

    neck = canvas()
    d = ImageDraw.Draw(neck)
    d.rounded_rectangle([W * 0.415, H * 0.40, W * 0.585, H * 0.62],
                        radius=W * 0.05, fill=255)

    head = canvas()
    d = ImageDraw.Draw(head)
    d.ellipse([W * 0.352, H * 0.145, W * 0.648, H * 0.485], fill=255)      # 脸
    d.ellipse([W * 0.336, H * 0.128, W * 0.470, H * 0.395], fill=255)      # 左耳
    d.ellipse([W * 0.530, H * 0.128, W * 0.664, H * 0.395], fill=255)      # 右耳

    hair = canvas()
    d = ImageDraw.Draw(hair)
    d.ellipse([W * 0.330, H * 0.095, W * 0.670, H * 0.400], fill=255)      # 头发外廓
    d.ellipse([W * 0.360, H * 0.150, W * 0.640, H * 0.360], fill=0)        # 内圈挖掉(露脸)

    def arr(img):
        # 2D 覆盖率图：脸部/五官判定要和 X/Y 的 (h, w) 形状对齐，
        # 用 (h, w, 1) 会在 `face & eye_l` 处炸掉广播。
        return np.asarray(img.resize((w, h), Image.LANCZOS), np.float32) / 255.0

    A_torso, A_neck, A_head, A_hair = arr(torso), arr(neck), arr(head), arr(hair)

    yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)
    X, Y = xx / w, yy / h

    # ── 背景：影棚暗底 + 头后一盏柔光（单一光源，左上偏中）
    bg = 22.0 + 30.0 * np.clip(1.0 - (((X - 0.40) / 0.34) ** 2 + ((Y - 0.30) / 0.36) ** 2), 0, 1) ** 1.5
    img = np.dstack([bg * 0.90, bg * 0.97, bg * 1.12])

    # ── 单一光源：左上强、右下压
    light = np.clip(1.26 - 0.86 * (0.62 * X + 0.38 * Y), 0.24, 1.30)
    rim = np.clip(1.0 - np.abs((X - 0.34) - 0.55 * (Y - 0.22)) * 5.2, 0.0, 1.0)   # 左肩轮廓光

    skin = np.dstack([np.full((h, w), 222.0), np.full((h, w), 182.0), np.full((h, w), 158.0)])
    skin = skin * light[..., None]
    cloth = np.dstack([np.full((h, w), 44.0), np.full((h, w), 50.0), np.full((h, w), 64.0)])
    cloth = cloth * (0.45 + 0.75 * light)[..., None] + rim[..., None] * np.array([70.0, 82.0, 96.0])
    hair_c = np.dstack([np.full((h, w), 30.0), np.full((h, w), 26.0), np.full((h, w), 25.0)])
    hair_c = hair_c * (0.5 + 0.9 * light)[..., None]

    for alpha, color in ((A_torso, cloth), (A_neck, skin), (A_head, skin), (A_hair, hair_c)):
        img = img * (1.0 - alpha[..., None]) + color * alpha[..., None]

    # ── 五官（只画在脸部覆盖率内）
    face = (A_head > 0.5)
    eye_l = (((X - 0.442) / 0.030) ** 2 + ((Y - 0.268) / 0.013) ** 2) < 1.0
    eye_r = (((X - 0.558) / 0.030) ** 2 + ((Y - 0.268) / 0.013) ** 2) < 1.0
    brow_l = (((X - 0.440) / 0.045) ** 2 + ((Y - 0.228) / 0.008) ** 2) < 1.0
    brow_r = (((X - 0.560) / 0.045) ** 2 + ((Y - 0.228) / 0.008) ** 2) < 1.0
    mouth = (((X - 0.500) / 0.055) ** 2 + ((Y - 0.372) / 0.014) ** 2) < 1.0
    nose_sh = (((X - 0.520) / 0.022) ** 2 + ((Y - 0.330) / 0.030) ** 2) < 1.0
    img[face & eye_l] = np.array([238.0, 236.0, 232.0])
    img[face & eye_r] = np.array([238.0, 236.0, 232.0])
    img[face & (brow_l | brow_r)] = np.array([58.0, 46.0, 42.0])
    img[face & mouth] = np.array([146.0, 92.0, 88.0])
    img[face & nose_sh] *= np.array([0.80, 0.78, 0.78])
    jaw = 1.0 - 0.10 * np.clip((Y - 0.30) * 3.0, 0.0, 1.0)               # 下颌影
    img *= np.where(face, jaw, 1.0)[..., None]

    # ── 材质：细噪点 + 皮肤微纹理（真照片没有一块是纯色的）
    img += rng.normal(0.0, 3.4, (h, w, 3))
    img += rng.normal(0.0, 1.6, (h, w, 3))   # 通道无关的细颗粒
    photo = Image.fromarray(np.clip(img, 0, 255).astype(np.uint8), "RGB").filter(ImageFilter.GaussianBlur(0.6))

    # ── 掩码：整条人形剪影 + 边缘抖动 + 顶部一小块溢出（刻意不完美）
    m = np.clip(A_torso + A_neck + A_head + A_hair, 0, 1)
    wob = _soft(Image.fromarray((m * 255).astype(np.uint8), "L"), 2.2)
    mimg = Image.fromarray(np.clip(np.asarray(wob, np.float32) + 12.0, 0, 255).astype(np.uint8), "L")
    blob = Image.new("L", (w, h), 0)
    ImageDraw.Draw(blob).ellipse([w * 0.30, h * 0.055, w * 0.40, h * 0.105], fill=210)
    mimg = Image.fromarray(np.maximum(np.asarray(mimg, np.float32),
                                      np.asarray(blob, np.float32)).astype(np.uint8), "L")
    mimg = mimg.point(lambda v: 255 if v > 140 else 0)
    return photo, mimg.filter(ImageFilter.GaussianBlur(0.8))


# --------------------------------------------------------------------------
# 验收
# --------------------------------------------------------------------------


def _check(image, tag, rows, fails):
    mt = stylize.dark_metrics(image)
    # 判据在 stylize 里（原色不进暗调窗口，见 natural_metrics_ok）——
    # 不在这里另写一份，否则两边会分弆（坑 23）。
    ok_metrics = stylize.natural_metrics_ok(mt)
    ok_detail = mt["detail_pct"] > stylize.DETAIL_MIN
    rows.append({"tag": tag, **mt, "ok_metrics": ok_metrics, "ok_detail": ok_detail})
    if not (ok_metrics and ok_detail):
        fails.append("%s 指标越界: %s" % (tag, mt))
    return mt


def _label(draw, box, text, font, fg=(196, 208, 214)):
    """text 可以是 str 或 (行1, 行2, ...)：长标签换行贴，不压隔壁格。"""
    lines = (text,) if isinstance(text, str) else tuple(text)
    step = int(watermark.fn_size(font) + 3)
    for i, ln in enumerate(lines):
        draw.text((box[0], box[1] + i * step), ln, font=font, fill=fg)


def contact_sheet(items, path, title, subtitle, cols=4):
    """items: [(PIL 图, 标签), ...]；前两张各占半行（源图 / 掩码），其余按 cols 列铺开。

    行高逐行取该行最高，竖版（4:5）和横版（16:10）混排时横版那行不会白留一半。
    """
    gap, pad, label_h, head_h = 16, 22, 34, 96
    small, big = list(items[:2]), list(items[2:])
    width = cols * 340 + (cols + 1) * gap
    big_w = (width - (cols + 1) * gap) // cols
    rows = [big[i:i + cols] for i in range(0, len(big), cols)]
    row_hs = [max(int(big_w * im.size[1] / im.size[0]) for im, _ in r) for r in rows]
    small_w = (width - 3 * gap) // 2
    small_h = max([int(small_w * im.size[1] / im.size[0]) for im, _ in small] or [0])
    height = head_h + (small_h + label_h + gap if small else 0) \
        + sum(h + label_h + gap for h in row_hs) + pad
    sheet = Image.new("RGB", (width, height), (13, 14, 16))
    d = ImageDraw.Draw(sheet)
    f_title = watermark.load_font(26)
    f_sub = watermark.load_font(13)          # 等宽字体没有汉字，副标题必须用含 CJK 的字体
    f_label = watermark.load_font(13)
    _label(d, (pad, 24), title, f_title, watermark.INK)
    _label(d, (pad, 60), subtitle, f_sub, watermark.BRAND)
    d.line([(pad, head_h - 12), (width - pad, head_h - 12)], fill=(60, 68, 72), width=1)

    y = head_h
    for i, (im, lb) in enumerate(small):
        cw = small_w
        ch = int(cw * im.size[1] / im.size[0])
        x = gap + i * (cw + gap)
        sheet.paste(im.convert("RGB").resize((cw, ch), Image.LANCZOS), (x, y))
        d.rectangle([x, y, x + cw - 1, y + ch - 1], outline=(48, 54, 58), width=1)
        _label(d, (x, y + ch + 6), lb, f_label)
    if small:
        y += small_h + label_h + gap

    for r, row in enumerate(rows):
        for c, (im, lb) in enumerate(row):
            cw = big_w
            ch = int(cw * im.size[1] / im.size[0])
            px = gap + c * (cw + gap)
            sheet.paste(im.convert("RGB").resize((cw, ch), Image.LANCZOS), (px, y))
            d.rectangle([px, y, px + cw - 1, y + ch - 1], outline=(48, 54, 58), width=1)
            _label(d, (px, y + ch + 6), lb, f_label)
        y += row_hs[r] + label_h + gap
    sheet.save(path, optimize=True)
    return sheet


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------


def main(argv=None) -> int:
    t0 = time.time()
    out_dir = SAMPLES_DIR
    args = list(sys.argv[1:] if argv is None else argv)
    if "--out" in args:
        out_dir = args[args.index("--out") + 1]
    os.makedirs(out_dir, exist_ok=True)
    fails = []
    rows = []

    print("== T-3 渲染管线样张 ==")
    print("输出目录: %s" % out_dir)
    print("风格: %s" % ", ".join(stylize.STYLES))

    photo, mask = synthetic_portrait(seed=DEMO_SEED)
    photo_path = os.path.join(out_dir, "01-source-synthetic.png")
    photo.save(photo_path)
    print("合成人像: %s  %s  掩码覆盖率 %.1f%%"
          % (photo_path, photo.size, float(np.asarray(mask).mean() / 255.0 * 100)))

    prev = stylize.mask_preview_png(mask)
    prev.save(os.path.join(out_dir, "02-mask-preview.png"))

    items = [(photo, _t("01 合成人像（离线几何绘制）", "01 source portrait (synthetic, offline)")),
             (prev, _t("02 掩码预览 mask_preview_png", "02 mask preview  mask_preview_png"))]
    verts, horizs = [], []   # 分行收口：竖版一行、横版一行，拼版不会参差
    for style in stylize.STYLES:
        t_style = time.time()
        plate = stylize.render(style, photo, mask, size=card.VERTICAL, seed=DEMO_SEED)
        plate.save(os.path.join(out_dir, "30-plate-%s.png" % style))
        _check(plate, "plate/%s" % style, rows, fails)

        vert = card.compose(DEMO_ID, plate, style=style, name=DEMO_NAME, handle=DEMO_HANDLE,
                            ep=True, ep_threads=4, infer_ms=205, person_count=1,
                            size=card.VERTICAL, seed=DEMO_SEED)
        v_guard = card.guard_report()
        if v_guard:
            fails.append("vertical/%s 几何越界: %s" % (style, v_guard))
        if vert.size != card.VERTICAL:
            fails.append("vertical/%s 尺寸错误: %s" % (style, vert.size))
        mt_v = _check(vert, "vertical/%s" % style, rows, fails)
        vert.save(os.path.join(out_dir, "10-vertical-%s.png" % style), optimize=True)

        horiz = card.compose(DEMO_ID, plate, style=style, name=DEMO_NAME, handle=DEMO_HANDLE,
                             ep=False, infer_ms=1180, person_count=2,
                             size=card.HORIZONTAL, seed=DEMO_SEED)
        h_guard = card.guard_report()
        if h_guard:
            fails.append("horizontal/%s 几何越界: %s" % (style, h_guard))
        if horiz.size != card.HORIZONTAL:
            fails.append("horizontal/%s 尺寸错误: %s" % (style, horiz.size))
        mt_h = _check(horiz, "horizontal/%s" % style, rows, fails)
        horiz.save(os.path.join(out_dir, "20-horizontal-%s.png" % style), optimize=True)

        verts.append((vert, (_t("10 竖版 %s", "10 vertical %s") % style,
                              "1000x1250  mean=%.1f  hi=%.2f%%  detail=%.1f%%"
                              % (mt_v["mean_luma"], mt_v["hi_luma_pct"], mt_v["detail_pct"]))))
        horizs.append((horiz, (_t("20 横版 %s", "20 horizontal %s") % style,
                               "1600x1000  mean=%.1f  hi=%.2f%%  detail=%.1f%%"
                               % (mt_h["mean_luma"], mt_h["hi_luma_pct"], mt_h["detail_pct"]))))
        print("  %-13s 竖版 mean=%5.2f hi=%4.2f%% detail=%5.2f%% guard=%d | 横版 mean=%5.2f hi=%4.2f%% guard=%d | %.1fs"
              % (style, mt_v["mean_luma"], mt_v["hi_luma_pct"], mt_v["detail_pct"], len(v_guard),
                 mt_h["mean_luma"], mt_h["hi_luma_pct"], len(h_guard), time.time() - t_style))

    # 徽章诚实性 + 姓名占位
    if watermark.badge_text(False) != "CPU":
        fails.append("badge_text(False) 不是 CPU: %r" % watermark.badge_text(False))
    blank = card.compose(1, photo, style="natural", name="", size=card.VERTICAL, seed=DEMO_SEED)
    named = card.compose(1, photo, style="natural", name=card.NAME_FALLBACK,
                         size=card.VERTICAL, seed=DEMO_SEED)
    a = np.asarray(blank.convert("L"), np.int16)
    b = np.asarray(named.convert("L"), np.int16)
    if int(np.abs(a - b).max()) != 0:
        fails.append("空姓名没有退化成 %s 占位（与显式占位版不一致，最大差 %d）"
                     % (card.NAME_FALLBACK, int(np.abs(a - b).max())))
    if watermark.format_id(DEMO_ID) != "SM-2026-0471":
        fails.append("编号格式不对: %r" % watermark.format_id(DEMO_ID))

    sheet = contact_sheet(items + verts + horizs, os.path.join(out_dir, "00-contact-sheet.png"),
                          _t("SpacemiT DevCon 2026 · 装置5「赛博开发者证件照」· T-3 样张",
                             "SpacemiT DevCon 2026 - Booth 5 Cyber Developer ID - T-3 samples"),
                          _t("暗底卡片 + 原色人像 + 官方 logo + #SpacemiTDevCon + 编号 + RISC-V 指令角",
                             "dark card + natural portrait + official logo + #SpacemiTDevCon + id + RISC-V corner"))
    print("拼版: %s %s" % (os.path.join(out_dir, "00-contact-sheet.png"), sheet.size))

    summary = {
        "styles": list(stylize.STYLES),
        "rows": rows,
        "failures": fails,
        "elapsed_s": round(time.time() - t0, 1),
        "samples_dir": out_dir,
    }
    with io.open(os.path.join(out_dir, "99-report.json"), "w", encoding="utf-8", newline="\n") as fh:
        import json
        fh.write(json.dumps(summary, ensure_ascii=False, indent=1, default=str))

    # 判据印在纸上，就必须和代码里那份是同一份 —— 直接从 stylize 引（坑 23）。
    criteria = "mean %g~%g / hi<=%g%% / detail>%g%%" % (
        stylize.NATURAL_MEAN_RANGE[0], stylize.NATURAL_MEAN_RANGE[1],
        stylize.NATURAL_HI_MAX, stylize.DETAIL_MIN)

    def _line(r):
        verdict = "OK" if (r["ok_metrics"] and r["ok_detail"]) else "FAIL"
        return ("  %-22s mean=%6.2f  hi=%5.2f%%  hi_max=%5.2f%%  detail=%5.2f%%  %s"
                % (r["tag"], r["mean_luma"], r["hi_luma_pct"], r["hi_max_pct"],
                   r["detail_pct"], verdict))

    print()
    for prefix, title in (("vertical/", "竖版成片"), ("horizontal/", "横版成片"),
                          ("plate/", "原色人像板")):
        hits = [r for r in rows if r["tag"].startswith(prefix)]
        if not hits:
            continue
        print("-- %s（%s）--" % (title, criteria))
        for r in hits:
            print(_line(r))
    print()
    if fails:
        print("结果: FAIL（%d 项）" % len(fails))
        for f in fails:
            print("  -", f)
        return 1
    print("结果: PASS  共 %d 张成片 + 拼版，用时 %.1fs" % (len(rows), time.time() - t0))
    return 0


if __name__ == "__main__":
    sys.exit(main())
