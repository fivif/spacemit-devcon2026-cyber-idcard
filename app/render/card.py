# -*- coding: utf-8 -*-
"""成片总装 —— SpacemiT DevCon 2026 · 装置5「赛博开发者证件照」· T-3

两档输出（系统规格 v2（内部文档） §8 / §11.3）::

    1000x1250  竖版 4:5   主输出（社交友好），版式为 T-3 新造
    1600x1000  横版 8:5   沿用 cards3 版面：人像贴进照片区
                          x∈[0.040,0.455]、y∈[0.455,0.975]

两档都必含：官方 logo 锁定组 /「SpacemiT DevCon 2026」/ #SpacemiTDevCon /
编号 SM-YYYY-NNNN / 姓名牌 / RISC-V 指令角。

两条硬纪律
----------
1. **姓名不留空**：拿不到姓名用 ``DEVELOPER`` 占位 —— 空一块比占位更像故障
   （视觉规格 §7）。
2. **几何守护**：所有文字与块的真实包围盒（PIL textbbox）都要落在画布内、
   且不进照片区。越界不抛异常，但会记进 ``guard_report()``，由
   ``tools/make_samples.py`` 判定非零退出 —— 越界 = 现场出废片。

暗调：底图用 ``app/assets/cards/*.png``（cards3 暗调六套）+ 三级模糊 glow。
风格 → 卡片是**恒定映射**（``STYLE_TO_CARD``），保证同一风格现场每一张都一个样。
"""

from __future__ import annotations

import importlib.util
import os
import re
from types import SimpleNamespace

import numpy as np
from PIL import Image, ImageDraw, ImageFilter

try:                       # 作为包导入（app.render.card）
    from . import stylize, watermark
except ImportError:        # 作为平铺模块导入（sys.path 指向 app/render）
    import stylize          # type: ignore
    import watermark        # type: ignore

# --------------------------------------------------------------------------
# 冻结接口（系统规格 v2（内部文档） §11.3）
# --------------------------------------------------------------------------

CARD_NAMES = ("blueprint", "schematic", "terminal", "datapath", "logo", "timeline")

VERTICAL = (1000, 1250)
HORIZONTAL = (1600, 1000)
SIZES = (VERTICAL, HORIZONTAL)          # size 只接受两档

# 风格 → 卡片（恒定映射，保证现场一致性；调用方可用 card= 覆盖）。
# 早期按风格配过四张卡（terminal / datapath / blueprint / schematic），
# 风格收窄到「原色」之后这套映射没用了。留空 = 一律用默认卡，
# card= 仍可显式指定（CARD_NAMES 那六张卡都还在）。
STYLE_TO_CARD = {}
# 注：logo 卡（石墨银）自带 5.6% 高光（整块白色 SPACEM 字标），单张就吃满
# 6% 的高光预算，叠上照片必然越界 —— 因此不进默认映射，但 card="logo" 仍可用。

CARD_LABELS = {                     # 与 cards3 版面一致的中英双标
    "blueprint": ("主机版图", "BLUEPRINT"),
    "schematic": ("电路原理", "SCHEMATIC"),
    "terminal": ("终端会话", "TERMINAL"),
    "datapath": ("数据通路", "DATAPATH"),
    "logo": ("品牌标板", "LOGO"),
    "timeline": ("世代谱", "TIMELINE"),
}
CARD_LABELS_EN = {
    "blueprint": ("BLUEPRINT", "BLUEPRINT"), "schematic": ("SCHEMATIC", "SCHEMATIC"),
    "terminal": ("TERMINAL", "TERMINAL"), "datapath": ("DATAPATH", "DATAPATH"),
    "logo": ("BRAND BOARD", "LOGO"), "timeline": ("TIMELINE", "TIMELINE"),
}

# 视觉规格 §4 的六套暗调板（纸色 / 发光主色 / 副发光）
CARD_PALETTE = {
    "blueprint": {"paper": (9, 14, 26), "glow": (58, 214, 255), "glow2": (26, 92, 150)},
    "schematic": {"paper": (14, 12, 10), "glow": (255, 176, 84), "glow2": (140, 82, 30)},
    "terminal": {"paper": (8, 14, 12), "glow": (72, 246, 156), "glow2": (32, 124, 82)},
    "datapath": {"paper": (8, 14, 20), "glow": (86, 236, 232), "glow2": (32, 112, 132)},
    "logo": {"paper": (12, 13, 16), "glow": (150, 210, 255), "glow2": (62, 92, 132)},
    "timeline": {"paper": (14, 11, 8), "glow": (255, 168, 56), "glow2": (150, 92, 22)},
}

CARD_FILES = {
    "blueprint": "01-blueprint.png", "schematic": "02-schematic.png",
    "terminal": "03-terminal.png", "datapath": "04-datapath.png",
    "logo": "05-logo.png", "timeline": "06-timeline.png",
}

_HERE = os.path.dirname(os.path.abspath(__file__))
_APP = os.path.normpath(os.path.join(_HERE, ".."))
CARDS_DIR = os.path.join(_APP, "assets", "cards")
BRAND_DIR = os.path.join(_APP, "assets", "brand")

# 照片区（比例坐标）与守护外扩
PHOTO_BOX_H = (0.040, 0.455, 0.455, 0.975)          # cards3 版面（横版）
KEEPOUT_H = (0.028, 0.443, 0.467, 0.987)            # 视觉规格 §3
PHOTO_BOX_V = (0.062, 0.106, 0.612, 0.656)          # 竖版照片窗（4:5，主输出）

NAME_FALLBACK = "DEVELOPER"

