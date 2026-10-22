#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""主体选择（_select_subject）的验证。

背景：用户要求「多人也拍，取画面中间最全最近、正对着人的那个」。
模型只给人框，测不出朝向，所以这里是几何近似的打分：最完整 / 最近 / 居中 /
接近直立。选错人的后果是"被风格化的不是你"，所以必须逐场景钉住。

运行：.venv-test\\Scripts\\python.exe app\\tests\\test_subject_select.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "infer"))

from worker import _select_subject  # noqa: E402

W, H = 1920, 1080
results: list[tuple[bool, str, str]] = []

# 固定一份规则，别让测试结果跟着现场 settings.json 变
RULES = {
    "anchor": [0.5, 0.5],
    "weights": {"area": 0.444, "center": 0.333, "completeness": 0.222},
    "area_full": 0.45,
    "min_box_h": 0.22,
    "pad": 0.06,
}


def check(ok: bool, name: str, detail: str = "") -> None:
    results.append((bool(ok), name, detail))
    print(("  ✅ " if ok else "  ❌ ") + name + (("   " + detail) if detail else ""))


def person(x0, y0, x1, y1, score=0.9):
    return {"box": [x0, y0, x1, y1], "score": score, "label": "person"}


def pick(persons, label, rules=None):
    got = _select_subject(persons, W, H, rules=rules or RULES)
    idx = None if got is None else got["index"]
    print("--- %s → 选中第 %s 个%s" % (
        label, idx, "（得分 %.4f）" % got["score"] if got else "（无人）"))
    return got


print("=== A) 空画面")
check(_select_subject([], W, H, rules=RULES) is None, "没有人 → 不选（返回 None）")

print()
print("=== B) 单人：必须选中他")
p = [person(700, 200, 1000, 900)]
got = pick(p, "单人")
check(got is not None and got["index"] == 0, "单人被选中")

print()
print("=== C) 两个人并排：选更居中、更大的那个（主办者站在引导线上）")
# 左边的人大但偏左；中间的人略小但正中
left = person(120, 160, 560, 1000)      # 宽 440，偏左
middle = person(820, 200, 1130, 920)    # 宽 310，居中
got = pick([left, middle], "左大 vs 中")
check(got is not None and got["index"] == 1, "选中居中的那个", f"实际 index={got and got['index']}")

print()
print("=== D) 同伴在远处更小：选近的大的")
near = person(760, 220, 1120, 980)
far = person(1500, 380, 1640, 700)      # 又小又偏右
got = pick([far, near], "远小 vs 近大")
check(got is not None and got["index"] == 1, "选中近的大的那个", f"实际 index={got and got['index']}")

print()
print("=== E) 有人被画面边缘切掉：优先完整的那个")
cut = person(0, 300, 260, 900)          # 左边缘切掉
whole = person(820, 240, 1120, 900)     # 完整居中
got = pick([cut, whole], "被切 vs 完整")
check(got is not None and got["index"] == 1, "选中没被切的那个", f"实际 index={got and got['index']}")

print()
print("=== F) 完全一样的两个框（并排）：取更靠前（索引更小）的那个，结果稳定")
a = person(600, 200, 900, 900)
b = person(600, 200, 900, 900)
got1 = pick([a, b], "重复框 第一次")
got2 = pick([a, b], "重复框 第二次")
check(got1 is not None and got1["index"] == 0, "平手取靠前的", f"index={got1 and got1['index']}")
check(got1 == got2 or (got1 and got2 and got1["index"] == got2["index"]),
      "同一输入结果稳定（可复现）")

print()
print("=== G) 坏数据：框缺失/退化，不能崩，也不能误选")
bad = [{"score": 0.9, "label": "person"}, {"box": [10, 10, 10, 10]},
       {"box": [0, 0, 0, 0], "score": 0.5}]
got = _select_subject(bad, W, H, rules=RULES)
check(got is None, "全是坏框 → 不选（返回 None，而不是抛异常）", f"实际={got}")

mixed = [{"box": [10, 10, 10, 10]}, person(800, 220, 1120, 920)]
got = pick(mixed, "一个坏框 + 一个好框")
check(got is not None and got["index"] == 1, "跳过坏框选到好框", f"实际 index={got and got['index']}")

