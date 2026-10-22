# -*- coding: utf-8 -*-
"""主视觉背景板 —— 「用 DevCon 2026 主视觉做拍照区背景」的贴图实现。

素材在 ``app/assets/bg/``，主方向为手绘芯片底板；旧底板继续保留作内部候选：

* ``film``（默认）``devcon2026-keyvisual-film.png`` —— 深色芯片与电路统一手绘动画电影风。
* ``dark`` / ``light`` —— 旧版写实底板，不进入默认随机池。

两套的**版面是同一个**（左上标题 / 左下留白 / 右侧芯片），所以人像窗共用一组坐标。

默认固定使用 ``film``，避免同一场活动出现不同视觉方向。只有显式设置
``BOOTH_KV_RANDOM=1`` 才允许旧底板参与随机实验。

做法不是「画一张新图」，而是**把人贴进预留的位置**，其余版面一概不动。
标题字是**烙在底图里的**，代码这边一个字都不叠（成片里只有人，见 README §五）。

**两套底图的「光」不一样，人就不能用同一道调子**：暗调那套要把人往低照度拉，
亮调那套再把人的曝光压下去，人就比整个画面暗一档 —— 换了个方向的突兀。
所以每套底图在注册表里声明自己的 ``tone``，见 ``_tone_spec()``。

## 版式为什么是现在这样

底图的构图是「左上标题 / 左下留白 / 右侧芯片」，所以：

* **标题已经在上方占掉了 y 0..0.42**，人像窗只能往下走 ——
  窗口顶必须落在标题下缘（实测标题最低一行在 y≈0.42）之下。
* **右侧是芯片**（x>0.42 全是亮的），人像窗必须待在左栏。
* 于是窗口 = 左栏里一块宽高比 0.78 的区域，位置是**扫出来**的：
  在 x 0.03..0.40 / y 0.42..0.955 里搜「边缘能量 + 亮度标准差」最低的框，
  再用面积做一点加成（太小的框虽然更干净，但人会被缩没）。

**窗口宽高比必须等于 ``PORTRAIT_SIZE`` 的比例**：``_compose_cutout`` 是直接
``resize`` 到窗口尺寸的，比例不一致人就会被拉变形（坑 19）。所以窗口是
365x468 @ 0.7799，不是随便定的。

## 出图比例对不上怎么办

出图是 1.49:1，成片是 16:9，必须裁。**裁哪儿不能随便**：左上角是标题、
右下角是芯片，只有底部可以割。统一走 ``tools/make_keyvisual.py``：
按宽度铺满 → 顶部对齐 → 从下往上裁到 1600x900。烤好的底板就存在
``app/assets/bg/``，所以运行期不再需要对齐参数，``box`` 的归一化坐标
可以直接按 1600x900 量。
"""

from __future__ import annotations

import os
import random

import numpy as np
from PIL import Image, ImageDraw, ImageFilter

try:                       # 作为包导入（app.render.keyvisual）
    from . import watermark as wm
except ImportError:        # 作为平铺模块导入
    import watermark as wm  # type: ignore

_HERE = os.path.dirname(os.path.abspath(__file__))
_APP = os.path.normpath(os.path.join(_HERE, ".."))
BG_DIR = os.path.join(_APP, "assets", "bg")

# 每套底图的窗口都是**量出来**的，不是目测；宽高比统一取 0.78，与 PORTRAIT_SIZE
# 一致 —— 人贴进去不会横向或纵向拉变形。
KEYVISUALS = {
    "light-film-tech": {
        "file": "devcon2026-light-film-wide-1600x900.png",
        "vertical_file": "devcon2026-light-film-vertical.png",
        "native": (1600, 900),
        "vertical_native": (1024, 1536),
        "box": (0.04375, 0.1277778, 0.45000, 0.9111111),
        "vertical_box": (0.09766, 0.11068, 0.90234, 0.87240),
        "label": "晴空手绘",
        "label_en": "LIGHT FILM",
        "placeholder": False,
        "tone": "none",
        "local_brand": True,
        "public": True,
    },
    "film": {
        "file": "devcon2026-keyvisual-film.png",
        "native": (1600, 900),
        "box": (0.0150, 0.4000, 0.271670, 0.9850),
        "label": "手绘芯片主方向",
        "label_en": "FILM CHIP MAIN",
        "placeholder": False,
        "tone": "dark",
        "public": True,
    },
    "dark": {
        "file": "devcon2026-keyvisual-a.png",
        # 已经用 tools/make_keyvisual.py 烤成与成片同尺寸，这里就是底板本身
        "native": (1600, 900),
        # 左下留白里最干净的一块，`make_keyvisual.py --scan` 扫出来的
        # （窗内「有内容」像素 0 / 边缘 0.897 / 亮度std 1.95）
        # 窗口顶 y=400：标题最低一笔在 y=354，间隙 46px
        "box": (0.0150, 0.4000, 0.271670, 0.9850),
        "label": "暗调芯片",
        "label_en": "DARK",
        # 这套窗里**没有**浅灰人形占位 —— 别去盖（亮底素材会误判，见 _window_cover_mask）
        "placeholder": False,
        # 低照度冷调：人得跟着压一点（入景暗调）
        "tone": "dark",
        "public": False,
    },
    "light": {
        "file": "devcon2026-keyvisual-b.png",
        "native": (1600, 900),
        # 与 dark 共用同一个窗：两套的版面是同一个，人也该待在同一个位置
        "box": (0.0150, 0.4000, 0.271670, 0.9850),
        "label": "亮调芯片",
        "label_en": "LIGHT",
        "placeholder": False,
        # 底子本来就是亮的：人正常曝光才对，**不许再压暗**
        "tone": "none",
        "public": False,
    },
}
DEFAULT_KEYVISUAL = "light-film-tech"

