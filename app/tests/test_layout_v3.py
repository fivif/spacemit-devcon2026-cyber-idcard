"""浅色主视觉横竖版式离线验收。"""

from __future__ import annotations

from pathlib import Path
import sys

import numpy as np
from PIL import Image, ImageDraw

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from render import keyvisual  # noqa: E402


def synthetic_cutout():
    width, height = 520, 900
    rgb = np.zeros((height, width, 3), np.uint8)
    rgb[:] = (52, 90, 180)
    alpha = np.zeros((height, width), np.float32)
    image = Image.fromarray((alpha * 255).astype(np.uint8), "L")
    draw = ImageDraw.Draw(image)
    draw.ellipse((130, 35, 390, 315), fill=255)
    draw.rounded_rectangle((70, 285, 450, 895), radius=150, fill=255)
    return rgb, np.asarray(image, np.float32) / 255.0


def main() -> int:
    checks = []

    def check(value, label):
        ok = bool(value)
        checks.append(ok)
        print("  [%s] %s" % ("PASS" if ok else "FAIL", label))

    cutout = synthetic_cutout()
    fitted_w = keyvisual._fit_cutout(cutout, (650, 705))
    fitted_v = keyvisual._fit_cutout(cutout, (824, 1170))
    alpha_points = np.argwhere(cutout[1] > 0.01)
    y0, x0 = alpha_points.min(axis=0)
    y1, x1 = alpha_points.max(axis=0) + 1
    active_ratio = (x1 - x0) / (y1 - y0)
    check(abs(fitted_w[0].width / fitted_w[0].height - active_ratio) < 0.01,
          "横版人物等比缩放")
    check(abs(fitted_v[0].width / fitted_v[0].height - active_ratio) < 0.01,
          "竖版人物等比缩放")

    wide = keyvisual.compose(None, record_id="SM-2026-0001", name="", handle="",
                             cutout=cutout, size=keyvisual.WIDE,
                             keyvisual="light-film-tech")
    vertical = keyvisual.compose(None, record_id="SM-2026-0001", name="", handle="",
                                 cutout=cutout, size=keyvisual.VERTICAL,
                                 keyvisual="light-film-tech")
    vertical_clean = keyvisual.compose(None, record_id="SM-2026-0001", name="", handle="",
                                       cutout=cutout, size=keyvisual.VERTICAL,
                                       keyvisual="light-film-tech", include_event_text=False)
    check(wide.size == (1600, 900), "横版输出 1600x900")
    check(vertical.size == (1024, 1536), "竖版输出 1024x1536")

    blue = np.array([52, 90, 180], np.int16)
    wide_arr = np.asarray(wide, np.int16)
    vertical_arr = np.asarray(vertical, np.int16)
    wide_person = np.max(np.abs(wide_arr - blue), axis=2) < 5
    vertical_person = np.max(np.abs(vertical_arr - blue), axis=2) < 5
    wide_x = np.where(wide_person)[1]
    vertical_x = np.where(vertical_person)[1]
    check(wide_x.size and float(wide_x.mean()) < 720, "横版人物位于左侧")
    check(vertical_x.size and 350 < float(vertical_x.mean()) < 674, "竖版人物居中突出")

    with_info = keyvisual.compose(None, record_id="SM-2026-0001", name="李维", handle="开发者",
                                  cutout=cutout, size=keyvisual.VERTICAL,
                                  keyvisual="light-film-tech")
    blank_arr = np.asarray(vertical)
    info_arr = np.asarray(with_info)
    diff = np.abs(info_arr.astype(np.int16) - blank_arr.astype(np.int16)).max(axis=2)
    check(diff[1170:1305, 130:830].mean() > 0.2, "个人信息只在填写后出现")
    identity_diff = np.abs(np.asarray(vertical, np.int16)
                           - np.asarray(vertical_clean, np.int16)).max(axis=2)
    check(identity_diff[100:170, 130:610].mean() > 0.2, "最终成片包含 Logo 与大会标题")

    check(keyvisual.window_rect(keyvisual.WIDE, "light-film-tech") == (70, 115, 720, 820),
          "横版人物安全区固定")
    check(keyvisual.window_rect(keyvisual.VERTICAL, "light-film-tech") == (100, 170, 924, 1340),
          "竖版人物安全区固定")

    generated = Image.new("RGB", (1024, 1536), (232, 238, 226))
    generated_card = keyvisual.compose_generated(
        generated, record_id="SM-2026-0001", name="李维", handle="开发者",
        size=keyvisual.VERTICAL)
    generated_diff = np.abs(np.asarray(generated_card, np.int16)
                            - np.asarray(generated, np.int16)).max(axis=2)
    check(generated_card.size == keyvisual.VERTICAL, "AIGC 完整场景不套固定底图")
    check(generated_diff[100:170, 130:610].mean() > 0.1, "AIGC 场景叠加 Logo 与大会标题")
    check(generated_diff[1170:1305, 130:830].mean() > 0.2, "AIGC 场景叠加个人信息")

    print("%d/%d PASS" % (sum(checks), len(checks)))
    return 0 if all(checks) else 1


if __name__ == "__main__":
    raise SystemExit(main())