# --------------------------------------------------------------------------
# 几何守护
# --------------------------------------------------------------------------

_VIOLATIONS: list = []
_METRICS: list = []


def guard_report() -> list:
    """最近一次 compose 的越界记录（空列表 = 干净）。"""
    return list(_VIOLATIONS)


def metrics_report() -> list:
    """最近一次 compose 的暗调指标（验收用）。"""
    return list(_METRICS)


def _intersects(a, b) -> bool:
    ax0, ay0, ax1, ay1 = a
    bx0, by0, bx1, by1 = b
    return not (ax1 <= bx0 or bx1 <= ax0 or ay1 <= by0 or by1 <= ay0)


def _check_guard(w, h, keepout):
    _VIOLATIONS.clear()
    for box, label in watermark.captured_boxes():
        if box[2] - box[0] <= 0 or box[3] - box[1] <= 0:
            continue
        if box[0] < -0.5 or box[1] < -0.5 or box[2] > w + 0.5 or box[3] > h + 0.5:
            _VIOLATIONS.append({"label": label, "why": "canvas",
                                "box": tuple(round(v, 1) for v in box)})
        elif keepout and label != "portrait" and _intersects(box, keepout):
            _VIOLATIONS.append({"label": label, "why": "photo",
                                "box": tuple(round(v, 1) for v in box)})
    return _VIOLATIONS


# --------------------------------------------------------------------------
# RISC-V 真指令库：单一真源是早期原型的 riscv.py（有自检），缺失时退内置真指令子集
# --------------------------------------------------------------------------

_FALLBACK_INSTRUCTIONS = (
    ("add", "a0, a1, a2", "rd = rs1 + rs2"),
    ("addi", "sp, sp, -16", "rd = rs1 + imm"),
    ("sub", "t0, t1, t2", "rd = rs1 - rs2"),
    ("and", "a3, a4, a5", "rd = rs1 & rs2"),
    ("or", "s0, s1, s2", "rd = rs1 | rs2"),
    ("xor", "t3, t4, t5", "rd = rs1 ^ rs2"),
    ("slli", "a0, a0, 3", "logical shift left"),
    ("lw", "a0, 8(sp)", "load word"),
    ("sd", "s0, 8(a1)", "store doubleword"),
    ("beq", "a0, a1, .L2", "branch if equal"),
    ("jal", "ra, 1f", "jump and link"),
    ("mul", "a0, a1, a2", "rd = rs1 * rs2"),
    ("fadd.s", "fa0, fa1, fa2", "single-precision add"),
    ("vsetvli", "t0, a0, e8, m2", "configure vector length"),
    ("vle8.v", "v0, (a1)", "vector load, 8-bit"),
    ("vadd.vv", "v1, v2, v3", "vector add"),
    ("vmul.vx", "v1, v2, a0", "vector multiply by scalar"),
    ("vse8.v", "v0, (a1)", "vector store, 8-bit"),
)
_FALLBACK_ASM = (
    ".globl  kern_forward", "kern_forward:", "    addi    sp, sp, -32",
    "    sd      ra, 24(sp)", "    vsetvli t0, a2, e8, m4", "    vle8.v  v4, (a3)",
    "    vadd.vv v8, v4, v4", "    vse8.v  v8, (a4)", "    ld      ra, 24(sp)", "    ret",
)
_FALLBACK_ISA = ("RVA23", "RVV", "RV64", "CSR", "Zicsr", "Zba", "Zbb", "Zvfh",
                 "VLEN", "LMUL", "SEW", "HART")
_FALLBACK_BADGES = ("RV64I", "RVA23U64", "Zicsr", "Zve64d", "VLEN 256")

_K3_ISA_LINE = "rv64imafdcvh_zba_zbb_zbc_zbs_zve64d_zvfh"


def _fallback_riscv():
    def instruction_line(index, with_comment=True):
        m, o, n = _FALLBACK_INSTRUCTIONS[index % len(_FALLBACK_INSTRUCTIONS)]
        t = ("%-10s %s" % (m, o)).rstrip()
        return t + ("   # " + n if with_comment and n else "")

    def instruction_short(index):
        m, o, _ = _FALLBACK_INSTRUCTIONS[index % len(_FALLBACK_INSTRUCTIONS)]
        return ("%s %s" % (m, o)).strip()

    def asm_snippet(rows, seed=0, start=None):
        if rows <= 0:
            return []
        return [_FALLBACK_ASM[i % len(_FALLBACK_ASM)] for i in range(rows)]

    def isa_badges(seed=0):
        return list(_FALLBACK_BADGES) + [("RVV 1.0 · VLEN 256" if seed % 2 else "RV64GC + V")]

    return SimpleNamespace(INSTRUCTIONS=_FALLBACK_INSTRUCTIONS, ISA_TERMS=_FALLBACK_ISA,
                           K3_ISA_LINE=_K3_ISA_LINE, instruction_line=instruction_line,
                           instruction_short=instruction_short, asm_snippet=asm_snippet,
                           isa_badges=isa_badges, source="builtin")


_riscv_cache = None


def riscv_lib():
    """真指令库。优先早期原型的 riscv.py（唯一真源），缺失时退内置的真指令子集。"""
    global _riscv_cache
    if _riscv_cache is not None:
        return _riscv_cache
    root = os.path.dirname(_APP)
    for path in (os.path.join(_HERE, "riscv.py"),
                 os.path.join(root, "booth", "riscv.py"),
                 os.path.join(_APP, "booth", "riscv.py")):
        if os.path.isfile(path):
            try:
                spec = importlib.util.spec_from_file_location("_devcon_riscv", path)
                mod = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(mod)          # type: ignore[union-attr]
                if hasattr(mod, "instruction_line") and hasattr(mod, "asm_snippet"):
                    mod.source = path
                    _riscv_cache = mod
                    return _riscv_cache
            except Exception:
                continue
    _riscv_cache = _fallback_riscv()
    return _riscv_cache


