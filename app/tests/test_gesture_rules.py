# -*- coding: utf-8 -*-
"""手势判据回归：**空场景绝不能被判成 OK 手势**（2026-10-09 线上事故的回归锁）。

事故：handpose 对着**没有手**的画面会强行拟合 21 个关键点。K3 实测对着空办公室
连拍 20 帧，20/20 帧都报出"一只手"（置信度 0.54~0.84），捏合比最低 0.0219 ——
比真 OK 手势（0.137）还像 OK。仅靠捏合比阈值 + 置信度门限**完全挡不住**，
一旦接上自动拍摄，机器会在空无一人的时候自己按快门。

修复：加"人形验真"判据 —— 关键点必须落在一张真实人形框里（框高≥min_box_h 的
人形检测结果）才往下判捏合比。本文件锁住这个行为。

这些用例是**纯逻辑**的（造坐标，不加载模型），可在开发机上跑：
    python3 -m pytest app/tests/test_gesture_rules.py -v
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from gesture_rules import (  # noqa: E402
    PERSON_BOX_PAD,
    hand_inside_person,
    is_ok,
    pinch_ratio,
    points_box,
)


def _hand(points):
    """按索引摆点：0=腕, 4=拇指尖, 8=食指尖, 9=中指根，其余补 (0,0)。

    早前版本用列表顺序拼点，把中指根(索引 9)写成了 (0,0)，导致手掌尺度为 0、
    pinch_ratio 恒返回 None —— 用字典明示索引，杜绝这种错位。
    """
    pts = [[0.0, 0.0, 0.0] for _ in range(21)]
    for index, (x, y) in points.items():
        pts[index] = [float(x), float(y), 0.0]
    return pts


# 捏合的手：腕(0,0)、中指根(0,100) → 手掌尺度 100；
# 拇指尖(10,10) 与食指尖(20,20) 相距 14.14 → 捏合比 0.1414（非常像 OK）
TIGHT_HAND = _hand({0: (0, 0), 4: (10, 10), 8: (20, 20), 9: (0, 100)})

# 张开的手：拇指尖(60,0) 与食指尖(0,90) 相距 108.2 → 比值 1.082
OPEN_HAND = _hand({0: (0, 0), 4: (60, 0), 8: (0, 90), 9: (0, 100)})


# ---- 纯几何 ---------------------------------------------------------------

def test_pinch_ratio_uses_palm_scale():
    """捏合比用"腕→中指根"当分母，与画面缩放无关。"""
    assert pinch_ratio(TIGHT_HAND) == pytest.approx(0.141, abs=0.01)
    assert pinch_ratio(OPEN_HAND) == pytest.approx(1.082, abs=0.01)


def test_pinch_ratio_rejects_degenerate_palm():
    """手掌尺度退化成 0 时返回 None（判不出），而不是拿 0 去除。"""
    flat = _hand({0: (5, 5), 4: (5, 5), 8: (5, 5), 9: (5, 5)})
    assert pinch_ratio(flat) is None


def test_points_box():
    box = points_box(TIGHT_HAND)
    assert box == (0.0, 0.0, 20.0, 100.0)


# ---- 人形验真：本次事故的核心 ---------------------------------------------

def test_no_person_boxes_never_ok():
    """★ 事故回归：画面里没有人形框时，再"像 OK"也不许判阳性。

    这正是现场发生的情形：空办公室里 handpose 拟合出一只捏合比 0.0219 的假手。

    注意 ``[]`` 与 ``None`` 语义不同，别混：
      ``[]``   = "查过了，画面里没有人" → 判阴性（生产路径，见 GestureClient.observe）
      ``None`` = "没查"（不传该参数）→ 跳过验真，只供纯函数单测用
    """
    assert is_ok(TIGHT_HAND, person_boxes=[], hand_box=points_box(TIGHT_HAND)) is False


def test_hand_inside_person_accepts_person_present():
    """手在人的框里 → 过闸，捏合比决定结果。"""
    person = [(700.0, 350.0, 900.0, 700.0)]
    hand = [(760.0, 400.0, 820.0, 470.0)]
    assert hand_inside_person(hand, person) is True
    assert is_ok(TIGHT_HAND, person_boxes=person,
                 hand_box=(760.0, 400.0, 820.0, 470.0)) is True


def test_far_hand_is_rejected_even_when_pinched():
    """★ 事故现场的真实几何：假手落在人形框**外面**（左边 60px），必须拦住。

    实测坐标（空办公室 003.jpg）：人形框 x1=701，假手中心 x=639。
    """
    person = [(701.3, 381.8, 884.0, 679.8)]
    hallucinated_hand = (568.0, 400.0, 710.0, 524.0)   # 中心 x=639
    assert hand_inside_person(hallucinated_hand, person) is False
    assert is_ok(TIGHT_HAND, person_boxes=person,
                 hand_box=hallucinated_hand) is False


def test_person_pad_tolerates_hand_at_edge():
    """人形框外扩 PERSON_BOX_PAD 后，贴边/举手到头顶仍算同一人。"""
    person = [(700.0, 400.0, 900.0, 700.0)]
    # 中心在框正上方 20px：人形框高 300 → 容忍 45px（0.15×300）
    above = (780.0, 330.0, 820.0, 365.0)   # 中心 y=347.5，框顶 400 − 45 = 355
    assert hand_inside_person(above, person) is True
    # 再远就不算（超出外扩范围）
    far = (780.0, 200.0, 820.0, 240.0)     # 中心 y=220
    assert hand_inside_person(far, person) is False


def test_degenerate_person_box_ignored():
    """退化的人形框（宽或高 <= 0）不参与判定，避免除零/误包含。"""
    assert hand_inside_person((10.0, 10.0, 20.0, 20.0), [[0.0, 0.0, 0.0, 0.0]]) is False
    assert hand_inside_person((10.0, 10.0, 20.0, 20.0), [None, [], [1, 2, 3]]) is False


def test_open_hand_on_person_is_not_ok():
    """人在，但手是张开的 → 不是 OK（捏合比不够小）。"""
    person = [(700.0, 350.0, 900.0, 700.0)]
    assert is_ok(OPEN_HAND, person_boxes=person,
                 hand_box=points_box(OPEN_HAND)) is False


def test_pad_constant_is_sane():
    """外扩系数必须是"可容忍"量级，不能大到把远处的人也算进来。"""
    assert 0.0 <= PERSON_BOX_PAD <= 0.5


# ---- 向后兼容 -------------------------------------------------------------

def test_backward_compatible_without_person_boxes():
    """不传 person_boxes 时保持旧行为（纯函数单测用），捏合比说了算。"""
    assert is_ok(TIGHT_HAND) is True
    assert is_ok(OPEN_HAND) is False
