"""preflight 中可选 det 配置的离线回归。"""

import importlib.util
from pathlib import Path


PREFLIGHT_PATH = Path(__file__).resolve().parents[1] / "tools" / "preflight.py"
SPEC = importlib.util.spec_from_file_location("preflight_under_test", PREFLIGHT_PATH)
preflight = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(preflight)


def _model_fixture(monkeypatch, tmp_path, *, det_exists):
    models = {}
    for key in ("seg", "clip", "matte"):
        path = tmp_path / (key + ".onnx")
        path.write_bytes(b"x" * (preflight.MODEL_MIN_BYTES + 1))
        models[key] = str(path)
    det_path = tmp_path / "det.onnx"
    if det_exists:
        det_path.write_bytes(b"x" * (preflight.MODEL_MIN_BYTES + 1))
    models["det"] = str(det_path)
    bpe = tmp_path / "bpe_merges.txt"
    bpe.write_text("x" * 1001, encoding="utf-8")
    monkeypatch.setattr(preflight, "MODEL_DEFAULTS", models)
    monkeypatch.setattr(preflight, "BPE", str(bpe))


def _det_check(report):
    return next(item for item in report.checks
                if item["title"].startswith("检测（人形框"))


def test_det_model_is_optional_by_default(monkeypatch, tmp_path):
    monkeypatch.delenv("BOOTH_DET_ENABLED", raising=False)
    _model_fixture(monkeypatch, tmp_path, det_exists=False)
    report = preflight.Report()

    problems = preflight.check_models(report)

    assert "det" not in problems
    assert _det_check(report)["status"] == "SKIP"


def test_explicit_det_enabled_keeps_required_model_check(monkeypatch, tmp_path):
    monkeypatch.setenv("BOOTH_DET_ENABLED", "true")
    _model_fixture(monkeypatch, tmp_path, det_exists=False)
    report = preflight.Report()

    problems = preflight.check_models(report)

    assert "det" in problems
    assert _det_check(report)["status"] == "FAIL"


def test_det_health_warning_is_skipped_when_disabled(monkeypatch):
    monkeypatch.delenv("BOOTH_DET_ENABLED", raising=False)
    report = preflight.Report()
    health = {
        "ep": True,
        "providers": ["SpaceMITExecutionProvider"],
        "ep_threads": 4,
        "seg_model": "seg.onnx",
        "det_model": None,
        "det_ep": False,
    }

    preflight.shoot_ai_checks(report, health, "fake")

    assert not any(item["title"] == "检测模型未加载" for item in report.checks)
    assert any(item["title"] == "检测模型" and item["status"] == "SKIP"
               for item in report.checks)


def test_explicit_det_enabled_keeps_health_warning(monkeypatch):
    monkeypatch.setenv("BOOTH_DET_ENABLED", "true")
    report = preflight.Report()
    health = {
        "ep": True,
        "providers": ["SpaceMITExecutionProvider"],
        "ep_threads": 4,
        "seg_model": "seg.onnx",
        "det_model": None,
        "det_ep": False,
    }

    preflight.shoot_ai_checks(report, health, "fake")

    assert any(item["title"] == "检测模型未加载" and item["status"] == "WARN"
               for item in report.checks)
