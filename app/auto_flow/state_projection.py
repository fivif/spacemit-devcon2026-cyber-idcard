# -*- coding: utf-8 -*-
"""公共状态投影（T-20）：把内部排队 / 拍摄 / 组件状态压成可公开的脱敏字段。

规格依据：`自动流程规格 v1（内部文档）` 的「External HTTP and page contracts」
（`/api/state`、`/api/overlay.json` 不得暴露同意值、可选个人信息、取图令牌与完整
`queue_id`）与 T-20 任务合同。

对外契约（三个小节，键完全白名单）：
- queue:        {"counts": {九个队列状态: int}, "called_label": "第 N 位" | None}
- capture:      {"phase": 见 PUBLIC_CAPTURE_PHASES, "countdown_remaining": int | None}
- capabilities: 六个组件名 -> bool（只允许布尔，缺省 / 未知一律 False）

脱敏保证：本模块只从白名单语义键读值、只向白名单键写值。`queue_id`、`job_id`、
`request_id`、同意 / 幂等令牌、`photo_token`、个人字段（name / handle / phone 等）
与完整 SM 编号都不会出现在输出里；called_label 只保留「第 N 位」匿名展示串。

输入约定：内部状态用简单 dict / 普通对象传入（读 `status`、`phase`、
`display_number`、`countdown_remaining` 这些公开语义键）；本模块不 import T-1 的
模型，不耦合其实现细节，也不做任何 IO。
"""

from __future__ import annotations

import math
import re
from collections.abc import Iterable, Mapping
from typing import Any

__all__ = [
    "QUEUE_STATUSES",
    "PUBLIC_CAPTURE_PHASES",
    "COMPONENTS",
    "PUBLIC_STATE_SECTIONS",
    "called_label",
    "queue_counts",
    "public_queue_fields",
    "public_capture_fields",
    "public_capabilities",
    "public_state",
    "public_overlay_fields",
]


# 02-spec Data models 的 QueueStatus 全集（counts 固定输出这九个键）
QUEUE_STATUSES: tuple[str, ...] = (
    "queued",
    "called",
    "ready",
    "capturing",
    "processing",
    "ready_for_pickup",
    "expired",
    "cancelled",
    "failed",
)

# 公共拍摄阶段：idle 之外与 CaptureJob.status 同名，外加 created -> waiting 映射；
# 遇到无法识别时统一给 "unknown"（宁可显示未知，也不回显内部原值）。
PUBLIC_CAPTURE_PHASES: tuple[str, ...] = (
    "idle",
    "waiting",
    "countdown",
    "capturing",
    "processing",
    "completed",
    "failed",
    "unknown",
)

# 六个组件与 SystemHealth 同名（capabilities 固定输出这六个键）
COMPONENTS: tuple[str, ...] = ("camera", "gesture", "queue", "storage", "aigc", "printer")

PUBLIC_STATE_SECTIONS: tuple[str, ...] = ("queue", "capture", "capabilities")

_CAPTURE_STATUS_TO_PHASE: dict[str, str] = {
    "created": "waiting",
    "countdown": "countdown",
    "capturing": "capturing",
    "processing": "processing",
    "completed": "completed",
    "failed": "failed",
}

# 字符串位次只接受两种可公开的口径（防把不透明 id 的尾部数字误当地位次）：
# 1) 纯数字（如 "12"）；2) SM 编号式（字母前缀-年-序号，如 "SM-2026-0012"）。
_PLAIN_NUMBER_RE = re.compile(r"\s*(\d{1,6})\s*")
_DISPLAY_NUMBER_RE = re.compile(r"(?i)\s*[a-z]{1,4}[-_]\d{2,4}[-_](\d{1,6})\s*")


def _value(source: Any, key: str) -> Any:
    """从 dict / 普通对象里读一个键；缺失一律返回 None，不抛异常。"""
    if isinstance(source, Mapping):
        return source.get(key)
    return getattr(source, key, None)


def _positive_int(raw: Any) -> int | None:
    """把整数 / 浮点的「位次」收成 >= 1 的 int；收不出来返回 None（字符串走正则口径）。"""
    if isinstance(raw, bool):
        return None
    if isinstance(raw, int):
        return raw if raw >= 1 else None
    if isinstance(raw, float):
        if math.isfinite(raw) and raw.is_integer() and raw >= 1:
            return int(raw)
        return None
    return None