# 默认那套的兼容常量（外部按旧名字读得到）
KV_NATIVE = KEYVISUALS[DEFAULT_KEYVISUAL]["native"]
BOX = KEYVISUALS[DEFAULT_KEYVISUAL]["box"]
KV_PATH = os.path.join(BG_DIR, KEYVISUALS[DEFAULT_KEYVISUAL]["file"])
WIDE = (1600, 900)
VERTICAL = (1024, 1536)
SIZES = (WIDE, VERTICAL)
PORTRAIT_SIZE = (780, 1000)                 # 与窗口 0.78 的宽高比一致

BRAND = (178, 228, 13)
INK = (243, 247, 248)
INK2 = (150, 170, 200)


def _envf(name: str, default) -> float:
    try:
        return float(os.environ.get(name, default))
    except Exception:                      # 环境变量写错不能让出图挂掉
        return float(default)


# --------------------------------------------------------------------------
# 入景暗调：把人从「办公室顶灯」放进「暗调棚拍」
#
# 现场的问题不是抠得不干净，是**光不对**：底板是暗调冷蓝 + 一缕暖金，
# 而人是办公室顶灯下的中性白 —— 直接贴上去像一张亮贴纸浮在画面上。
# 三条都很轻，目的是**同光**，不是「修图」—— 人还是他本人的颜色，
# 只是不再比整个画面亮一档。
#
# 只作用在**贴上去的人**：底板一个像素都不动。
# BOOTH_PERSON_TONE=0 可整条关掉；其余三个参数见 README §三。
# --------------------------------------------------------------------------
PERSON_TONE = os.environ.get("BOOTH_PERSON_TONE", "1") != "0"
PERSON_EXPOSURE = _envf("BOOTH_PERSON_EXPOSURE", 0.88)      # 曝光：1.0 = 不动
PERSON_SATURATION = _envf("BOOTH_PERSON_SATURATION", 0.94)  # 饱和：底板本身低饱和
PERSON_AMBIENT = _envf("BOOTH_PERSON_AMBIENT", 0.05)        # 环境光掺入量
PERSON_AMBIENT_RGB = (18, 33, 58)                           # 底板那边的冷色


def _tone_spec(name=None):
    """这套底图该把人往哪边拉；``None`` = 一点都不动。

    注册表里 ``tone="dark"`` 的（低照度冷调棚拍）走上面那三个参数；
    ``tone="none"`` 的（亮台面）**原样返回** —— 往亮底上贴一个被压暗的人，
    等于把人做成了剪影，那是另一种突兀。
    """
    if not PERSON_TONE or KEYVISUALS[resolve(name)].get("tone", "dark") == "none":
        return None
    return {"exposure": PERSON_EXPOSURE, "saturation": PERSON_SATURATION,
            "ambient": PERSON_AMBIENT, "ambient_rgb": PERSON_AMBIENT_RGB}


def _into_scene(img: Image.Image, tone=None) -> Image.Image:
    """把贴上去的人往底板的「光」里拉一把。``tone=None`` 原样返回。

    三步：压曝光 → 微去饱和 → 掺一点点环境冷色。
    都是乘法/线性混合，不动色相关系，不会把脸调成别的颜色。
    """
    if tone is None:
        return img
    a = np.asarray(img, np.float32) * tone["exposure"]
    if tone["saturation"] != 1.0:
        gray = (a @ np.array([0.299, 0.587, 0.114], np.float32))[..., None]
        a = gray + (a - gray) * tone["saturation"]
    if tone["ambient"] > 0.0:
        amb = np.array(tone["ambient_rgb"], np.float32)[None, None, :]
        a = a * (1.0 - tone["ambient"]) + amb * tone["ambient"]
    return Image.fromarray(np.clip(a, 0.0, 255.0).astype(np.uint8), "RGB")


# 没点名时是否随机挑一套。现场要的「两张随机用」。
# 设 BOOTH_KV_RANDOM=0 就固定用默认那套（同一晚的成片色调一致）。
RANDOM = os.environ.get("BOOTH_KV_RANDOM", "0") != "0"


def pick(name=None, chooser=None) -> str:
    """这一张用哪套底图。

    点名的按点名的来；**没点名**时才随机（在「素材存在」的几套里挑）——
    拍摄位按了某一套，就得按那一套出图，不能被随机顶掉。

    chooser 只是给自检用的注入点：不然「随机」这件事在自检里没法确定性地验 ——
    「跑一百次看看两套都出过没有」是个概率断言，会偶发地绿。
    """
    key = str(name or "").strip()
    if key:
        return resolve(key)
    if not RANDOM:
        return DEFAULT_KEYVISUAL
    pool = [k for k in names() if available(k)]
    if not pool:
        return DEFAULT_KEYVISUAL
    return (chooser or random.choice)(pool)


def names() -> tuple:
    """注册表里的底图名，默认那套排第一。"""
    return (DEFAULT_KEYVISUAL,) + tuple(k for k in KEYVISUALS if k != DEFAULT_KEYVISUAL)


def resolve(name) -> str:
    """把请求的底图名归一成注册表里的键。

    认不出来就退回默认 —— 底图名是前端传的，一个手滑的字符串不该让参会者拿不到照片。
    """
    key = str(name or "").strip()
    if key in KEYVISUALS:
        return key
    for k, spec in KEYVISUALS.items():
        if key and key in (spec.get("label"), spec.get("label_en"), spec.get("file")):
            return k
    return DEFAULT_KEYVISUAL


