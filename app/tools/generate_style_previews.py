"""Generate public portrait-style previews through the configured AIGC gateway."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from PIL import Image


APP_DIR = Path(__file__).resolve().parents[1]
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))

from aigc_catalog import StyleCatalog  # noqa: E402
from aigc_client import edit_portrait  # noqa: E402


PUBLIC_STYLE_IDS = (
    "pixel-person",
    "hand-painted-animation",
    "american-comic",
    "korean-pop",
    "hand-doodle",
    "professional-suit",
    "chinese-elegance",
)

PREVIEW_RULES = (
    "This is a polished style-selection preview for a public photo booth. "
    "Keep exactly one fictional adult woman, chest-up, with her complete head and shoulders visible. "
    "Preserve the reference person's recognizable facial structure, long dark hair and friendly expression. "
    "Create a finished 2:3 vertical portrait with the person clearly dominant and a coherent style-matched background. "
    "Do not generate text, letters, numbers, logos, watermarks, borders, UI, a second person or busy props."
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--source",
        type=Path,
        default=APP_DIR / "assets" / "styles" / "hand-painted-animation.png",
    )
    parser.add_argument("--output", type=Path, default=APP_DIR / "assets" / "styles")
    parser.add_argument("--data", type=Path, default=APP_DIR / "data")
    parser.add_argument("--only", choices=PUBLIC_STYLE_IDS)
    args = parser.parse_args()

    source = Image.open(args.source).convert("RGB")
    alpha = Image.new("L", source.size, 255)
    catalog = {item["id"]: item for item in StyleCatalog(args.data).snapshot()["styles"]}
    selected = (args.only,) if args.only else PUBLIC_STYLE_IDS
    args.output.mkdir(parents=True, exist_ok=True)

    for style_id in selected:
        style = catalog[style_id]
        prompt = str(style["vertical_prompt"]).strip() + "\n" + PREVIEW_RULES
        generated = edit_portrait(
            source,
            alpha,
            prompt=prompt,
            model=str(style["model"]),
            size="1024x1536",
        )
        destination = args.output / f"{style_id}-preview.jpg"
        generated.convert("RGB").save(destination, "JPEG", quality=88, optimize=True)
        print(f"{style_id}: {destination.name} {generated.width}x{generated.height}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
