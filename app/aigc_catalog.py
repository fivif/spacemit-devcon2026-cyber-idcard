"""本地 AIGC 风格提示词目录。"""

from __future__ import annotations

import json
import os
import re
import threading
import time
from pathlib import Path


ID_RE = re.compile(r"^[a-z][a-z0-9-]{2,48}$")
MODEL_RE = re.compile(r"^[A-Za-z0-9._:/-]{1,128}$")
SIZE_RE = re.compile(r"^[0-9]{3,5}x[0-9]{3,5}$")
ORIENTATIONS = ("wide", "vertical")
PRESET_VERSION = 10
LEGACY_PRESET_VERSION = 4
LEGACY_STYLE_IDS = {"korean-pop": "soft-chibi", "professional-suit": "cel-shaded-3d"}
TRACKED_FIELDS = (
    "title", "description", "wide_prompt", "vertical_prompt", "negative_prompt", "model",
    "sizes", "backdrop_id", "default_orientation", "preserve_identity", "single_person",
    "no_generated_text", "no_border", "enabled", "is_default",
)

GENERAL_NEGATIVE = (
    "No text, letters, numbers, logo, watermark, signature, border or card frame. "
    "No second person, duplicate face, altered identity, extra limbs, distorted anatomy, "
    "cropped head or cropped shoulders. "
)

CLOTHING_NEGATIVE = (
    "Preserve the original clothing exactly: no outfit change, "
    "do not invent or add new clothes, a suit, tie, jacket, costume or uniform; do not remove or "
    "recolour any original garment or accessory. "
)

COMMON_NEGATIVE = GENERAL_NEGATIVE + CLOTHING_NEGATIVE + "Avoid decorations that obscure the portrait."

CLOTHING_LOCK = (
    " Keep the person's original clothing exactly as photographed: preserve the same garments, "
    "colours, patterns, neckline, sleeves, collar, logos and accessories. Do not redesign, replace, "
    "recolour, add or remove any clothing or accessory; only transform the visual rendering style "
    "of the person and background."
)

SUIT_CLOTHING = (
    " Replace the person's original outfit with a well-fitted dark business suit, a crisp dress "
    "shirt and a coordinated tie. The suit jacket, shirt and tie must be clearly visible. "
    "Change only the clothing; preserve the person's identity, facial structure, hairstyle, pose, "
    "and non-clothing accessories such as glasses."
)

SUIT_CLOTHING_NEGATIVE = (
    "Do not retain the original casual outfit or show a T-shirt, hoodie, casual jacket, costume "
    "or uniform instead of the business suit. No missing suit jacket, dress shirt or tie. "
    "No altered identity, hairstyle, pose or glasses. "
)

SUIT_NEGATIVE = GENERAL_NEGATIVE + SUIT_CLOTHING_NEGATIVE + "Avoid decorations that obscure the portrait."

CHINESE_CLOTHING = (
    " Replace only the clothing with a fresh, flattering contemporary Chinese-style outfit. "
    "Freely reinterpret traditional Chinese tailoring with a relaxed stand collar, an elegant crossed "
    "collar, an asymmetric wrap neckline or a light Han-inspired layered jacket; choose one, not all. "
    "Give the garment an airy silhouette, soft folds and tactile silk, linen or fine woven texture, "
    "with one tasteful accent such as a delicate frog fastening, tonal embroidery or a jacquard edge. "
    "Invent a harmonious palette inspired by jade, bamboo, cinnabar, ink and warm ivory; allow lively "
    "colour contrasts and a small luminous accent instead of repeating a dull grey-green uniform. "
    "Dress this person naturally, without assigning clothing by a guessed gender or altering the "
    "person's face, hairstyle, age or glasses. No imperial costume or added jewellery."
)

CHINESE_CLOTHING_NEGATIVE = (
    "No business suit and tie, imperial robes, armour, elaborate headdress or excessive jewellery. "
    "No rigid ceremonial portrait, bureaucratic uniform, lifeless mannequin, funeral styling, "
    "muddy desaturated colour cast, repeated plain green tunic or overcrowded decorative collage. "
    "No neon glow, cyberpunk city, holographic interface or glowing eyes. "
    "No profile view, tilted head, looking away, closed eyes, hair covering the eyes, "
    "face covering, sunglasses, blurred face, plastic skin, heavy makeup, harsh shadows or oversaturation. "
)

CHINESE_NEGATIVE = GENERAL_NEGATIVE + CHINESE_CLOTHING_NEGATIVE + "Avoid decorations that obscure the portrait."

LIGHT_BEAUTY = (
    "LIGHT STYLE-AWARE BEAUTIFICATION: Give this same person a gently flattering, fresh and polished finish. "
    "Even the complexion, soften temporary blemishes and tired-looking shadows, brighten existing eye highlights "
    "and add restrained healthy cheek and lip colour. Express these improvements through the selected style's own "
    "palette, shading and drawing technique. Keep realistic skin texture in photographic styles and visible square "
    "pixel clusters in pixel art. This is light cosmetic refinement, not a new face: preserve facial contour, "
    "jawline, chin, cheekbones, eye size and spacing, nose and mouth shape, age, hairline and distinctive features. "
    "No face slimming, structural reshaping, plastic skin, heavy makeup or generic beautified replacement face. "
)

