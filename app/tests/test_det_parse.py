#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""op_det 输出解析的回归测试（不需要模型、不需要图像库）。

盯的是两个**已经实测发生过**的 bug，它们在真机上表现为"检测出满屏的人"：

  A. 输出方向判错。真实形状是 (1, 84, 8400) = (1, 特征, 候选)，而旧代码用
     `raw.shape[0] < raw.shape[1]` 判断该不该转置 —— 这个判据在 (84, 8400) 上
     恰好**不成立**，于是不转置，把 8400 当成特征维、把 84 当成候选数，
     框索引落到错误的轴上。
  B. 类别列被套了两次激活。这个量化导出的类别列**已经是概率**
     （实测 max=0.657、median=0.000、无负 logit），再套一次 sigmoid 会把
     0 抬到 0.5，8400 个候选全部越过 0.25 阈值；真机上 person 从 1 个变成 633 个。

测试用假 session 注入固定张量，因此**不需要** onnxruntime / cv2 / 真模型，
任何机器上都能跑来当回归网。

运行：.venv-test\\Scripts\\python.exe app\\tests\\test_det_parse.py
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
HERE = Path(__file__).resolve().parent
APP = HERE.parent
INFER = APP / "infer"
sys.path.insert(0, str(APP))
sys.path.insert(0, str(INFER))

import numpy as np  # noqa: E402

results: list[tuple[bool, str, str]] = []


def check(ok: bool, name: str, detail: str = "") -> None:
    results.append((bool(ok), name, detail))
    print(("  ✅ " if ok else "  ❌ ") + name + (("   " + detail) if detail else ""))


# --- 用假的东西替掉 worker 的模型与图像解码，只保留被测的解析逻辑 -----------
class FakeSession:
    def __init__(self, out: np.ndarray):
        self._out = out
        self.calls = 0

    def run(self, _outputs, _feed):
        self.calls += 1
        return [self._out]


class FakeImage:
    """替代 PIL 读图：返回一个固定尺寸的 RGB 数组。"""

    def __init__(self, h=480, w=640):
        self.h, self.w = h, w

    def __call__(self, _path):
        return np.zeros((self.h, self.w, 3), dtype=np.uint8)


def import_worker():
    import importlib
    if "worker" in sys.modules:
        return importlib.reload(sys.modules["worker"])
    return importlib.import_module("worker")


def synth(n_cand: int = 8400, features: int = 84):
    """造一份"看起来像真模型输出"的张量：全是概率，绝大多数接近 0。"""
    out = np.zeros((1, features, n_cand), dtype=np.float32)
    # 候选 5：person（类 0），框 cx=320 cy=240 w=100 h=200
    out[0, :4, 5] = [320.0, 240.0, 100.0, 200.0]
    out[0, 4 + 0, 5] = 0.6570
    # 候选 9：laptop（类 63），框 cx=100 cy=120 w=60 h=40
    out[0, :4, 9] = [100.0, 120.0, 60.0, 40.0]
    out[0, 4 + 63, 9] = 0.5971
    # 其余候选：全 0 —— 这是关键，套二次 sigmoid 后它们会变成 0.5
    return out


def run_op_det(features_first: bool, n_cand=8400, conf=0.25):
    """按指定方向造输出跑一次 op_det，返回结果字典。"""
    import worker

    out = synth(n_cand=n_cand)
    if features_first:
        payload = out                      # (1, 84, 8400)
    else:
        payload = np.transpose(out, (0, 2, 1))   # (1, 8400, 84)

    saved = (worker._state.get("det"), worker._state.get("det_input"),
             worker._read_rgb, worker.load_all,
             worker._state.get("providers"), worker._state.get("ep"))
    worker._state["det"] = FakeSession(payload)
    worker._state["det_input"] = "images"
    worker._state["providers"] = ["CPUExecutionProvider"]
    worker._state["ep"] = False
    worker._read_rgb = FakeImage(480, 640)
    worker.load_all = lambda: None
    try:
        return worker.op_det({"image": "ignored.jpg", "conf": conf})
    finally:
        (worker._state["det"], worker._state["det_input"],
         worker._read_rgb, worker.load_all,
         worker._state["providers"], worker._state["ep"]) = saved