# --------------------------------------------------------------------------
# 绘制小工具
# --------------------------------------------------------------------------


class Glow:
    """发光图层：细线/标签同时画进普通层与 glow 层，最后三级模糊 screen 叠回。"""

    def __init__(self, size):
        self.layer = Image.new("L", size, 0)
        self.d = ImageDraw.Draw(self.layer)

    def line(self, xy, value=220, width=1):
        self.d.line(xy, fill=int(value), width=width)

    def rect(self, box, value=200, width=1):
        self.d.rectangle(box, outline=int(value), width=width)

    def text(self, xy, s, font, value=200, anchor="la"):
        if s:
            self.d.text(xy, s, font=font, fill=int(value), anchor=anchor)

    def poly(self, pts, value=200):
        self.d.polygon(pts, fill=int(value))

    def drive(self, img, radius, gain, tint):
        return watermark.glow_pass(img, self.layer, radius, gain, tint)


def _text(draw, glow, xy, s, font, color, *, anchor="la", glow_value=0, label=None):
    if not s:
        return (0.0, 0.0, 0.0, 0.0)
    box = watermark.draw_text(draw, xy, s, font, color, anchor=anchor, label=label)
    if glow is not None and glow_value > 0:
        glow.text(xy, s, font, value=glow_value, anchor=anchor)
    return box


def _sample_glow(image, fallback):
    """从卡片底图取发光主色（最亮且最饱和的像素），保证竖版/横版同一套色。"""
    try:
        small = np.asarray(image.convert("RGB").resize((96, 60), Image.BILINEAR), np.float32)
        mx = small.max(2)
        sat = mx - small.min(2)
        idx = np.unravel_index(int(np.argmax(sat * 1.6 + mx)), sat.shape)
        if float(sat[idx]) > 40 and float(mx[idx]) > 90:
            return tuple(int(v) for v in small[idx[0], idx[1]])
    except Exception:
        pass
    return tuple(fallback)


def _load_card_bg(card, size):
    """读 cards3 暗调底图；缺失时程序化兜底（不抛异常）。"""
    path = os.path.join(CARDS_DIR, CARD_FILES.get(card, ""))
    if os.path.isfile(path):
        try:
            img = Image.open(path).convert("RGB")
            if img.size != size:
                img = img.resize(size, Image.LANCZOS)
            return img
        except Exception:
            pass
    pal = CARD_PALETTE.get(card, CARD_PALETTE["blueprint"])
    arr = stylize._dark_base(size[0], size[1],
                             {"paper": pal["paper"],
                              "paper2": tuple(min(255, int(c * 1.7)) for c in pal["paper"])},
                             seed=7, mask=None)
    return stylize._to_image(arr)


def _patch_bg(img, box, *, pad=10, feather=7, radius=6):
    """用周围环的中位色盖掉一块底图内容（替换卡片自带的临时 logo / 占位姓名牌）。"""
    w, h = img.size
    arr = np.asarray(img.convert("RGB"), np.float32)
    x0 = int(max(0, box[0] - pad))
    y0 = int(max(0, box[1] - pad))
    x1 = int(min(w, box[2] + pad))
    y1 = int(min(h, box[3] + pad))
    if x1 <= x0 or y1 <= y0:
        return img
    rx0, ry0 = max(0, x0 - 16), max(0, y0 - 16)
    rx1, ry1 = min(w, x1 + 16), min(h, y1 + 16)
    parts = []
    if y0 > ry0:
        parts.append(arr[ry0:y0, rx0:rx1].reshape(-1, 3))
    if ry1 > y1:
        parts.append(arr[y1:ry1, rx0:rx1].reshape(-1, 3))
    if x0 > rx0:
        parts.append(arr[y0:y1, rx0:x0].reshape(-1, 3))
    if rx1 > x1:
        parts.append(arr[y0:y1, x1:rx1].reshape(-1, 3))
    ring = np.concatenate(parts) if parts else np.zeros((0, 3), np.float32)
    color = np.median(ring, axis=0) if len(ring) else np.array((9, 11, 14), np.float32)
    layer = Image.new("L", (w, h), 0)
    ImageDraw.Draw(layer).rounded_rectangle([x0, y0, x1, y1], radius=radius, fill=255)
    layer = layer.filter(ImageFilter.GaussianBlur(feather))
    m = np.asarray(layer, np.float32)[..., None] / 255.0
    out = arr * (1.0 - m) + color[None, None, :] * m
    return Image.fromarray(np.clip(out, 0, 255).astype(np.uint8), "RGB")


def _cover_crop(img, box_w, box_h, anchor_y=0.06):
    """cover 裁切：放大到刚好盖住目标框，横向居中、纵向按 anchor 偏置。"""
    pw, ph = img.size
    scale = max(box_w / float(pw), box_h / float(ph))
    nw = max(box_w, int(round(pw * scale)))
    nh = max(box_h, int(round(ph * scale)))
    resized = img.convert("RGB").resize((nw, nh), Image.LANCZOS)
    x0 = (nw - box_w) // 2
    y0 = int(round(anchor_y * (nh - box_h)))
    return resized.crop((x0, y0, x0 + box_w, y0 + box_h))