STANDARD_BEAUTY = (
    "STANDARD PORTRAIT BEAUTY DIRECTION: Do not present this as an untouched documentary photo. "
    "Create a polished, flattering editorial finish while keeping the same recognizable person. "
    "Use a naturally brighter ambient environment with soft key-and-fill light; lift the overall exposure "
    "of the face, background and clothing detail without flattening natural shadows. Gently brighten and even the complexion while preserving the original "
    "skin undertone, carefully groomed hair and brows, clear eyes, "
    "subtle under-eye correction and natural lip colour. Where the source presentation calls for makeup, "
    "apply tasteful style-appropriate makeup with restrained eye definition, balanced blush and a clean lip; "
    "where it does not, use neat grooming and understated complexion correction instead. Make the result "
    "more confident, fresh and attractive, with refined facial shading and natural skin texture. "
    "Do not change gender presentation, facial anatomy, face shape, age, hairstyle identity or expression; "
    "Do not porcelain-whiten, bleach or flatten the natural skin tone; do not use a beauty-filter replacement face, "
    "waxy skin, dramatic stage makeup or exaggerated idol features."
)

REFERENCE_QUALITY = (
    "REFERENCE QUALITY ENHANCEMENT: The supplied camera image may be low-resolution. Use it as the identity "
    "reference, then reconstruct a clean high-resolution final portrait with crisp eyes, coherent facial edges, "
    "natural hair strands, clear clothing texture and smooth style-specific shading. Remove camera noise, JPEG "
    "blocks, motion blur, colour noise, jagged edges, double features and muddy fine detail. Enhance detail "
    "through the chosen art technique, not by inventing a different face or changing the original clothing."
)

STYLE_BEAUTY = {
    "hand-painted-animation": (
        "STYLE BEAUTY FINISH: Use thin translucent peach-and-warm skin washes, soft transitions under the eyes "
        "and small natural catchlights. Reduce temporary blemish marks without erasing expressive hand-painted "
        "texture, the original skin tone or distinctive facial details."
    ),
    "american-comic": (
        "STYLE BEAUTY FINISH: Keep facial colour planes clean and flattering, with sparse fine halftone dots "
        "and soft midtone shadows. Lighten tired under-eye shadows and temporary blemishes; keep bold ink mainly "
        "on the actual contours rather than adding deep cheek hollows, wrinkle lines or superhero anatomy."
    ),
    "hand-doodle": (
        "STYLE BEAUTY FINISH: Use airy warm marker washes, delicate coloured-pencil shading and a few clear "
        "eye highlights. Soften temporary blemishes and tired shadows through light strokes; retain paper grain "
        "and recognizable facial lines, keeping dense hatching and playful scribbles away from the face."
    ),
    "professional-suit": (
        "STYLE BEAUTY FINISH: Apply light editorial portrait retouching with soft fill light, gently even "
        "complexion, reduced temporary blemishes and softened under-eye shadows. Preserve pores, fine skin "
        "texture and natural skin tone; keep lips and eyes understated, without waxy smoothing or beauty-filter makeup."
    ),
    "chinese-elegance": (
        "STYLE BEAUTY FINISH: Use translucent warm mineral-pigment skin washes, delicate ink modelling and "
        "restrained natural cheek and lip colour. Gently soften temporary blemishes and tired shadows while "
        "keeping the original skin tone, facial character and clear eyes; avoid porcelain whitening or theatrical makeup."
    ),
    "pixel-person": (
        "STYLE BEAUTY FINISH: Use a few clean warm midtone pixel clusters to even the complexion, simplify "
        "temporary blemish pixels and soften tired shadow clusters. Add tiny catchlight pixels and restrained cheek "
        "colour for a cute fresh finish, preserving square pixel edges and the person's distinctive facial proportions."
    ),
}

SCENE_RULES = 'IMPORTANT: The following scene rules override any earlier instruction about a fixed background, background plate, processor placement or portrait cutout. The input is a neutral-gray portrait reference. Generate the complete finished portrait scene in one pass: stylize the single person and replace the gray with a newly invented background that naturally matches the selected visual style, palette, texture and light direction. Do not reuse a fixed backdrop or template. Choose one restrained background idea for this generation and vary its composition from other results. The person is the unmistakable primary subject; the background is airy, quiet and low-detail, using broad colour fields and at most two or three subtle abstract or technology-inspired forms. Keep a clean natural silhouette without white or coloured outlines, sticker edges, halos, drop shadows, pedestals or base lines. Leave calm negative space near the upper edge for a small brand caption added later. Do not generate text, letters, numbers, logos, watermarks, extra people or busy props.'


def clothing_prompt(style_id: str) -> str:
    return {"professional-suit": SUIT_CLOTHING, "chinese-elegance": CHINESE_CLOTHING}.get(style_id, CLOTHING_LOCK)


def clothing_negative_prompt(style_id: str) -> str:
    return {"professional-suit": SUIT_NEGATIVE, "chinese-elegance": CHINESE_NEGATIVE}.get(style_id, COMMON_NEGATIVE)


