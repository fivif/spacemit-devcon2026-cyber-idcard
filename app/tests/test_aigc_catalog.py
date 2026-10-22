"""AIGC 提示词目录的离线安全回归。"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from aigc_catalog import CLOTHING_LOCK, CatalogError, StyleCatalog  # noqa: E402


PROMPT = "A cohesive hand-painted developer conference portrait with no text, border or extra people."
EXPECTED_STYLES = [
    ("hand-painted-animation", "晴空手绘动画"),
    ("american-comic", "高能漫画封面"),
    ("hand-doodle", "开发者手账速写"),
    ("professional-suit", "未来职业肖像"),
    ("chinese-elegance", "东方新潮国风"),
    ("pixel-person", "复古像素头像"),
]


def check(value, label):
    print("  [%s] %s" % ("PASS" if value else "FAIL", label))
    return bool(value)


def main() -> int:
    results = []
    with tempfile.TemporaryDirectory() as tmp:
        catalog = StyleCatalog(Path(tmp))
        first = catalog.snapshot()
        results.append(check([(item["id"], item["title"]) for item in first["styles"]]
                             == EXPECTED_STYLES, "六款默认风格按产品顺序建立，像素风位于最后"))
        results.append(check([(item["id"], item["title"]) for item in catalog.public()]
                             == EXPECTED_STYLES and "wide_prompt" not in catalog.public()[0],
                             "公开接口保持六款顺序且不暴露提示词"))

        edited = dict(first["styles"][0])
        edited["description"] = "已编辑的说明"
        edited["wide_prompt"] = PROMPT
        saved = catalog.upsert(edited, edited["id"])
        results.append(check(saved["version"] == 2 and saved["styles"][0]["revision"] == 2,
                             "编辑后版本递增"))
        results.append(check(saved["styles"][0]["history"], "编辑前版本进入历史记录"))

        added = catalog.upsert({"id": "warm-film-copy", "title": "温暖手绘副本",
                                "description": "后续可启用的方向",
                                "wide_prompt": PROMPT, "vertical_prompt": PROMPT,
                                "negative_prompt": PROMPT,
                                "model": "gpt-image-1", "sizes": ["1024x1536"],
                                "backdrop_id": "light-film-tech",
                                "default_orientation": "vertical",
                                "enabled": False, "is_default": False})
        results.append(check(len(added["styles"]) == 7, "可新增停用提示词"))
        results.append(check(len(catalog.public()) == 6, "停用风格不出现在参与者目录"))

        try:
            named = dict(edited, wide_prompt="Use a Ghibli studio look with soft natural light.")
            validated = StyleCatalog.validate_style(named)
            results.append(check("Ghibli" in validated["wide_prompt"], "风格名称引用不阻止配置校验"))
        except CatalogError:
            results.append(check(False, "风格名称引用不阻止配置校验"))

        rolled = catalog.rollback(edited["id"])
        rolled_prompt = rolled["styles"][0]["wide_prompt"]
        results.append(check(rolled_prompt == first["styles"][0]["wide_prompt"]
                             and rolled_prompt.count(CLOTHING_LOCK.strip()) == 1,
                             "恢复上一版提示词并继续锁定原服装"))

        raw = json.loads((Path(tmp) / "style-catalog.json").read_text(encoding="utf-8"))
        results.append(check(raw["schema_version"] == 2 and raw["version"] >= 4,
                             "目录以完整 JSON 原子保存"))

        previous = (Path(tmp) / "style-catalog.json").read_bytes()
        real_replace = os.replace
        os.replace = lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("模拟写失败"))
        try:
            try:
                changed = dict(catalog.snapshot()["styles"][0])
                changed["description"] = "这次不能落盘"
                catalog.upsert(changed, changed["id"])
                results.append(check(False, "写失败不会覆盖旧目录"))
            except OSError:
                results.append(check((Path(tmp) / "style-catalog.json").read_bytes() == previous,
                                     "写失败不会覆盖旧目录"))
        finally:
            os.replace = real_replace

    print("%d/%d PASS" % (sum(results), len(results)))
    return 0 if all(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
