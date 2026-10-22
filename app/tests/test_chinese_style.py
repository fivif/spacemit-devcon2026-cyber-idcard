from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.aigc_catalog import CHINESE_CLOTHING, CLOTHING_LOCK, StyleCatalog, default_catalog


class ChineseStyleMigrationTests(unittest.TestCase):
    prompt_fields = ("description", "wide_prompt", "vertical_prompt", "negative_prompt")

    def chinese_style(self, value):
        return next(item for item in value["styles"] if item["id"] == "chinese-elegance")

    def version_eight_catalog(self):
        value = default_catalog()
        value.update(preset_version=8, version=23, updated_at=1000,
                     deleted_style_ids=["retired-custom-style"])
        chinese = self.chinese_style(value)
        chinese.update(
            title="现场自定义古风", description="旧版古风说明",
            wide_prompt="Legacy landscape ink portrait with a quiet plain background.",
            vertical_prompt="Legacy vertical ink portrait with a quiet plain background.",
            negative_prompt="Avoid distracting backgrounds and incomplete portrait framing.",
            model="operator-custom-model", sizes=["1280x1920", "1920x1280"],
            backdrop_id="operator-backdrop", default_orientation="wide",
            revision=4, updated_at=1000,
        )
        previous = {key: entry for key, entry in chinese.items() if key != "history"}
        previous.update(description="更早的古风说明", revision=3, updated_at=900)
        chinese["history"] = [previous]
        other = value["styles"][1]
        other.update(title="现场编辑的其他风格", description="其他风格保留原样",
                     enabled=False, revision=6, updated_at=800)
        other["history"] = [{key: entry for key, entry in other.items() if key != "history"}]
        return value

    def write_catalog(self, root, value):
        path = Path(root) / "style-catalog.json"
        path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
        return path

    def legacy_catalog(self):
        value = default_catalog()
        value["preset_version"] = 7
        value["styles"] = [item for item in value["styles"] if item["id"] != "chinese-elegance"]
        value["styles"][0].update(enabled=False, is_default=False, description="现场编辑内容")
        value["styles"][1]["is_default"] = True
        return value

    def test_upgrade_adds_style_without_changing_existing_entries(self):
        original = self.legacy_catalog()
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "style-catalog.json"
            path.write_text(json.dumps(original), encoding="utf-8")
            catalog = StyleCatalog(root)
            upgraded = catalog.snapshot()
            self.assertEqual(upgraded["styles"][:-1], original["styles"])
            chinese = upgraded["styles"][-1]
            self.assertEqual(chinese["id"], "chinese-elegance")
            self.assertTrue(chinese["enabled"])
            self.assertFalse(chinese["is_default"])
            persisted = path.read_bytes()
            self.assertEqual(StyleCatalog(root).snapshot(), upgraded)
            self.assertEqual(path.read_bytes(), persisted)
            catalog.upsert({"enabled": False}, "chinese-elegance")
            self.assertNotIn("chinese-elegance", [item["id"] for item in catalog.public()])

    def test_deleted_style_is_not_restored_by_upgrade(self):
        original = self.legacy_catalog()
        original["deleted_style_ids"] = ["chinese-elegance", "hand-doodle"]
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "style-catalog.json"
            path.write_text(json.dumps(original), encoding="utf-8")
            upgraded = StyleCatalog(root).snapshot()
            self.assertEqual(upgraded["styles"], original["styles"])
            self.assertEqual(upgraded["deleted_style_ids"], original["deleted_style_ids"])

    def test_existing_chinese_style_edits_are_preserved(self):
        original = self.legacy_catalog()
        chinese = next(item for item in default_catalog()["styles"] if item["id"] == "chinese-elegance")
        chinese.update(title="现场新中式", enabled=False, revision=4)
        original["styles"].append(chinese)
        self.assertEqual(StyleCatalog._migrate(original)["styles"], original["styles"])

    def test_validation_removes_legacy_clothing_lock_and_is_idempotent(self):
        chinese = next(item for item in default_catalog()["styles"] if item["id"] == "chinese-elegance")
        ordinary = default_catalog()["styles"][0]
        chinese["wide_prompt"] += CLOTHING_LOCK
        chinese["negative_prompt"] = ordinary["negative_prompt"]
        validated = StyleCatalog.validate_style(chinese)
        self.assertIn(CHINESE_CLOTHING.strip(), validated["wide_prompt"])
        self.assertNotIn(CLOTHING_LOCK.strip(), validated["wide_prompt"])
        self.assertNotIn("no outfit change", validated["negative_prompt"])
        self.assertEqual(StyleCatalog.validate_style(validated), validated)

    def test_eight_to_nine_updates_only_chinese_prompt_fields_and_revision(self):
        original = self.version_eight_catalog()
        old = self.chinese_style(original)
        seed = self.chinese_style(default_catalog())
        with tempfile.TemporaryDirectory() as root:
            path = self.write_catalog(root, original)
            upgraded = StyleCatalog(root).snapshot()
            chinese = self.chinese_style(upgraded)
            self.assertEqual(upgraded["preset_version"], 9)
            self.assertEqual(upgraded["version"], original["version"] + 1)
            self.assertEqual(upgraded["schema_version"], original["schema_version"])
            for key in self.prompt_fields:
                self.assertEqual(chinese[key], seed[key])
            mutable = {*self.prompt_fields, "history", "revision", "updated_at"}
            self.assertEqual(
                {key: entry for key, entry in chinese.items() if key not in mutable},
                {key: entry for key, entry in old.items() if key not in mutable},
            )
            self.assertEqual(chinese["revision"], old["revision"] + 1)
            self.assertEqual(chinese["history"][:-1], old["history"])
            self.assertEqual(chinese["history"][-1],
                             {key: entry for key, entry in old.items() if key != "history"})
            self.assertEqual(json.loads(path.read_text(encoding="utf-8")), upgraded)

    def test_eight_to_nine_preserves_enabled_default_and_custom_settings(self):
        for enabled in (False, True):
            for is_default in (False, True):
                with self.subTest(enabled=enabled, is_default=is_default):
                    original = self.version_eight_catalog()
                    for item in original["styles"]:
                        item["is_default"] = False
                    old = self.chinese_style(original)
                    old.update(enabled=enabled, is_default=is_default)
                    if not is_default:
                        original["styles"][0]["is_default"] = True
                    with tempfile.TemporaryDirectory() as root:
                        self.write_catalog(root, original)
                        catalog = StyleCatalog(root)
                        upgraded = catalog.snapshot()
                        chinese = self.chinese_style(upgraded)
                        for key in ("enabled", "is_default", "title", "model", "sizes",
                                    "backdrop_id", "default_orientation"):
                            self.assertEqual(chinese[key], old[key])
                        self.assertEqual(
                            [item["id"] for item in upgraded["styles"] if item["is_default"]],
                            [item["id"] for item in original["styles"] if item["is_default"]],
                        )
                        public = next((item for item in catalog.public()
                                       if item["id"] == "chinese-elegance"), None)
                        self.assertEqual(public is not None, enabled)
                        if enabled:
                            self.assertEqual(public["title"], old["title"])
                            self.assertEqual(public["sizes"], old["sizes"])
                            self.assertEqual(public["is_default"], is_default)

    def test_eight_to_nine_preserves_other_styles_order_settings_and_tombstones(self):
        original = self.version_eight_catalog()
        custom = dict(original["styles"][1])
        custom.update(id="operator-custom-style", title="自定义风格", history=[])
        original["styles"].insert(2, custom)
        original["operator_settings"] = {"orientation": "wide", "preview_enabled": False}
        with tempfile.TemporaryDirectory() as root:
            self.write_catalog(root, original)
            upgraded = StyleCatalog(root).snapshot()
        self.assertEqual([item["id"] for item in upgraded["styles"]],
                         [item["id"] for item in original["styles"]])
        self.assertEqual(
            [item for item in upgraded["styles"] if item["id"] != "chinese-elegance"],
            [item for item in original["styles"] if item["id"] != "chinese-elegance"],
        )
        self.assertEqual(upgraded["deleted_style_ids"], original["deleted_style_ids"])
        self.assertEqual(upgraded["operator_settings"], original["operator_settings"])

    def test_migration_history_can_restore_previous_and_older_revisions(self):
        for revision in (None, 3):
            with self.subTest(revision=revision):
                original = self.version_eight_catalog()
                old = self.chinese_style(original)
                target = old if revision is None else old["history"][0]
                expected = StyleCatalog.validate_style(target)
                with tempfile.TemporaryDirectory() as root:
                    self.write_catalog(root, original)
                    migrated = StyleCatalog(root).snapshot()
                    restored = StyleCatalog(root).rollback("chinese-elegance", revision)
                    chinese = self.chinese_style(restored)
                    for key in (*self.prompt_fields, "title", "model", "sizes", "enabled", "is_default"):
                        self.assertEqual(chinese[key], expected[key])
                    self.assertEqual(chinese["revision"], self.chinese_style(migrated)["revision"] + 1)
                    self.assertEqual(chinese["history"][:-1], self.chinese_style(migrated)["history"])
                    self.assertEqual(chinese["history"][-1]["revision"],
                                     self.chinese_style(migrated)["revision"])
                    self.assertEqual(restored["preset_version"], 9)
                    self.assertEqual(StyleCatalog(root).snapshot(), restored)

    def test_repeat_reads_and_restart_do_not_rewrite_or_append_history(self):
        with tempfile.TemporaryDirectory() as root:
            path = self.write_catalog(root, self.version_eight_catalog())
            catalog = StyleCatalog(root)
            upgraded = catalog.snapshot()
            persisted = path.read_bytes()
            modified_at = path.stat().st_mtime_ns
            with patch.object(StyleCatalog, "_atomic_write", side_effect=AssertionError("不得重复写入")):
                for attempt in range(3):
                    with self.subTest(attempt=attempt):
                        self.assertEqual(catalog.snapshot(), upgraded)
                        self.assertEqual(StyleCatalog(root).snapshot(), upgraded)
            self.assertEqual(path.read_bytes(), persisted)
            self.assertEqual(path.stat().st_mtime_ns, modified_at)

    def test_identical_new_seed_does_not_append_revision_or_history(self):
        original = self.version_eight_catalog()
        chinese = self.chinese_style(original)
        seed = self.chinese_style(default_catalog())
        chinese.update({key: seed[key] for key in self.prompt_fields})
        with tempfile.TemporaryDirectory() as root:
            path = self.write_catalog(root, original)
            upgraded = StyleCatalog(root).snapshot()
            self.assertEqual(upgraded["preset_version"], 9)
            self.assertEqual(upgraded["styles"], original["styles"])
            persisted = path.read_bytes()
            self.assertEqual(StyleCatalog(root).snapshot(), upgraded)
            self.assertEqual(path.read_bytes(), persisted)

    def test_style_deleted_in_eight_is_not_revived_by_nine(self):
        with tempfile.TemporaryDirectory() as root:
            catalog = StyleCatalog(root)
            deleted = catalog.delete("chinese-elegance")
            deleted["preset_version"] = 8
            path = self.write_catalog(root, deleted)
            upgraded = StyleCatalog(root).snapshot()
            self.assertEqual(upgraded["preset_version"], 9)
            self.assertEqual(upgraded["styles"], deleted["styles"])
            self.assertEqual(upgraded["deleted_style_ids"], deleted["deleted_style_ids"])
            self.assertNotIn("chinese-elegance", [item["id"] for item in upgraded["styles"]])
            self.assertNotIn("chinese-elegance", [item["id"] for item in StyleCatalog(root).public()])
            persisted = path.read_bytes()
            self.assertEqual(StyleCatalog(root).snapshot(), upgraded)
            self.assertEqual(path.read_bytes(), persisted)


if __name__ == "__main__":
    unittest.main()
