import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from aigc_catalog import CatalogError, StyleCatalog, build_portrait_prompt, clothing_negative_prompt


PROMPT = "Create a clean hand-painted portrait with natural facial proportions and soft light."


class OptionalStylePromptsTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.catalog = StyleCatalog(Path(self.temporary.name))
        self.style = {"id": "portrait-only", "title": "竖版风格", "vertical_prompt": PROMPT,
                      "backdrop_id": "dynamic-minimal", "enabled": False}

    def test_create_and_reload_with_only_vertical_prompt(self):
        self.catalog.upsert(self.style)
        reloaded = StyleCatalog(Path(self.temporary.name)).snapshot()
        saved = next(item for item in reloaded["styles"] if item["id"] == self.style["id"])
        self.assertEqual(saved["wide_prompt"], "")
        self.assertEqual(saved["negative_prompt"], "")
        self.assertIn(PROMPT, saved["vertical_prompt"])
        self.assertEqual(saved["default_orientation"], "vertical")

    def test_edit_can_clear_optional_prompts(self):
        original = self.catalog.snapshot()["styles"][0]
        updated = self.catalog.upsert({"wide_prompt": "  ", "negative_prompt": ""}, original["id"])
        saved = next(item for item in updated["styles"] if item["id"] == original["id"])
        self.assertEqual(saved["wide_prompt"], "")
        self.assertEqual(saved["negative_prompt"], "")
        self.assertEqual(saved["vertical_prompt"], original["vertical_prompt"])
        self.assertTrue(saved["history"])

    def test_missing_wide_prompt_uses_vertical_in_both_portrait_modes(self):
        saved = StyleCatalog.validate_style(self.style)
        for orientation in ("wide", "vertical"):
            for mode in ("standard", "chibi"):
                with self.subTest(orientation=orientation, mode=mode):
                    prompt = build_portrait_prompt(saved, orientation, mode)
                    self.assertIn(PROMPT, prompt)
                    self.assertIn(clothing_negative_prompt(saved["id"]), prompt)

    def test_short_optional_negative_is_allowed(self):
        saved = StyleCatalog.validate_style({**self.style, "negative_prompt": "不要花纹"})
        self.assertIn("不要花纹", saved["negative_prompt"])

    def test_required_vertical_prompt_and_optional_limits_still_checked(self):
        for fields in ({"vertical_prompt": ""}, {"wide_prompt": "too short"},
                       {"negative_prompt": "禁" * 12001}):
            with self.subTest(fields=list(fields)):
                with self.assertRaises(CatalogError):
                    StyleCatalog.validate_style({**self.style, **fields})

    def test_optional_prompts_remain_empty_on_repeat_validation(self):
        saved = StyleCatalog.validate_style(self.style)
        self.assertEqual(saved, StyleCatalog.validate_style(saved))

    def test_named_style_references_can_be_saved_and_reloaded(self):
        fields = {
            "title": "宫崎骏手绘风格",
            "description": "参考 Pixar 的柔和光影",
            "vertical_prompt": PROMPT + " Ghibli hand-painted look.",
            "wide_prompt": PROMPT + " DISNEY animation lighting.",
            "negative_prompt": "不要漫威角色、不要 DreamWorks 标志",
        }
        self.catalog.upsert(self.style)
        for field, value in fields.items():
            with self.subTest(field=field):
                self.catalog.upsert({**self.style, **{field: value}},
                                    self.style["id"])
                reloaded = StyleCatalog(Path(self.temporary.name))
                saved = next(item for item in reloaded.snapshot()["styles"]
                             if item["id"] == self.style["id"])
                self.assertIn(value, saved[field])

    def test_name_and_description_limits_still_checked(self):
        for fields in ({"title": ""}, {"title": "名" * 81}, {"description": "述" * 241}):
            with self.subTest(fields=list(fields)):
                with self.assertRaises(CatalogError):
                    StyleCatalog.validate_style({**self.style, **fields})


if __name__ == "__main__":
    unittest.main()
