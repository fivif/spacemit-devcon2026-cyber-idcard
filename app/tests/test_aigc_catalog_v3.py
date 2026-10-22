"""衣服规则与历史风格目录兼容迁移。"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from aigc_catalog import (CHINESE_CLOTHING, CLOTHING_LOCK, PRESET_VERSION, SUIT_CLOTHING,
                          CatalogError, StyleCatalog, default_catalog)


EXPECTED_STYLES = [
    ("hand-painted-animation", "晴空手绘动画"),
    ("american-comic", "高能漫画封面"),
    ("hand-doodle", "开发者手账速写"),
    ("professional-suit", "未来职业肖像"),
    ("chinese-elegance", "东方新潮国风"),
    ("pixel-person", "复古像素头像"),
]
EXPECTED_IDS = {style_id for style_id, _title in EXPECTED_STYLES}


def main() -> int:
    checks = []

    def check(value, label):
        ok = bool(value)
        checks.append(ok)
        print("  [%s] %s" % ("PASS" if ok else "FAIL", label))

    with tempfile.TemporaryDirectory() as tmp:
        catalog = StyleCatalog(tmp)
        snapshot = catalog.snapshot()
        check(snapshot["schema_version"] == 2, "使用 v2 目录契约")
        check(snapshot["preset_version"] == PRESET_VERSION, "内置风格升级为当前版本")
        check([(item["id"], item["title"]) for item in catalog.public()] == EXPECTED_STYLES,
              "公开六款风格，名称和顺序一致，像素风位于最后")
        check(all(item["wide_prompt"] and item["vertical_prompt"] and item["negative_prompt"]
                  for item in snapshot["styles"]), "每款包含横版、竖版和禁止项")
        check(all(item["wide_prompt"].count(CLOTHING_LOCK.strip()) == 1
                  and item["vertical_prompt"].count(CLOTHING_LOCK.strip()) == 1
                  for item in snapshot["styles"] if item["id"] not in ("professional-suit", "chinese-elegance")),
              "普通风格默认提示词只保留一份原服装规则")
        suit = next(item for item in snapshot["styles"] if item["id"] == "professional-suit")
        check(all(SUIT_CLOTHING.strip() in suit[key] and CLOTHING_LOCK.strip() not in suit[key]
                  for key in ("wide_prompt", "vertical_prompt"))
              and "No missing suit jacket, dress shirt or tie" in suit["negative_prompt"],
              "西装风独立正负提示词明确换装且无锁衣冲突")
        chinese = next(item for item in snapshot["styles"] if item["id"] == "chinese-elegance")
        check(all(CHINESE_CLOTHING.strip() in chinese[key] and CLOTHING_LOCK.strip() not in chinese[key]
                  for key in ("wide_prompt", "vertical_prompt")),
              "国风独立使用新中式服装规则且无锁衣冲突")
        pixel = next(item for item in snapshot["styles"] if item["id"] == "pixel-person")
        check("16-bit pixel-art" in pixel["wide_prompt"] and "红蓝外套" not in pixel["description"],
              "像素风使用真实像素画法且不指定固定外套")
        check(all(StyleCatalog.validate_style(StyleCatalog.validate_style(item))
                  == StyleCatalog.validate_style(item) for item in snapshot["styles"]),
              "六款提示词重复校验保持幂等")
        check(all(StyleCatalog._lock_clothing_prompt(item["wide_prompt"])
                  == item["wide_prompt"] for item in snapshot["styles"]
                  if item["id"] not in ("professional-suit", "chinese-elegance")), "普通风已锁定提示词不会重复追加")
        check("wearing a well-fitted dark suit" in StyleCatalog.validate_style(
            dict(suit, wide_prompt="Create a professional portrait wearing a well-fitted dark suit"))["wide_prompt"],
              "西装风校验允许指定西装")
        normalized_suit = StyleCatalog.validate_style(dict(
            suit, wide_prompt=snapshot["styles"][0]["wide_prompt"],
            negative_prompt=snapshot["styles"][0]["negative_prompt"]))
        check(CLOTHING_LOCK.strip() not in normalized_suit["wide_prompt"]
              and SUIT_CLOTHING.strip() in normalized_suit["wide_prompt"]
              and "no outfit change" not in normalized_suit["negative_prompt"],
              "西装旧锁衣规则在保存校验时解除")
        check(all(item["backdrop_id"] == "dynamic-minimal" for item in snapshot["styles"]),
              "风格绑定动态简约背景策略")
        check(all(key not in item for item in catalog.public()
                  for key in ("wide_prompt", "vertical_prompt", "negative_prompt", "model")),
              "参与者目录不泄露提示词和模型")
        check(all("MODE:" not in item["vertical_prompt"]
                  and "FINAL PRIORITY" not in item["vertical_prompt"]
                  and "override any earlier" not in item["vertical_prompt"].lower()
                  for item in snapshot["styles"]),
              "六款基础画法不提前绑定标准/Q版，也不使用覆盖段")
        catalog.save(list(reversed(snapshot["styles"])))
        check([(item["id"], item["title"]) for item in catalog.public()] == EXPECTED_STYLES,
              "后台重排目录不会改变参与者六款展示顺序")

        named = dict(snapshot["styles"][0], vertical_prompt="Draw it like Disney animation with natural light")
        try:
            validated = catalog.validate_style(named)
            check("Disney" in validated["vertical_prompt"], "第三方名称引用可正常配置")
        except CatalogError:
            check(False, "第三方名称引用可正常配置")

        bad_clothes = dict(snapshot["styles"][0],
                           vertical_prompt="Create a new outfit with a suit and tie for this portrait")
        try:
            catalog.upsert(bad_clothes, bad_clothes["id"])
            check(False, "禁止提示词要求更换服装")
        except CatalogError:
            check(True, "禁止提示词要求更换服装")

    with tempfile.TemporaryDirectory() as tmp:
        old_prompt = "A clean single-person illustrated portrait with a flat neutral background and no text."
        old = {"schema_version": 1, "version": 7, "updated_at": 1, "styles": [{
            "id": "legacy-film", "title": "旧风格", "description": "兼容测试",
            "prompt": old_prompt, "model": "gpt-image-1",
            "sizes": ["1024x1024"], "enabled": True, "is_default": True,
            "revision": 2, "updated_at": 1, "history": []}]}
        (Path(tmp) / "style-catalog.json").write_text(
            json.dumps(old, ensure_ascii=False), encoding="utf-8")
        migrated = StyleCatalog(tmp).snapshot()
        item = next(entry for entry in migrated["styles"] if entry["id"] == "legacy-film")
        check(migrated["schema_version"] == 2 and item["wide_prompt"] == old_prompt
              and item["vertical_prompt"] == old_prompt, "v1 目录可无损读入")
        check(not item["enabled"] and not item["is_default"] and len(migrated["styles"]) == 7
              and item["history"][-1]["enabled"],
              "旧风格停用但保留内容和历史，自动补齐六款内置风格")
        check(item["backdrop_id"] == "dynamic-minimal", "旧固定背景标识迁移为动态背景")

    with tempfile.TemporaryDirectory() as tmp:
        current = {"schema_version": 2, "version": 9, "updated_at": 1, "styles": [{
            "id": "main-film-chip", "title": "现场主风格", "description": "保留现场编辑内容",
            "wide_prompt": old_prompt, "vertical_prompt": old_prompt,
            "negative_prompt": old_prompt, "model": "gpt-image-1",
            "sizes": ["1024x1024"], "backdrop_id": "light-film-tech",
            "default_orientation": "vertical", "preserve_identity": True,
            "single_person": True, "no_generated_text": True, "no_border": True,
            "enabled": True, "is_default": True, "revision": 8,
            "updated_at": 1, "history": []}]}
        (Path(tmp) / "style-catalog.json").write_text(
            json.dumps(current, ensure_ascii=False), encoding="utf-8")
        merged = StyleCatalog(tmp).snapshot()
        item = next(entry for entry in merged["styles"] if entry["id"] == "main-film-chip")
        check(item["revision"] == 9 and item["history"][-1]["revision"] == 8
              and item["wide_prompt"] == old_prompt
              and len(merged["styles"]) == 7,
              "v2 现场旧提示词和版本历史保留")
        check(item["backdrop_id"] == "dynamic-minimal",
              "v2 现场目录切换为动态背景策略")

    with tempfile.TemporaryDirectory() as tmp:
        legacy = default_catalog()
        legacy.pop("preset_version")
        legacy["version"] = 15
        legacy["styles"] = [
            dict(legacy["styles"][0], wide_prompt=old_prompt, vertical_prompt=old_prompt, revision=4,
                 model="onsite-model", sizes=["1024x1024"],
                 history=[{"revision": 3, "wide_prompt": "Previous onsite prompt"}]),
            dict(legacy["styles"][1], id="korean-pop", title="韩式流行风", revision=6,
                 model="onsite-korean-model", sizes=["1024x1024"],
                 history=[{"revision": 5, "wide_prompt": "Old Q-style prompt"}]),
            dict(legacy["styles"][1], id="soft-chibi", title="柔软Q版", revision=6,
                 model="onsite-chibi-model", sizes=["1024x1024"],
                 history=[{"revision": 5, "wide_prompt": "Old Q-style prompt"}]),
            dict(legacy["styles"][3], id="cel-shaded-3d", title="赛璐璐3D", revision=7,
                 wide_prompt=old_prompt, vertical_prompt=old_prompt,
                 model="onsite-suit-model", default_orientation="wide"),
        ]
        path = Path(tmp) / "style-catalog.json"
        path.write_text(json.dumps(legacy, ensure_ascii=False), encoding="utf-8")
        catalog = StyleCatalog(tmp)
        migrated = catalog.snapshot()
        styles = {item["id"]: item for item in migrated["styles"]}
        check([(item["id"], item["title"]) for item in catalog.public()] == EXPECTED_STYLES,
              "旧目录迁移后仅六种新风格公开")
        check(styles["hand-painted-animation"]["model"] == "onsite-model"
              and styles["hand-painted-animation"]["sizes"] == ["1024x1024"]
              and styles["hand-painted-animation"]["history"][0]["revision"] == 3
              and any(old_prompt in str(entry.get("wide_prompt", ""))
                      for entry in styles["hand-painted-animation"]["history"]),
              "现有正式ID保留现场模型、尺寸和全部历史")
        check(styles["professional-suit"]["model"] == "onsite-suit-model"
              and styles["professional-suit"]["default_orientation"] == "wide",
              "旧3D配置迁移至职业风正式ID，不丢现场设置")
        check(not styles["korean-pop"]["enabled"] and not styles["korean-pop"]["is_default"]
              and styles["korean-pop"]["model"] == "onsite-korean-model"
              and styles["korean-pop"]["history"][0]["revision"] == 5,
              "韩式风格保留配置与历史但作为退役实体停用")
        check(not styles["soft-chibi"]["enabled"] and not styles["cel-shaded-3d"]["enabled"]
              and styles["soft-chibi"]["history"][0]["revision"] == 5
              and styles["cel-shaded-3d"]["history"][-1]["revision"] == 7,
              "旧ID实体与历史仍在，仅停用")
        persisted = path.read_bytes()
        reopened = StyleCatalog(tmp).snapshot()
        check(reopened == migrated and path.read_bytes() == persisted,
              "迁移原子落盘且重复加载不重复修改")
        edited = dict(styles["korean-pop"], description="退役实体仍可在后台维护")
        saved = catalog.upsert(edited, "korean-pop")
        check(StyleCatalog(tmp).snapshot() == saved
              and next(item for item in saved["styles"] if item["id"] == "korean-pop")["description"]
              == "退役实体仍可在后台维护", "退役韩式编辑不会被种子覆盖")
        saved = catalog.upsert(dict(styles["soft-chibi"], enabled=True), "soft-chibi")
        check(next(item for item in saved["styles"] if item["id"] == "soft-chibi")["enabled"]
              and len(catalog.public()) == 7, "显式启用自定义旧ID后进入公共目录")
        saved = catalog.upsert({"enabled": False}, "soft-chibi")
        catalog.save([item for item in saved["styles"] if item["id"] in dict(EXPECTED_STYLES)])
        check({"soft-chibi", "cel-shaded-3d"}.issubset(
            {item["id"] for item in catalog.snapshot()["styles"]}),
            "仅保存六种公开风格也不会删除旧实体与历史")
        for index in range(12):
            edited = dict(next(item for item in catalog.snapshot()["styles"] if item["id"] == "korean-pop"),
                          description="退役编辑 %d" % index)
            catalog.upsert(edited, "korean-pop")
        check(len(next(item for item in catalog.snapshot()["styles"] if item["id"] == "korean-pop")["history"])
              > 10, "后续编辑不截断已有历史")

    with tempfile.TemporaryDirectory() as tmp:
        legacy = default_catalog()
        legacy["preset_version"] = 3
        legacy["version"] = 27
        for item in legacy["styles"]:
            item.update(model="onsite-" + item["id"], sizes=["1280x720", "720x1280"],
                        default_orientation="wide", revision=9,
                        history=[{"revision": 8, "wide_prompt": "Unmodified onsite history"}])
            if item["id"] == "professional-suit":
                item["wide_prompt"] = "Professional portrait wearing a well-fitted dark suit." + CLOTHING_LOCK
                item["vertical_prompt"] = "Professional portrait with natural lighting." + CLOTHING_LOCK
                item["negative_prompt"] = legacy["styles"][0]["negative_prompt"]
        path = Path(tmp) / "style-catalog.json"
        path.write_text(json.dumps(legacy, ensure_ascii=False), encoding="utf-8")
        migrated = StyleCatalog(tmp).snapshot()
        check(migrated["preset_version"] == PRESET_VERSION and migrated["version"] == 28,
              "v3 目录自动升级一次至当前版本")
        check(all(item["model"] == old["model"] and item["sizes"] == old["sizes"]
                  and item["default_orientation"] == old["default_orientation"]
                  and item["revision"] == 10
                  and item["history"] == old["history"] + [
                      {key: value for key, value in old.items() if key != "history"}]
                  for item, old in zip(migrated["styles"], legacy["styles"])),
              "v3 迁移保留六款现场模型、尺寸、版式与原始历史快照")
        suit = next(item for item in migrated["styles"] if item["id"] == "professional-suit")
        check(SUIT_CLOTHING.strip() in suit["wide_prompt"]
              and "no outfit change" not in suit["negative_prompt"],
              "v3 西装一刀切锁衣规则迁移后解除")
        persisted = path.read_bytes()
        check(StyleCatalog(tmp).snapshot() == migrated and path.read_bytes() == persisted,
              "当前版本重复加载不改提示词、不新增历史")

    bundled_path = Path(__file__).resolve().parents[1] / "data" / "style-catalog.json"
    bundled_bytes = bundled_path.read_bytes()
    with tempfile.TemporaryDirectory() as tmp:
        (Path(tmp) / "style-catalog.json").write_bytes(bundled_bytes)
        bundled = StyleCatalog(tmp).snapshot()
        StyleCatalog.validate_catalog(bundled)
        check([(item["id"], item["title"]) for item in bundled["styles"] if item["enabled"]]
              == EXPECTED_STYLES and bundled["preset_version"] == PRESET_VERSION,
              "实际项目目录副本可完整加载并迁移至当前版本")
    check(bundled_path.read_bytes() == bundled_bytes, "测试未改写实际项目目录")

    print("%d/%d PASS" % (sum(checks), len(checks)))
    return 0 if all(checks) else 1


if __name__ == "__main__":
    raise SystemExit(main())
