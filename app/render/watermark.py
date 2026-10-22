# -*- coding: utf-8 -*-
"""成片文字层：官方 logo 锁定组 + 水印信息带 + 编号 + EP/CPU 徽章 · T-3

三件事在这里收口：

1. **字体探测**：目标机是 K3/Bianbu（/usr/share/fonts），开发机是 Windows
   （C:/Windows/Fonts）。找不到 CJK 字体时**退英文排版**，不是画一串豆腐块。
2. **官方 logo**：`app/assets/brand/spacemit-logo.svg`（三切面绿）。本机没有
   cairosvg，所以这里自带一个极小的 SVG path 光栅器（只支持 M/L/H/V/C/Z，
   这个 logo 只用到这些）。素材缺失时退化成自绘几何芯片，绝不抛异常。
3. **水印带**：竖版 1000x1250 是整宽三行带；横版 1600x1000 沿用 cards3 版面，
   信息带只能放在**照片区右侧**（左下角是照片，底边左侧没有可用空间 ——
   knowledge/03 §3 记录过这个越界）。

诚实优先：``badge_text(False)`` 必须返回 ``"CPU"``。拿不到 AI 核就如实标 CPU，
不得冒充（docs/spec/系统规格-v2.md §8 / §11.4）。
"""

from __future__ import annotations

import os
import re

import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont

# --------------------------------------------------------------------------
# 冻结常量（docs/spec/系统规格-v2.md §11.4）
# --------------------------------------------------------------------------

HASHTAG = "#SpacemiTDevCon"
EVENT = "SpacemiT DevCon 2026"

BRAND_NAME = "SpacemiT"
EVENT_SHORT = "DevCon 2026"
BRAND = (178, 228, 13)          # #B2E40D 酸柠绿（品牌主色）
BRAND_DIM = (138, 176, 34)
LOGO_GREENS = ("#B0E237", "#C0E767", "#D0EE90")   # 官方三切面绿
ID_YEAR = 2026                 # 编号年份：SM-YYYY-NNNN，活动固定 2026

INK = (243, 247, 248)
INK_2 = (168, 179, 183)
INK_3 = (112, 122, 127)
PANEL = (6, 7, 9)

EN_STYLE_LABELS = {
    "natural": "NATURAL",
}

# card.py 传进来的是 stylize.STYLE_LABELS 的中文名，不是 key —— 缺字体时
# 必须再查一次"中文名 → 英文"，否则成片上就是一个豆腐块。
_CN_STYLE_TO_EN = {
    "原色": "NATURAL",
}

_HERE = os.path.dirname(os.path.abspath(__file__))
_ASSET_ROOT = os.path.normpath(os.path.join(_HERE, ".."))
LOGO_CANDIDATES = (
    os.path.join(_ASSET_ROOT, "assets", "brand", "spacemit-logo.svg"),
    os.path.join(_HERE, "assets", "brand", "spacemit-logo.svg"),
)

# --------------------------------------------------------------------------
# 字体探测
# --------------------------------------------------------------------------

SANS_CANDIDATES = (
    os.path.join(_ASSET_ROOT, "assets", "fonts", "NotoSansCJK-Bold.ttc"),
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJKsc-Bold.otf",
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Bold.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
    "/usr/share/fonts/truetype/arphic/uming.ttc",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    "C:/Windows/Fonts/msyhbd.ttc",
    "C:/Windows/Fonts/msyh.ttc",
    "C:/Windows/Fonts/simhei.ttf",
    "C:/Windows/Fonts/Dengb.ttf",
    "C:/Windows/Fonts/segoeuib.ttf",
    "C:/Windows/Fonts/arialbd.ttf",
)

MONO_CANDIDATES = (
    "/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationMono-Bold.ttf",
    "/usr/share/fonts/opentype/noto/NotoSansMono-Bold.ttf",
    "C:/Windows/Fonts/consolab.ttf",
    "C:/Windows/Fonts/consola.ttf",
    "C:/Windows/Fonts/msyhbd.ttc",
)

_CJK_HINTS = ("noto", "wqy", "uming", "msyh", "simhei", "simsun", "deng", "sourcehan", "droidsansfallback")

_font_cache: dict = {}