def catalog() -> list:
    """给前端 / 状态页的清单：顺序固定，带「素材在不在」，缺的照样列出来。"""
    return [{"name": k, "label": KEYVISUALS[k]["label"],
             "label_en": KEYVISUALS[k]["label_en"],
             "default": k == DEFAULT_KEYVISUAL,
             "present": available(k),
             "orientations": [orientation for orientation, size in
                              (("wide", WIDE), ("vertical", VERTICAL))
                              if available(k, size)]}
            for k in names() if KEYVISUALS[k].get("public", True)]


def path_of(name=None, size=WIDE) -> str:
    spec = KEYVISUALS[resolve(name)]
    key = "vertical_file" if tuple(size) == VERTICAL and spec.get("vertical_file") else "file"
    return os.path.join(BG_DIR, spec[key])


def available(name=None, size=WIDE) -> bool:
    return os.path.isfile(path_of(name, size))


def _kv_source(name=None, size=WIDE):
    """返回 (图像, 人像窗归一化坐标)。素材分辨率换了也照样对得上。"""
    path = path_of(name, size)
    if not os.path.isfile(path):
        raise FileNotFoundError("主视觉缺失：%s" % path)
    img = Image.open(path).convert("RGB")
    spec = KEYVISUALS[resolve(name)]
    box = spec.get("vertical_box", spec["box"]) if tuple(size) == VERTICAL else spec["box"]
    return img, box


def _cover(img: Image.Image, size: tuple[int, int], align_x: float = 0.5,
           align_y: float = 0.5) -> Image.Image:
    """按 cover 缩放并裁到 size；align_* 决定裁哪边（0=靠左/上，1=靠右/下）。"""
    target_w, target_h = size
    src_w, src_h = img.size
    scale = max(target_w / src_w, target_h / src_h)
    scaled = img.resize((max(1, round(src_w * scale)), max(1, round(src_h * scale))), Image.LANCZOS)
    left = int(round((scaled.size[0] - target_w) * align_x))
    top = int(round((scaled.size[1] - target_h) * align_y))
    return scaled.crop((left, top, left + target_w, top + target_h))


def _rounded_mask(size: tuple[int, int], radius: float) -> Image.Image:
    mask = Image.new("L", size, 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, size[0] - 1, size[1] - 1],
                                           radius=max(0, int(round(radius))), fill=255)
    return mask


def _as_portrait(portrait) -> Image.Image:
    if portrait is None:
        return Image.new("RGB", (800, 1000), (20, 24, 32))
    if isinstance(portrait, np.ndarray):
        portrait = Image.fromarray(np.clip(portrait, 0, 255).astype(np.uint8))
    return portrait.convert("RGB")


def box_pixels(size: tuple[int, int], scale: float, offset: tuple[float, float],
               box=None, native=None) -> tuple[int, int, int, int]:
    """把归一化窗口换算成输出图上的像素框。

    native 要传**素材的实际尺寸**，不是注册表里写的那个：素材换版本时
    scale 是按实际尺寸算的，窗口再拿注册表尺寸去乘就对不上了。
    """
    box = BOX if box is None else box
    native = KV_NATIVE if native is None else native
    x0 = box[0] * native[0] * scale + offset[0]
    y0 = box[1] * native[1] * scale + offset[1]
    x1 = box[2] * native[0] * scale + offset[0]
    y1 = box[3] * native[1] * scale + offset[1]
    return (int(round(x0)), int(round(y0)), int(round(x1)), int(round(y1)))


# 窗内**不再画任何东西**：成片就是官方主视觉这一张，人贴在预留窗口里。
# 早期这里还画姓名牌（姓名 / 编号 / 风格 / RISC-V 助记符 / 话题标签 / 由下往上渐隐的暗底）
# 和右上角的算力徽章。现场判定是多余的「加框加字」，已全部去掉 —— 保留说明是为了让
# 后来的人知道这块曾经存在过、以及为什么没了。

# 窗里如果有「素材自己画的浅灰人形占位」（官方主视觉就是），得先盖掉，否则人的四周
# 会露一圈浅灰。**盖哪一块不靠猜 inset**：占位是中性灰（R≈G≈B 且亮），窗框是品牌蓝
# （B 远高于 R），拿亮度 + 色度一卡就分开了 —— 素材换版本也不用重新量。
# 没有占位的那套（芯片特写）会自动什么都不画，不会凭空多出一块矩形。
WINDOW_FILL_TOP = (14, 22, 40)
WINDOW_FILL_BOTTOM = (4, 6, 12)
WINDOW_LIGHT_T = 150.0          # 亮过这条才可能是占位
WINDOW_CHROMA_T = 60.0          # 色度低于这条才算「中性灰」（品牌蓝的色度 >150）


# 窗边柔化的宽度（占窗口宽 / 高的比例）：左 / 上 / 右 / 下。
# 底面留得最宽 —— 人本来就是被画面下沿截断的，那里淡出去最自然。
# 全 0 = 关掉（回到「人贴着窗框、切面看得见」的老样子）。
def _read_feather() -> tuple:
    raw = os.environ.get("BOOTH_KV_FEATHER", "0.10,0.06,0.10,0.16")
    try:
        vals = tuple(max(0.0, float(v)) for v in str(raw).split(","))
    except ValueError:
        vals = ()
    return vals if len(vals) == 4 else (0.10, 0.06, 0.10, 0.16)