def portrait_mode_prompt(mode: str = "standard") -> str:
    if mode not in ("standard", "chibi"):
        raise ValueError("人像模式只能是写实版或Q版大头像")
    identity = (
        "Preserve identity: the reference photograph is the sole authority for this person's likeness. "
        "Keep the characteristic facial contour, jawline, chin shape, cheekbones, face width-to-height ratio, "
        "eye spacing, nose shape, mouth proportions, hairline, hairstyle, age, expression and glasses. "
        "Keep recognizable likeness while allowing light style-aware cosmetic refinement. "
    )
    composition = (
        "Show exactly one person with a complete head and shoulders, comfortable breathing room and a clear face. "
        "Do not create a full-body little character, visible feet, props obscuring the face or a cropped head. "
    )
    if mode == "chibi":
        return identity + LIGHT_BEAUTY + composition + (
            "SELECTED MODE: CHIBI LARGE-HEAD AVATAR. Create a cute chibi head-and-shoulders portrait "
            "with an oversized head and small shoulders, framed closely so the head is the dominant visual focus. "
            "Enlarge the head as a whole while preserving the person's distinctive face shape and relative feature placement. "
            "Simplify details gently without turning everyone into the same round baby face. "
            "Keep the selected art style's own drawing technique and textures; cute proportions do not replace the art style. "
            "Pixel art must remain visibly pixelated, not turn into a smooth anime illustration. "
            "For the professional suit style, use a cute illustrated business portrait."
        )
    return identity + LIGHT_BEAUTY + STANDARD_BEAUTY + composition + (
        "SELECTED MODE: STANDARD NON-CHIBI ID PORTRAIT. Preserve the original face geometry and natural "
        "head-to-shoulder proportions, shown from the chest up. Apply the selected art style and light cosmetic refinement "
        "to skin colour, shading and eye highlights; also vary lighting and background. A cute, friendly finish is allowed "
        "without changing facial anatomy or adult proportions. "
        "No chibi proportions, oversized head, enlarged eyes, "
        "shrunken chin, jaw slimming, face rounding, feature repositioning or generic anime/idol/superhero face."
    )


def mode_neutral_prompt(prompt: str) -> str:
    return re.sub(
        r"(?i)[^.!?\n]*(?:\bchibi\b|\btiny (?:compact )?body\b|\bvisible feet\b|"
        r"\boversized head\b|\btwo to three head heights\b|\bno realistic adult proportions\b)[^.!?\n]*[.!?]?",
        "", prompt,
    ).strip()


STYLE_SEEDS = (
    ('hand-painted-animation', '晴空手绘动画', '自然手绘光感与清透水彩，温暖但不套用模板脸。', "Render the supplied person's ID portrait with warm hand-painted animated-film textures, watercolour and gouache, subtle linework and gentle natural colours. Draw the person's actual facial contours faithfully rather than substituting a stock animation face. Gently beautify with luminous watercolour skin washes, restrained rosy cheeks and delicate eye highlights without changing the features. Freely vary the airy background, palette and natural light; a watercolour sky is one possible setting."),
    ('american-comic', '高能漫画封面', '硬朗墨线、套色网点与明快节奏，保留本人的真实轮廓。', "Render the supplied person's ID portrait as American comic-book art with bold black ink contours, halftone shading and vivid colours. Ink the actual jaw, cheekbones and facial features without exaggerating them into a superhero face. Use flattering clean facial colour planes, gentle halftone shadows and lively eye highlights for light comic-style beautification; avoid harsh ink lines that deepen wrinkles or tired shadows. Freely vary lighting, colour treatment and restrained graphic background motifs."),
    ('hand-doodle', '开发者手账速写', '钢笔、彩铅与马克笔的轻松手账感，创意元素克制不遮脸。', "Render the supplied person's ID portrait with colourful marker strokes, pen linework and sketch texture. Trace the actual facial silhouette and feature placement faithfully. Use light warm marker skin tones, small playful eye highlights and delicate cheek colour for a fresh flattering finish; avoid dense facial scribbles or heavy tired-looking shading. Keep expressive scribbles and splashes mainly around the subject and away from the facial contours. Freely vary colours, paper texture and surrounding playful doodles."),
    ('professional-suit', '未来职业肖像', '克制的未来商务质感，深色西装与高级影棚光。', 'Create a confident professional photographic ID portrait of the supplied person, recognizable as the same person. Preserve natural facial structure and skin texture. Apply restrained professional retouching to even skin tone, soften temporary blemishes and tired shadows, and bring clarity to the eyes without glossy plastic skin or facial reshaping. Use refined studio or office background and flattering editorial lighting for a fresh business portrait variation each time.'),
    ('chinese-elegance', '东方新潮国风', '现代东方时尚插画与新中式剪裁，灵动、清雅、不影楼化。', "Create a lively contemporary Chinese-style collectible ID portrait of the supplied person, like a beautifully art-directed modern guofeng illustration rather than a formal costume photograph. Front-facing, looking directly at the camera, head upright, both eyes clearly visible, unobstructed face and anatomically accurate facial features. Keep the person's recognizable facial proportions, jawline, age, skin tone and original expression; bring warmth and life through clear eye highlights, fresh skin colour and relaxed clothing, not a replacement face. Use fine expressive linework, translucent mineral-pigment washes, delicate ink accents and soft luminous depth; keep the actual facial structure clear and lightly beautified. Invent one cohesive artistic interpretation on each generation: an airy bamboo-and-jade wash, luminous cinnabar and peach silk, flowing blue-green mountain pigments, or warm ivory and expressive ink are examples, not fixed templates. Let colour, fabric folds and sparse brush shapes suggest gentle movement while the person stays still and front-facing. Keep the background spacious and low-detail, with at most one poetic motif, clear separation from the hair and calm space near the upper edge. Technology is optional: if used, integrate a tiny geometric seam or pigment accent into the artwork. Give the model freedom in colour, material, brushwork and light; avoid repeating the same green stand-collar outfit and beige bamboo background for every person."),
    ('pixel-person', '复古像素头像', '真正的16-bit像素头像，清晰可爱但不把本人变成模板娃娃脸。', "Render the supplied person as an 8-bit or 16-bit pixel-art ID portrait. Use crisp pixel clusters, carefully stepped facial contours and a limited retro colour palette. Make visible square pixels and stepped edges the rendering language of the entire face, hair, clothing and background. Use a low-resolution pixel-art canvas enlarged with nearest-neighbour scaling, not a smooth anime painting with pixel decorations. Avoid smooth gradients, airbrushed skin, vector curves and painterly brushwork in the subject. Keep enough facial pixel detail to distinguish this person's actual eyes, nose, mouth and jawline. Make both modes cute, friendly and gently beautified using warm playful colours, clean flattering pixel shading, tiny eye-light pixels and restrained blush pixels. In standard mode retain natural adult head and eye size: cuteness comes from colour and rendering, not baby-face anatomy or an exaggerated head-to-body ratio. Translate the original clothing into pixel art without changing the garments, colours, patterns or accessories. Use a playful retro-game background for a fresh collectible variation each time."),
)


