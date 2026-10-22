from __future__ import annotations

import os

from PIL import Image, ImageDraw, ImageFont

from . import watermark


def apply_ai_label(image: Image.Image) -> Image.Image:
    result = image.convert("RGBA")
    short_side = min(result.size)
    font_size = max(10, round(short_side * 0.020))
    path = next((path for path in watermark.SANS_CANDIDATES
                 if any(hint in path.lower() for hint in watermark._CJK_HINTS)
                 and os.path.isfile(path)), None)
    font = ImageFont.truetype(path, font_size) if path else watermark.load_font(font_size)
    label = "AI生成" if path else "AI Generated"
    bounds = font.getbbox(label)
    padding_x = max(4, round(font_size * 0.5))
    padding_y = max(3, round(font_size * 0.3))
    width = bounds[2] - bounds[0] + padding_x * 2
    height = bounds[3] - bounds[1] + padding_y * 2
    margin = max(8, round(short_side * 0.065))
    left = result.width - margin - width
    top = margin
    layer = Image.new("RGBA", result.size)
    draw = ImageDraw.Draw(layer)
    draw.rounded_rectangle((left, top, left + width, top + height),
                           radius=max(3, round(font_size * 0.3)), fill=(255, 255, 255, 140))
    draw.text((left + padding_x - bounds[0], top + padding_y - bounds[1]),
              label, font=font, fill=(68, 76, 80, 180))
    return Image.alpha_composite(result, layer).convert("RGB")
