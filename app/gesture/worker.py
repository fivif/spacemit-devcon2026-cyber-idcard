# -*- coding: utf-8 -*-
"""手势 worker：独立进程，stdin/stdout 行分隔 JSON。

流水线：PalmDetector(palm.onnx) 全图找手 -> 裁出手区域 -> HandPose(handpose.onnx) 跑
21 关键点 -> gesture_rules 判手势。

比旧的"两遍 handpose"稳：palm.onnx 是专为全图扫描设计的 SSD 检测器，远距离、
侧角度都能找到手；旧的两遍法把全图喂进 224x224 近景模型，远距离手太小会漏检。

进程纪律（不能改）：
  本进程用 cv2 + onnxruntime(CPU)，绝不 import spacemit_ort。
  AI 核已被 infer/worker.py 占住，这里只用 CPU。
"""
from __future__ import annotations

import json
import os
import sys
import time

import numpy as np
import cv2
import onnxruntime as ort

HERE = os.path.dirname(os.path.abspath(__file__))
APP = os.path.dirname(HERE)
if APP not in sys.path:
    sys.path.insert(0, APP)

from gesture_rules import (  # noqa: E402
    PINCH_MAX,
    PERSON_BOX_PAD,
    hand_inside_person,
    is_thumbs_up,
    pinch_ratio,
    points_box,
)

SENTINEL = "@@J "
PALM_INPUT = 192
POSE_INPUT = 224
HAND_PAD = 1.4
PALM_CONF_MIN = float(os.environ.get("BOOTH_GESTURE_PALM_CONF", "0.6"))
HAND_CONF_MIN = float(os.environ.get("BOOTH_GESTURE_HAND_CONF", "0.30"))

DEFAULT_PALM = "/opt/devcon/assets/models/gesture/palm.onnx"
DEFAULT_HANDPOSE = "/opt/devcon/assets/models/gesture/handpose.onnx"

_PALM_ANCHORS = None


def _make_palm_anchors():
    global _PALM_ANCHORS
    if _PALM_ANCHORS is not None:
        return _PALM_ANCHORS
    strides = [8, 16]
    anchors_per_stride = [2, 6]
    anchors = []
    for stride, n_anchors in zip(strides, anchors_per_stride):
        grid = PALM_INPUT // stride
        for y in range(grid):
            for x in range(grid):
                cx = (x + 0.5) / grid
                cy = (y + 0.5) / grid
                for _ in range(n_anchors):
                    anchors.append([cx, cy])
    _PALM_ANCHORS = np.array(anchors, dtype=np.float32)
    return _PALM_ANCHORS


def _session(path):
    options = ort.SessionOptions()
    threads = max(1, int(os.environ.get("BOOTH_GESTURE_ORT_THREADS", "1")))
    options.intra_op_num_threads = threads
    options.inter_op_num_threads = 1
    options.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
    options.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
    return ort.InferenceSession(
        path, sess_options=options, providers=["CPUExecutionProvider"])


def _read_bgr(path):
    try:
        img = cv2.imread(path)
        return img if img is not None and img.size > 0 else None
    except Exception:
        return None


class PalmDetector:
    """MediaPipe Palm Detector: 全图找手掌，返回 [x1,y1,x2,y2] 列表。"""

    def __init__(self, model_path):
        self.session = _session(model_path)
        self.input_name = self.session.get_inputs()[0].name

    def detect(self, image, conf_min=None):
        if conf_min is None:
            conf_min = PALM_CONF_MIN
        h, w = image.shape[:2]
        scale = PALM_INPUT / max(h, w)
        nh = max(1, int(round(h * scale)))
        nw = max(1, int(round(w * scale)))
        resized = cv2.resize(image, (nw, nh), interpolation=cv2.INTER_AREA)
        canvas = np.zeros((PALM_INPUT, PALM_INPUT, 3), dtype=np.uint8)
        top = (PALM_INPUT - nh) // 2
        left = (PALM_INPUT - nw) // 2
        canvas[top:top + nh, left:left + nw] = resized
        rgb = cv2.cvtColor(canvas, cv2.COLOR_BGR2RGB)
        blob = (rgb.astype(np.float32) / 255.0)[np.newaxis]

        boxes_raw, scores_raw = self.session.run(None, {self.input_name: blob})
        boxes_raw = np.asarray(boxes_raw, dtype=np.float32).reshape(-1, 18)
        scores_raw = np.asarray(scores_raw, dtype=np.float32).reshape(-1)

        scores = 1.0 / (1.0 + np.exp(-np.clip(scores_raw, -60, 60)))
        keep = np.where(scores >= conf_min)[0]
        if keep.size == 0:
            return []

        anchors = _make_palm_anchors()
        results = []
        for i in keep:
            anc = anchors[i]
            cx = anc[0] + boxes_raw[i, 0] / PALM_INPUT
            cy = anc[1] + boxes_raw[i, 1] / PALM_INPUT
            bw = boxes_raw[i, 2] / PALM_INPUT
            bh = boxes_raw[i, 3] / PALM_INPUT
            x1 = ((cx - bw / 2) * PALM_INPUT - left) / scale
            y1 = ((cy - bh / 2) * PALM_INPUT - top) / scale
            x2 = ((cx + bw / 2) * PALM_INPUT - left) / scale
            y2 = ((cy + bh / 2) * PALM_INPUT - top) / scale
            x1 = max(0.0, x1)
            y1 = max(0.0, y1)
            x2 = min(float(w), x2)
            y2 = min(float(h), y2)
            if x2 > x1 and y2 > y1:
                results.append([x1, y1, x2, y2, float(scores[i])])

        results.sort(key=lambda r: -r[4])
        kept = []
        for r in results:
            merged = False
            for k in kept:
                ix1 = max(r[0], k[0])
                iy1 = max(r[1], k[1])
                ix2 = min(r[2], k[2])
                iy2 = min(r[3], k[3])
                iw = max(0.0, ix2 - ix1)
                ih = max(0.0, iy2 - iy1)
                inter = iw * ih
                area_r = (r[2] - r[0]) * (r[3] - r[1])
                area_k = (k[2] - k[0]) * (k[3] - k[1])
                if inter / (area_r + area_k - inter + 1e-6) > 0.4:
                    merged = True
                    break
            if not merged:
                kept.append(r)
        return [[r[0], r[1], r[2], r[3]] for r in kept]