def _brackets(draw, glow, box, color, *, arm, width=2, alpha=235, value=205):
    x0, y0, x1, y1 = box
    for (cx, cy, sx, sy) in ((x0, y0, 1, 1), (x1, y0, -1, 1), (x0, y1, 1, -1), (x1, y1, -1, -1)):
        draw.line([(cx, cy), (cx + sx * arm, cy)], fill=color + (alpha,), width=width)
        draw.line([(cx, cy), (cx, cy + sy * arm)], fill=color + (alpha,), width=width)
        glow.line([(cx, cy), (cx + sx * arm, cy)], value=value, width=width + 1)
        glow.line([(cx, cy), (cx, cy + sy * arm)], value=value, width=width + 1)


def _pill(draw, glow, xy, s, font, color, *, pad_x=10, pad_y=5, alpha=210):
    """小圆角标签（ISA 徽标）。返回包围盒。"""
    tw = watermark.text_w(draw, s, font)
    h = watermark.fn_size(font) + pad_y * 2
    w = tw + pad_x * 2
    x, y = xy
    r = int(h * 0.32)
    draw.rounded_rectangle([x, y, x + w, y + h], radius=r, fill=(10, 12, 14, 190),
                           outline=color + (alpha,), width=1)
    draw.text((x + w / 2.0, y + h / 2.0), s, font=font, fill=color + (248,), anchor="mm")
    glow.d.rounded_rectangle([x, y, x + w, y + h], radius=r, outline=150, width=2)
    _rec((x, y, x + w, y + h), "pill")
    return (x, y, x + w, y + h)


def _grade_window(img, box, *, mean_target=44.0, hi_lo=4.5, hi_hi=10.0, k_range=(0.90, 1.70)):
    """照片窗局部定标：光照片该是最亮的一块 —— 人像窗有真高光，底图才压得住。

    整幅不定标（底图和卡片各自已在窗口内），只把"照片"这一块提成正常曝光：
    平均 44、高光 4.5%-10%。这样成片的高光占比由照片本身提供，
    而不是靠文字凑出来的。
    """
    x0, y0, x1, y1 = (int(round(v)) for v in box)
    x0, y0 = max(0, x0), max(0, y0)
    x1, y1 = min(img.size[0], x1), min(img.size[1], y1)
    if x1 - x0 < 8 or y1 - y0 < 8:
        return img
    arr = np.asarray(img.convert("RGB"), np.float32)
    arr[y0:y1, x0:x1] = stylize.calibrate_dark(arr[y0:y1, x0:x1], mean_target=mean_target,
                                               hi_lo=hi_lo, hi_hi=hi_hi, k_range=k_range)
    return stylize._to_image(arr)


def _cap_dark(img, *, mean_max=33.0, hi_max=5.6, floor=0.85, trigger=1.03):
    """成片封顶：整幅不定标，只保证不越过"平均亮度 <=35 / 高光 <=6%"的天花板。

    注意：高光像素的值大多刚过 200，粗暴缩放会把它们整片打到 200 以下
    （实测 0.8 倍缩放能让 8% 掉到 0.9%）。所以这里只做轻压（>=0.85），
    并且宁可在报告里如实标数字，也不为了指标把画面压成灰饼。
    """
    arr = np.asarray(img.convert("RGB"), np.float32)
    for _ in range(4):
        mt = stylize.dark_metrics(stylize._to_image(arr))
        need = max(mt["mean_luma"] / max(mean_max, 1e-3), mt["hi_max_pct"] / max(hi_max, 1e-3))
        if need <= trigger:
            break
        arr = np.clip(arr * max(floor, 1.0 / need), 0, 255)
    return stylize._to_image(arr)


def _rid_num(record_id) -> int:
    """编号可能是 int，也可能是 "SM-2026-0011" 这类带前缀的字符串 —— 一律取出尾部数字。"""
    if isinstance(record_id, int) and not isinstance(record_id, bool):
        return record_id
    m = re.search(r"(\d+)\s*$", str(record_id or ""))
    return int(m.group(1)) if m else 0


def _pick_instruction(rl, index: int, with_comment: bool = True) -> str:
    """挑一条**有操作数**的真指令：ecall/ebreak/mret 这类没有操作数，看着不像代码。"""
    table = getattr(rl, "INSTRUCTIONS", None) or _FALLBACK_INSTRUCTIONS
    usable = [i for i, row in enumerate(table) if len(row) > 1 and str(row[1]).strip()]
    if not usable:
        usable = list(range(len(table)))
    i = usable[int(index) % len(usable)]
    try:
        return rl.instruction_line(i, with_comment) if with_comment else rl.instruction_short(i)
    except Exception:
        return _fallback_riscv().instruction_line(i, with_comment)


def _rec(box, label):
    watermark._record(box, label)


def _ticker(size, lines, font, color, spacing=4, alpha=52):
    """竖排贴边字条（背景文字用真指令）。返回 RGBA。"""
    probe = ImageDraw.Draw(Image.new("RGB", (4, 4)))
    tw = int(max([watermark.text_w(probe, s, font) for s in lines] + [10]))
    th = int((watermark.fn_size(font) + spacing) * len(lines) + spacing)
    strip = Image.new("RGBA", (tw + 8, th + 8), (0, 0, 0, 0))
    d = ImageDraw.Draw(strip)
    y = 4
    for s in lines:
        d.text((4, y), s, font=font, fill=color + (alpha,))
        y += watermark.fn_size(font) + spacing
    return strip.rotate(90, expand=True, resample=Image.BICUBIC)

# --------------------------------------------------------------------------
# 竖版 1000x1250（主输出）
# --------------------------------------------------------------------------


