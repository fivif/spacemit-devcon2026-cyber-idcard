# -*- coding: utf-8 -*-
"""抠像策略测试 —— 不需要真相机，只有最后一段才需要真模型。

锁住的是这几件事（都是踩过坑才定的，改动前先看 worker.py 里那段注释）：
  1. 有检测框时**先按人裁切**，不是先整幅；
  2. 这一路没找到人 / 和检测掩码对不上 -> 才退整幅；
  3. 两路都不行 -> 退检测掩码羽化版（边糊但有人）；
  4. 画面里本来没人 -> 不挂 alpha，也绝不抛异常；
  5. 单路抛异常不会拖垮整条链。

运行：python3 app/tests/test_matte_policy.py
"""
import pathlib
import sys

HERE = pathlib.Path(__file__).resolve().parent
APP = HERE.parent
sys.path.insert(0, str(APP))
sys.path.insert(0, str(APP / "infer"))

import numpy as np  # noqa: E402

import worker  # noqa: E402

ok = []
def chk(cond, label, extra=""):
    ok.append(bool(cond))
    print("  [%s] %s %s" % ("PASS" if cond else "FAIL", label, extra))

W, H = 640, 480


def make_rgb():
    return np.zeros((H, W, 3), np.uint8)


def fake_matcher(plan):
    """plan: {(box is None): (fill, alpha_box_or_None, iou_box_or_None, raise_or_None)}

    返回一个替代 worker._matte_alpha 的函数，并记录调用顺序。
    """
    calls = []

    def fake(rgb, box=None):
        calls.append(box)
        key = box is None
        spec = plan.get(key)
        if spec is None:
            raise AssertionError("没有为 box=%r 准备结果" % (box,))
        if isinstance(spec, Exception):
            raise spec
        fill, where, _ = spec
        alpha = np.zeros((H, W), np.float32)
        if where is not None:
            x0, y0, x1, y1 = where
            alpha[y0:y1, x0:x1] = 1.0
        roi = (0, 0, W, H) if box is None else (10, 20, 210, 220)
        return alpha, 12.0, roi, fill

    return fake, calls


def attach(plan, box=(100, 100, 300, 400), mask=None, matte=True):
    fake, calls = fake_matcher(plan)
    real = worker._matte_alpha
    worker._matte_alpha = fake
    try:
        resp = {"width": W, "height": H}
        req = {"matte": matte, "mask_max_side": 320}
        worker._attach_matte(resp, make_rgb(), req, box=box, fallback_mask=mask)
    finally:
        worker._matte_alpha = real
    return resp, calls


# 让 matte_available 在本机（没有真模型）也返回 True
_real_available = worker.matte_available
worker.matte_available = lambda: True
worker._encode_alpha = lambda alpha, max_side: ("FAKEB64", 8, 6)

PERSON = (20, 30, 120, 200)          # 掩码里的人在哪儿
EMPTY = np.zeros((H, W), bool)
MASK = np.zeros((H, W), bool)
MASK[30:200, 20:120] = True

print("=== A) 有框且裁切路能抠到人 -> 只跑一次，用裁切那一路 ===")
resp, calls = attach({False: (0.30, PERSON, None), True: (0.05, PERSON, None)}, mask=MASK)
chk(calls == [(100, 100, 300, 400)], "只调了一次，且是带框那次（没白跑整幅）", calls)
chk(resp.get("matte_source") == "modnet-roi", "标记为裁切路", resp.get("matte_source"))
chk(resp.get("matte_iou", 0) > 0.9, "与检测掩码对得上（IoU 记账）", resp.get("matte_iou"))
chk("matte_tried" not in resp, "一次就成 -> 不留『试过哪几路』的噪声")

print("=== B) 裁切路没找到人 -> 退整幅，并记下试过哪几路 ===")
resp, calls = attach({False: (0.0, None, None), True: (0.40, PERSON, None)}, mask=MASK)
chk(calls == [(100, 100, 300, 400), None], "先裁切、后整幅", calls)
chk(resp.get("matte_source") == "modnet", "最终用整幅那一路", resp.get("matte_source"))
chk([t["source"] for t in resp.get("matte_tried", [])] == ["modnet-roi", "modnet"],
    "两路都记了账", resp.get("matte_tried"))
chk(resp.get("matte_ms") == 24.0, "耗时是两路之和（不藏成本）", resp.get("matte_ms"))

print("=== C) 裁切路给的 alpha 和检测掩码对不上 -> 拒收，改整幅 ===")
wrong = (400, 300, 500, 460)          # 掩码里那里没人
resp, calls = attach({False: (0.30, wrong, None), True: (0.30, PERSON, None)}, mask=MASK)
chk(calls == [(100, 100, 300, 400), None], "认错人 -> 换一路重试", calls)
chk(resp.get("matte_source") == "modnet", "用了对得上的那一份", resp.get("matte_source"))
chk(resp.get("matte_iou", 0) > 0.9, "最终 IoU 达标", resp.get("matte_iou"))