class HandPose:
    """HandPose: 裁好的手区域 -> 21 关键点。"""

    def __init__(self, model_path):
        self.session = _session(model_path)
        self.input_name = self.session.get_inputs()[0].name

    def run(self, crop):
        h, w = crop.shape[:2]
        scale = POSE_INPUT / max(h, w)
        nh = max(1, int(round(h * scale)))
        nw = max(1, int(round(w * scale)))
        resized = cv2.resize(crop, (nw, nh), interpolation=cv2.INTER_AREA)
        canvas = np.zeros((POSE_INPUT, POSE_INPUT, 3), dtype=np.uint8)
        top = (POSE_INPUT - nh) // 2
        left = (POSE_INPUT - nw) // 2
        canvas[top:top + nh, left:left + nw] = resized
        rgb = cv2.cvtColor(canvas, cv2.COLOR_BGR2RGB)
        blob = (rgb.astype(np.float32) / 255.0)[np.newaxis]

        outs = self.session.run(None, {self.input_name: blob})
        landmarks = np.asarray(outs[0], dtype=np.float32).reshape(21, 3)
        conf = 0.0
        for out in outs:
            arr = np.asarray(out)
            if arr.size == 1:
                conf = float(arr.reshape(-1)[0])
        pts = landmarks[:, :2].copy()
        pts[:, 0] = (pts[:, 0] - left) / scale
        pts[:, 1] = (pts[:, 1] - top) / scale
        return pts, conf


_PALM = None
_POSE = None


def _detect_hand(image):
    """palm -> 找手框 -> 裁图 -> handpose -> 返回关键点，没找到返回 None。"""
    h, w = image.shape[:2]

    palm_boxes = _PALM.detect(image) if _PALM else []

    if palm_boxes:
        pb = palm_boxes[0]
        bw = pb[2] - pb[0]
        bh = pb[3] - pb[1]
        pad = max(bw, bh) * (HAND_PAD - 1.0) / 2.0
        x1 = int(max(0, pb[0] - pad))
        y1 = int(max(0, pb[1] - pad))
        x2 = int(min(w, pb[2] + pad))
        y2 = int(min(h, pb[3] + pad))
        crop = image[y1:y2, x1:x2]
        if crop.size == 0:
            return None
        pts, conf = _POSE.run(crop)
        if conf < HAND_CONF_MIN:
            return None
        pts[:, 0] += x1
        pts[:, 1] += y1
        return {"points": pts, "confidence": round(float(conf), 4),
                "palm_box": [pb[0], pb[1], pb[2], pb[3]]}

    if os.environ.get("BOOTH_GESTURE_ALLOW_POSE_FALLBACK", "").strip().lower() not in ("1", "true", "yes"):
        return None

    # fallback: 没有 palm 框时用两遍法
    if _POSE is None:
        return None
    pts, conf = _POSE.run(image)
    if conf < HAND_CONF_MIN:
        return None
    x1_f = float(pts[:, 0].min())
    y1_f = float(pts[:, 1].min())
    x2_f = float(pts[:, 0].max())
    y2_f = float(pts[:, 1].max())
    cx = (x1_f + x2_f) / 2
    cy = (y1_f + y2_f) / 2
    half = max(x2_f - x1_f, y2_f - y1_f) * HAND_PAD / 2
    bx1 = int(max(0, cx - half))
    by1 = int(max(0, cy - half))
    bx2 = int(min(w, cx + half))
    by2 = int(min(h, cy + half))
    crop2 = image[by1:by2, bx1:bx2]
    if crop2.size > 0:
        pts2, conf2 = _POSE.run(crop2)
        if conf2 >= HAND_CONF_MIN:
            pts2[:, 0] += bx1
            pts2[:, 1] += by1
            return {"points": pts2, "confidence": round(float(conf2), 4)}
    return {"points": pts, "confidence": round(float(conf), 4)}