WINDOW_FEATHER = _read_feather()


def _window_cover_mask(base: Image.Image, rect, name=None):
    """窗内需要盖住的区域。**只有素材自己画了浅灰人形占位时才盖。**

    这里原来是一段纯启发式：窗内「中性灰且亮」的像素就盖。换成亮底素材之后它
    立刻误伤 —— 亮底整块都「中性灰且亮」，于是往窗里糊了一块深色矩形。在暗底上
    看不出来，在亮底上就是一块明晃晃的黑方块（坑 20 的复发）。

    所以改成**由注册表里那句 ``placeholder`` 说了算**：启发式只在声明「有占位」时
    用来定位占位在哪。素材换版本也不会把「有没有占位」这件事猜错。
    """
    if not KEYVISUALS[resolve(name)].get("placeholder", False):
        return None
    crop = np.asarray(base.crop(rect), np.float32)
    luma = crop[..., 0] * 0.299 + crop[..., 1] * 0.587 + crop[..., 2] * 0.114
    chroma = crop.max(2) - crop.min(2)
    mask = (luma > WINDOW_LIGHT_T) & (chroma < WINDOW_CHROMA_T)
    if float(mask.mean()) < 0.02:            # 没有占位
        return None
    art = Image.fromarray((mask * 255).astype(np.uint8), "L")
    # 涨一圈再羽化：占位与蓝框之间的抗锯齿像素是「灰蓝之间」的颜色，不涨一圈会留一道细缝
    return art.filter(ImageFilter.MaxFilter(3)).filter(ImageFilter.GaussianBlur(0.6))


def _window_fill(size: tuple[int, int]) -> Image.Image:
    """盖占位用的暗底：一道极轻的上下渐变，**不带任何纹理**。

    起先这块是「径向光 + 网格」的画法，看着像往窗里贴了一张卡片 —— 纹理会把
    「盖住的区域」变成「看得见的一个矩形」。现在只留渐变，边界就是窗框本身。
    """
    w, h = int(size[0]), int(size[1])
    yy = np.mgrid[0:h, 0:w][0].astype(np.float32)
    k = np.clip(1.0 - (yy / max(1.0, h - 1)) * 0.62, 0.38, 1.0)[..., None]
    top = np.array(WINDOW_FILL_TOP, np.float32)[None, None, :]
    bottom = np.array(WINDOW_FILL_BOTTOM, np.float32)[None, None, :]
    base = bottom + (top - bottom) * k
    return Image.fromarray(np.clip(base, 0, 255).astype(np.uint8), "RGB")


def _window_feather(size, feather=None) -> np.ndarray:
    """窗口四周的柔化权重 0..1（中间恒为 1，四边淡到 0）。

    为什么需要它：取景框是**矩形**，人是顺着轮廓的。人的肩膀往往比窗还宽
    （实测一张站姿抓拍：肩膀 1105px，窗只有 842px），于是两侧被切成直边，
    底边也被切成直边 —— 远处一看就是「人像背后贴了一块矩形」。四边淡出之后，
    人的轮廓是淡进背景的，没有一条直线。
    """
    w, h = int(size[0]), int(size[1])
    fl, ft, fr, fb = WINDOW_FEATHER if feather is None else feather
    yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)
    m = np.minimum(
        np.minimum(np.clip((xx + 0.5) / max(1.0, w * fl), 0.0, 1.0),
                   np.clip((w - 0.5 - xx) / max(1.0, w * fr), 0.0, 1.0)),
        np.minimum(np.clip((yy + 0.5) / max(1.0, h * ft), 0.0, 1.0),
                   np.clip((h - 0.5 - yy) / max(1.0, h * fb), 0.0, 1.0)))
    return (m * m * (3.0 - 2.0 * m)).astype(np.float32)      # smoothstep，不带硬拐点


def _rim_light(soft: Image.Image, strength: float = 0.62) -> Image.Image:
    """历史轮廓光实现，仅保留给旧样片工具兼容；正式合成不再调用。"""
    inner = np.asarray(soft, np.float32) / 255.0
    blurred = soft.filter(ImageFilter.GaussianBlur(2.4))
    halo = np.asarray(blurred, np.float32) / 255.0
    rim = np.clip(halo - inner * 0.52, 0.0, 1.0) * float(strength)
    return Image.fromarray((rim * 255).astype(np.uint8), "L")


def _window_geom(size=WIDE, kv_name=None):
    """人像窗的几何：``(铺好的底图, 窗口在成片里的像素矩形, 底图→成片的缩放)``。

    三个地方要这几个数（``_compose_cutout`` / ``_compose_wide`` / ``self_test``），
    所以只算一份 —— 抄成三份就会分岔（坑 23）。
    """
    kv, kbox = _kv_source(kv_name, size)
    scale = max(size[0] / kv.size[0], size[1] / kv.size[1])
    offset = ((size[0] - kv.size[0] * scale) / 2.0, (size[1] - kv.size[1] * scale) / 2.0)
    return kv, box_pixels(size, scale, offset, box=kbox, native=kv.size), scale


def window_rect(size=WIDE, kv_name=None):
    """人像窗在成片里的像素矩形 ``(左, 上, 右, 下)``。"""
    return _window_geom(size, kv_name)[1]


