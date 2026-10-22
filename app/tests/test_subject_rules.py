#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""subject_rules 的验证：默认值、环境变量、settings.json、坏值退回、热更新。

这是"现场能调"的地基，所以重点验三件事：
  A. 优先级正确：环境变量 > settings.json > 默认值；
  B. 坏值绝不让拍摄崩：任何畸形输入都退回默认，而不是抛异常；
  C. 改完不重启就生效（mtime 变化被感知）。

运行：.venv-test\\Scripts\\python.exe app\\tests\\test_subject_rules.py
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

results: list[tuple[bool, str, str]] = []


def check(ok: bool, name: str, detail: str = "") -> None:
    results.append((bool(ok), name, detail))
    print(("  ✅ " if ok else "  ❌ ") + name + (("   " + detail) if detail else ""))


def fresh_module(data_dir: Path, env: dict | None = None):
    """在隔离的数据目录 + 指定环境变量下重新导入模块。"""
    for key in list(os.environ):
        if key.startswith("BOOTH_SUBJECT_"):
            del os.environ[key]
    os.environ["BOOTH_DATA"] = str(data_dir)
    for key, value in (env or {}).items():
        os.environ[key] = value
    for name in ("subject_rules",):
        sys.modules.pop(name, None)
    import subject_rules
    return subject_rules


print("=== A) 默认值（没有任何配置时）")
with tempfile.TemporaryDirectory() as tmp:
    sr = fresh_module(Path(tmp))
    r = sr.rules(force=True)
    check(r["anchor"] == sr.DEFAULTS["anchor"], "锚点用默认", str(r["anchor"]))
    check(abs(sum(r["weights"].values()) - 1.0) < 1e-5,
          "权重自动归一化到 1", str(r["weights"]))
    check(r["area_full"] == sr.DEFAULTS["area_full"], "area_full 用默认")

print()
print("=== B) settings.json 覆盖默认值")
with tempfile.TemporaryDirectory() as tmp:
    data = Path(tmp)
    (data / "settings.json").write_text(json.dumps({
        "public_base": "https://example.test",   # 别的段必须原样保留
        "subject": {"anchor": [0.3, 0.42], "weights": {"area": 1, "center": 0, "completeness": 0}},
    }), encoding="utf-8")
    sr = fresh_module(data)
    r = sr.rules(force=True)
    check(r["anchor"] == [0.3, 0.42], "锚点读到文件值", str(r["anchor"]))
    check(r["weights"]["area"] == 1.0 and r["weights"]["center"] == 0.0,
          "权重读到文件值", str(r["weights"]))

print()
print("=== C) 环境变量优先于文件")
with tempfile.TemporaryDirectory() as tmp:
    data = Path(tmp)
    (data / "settings.json").write_text(json.dumps({
        "subject": {"anchor": [0.3, 0.42]},
    }), encoding="utf-8")
    sr = fresh_module(data, {"BOOTH_SUBJECT_ANCHOR": "0.77,0.25"})
    r = sr.rules(force=True)
    check(r["anchor"] == [0.77, 0.25], "环境变量赢过文件", str(r["anchor"]))
    d = sr.describe()
    check("BOOTH_SUBJECT_ANCHOR" in d["env_overrides"], "describe 报出环境变量来源")

print()
print("=== D) 坏值必须安全退回，绝不抛异常（现场手改一行写错不能崩拍摄）")
BAD_CASES = [
    ("锚点不是两个数", {"anchor": "middle"}),
    ("锚点越界", {"anchor": [5.0, -3.0]}),
    ("锚点含非数字", {"anchor": ["a", "b"]}),
    ("area_full 是文字", {"area_full": "big"}),
    ("area_full 为 0", {"area_full": 0}),
    ("权重全为 0", {"weights": {"area": 0, "center": 0, "completeness": 0}}),
    ("权重是数组", {"weights": [1, 2, 3]}),
    ("pad 负数", {"pad": -1}),
    ("整个 subject 段是字符串", None),
]
for label, section in BAD_CASES:
    with tempfile.TemporaryDirectory() as tmp:
        data = Path(tmp)
        payload = {"subject": section} if section is not None else {"subject": "oops"}
        (data / "settings.json").write_text(json.dumps(payload), encoding="utf-8")
        sr = fresh_module(data)
        try:
            r = sr.rules(force=True)
        except Exception as exc:  # noqa: BLE001
            check(False, f"坏值不抛异常：{label}", f"{type(exc).__name__}: {exc}")
            continue
        ok = (isinstance(r["anchor"], list) and len(r["anchor"]) == 2
              and 0.0 <= r["anchor"][0] <= 1.0 and 0.0 <= r["anchor"][1] <= 1.0
              and 0.0 < r["area_full"] <= 1.0
              and abs(sum(r["weights"].values()) - 1.0) < 1e-5
              and 0.0 <= r["pad"] <= 0.5)
        check(ok, f"坏值不抛异常且仍在合法范围：{label}", json.dumps(r, ensure_ascii=False))

print()
print("=== E) 文件写坏（不是 JSON）也要能起来")
with tempfile.TemporaryDirectory() as tmp:
    data = Path(tmp)
    (data / "settings.json").write_text("{ this is not json", encoding="utf-8")
    sr = fresh_module(data)
    r = sr.rules(force=True)
    check(r["anchor"] == sr.DEFAULTS["anchor"], "JSON 写坏 → 退回默认（不崩）")

print()
print("=== F) 改完不重启就生效（现场调参的关键）")
with tempfile.TemporaryDirectory() as tmp:
    data = Path(tmp)
    sr = fresh_module(data)
    before = sr.rules(force=True)
    check(before["anchor"] == [0.5, 0.5], "初始是默认锚点")
    sr.write_rules({"anchor": [0.25, 0.6]})
    # 故意不 force：走的是"文件变了就重读"这条真实路径
    time.sleep(0.01)
    after = sr.rules()
    check(after["anchor"] == [0.25, 0.6],
          "写回后无需重启即生效", str(after["anchor"]))
    # 再确认别的段没被写没
    whole = json.loads((data / "settings.json").read_text(encoding="utf-8"))
    check("subject" in whole, "写回保留了 settings.json 结构")

print()
print("=== G) write_rules 会归一化（写进去的坏值不落盘）")
with tempfile.TemporaryDirectory() as tmp:
    data = Path(tmp)
    sr = fresh_module(data)
    sr.write_rules({"anchor": [9, -9], "weights": {"area": 3, "center": 1}})
    r = sr.rules(force=True)
    check(r["anchor"] == [1.0, 0.0], "越界锚点被夹到合法区间", str(r["anchor"]))
    # 没写的维度沿用默认（不被静默清零），已写的两个保持它们的**比例**：
    # 写 area=3 / center=1 是在说"面积比居中重要三倍"。
    ratio = r["weights"]["area"] / r["weights"]["center"]
    check(abs(ratio - 3.0) < 1e-4,
          "已写的权重保持相对比例（area 是 center 的 3 倍）",
          f"area={r['weights']['area']} center={r['weights']['center']} 比值={ratio:.4f}")
    check(r["weights"]["completeness"] > 0,
          "没写的维度沿用默认而不是被清零", f"completeness={r['weights']['completeness']}")

failed = [r for r in results if not r[0]]
print()
print(f"共 {len(results)} 项，失败 {len(failed)} 项")
if failed:
    for _, name, detail in failed:
        print(f"  ❌ {name}  {detail}")
    sys.exit(1)
print("✅ subject_rules 全部通过")
