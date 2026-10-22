# -*- coding: utf-8 -*-
"""主体选择规则：可调参数集中在这里，现场不改代码、不重启即可调整。

为什么单独一个模块：
- 「谁算主体」是**现场标定**出来的，不是拍脑袋定的常数。展台的拍摄位在哪、
  引导线偏左还是偏右、人站得离镜头多近，都得看实景；写死在 worker.py 里的
  数字，现场要改就得改代码、重新部署。
- 两个进程都要读：worker 子进程做打分，booth_server 提供操作员接口与预检。

配置来源（优先级从高到低）：
  1. 环境变量 BOOTH_SUBJECT_*（systemd drop-in 里设，适合开机固定）
  2. <数据目录>/settings.json 的 "subject" 段（操作员面板写，适合现场微调）
  3. 本文件的默认值

**改完立即生效**：worker 每次打分都按文件 mtime 判断要不要重读，所以现场在
面板上改一下、下一个人拍照就是新规则，不用重启服务、更不用重启 worker。

坐标口径：一律用**画面比例（0~1）**，不用像素 —— 相机分辨率换回 720p 或
换成 4K 时，同一份配置照样成立。
"""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

__all__ = ["rules", "DEFAULTS", "rules_path", "write_rules", "describe"]

_LOCK = threading.Lock()
_CACHE: dict | None = None
_CACHE_STAMP: tuple | None = None
_CACHE_AT = 0.0
_RECHECK_S = 2.0     # 同一进程最多每 2 秒 stat 一次，别在热路径上反复碰盘

# 默认值说明（现场可按实景改）：
#   anchor      —— 拍摄位的**中心点在画面里的位置**（比例）。默认画面正中；
#                  如果展台把相机装偏了、引导线在画面偏左，就调成 [0.42, 0.5]。
#   weights     —— 三项各占多少。越大表示"越看重"。
#                  area=最近最大、center=最靠近 anchor、completeness=没被边缘切掉。
#   area_full   —— 框面积占画面多少比例就算"满分的近"。0.45 表示占到 45% 封顶；
#                  拍摄位如果离得远，调小它（如 0.18），免得所有人都是低分。
#   min_box_h   —— 框高至少占画面多少才算"到场的人"（过滤远处的路人）。
#   pad         —— 主体框往外扩一点再送去抠像（比例），避免贴边切到头发/肩膀。
DEFAULTS: dict = {
    "anchor": [0.5, 0.5],
    "weights": {"area": 0.444, "center": 0.333, "completeness": 0.222},
    "area_full": 0.45,
    "min_box_h": 0.22,
    "pad": 0.06,
    # AIGC 送图时人像在画布上的摆位（比例）。这三个曾经写死在 aigc_client.py 里，
    # 现场想让人更大/更靠上只能改代码 —— 现在可调：
    #   plate_scale_x/y —— 人像最多占画布宽/高的多少
    #   plate_top       —— 人像顶部距画布顶部的距离（留白给后期加的标题条）
    "plate_scale_x": 0.74,
    "plate_scale_y": 0.84,
    "plate_top": 0.10,
}


def rules_path() -> Path:
    """settings.json 的位置。与 booth_server 同一口径（BOOTH_DATA 可覆盖）。"""
    base = os.environ.get("BOOTH_DATA")
    root = Path(base) if base else Path(__file__).resolve().parent / "data"
    return root / "settings.json"