def _fit_cutout(cutout, size: tuple[int, int]):
    """按 alpha 有效范围等比缩放，返回人物图、蒙版和居中贴图坐标。"""
    rgb, alpha = cutout
    person = Image.fromarray(np.clip(np.asarray(rgb), 0, 255).astype(np.uint8), "RGB")
    alpha_arr = np.asarray(alpha, np.float32)
    if alpha_arr.max(initial=0.0) > 1.0:
        alpha_arr /= 255.0
    alpha_arr = np.clip(alpha_arr, 0.0, 1.0)
    active = np.argwhere(alpha_arr > 0.01)
    if not active.size:
        return None
    y0, x0 = active.min(axis=0)
    y1, x1 = active.max(axis=0) + 1
    person = person.crop((int(x0), int(y0), int(x1), int(y1)))
    soft = Image.fromarray((alpha_arr[y0:y1, x0:x1] * 255).astype(np.uint8), "L")
    scale = min(size[0] / person.width, size[1] / person.height)
    fitted = (max(1, int(round(person.width * scale))),
              max(1, int(round(person.height * scale))))
    person = person.resize(fitted, Image.LANCZOS)
    soft = soft.resize(fitted, Image.BILINEAR)
    soft = soft.filter(ImageFilter.MinFilter(3)).filter(ImageFilter.GaussianBlur(0.45))
    return person, soft, ((size[0] - fitted[0]) // 2, size[1] - fitted[1])


def _window_inner(rect, scale):
    """人像实际可贴的范围：窗口四边各收一点（_compose_cutout 与 cutout_mask 共用）。"""
    inset = max(2, int(round(scale * 6)))
    return (rect[0] + inset, rect[1] + inset, rect[2] - inset, rect[3] - inset)


def cutout_mask(cutout, kv_name=None, size=WIDE) -> Image.Image:
    """_compose_cutout 把人贴在哪：同一套几何得到的成片坐标人像蒙版（给品牌字避让用）。"""
    _kv, rect, scale = _window_geom(size, kv_name)
    inner = _window_inner(rect, scale)
    mask = Image.new("L", (int(size[0]), int(size[1])), 0)
    fitted = _fit_cutout(cutout, (inner[2] - inner[0], inner[3] - inner[1]))
    if fitted is not None:
        _person, soft, offset = fitted
        mask.paste(soft, (inner[0] + offset[0], inner[1] + offset[1]))
    return mask


def _compose_cutout(cutout, natural: bool = False, kv_name=None, size=WIDE) -> Image.Image:
    """把人像按 alpha 贴进底图预留窗 —— 只把人带进来，不带矩形底。"""
    kv, rect, scale = _window_geom(size, kv_name)
    base = _cover(kv, size, align_x=0.5, align_y=0.5)
    inner = _window_inner(rect, scale)
    window_size = (inner[2] - inner[0], inner[3] - inner[1])

    cover = _window_cover_mask(base, rect, kv_name)
    if cover is not None:
        base.paste(_window_fill((rect[2] - rect[0], rect[3] - rect[1])),
                   (rect[0], rect[1]), cover)

    fitted = _fit_cutout(cutout, window_size)
    if fitted is None:
        return base
    person, soft, offset = fitted
    person = _into_scene(person, _tone_spec(kv_name))
    base.paste(person, (inner[0] + offset[0], inner[1] + offset[1]), soft)
    return base


def _compose_wide(portrait: Image.Image, kv_name=None, size=WIDE) -> Image.Image:
    kv, rect, scale = _window_geom(size, kv_name)
    base = _cover(kv, size, align_x=0.5, align_y=0.5)
    inset = max(2, int(round(scale * 6)))              # 让官方蓝色描边留在外面
    inner = (rect[0] + inset, rect[1] + inset, rect[2] - inset, rect[3] - inset)
    size = (inner[2] - inner[0], inner[3] - inner[1])
    cover = _window_cover_mask(base, rect, kv_name)
    if cover is not None:
        base.paste(_window_fill((rect[2] - rect[0], rect[3] - rect[1])),
                   (rect[0], rect[1]), cover)
    radius = 0.085 * size[0]
    face = _into_scene(_cover(portrait, size, align_y=0.10), _tone_spec(kv_name))
    base.paste(face, (inner[0], inner[1]), _rounded_mask(size, radius))
    return base


def _draw_local_text(img: Image.Image, *, record_id, name: str, handle: str,
                     kv_name: str, subject_mask=None, subject_margin: int = 0) -> Image.Image:
    if not KEYVISUALS[kv_name].get("local_brand"):
        return img
    return _draw_event_identity(img, name=name, handle=handle,
                                subject_mask=subject_mask, subject_margin=subject_margin)


def _scene_ink(img: Image.Image, box) -> tuple:
    sample = np.asarray(img.crop(box).resize((1, 1)), np.float32)[0, 0]
    luma = float(sample @ np.array([0.299, 0.587, 0.114], np.float32))
    if luma > 145:
        return (27, 35, 48, 235), (255, 255, 255, 125)
    return (248, 249, 246, 245), (10, 16, 28, 145)


def _draw_generated_text(img: Image.Image, *, record_id, name: str, handle: str,
                         subject_mask=None, subject_margin: int = 0) -> Image.Image:
    return _draw_event_identity(img, name=name, handle=handle,
                                subject_mask=subject_mask, subject_margin=subject_margin)


BRAND_TITLE = "SpacemiT DevCon 2026"
BRAND_SCALE = 1.0
BRAND_MIN_SCALE = 0.65
BRAND_LEFT = 138
BRAND_LOGO_SIZE = 44
BRAND_TITLE_SIZE = 33
BRAND_GAP = 16
BRAND_CENTER_Y = 132
BRAND_CLEARANCE = 28        # 锁定组底边与人像顶边至少留的距离（样稿像素）
SOLO_HANDLE_SIZE = 40       # 只填了社交账号时它就是唯一一行，比两行版的 27 大一档
TITLE_INK = (29, 43, 49, 255)          # 样稿标题深墨（亮底）
TITLE_INK_ON_DARK = (246, 248, 244, 255)
TITLE_INK_SWITCH = 120      # 标题背后平均亮度低于它就换浅色：两种字色对比度相等的点（WCAG 算法）


def _title_ink(img: Image.Image, rect) -> tuple:
    """标题字色跟着底色走：亮底沿用样稿深墨，暗底（如美漫蓝底）换浅色，保证看得清。"""
    box = (max(0, int(rect[0])), max(0, int(rect[1])),
           min(img.width, int(rect[2])), min(img.height, int(rect[3])))
    if box[2] <= box[0] or box[3] <= box[1]:
        return TITLE_INK
    luma = float(np.asarray(img.convert("L").crop(box), np.float32).mean())
    return TITLE_INK if luma >= TITLE_INK_SWITCH else TITLE_INK_ON_DARK


def _subject_tops(mask, size) -> np.ndarray | None:
    """人像掩码 → 每一列最靠上的人像像素行号；整列没有人像记为画布高。"""
    if mask is None:
        return None
    size = (int(size[0]), int(size[1]))
    gray = mask.convert("L")
    if gray.size != size:
        gray = gray.resize(size, Image.Resampling.BILINEAR)
    solid = np.asarray(gray) >= 96
    if not solid.any():
        return None
    return np.where(solid.any(axis=0), solid.argmax(axis=0), size[1])


def _brand_title_xy(px, logo_width: int, title_size: int) -> tuple[int, int]:
    bounds = wm.load_font(title_size).getbbox(BRAND_TITLE, anchor="ls")
    gap = max(1, round(BRAND_GAP * title_size / BRAND_TITLE_SIZE))
    left = px(BRAND_LEFT) + logo_width + gap - bounds[0]
    baseline = round(px(BRAND_CENTER_Y) - (bounds[1] + bounds[3]) / 2)
    return left, baseline


def _brand_layout(px, tops, clearance: int) -> tuple[int, int, int]:
    """顶部锁定组用多大、放多高：返回 (logo 高, 标题字号, logo 顶)。"""
    probe = ImageDraw.Draw(Image.new("L", (1, 1)))
    left = px(BRAND_LEFT)

    def place(s: float):
        logo_h = max(1, px(BRAND_LOGO_SIZE * s))
        title_size = max(6, px(BRAND_TITLE_SIZE * s))
        logo_top = px(BRAND_CENTER_Y) - logo_h // 2
        logo_w = wm.logo_glyph(logo_h).width
        title_xy = _brand_title_xy(px, logo_w, title_size)
        x0, y0, x1, y1 = probe.textbbox(title_xy, BRAND_TITLE,
                                        font=wm.load_font(title_size), anchor="ls")
        rect = (left, min(logo_top, y0), max(left + logo_w, x1), max(logo_top + logo_h, y1))
        return logo_h, title_size, logo_top, rect

    def below(rect) -> int:
        lo, hi = max(0, rect[0] - px(12)), min(len(tops), rect[2] + px(12))
        return int(tops[lo:hi].min()) if hi > lo else 1 << 30

    if tops is not None:
        s = BRAND_SCALE
        while s > BRAND_MIN_SCALE + 1e-6:
            logo_h, title_size, logo_top, rect = place(s)
            if rect[3] + clearance <= below(rect):
                return logo_h, title_size, logo_top
            s = round(s - 0.05, 2)
        return place(BRAND_MIN_SCALE)[:3]
    return place(BRAND_SCALE)[:3]


def _draw_event_identity(img: Image.Image, *, name: str, handle: str,
                         subject_mask=None, subject_margin: int = 0) -> Image.Image:
    """按确认样稿绘制大会标识和个人信息。

    竖版坐标以 1024x1536 样稿为基准，Logo、标题和底部签名保持同一组比例；
    横版只做等比例缩放，避免两条出图路径再次出现两套字级和 Logo。
    """
    canvas_w, canvas_h = img.size
    base_w, base_h = VERTICAL
    scale = min(canvas_w / base_w, canvas_h / base_h)
    if img.size == VERTICAL:
        scale = 1.0

    def px(value):
        return int(round(value * scale))

    overlay = img.convert("RGBA")
    gradient = np.zeros((canvas_h, canvas_w, 4), dtype=np.uint8)
    gradient[:, :, :3] = (12, 20, 28)
    for row in range(canvas_h):
        position = min(1.0, max(0.0, (row / max(1, canvas_h) - 0.66) / 0.27))
        gradient[row, :, 3] = round(172 * position * position * (3 - 2 * position))
    overlay.alpha_composite(Image.fromarray(gradient, "RGBA"))

    logo_size, title_size, group_top = _brand_layout(
        px, _subject_tops(subject_mask, img.size), px(BRAND_CLEARANCE + subject_margin))
    logo = wm.logo_glyph(logo_size)
    group_left = px(BRAND_LEFT)
    overlay.alpha_composite(logo, (group_left, group_top))
    draw = ImageDraw.Draw(overlay, "RGBA")
    title_font = wm.load_font(title_size)
    title_xy = _brand_title_xy(px, logo.width, title_size)
    draw.text(title_xy, BRAND_TITLE, font=title_font,
              fill=_title_ink(img, draw.textbbox(title_xy, BRAND_TITLE, font=title_font, anchor="ls")),
              anchor="ls")

    signature_left = px(142)
    name_text, handle_text = name.strip(), handle.strip()
    if name_text or handle_text:
        bar_top, bar_bottom = px(1187), px(1292)
        draw.rounded_rectangle((signature_left, bar_top, signature_left + px(5), bar_bottom),
                               radius=max(1, px(2)), fill=(178, 228, 13, 255))
    if name_text and handle_text:
        name_font = wm.fit_font(draw, name_text, px(650), px(55), min_size=max(12, px(22)))
        draw.text((signature_left + px(24), px(1177)), name_text,
                  font=name_font, fill=(250, 251, 247, 255), anchor="lt")
        handle_font = wm.fit_font(draw, handle_text, px(650), px(27),
                                  min_size=max(10, px(17)))
        draw.text((signature_left + px(26), px(1254)), handle_text,
                  font=handle_font, fill=(223, 232, 228, 255), anchor="lt")
    elif name_text or handle_text:
        # 只填了一项：这一行的字面（墨迹）上下居中到竖线中点
        if name_text:
            text, x, fill = name_text, signature_left + px(24), (250, 251, 247, 255)
            font = wm.fit_font(draw, text, px(650), px(55), min_size=max(12, px(22)))
        else:
            text, x, fill = handle_text, signature_left + px(26), (223, 232, 228, 255)
            font = wm.fit_font(draw, text, px(650), px(SOLO_HANDLE_SIZE),
                               min_size=max(10, px(17)))
        ink_top, ink_bottom = draw.textbbox((x, 0), text, font=font, anchor="ls")[1::2]
        baseline = (bar_top + bar_bottom) / 2 - (ink_top + ink_bottom) / 2
        draw.text((x, round(baseline)), text, font=font, fill=fill, anchor="ls")
    return overlay.convert("RGB")


def compose_generated(scene, *, record_id, name: str = "", handle: str = "",
                      size=WIDE, mock: bool = False,
                      include_event_text: bool = True, subject_mask=None) -> Image.Image:
    size = (int(size[0]), int(size[1]))
    if size not in SIZES:
        raise ValueError("generated scene size only accepts %s (got %s)" % (SIZES, size))
    img = _cover(_as_portrait(scene), size, align_x=0.5, align_y=0.5)
    if include_event_text:
        img = _draw_generated_text(img, record_id=record_id, name=name, handle=handle,
                                   subject_mask=subject_mask)
    if mock:
        _mock_chip(img)
    return img


def compose(portrait, *, record_id, name: str = "", handle: str = "", style_label: str = "",
            mnemonic: str = "", ep: bool = True, ep_threads=None, infer_ms=None,
            person_count: int = 0, size=WIDE, seed: int = 0, mock: bool = False,
            cutout=None, natural: bool = False, keyvisual=None,
            include_event_text: bool = True) -> Image.Image:
    """把风格化人像贴进官方主视觉的人像窗，返回成片。

    name / handle / style_label / mnemonic / record_id / ep / ep_threads / infer_ms /
    person_count 这些参数**保留签名但已经不参与构图** —— 窗内不再叠任何文字或徽章，
    调用方（booth_server）照旧传即可。哪天真要在窗内加东西，实名信息从这里进来。

    cutout: ``(RGB 图, alpha 数组)``。给了就按 alpha 把人抠着贴进去（只有人，没有框）；
    没给（抠像失败 / 没有模型）就退回「整幅取景」的老路 —— 出图不能断。
    natural: 兼容旧调用方；所有风格都不再添加轮廓光。
    keyvisual: 用哪套底图（``KEYVISUALS`` 的键）；认不出来退回默认，不抛异常。
    """
    size = (int(size[0]), int(size[1]))
    if size not in SIZES:
        raise ValueError("keyvisual size only accepts %s (got %s)" % (SIZES, size))
    portrait = _as_portrait(portrait)
    kv_name = resolve(keyvisual)
    img = (_compose_cutout(cutout, natural=natural, kv_name=kv_name, size=size)
           if cutout is not None else _compose_wide(portrait, kv_name=kv_name, size=size))
    if include_event_text:
        img = _draw_local_text(img, record_id=record_id, name=name, handle=handle,
                               kv_name=kv_name)
    if mock:
        # 开发用合成源的成片必须一眼看出不是真人拍的 —— 否则这种图流出去就是事故
        _mock_chip(img)
    return img


def _mock_chip(img: Image.Image) -> None:
    """左上角钉一个琥珀色 MOCK 标签。"""
    draw = ImageDraw.Draw(img)
    font = wm.load_font(22)
    text = "MOCK · 合成源"
    w = draw.textlength(text, font=font)
    draw.rounded_rectangle([18, 18, 18 + w + 28, 62], radius=12,
                           fill=(26, 18, 4), outline=(255, 176, 32), width=2)
    draw.text((32, 30), text, font=font, fill=(255, 176, 32))


def _tone_probe() -> dict:
    """入景暗调（``_into_scene``）：压的是贴上去的人，**不是底板**。

    两件事都要能量出来，光看「人变暗了」不够：

    - 压了多少（``tone_ratio``）—— 0.80 以下是把人压成剪影，0.98 以上等于没做；
    - 底板动没动（``tone_base_untouched``）—— 窗口以外必须**一个像素都不变**。
      混了就成了「把整幅画调暗」，标题会跟着发灰，那是另一个功能。
    """
    global PERSON_TONE
    w, h = PORTRAIT_SIZE
    face = np.zeros((h, w, 3), np.float32)
    face[..., 0], face[..., 1], face[..., 2] = 204.0, 176.0, 152.0     # 一块中性「脸」
    yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)
    alpha = (((xx - w * 0.5) ** 2 + (yy - h * 0.58) ** 2)
             < (min(w, h) * 0.30) ** 2).astype(np.float32)
    plate = Image.fromarray(face.astype(np.uint8), "RGB")
    cutout = (plate, alpha)
    W3 = np.array([0.299, 0.587, 0.114], np.float32)

    saved = PERSON_TONE
    try:
        PERSON_TONE = False
        plain = float((np.asarray(_into_scene(plate), np.float32) @ W3).mean())
        off = np.asarray(_compose_cutout(cutout, natural=True), np.float32)
        PERSON_TONE = True
        toned = float((np.asarray(_into_scene(plate, _tone_spec()), np.float32) @ W3).mean())
        on = np.asarray(_compose_cutout(cutout, natural=True), np.float32)
    finally:
        PERSON_TONE = saved

    out = {"tone_off_luma": round(plain, 2), "tone_on_luma": round(toned, 2),
           "tone_ratio": round(toned / plain, 4) if plain else 0.0}
    out["tone_ok"] = bool(0.80 <= out["tone_ratio"] <= 0.98)
    x0, y0, x1, y1 = window_rect(WIDE)
    diff = np.abs(on - off).max(axis=2)
    outside = np.ones(diff.shape, bool)
    outside[y0:y1, x0:x1] = False
    out["tone_outside_max"] = round(float(diff[outside].max()), 4)
    out["tone_inside_max"] = round(float(diff[~outside].max()), 4)
    out["tone_base_untouched"] = bool(out["tone_outside_max"] == 0.0)

    # 随机挑底图：注入一个确定的 chooser 走一遍，两套都得能被选中
    pool = [k for k in names() if available(k)]
    out["pick_pool"] = pool
    out["pick_explicit_ok"] = bool(
        all(pick(k) == k for k in names()) and pick("不存在的名字") == DEFAULT_KEYVISUAL)
    if RANDOM and pool:
        seen = {pick(None, chooser=lambda seq, i=i: seq[i % len(seq)])
                for i in range(len(pool))}
        out["pick_covers_all"] = bool(seen == set(pool))
    else:
        out["pick_covers_all"] = bool(pick(None) == DEFAULT_KEYVISUAL)

    # 声明「亮底、不压暗」的那几套，必须真的一个像素都不动 —— 拿错调子会把人做成剪影
    quiet = [k for k in names() if KEYVISUALS[k].get("tone") == "none"]
    out["tone_quiet_boards"] = quiet
    out["tone_quiet_untouched"] = all(
        np.array_equal(np.asarray(_into_scene(plate, _tone_spec(k))), np.asarray(plate))
        for k in quiet)
    return out