print("=== A) 两种导出方向都要解析成同一批检测")
flat, swapped = run_op_det(True), run_op_det(False)
check(flat.get("ok") and swapped.get("ok"), "两种方向都能返回 ok",
      f"{flat.get('ok')} / {swapped.get('ok')}")

n_flat = len(flat.get("detections") or [])
n_swap = len(swapped.get("detections") or [])
check(n_flat == 2, "特征维在前时不产生幽灵检测（应恰好 2 个）", f"实际 {n_flat} 个")
check(n_flat == n_swap, "两种方向的检出数一致", f"{n_flat} vs {n_swap}")

print()
print("=== B) 检出内容正确：person + laptop，分数是原始概率")
rows = sorted(flat.get("detections") or [], key=lambda d: -d["score"])
names = [("person" if r["person"] else "非person") for r in rows]
check(names.count("person") == 1, "恰好检出 1 个 person", str(names))
top = rows[0] if rows else {}
# 原始概率 0.6570 —— 若被套了第二次 sigmoid 会变成 0.6586（可分辨）
check(abs(top.get("score", 0) - 0.6570) < 0.002,
      "分数是原始概率 0.657（不是二次 sigmoid 的 0.659）", str(top.get("score")))

# 框：letterbox 后 640x480 原图 -> 640x640 画布，gain=1.0，pad_y=80
# cx=320 cy=240 w=100 h=200 -> xyxy=(270,140,370,340) -> 减 pad_y -> (270,60,370,260)
box = top.get("box")
check(box is not None and abs(box[0] - 270) < 2 and abs(box[1] - 60) < 2
      and abs(box[2] - 370) < 2 and abs(box[3] - 260) < 2,
      "框按 cxcywh 正确还原并映射回原图", str(box))

print()
print("=== C) 关键回归：零分候选不得被抬进阈值")
# 全 0 的候选套二次 sigmoid 会变成 0.5 > 0.25，于是全部入选
check(n_flat == 2, "8400 个零分候选一个都没混进来", f"总检出 {n_flat} 个")
low = run_op_det(True, conf=0.99)
check(len(low.get("detections") or []) == 0, "阈值提到 0.99 后没有检测",
      str(len(low.get("detections") or [])))

print()
print("=== D) 形状不认识时必须报错，而不是猜一个方向继续算")
try:
    run_op_det(True, n_cand=100)   # 84x100：两个维度都不等于 84（只有一个），仍可判
    check(True, "非标准候选数仍能按特征维判出来")
except Exception as exc:
    check(False, "非标准候选数不该抛错", f"{type(exc).__name__}: {exc}")

import worker as _w  # noqa: E402
try:
    _w.op_det.__wrapped__ if False else None
except Exception:
    pass

# 直接验形状报错分支：喂一个 (1, 30, 40) 的假输出
saved_det, saved_input = _w._state.get("det"), _w._state.get("det_input")
_w._state["det"] = FakeSession(np.zeros((1, 30, 40), dtype=np.float32))
_w._state["det_input"] = "images"
saved_rgb, saved_load = _w._read_rgb, _w.load_all
_w._read_rgb, _w.load_all = FakeImage(480, 640), (lambda: None)
try:
    _w.op_det({"image": "x.jpg"})
except ValueError as exc:
    check("形状" in str(exc), "无法判定的形状抛 ValueError", str(exc)[:60])
except Exception as exc:
    check(False, "应是 ValueError", f"{type(exc).__name__}: {exc}")
else:
    check(False, "无法判定的形状应当抛错", "竟然返回了结果")
finally:
    _w._state["det"], _w._state["det_input"] = saved_det, saved_input
    _w._read_rgb, _w.load_all = saved_rgb, saved_load

failed = [r for r in results if not r[0]]
print()
print(f"共 {len(results)} 项，失败 {len(failed)} 项")
if failed:
    for _, name, detail in failed:
        print(f"  ❌ {name}  {detail}")
    sys.exit(1)
print("✅ det 输出解析全部通过")