def _coerce_number(value, fallback: float, lo: float, hi: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return float(fallback)
    if number != number:          # NaN
        return float(fallback)
    return max(lo, min(hi, number))


def _coerce_anchor(value, fallback) -> list[float]:
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        return list(fallback)
    return [_coerce_number(value[0], fallback[0], 0.0, 1.0),
            _coerce_number(value[1], fallback[1], 0.0, 1.0)]


def _normalize(raw: dict) -> dict:
    """把外部配置收成一份合法规则：坏值逐项退回默认，绝不因为一行写错就崩掉拍摄。"""
    out = {
        "anchor": _coerce_anchor(raw.get("anchor"), DEFAULTS["anchor"]),
        "area_full": _coerce_number(raw.get("area_full"), DEFAULTS["area_full"], 0.01, 1.0),
        "min_box_h": _coerce_number(raw.get("min_box_h"), DEFAULTS["min_box_h"], 0.01, 1.0),
        "pad": _coerce_number(raw.get("pad"), DEFAULTS["pad"], 0.0, 0.5),
        "plate_scale_x": _coerce_number(
            raw.get("plate_scale_x"), DEFAULTS["plate_scale_x"], 0.10, 1.0),
        "plate_scale_y": _coerce_number(
            raw.get("plate_scale_y"), DEFAULTS["plate_scale_y"], 0.10, 1.0),
        "plate_top": _coerce_number(
            raw.get("plate_top"), DEFAULTS["plate_top"], 0.0, 0.9),
        "weights": {},
    }
    given = raw.get("weights") if isinstance(raw.get("weights"), dict) else {}
    weights = {}
    for key, default in DEFAULTS["weights"].items():
        # 权重是**相对值**，上限放到 100 而不是 1：写 {"area": 10, "center": 1}
        # 是在说"面积比居中重要十倍"。夹到 1 会把这个意图压平，两个都变 1.0。
        weights[key] = _coerce_number(given.get(key), default, 0.0, 100.0)
    total = sum(weights.values())
    if total <= 0:
        weights = dict(DEFAULTS["weights"])      # 全 0 等于没有偏好，退回默认
        total = sum(weights.values())
    out["weights"] = {key: round(value / total, 6) for key, value in weights.items()}
    return out


def _env_overrides() -> dict:
    """环境变量覆盖（systemd drop-in 用）。只认明确写了的键。"""
    raw: dict = {}
    anchor = os.environ.get("BOOTH_SUBJECT_ANCHOR", "").strip()
    if anchor:
        parts = [p for p in anchor.replace("，", ",").split(",") if p.strip()]
        if len(parts) == 2:
            raw["anchor"] = parts
    for key, env in (("area_full", "BOOTH_SUBJECT_AREA_FULL"),
                     ("min_box_h", "BOOTH_SUBJECT_MIN_BOX_H"),
                     ("pad", "BOOTH_SUBJECT_PAD"),
                     ("plate_scale_x", "BOOTH_SUBJECT_PLATE_SCALE_X"),
                     ("plate_scale_y", "BOOTH_SUBJECT_PLATE_SCALE_Y"),
                     ("plate_top", "BOOTH_SUBJECT_PLATE_TOP")):
        value = os.environ.get(env, "").strip()
        if value:
            raw[key] = value
    weights = {}
    weight_env = {
        "area": "BOOTH_SUBJECT_W_AREA",
        "center": "BOOTH_SUBJECT_W_CENTER",
        "completeness": "BOOTH_SUBJECT_W_COMPLETENESS",
    }
    for key, env in weight_env.items():
        value = os.environ.get(env, "").strip()
        if value:
            weights[key] = value
    if weights:
        raw["weights"] = weights
    return raw


def _read_file() -> dict:
    path = rules_path()
    try:
        if not path.is_file():
            return {}
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    if not isinstance(loaded, dict):
        return {}
    section = loaded.get("subject")
    return section if isinstance(section, dict) else {}


def rules(force: bool = False) -> dict:
    """当前生效的主体规则。带 mtime 缓存，改完文件最多 2 秒内生效。"""
    global _CACHE, _CACHE_STAMP, _CACHE_AT
    path = rules_path()
    try:
        stat = path.stat()
        stamp = (str(path), stat.st_mtime_ns, stat.st_size)
    except OSError:
        stamp = (str(path), None, None)

    with _LOCK:
        now = time.time()
        fresh = (_CACHE is not None and _CACHE_STAMP == stamp
                 and (now - _CACHE_AT) < _RECHECK_S)
        if fresh and not force:
            return _CACHE
        if _CACHE is not None and _CACHE_STAMP == stamp and not force:
            _CACHE_AT = now
            return _CACHE
        merged = dict(_read_file())
        merged.update(_env_overrides())
        _CACHE = _normalize(merged)
        _CACHE_STAMP = stamp
        _CACHE_AT = now
        return _CACHE


def write_rules(section: dict) -> dict:
    """写回 settings.json 的 "subject" 段（整份读改写，先临时文件再替换）。"""
    path = rules_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    whole: dict = {}
    try:
        if path.is_file():
            loaded = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                whole = loaded
    except Exception:
        whole = {}
    whole["subject"] = _normalize(section)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(whole, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)
    return rules(force=True)


def describe() -> dict:
    """给面板/预检看的：当前生效值 + 来源，便于现场判断"我改的生效了没"。"""
    current = rules()
    path = rules_path()
    env_keys = [key for key in
                ("BOOTH_SUBJECT_ANCHOR", "BOOTH_SUBJECT_AREA_FULL",
                 "BOOTH_SUBJECT_MIN_BOX_H", "BOOTH_SUBJECT_PAD",
                 "BOOTH_SUBJECT_PLATE_SCALE_X", "BOOTH_SUBJECT_PLATE_SCALE_Y",
                 "BOOTH_SUBJECT_PLATE_TOP",
                 "BOOTH_SUBJECT_W_AREA", "BOOTH_SUBJECT_W_CENTER",
                 "BOOTH_SUBJECT_W_COMPLETENESS")
                if os.environ.get(key, "").strip()]
    return {
        "rules": current,
        "defaults": DEFAULTS,
        "settings_path": str(path),
        "settings_exists": path.is_file(),
        "env_overrides": env_keys,
        "note": "环境变量优先于 settings.json；改完最多 2 秒生效，不必重启。",
    }