def self_test() -> dict:
    """无依赖自检：每套底图素材在不在、能不能出图、窗口有没有落在画布里。

    缺一套不该拖垮另一套：**默认那套**缺了才算失败，其他缺了如实记下来。
    """
    out: dict = {"default": DEFAULT_KEYVISUAL, "keyvisuals": []}
    demo = Image.new("RGB", PORTRAIT_SIZE, (30, 34, 42))
    d = ImageDraw.Draw(demo)
    d.ellipse([260, 160, 520, 420], fill=(120, 130, 150))
    d.rectangle([300, 430, 480, 980], fill=(110, 120, 140))
    ft = _window_feather((100, 128))
    out["feather_edge"] = round(float(max(ft[0].max(), ft[-1].max(), ft[:, 0].max(),
                                          ft[:, -1].max())), 4)
    out["feather_core"] = round(float(ft[64, 50]), 4)
    out["feather_ok"] = bool(out["feather_edge"] <= 0.03 and out["feather_core"] >= 0.99)
    out.update(_tone_probe())

    all_ok = True
    for name in names():
        entry: dict = {"name": name, "path": path_of(name), "present": available(name),
                       "steps": []}
        if not entry["present"]:
            entry["ok"] = name != DEFAULT_KEYVISUAL
            entry["note"] = "素材缺失"
        else:
            _kv, _kbox = _kv_source(name)      # 顺带确认这套素材真能打开
            for size in SIZES:
                img = compose(demo, record_id="SM-2026-0001", name="张伟 ZHANG WEI",
                              handle="@zhangwei", style_label="高对比", mnemonic="vsetvli",
                              ep=True, infer_ms=205.4, person_count=1, size=size,
                              keyvisual=name)
                arr = np.asarray(img, np.float32)
                entry["steps"].append({"size": size, "mean": round(float(arr.mean()), 2),
                                       "ok": img.size == size})
            if WIDE in SIZES:
                rect = window_rect(WIDE, name)
                entry["window"] = rect
                entry["window_in_canvas"] = (rect[0] >= 0 and rect[1] >= 0
                                             and rect[2] <= WIDE[0] and rect[3] <= WIDE[1])
            entry["ok"] = (all(s["ok"] for s in entry["steps"])
                           and entry.get("window_in_canvas", False))
        all_ok = all_ok and entry["ok"]
        out["keyvisuals"].append(entry)
    out["ok"] = (all_ok and out["feather_ok"] and out["tone_ok"]
                 and out["tone_base_untouched"] and out["tone_quiet_untouched"]
                 and out["pick_covers_all"] and out["pick_explicit_ok"])
    return out


if __name__ == "__main__":
    import json
    print(json.dumps(self_test(), ensure_ascii=False, indent=2))