def called_label(value: Any) -> str | None:
    """把「被叫到的那一位」压成匿名展示串（如 "第 12 位"）。

    接受：None（没人被叫）、>= 1 的位次（int / 数字串）、编号串（如
    "SM-2026-0012"）、或含 display_number / number 的内部条目（dict 或普通对象）。
    字符串只认两种可公开口径：纯数字、或字母前缀-年-序号（SM 编号式）；
    其它字符串一律 "已叫号"——防止把不透明 id 的尾部数字误当公开位次。
    只回显提取到的位次数字，绝不回显原串 / queue_id / 完整 SM 编号；
    给出值但提取不出位次时返回 "已叫号"。
    """
    if value is None:
        return None
    if isinstance(value, (int, float, bool)):
        number = _positive_int(value)
        return f"第 {number} 位" if number is not None else "已叫号"
    if isinstance(value, str):
        match = _PLAIN_NUMBER_RE.fullmatch(value) or _DISPLAY_NUMBER_RE.fullmatch(value)
        number = int(match.group(1)) if match else None
        return f"第 {number} 位" if number else "已叫号"
    # dict / 普通对象：只挑公开语义字段，绝不回显原条目
    for key in ("display_number", "number"):
        inner = _value(value, key)
        if inner is not None:
            label = called_label(inner)
            if label not in (None, "已叫号"):
                return label
    return "已叫号"


def queue_counts(entries: Iterable[Any] | None) -> dict[str, int]:
    """各队列状态计数；固定输出 QUEUE_STATUSES 九个键（缺省 0）。

    内部条目里不认识的状态字符串不计数、不进入输出（白名单语义）。
    """
    counts = {status: 0 for status in QUEUE_STATUSES}
    for entry in entries or ():
        status = _value(entry, "status")
        if isinstance(status, str) and status in counts:
            counts[status] += 1
    return counts


def public_queue_fields(entries: Iterable[Any] | None = None, called: Any = None) -> dict[str, Any]:
    """公共 queue 小节：只给 counts 与匿名 called_label。

    called 优先于 entries：显式传入「被叫到的那一位」（位次 / 编号串 / 条目），
    不传时从 entries 里找 status == "called" 的最后一条（按迭代顺序）。
    """
    if isinstance(entries, Mapping):
        items = [entries]
    else:
        items = list(entries or ())
    if called is None:
        for entry in reversed(items):
            if _value(entry, "status") == "called":
                called = entry
                break
    return {"counts": queue_counts(items), "called_label": called_label(called)}


def _normalize_phase(raw: Any) -> str:
    if isinstance(raw, str):
        if raw in PUBLIC_CAPTURE_PHASES and raw != "unknown":
            return raw
        mapped = _CAPTURE_STATUS_TO_PHASE.get(raw)
        if mapped:
            return mapped
    return "unknown"


def public_capture_fields(capture: Any = None) -> dict[str, Any]:
    """公共 capture 小节：只给 phase 与 countdown_remaining。

    capture 为 None（没有进行中的拍摄）时 phase == "idle"；
    倒计时剩余秒数向上取整为非负整数，非倒计时阶段一律 None。
    """
    if capture is None:
        return {"phase": "idle", "countdown_remaining": None}
    raw_phase = _value(capture, "phase")
    if raw_phase is None:
        raw_phase = _value(capture, "status")
    phase = _normalize_phase(raw_phase)
    remaining = None
    if phase == "countdown":
        raw = _value(capture, "countdown_remaining")
        if isinstance(raw, (int, float)) and not isinstance(raw, bool):
            seconds = float(raw)
            if math.isfinite(seconds):
                remaining = max(0, int(math.ceil(seconds)))
    return {"phase": phase, "countdown_remaining": remaining}


def _status_of(value: Any) -> str | None:
    if isinstance(value, str):
        return value
    if value is None:
        return None
    status = _value(value, "status")
    return status if isinstance(status, str) else None


def public_capabilities(health: Any = None) -> dict[str, bool]:
    """六个组件的公开能力位（只允许布尔）。

    读 ComponentHealth 的 status 字符串（或直接传字符串）；
    只有 "ok" 才算能力可用，degraded / unavailable / disabled / 缺省一律 False。
    """
    return {
        name: _status_of(_value(health, name)) == "ok"
        for name in COMPONENTS
    }


def public_state(
    *,
    queue_entries: Iterable[Any] | None = None,
    called: Any = None,
    capture: Any = None,
    health: Any = None,
) -> dict[str, Any]:
    """`/api/state` 的公共投影包：queue / capture / capabilities 三小节。

    只包含脱敏后的公开字段，可直接合并进现有状态对象；现有字段（camera、share、
    styles 等）不在本模块职责内，由接线方保留。
    """
    return {
        "queue": public_queue_fields(queue_entries, called),
        "capture": public_capture_fields(capture),
        "capabilities": public_capabilities(health),
    }


def public_overlay_fields(**kwargs: Any) -> dict[str, Any]:
    """`/api/overlay.json` 要并入的公共字段包（非识别性排队提示 + 就绪状态）。

    与 public_state 同形（queue / capture / capabilities），便于 T-16 直接 merge；
    同样受脱敏白名单约束。
    """
    return public_state(**kwargs)