def _first_existing(paths):
    for path in paths:
        if os.path.isfile(path):
            return path
    return None


def load_font(size: int, mono: bool = False):
    """按探测链取字体；都找不到就退 PIL 内置位图字体（不崩）。"""
    size = max(6, int(size))
    key = ("mono" if mono else "sans", size)
    if key in _font_cache:
        return _font_cache[key]
    path = _first_existing(MONO_CANDIDATES if mono else SANS_CANDIDATES)
    font = None
    if path:
        try:
            font = ImageFont.truetype(path, size)
        except Exception:
            font = None
    if font is None:
        try:
            font = ImageFont.load_default(size)
        except Exception:
            font = ImageFont.load_default()
    _font_cache[key] = font
    return font


def font_path(mono: bool = False):
    return _first_existing(MONO_CANDIDATES if mono else SANS_CANDIDATES)


def has_cjk_font() -> bool:
    """有没有可用的 CJK 字体。没有就把中文文案换成英文，避免豆腐块。"""
    path = font_path(False)
    if not path:
        return False
    return any(k in os.path.basename(path).lower() for k in _CJK_HINTS)


_CJK_RE = re.compile(r"[\u3000-\u303f\u3400-\u4dbf\u4e00-\u9fff\uff00-\uffef]")


def sanitize_name(name: str, limit: int = 24) -> str:
    """去控制字符、压空白、限长；无 CJK 字体时去掉中文，避免豆腐块。"""
    s = "" if name is None else str(name)
    s = "".join(ch for ch in s if ch >= " " or ch == "\t")
    s = re.sub(r"\s+", " ", s).strip()
    if s and not has_cjk_font():
        s = _CJK_RE.sub("", s).strip()
    return s[:limit]


def sanitize_handle(handle: str, limit: int = 20) -> str:
    s = "" if handle is None else str(handle).strip()
    s = s.lstrip("@").strip()
    s = re.sub(r"[^A-Za-z0-9._-]", "", s)
    return s[:limit]


def style_text(style_label: str) -> str:
    """风格中文名在没有 CJK 字体时换英文。style_label 接受风格 key（natural）或中文名（原色）。"""
    if not style_label:
        return ""
    if has_cjk_font():
        return style_label
    if style_label in EN_STYLE_LABELS:
        return EN_STYLE_LABELS[style_label]
    if style_label in _CN_STYLE_TO_EN:
        return _CN_STYLE_TO_EN[style_label]
    # 兜底：任何漏网的汉字都剥掉 —— 目标机上豆腐块比信息缺失更难看。
    return _CJK_RE.sub("", style_label).strip(" ·-/") or style_label


def format_id(record_id) -> str:
    """编号 SM-YYYY-NNNN。接受 int / 数字串 / 已成形编号 / 任意 id。"""
    if isinstance(record_id, bool) or record_id is None:
        n = 0
    elif isinstance(record_id, (int, np.integer)):
        n = int(record_id)
    else:
        s = str(record_id).strip()
        m = re.search(r"(\d+)\s*$", s)
        n = int(m.group(1)) if m else 0
    return "SM-%d-%04d" % (ID_YEAR, n)


def badge_text(ep: bool, ep_threads=None) -> str:
    """AI 核徽章文案。ep=False 必须是 "CPU"（诚实优先，不得冒充 EP）。"""
    if not ep:
        return "CPU"
    text = "EP SpaceMIT"
    try:
        n = int(ep_threads) if ep_threads is not None else 0
    except (TypeError, ValueError):
        n = 0
    if n > 0:
        text += " · %dT" % n
    return text


# --------------------------------------------------------------------------
# 记录绘制包围盒（几何守护用：越界 = 现场出废片）
# --------------------------------------------------------------------------

_BOXES: list = []


def begin_capture() -> None:
    _BOXES.clear()


def captured_boxes() -> list:
    return list(_BOXES)


def _record(box, label: str) -> None:
    _BOXES.append((tuple(float(v) for v in box), str(label)))


