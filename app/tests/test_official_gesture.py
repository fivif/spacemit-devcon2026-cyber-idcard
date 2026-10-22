# -*- coding: utf-8 -*-
"""官方 YOLOv5 手势接入回归测试。

只用假 ONNX session 和假 provider，不加载真实模型、不访问 K3。
"""
from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest
from PIL import Image


APP_DIR = Path(__file__).resolve().parents[1]
PROJECT_ROOT = APP_DIR.parent
WORKER_PATH = APP_DIR / "infer" / "worker.py"

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.auto_flow.gesture_adapter import DisabledGestureAdapter, build_gesture_adapter  # noqa: E402
from app.gesture_client import GestureClient  # noqa: E402


class _FakeSession:
    def __init__(self, output: np.ndarray):
        self.output = output
        self.calls = []

    def run(self, output_names, feeds):
        self.calls.append((output_names, feeds))
        return [self.output]


def _load_worker_with_fake_session(output: np.ndarray):
    """独立 import worker，并预先注入状态，保证 load_all 不碰真实模型。"""
    module_name = "test_official_gesture_worker"
    spec = importlib.util.spec_from_file_location(module_name, WORKER_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules.pop(module_name, None)
    spec.loader.exec_module(module)
    session = _FakeSession(output)
    module._state.clear()
    module._state.update(
        gesture=session,
        gesture_input="images",
        gesture_providers=["FakeExecutionProvider"],
        gesture_ep=False,
    )
    return module, session


def _gesture_row(
    *,
    box=(320.0, 320.0, 100.0, 100.0),
    objectness=0.9,
    label=0,
    class_score=0.9,
) -> np.ndarray:
    row = np.zeros(24, dtype=np.float32)
    row[:4] = box
    row[4] = objectness
    row[5 + label] = class_score
    return row


def _image(tmp_path: Path, size=(320, 160)) -> Path:
    path = tmp_path / "frame.png"
    Image.new("RGB", size, (20, 30, 40)).save(path)
    return path


def _provider(detections=None, *, ok=True, error=None):
    payload = {"ok": ok, "detections": list(detections or [])}
    if error is not None:
        payload["error"] = error
    return lambda _image_path: payload


def _client(monkeypatch, *, gesture_provider, person_boxes_provider=None) -> GestureClient:
    monkeypatch.setenv("BOOTH_GESTURE_ENGINE", "official_yolo")
    monkeypatch.setenv("BOOTH_GESTURE_OK_SCORE", "0.45")
    return GestureClient(
        gesture_provider=gesture_provider,
        person_boxes_provider=person_boxes_provider,
    )


def test_worker_score_is_objectness_times_class_score(tmp_path):
    """class=0.99 但 obj=0.10 时，总分 0.099，不能越过 0.25 门槛。"""
    raw = _gesture_row(objectness=0.10, class_score=0.99)[None, None, :]
    worker, session = _load_worker_with_fake_session(raw)

    result = worker.op_gesture({"image": str(_image(tmp_path)), "conf": 0.25})

    assert result["ok"] is True
    assert result["detections"] == []
    assert len(session.calls) == 1


def test_worker_maps_letterbox_box_back_to_original_image(tmp_path):
    """320x160 原图上下补边 160px；输出框应正确去 padding、除 gain。"""
    # 原图框 [40,20,120,100] -> 640 letterbox 中 xyxy=[80,200,240,360]
    raw = _gesture_row(box=(160, 280, 160, 160), objectness=0.8, class_score=0.9)[
        None, None, :
    ]
    worker, _ = _load_worker_with_fake_session(raw)

    result = worker.op_gesture({"image": str(_image(tmp_path)), "conf": 0.25})

    assert result["detections"] == [
        {"label": "ok", "label_id": 0, "score": 0.72, "box": [40.0, 20.0, 120.0, 100.0]}
    ]


def test_worker_empty_output_does_not_report_gesture(tmp_path):
    worker, _ = _load_worker_with_fake_session(np.empty((1, 0, 24), dtype=np.float32))

    result = worker.op_gesture({"image": str(_image(tmp_path)), "conf": 0.25})

    assert result["ok"] is True
    assert result["detections"] == []


def test_worker_seg_uses_fixed_exported_class_head(tmp_path):
    """量化 YOLOE 导出固定 80 类槽位，提示词数量不能决定 mask 起点。"""
    worker, _ = _load_worker_with_fake_session(np.empty((1, 0, 24), dtype=np.float32))
    proto = np.zeros((1, 32, 2, 2), dtype=np.float32)
    prediction = np.zeros((1, 116, 1), dtype=np.float32)
    prediction[0, 4, 0] = 0.9
    class FakeSegSession:
        def run(self, output_names, feeds):
            return [prediction, proto]

    worker._state["seg"] = FakeSegSession()
    worker._state["seg_img_input"] = "images"
    worker._state["seg_text_input"] = "txt_feats"
    worker._state["providers"] = ["FakeExecutionProvider"]
    worker._state["ep"] = False
    worker._state["clip"] = None
    worker._state["text_cache"] = {}
    worker.text_features = lambda prompts: np.zeros((1, 80, 512), dtype=np.float32)

    result = worker.op_seg({"image": str(_image(tmp_path)), "prompts": ["person"], "mask": False, "matte": False})

    assert result["ok"] is True


def test_worker_nms_is_class_agnostic(tmp_path):
    """同位置不同类别也只能保留最高分框，避免一只手重复上报。"""
    raw = np.stack(
        [
            _gesture_row(label=0, objectness=0.8, class_score=0.8),
            _gesture_row(label=1, objectness=0.9, class_score=0.9),
        ]
    )[None, :, :]
    worker, _ = _load_worker_with_fake_session(raw)

    result = worker.op_gesture({"image": str(_image(tmp_path)), "conf": 0.25, "iou": 0.45})

    assert [(item["label"], item["score"]) for item in result["detections"]] == [
        ("stop", 0.81)
    ]


def test_official_visual_uses_provider_without_starting_subprocess(monkeypatch):
    def forbidden_popen(*_args, **_kwargs):
        raise AssertionError("official_yolo visual 不应启动独立 subprocess")

    monkeypatch.setattr(subprocess, "Popen", forbidden_popen)
    client = _client(
        monkeypatch,
        gesture_provider=_provider([{"label": "ok", "score": 0.88, "box": [20, 20, 80, 80]}]),
    )

    observed = client.observe_visual("unused.jpg")

    assert observed.available is True
    assert observed.gesture == "ok"
    assert observed.person_count == 0
    assert client._proc is None


@pytest.mark.parametrize(
    "gesture_provider",
    [
        pytest.param(lambda _path: (_ for _ in ()).throw(RuntimeError("provider down")), id="raises"),
        pytest.param(_provider(ok=False, error="provider unavailable"), id="error-response"),
    ],
)
def test_official_provider_failure_is_unavailable(monkeypatch, gesture_provider):
    client = _client(monkeypatch, gesture_provider=gesture_provider)

    observed = client.observe_visual("unused.jpg")

    assert observed.available is False
    assert observed.gesture == "none"
    assert observed.in_frame is not True


def test_official_visual_person_count_zero_means_unknown_not_no_person(monkeypatch):
    client = _client(monkeypatch, gesture_provider=_provider([]))

    observed = client.observe_visual("unused.jpg")

    assert observed.available is True
    assert observed.person_count == 0
    assert "无人" not in (observed.reason or "")
    assert "没有人" not in (observed.reason or "")


@pytest.mark.parametrize(
    ("person_boxes", "hand_box", "expected_gesture", "expected_count"),
    [
        pytest.param([], [40, 40, 80, 80], "none", 0, id="no-person"),
        pytest.param(
            [[0, 0, 200, 200], [220, 0, 420, 200]],
            [40, 40, 80, 80],
            "none",
            2,
            id="two-persons",
        ),
        pytest.param([[0, 0, 200, 200]], [260, 40, 300, 80], "none", 1, id="outside"),
        pytest.param([[0, 0, 200, 200]], [40, 40, 80, 80], "ok", 1, id="single-inside"),
    ],
)
def test_official_auto_observe_requires_one_person_and_hand_inside(
    monkeypatch, person_boxes, hand_box, expected_gesture, expected_count
):
    """自动路径人数只认 person provider；必须恰好一人且手框中心在人框内。"""
    client = _client(
        monkeypatch,
        gesture_provider=_provider([{"label": "ok", "score": 0.88, "box": hand_box}]),
        person_boxes_provider=lambda _path: person_boxes,
    )

    observed = client.observe("unused.jpg")

    assert observed.available is True
    assert observed.gesture == expected_gesture
    assert observed.person_count == expected_count
    assert observed.in_frame is (expected_gesture == "ok")


def test_official_auto_observe_does_not_invent_one_person(monkeypatch):
    client = _client(
        monkeypatch,
        gesture_provider=_provider([{"label": "ok", "score": 0.91, "box": [20, 20, 80, 80]}]),
        person_boxes_provider=lambda _path: [],
    )

    observed = client.observe("unused.jpg")

    assert observed.person_count == 0
    assert observed.gesture == "none"


def test_automatic_gesture_remains_disabled_by_default(monkeypatch):
    monkeypatch.delenv("BOOTH_GESTURE_ENABLED", raising=False)
    monkeypatch.delenv("BOOTH_GESTURE_VISUAL_ENABLED", raising=False)

    adapter = build_gesture_adapter()

    assert isinstance(adapter, DisabledGestureAdapter)
    safe_config = (APP_DIR / "deploy" / "k3-gesture-safe.conf").read_text(encoding="utf-8")
    assert "Environment=BOOTH_GESTURE_ENABLED=true" in safe_config
    assert "Environment=BOOTH_DET_ENABLED=false" in safe_config
    assert "Environment=BOOTH_GESTURE_DEVICE=ep" in safe_config
    assert "BOOTH_EP_SHARED_THREADS" not in safe_config
