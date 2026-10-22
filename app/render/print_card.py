"""将电子照片等比裁切为 300dpi 的无边距打印画布。"""

from pathlib import Path

from PIL import Image, ImageOps


CARD_SIZE = (638, 1016)
POSTCARD_SIZE = (1181, 1748)


def prepare_card(source: Path | str, target: Path | str, *, size=CARD_SIZE) -> Path:
    source = Path(source)
    target = Path(target)
    with Image.open(source) as original:
        portrait = ImageOps.exif_transpose(original).convert("RGB")
        size = size if portrait.height >= portrait.width else size[::-1]
        canvas = ImageOps.fit(portrait, size, Image.Resampling.LANCZOS)
    target.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(target, "JPEG", quality=95, subsampling=0, dpi=(300, 300))
    return target
