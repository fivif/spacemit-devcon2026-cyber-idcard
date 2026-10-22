#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""成片大会标识：顶部品牌字放大且避让人像、只填一项个人信息时居中到竖线。

运行：.venv-test\\Scripts\\python.exe -m unittest app.tests.test_event_identity
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import numpy as np
from PIL import Image, ImageDraw

APP = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP))

import booth_server  # noqa: E402
import render.keyvisual as kv  # noqa: E402
import render.watermark as wm  # noqa: E402


def identity(value):
    return int(round(value))


def person_mask(head_top: int) -> Image.Image:
    mask = Image.new("L", kv.VERTICAL, 0)
    draw = ImageDraw.Draw(mask)
    draw.ellipse((300, head_top, 740, head_top + 620), fill=255)
    draw.rectangle((60, head_top + 560, 990, 1536), fill=255)
    return mask


def legacy_identity(img: Image.Image, *, name: str, handle: str) -> Image.Image:
    """照抄改动前 _draw_event_identity 的竖版画法，作为逐像素基准。"""
    overlay = img.convert("RGBA")
    w, h = img.size
    gradient = np.zeros((h, w, 4), dtype=np.uint8)
    gradient[:, :, :3] = (12, 20, 28)
    for row in range(h):
        position = min(1.0, max(0.0, (row / max(1, h) - 0.66) / 0.27))
        gradient[row, :, 3] = round(172 * position * position * (3 - 2 * position))
    overlay.alpha_composite(Image.fromarray(gradient, "RGBA"))
    logo = wm.logo_glyph(38)
    overlay.alpha_composite(logo, (138, 112))
    draw = ImageDraw.Draw(overlay, "RGBA")
    draw.text((138 + logo.width + 15, 111), "SpacemiT DevCon 2026",
              font=wm.load_font(31), fill=(29, 43, 49, 255))
    if name.strip() or handle.strip():
        draw.rounded_rectangle((142, 1187, 147, 1292), radius=2, fill=(178, 228, 13, 255))
        if name.strip():
            font = wm.fit_font(draw, name.strip(), 650, 55, min_size=22)
            draw.text((166, 1177), name.strip(), font=font, fill=(250, 251, 247, 255), anchor="lt")
        if handle.strip():
            font = wm.fit_font(draw, handle.strip(), 650, 27, min_size=17)
            draw.text((168, 1254), handle.strip(), font=font, fill=(223, 232, 228, 255), anchor="lt")
    return overlay.convert("RGB")


def ink_rows(img: Image.Image, box) -> tuple[int, int]:
    """box 区域里亮色文字墨迹的上下边界（深底上找亮像素）。"""
    arr = np.asarray(img.crop(box).convert("L"))
    rows = np.where((arr > 170).any(axis=1))[0]
    return box[1] + int(rows.min()), box[1] + int(rows.max())