print()
print("=== H) 坐得很近（框占满画面）：仍然选得出来，不因面积超阈值而失败")
big = person(100, 60, 1800, 1060)
got = pick([big], "超大人像")
check(got is not None and got["index"] == 0, "超大人像仍被选中")

print()
print("=== I) 五个人群拍：选出最像主办者的那个")
crowd = [
    person(80, 300, 380, 820),      # 0 左远
    person(460, 340, 720, 760),     # 1 中左，较小
    person(830, 210, 1150, 930),    # 2 正中、较大 ← 期望
    person(1280, 330, 1520, 780),   # 3 右中
    person(1650, 380, 1830, 720),   # 4 右远
]
got = pick(crowd, "五人")
check(got is not None and got["index"] == 2, "五人里选中正中最完整的", f"实际 index={got and got['index']}")

print()
print("=== J) 可调参数必须真的生效（否则「现场能调」是假的）")
# J1 锚点左移：构造两个**等大、完整**的人，只有横向位置不同，
# 这样胜负完全由"谁离锚点近"决定。
#   A 中心 x=500、B 中心 x=1100。默认锚点在画面正中(960)：B 更近 → 应选 B。
#   锚点左移到 0.20（x=384）：A 更近 → 应改选 A。
# 注意别用"离中心等距"的构造：那会变成平手、按索引取靠前的，测不出锚点是否生效。
same_a = person(250, 300, 750, 900)      # 中心 x=500
same_b = person(850, 300, 1350, 900)     # 中心 x=1100
default_pick = pick([same_a, same_b], "等大两人·默认锚点(0.5,0.5)")
left_rules = dict(RULES, anchor=[0.20, 0.5])
anchored = pick([same_a, same_b], "等大两人·锚点左移到(0.20,0.5)", rules=left_rules)
check(default_pick is not None and default_pick["index"] == 1,
      "默认锚点（正中）时选更近中心的右边那个", f"实际 {default_pick and default_pick['index']}")
check(anchored is not None and anchored["index"] == 0,
      "锚点左移后改选左边那个（锚点真的生效）",
      f"默认选中{default_pick and default_pick['index']}，左移后{anchored and anchored['index']}")

# J2 权重只留 area：应当选面积大的那个，忽略居中
area_only = dict(RULES, weights={"area": 1.0, "center": 0.0, "completeness": 0.0})
big_off_center = person(120, 150, 700, 1040)   # 大但偏左
small_center = person(880, 380, 1060, 780)     # 小但正中
area_pick = pick([big_off_center, small_center], "权重只看面积", rules=area_only)
check(area_pick is not None and area_pick["index"] == 0,
      "权重只看面积时选大的（权重生效）", f"实际 index={area_pick and area_pick['index']}")

center_only = dict(RULES, weights={"area": 0.0, "center": 1.0, "completeness": 0.0})
center_pick = pick([big_off_center, small_center], "权重只看居中", rules=center_only)
check(center_pick is not None and center_pick["index"] == 1,
      "权重只看居中时选正中的（权重生效）", f"实际 index={center_pick and center_pick['index']}")

# J3 area_full：调小后"很近"的门槛变低，大框不再被压成 1.0 封顶
low_full = dict(RULES, area_full=0.10)
got_low = pick([person(700, 200, 1200, 1000)], "area_full=0.10", rules=low_full)
check(got_low is not None and got_low["detail"]["area"] == 1.0,
      "area_full 调小后同一个人 area 项到顶（参数生效）",
      f"area={got_low and got_low['detail']['area']}")
high_full = dict(RULES, area_full=0.95)
got_high = pick([person(700, 200, 1200, 1000)], "area_full=0.95", rules=high_full)
check(got_high is not None and got_high["detail"]["area"] < 1.0,
      "area_full 调大后同一个人 area 项不再是满分",
      f"area={got_high and got_high['detail']['area']}")

# J4 返回体要带上生效的权重与锚点，现场才知道"我改的生效了没"
check(got_low is not None and got_low.get("weights") == low_full["weights"]
      and got_low.get("anchor") == low_full["anchor"],
      "返回体带出生效的 weights/anchor（可现场核对）")

failed = [r for r in results if not r[0]]
print()
print(f"共 {len(results)} 项，失败 {len(failed)} 项")
if failed:
    for _, name, detail in failed:
        print(f"  ❌ {name}  {detail}")
    sys.exit(1)
print("✅ 主体选择全部通过")
