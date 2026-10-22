# -*- coding: utf-8 -*-
"""手势几何判据（纯函数，不依赖 onnxruntime / cv2，可单独测试）。

支持两种手势：
  - OK（捏合）：拇指尖(4)与食指尖(8)捏合比 < PINCH_MAX
  - 竖拇指（thumbs_up）：拇指尖(4)高于其他四指尖，且其他四指弯曲
"""
from __future__ import annotations

PINCH_MAX = 0.35  # 捏合比阈值（拇指食指距离 / 腕到中指根距离）
PERSON_BOX_PAD = 1.4  # 手框验真时人形框外扩倍数


def pinch_ratio(points) -> float | None:
    """计算捏合比：拇指尖(4)与食指尖(8)距离 / 腕(0)到中指根(9)距离。"""
    if points is None or len(points) < 10:
        return None
    thumb_tip = points[4]  # 拇指尖
    index_tip = points[8]  # 食指尖
    wrist = points[0]  # 腕
    middle_mcp = points[9]  # 中指根

    dist_tips = ((thumb_tip[0] - index_tip[0]) ** 2 + (thumb_tip[1] - index_tip[1]) ** 2) ** 0.5
    dist_wrist = ((wrist[0] - middle_mcp[0]) ** 2 + (wrist[1] - middle_mcp[1]) ** 2) ** 0.5

    if dist_wrist < 1e-6:
        return None
    return dist_tips / dist_wrist


def is_thumbs_up(points) -> bool | None:
    """判断竖拇指手势：拇指尖(4)高于其他四指尖，且至少3个手指弯曲。"""
    if points is None or len(points) < 21:
        return None

    thumb_tip = points[4]  # 拇指尖
    thumb_mcp = points[2]  # 拇指根部关节

    # 拇指必须向上（尖比根高）
    if thumb_tip[1] >= thumb_mcp[1]:
        return False

    # 拇指尖必须高于其他四指尖
    fingertips = [points[8], points[12], points[16], points[20]]  # 食指、中指、无名指、小指尖
    for tip in fingertips:
        if thumb_tip[1] >= tip[1]:  # y坐标小 = 更高
            return False

    # 其他四指必须弯曲（至少3个手指的尖比根部关节低）
    finger_mcps = [points[5], points[9], points[13], points[17]]  # 食、中、无名、小指根部
    bent_count = 0
    for i in range(4):
        if fingertips[i][1] > finger_mcps[i][1]:  # 尖比根低 = 弯曲
            bent_count += 1

    return bent_count >= 3


def points_box(points) -> list[float] | None:
    """计算关键点外接框 [x1, y1, x2, y2]。"""
    if points is None or len(points) == 0:
        return None
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    return [min(xs), min(ys), max(xs), max(ys)]


def hand_inside_person(hand_box, person_boxes, pad=1.0):
    """检查手框中心是否在任一人形框内（人框外扩 pad 倍后检查）。"""
    if hand_box is None or not person_boxes:
        return False

    hx1, hy1, hx2, hy2 = hand_box
    hcx = (hx1 + hx2) / 2.0
    hcy = (hy1 + hy2) / 2.0

    for person_box in person_boxes:
        if len(person_box) < 4:
            continue
        px1, py1, px2, py2 = person_box[:4]
        pw = px2 - px1
        ph = py2 - py1
        expand = ((pad - 1.0) / 2.0)
        px1 -= pw * expand
        py1 -= ph * expand
        px2 += pw * expand
        py2 += ph * expand

        if px1 <= hcx <= px2 and py1 <= hcy <= py2:
            return True

    return False


# MediaPipe 21 关键点名称
LANDMARKS_NAMES = [
    "WRIST", "THUMB_CMC", "THUMB_MCP", "THUMB_IP", "THUMB_TIP",
    "INDEX_MCP", "INDEX_PIP", "INDEX_DIP", "INDEX_TIP",
    "MIDDLE_MCP", "MIDDLE_PIP", "MIDDLE_DIP", "MIDDLE_TIP",
    "RING_MCP", "RING_PIP", "RING_DIP", "RING_TIP",
    "PINKY_MCP", "PINKY_PIP", "PINKY_DIP", "PINKY_TIP",
]
