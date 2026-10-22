#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""无 pytest 环境下的手势判据校验（K3 上跑）。

与 app/tests/test_gesture_rules.py 同一批断言，但只用标准库。
用途：板上没装 pytest，而这条判据是"空场景不许误触发拍摄"的性命线，必须能随时验。

    python3 app/tools/check_gesture_rules.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from gesture_rules import (  # noqa: E402
    PERSON_BOX_PAD,
    hand_inside_person,
    is_ok,
    pinch_ratio,
    points_box,
)

PASSED = 0
FAILED = 0


def check(name: str, got, want) -> None:
    global PASSED, FAILED
    if got == want:
        PASSED += 1
        print(f"  ok   {name}")
    else:
        FAILED += 1
        print(f"  FAIL {name}\n       得到 {got!r}，期望 {want!r}")


def _hand(points):
    """按索引顺序摆点：0=腕, 4=拇指尖, 8=食指尖, 9=中指根，其余补 (0,0)。"""
    pts = [[0.0, 0.0, 0.0] for _ in range(21)]
    for index, (x, y) in points.items():
        pts[index] = [float(x), float(y), 0.0]
    return pts


# 捏合的手：腕(0,0)、中指根(0,100) → 手掌尺度 100；
# 拇指尖(10,10) 与食指尖(20,20) 相距 14.14 → 捏合比 0.1414
TIGHT = _hand({0: (0, 0), 4: (10, 10), 8: (20, 20), 9: (0, 100)})
# 张开的手：拇指尖(60,0) 与食指尖(0,90) 相距 108.2 → 比值 1.082
OPEN = _hand({0: (0, 0), 4: (60, 0), 8: (0, 90), 9: (0, 100)})


def main() -> int:
    print("== 捏合比基础 ==")
    r = pinch_ratio(TIGHT)
    check("捏合手比值 ≈0.14", round(r, 3), 0.141)
    r2 = pinch_ratio(OPEN)
    check("张开手比值 ≈1.08", round(r2, 3), 1.082)
    check("退化尺度返回 None", pinch_ratio(_hand({0: (5, 5), 4: (5, 5), 8: (5, 5), 9: (5, 5)})), None)
    check("points_box", points_box(TIGHT), (0.0, 0.0, 20.0, 100.0))

    print("\n== 人形验真（2026-10-09 事故回归）==")
    check("没有人形框 → 判阴性（空场景）",
          is_ok(TIGHT, person_boxes=[], hand_box=points_box(TIGHT)), False)
    check("person_boxes=[] （查了，没人）→ 判阴性",
          is_ok(TIGHT, person_boxes=[], hand_box=points_box(TIGHT)), False)

    person = [(700.0, 350.0, 900.0, 700.0)]
    check("手在人框内 → 过闸且判 OK",
          is_ok(TIGHT, person_boxes=person, hand_box=(760.0, 400.0, 820.0, 470.0)), True)
    check("手在人框内但张开 → 阴性",
          is_ok(OPEN, person_boxes=person, hand_box=points_box(OPEN)), False)

    # 现场真实坐标：人形框 x1=701.3，假手中心 x=639（框外左侧 62px）
    live_person = [(701.3, 381.8, 884.0, 679.8)]
    fake_hand = (568.0, 400.0, 710.0, 524.0)
    check("现场假手位置（框外）→ 拦住",
          hand_inside_person(fake_hand, live_person), False)
    check("现场假手位置 → 不判 OK",
          is_ok(TIGHT, person_boxes=live_person, hand_box=fake_hand), False)

    print("\n== 外扩容忍 ==")
    check("框顶上方 20px（框高 300，容忍 45px）→ 算同一人",
          hand_inside_person((780.0, 330.0, 820.0, 365.0), person), True)
    check("框顶上方 180px → 不算", hand_inside_person((780.0, 200.0, 820.0, 240.0), person), False)
    check("退化人形框被忽略",
          hand_inside_person((10.0, 10.0, 20.0, 20.0), [[0.0, 0.0, 0.0, 0.0]]), False)
    check("畸形人形框被忽略",
          hand_inside_person((10.0, 10.0, 20.0, 20.0), [None, [], [1, 2, 3]]), False)
    check("外扩系数在合理量级", 0.0 <= PERSON_BOX_PAD <= 0.5, True)

    print("\n== 向后兼容（不传 person_boxes）==")
    check("捏合 → True", is_ok(TIGHT), True)
    check("张开 → False", is_ok(OPEN), False)

    print(f"\n通过 {PASSED}，失败 {FAILED}")
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
