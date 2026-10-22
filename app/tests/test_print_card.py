"""卡片与明信片满版输出，不加白边、不拉伸。"""

import sys
import tempfile
from pathlib import Path

from PIL import Image, ImageDraw

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from render.print_card import CARD_SIZE, POSTCARD_SIZE, prepare_card


def main():
    with tempfile.TemporaryDirectory() as folder:
        root = Path(folder)
        for paper_size in (CARD_SIZE, POSTCARD_SIZE):
            for size in ((1024, 1536), (1600, 900), paper_size):
                expected = paper_size if size[1] >= size[0] else paper_size[::-1]
                source = root / "source.png"
                image = Image.new("RGB", size, "blue")
                drawing = ImageDraw.Draw(image)
                center = (size[0] // 2, size[1] // 2)
                radius = min(size) // 8
                drawing.ellipse((center[0] - radius, center[1] - radius,
                                 center[0] + radius, center[1] + radius), fill="red")
                image.save(source)
                output = prepare_card(source, root / "print.jpg", size=paper_size)
                with Image.open(output) as rendered:
                    assert rendered.size == expected
                    assert rendered.info["dpi"] == (300, 300)
                    for edge in ((0, 0, expected[0], 1), (0, expected[1] - 1, *expected),
                                 (0, 0, 1, expected[1]), (expected[0] - 1, 0, *expected)):
                        assert all(blue > 230 and red < 25 and green < 25
                                   for red, green, blue in rendered.crop(edge).getdata())
                    red_mask = rendered.getchannel("R").point(lambda value: 255 if value > 180 else 0)
                    left, top, right, bottom = red_mask.getbbox()
                    assert abs((right - left) - (bottom - top)) <= 2
                    assert abs((left + right) / 2 - expected[0] / 2) <= 2
                    assert abs((top + bottom) / 2 - expected[1] / 2) <= 2

        source = root / "rotated.jpg"
        image = Image.new("RGB", (1536, 1024), "blue")
        exif = image.getexif()
        exif[274] = 6
        image.save(source, exif=exif)
        output = prepare_card(source, root / "rotated-print.jpg")
        with Image.open(output) as rendered:
            assert rendered.size == CARD_SIZE
    print("7/7 PASS: both paper sizes, full-bleed edges, aspect ratio, centering and EXIF")


if __name__ == "__main__":
    main()
