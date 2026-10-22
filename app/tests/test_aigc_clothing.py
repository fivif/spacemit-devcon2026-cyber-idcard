"""通过真实目录和运行时方法验证最终衣服提示词，网关使用 mock。"""

from __future__ import annotations

import base64
import io
import importlib
import json
import os
import sys
import tempfile
import unittest
from email import policy
from email.parser import BytesParser
from itertools import product
from pathlib import Path
from unittest.mock import patch

from PIL import Image

APP_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP_DIR))

from aigc_catalog import (CHINESE_CLOTHING, CLOTHING_LOCK, LIGHT_BEAUTY, PRESET_VERSION,
                          REFERENCE_QUALITY,
                          STANDARD_BEAUTY,
                          SCENE_RULES, STYLE_BEAUTY, SUIT_CLOTHING, CatalogError, StyleCatalog,
                          build_portrait_prompt, default_catalog, mode_neutral_prompt)


STYLE_RENDERING = {
    "hand-painted-animation": "watercolour",
    "american-comic": "halftone",
    "hand-doodle": "sketch texture",
    "professional-suit": "professional",
    "chinese-elegance": "mineral-pigment",
    "pixel-person": "16-bit pixel-art",
}
EXPECTED_STYLES = [
    ("hand-painted-animation", "晴空手绘动画"),
    ("american-comic", "高能漫画封面"),
    ("hand-doodle", "开发者手账速写"),
    ("professional-suit", "未来职业肖像"),
    ("chinese-elegance", "东方新潮国风"),
    ("pixel-person", "复古像素头像"),
]


class ClothingPromptTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp_data = tempfile.TemporaryDirectory(prefix="aigc-clothing-")
        with patch.dict(os.environ, {"BOOTH_DATA": cls.temp_data.name, "BOOTH_CAMERA": "mock"}):
            cls.server = importlib.import_module("booth_server")
            cls.client = importlib.import_module("aigc_client")

    @classmethod
    def tearDownClass(cls):
        cls.temp_data.cleanup()

    def assert_clothing_rules(self, prompt, style_id):
        if style_id == "professional-suit":
            self.assertGreaterEqual(prompt.count(SUIT_CLOTHING.strip()), 1)
            self.assertIn("No missing suit jacket, dress shirt or tie", prompt)
            self.assertNotIn(CLOTHING_LOCK.strip(), prompt)
            self.assertNotIn(CHINESE_CLOTHING.strip(), prompt)
        elif style_id == "chinese-elegance":
            self.assertGreaterEqual(prompt.count(CHINESE_CLOTHING.strip()), 1)
            self.assertIn("No business suit", prompt)
            self.assertNotIn(CLOTHING_LOCK.strip(), prompt)
            self.assertNotIn(SUIT_CLOTHING.strip(), prompt)
        else:
            self.assertGreaterEqual(prompt.count(CLOTHING_LOCK.strip()), 1)
            self.assertNotIn(SUIT_CLOTHING.strip(), prompt)
            self.assertNotIn(CHINESE_CLOTHING.strip(), prompt)
        self.assertEqual(prompt.count(SCENE_RULES.strip()), 1)
        self.assertGreaterEqual(prompt.count(LIGHT_BEAUTY.strip()), 1)
        if style_id in STYLE_BEAUTY:
            self.assertEqual(prompt.count(STYLE_BEAUTY[style_id]), 1)
        self.assertIn("exactly one person", prompt)
        self.assertIn("complete head and shoulders", prompt)
        self.assertIn("near the upper edge", prompt)
        self.assertNotIn("upper fifth", prompt)
        self.assertLess(len(prompt), 12000)

    def test_low_resolution_reference_gets_quality_reconstruction(self):
        prompt = build_portrait_prompt({"id": "hand-painted-animation",
                                        "vertical_prompt": "A clean portrait."})
        self.assertEqual(prompt.count(REFERENCE_QUALITY.strip()), 1)
        for detail in ("crisp eyes", "natural hair strands", "JPEG blocks", "double features"):
            self.assertIn(detail, prompt)

    def test_standard_mode_adds_editorial_beauty_without_changing_chibi_prompt(self):
        spec = {"id": "professional-suit", "wide_prompt": "A polished portrait.",
                "vertical_prompt": "A polished portrait."}
        standard = build_portrait_prompt(spec, mode="standard")
        chibi = build_portrait_prompt(spec, mode="chibi")
        self.assertEqual(standard.count(STANDARD_BEAUTY.strip()), 1)
        self.assertNotIn(STANDARD_BEAUTY.strip(), chibi)
        self.assertIn("tasteful style-appropriate makeup", standard)
        self.assertIn("same recognizable person", standard)

    def test_style_beauty_reaches_saved_and_custom_prompts_in_both_modes(self):
        for style_id, orientation, mode in product(STYLE_BEAUTY, ("wide", "vertical"), ("standard", "chibi")):
            with self.subTest(style=style_id, orientation=orientation, mode=mode):
                spec = {"id": style_id, "wide_prompt": "Custom lighting and original composition.",
                        "vertical_prompt": "Custom lighting and original composition."}
                original = dict(spec)
                prompt = build_portrait_prompt(spec, orientation, mode)
                self.assertEqual(prompt.count(STYLE_BEAUTY[style_id]), 1)
                self.assertIn("Custom lighting and original composition.", prompt)
                self.assertIn("preserve facial contour", prompt)
                self.assertEqual(spec, original)
                spec[orientation + "_prompt"] += " " + STYLE_BEAUTY[style_id]
                self.assertEqual(build_portrait_prompt(spec, orientation, mode).count(STYLE_BEAUTY[style_id]), 1)

    def assert_portrait_mode(self, prompt, portrait_mode):
        self.assertIn("FINAL PRIORITY", prompt)
        self.assertIn("override any earlier", prompt.lower())
        self.assertIn("SELECTED MODE:", prompt)
        self.assertEqual(prompt.count("SELECTED MODE:"), 1)
        self.assertEqual(prompt.count("Preserve identity: the reference photograph is the sole authority"), 1)
        for feature in ("jawline", "chin", "cheekbones", "face width", "eye spacing",
                        "nose and mouth", "hairline", "hairstyle", "age", "expression", "glasses"):
            self.assertIn(feature, prompt)
        for obsolete in ("tiny compact body", "two to three head heights", "no realistic adult proportions"):
            self.assertNotIn(obsolete, prompt.lower())
        if portrait_mode == "chibi":
            self.assertIn("MODE: CHIBI LARGE-HEAD AVATAR", prompt)
            self.assertIn("Enlarge the head as a whole", prompt)
            self.assertIn("small shoulders", prompt)
            self.assertIn("distinctive face shape and relative feature placement", prompt)
            self.assertIn("Do not create a full-body little character", prompt)
            self.assertIn("selected art style's own drawing technique and textures", prompt)
            self.assertNotIn("MODE: STANDARD", prompt)
        else:
            self.assertIn("MODE: STANDARD NON-CHIBI ID PORTRAIT", prompt)
            self.assertIn("head-to-shoulder proportions", prompt)
            self.assertIn("shown from the chest up", prompt)
            self.assertIn("No chibi proportions, oversized head", prompt)
            self.assertNotIn("MODE: CHIBI", prompt)

    def assert_pixel_technique(self, prompt):
        self.assertIn("REQUIRED PIXEL TECHNIQUE", prompt)
        for technique in ("16-bit pixel-art ID portrait", "visible square pixel clusters",
                          "stepped edges", "limited retro colour palette",
                          "nearest-neighbour scaling", "No smooth anime face"):
            self.assertIn(technique, prompt)

    def run_scene_matrix(self, catalog, global_model=""):
        booth = object.__new__(self.server.Booth)
        booth.style_catalog = catalog
        photo, alpha, generated = object(), object(), object()
        for spec in catalog.snapshot()["styles"]:
            if not spec["enabled"]:
                continue
            for orientation, portrait_mode in product(("wide", "vertical"), ("standard", "chibi")):
                with self.subTest(style=spec["id"], orientation=orientation, mode=portrait_mode,
                                  global_model=global_model):
                    with patch.object(self.client, "enabled", return_value=True), patch.object(
                        self.client, "edit_portrait", return_value=generated
                    ) as gateway, patch.object(
                        self.server, "read_settings", return_value={"aigc_global_model": global_model}
                    ):
                        result = booth._aigc_scene(spec["id"], photo, alpha, orientation, portrait_mode)
                    self.assertIs(result, generated)
                    gateway.assert_called_once()
                    self.assertEqual(gateway.call_args.args, (photo, alpha))
                    payload = gateway.call_args.kwargs
                    self.assert_clothing_rules(payload["prompt"], spec["id"])
                    self.assert_portrait_mode(payload["prompt"], portrait_mode)
                    expected_style = mode_neutral_prompt(spec[orientation + "_prompt"])
                    expected_negative = mode_neutral_prompt(spec["negative_prompt"])
                    self.assertEqual(payload["prompt"].count(expected_style), 1)
                    self.assertEqual(payload["prompt"].count(expected_negative), 1)
                    if spec["id"] in STYLE_RENDERING:
                        self.assertIn(STYLE_RENDERING[spec["id"]], payload["prompt"].lower())
                    if spec["id"] == "pixel-person":
                        self.assertIn("cute", payload["prompt"].lower())
                        self.assert_pixel_technique(payload["prompt"])
                    self.assertEqual(payload["model"], os.environ.get("AIGC_MODEL", "gpt-image-1"))
                    self.assertIn(payload["size"], spec["sizes"])
                    width, height = (int(part) for part in payload["size"].split("x"))
                    self.assertEqual(width >= height, orientation == "wide")

    def test_default_catalog_final_prompt(self):
        with tempfile.TemporaryDirectory() as tmp:
            catalog = StyleCatalog(tmp)
            self.assertEqual([(item["id"], item["title"]) for item in catalog.snapshot()["styles"]],
                             EXPECTED_STYLES)
            self.assertEqual(list(STYLE_RENDERING), [style_id for style_id, _title in EXPECTED_STYLES])
            self.run_scene_matrix(catalog)

    def test_all_styles_use_priority_channel_and_its_selected_model(self):
        from aigc_channels import AIGCChannels

        with tempfile.TemporaryDirectory() as tmp:
            booth = object.__new__(self.server.Booth)
            booth.style_catalog = StyleCatalog(tmp)
            booth.aigc_channels = AIGCChannels(tmp)
            with patch.object(booth.aigc_channels, "_ensure_monitor"):
                booth.aigc_channels.save({"channels": [
                    {"id": "primary", "name": "主用", "base_url": "https://primary.invalid/v1",
                     "model": "selected-primary", "api_key": "offline-key", "priority": 1},
                    {"id": "backup", "name": "候补", "base_url": "https://backup.invalid/v1",
                     "model": "selected-backup", "api_key": "offline-key", "priority": 2},
                ]})
            photo = Image.new("RGB", (24, 32), "navy")
            alpha = Image.new("L", photo.size, 255)
            generated = Image.new("RGB", (32, 48), "orange")
            with patch.object(self.client, "enabled", return_value=True), patch.object(
                self.server, "read_settings", return_value={"aigc_global_model": "selected-backup"}
            ), patch.object(self.client, "_upload_image", return_value=b"offline-image"), patch.object(
                self.client, "_request_edit", return_value=generated
            ) as request:
                for spec in booth.style_catalog.snapshot()["styles"]:
                    if spec["enabled"]:
                        with self.subTest(style=spec["id"]):
                            self.assertIs(booth._aigc_scene(spec["id"], photo, alpha), generated)
                            recent_calls = request.call_args_list[-2:]
                            self.assertEqual({call.kwargs["model"] for call in recent_calls},
                                             {"selected-primary"})
                            self.assertEqual({call.kwargs["endpoint_base"] for call in recent_calls},
                                             {"https://primary.invalid/v1", "https://backup.invalid/v1"})
                booth.aigc_channels.record_result("primary", False, "offline failure")
                self.assertIs(booth._aigc_scene("pixel-person", photo, alpha), generated)
                self.assertEqual(request.call_args.kwargs["model"], "selected-backup")
                self.assertEqual(request.call_args.kwargs["endpoint_base"], "https://backup.invalid/v1")

    def test_omitted_mode_defaults_to_standard_for_all_styles_and_orientations(self):
        with tempfile.TemporaryDirectory() as tmp:
            booth = object.__new__(self.server.Booth)
            booth.style_catalog = StyleCatalog(tmp)
            photo, alpha = object(), object()
            for style_id, orientation in product(STYLE_RENDERING, ("wide", "vertical")):
                with self.subTest(style=style_id, orientation=orientation), patch.object(
                    self.client, "enabled", return_value=True
                ), patch.object(self.client, "edit_portrait") as gateway, patch.object(
                    self.server, "read_settings", return_value={}
                ):
                    booth._aigc_scene(style_id, photo, alpha, orientation)
                    default_payload = gateway.call_args.kwargs
                    self.assert_portrait_mode(default_payload["prompt"], "standard")
                    booth._aigc_scene(style_id, photo, alpha, orientation, "standard")
                    self.assertEqual(gateway.call_args.kwargs, default_payload)

    def test_invalid_portrait_mode_never_calls_gateway(self):
        with tempfile.TemporaryDirectory() as tmp:
            booth = object.__new__(self.server.Booth)
            booth.style_catalog = StyleCatalog(tmp)
            for portrait_mode in ("", "q版", "CHIBI", "full-body", None):
                with self.subTest(mode=portrait_mode), patch.object(
                    self.client, "enabled", return_value=True
                ), patch.object(self.client, "edit_portrait") as gateway:
                    with self.assertRaisesRegex(ValueError, "人像模式"):
                        booth._aigc_scene("pixel-person", object(), object(), portrait_mode=portrait_mode)
                    gateway.assert_not_called()

    def test_pixel_copy_final_prompt_honors_both_modes_and_original_clothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            catalog = StyleCatalog(tmp)
            pixel = next(item for item in catalog.snapshot()["styles"] if item["id"] == "pixel-person")
            catalog.upsert(dict(pixel, id="pixel-person-copy", title="像素副本", is_default=False))
            booth = object.__new__(self.server.Booth)
            booth.style_catalog = catalog
            photo, alpha, generated = object(), object(), object()
            for orientation, portrait_mode in product(("wide", "vertical"), ("standard", "chibi")):
                with self.subTest(orientation=orientation, mode=portrait_mode):
                    with patch.object(self.client, "enabled", return_value=True), patch.object(
                        self.client, "edit_portrait", return_value=generated
                    ) as gateway, patch.object(self.server, "read_settings", return_value={}):
                        result = booth._aigc_scene("pixel-person-copy", photo, alpha, orientation, portrait_mode)
                    self.assertIs(result, generated)
                    gateway.assert_called_once()
                    prompt = gateway.call_args.kwargs["prompt"]
                    self.assert_clothing_rules(prompt, "pixel-person-copy")
                    self.assert_portrait_mode(prompt, portrait_mode)
                    self.assertTrue(prompt.startswith(mode_neutral_prompt(pixel[orientation + "_prompt"])))
                    self.assertEqual(prompt.count(mode_neutral_prompt(pixel[orientation + "_prompt"])), 1)
                    self.assertEqual(prompt.count(mode_neutral_prompt(pixel["negative_prompt"])), 1)
                    self.assertNotIn("Replace the person's original outfit", prompt)

    def test_actual_catalog_copy_final_prompt(self):
        source = APP_DIR / "data" / "style-catalog.json"
        original = source.read_bytes()
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "style-catalog.json").write_bytes(original)
            catalog = StyleCatalog(tmp)
            self.assertEqual(catalog.snapshot()["preset_version"], PRESET_VERSION)
            self.run_scene_matrix(catalog)
            self.run_scene_matrix(catalog, "onsite-global-model")
        self.assertEqual(source.read_bytes(), original)

    def test_actual_catalog_final_http_request(self):
        source = APP_DIR / "data" / "style-catalog.json"
        original = source.read_bytes()
        result_image = Image.new("RGB", (24, 32), (20, 60, 100))
        result_buffer = io.BytesIO()
        result_image.save(result_buffer, "PNG")
        response_body = json.dumps({"data": [{
            "b64_json": base64.b64encode(result_buffer.getvalue()).decode("ascii")
        }]}).encode("utf-8")
        photo = Image.new("RGB", (80, 120), (40, 100, 60))
        alpha = Image.new("L", photo.size, 0)
        alpha.paste(255, (16, 12, 64, 112))
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "style-catalog.json").write_bytes(original)
            key_path = root / "test-key.txt"
            key_path.write_text("offline-test-key", encoding="utf-8")
            catalog = StyleCatalog(root)
            migrated = catalog.snapshot()
            self.assertEqual(migrated["preset_version"], PRESET_VERSION)
            before = {item["id"]: item for item in json.loads(original)["styles"]}
            booth = object.__new__(self.server.Booth)
            booth.style_catalog = catalog
            for global_model in ("", "onsite-global-model"):
                for spec in migrated["styles"]:
                    if not spec["enabled"]:
                        continue
                    if spec["id"] in before:
                        self.assertEqual(spec["model"], before[spec["id"]]["model"])
                    if spec["id"] in before:
                        self.assertEqual(spec["sizes"], before[spec["id"]]["sizes"])
                    for orientation, portrait_mode in product(("wide", "vertical"), ("standard", "chibi")):
                        with self.subTest(style=spec["id"], orientation=orientation, mode=portrait_mode,
                                          global_model=global_model):
                            with patch.dict(os.environ, {
                                "BOOTH_DATA": tmp,
                                "AIGC_ENABLED": "1",
                                "AIGC_BACKEND": "public_api",
                                "AIGC_ENDPOINT": "https://aigc.invalid/v1",
                                "AIGC_MODEL": "unified-environment-model",
                                "AIGC_API_KEY_FILE": str(key_path),
                                "AIGC_TIMEOUT": "30",
                            }), patch.object(
                                self.server, "read_settings", return_value={"aigc_global_model": global_model}
                            ), patch.object(
                                self.client.urllib.request, "urlopen", return_value=io.BytesIO(response_body)
                            ) as transport:
                                result = booth._aigc_scene(spec["id"], photo, alpha, orientation, portrait_mode)
                            self.assertEqual(result.tobytes(), result_image.tobytes())
                            transport.assert_called_once()
                            request = transport.call_args.args[0]
                            self.assertEqual(request.get_method(), "POST")
                            self.assertEqual(request.full_url, "https://aigc.invalid/v1/images/edits")
                            self.assertEqual(request.get_header("Authorization"), "Bearer offline-test-key")
                            self.assertEqual(transport.call_args.kwargs["timeout"], 30)
                            content_type = request.get_header("Content-type")
                            message = BytesParser(policy=policy.default).parsebytes(
                                ("Content-Type: " + content_type + "\r\nMIME-Version: 1.0\r\n\r\n").encode()
                                + request.data
                            )
                            parts = {part.get_param("name", header="content-disposition"): part
                                     for part in message.iter_parts()}
                            self.assertEqual(set(parts), {"model", "prompt", "size", "n", "image"})
                            fields = {name: part.get_payload(decode=True).decode("utf-8")
                                      for name, part in parts.items() if name != "image"}
                            self.assert_clothing_rules(fields["prompt"], spec["id"])
                            self.assert_portrait_mode(fields["prompt"], portrait_mode)
                            if spec["id"] == "pixel-person":
                                self.assertIn("cute", fields["prompt"].lower())
                                self.assert_pixel_technique(fields["prompt"])
                            self.assertTrue(fields["prompt"].startswith(mode_neutral_prompt(spec[orientation + "_prompt"])))
                            self.assertEqual(fields["prompt"].count(
                                mode_neutral_prompt(spec[orientation + "_prompt"])), 1)
                            self.assertEqual(fields["prompt"].count(
                                mode_neutral_prompt(spec["negative_prompt"])), 1)
                            self.assertEqual(fields["model"], "unified-environment-model")
                            self.assertEqual(fields["n"], "1")
                            preferred = [size for size in spec["sizes"]
                                         if (int(size.split("x")[0]) >= int(size.split("x")[1]))
                                         == (orientation == "wide")]
                            self.assertEqual(fields["size"], (preferred or spec["sizes"])[0])
                            self.assertEqual(parts["image"].get_filename(), "portrait.jpg")
                            with Image.open(io.BytesIO(parts["image"].get_payload(decode=True))) as upload:
                                self.assertEqual(upload.format, "JPEG")
                                self.assertEqual(upload.size, tuple(int(part) for part in fields["size"].split("x")))
            persisted = (root / "style-catalog.json").read_bytes()
            self.assertEqual(StyleCatalog(root).snapshot(), migrated)
            self.assertEqual((root / "style-catalog.json").read_bytes(), persisted)
        self.assertEqual(source.read_bytes(), original)

    def test_repeated_save_keeps_prompt_and_history_stable(self):
        with tempfile.TemporaryDirectory() as tmp:
            catalog = StyleCatalog(tmp)
            original = catalog.snapshot()["styles"]
            for iteration in range(3):
                saved = catalog.save(catalog.snapshot()["styles"])
                for item, before in zip(saved["styles"], original):
                    with self.subTest(style=item["id"], iteration=iteration):
                        for key in ("wide_prompt", "vertical_prompt", "negative_prompt", "history"):
                            self.assertEqual(item[key], before[key])
            self.run_scene_matrix(catalog)

    def test_current_suit_prompt_not_rewritten_on_reload(self):
        with tempfile.TemporaryDirectory() as tmp:
            catalog = StyleCatalog(tmp)
            suit = next(item for item in default_catalog()["styles"] if item["id"] == "professional-suit")
            suit["wide_prompt"] = (
                "Create a professional portrait wearing a suit and keep the face recognizable. "
                + suit["wide_prompt"]
            )
            catalog.upsert(suit, "professional-suit")
            saved = catalog.snapshot()
            reopened = StyleCatalog(tmp).snapshot()
            self.assertEqual(reopened, saved)
            self.assertIn("wearing a suit", next(
                item for item in reopened["styles"] if item["id"] == "professional-suit"
            )["wide_prompt"])
            self.run_scene_matrix(catalog)

    def test_ordinary_styles_reject_outfit_changes(self):
        for item in default_catalog()["styles"]:
            if item["id"] in ("professional-suit", "chinese-elegance"):
                continue
            with self.subTest(style=item["id"]):
                with self.assertRaises(CatalogError):
                    StyleCatalog.validate_style(dict(
                        item, wide_prompt="Create a new outfit with a suit and tie for this portrait."
                    ))

    def test_legacy_rollback_and_copy_remove_conflicting_modes_without_override_blocks(self):
        with tempfile.TemporaryDirectory() as tmp:
            legacy = default_catalog()
            legacy["preset_version"] = 5
            for item in legacy["styles"]:
                for key in ("wide_prompt", "vertical_prompt"):
                    item[key] += (
                        " Create a cute chibi with an oversized head and tiny compact body. "
                        "Use two to three head heights and visible feet. "
                        "Preserve pose, body proportions, framing and skin details."
                    )
                item["negative_prompt"] += " No realistic adult proportions. No chibi head-and-shoulders crop."
            path = Path(tmp) / "style-catalog.json"
            path.write_text(json.dumps(legacy), encoding="utf-8")
            catalog = StyleCatalog(tmp)
            catalog.snapshot()
            for item in legacy["styles"]:
                restored = catalog.rollback(item["id"])
                restored_item = next(spec for spec in restored["styles"] if spec["id"] == item["id"])
                self.assertEqual(restored_item["wide_prompt"], item["wide_prompt"])
                if item["id"] == "pixel-person":
                    catalog.upsert(dict(restored_item, id="pixel-person-copy", title="旧像素副本",
                                        is_default=False))
            before = catalog.snapshot()
            self.run_scene_matrix(catalog)
            self.assertEqual(catalog.snapshot(), before)

    def test_rollback_restores_previous_prompt_without_duplicate_history(self):
        with tempfile.TemporaryDirectory() as tmp:
            catalog = StyleCatalog(tmp)
            original = catalog.snapshot()["styles"][0]
            catalog.upsert(dict(original, wide_prompt=(
                "Create a bright pixel portrait with a quiet background and natural lighting."
            )), original["id"])
            edited = catalog.snapshot()
            saved = catalog.save(edited["styles"])
            self.assertEqual(saved["styles"][0]["history"], edited["styles"][0]["history"])
            self.assertEqual(len(saved["styles"][0]["history"]), 1)
            restored = catalog.rollback(original["id"])["styles"][0]
            self.assertEqual(restored["wide_prompt"], original["wide_prompt"])
            self.assertEqual(restored["wide_prompt"].count(CLOTHING_LOCK.strip()), 1)

    def test_suit_rollback_from_v3_keeps_suit_policy(self):
        with tempfile.TemporaryDirectory() as tmp:
            legacy = default_catalog()
            legacy["preset_version"] = 3
            suit = next(item for item in legacy["styles"] if item["id"] == "professional-suit")
            suit["wide_prompt"] = suit["wide_prompt"].replace(SUIT_CLOTHING, CLOTHING_LOCK)
            suit["vertical_prompt"] = suit["vertical_prompt"].replace(SUIT_CLOTHING, CLOTHING_LOCK)
            suit["negative_prompt"] = legacy["styles"][0]["negative_prompt"]
            path = Path(tmp) / "style-catalog.json"
            path.write_text(json.dumps(legacy, ensure_ascii=False), encoding="utf-8")
            catalog = StyleCatalog(tmp)
            migrated = catalog.snapshot()
            migrated_suit = next(item for item in migrated["styles"] if item["id"] == "professional-suit")
            self.assertEqual(migrated_suit["history"][-1], {
                key: value for key, value in suit.items() if key != "history"
            })
            restored = catalog.rollback("professional-suit")
            restored_suit = next(item for item in restored["styles"] if item["id"] == "professional-suit")
            self.assertNotIn(CLOTHING_LOCK.strip(), restored_suit["wide_prompt"])
            self.assertNotIn("no outfit change", restored_suit["negative_prompt"])
            self.run_scene_matrix(catalog)


if __name__ == "__main__":
    unittest.main()