def op_ok(request):
    path = str(request.get("image") or "")
    gesture_type = str(request.get("gesture") or "thumbs_up").strip().lower()
    started = time.perf_counter()
    if not path:
        return {"ok": False, "error": "missing image"}

    raw_boxes = request.get("person_boxes")
    person_boxes = []
    if isinstance(raw_boxes, (list, tuple)):
        for b in raw_boxes:
            if isinstance(b, (list, tuple)) and len(b) >= 4:
                person_boxes.append([float(v) for v in b[:4]])

    image = _read_bgr(path)
    if image is None:
        return {"ok": False, "error": "cannot read image: %s" % path}

    found = _detect_hand(image)
    elapsed = round((time.perf_counter() - started) * 1000.0, 1)

    if found is None:
        return {"ok": True, "gesture": "none", "confidence": None,
                "raw_gesture": "none", "pinch": None, "hand_count": 0,
                "person_ok": False, "points": [], "box": None,
                "palm_box": None, "image_width": int(image.shape[1]),
                "image_height": int(image.shape[0]), "ms": elapsed}

    points = found["points"]
    confidence = found["confidence"]
    box = points_box(points)
    verified = hand_inside_person(box, person_boxes, pad=PERSON_BOX_PAD)

    ratio = pinch_ratio(points)
    if gesture_type == "ok":
        raw_verdict = ratio is not None and ratio < PINCH_MAX
    else:
        result = is_thumbs_up(points)
        raw_verdict = result if result is not None else False
    verdict = bool(verified and raw_verdict)

    return {
        "ok": True,
        "gesture": gesture_type if verdict else "none",
        "raw_gesture": gesture_type if raw_verdict else "none",
        "available": verified,
        "pinch": None if ratio is None else round(float(ratio), 4),
        "confidence": confidence,
        "hand_count": 1,
        "person_ok": bool(verified),
        "box": None if box is None else [round(float(v), 1) for v in box],
        "palm_box": [round(float(v), 1) for v in found.get("palm_box", [])]
        if found.get("palm_box") else None,
        "points": [[round(float(x), 1), round(float(y), 1)] for x, y in points[:, :2]],
        "image_width": int(image.shape[1]),
        "image_height": int(image.shape[0]),
        "ms": elapsed,
    }


def op_health(request):
    return {
        "ok": True, "op": "health",
        "palm_model": DEFAULT_PALM,
        "handpose_model": DEFAULT_HANDPOSE,
        "palm_loaded": _PALM is not None,
        "pose_loaded": _POSE is not None,
        "providers": _POSE.session.get_providers() if _POSE else [],
        "pinch_max": PINCH_MAX,
        "person_pad": PERSON_BOX_PAD,
    }


HANDLERS = {"ok": op_ok, "health": op_health}


def _emit(payload):
    sys.stdout.write(SENTINEL + json.dumps(payload, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def main():
    global _PALM, _POSE
    palm_path = os.environ.get("BOOTH_GESTURE_PALM_PATH") or DEFAULT_PALM
    pose_path = os.environ.get("BOOTH_GESTURE_MODEL_PATH") or DEFAULT_HANDPOSE
    try:
        _PALM = PalmDetector(palm_path)
    except Exception as exc:
        _emit({"id": 0, "op": "boot", "ok": False,
               "error": "palm model load failed: %s: %s" % (type(exc).__name__, exc)})
    try:
        _POSE = HandPose(pose_path)
    except Exception as exc:
        _emit({"id": 0, "op": "boot", "ok": False,
               "error": "handpose model load failed: %s: %s" % (type(exc).__name__, exc)})
        return 1
    _emit({"id": 0, "op": "boot", "ok": True,
           "palm": palm_path, "handpose": pose_path,
           "providers": _POSE.session.get_providers()})

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
        except Exception:
            continue
        request_id = request.get("id", 0)
        op = str(request.get("op") or "ok")
        handler = HANDLERS.get(op)
        if handler is None:
            _emit({"id": request_id, "op": op, "ok": False,
                   "error": "unknown op %r" % op})
            continue
        try:
            payload = handler(request)
        except Exception as exc:
            payload = {"ok": False, "error": "%s: %s" % (type(exc).__name__, exc)}
        payload["id"] = request_id
        payload.setdefault("op", op)
        _emit(payload)
    return 0


if __name__ == "__main__":
    sys.exit(main())