def _vertical_geometry(w, h):
    pad = int(round(0.062 * w))
    right = w - pad
    win = (int(round(PHOTO_BOX_V[0] * w)), int(round(PHOTO_BOX_V[1] * h)),
           int(round(PHOTO_BOX_V[2] * w)), int(round(PHOTO_BOX_V[3] * h)))
    band_h = int(round(0.188 * h))
    top = h - band_h
    col_x = win[2] + int(round(0.034 * w))
    return {"pad": pad, "right": right, "win": win, "band_top": top,
            "col": (col_x, right), "header_y": int(round(0.030 * h)),
            "rule_y": win[1] - int(round(0.014 * h)),
            "instr": (pad, win[3] + int(round(0.020 * h)), right, top - int(round(0.030 * h)))}


def _vertical_bg(card, size, seed):
    """竖版底：程序化暗底（与卡片同一套色）+ 真指令竖排字条 + 网格材质。"""
    w, h = size
    pal = CARD_PALETTE.get(card, CARD_PALETTE["blueprint"])
    base = stylize._dark_base(w, h,
                              {"paper": pal["paper"],
                               "paper2": tuple(min(255, int(c * 1.8)) for c in pal["paper"])},
                              seed=int(seed) + 11, mask=None)
    img = stylize._to_image(base).convert("RGB")
    rl = riscv_lib()
    tick_font = watermark.load_font(max(9, int(round(w * 0.0092))), mono=True)
    start = int(seed) % 7
    lines = [rl.instruction_short(start + i) for i in range(26)]
    strip = _ticker(size, lines, tick_font, (134, 156, 160), spacing=3, alpha=46)
    img.paste(strip, (max(2, int(0.012 * w)), int(0.105 * h)), strip)
    return img


