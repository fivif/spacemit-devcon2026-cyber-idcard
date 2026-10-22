"""AIGC 人像边缘、合成描边与检测去重的离线回归。"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFilter

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from aigc_client import constrain_alpha, fit_cutout, refine_generated_alpha  # noqa: E402
from infer.worker import _drop_nested_persons  # noqa: E402
from render import keyvisual  # noqa: E402


def check(value, label):
    print("  [%s] %s" % ("PASS" if value else "FAIL", label))
    return bool(value)


def main() -> int:
    results = []

    fine = Image.new("L", (256, 256), 0)
    draw = ImageDraw.Draw(fine)
    draw.ellipse((52, 34, 202, 244), fill=255)
    draw.ellipse((224, 18, 252, 52), fill=210)
    coarse = Image.new("L", fine.size, 255)
    cleaned = constrain_alpha(fine, coarse)
    results.append(check(cleaned.getpixel((238, 34)) == 0, "移除主轮廓外的离散误检"))
    results.append(check(cleaned.getpixel((128, 128)) == 255, "保留主体内部实心区域"))

    two_people = Image.new("L", (256, 256), 0)
    two_draw = ImageDraw.Draw(two_people)
    two_draw.ellipse((42, 30, 198, 250), fill=255)
    two_draw.ellipse((208, 20, 250, 88), fill=255)
    selected_person = Image.new("L", two_people.size, 0)
    ImageDraw.Draw(selected_person).ellipse((42, 30, 198, 250), fill=255)
    selected_only = constrain_alpha(two_people, selected_person)
    results.append(check(selected_only.getpixel((228, 50)) == 0,
                         "主体专属掩膜会清除远处第二个人"))

    guide = Image.new("L", (512, 512), 0)
    ImageDraw.Draw(guide).ellipse((150, 54, 362, 294), fill=255)
    generated = Image.new("L", guide.size, 0)
    generated_draw = ImageDraw.Draw(generated)
    generated_draw.ellipse((178, 44, 430, 292), fill=255)
    generated_draw.rounded_rectangle((70, 302, 500, 510), radius=70, fill=255)
    generated_draw.rectangle((0, 0, 20, 20), fill=48)
    refined = refine_generated_alpha(generated, guide)
    results.append(check(refined.getpixel((408, 180)) > 200,
                         "AIGC 人像横向位移后仍保留完整头部"))
    results.append(check(refined.getpixel((280, 420)) > 200,
                         "二次抠像断开的衣服主体不会被误删"))
    results.append(check(refined.getpixel((10, 10)) == 0,
                         "低透明度白边与阴影会被清除"))

    alpha = Image.new("L", (256, 256), 0)
    ImageDraw.Draw(alpha).ellipse((48, 28, 208, 246), fill=255)
    alpha = alpha.filter(ImageFilter.GaussianBlur(6))
    rgb = Image.new("RGB", alpha.size, (238, 238, 238))
    red = Image.new("RGB", alpha.size, (150, 35, 28))
    rgb.paste(red, (0, 0), alpha)
    fitted, fitted_alpha = fit_cutout(rgb, alpha, size=(390, 500))
    arr = np.asarray(fitted, np.float32)
    matte = np.asarray(fitted_alpha, np.float32) / 255.0
    edge = (matte > 0.08) & (matte < 0.70)
    edge_rgb = arr[edge].mean(axis=0)
    results.append(check(edge_rgb[0] > edge_rgb[1] * 2.0,
                         "柔边颜色由人物内部延展，不保留灰白底色"))

    nested = [
        {"score": 0.6026, "box": [399.9, 256.2, 1341.7, 983.6]},
        {"score": 0.5745, "box": [939.8, 320.2, 1198.3, 701.6]},
    ]
    cleaned_people, suppressed = _drop_nested_persons(nested)
    results.append(check(len(cleaned_people) == 1 and suppressed == 1,
                         "主人物内部的小重复框不会被误算成第二个人"))

    foreground_mask = np.zeros((120, 120), dtype=bool)
    foreground_mask[12:116, 8:96] = True
    background_mask = np.zeros((120, 120), dtype=bool)
    background_mask[8:34, 88:108] = True
    nested_real_people = [
        {"score": 0.91, "box": [8, 8, 108, 116]},
        {"score": 0.82, "box": [88, 8, 108, 34]},
    ]
    cleaned_people, suppressed = _drop_nested_persons(
        nested_real_people, masks={0: foreground_mask, 1: background_mask})
    results.append(check(len(cleaned_people) == 2 and suppressed == 0,
                         "大前景框包住远处真人时，分离的掩膜不会被当重复框"))

    duplicate_mask = np.zeros((120, 120), dtype=bool)
    duplicate_mask[20:58, 42:78] = True
    same_person_masks = {0: foreground_mask, 1: duplicate_mask}
    cleaned_people, suppressed = _drop_nested_persons(
        nested_real_people, masks=same_person_masks)
    results.append(check(len(cleaned_people) == 1 and suppressed == 1,
                         "框和掩膜都嵌套时仍会去掉同一人物的重复框"))
    separate = [
        {"score": 0.81, "box": [80, 100, 420, 900]},
        {"score": 0.77, "box": [460, 120, 800, 910]},
    ]
    separate_people, suppressed = _drop_nested_persons(separate)
    results.append(check(len(separate_people) == 2 and suppressed == 0,
                         "真正并排的两个人仍保持两人计数"))

    portrait = Image.new("RGB", keyvisual.PORTRAIT_SIZE, (160, 45, 35))
    portrait_alpha = np.zeros((keyvisual.PORTRAIT_SIZE[1], keyvisual.PORTRAIT_SIZE[0]),
                              dtype=np.float32)
    portrait_alpha[120:940, 130:650] = 1.0
    saved_tone = keyvisual.PERSON_TONE
    try:
        keyvisual.PERSON_TONE = False
        natural_composite = np.asarray(
            keyvisual._compose_cutout((portrait, portrait_alpha), natural=True))
        styled_composite = np.asarray(
            keyvisual._compose_cutout((portrait, portrait_alpha), natural=False))
    finally:
        keyvisual.PERSON_TONE = saved_tone
    results.append(check(np.array_equal(natural_composite, styled_composite),
                         "原片与 AIGC 路径都不会额外添加人物描边"))

    print("%d/%d PASS" % (sum(results), len(results)))
    return 0 if all(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