def _style_seed(style_id: str, title: str, description: str, base_prompt: str,
                *, is_default: bool, now: int) -> dict:
    identity = (
        "Preserve the subject's facial contour, jawline, cheekbones, face width, eye spacing, "
        "nose and mouth proportions, hairline, age and characteristic features. "
        "Keep the person dominant and the background secondary."
    )
    return {
        "id": style_id,
        "title": title,
        "description": description,
        "wide_prompt": base_prompt + clothing_prompt(style_id) + " " + LIGHT_BEAUTY + STYLE_BEAUTY[style_id] + (
            " Landscape ID-photo composition: exactly one person, shown from the chest up, "
            "with a complete head and shoulders and comfortable breathing room. " + identity),
        "vertical_prompt": base_prompt + clothing_prompt(style_id) + " " + LIGHT_BEAUTY + STYLE_BEAUTY[style_id] + (
            " Vertical ID-photo composition: exactly one person, centered and shown from the chest up, "
            "with a complete head and shoulders and comfortable breathing room. " + identity),
        "negative_prompt": clothing_negative_prompt(style_id)
            + " No generic replacement face, jaw slimming, reshaped cheekbones, altered hairline or age change.",
        "model": "gpt-image-1",
        "sizes": ["1536x1024", "1024x1536"],
        "backdrop_id": "dynamic-minimal",
        "default_orientation": "vertical",
        "preserve_identity": True,
        "single_person": True,
        "no_generated_text": True,
        "no_border": True,
        "enabled": True,
        "is_default": is_default,
        "revision": 1,
        "updated_at": now,
        "history": [],
    }


def build_portrait_prompt(spec: dict, orientation: str = "vertical", mode: str = "standard") -> str:
    portrait_mode_prompt(mode)
    orientation = "wide" if orientation == "wide" else "vertical"
    prompt_key = 'wide_prompt' if orientation == 'wide' else 'vertical_prompt'
    prompt = str(spec.get(prompt_key) or spec.get('vertical_prompt') or spec.get('wide_prompt') or spec.get('prompt') or '').strip()
    prompt = mode_neutral_prompt(prompt)
    beauty = STYLE_BEAUTY.get(str(spec.get('id') or ''))
    if beauty and beauty not in prompt:
        prompt += '\n' + beauty
    prompt += '\n' + REFERENCE_QUALITY
    prompt += '\n' + SCENE_RULES
    prompt += clothing_prompt(str(spec.get('id') or ''))
    negative_prompt = mode_neutral_prompt(str(spec.get('negative_prompt') or clothing_negative_prompt(str(spec.get('id') or ''))))
    if negative_prompt:
        prompt += '\n' + negative_prompt
    prompt += '\nFINAL PRIORITY: The selected portrait mode and likeness rules override any earlier instruction about proportions, framing or facial stylization. ' + portrait_mode_prompt(mode)
    if str(spec.get('id') or '') == 'pixel-person':
        prompt += "\nREQUIRED PIXEL TECHNIQUE IN EITHER MODE: Draw the entire portrait as actual low-resolution pixel art. The face, eyes, hair and clothes must all have clearly visible square pixel clusters and stepped edges, enlarged with nearest-neighbour scaling. No smooth anime face, airbrush, painted gradients or brush strokes. Use sufficient pixel detail to preserve the person's distinctive facial contour and feature placement. Make the portrait cute and gently beautified with flattering warm pixel colours, clear eye-light pixels and subtle blush pixels. Standard mode keeps natural adult head-to-shoulder proportions and eye size; do not convert a non-chibi choice into a baby face or oversized-head character."
    return prompt


def default_catalog() -> dict:
    now = int(time.time())
    return {
        "schema_version": 2,
        "preset_version": PRESET_VERSION,
        "version": 1,
        "updated_at": now,
        "styles": [
            _style_seed(style_id, title, description, prompt,
                        is_default=index == 0, now=now)
            for index, (style_id, title, description, prompt) in enumerate(STYLE_SEEDS)
        ],
    }