def _render_vertical(record_id, portrait, style, name, handle, card, ep, ep_threads,
                     infer_ms, person_count, mnemonic, size, seed):
    w, h = size
    pal = CARD_PALETTE.get(card, CARD_PALETTE["blueprint"])
    glow_color = tuple(pal["glow"])
    img = _vertical_bg(card, size, seed)
    g = _vertical_geometry(w, h)
    G = Glow(size)

    # 1) 人像进窗（竖版窗口与风格化板同为 4:5，几乎不裁）
    win = g["win"]
    plate = _cover_crop(portrait, win[2] - win[0], win[3] - win[1], anchor_y=0.10)
    img.paste(plate, (win[0], win[1]))
    _rec(win, "portrait")
    img = _grade_window(img, win, mean_target=42.0, hi_lo=4.0, hi_hi=9.0)
    draw = ImageDraw.Draw(img, "RGBA")

    # 2) 照片窗边框 + 四角
    draw.rectangle([win[0] - 1, win[1] - 1, win[2], win[3]], outline=glow_color + (60,), width=1)
    _brackets(draw, G, (win[0] - 2, win[1] - 2, win[2] + 1, win[3] + 1), glow_color,
              arm=int(round(0.052 * w)), width=2)

    # 3) 页眉：官方 logo 锁定组 + 卡片中英双标 + 发光线
    lk = watermark.brand_lockup(1.16)
    img.paste(lk, (g["pad"], g["header_y"]), lk)
    _rec((g["pad"], g["header_y"], g["pad"] + lk.width, g["header_y"] + lk.height), "lockup")
    zh, en = CARD_LABELS.get(card, ("", ""))
    if not watermark.has_cjk_font():
        zh, en = CARD_LABELS_EN.get(card, (en, en))
    f_zh = watermark.load_font(max(14, int(round(w * 0.021))))
    f_en = watermark.load_font(max(10, int(round(w * 0.0128))), mono=True)
    _text(draw, G, (g["right"], g["header_y"] + int(h * 0.006)), zh, f_zh,
          watermark.INK + (240,), anchor="ra", glow_value=90, label="card_zh")
    _text(draw, G, (g["right"], g["header_y"] + int(h * 0.030)), en, f_en,
          glow_color + (238,), anchor="ra", glow_value=190, label="card_en")
    draw.line([(g["pad"], g["rule_y"]), (g["right"], g["rule_y"])],
              fill=glow_color + (120,), width=1)
    G.line([(g["pad"], g["rule_y"]), (g["right"], g["rule_y"])], value=150, width=2)

    # 4) 右栏：真实规格 + 真汇编 + K3 字标 + ISA 徽标 + 话题标签
    cx0, cx1 = g["col"]
    cw = cx1 - cx0
    rl = riscv_lib()
    f_lab = watermark.load_font(max(9, int(round(w * 0.0104))), mono=True)
    f_val = watermark.load_font(max(10, int(round(w * 0.0112))), mono=True)
    y = win[1] + int(h * 0.004)
    _text(draw, G, (cx0, y), "$ spacemit-smi", f_val, glow_color + (236,),
          glow_value=150, label="smi")
    y += int(h * 0.026)
    rows = [("SoC", "K3 · 8xX100 + 8xA100"),
            ("AI", watermark.badge_text(ep, ep_threads))]
    if infer_ms is not None:
        try:
            rows.append(("SEG", "%d ms" % int(round(float(infer_ms)))))
        except (TypeError, ValueError):
            pass
    rows.append(("PERSON", str(int(person_count or 1))))
    label_w = int(cw * 0.30)
    for lab, val in rows:
        _text(draw, G, (cx0 + int(cw * 0.03), y), lab, f_lab, watermark.INK_3 + (250,), label="spec")
        _text(draw, G, (cx0 + label_w, y), val, f_val, watermark.INK + (248,), label="spec")
        y += int(h * 0.0215)
    _text(draw, G, (cx0 + int(cw * 0.03), y), "ISA", f_lab, watermark.INK_3 + (250,), label="spec")
    isa_font = watermark.fit_font(draw, rl.K3_ISA_LINE, cw - label_w - 2,
                                  watermark.fn_size(f_val), mono=True, min_size=7)
    for k, chunk in enumerate((rl.K3_ISA_LINE[:24], rl.K3_ISA_LINE[24:])):
        _text(draw, G, (cx0 + label_w, y + k * int(h * 0.0165)), chunk, isa_font,
              watermark.INK_2 + (245,), label="isa")
    y += int(h * 0.037)

    # 真汇编片段（背景文字必须是真指令）
    y += int(h * 0.012)
    _text(draw, G, (cx0, y), "# kernel", f_lab, glow_color + (200,), glow_value=120, label="asm")
    y += int(h * 0.020)
    f_asm = watermark.load_font(max(9, int(round(w * 0.0104))), mono=True)
    for line in rl.asm_snippet(7, seed=int(seed) % 3):
        _text(draw, G, (cx0, y), line, f_asm, watermark.INK_2 + (232,), glow_value=110, label="asm")
        y += int(h * 0.0172)

    # ISA 徽标（紧跟汇编块）
    hs = watermark.hashtag_strip(scale=(w / 1000.0) * 0.92)
    limit_y = win[3] - hs.height - int(h * 0.018)
    by = y + int(h * 0.014)
    bx = cx0
    f_badge = watermark.load_font(max(8, int(round(w * 0.0096))), mono=True)
    for b in list(rl.isa_badges(int(seed) % 5))[:7]:
        tw = watermark.text_w(draw, b, f_badge) + 20
        if bx + tw > cx1 and bx > cx0:
            bx = cx0
            by += watermark.fn_size(f_badge) + 16
        if by > limit_y - watermark.fn_size(f_badge) - 12:
            break
        _pill(draw, G, (bx, by), b, f_badge, watermark.INK_2)
        bx += int(tw) + 8

    # K3 字标 + 副标
    ky = max(by + watermark.fn_size(f_badge) + int(h * 0.030), win[1] + int(h * 0.335))
    f_k3 = watermark.load_font(max(30, int(round(w * 0.088))), mono=True)
    _text(draw, G, (cx0, ky), "K3", f_k3, glow_color + (150,), glow_value=170, label="k3")
    f_k3b = watermark.load_font(max(9, int(round(w * 0.0112))), mono=True)
    _text(draw, G, (cx0 + int(cw * 0.02), ky + int(h * 0.092)), "RISC-V AI CPU", f_k3b,
          watermark.INK_2 + (235,), label="k3b")

    # 话题标签（背景也烙一份）
    hy = min(win[3] - hs.height, max(win[1], ky + int(h * 0.128)))
    img.paste(hs, (cx0, hy), hs)
    G.d.rounded_rectangle([cx0, hy, cx0 + hs.width, hy + hs.height], radius=8,
                          outline=190, width=2)
    _rec((cx0, hy, cx0 + hs.width, hy + hs.height), "hashtag_strip")

    # 5) RISC-V 指令角（成片底部）
    ix0, iy0, ix1, iy1 = g["instr"]
    _brackets(draw, G, (ix0, iy0, ix1, iy1), glow_color, arm=int(0.036 * w), width=1, alpha=190)
    f_it = watermark.load_font(max(9, int(round(w * 0.0104))), mono=False)
    title = "RISC-V · 指令角" if watermark.has_cjk_font() else "RISC-V"
    _text(draw, G, (ix0 + int(0.022 * w), iy0 + int(0.008 * w)),
          title + "   INSTRUCTION CORNER", f_it, glow_color + (240,),
          glow_value=150, label="instr_title")
    f_ins = watermark.load_font(max(10, int(round(w * 0.0122))), mono=True)
    iy = iy0 + int(0.034 * w)
    for k in range(3):
        _text(draw, G, (ix0 + int(0.022 * w), iy + k * int(0.021 * w)),
              _pick_instruction(rl, int(seed) + _rid_num(record_id) + k), f_ins,
              watermark.INK_2 + (240,), glow_value=95, label="instr")
    f_isa2 = watermark.load_font(max(8, int(round(w * 0.0092))), mono=True)
    for k, chunk in enumerate((rl.K3_ISA_LINE[:24], rl.K3_ISA_LINE[24:])):
        _text(draw, G, (ix1 - int(0.022 * w), iy + k * int(0.017 * w)), chunk,
              f_isa2, watermark.INK_3 + (235,), anchor="ra", label="isa_corner")

    img = G.drive(img, radius=max(6.0, w * 0.013), gain=0.48, tint=glow_color)
    return img

# --------------------------------------------------------------------------
# 横版 1600x1000（cards3 版面）
# --------------------------------------------------------------------------