class EventIdentityTests(unittest.TestCase):
    def setUp(self):
        self.base = Image.new("RGB", kv.VERTICAL, (28, 34, 40))

    def test_brand_update_preserves_personal_information_pixel_for_pixel(self):
        light = Image.new("RGB", kv.VERTICAL, (226, 228, 222))   # 样稿就是亮底
        for name, handle in (("林小满", "@xiaoman.dev"), ("", "")):
            new = kv._draw_event_identity(light.copy(), name=name, handle=handle)
            old = legacy_identity(light.copy(), name=name, handle=handle)
            self.assertEqual(new.crop((0, 300, 1024, 1536)).tobytes(),
                             old.crop((0, 300, 1024, 1536)).tobytes(), (name, handle))

    def test_title_ink_follows_background_brightness(self):
        for color, want in (((226, 228, 222), kv.TITLE_INK), ((30, 60, 120), kv.TITLE_INK_ON_DARK)):
            img = kv._draw_event_identity(Image.new("RGB", kv.VERTICAL, color), name="", handle="")
            title = np.asarray(img)[95:160, 150:680].reshape(-1, 3).astype(int)
            ink = np.abs(title - np.array(want[:3])).max(axis=1) < 24
            self.assertGreater(int(ink.sum()), 300, color)

    def test_brand_enlarges_only_when_there_is_room(self):
        confirmed = (44, 33, 110)
        self.assertEqual(kv._brand_layout(identity, None, 28), confirmed)
        far = kv._brand_layout(identity, kv._subject_tops(person_mask(255), kv.VERTICAL), 28)
        self.assertEqual(far, confirmed)
        close = kv._brand_layout(identity, kv._subject_tops(person_mask(165), kv.VERTICAL), 28)
        self.assertLess(close[0], confirmed[0])
        self.assertLess(close[1], confirmed[1])

    def test_generated_portrait_preserves_confirmed_brand_size_without_reference_estimate(self):
        booth = object.__new__(booth_server.Booth)
        booth.aigc_style = Mock(return_value={"title": "测试风格", "sizes": ["1024x1536"]})
        booth._plate_subject = Mock(side_effect=AssertionError("reference placement must not shrink finished brand"))
        job = SimpleNamespace(source="operator_regenerate", step=Mock())
        scene = Image.new("RGB", kv.VERTICAL, (226, 228, 222))
        photo = Image.new("RGB", (600, 900), (90, 90, 90))
        alpha = Image.new("L", photo.size, 255)
        meta = {"orientation": "vertical", "card": "keyvisual"}
        with patch("render.stylize.render_cutout", return_value=(photo, alpha)):
            final, _, _, clean = booth._render(job, photo, None, "hand-doodle", "test", meta,
                                               alpha=alpha, aigc_scene=scene)
        expected = kv._draw_event_identity(scene.copy(), name="", handle="")
        self.assertEqual(final.tobytes(), expected.tobytes())
        self.assertEqual(clean.tobytes(), scene.tobytes())
        booth._plate_subject.assert_not_called()

    def test_enlarged_brand_never_reaches_into_head_clearance(self):
        for head_top in (165, 185, 200, 230, 255, 300):
            layout = kv._brand_layout(identity, kv._subject_tops(person_mask(head_top), kv.VERTICAL),
                                      kv.BRAND_CLEARANCE)
            if layout[0] == identity(kv.BRAND_LOGO_SIZE * kv.BRAND_MIN_SCALE):
                continue
            img = kv._draw_event_identity(self.base.copy(), name="", handle="",
                                          subject_mask=person_mask(head_top))
            arr = np.asarray(img.convert("L")).astype(int) - 33
            drawn = np.where((np.abs(arr) > 12).any(axis=1))[0]
            brand_bottom = int(drawn[drawn < 400].max())
            logo_size, title_size, _ = layout
            logo_width = wm.logo_glyph(logo_size).width
            position = kv._brand_title_xy(identity, logo_width, title_size)
            bounds = wm.load_font(title_size).getbbox(kv.BRAND_TITLE, anchor="ls")
            right = max(kv.BRAND_LEFT + logo_width, position[0] + bounds[2])
            tops = kv._subject_tops(person_mask(head_top), kv.VERTICAL)
            subject_top = int(tops[max(0, kv.BRAND_LEFT - 12):right + 12].min())
            self.assertLessEqual(brand_bottom + kv.BRAND_CLEARANCE, subject_top, head_top)

    def test_confirmed_brand_margin_alignment_and_original_font(self):
        for size in (kv.VERTICAL, kv.WIDE):
            scale = min(size[0] / 1024, size[1] / 1536)
            px = lambda value: int(round(value * scale))
            base = Image.new("RGB", size, (226, 228, 222))
            result = kv._draw_event_identity(base.copy(), name="", handle="")
            logo_size, title_size, logo_top = kv._brand_layout(px, None, px(28))
            logo = wm.logo_glyph(logo_size)
            position = kv._brand_title_xy(px, logo.width, title_size)
            bounds = wm.load_font(title_size).getbbox(kv.BRAND_TITLE, anchor="ls")
            self.assertAlmostEqual(position[1] + (bounds[1] + bounds[3]) / 2,
                                   px(132), delta=1)
            self.assertEqual(logo_top, px(132) - logo_size // 2)
            self.assertEqual(result.crop((0, 0, px(138), px(220))).tobytes(),
                             base.crop((0, 0, px(138), px(220))).tobytes())
            title_box = (position[0] + bounds[0], position[1] + bounds[1],
                         position[0] + bounds[2], position[1] + bounds[3])
            expected = base.copy()
            ImageDraw.Draw(expected).text(position, kv.BRAND_TITLE,
                                          font=wm.load_font(title_size), fill=kv.TITLE_INK[:3],
                                          anchor="ls")
            self.assertEqual(result.crop(title_box).tobytes(), expected.crop(title_box).tobytes())

    def test_single_field_is_centered_on_the_bar(self):
        bar_center = (1187 + 1292) / 2
        for name, handle in (("林小满", ""), ("", "@xiaoman.dev"), ("Ada", "")):
            img = kv._draw_event_identity(self.base.copy(), name=name, handle=handle)
            top, bottom = ink_rows(img, (160, 1150, 900, 1330))
            self.assertAlmostEqual((top + bottom) / 2, bar_center, delta=1.5, msg=(name, handle))

    def test_cutout_mask_matches_where_compose_pastes_the_person(self):
        if not kv.available(kv.DEFAULT_KEYVISUAL, kv.VERTICAL):
            self.skipTest("本地缺主视觉素材")
        rgb = np.zeros((1000, 780, 3), np.uint8)
        rgb[:] = (255, 0, 255)
        alpha = np.zeros((1000, 780), np.uint8)
        alpha[120:980, 160:620] = 255
        composed = kv._compose_cutout((rgb, alpha), size=kv.VERTICAL)
        mask = kv.cutout_mask((rgb, alpha), size=kv.VERTICAL)
        arr = np.asarray(composed).astype(int)
        magenta = (arr[:, :, 0] > 200) & (arr[:, :, 1] < 60) & (arr[:, :, 2] > 200)
        solid = np.asarray(mask) > 200
        self.assertGreater(solid.sum(), 1000)
        self.assertGreater((magenta & solid).sum() / solid.sum(), 0.98)

    def test_plate_estimate_adds_margin_for_aigc_drift(self):
        photo = Image.new("RGB", (600, 900), (90, 90, 90))
        alpha = Image.new("L", (600, 900), 0)
        alpha.paste(255, (150, 120, 450, 880))
        stub = SimpleNamespace(_aigc_request_size=booth_server.Booth._aigc_request_size)
        spec = {"sizes": ["1536x1024", "1024x1536"]}
        got = booth_server.Booth._plate_subject(stub, photo, alpha, spec, "vertical", kv.VERTICAL)
        self.assertEqual(got["subject_margin"], 48)
        self.assertEqual(got["subject_mask"].size, kv.VERTICAL)
        self.assertIsNotNone(got["subject_mask"].getbbox())
        self.assertEqual(booth_server.Booth._aigc_request_size(spec, "wide"), "1536x1024")
        self.assertEqual(booth_server.Booth._aigc_request_size(spec, "vertical"), "1024x1536")


if __name__ == "__main__":
    unittest.main()