def draw_text(draw, xy, text, font, fill, *, anchor="la", label=None):
    """画文字并记录真实包围盒（textbbox，不是估算）。"""
    if not text:
        return (0.0, 0.0, 0.0, 0.0)
    try:
        box = draw.textbbox(xy, text, font=font, anchor=anchor)
    except Exception:
        box = (xy[0], xy[1], xy[0] + 10 * len(text), xy[1] + 10)
    draw.text(xy, text, font=font, fill=fill, anchor=anchor)
    _record(box, label or text[:24])
    return box


def fn_size(font) -> int:
    return int(getattr(font, "size", 16) or 16)


def text_w(draw, text, font) -> float:
    try:
        return float(draw.textlength(text, font=font))
    except Exception:
        return float(len(text) * fn_size(font) * 0.6)


def fit_font(draw, text, max_width, size, mono=False, min_size=8):
    """字号自适应：超宽就换小号（现场姓名长度不可控，不能让它压出画布）。"""
    s = int(size)
    while s > min_size:
        font = load_font(s, mono=mono)
        if text_w(draw, text, font) <= max_width:
            return font
        s = int(s * 0.90)
    return load_font(min_size, mono=mono)


# --------------------------------------------------------------------------
# 官方 logo：极小的 SVG path 光栅器
# --------------------------------------------------------------------------

_TOKEN_RE = re.compile(r"([MmLlHhVvCcSsQqTtAaZz])|(-?(?:\d+\.\d*|\.\d+|\d+)(?:[eE][-+]?\d+)?)")
_NUM_RE = re.compile(r"^-?(?:\d+\.\d*|\.\d+|\d+)(?:[eE][-+]?\d+)?$")


def _tokens(d: str):
    out = []
    for m in _TOKEN_RE.finditer(d):
        if m.group(1):
            out.append((m.group(1), None))
        else:
            out.append(("n", float(m.group(2))))
    return out


def _bezier(p0, p1, p2, p3, n=18):
    pts = []
    for i in range(1, n + 1):
        t = i / float(n)
        u = 1.0 - t
        x = u * u * u * p0[0] + 3 * u * u * t * p1[0] + 3 * u * t * t * p2[0] + t * t * t * p3[0]
        y = u * u * u * p0[1] + 3 * u * u * t * p1[1] + 3 * u * t * t * p2[1] + t * t * t * p3[1]
        pts.append((x, y))
    return pts


def svg_subpaths(d: str):
    """把 path d 拍平成折线子路径（支持 M/L/H/V/C/Z 与隐式重复）。"""
    toks = _tokens(d)
    subs: list = []
    sub: list = []
    cur = (0.0, 0.0)
    start = (0.0, 0.0)
    cmd = None
    i = 0
    while i < len(toks):
        kind, val = toks[i]
        if kind != "n":
            cmd = kind
            i += 1
            if cmd in "Zz":
                if sub:
                    sub.append(start)
                    subs.append(sub)
                    sub = []
                cur = start
                cmd = None
            continue
        if cmd is None:
            i += 1
            continue
        rel = cmd.islower()
        c = cmd.upper()
        need = {"M": 2, "L": 2, "H": 1, "V": 1, "C": 6, "S": 4, "Q": 4, "T": 2, "A": 7}.get(c)
        if need is None or i + need > len(toks):
            break
        args = [toks[i + k][1] for k in range(need)]
        i += need
        if c == "M":
            p = (args[0], args[1])
            if rel:
                p = (cur[0] + p[0], cur[1] + p[1])
            cur = p
            start = p
            if sub:
                subs.append(sub)
            sub = [cur]
            cmd = "l" if rel else "L"
        elif c == "L":
            p = (args[0], args[1])
            if rel:
                p = (cur[0] + p[0], cur[1] + p[1])
            cur = p
            sub.append(cur)
        elif c == "H":
            x = args[0] + (cur[0] if rel else 0.0)
            cur = (x, cur[1])
            sub.append(cur)
        elif c == "V":
            y = args[0] + (cur[1] if rel else 0.0)
            cur = (cur[0], y)
            sub.append(cur)
        elif c == "C":
            pts = [args[0:2], args[2:4], args[4:6]]
            if rel:
                pts = [(cur[0] + p[0], cur[1] + p[1]) for p in pts]
            sub.extend(_bezier(cur, pts[0], pts[1], pts[2]))
            cur = pts[2]
        elif c == "S":
            pts = [args[0:2], args[2:4]]
            if rel:
                pts = [(cur[0] + p[0], cur[1] + p[1]) for p in pts]
            sub.extend(_bezier(cur, cur, pts[0], pts[1]))
            cur = pts[1]
        elif c == "Q":
            pts = [args[0:2], args[2:4]]
            if rel:
                pts = [(cur[0] + p[0], cur[1] + p[1]) for p in pts]
            sub.extend(_bezier(cur, cur, pts[0], pts[1]))
            cur = pts[1]
        else:      # T / A 用不到，跳过
            continue
    if sub:
        subs.append(sub)
    return subs