def _render_horizontal(record_id, portrait, style, name, handle, card, ep, ep_threads,
                       infer_ms, person_count, mnemonic, size, seed):
    w, h = size
    img = _load_card_bg(card, size)
    # 发光主色和竖版同源（CARD_PALETTE），两档才是同一套色。直接从底图采样会采到
    # 卡片自带的杂色 —— 实测 terminal 底图采出暗红 (120,55,67)，和竖版的磷光绿
    # (72,246,156) 不是一个色相，同一风格的两档放一起就像两套皮肤。
    pal = CARD_PALETTE.get(card)
    glow_color = tuple(pal["glow"]) if pal else _sample_glow(img, (58, 214, 255))
    rl = riscv_lib()
    G = Glow(size)

    # 1) 人像贴进照片区（cards3 版面：左下）
    win = (int(round(PHOTO_BOX_H[0] * w)), int(round(PHOTO_BOX_H[1] * h)),
           int(round(PHOTO_BOX_H[2] * w)), int(round(PHOTO_BOX_H[3] * h)))
    plate = _cover_crop(portrait, win[2] - win[0], win[3] - win[1], anchor_y=0.05)
    img.paste(plate, (win[0], win[1]))
    _rec(win, "portrait")
    img = _grade_window(img, win, mean_target=46.0, hi_lo=4.5, hi_hi=9.0)
    draw = ImageDraw.Draw(img, "RGBA")

    # 底图自带的姓名牌占位会从信息带面板里微微透出来，先在原位抹平
    img = _patch_bg(img, (0.740 * w, 0.845 * h, 0.968 * w, 0.898 * h), pad=8, feather=6, radius=12)
    draw = ImageDraw.Draw(img, "RGBA")

    # 窗框 + 四角（压住底图自带的占位框）
    draw.rectangle([win[0] - 2, win[1] - 2, win[2] + 1, win[3] + 1],
                   outline=glow_color + (70,), width=1)
    _brackets(draw, G, (win[0] - 3, win[1] - 3, win[2] + 2, win[3] + 2), glow_color,
              arm=int(0.030 * w), width=2)

    # 2) 官方 logo 锁定组（盖掉底图自带的临时 logo）
    img = _patch_bg(img, (0.030 * w, 0.030 * h, 0.210 * w, 0.086 * h))
    draw = ImageDraw.Draw(img, "RGBA")
    lk = watermark.brand_lockup(1.06)
    img.paste(lk, (int(0.034 * w), int(0.034 * h)), lk)
    _rec((0.034 * w, 0.034 * h, 0.034 * w + lk.width, 0.034 * h + lk.height), "lockup")

    # 3) RISC-V 指令角（照片窗右上角外，避开照片区）
    ix0, iy0 = int(0.472 * w), int(0.462 * h)
    ix1, iy1 = int(0.706 * w), int(0.556 * h)
    img = _patch_bg(img, (ix0, iy0, ix1, iy1), pad=2, feather=6, radius=8)
    draw = ImageDraw.Draw(img, "RGBA")
    draw.rounded_rectangle([ix0, iy0, ix1, iy1], radius=8, fill=(9, 11, 13, 205),
                           outline=glow_color + (95,), width=1)
    G.d.rounded_rectangle([ix0, iy0, ix1, iy1], radius=8, outline=165, width=2)
    f_it = watermark.load_font(max(9, int(round(w * 0.0068))), mono=False)
    f_ins = watermark.load_font(max(10, int(round(w * 0.0080))), mono=True)
    title = "RISC-V · 指令角" if watermark.has_cjk_font() else "RISC-V"
    _text(draw, G, (ix0 + 14, iy0 + 8), title + "   INSTRUCTION CORNER", f_it,
          glow_color + (240,), glow_value=150, label="instr_title")
    for k in range(2):
        _text(draw, G, (ix0 + 14, iy0 + 30 + k * 19),
              _pick_instruction(rl, int(seed) + _rid_num(record_id) + k), f_ins,
              watermark.INK_2 + (245,), glow_value=105, label="instr")
    _rec((ix0, iy0, ix1, iy1), "instr_block")

    img = G.drive(img, radius=max(5.0, w * 0.008), gain=0.44, tint=glow_color)
    return img


# --------------------------------------------------------------------------
# 对外入口（冻结签名）
# --------------------------------------------------------------------------


def compose(record_id, portrait, *, style, name: str = "", handle: str = "", card=None,
            ep: bool = True, ep_threads=None, infer_ms=None, person_count: int = 0,
            mnemonic=None, size=(1000, 1250), seed: int = 0):
    """总装：底图 + 风格化人像 + 文字层，返回 PIL RGB 成片。

    record_id: 编号（int / 数字串 / "SM-2026-0471" 均可）
    portrait:  风格化人像板（``stylize.render`` 输出）或任意人像照片
    style:     natural（当前只有这一档）
    card:      None → 按风格恒定映射（STYLE_TO_CARD）
    """
    style = str(style or "").strip()
    if style not in stylize.STYLES:
        raise ValueError("unknown style: %r (expected one of %s)" % (style, stylize.STYLES))
    size = (int(size[0]), int(size[1]))
    if size not in SIZES:
        raise ValueError("size only accepts %s (got %s)" % (SIZES, size))
    card = card or STYLE_TO_CARD.get(style, CARD_NAMES[0])
    if card not in CARD_NAMES:
        raise ValueError("unknown card: %r (expected one of %s)" % (card, CARD_NAMES))

    if portrait is None:
        portrait = Image.new("RGB", (800, 1000), (22, 24, 28))
    elif isinstance(portrait, np.ndarray):
        portrait = Image.fromarray(np.clip(portrait, 0, 255).astype(np.uint8))
    portrait = portrait.convert("RGB")

    rl = riscv_lib()
    if mnemonic is None:
        rid = _rid_num(record_id)
        mnemonic = rl.instruction_short(int(seed) + rid)
    ep = bool(ep)
    if ep_threads is None:
        ep_threads = 4 if ep else None

    watermark.begin_capture()
    _METRICS.clear()
    if size == VERTICAL:
        img = _render_vertical(record_id, portrait, style, name, handle, card, ep, ep_threads,
                               infer_ms, person_count, mnemonic, size, seed)
        keepout = (int(round(PHOTO_BOX_V[0] * size[0])), int(round(PHOTO_BOX_V[1] * size[1])),
                   int(round(PHOTO_BOX_V[2] * size[0])), int(round(PHOTO_BOX_V[3] * size[1])))
    else:
        img = _render_horizontal(record_id, portrait, style, name, handle, card, ep, ep_threads,
                                 infer_ms, person_count, mnemonic, size, seed)
        keepout = (int(round(KEEPOUT_H[0] * size[0])), int(round(KEEPOUT_H[1] * size[1])),
                   int(round(KEEPOUT_H[2] * size[0])), int(round(KEEPOUT_H[3] * size[1])))

    style_label = stylize.STYLE_LABELS.get(style, style)
    img = watermark.apply_watermark(img, record_id, name=name, handle=handle,
                                    style_label=style_label, mnemonic=mnemonic, ep=ep,
                                    infer_ms=infer_ms, person_count=person_count)

    # 收尾：文字层加上来之后只封顶（不定标 —— 底图与人像窗各自已在窗口内）
    img = _cap_dark(img)

    mt = stylize.dark_metrics(img)
    mt["record_id"] = watermark.format_id(record_id)
    mt["card"] = card
    mt["style"] = style
    _METRICS.append(mt)
    _check_guard(size[0], size[1], keepout)
    return img