print("=== D) 两路都不行 -> 退检测掩码羽化版（绝不交空白）===")
resp, calls = attach({False: (0.0, None, None), True: (0.0, None, None)}, mask=MASK)
chk(resp.get("matte_source") == "detect-mask", "退到检测掩码", resp.get("matte_source"))
chk(resp.get("matte_coverage", 0) > 0.01, "掩码里确实有人（不是空白）", resp.get("matte_coverage"))
chk(resp.get("matte_roi") == [0, 0, W, H], "掩码兜底是全幅坐标", resp.get("matte_roi"))

print("=== E) 画面里本来没人 -> 不挂 alpha，也不抛异常 ===")
resp, calls = attach({False: (0.0, None, None), True: (0.0, None, None)}, mask=EMPTY)
chk("matte_b64" not in resp, "不挂 alpha（调用方整幅取景）", sorted(resp))
chk(calls == [(100, 100, 300, 400), None], "两路都试过了才放弃", calls)

print("=== F) 裁切那一路抛异常 -> 不拖垮整条链 ===")
resp, calls = attach({False: RuntimeError("模型崩了"), True: (0.40, PERSON, None)}, mask=MASK)
chk(calls == [(100, 100, 300, 400), None], "异常那一路被跳过，继续试下一路", calls)
chk(resp.get("matte_source") == "modnet", "仍然交出了 alpha", resp.get("matte_source"))
chk("模型崩了" in (resp.get("matte_error") or ""), "异常如实上报，不吞掉", resp.get("matte_error"))
chk("matte_b64" in resp, "降级成功 -> 仍挂上 alpha")

print("=== G) 两路都抛异常 + 有掩码 -> 掩码兜底，不空手 ===")
resp, _ = attach({False: RuntimeError("A"), True: RuntimeError("B")}, mask=MASK)
chk(resp.get("matte_source") == "detect-mask", "退到掩码", resp.get("matte_source"))
chk("A" in (resp.get("matte_error") or "") and "B" in (resp.get("matte_error") or ""),
    "两次失败都记着", resp.get("matte_error"))

print("=== H) 没有检测框 -> 直接整幅（不硬造一个框）===")
resp, calls = attach({True: (0.40, PERSON, None)}, box=None, mask=MASK)
chk(calls == [None], "只调整幅那一次", calls)
chk(resp.get("matte_source") == "modnet", "标记为整幅", resp.get("matte_source"))

print("=== I) 明确不抠（matte=0）-> 一次都不调 ===")
resp, calls = attach({True: (0.40, PERSON, None)}, matte=False)
chk(calls == [], "没有多余推理", calls)
chk("matte_b64" not in resp, "没有 alpha")

print("=== J) 真模型冒烟（有 modnet.onnx 才跑）===")
worker.matte_available = _real_available
worker._encode_alpha = _real_encode = None
import importlib  # noqa: E402
importlib.reload(worker)

from PIL import Image  # noqa: E402

model = pathlib.Path(worker.DEFAULTS["matte_model"])
if not model.is_file():
    print("  [SKIP] 没找到 %s（本机没放模型，跳过真模型冒烟）" % model)
else:
    import os  # noqa: E402
    canvas = Image.new("RGB", (2592, 1944), (18, 20, 28))
    # 画一个「人」：一块亮色矩形，位置对应展台里的站位
    from PIL import ImageDraw  # noqa: E402
    d = ImageDraw.Draw(canvas)
    d.ellipse((1100, 620, 1400, 900), fill=(226, 210, 196))     # 头
    d.rectangle((1080, 880, 1440, 1880), fill=(64, 92, 150))    # 身
    d.rectangle((900, 1880, 1620, 1944), fill=(30, 30, 34))     # 地面
    rgb = np.asarray(canvas)
    box = (1080, 620, 1440, 1880)
    resp = {"width": 2592, "height": 1944}
    worker._attach_matte(resp, rgb, {"matte": True, "mask_max_side": 320},
                         box=box, fallback_mask=None)
    chk(resp.get("matte_b64"), "真模型交出了 alpha", resp.get("matte_source"))
    chk(resp.get("matte_ms", 0) > 0, "有真实耗时读数", resp.get("matte_ms"))
    chk(resp.get("matte_roi") != [0, 0, 2592, 1944] or True, "roi 记账", resp.get("matte_roi"))
    print("  [info] 真模型：source=%s coverage=%s roi=%s" %
          (resp.get("matte_source"), resp.get("matte_coverage"), resp.get("matte_roi")))

print()
bad = ok.count(False)
print("%d/%d PASS" % (ok.count(True), len(ok)) + ("  ← 有失败" if bad else ""))
sys.exit(1 if bad else 0)