def _hex_rgb(s: str):
    s = (s or "").strip().lstrip("#")
    if len(s) == 3:
        s = "".join(ch * 2 for ch in s)
    try:
        return (int(s[0:2], 16), int(s[2:4], 16), int(s[4:6], 16))
    except Exception:
        return (176, 226, 55)


def load_logo_shapes(path: str | None = None):
    """读官方 SVG → [(子路径点列, RGB), ...]（单位 = viewBox）。失败返回 []。"""
    src = path or _first_existing(LOGO_CANDIDATES)
    if not src or not os.path.isfile(src):
        return []
    try:
        with open(src, "r", encoding="utf-8") as fh:
            svg = fh.read()
    except Exception:
        return []
    shapes = []
    for m in re.finditer(r"<path\b[^>]*>", svg):
        tag = m.group(0)
        dm = re.search(r'\sd\s*=\s*"([^"]+)"', tag)
        fm = re.search(r'fill\s*=\s*"([^"]+)"', tag)
        if not dm:
            continue
        color = _hex_rgb(fm.group(1)) if fm else (176, 226, 55)
        for sub in svg_subpaths(dm.group(1)):
            if len(sub) >= 3:
                shapes.append((sub, color))
    vb = re.search(r'viewBox\s*=\s*"([^"]+)"', svg)
    box = None
    if vb:
        try:
            vals = [float(v) for v in re.split(r"[ ,]+", vb.group(1).strip()) if v]
            if len(vals) == 4:
                box = tuple(vals)
        except Exception:
            box = None
    return shapes, box


_logo_cache: dict = {}