class CatalogError(ValueError):
    pass


class CatalogOperationError(CatalogError):
    def __init__(self, detail: str, code: str, http_status: int):
        super().__init__(detail)
        self.code = code
        self.http_status = http_status


class StyleCatalog:
    def __init__(self, root: Path | str):
        self.root = Path(root)
        self.path = self.root / "style-catalog.json"
        self._lock = threading.RLock()
        self._cached: dict | None = None
        self._mtime_ns: int | None = None

    def _ensure_file(self) -> None:
        if self.path.is_file():
            return
        self.root.mkdir(parents=True, exist_ok=True)
        self._atomic_write(default_catalog())

    def _atomic_write(self, value: dict) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
        descriptor = os.open(str(tmp), os.O_RDONLY)
        try:
            try:
                os.fsync(descriptor)
            except OSError:
                # Windows 的部分 Python 运行时不支持对只读描述符 fsync；
                # os.replace 仍保证读者不会看到半份 JSON。
                pass
        finally:
            os.close(descriptor)
        os.replace(tmp, self.path)
        self._cached = value
        self._mtime_ns = self.path.stat().st_mtime_ns

    @staticmethod
    def _migrate_pixel(value: dict) -> dict:
        if int(value.get("preset_version", 0)) >= 7:
            return StyleCatalog._migrate_chinese(value)
        now = int(time.time())
        seeds = {item["id"]: item for item in default_catalog()["styles"]}
        for item in value.get("styles", []):
            seed = seeds.get(item.get("id"))
            if seed is None:
                continue
            history = list(item.get("history") or [])
            history.append({key: entry for key, entry in item.items() if key != "history"})
            for key in ("description", "wide_prompt", "vertical_prompt", "negative_prompt"):
                item[key] = seed[key]
            if item.get("id") == "pixel-person" and item.get("title") == "Q版像素风":
                item["title"] = seed["title"]
            item["history"] = history
            item["revision"] = int(item.get("revision", 1)) + 1
            item["updated_at"] = now
        value["preset_version"] = 7
        value["version"] = int(value.get("version", 0)) + 1
        value["updated_at"] = now
        return StyleCatalog._migrate_chinese(value)

    @staticmethod
    def _migrate_chinese(value: dict) -> dict:
        previous_preset = int(value.get("preset_version", 0))
        if previous_preset >= PRESET_VERSION:
            return value
        now = int(time.time())
        styles = value.setdefault("styles", [])
        existing = {str(item.get("id") or ""): item for item in styles}
        deleted_ids = set(value.get("deleted_style_ids") or [])
        seeds = default_catalog()["styles"]
        refreshed: list[dict] = []
        changed = False
        refreshed_ids: set[str] = set()
        preset_fields = ("title", "description", "wide_prompt", "vertical_prompt", "negative_prompt",
                         "backdrop_id", "preserve_identity", "single_person", "no_generated_text", "no_border")
        for seed in seeds:
            style_id = seed["id"]
            if style_id in deleted_ids:
                continue
            old = existing.get(style_id)
            if old is None:
                item = dict(seed)
                item["is_default"] = False
                changed = True
            else:
                item = dict(old)
                if any(item.get(key) != seed.get(key) for key in preset_fields):
                    history = list(item.get("history") or [])
                    history.append({key: entry for key, entry in item.items() if key != "history"})
                    for key in preset_fields:
                        item[key] = seed[key]
                    item["history"] = history
                    item["revision"] = int(item.get("revision", 1)) + 1
                    item["updated_at"] = now
                    changed = True
            refreshed.append(item)
            refreshed_ids.add(style_id)

        for old in styles:
            style_id = str(old.get("id") or "")
            if style_id in refreshed_ids:
                continue
            item = dict(old)
            if style_id == "korean-pop" and (item.get("enabled") or item.get("is_default")):
                history = list(item.get("history") or [])
                history.append({key: entry for key, entry in item.items() if key != "history"})
                item.update(enabled=False, is_default=False, history=history,
                            revision=int(item.get("revision", 1)) + 1, updated_at=now)
                changed = True
            refreshed.append(item)

        defaults = [item for item in refreshed if item.get("is_default") and item.get("enabled")]
        if not defaults:
            replacement = next((item for item in refreshed if item.get("enabled")), None)
            if replacement is not None:
                replacement["is_default"] = True
                replacement["revision"] = int(replacement.get("revision", 1)) + 1
                replacement["updated_at"] = now
                changed = True
        elif len(defaults) > 1:
            for item in defaults[1:]:
                item["is_default"] = False
                item["revision"] = int(item.get("revision", 1)) + 1
                item["updated_at"] = now
                changed = True

        value["styles"] = refreshed
        value["preset_version"] = PRESET_VERSION
        if changed or previous_preset != PRESET_VERSION:
            value["version"] = int(value.get("version", 0)) + 1
            value["updated_at"] = now
        return value

    @staticmethod
    def _migrate(value: dict) -> dict:
        if not isinstance(value, dict):
            raise CatalogError("目录必须是对象")
        schema = int(value.get("schema_version", 0))
        if schema not in (1, 2):
            raise CatalogError("目录版本不支持")
        migrated = json.loads(json.dumps(value, ensure_ascii=False))
        if schema == 2 and int(migrated.get("preset_version", 0)) >= LEGACY_PRESET_VERSION:
            return StyleCatalog._migrate_pixel(migrated)
        if schema == 1:
            migrated["schema_version"] = 2
            for item in migrated.get("styles") or []:
                prompt = str(item.pop("prompt", "") or "").strip()
                item.setdefault("wide_prompt", prompt)
                item.setdefault("vertical_prompt", prompt)
                item.setdefault("negative_prompt", COMMON_NEGATIVE)
                item.setdefault("backdrop_id", "light-film-tech")
                item.setdefault("default_orientation", "vertical")
                item.setdefault("preserve_identity", True)
                item.setdefault("single_person", True)
                item.setdefault("no_generated_text", True)
                item.setdefault("no_border", True)

        # 旧目录可能含有已废弃的换装描述；活动六款会在下面按当前种子重建，
        # 历史版本原样保留。停用的旧实体只需能被安全读取，不能阻塞启动。
        for item in migrated.get("styles") or []:
            if item.get("id") == "professional-suit" or int(migrated.get("preset_version", 0)) >= LEGACY_PRESET_VERSION:
                continue
            for key in ("wide_prompt", "vertical_prompt"):
                prompt = str(item.get(key) or "").strip()
                if prompt:
                    item[key] = re.sub(
                        r"(?i)freely invent the outfit|freely invent clothing|"
                        r"wearing a well-fitted dark suit|wearing a suit|add a suit|"
                        r"change outfit|new outfit|different clothes|replace the clothing",
                        "preserve the supplied clothing",
                        prompt,
                    )

        styles = migrated.setdefault("styles", [])
        seed_prompts = {seed["id"]: seed for seed in default_catalog()["styles"]}
        for item in styles:
            seed = seed_prompts.get(str(item.get("id") or ""))
            if seed and int(migrated.get("preset_version", 0)) < LEGACY_PRESET_VERSION:
                for key in ("wide_prompt", "vertical_prompt", "negative_prompt"):
                    item[key] = seed[key]
        for item in styles:
            if item.get("backdrop_id") in (None, "", "light-film-tech", "film", "dark", "light"):
                item["backdrop_id"] = "dynamic-minimal"
        existing_ids = {str(item.get("id") or "") for item in styles}
        deleted_ids = set(migrated.get("deleted_style_ids") or [])
        for seed in default_catalog()["styles"]:
            if seed["id"] not in existing_ids and seed["id"] not in deleted_ids:
                seed["is_default"] = False
                styles.append(seed)
        if int(migrated.get("preset_version", 0)) < LEGACY_PRESET_VERSION:
            now = int(time.time())
            previous = {item["id"]: item for item in value.get("styles", [])}
            existing = {item["id"]: item for item in styles}
            active = []
            for seed in default_catalog()["styles"]:
                if seed["id"] in deleted_ids:
                    continue
                old = previous.get(seed["id"])
                source = old or previous.get(LEGACY_STYLE_IDS.get(seed["id"]))
                item = dict(existing[seed["id"]])
                history = list(item.get("history") or [])
                if old:
                    history.append({key: entry for key, entry in old.items()
                                    if key != "history"})
                elif source:
                    history = list(source.get("history") or [])
                    history.append({key: entry for key, entry in source.items()
                                    if key != "history"})
                    for key in ("model", "sizes", "default_orientation"):
                        if key in source:
                            item[key] = source[key]
                for key in ("title", "description", "wide_prompt", "vertical_prompt",
                            "negative_prompt", "enabled", "is_default"):
                    item[key] = seed[key]
                item["history"] = history
                item["revision"] = int(source.get("revision", 0)) + 1 if source else 1
                item["updated_at"] = now
                active.append(item)
            active_ids = {item["id"] for item in active}
            retired = []
            for item in styles:
                if item["id"] in active_ids:
                    continue
                if item.get("enabled") or item.get("is_default"):
                    history = list(item.get("history") or [])
                    history.append({key: entry for key, entry in previous[item["id"]].items()
                                    if key != "history"})
                    item["history"] = history
                    item["revision"] = int(item.get("revision", 1)) + 1
                    item["updated_at"] = now
                item["enabled"] = False
                item["is_default"] = False
                retired.append(item)
            migrated["styles"] = active + retired
            migrated["preset_version"] = PRESET_VERSION
            migrated["version"] = int(migrated.get("version", 0)) + 1
            migrated["updated_at"] = now
        return StyleCatalog._migrate_pixel(migrated)

    def _read(self) -> dict:
        self._ensure_file()
        stat = self.path.stat()
        if self._cached is not None and self._mtime_ns == stat.st_mtime_ns:
            return self._cached
        try:
            original = json.loads(self.path.read_text(encoding="utf-8"))
            value = self._migrate(original)
            self.validate_catalog(value)
            if value != original:
                self._atomic_write(value)
        except Exception as exc:
            if self._cached is not None:
                return self._cached
            raise CatalogError("风格目录不可用：%s" % exc) from exc
        self._cached = value
        self._mtime_ns = self.path.stat().st_mtime_ns
        return value

    def snapshot(self) -> dict:
        with self._lock:
            value = self._read()
            return json.loads(json.dumps(value, ensure_ascii=False))

    @staticmethod
    def _clean_prompt(style: dict, key: str, optional: bool = False) -> str:
        value = str(style.get(key) or "").strip()
        if optional and not value:
            return ""
        minimum = 1 if key == "negative_prompt" else 20
        if not minimum <= len(value) <= 12000:
            label = {"wide_prompt": "横版提示词", "vertical_prompt": "竖版提示词",
                     "negative_prompt": "避免内容"}[key]
            raise CatalogError("%s长度应为 %d 到 12000 个字符" % (label, minimum))
        return value

    @staticmethod
    def _lock_clothing_prompt(value: str, style_id: str = "") -> str:
        if style_id in ("professional-suit", "chinese-elegance"):
            value = value.replace(CLOTHING_LOCK.strip(), "").strip()
        lowered = value.lower()
        forbidden = (
            "freely invent the outfit", "freely invent clothing", "change outfit",
            "new outfit", "different clothes", "wearing a well-fitted dark suit",
            "wearing a suit", "add a suit", "replace the clothing")
        if style_id not in ("professional-suit", "chinese-elegance") and any(term in lowered for term in forbidden):
            raise CatalogError("风格提示词不能要求更换服装；必须保持原服装不变")
        rule = clothing_prompt(style_id)
        before, present, after = value.partition(rule.strip())
        if present:
            return before + present + after.replace(present, "")
        return value.rstrip(" .") + "." + rule

    @staticmethod
    def validate_style(style: dict, existing_ids: set[str] | None = None) -> dict:
        if not isinstance(style, dict):
            raise CatalogError("风格必须是对象")
        style_id = str(style.get("id") or "").strip()
        if not ID_RE.fullmatch(style_id):
            raise CatalogError("风格 ID 只能使用小写字母、数字和短横线")
        if existing_ids and style_id in existing_ids:
            raise CatalogError("风格 ID 已存在")
        title = str(style.get("title") or "").strip()
        if not 1 <= len(title) <= 80:
            raise CatalogError("风格名称长度应为 1 到 80 个字符")
        description = str(style.get("description") or "").strip()
        if len(description) > 240:
            raise CatalogError("风格说明不能超过 240 个字符")
        wide_prompt = StyleCatalog._clean_prompt(style, "wide_prompt", optional=True)
        if wide_prompt:
            wide_prompt = StyleCatalog._lock_clothing_prompt(wide_prompt, style_id)
        vertical_prompt = StyleCatalog._lock_clothing_prompt(
            StyleCatalog._clean_prompt(style, "vertical_prompt"), style_id)
        negative_prompt = StyleCatalog._clean_prompt(style, "negative_prompt", optional=True)
        if negative_prompt and style_id in ("professional-suit", "chinese-elegance"):
            required_negative = clothing_negative_prompt(style_id).strip()
            negative_prompt = negative_prompt.replace(COMMON_NEGATIVE.strip(), "").strip()
            negative_prompt = negative_prompt.replace(CLOTHING_NEGATIVE.strip(), "").strip()
            if required_negative not in negative_prompt:
                negative_prompt = negative_prompt.rstrip(" .") + ". " + required_negative
        elif negative_prompt:
            required_negative = COMMON_NEGATIVE.strip()
            if required_negative not in negative_prompt:
                negative_prompt = negative_prompt.rstrip(" .") + ". " + required_negative
        model = str(style.get("model") or "gpt-image-1").strip()
        if not MODEL_RE.fullmatch(model):
            raise CatalogError("模型名称格式不正确")
        sizes = style.get("sizes") or ["1024x1536"]
        if not isinstance(sizes, list) or not sizes or len(sizes) > 4:
            raise CatalogError("至少保留一种输出尺寸，最多四种")
        if any(not isinstance(size, str) or not SIZE_RE.fullmatch(size) for size in sizes):
            raise CatalogError("输出尺寸格式应为 1024x1536")
        backdrop_id = str(style.get("backdrop_id") or "").strip()
        if not ID_RE.fullmatch(backdrop_id):
            raise CatalogError("背景 ID 格式不正确")
        default_orientation = str(style.get("default_orientation") or "vertical").strip()
        if default_orientation not in ORIENTATIONS:
            raise CatalogError("默认版式只能是 wide 或 vertical")
        result = dict(style)
        result.pop("prompt", None)
        result.update({
            "id": style_id, "title": title, "description": description,
            "wide_prompt": wide_prompt, "vertical_prompt": vertical_prompt,
            "negative_prompt": negative_prompt, "model": model,
            "sizes": list(dict.fromkeys(sizes)), "backdrop_id": backdrop_id,
            "default_orientation": default_orientation,
            "preserve_identity": bool(style.get("preserve_identity", True)),
            "single_person": bool(style.get("single_person", True)),
            "no_generated_text": bool(style.get("no_generated_text", True)),
            "no_border": bool(style.get("no_border", True)),
            "enabled": bool(style.get("enabled", True)),
            "is_default": bool(style.get("is_default", False)),
            "revision": max(1, int(style.get("revision", 1))),
            "updated_at": int(style.get("updated_at") or time.time()),
        })
        if not all(result[key] for key in
                   ("preserve_identity", "single_person", "no_generated_text", "no_border")):
            raise CatalogError("风格必须保留本人、限制单人，并禁止模型文字和边框")
        return result

    @classmethod
    def validate_catalog(cls, value: dict) -> dict:
        if not isinstance(value, dict) or value.get("schema_version") != 2:
            raise CatalogError("目录版本不支持")
        styles = value.get("styles")
        if not isinstance(styles, list) or not styles or len(styles) > 50:
            raise CatalogError("目录至少需要一条风格")
        ids: set[str] = set()
        defaults = 0
        for item in styles:
            cls.validate_style(item, ids)
            ids.add(item["id"])
            defaults += int(bool(item.get("is_default")))
        if defaults > 1:
            raise CatalogError("只能有一个默认风格")
        if not any(item.get("enabled") for item in styles):
            raise CatalogError("至少保留一条启用风格")
        return value

    def public(self) -> list[dict]:
        with self._lock:
            styles = {item["id"]: item for item in self._read()["styles"]}
            order = [style_id for style_id, *_rest in STYLE_SEEDS if style_id in styles]
            order.extend(style_id for style_id in styles if style_id not in order)
            return [{k: item[k] for k in
                     ("id", "title", "description", "sizes", "default_orientation", "is_default")}
                    for style_id in order
                    if (item := styles.get(style_id)) and item.get("enabled")]

    def save(self, styles: list[dict]) -> dict:
        with self._lock:
            current = self._read()
            normalized = []
            ids: set[str] = set()
            previous = {item["id"]: item for item in current["styles"]}
            for item in styles:
                clean = self.validate_style(item, ids)
                ids.add(clean["id"])
                old = previous.get(clean["id"])
                history = list(old.get("history", [])) if old else list(clean.get("history", []))
                if old and any(old.get(key) != clean.get(key) for key in TRACKED_FIELDS):
                    history.append({key: old.get(key) for key in
                                    ("id", *TRACKED_FIELDS, "revision", "updated_at")})
                clean["history"] = history
                normalized.append(clean)
            for style_id, old in previous.items():
                if style_id not in ids and not old.get("enabled"):
                    retained = dict(old)
                    retained["enabled"] = False
                    retained["is_default"] = False
                    normalized.append(retained)
            if not any(item.get("enabled") for item in normalized):
                raise CatalogOperationError("至少保留一条启用风格", "last_enabled_style", 409)
            value = {**current, "schema_version": 2, "preset_version": PRESET_VERSION,
                     "version": int(current.get("version", 0)) + 1,
                     "updated_at": int(time.time()), "styles": normalized,
                     "deleted_style_ids": sorted(
                         (set(current.get("deleted_style_ids") or []) | set(previous))
                         - {item["id"] for item in normalized})}
            self.validate_catalog(value)
            self._atomic_write(value)
            return self.snapshot()

    def delete(self, style_id: str) -> dict:
        with self._lock:
            current = self.snapshot()
            target = next((item for item in current["styles"] if item["id"] == style_id), None)
            if target is None:
                raise CatalogOperationError("找不到要删除的风格", "style_not_found", 404)
            remaining = [item for item in current["styles"] if item["id"] != style_id]
            enabled = [item for item in remaining if item.get("enabled")]
            if not enabled:
                raise CatalogOperationError("至少保留一条启用风格", "last_enabled_style", 409)
            now = int(time.time())
            if target.get("is_default"):
                replacement = enabled[0]
                history = list(replacement.get("history") or [])
                history.append({key: entry for key, entry in replacement.items() if key != "history"})
                replacement.update(is_default=True, history=history,
                                   revision=int(replacement.get("revision", 1)) + 1, updated_at=now)
            current.update(styles=remaining, version=int(current.get("version", 0)) + 1,
                           updated_at=now, deleted_style_ids=sorted(
                               set(current.get("deleted_style_ids") or []) | {style_id}))
            self.validate_catalog(current)
            self._atomic_write(current)
            return self.snapshot()

    def upsert(self, style: dict, style_id: str | None = None) -> dict:
        with self._lock:
            current = self.snapshot()
            items = current["styles"]
            # 未带路径 ID 的 POST 明确表示新增，即使表单里带了建议 ID。
            target = str(style_id or "").strip()
            if target:
                found = next((item for item in items if item["id"] == target), None)
                if found is None:
                    raise CatalogError("找不到要修改的风格")
                merged = dict(found)
                merged.update(style)
                merged["id"] = target
                merged["revision"] = int(found.get("revision", 1)) + 1
                items[items.index(found)] = merged
            else:
                clean = dict(style)
                clean["revision"] = 1
                items.append(clean)
            return self.save(items)

    def rollback(self, style_id: str, revision: int | None = None) -> dict:
        with self._lock:
            current = self.snapshot()
            item = next((entry for entry in current["styles"] if entry["id"] == style_id), None)
            if item is None:
                raise CatalogError("找不到要恢复的风格")
            history = item.get("history") or []
            if revision is None:
                target = history[-1] if history else None
            else:
                target = next((entry for entry in reversed(history)
                               if int(entry.get("revision", 0)) == int(revision)), None)
            if target is None:
                raise CatalogError("没有可恢复的上一版本")
            for key, value in target.items():
                if key not in ("id", "history", "revision", "updated_at"):
                    item[key] = value
            item["revision"] = int(item.get("revision", 1)) + 1
            return self.save(current["styles"])


def catalog_path(root: Path | str) -> Path:
    return Path(root) / "style-catalog.json"