def self_test() -> dict:
    """结构自检：两档尺寸、原色成片、姓名占位、编号、徽章诚实、几何不越界。"""
    out = {}
    rl = riscv_lib()
    src = str(getattr(rl, "source", "builtin"))
    out["riscv_source"] = os.path.basename(src) if src != "builtin" else "builtin"
    out["card_names"] = len(CARD_NAMES)
    out["vertical"] = VERTICAL
    out["horizontal"] = HORIZONTAL

    # 合成人像（自带掩码形状，不依赖网络）
    W, H = 720, 900
    yy, xx = np.mgrid[0:H, 0:W].astype(np.float32)
    arr = np.zeros((H, W, 3), np.float32)
    arr[..., 0] = 30 + 46 * (yy / H)
    arr[..., 1] = 33 + 50 * (yy / H)
    arr[..., 2] = 40 + 58 * (yy / H)
    head = (((xx - 360) / 132.0) ** 2 + ((yy - 300) / 168.0) ** 2) < 1.0
    hair = (((xx - 360) / 146.0) ** 2 + ((yy - 276) / 164.0) ** 2) < 1.0
    body = (np.abs(xx - 360) < (96 + np.maximum(0, yy - 470) * 0.62)) & (yy > 440)
    arr[body] = (62, 70, 88)
    arr[hair] = (28, 25, 24)
    lit = (0.72 + 0.28 * np.clip(1.0 - ((xx - 300) ** 2 + (yy - 260) ** 2) / 90000.0, 0, 1))
    arr[head] = np.array((208, 170, 148), np.float32) * lit[head][:, None]
    photo = Image.fromarray(np.clip(arr, 0, 255).astype(np.uint8), "RGB")
    mask = (hair | body).astype(np.uint8) * 255

    for style in stylize.STYLES:
        plate = stylize.render(style, photo, mask, size=VERTICAL, seed=5)
        out["plate_%s" % style] = plate.size
        img = compose(471, plate, style=style, name="张伟", handle="@zhangwei",
                      ep=True, infer_ms=205, person_count=1, size=VERTICAL, seed=5)
        mt = stylize.dark_metrics(img)
        out["%s_v_ok" % style] = bool(img.size == VERTICAL
                                      and stylize.natural_metrics_ok(mt))
        out["%s_v_mean" % style] = mt["mean_luma"]
        out["%s_v_hi" % style] = mt["hi_luma_pct"]
        out["%s_v_detail" % style] = mt["detail_pct"]
        out["%s_v_guard" % style] = len(guard_report())
        img_h = compose(471, plate, style=style, name="LI MING", handle="@liming",
                        ep=False, infer_ms=1180, person_count=2, size=HORIZONTAL, seed=5)
        mh = stylize.dark_metrics(img_h)
        out["%s_h_ok" % style] = bool(img_h.size == HORIZONTAL
                                      and stylize.natural_metrics_ok(mh))
        out["%s_h_mean" % style] = mh["mean_luma"]
        out["%s_h_hi" % style] = mh["hi_luma_pct"]
        out["%s_h_guard" % style] = len(guard_report())

    # 姓名缺失 → 占位（不留空）
    blank = compose(1, Image.new("RGB", (800, 1000), (26, 28, 32)), style="natural", name="",
                    size=VERTICAL, seed=2)
    band = np.asarray(blank.convert("L").crop((60, 1080, 700, 1150)), np.float32)
    out["blank_name_has_ink"] = bool((band > 140).mean() > 0.01)

    out["badge_cpu"] = watermark.badge_text(False)
    out["badge_cpu_honest"] = watermark.badge_text(False) == "CPU"
    out["id"] = watermark.format_id(471)
    out["id_shape_ok"] = bool(watermark.format_id(471) == "SM-2026-0471")
    out["style_to_card"] = dict(STYLE_TO_CARD)

    try:
        compose(1, photo, style="natural", size=(1080, 1350))
        out["size_guard"] = False
    except ValueError:
        out["size_guard"] = True
    try:
        compose(1, photo, style="nope")
        out["style_guard"] = False
    except ValueError:
        out["style_guard"] = True

    out["ok"] = all(v for k, v in out.items() if isinstance(v, bool))
    return out


if __name__ == "__main__":
    import json

    print(json.dumps(self_test(), ensure_ascii=False, indent=2, default=str))
    print()
    print("guard:", guard_report())