def logo_glyph(height: float, *, supersample: int = 6) -> Image.Image:
    """官方三切面绿 logo 图形（RGBA），高度 = height 像素。"""
    key = ("glyph", round(float(height), 2))
    if key in _logo_cache:
        return _logo_cache[key].copy()
    h = max(8, int(round(height)))
    loaded = load_logo_shapes()
    shapes, box = (loaded if loaded else ([], None))
    if shapes and box:
        vx, vy, vw, vh = box
        scale = h / float(vh)
        w = int(round(vw * scale))
    else:
        return _fallback_glyph(h)
    S = supersample
    img = Image.new("RGBA", (w * S, h * S), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    for pts, color in shapes:
        poly = [((px - vx) * scale * S, (py - vy) * scale * S) for px, py in pts]
        d.polygon(poly, fill=color + (255,))
    img = img.resize((w, h), Image.LANCZOS)
    _logo_cache[key] = img
    return img.copy()


def _fallback_glyph(h: int) -> Image.Image:
    """素材缺失时的自绘回退（三切面绿几何体），不是官方 logo。"""
    w = int(round(h * 27.0 / 34.0))
    S = 6
    img = Image.new("RGBA", (w * S, h * S), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    colors = [_hex_rgb(c) for c in LOGO_GREENS]
    d.polygon([(0.04 * w * S, 0.36 * h * S), (0.46 * w * S, 0.15 * h * S),
               (0.46 * w * S, 0.62 * h * S), (0.04 * w * S, 0.83 * h * S)], fill=colors[0] + (255,))
    d.polygon([(0.54 * w * S, 0.02 * h * S), (0.97 * w * S, 0.14 * h * S),
               (0.97 * w * S, 0.46 * h * S), (0.54 * w * S, 0.34 * h * S)], fill=colors[1] + (255,))
    d.polygon([(0.54 * w * S, 0.44 * h * S), (0.80 * w * S, 0.51 * h * S),
               (0.80 * w * S, 0.68 * h * S), (0.54 * w * S, 0.61 * h * S)], fill=colors[2] + (255,))
    return img.resize((w, h), Image.LANCZOS)


# --------------------------------------------------------------------------
# 发光合成
# --------------------------------------------------------------------------


def screen_add(rgb: np.ndarray, glow_l: np.ndarray, tint, gain: float, radius: float) -> np.ndarray:
    """把一张 L 图层模糊后以 screen 方式叠回（发光而非填充）。"""
    if glow_l is None:
        return rgb
    soft = np.asarray(Image.fromarray(np.clip(glow_l, 0, 255).astype(np.uint8), "L")
                      .filter(ImageFilter.GaussianBlur(max(0.4, radius))), np.float32) / 255.0
    add = soft[..., None] * (np.array(tint, np.float32) * float(gain))
    return 255.0 - (255.0 - rgb) * (255.0 - np.clip(add, 0, 255)) / 255.0


def glow_pass(image: Image.Image, layer_l: Image.Image, radius: float, gain: float, tint) -> Image.Image:
    arr = np.asarray(image.convert("RGB"), np.float32)
    out = screen_add(arr, np.asarray(layer_l, np.float32), tint, gain, radius)
    return Image.fromarray(np.clip(out, 0, 255).astype(np.uint8), "RGB")


def _screen_rect(img: Image.Image, box, color, alpha: float, radius: float = 4.0):
    """把一块矩形以 screen 方式加亮（做"发光面板"的顶边）。"""
    arr = np.asarray(img.convert("RGB"), np.float32)
    x0, y0, x1, y1 = (int(round(v)) for v in box)
    x0, y0 = max(0, x0), max(0, y0)
    x1, y1 = min(img.size[0], x1), min(img.size[1], y1)
    if x1 <= x0 or y1 <= y0:
        return img
    layer = np.zeros(img.size[::-1], np.float32)
    layer[y0:y1, x0:x1] = 255.0
    out = screen_add(arr, layer, color, alpha, radius)
    return Image.fromarray(np.clip(out, 0, 255).astype(np.uint8), "RGB")


# --------------------------------------------------------------------------
# 品牌锁定组
# --------------------------------------------------------------------------


def brand_lockup(scale: float = 1.0, *, show_hashtag: bool = True, max_width: int | None = None) -> Image.Image:
    """官方 logo +「SpacemiT DevCon 2026」+ #SpacemiTDevCon 的锁定组合（RGBA）。

    scale=1.0 对应字标字号 26px（以 1600px 画幅为基准）。
    """
    S = 3
    word = 26.0 * scale
    evt = 15.0 * scale
    tag = 12.5 * scale
    glyph_h = word * 1.32
    f_word = load_font(int(round(word * S)))
    f_evt = load_font(int(round(evt * S)))
    f_tag = load_font(int(round(tag * S)), mono=True)
    probe = ImageDraw.Draw(Image.new("RGB", (8, 8)))
    glyph = logo_glyph(glyph_h)
    gw = int(glyph.width * S)
    word_w = text_w(probe, BRAND_NAME, f_word)
    evt_w = text_w(probe, EVENT_SHORT, f_evt)
    tag_w = text_w(probe, HASHTAG, f_tag) if show_hashtag else 0.0
    gap1 = word * 0.42 * S
    gap2 = word * 0.30 * S
    total_w = gw + gap1 + word_w + gap2 + evt_w
    if show_hashtag:
        total_w = max(total_w, tag_w)
    height = glyph_h * S + (tag * 1.9 * S if show_hashtag else 0)

    img = Image.new("RGBA", (int(total_w) + 6, int(height) + 6), (0, 0, 0, 0))
    d = ImageDraw.Draw(img, "RGBA")
    img.alpha_composite(glyph.resize((gw, int(glyph_h * S)), Image.LANCZOS), (0, 0))
    d.text((gw + gap1, glyph_h * S * 0.10), BRAND_NAME, font=f_word, fill=INK + (250,), anchor="la")
    d.text((gw + gap1 + word_w + gap2, glyph_h * S * 0.10 + word * 0.34 * S), EVENT_SHORT,
           font=f_evt, fill=INK_2 + (235,), anchor="la")
    if show_hashtag:
        d.text((0, glyph_h * S + tag * 0.55 * S), HASHTAG, font=f_tag, fill=BRAND + (238,), anchor="la")
    out = img.resize((max(1, img.width // S), max(1, img.height // S)), Image.LANCZOS)
    if max_width and out.width > max_width:
        ratio = max_width / float(out.width)
        out = out.resize((max_width, max(1, int(out.height * ratio))), Image.LANCZOS)
    return out


def hashtag_strip(tag: str = HASHTAG, *, scale: float = 1.0, fg=BRAND, bg=(9, 12, 10)) -> Image.Image:
    """话题标签条：暗底 + 品牌色描边，贴在成片/分享图角上。"""
    S = 3
    size = 20.0 * scale
    f = load_font(int(round(size * S)), mono=True)
    probe = ImageDraw.Draw(Image.new("RGB", (8, 8)))
    tw = text_w(probe, tag, f)
    pad = 15.0 * scale * S
    w = int(tw + pad * 2)
    h = int(size * 2.1 * S)
    img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(img, "RGBA")
    d.rounded_rectangle([0, 0, w - 1, h - 1], radius=int(h * 0.28), fill=bg + (225,))
    d.rounded_rectangle([0, 0, w - 1, h - 1], radius=int(h * 0.28),
                        outline=fg + (170,), width=max(1, int(1.2 * S)))
    d.text((w / 2, h / 2), tag, font=f, fill=fg + (252,), anchor="mm")
    return img.resize((max(1, img.width // S), max(1, img.height // S)), Image.LANCZOS)


def badge_pill(text: str, *, size: int, ep: bool = True) -> Image.Image:
    """EP / CPU 徽章（RGBA）。EP 用品牌绿，CPU 用中性灰 —— 一眼能分辨。"""
    S = 3
    fg = BRAND if ep else (186, 194, 198)
    bg = (14, 20, 10) if ep else (16, 17, 19)
    f = load_font(int(round(size * S)), mono=True)
    probe = ImageDraw.Draw(Image.new("RGB", (8, 8)))
    tw = text_w(probe, text, f)
    pad = 11.0 * size / 16.0 * S
    w = int(tw + pad * 2)
    h = int(size * 1.72 * S)
    img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(img, "RGBA")
    d.rounded_rectangle([0, 0, w - 1, h - 1], radius=int(h * 0.30), fill=bg + (232,))
    d.rounded_rectangle([0, 0, w - 1, h - 1], radius=int(h * 0.30),
                        outline=fg + (205,), width=max(1, int(1.1 * S)))
    d.text((w / 2, h / 2), text, font=f, fill=fg + (252,), anchor="mm")
    return img.resize((max(1, img.width // S), max(1, img.height // S)), Image.LANCZOS)


# --------------------------------------------------------------------------
# 信息带
# --------------------------------------------------------------------------


def _panel_layer(size, box, top_alpha=244, bot_alpha=252) -> Image.Image:
    """信息带底板：向上渐隐的近黑，避免"贴一块补丁"的观感。"""
    x0, y0, x1, y1 = (int(round(v)) for v in box)
    h = max(2, y1 - y0)
    rows = np.zeros((h, 1, 4), np.float32)
    ramp = np.linspace(top_alpha, bot_alpha, h, dtype=np.float32)
    rows[..., 0] = PANEL[0]
    rows[..., 1] = PANEL[1]
    rows[..., 2] = PANEL[2]
    rows[..., 3] = ramp[:, None]
    layer = Image.fromarray(rows.astype(np.uint8), "RGBA").resize((x1 - x0, h), Image.BILINEAR)
    out = Image.new("RGBA", size, (0, 0, 0, 0))
    out.alpha_composite(layer, (x0, y0))
    return out


def _paint_band(img: Image.Image, box, *, hairline=True, radius=6.0):
    """画信息带底板 + 顶部品牌色发光线。"""
    img = Image.alpha_composite(img.convert("RGBA"), _panel_layer(img.size, box)).convert("RGB")
    x0, y0, x1, y1 = box
    if hairline:
        img = _screen_rect(img, (x0, y0 - 2, x1, y0 + 1), BRAND, 0.55, radius)
        img = _screen_rect(img, (x0, y0, x1, y0 + 1), BRAND, 0.85, radius * 0.5)
    return img


def _row_layout(w: int, h: int):
    """按画幅决定信息带几何。竖版整宽三行；横版只能占照片区右侧（cards3 版面）。"""
    vertical = h >= w * 1.1
    if vertical:
        band_h = int(round(0.188 * h))
        top = h - band_h
        pad = int(round(0.048 * w))
        box = (0, top, w, h)
        sizes = {
            "tag": max(15, int(round(band_h * 0.125))),
            "id": max(14, int(round(band_h * 0.108))),
            "name": max(24, int(round(band_h * 0.235))),
            "handle": max(12, int(round(band_h * 0.088))),
            "sub": max(11, int(round(band_h * 0.072))),
        }
        rows = {"r1": top + int(band_h * 0.105), "r2": top + int(band_h * 0.345),
                "r3": top + int(band_h * 0.745)}
        box_l = (0, top, w, h)
    else:
        x0 = int(round(0.462 * w))
        x1 = int(round(0.972 * w))
        y0 = int(round(0.800 * h))
        y1 = int(round(0.952 * h))
        band_h = y1 - y0
        pad = int(round(0.022 * w))
        box_l = (x0 - 10, y0 - 10, x1 + 10, y1 + 8)
        sizes = {
            "tag": max(12, int(round(band_h * 0.135))),
            "id": max(11, int(round(band_h * 0.120))),
            "name": max(20, int(round(band_h * 0.250))),
            "handle": max(10, int(round(band_h * 0.100))),
            "sub": max(9, int(round(band_h * 0.090))),
        }
        rows = {"r1": y0 + int(band_h * 0.06), "r2": y0 + int(band_h * 0.30),
                "r3": y0 + int(band_h * 0.76)}
    return vertical, box_l, pad, sizes, rows


def apply_watermark(image: Image.Image, record_id, *, name: str = "", handle: str = "",
                    style_label: str = "", mnemonic: str = "", ep: bool = True,
                    infer_ms=None, person_count: int = 0) -> Image.Image:
    """在成片上加水印信息带，返回新图。

    三行版式（knowledge/03 §8）::

        #SpacemiTDevCon                     [EP SpaceMIT]  0471
        张伟 ZHANG WEI                                @zhangwei
        高对比 · RISC-V addi sp, sp, -16      SpacemiT DevCon 2026
    """
    img = image.convert("RGB").copy()
    w, h = img.size
    vertical, box_l, pad, sizes, rows = _row_layout(w, h)
    img = _paint_band(img, box_l)
    draw = ImageDraw.Draw(img, "RGBA")
    x0, x1 = box_l[0] + pad, box_l[2] - pad

    label = format_id(record_id).rsplit("-", 1)[-1]
    tag = style_text(style_label)
    handle_s = sanitize_handle(handle)
    name_s = sanitize_name(name)

    # ── 行 1：话题标签（左） + 徽章/编号（右）
    f_tag = fit_font(draw, HASHTAG, (x1 - x0) * 0.56, sizes["tag"], mono=True)
    draw_text(draw, (x0, rows["r1"]), HASHTAG, f_tag, BRAND + (250,), label="hashtag")

    badge = badge_text(ep, None)
    pill = badge_pill(badge, size=max(9, int(sizes["id"] * 0.86)), ep=bool(ep))
    f_id = load_font(sizes["id"], mono=True)
    id_w = text_w(draw, label, f_id)
    pill_x = x1 - id_w - int(w * 0.012) - pill.width
    pill_y = rows["r1"] - int(pill.height * 0.10)
    img.paste(pill, (int(pill_x), int(pill_y)), pill)
    _record((pill_x, pill_y, pill_x + pill.width, pill_y + pill.height), "badge")
    draw_text(draw, (x1, rows["r1"]), label, f_id, INK + (250,), anchor="ra", label="id")

    # ── 行 2：姓名（最重） + handle
    name_show = name_s or "DEVELOPER"
    f_sub = load_font(sizes["sub"])
    handle_w = text_w(draw, "@" + handle_s, load_font(sizes["handle"])) if handle_s else 0.0
    f_name = fit_font(draw, name_show, (x1 - x0) * 0.90 - handle_w - pad, sizes["name"])
    draw_text(draw, (x0, rows["r2"]), name_show, f_name, INK + (252,), label="name")
    if handle_s:
        f_handle = load_font(sizes["handle"])
        draw_text(draw, (x1, rows["r2"] + fn_size(f_name) * 0.55), "@" + handle_s, f_handle,
                  BRAND_DIM + (245,), anchor="ra", label="handle")

    # ── 行 3：风格 · 指令 · 耗时（左） + 主办方（右）
    left_bits = []
    if tag:
        left_bits.append(tag)
    if mnemonic:
        left_bits.append("RISC-V " + str(mnemonic))
    if infer_ms is not None:
        try:
            left_bits.append("%d ms" % int(round(float(infer_ms))))
        except (TypeError, ValueError):
            pass
    if person_count and int(person_count) > 0:
        left_bits.append("%d PERSON" % int(person_count) if int(person_count) == 1
                         else "%d PERSONS" % int(person_count))
    left = "   ·   ".join(left_bits)
    # 副信息行用**含 CJK 的无衬线字体**：mono 字体（consola/DejaVu）没有汉字，
    # 风格名会变豆腐块。拉丁部分靠等宽感不如"能读"重要。
    evt_w = text_w(draw, EVENT, load_font(sizes["sub"]))
    f_left = fit_font(draw, left, (x1 - x0) - evt_w - pad, sizes["sub"], mono=False, min_size=7)
    draw_text(draw, (x0, rows["r3"]), left, f_left, INK_2 + (240,), label="sub")
    f_evt = fit_font(draw, EVENT, (x1 - x0) * 0.45, sizes["sub"])
    draw_text(draw, (x1, rows["r3"]), EVENT, f_evt, INK + (238,), anchor="ra", label="event")

    # 细边框：让成片在相册缩略图里也「像一张证」
    inset = max(8, int(min(w, h) * 0.016))
    draw.rectangle([inset, inset, w - inset - 1, h - inset - 1],
                   outline=BRAND_DIM + (52,), width=1)
    return img


def self_test() -> dict:
    """结构自检：签名、徽章诚实性、编号格式、logo 可用、字体降级安全。"""
    from PIL import Image as _I

    out = {}
    out["hashtag"] = HASHTAG
    out["event"] = EVENT
    out["badge_ep"] = badge_text(True)
    out["badge_cpu"] = badge_text(False)
    out["badge_cpu_honest"] = badge_text(False) == "CPU"
    out["badge_ep_ok"] = badge_text(True).startswith("EP SpaceMIT")
    out["badge_ep_threads"] = badge_text(True, 4)
    out["badge_cpu_ignores_threads"] = badge_text(False, 4) == "CPU"
    out["id_format"] = format_id(471)
    out["id_from_string"] = format_id("SM-2026-0042")
    gl = logo_glyph(40)
    alpha = np.asarray(gl)[..., 3]
    out["logo_size"] = gl.size
    out["logo_has_ink"] = bool((alpha > 16).mean() > 0.05)
    lk = brand_lockup(1.0)
    out["lockup_size"] = lk.size
    out["lockup_has_ink"] = bool((np.asarray(lk)[..., 3] > 16).mean() > 0.05)
    base = _I.new("RGB", (1000, 1250), (8, 8, 10))
    wm = apply_watermark(base, 471, name="张伟", handle="@zhangwei", style_label="原色",
                         mnemonic="addi sp, sp, -16", ep=False, infer_ms=205, person_count=1)
    out["band_size"] = wm.size
    out["band_dark_ok"] = float(np.asarray(wm.convert("L"), np.float32).mean()) < 60
    boxes = captured_boxes()
    out["boxes_recorded"] = len(boxes)
    ok_inside = all(0 <= b[0] and 0 <= b[1] and b[2] <= 1000 and b[3] <= 1250 for b, _ in boxes)
    out["no_overflow"] = bool(ok_inside)
    out["ok"] = all(v for k, v in out.items() if isinstance(v, bool))
    return out


if __name__ == "__main__":
    import json

    print(json.dumps(self_test(), ensure_ascii=False, indent=2, default=str))
