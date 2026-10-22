# -*- coding: utf-8 -*-
"""auto_flow 共享数据模型：Queue、同意、作业、健康与打印（T-1）。

规格依据：`docs/spec/自动流程规格-v1.md`「Data models」。本文件是 T-2/T-6/T-8/
T-9/T-12/T-13/T-14/T-15/T-20 的公共 import 依赖：类型名与字段名与规格逐字一致，
不得改名；序列化格式为 JSON 友好的 snake_case 字典（可直接进 API 响应与持久化文件）。

约定（实现选择，供下游任务依赖）：
- 时间戳字段按规格使用 ISO 8601 UTC 字符串（秒精度，形如 ``2026-09-30T10:00:00Z``）；
  生成/解析统一走 to_utc_iso / parse_utc_iso / utc_now_iso；
- QueueStatus 是规格点名的枚举（str Enum，值即小写状态名）；规格里其余内联字符串
  联合（purpose / status / variant / gesture 等）保持普通字符串，用 Literal 别名 +
  frozenset 常量约束取值域（与仓库既有代码风格一致，JSON 序列化零转换）；
- 每个模型的 to_dict() 省略值为 None 的可选字段（对应规格的 ``?`` 字段），
  from_dict() 忽略未知键（旧数据只读兼容），必填字段缺失或类型不符抛 ValueError；
- 例外：QueueSubmission 是客户端输入模型，from_dict() 对未知字段直接报错
  （规格「Reject unexpected fields where practical」），由 T-2 换算为 400 INVALID_REQUEST；
- 模型只做结构与取值域校验，不做业务规则（容量、幂等、状态机、许可校验在各自服务里）；
- 规格字段顺序中必填项排在可选项后（如 QueueSubmission 的 capture_consent_id）；
  为满足 Python dataclass「默认值字段必须靠后」的规则，本文件对这类类调整了声明顺序，
  字段名与语义与规格完全一致（仅顺序不同）。

本文件不读环境变量、不写文件、不打印日志。
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, fields
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Literal

__all__ = [
    # 枚举 / 取值域
    "QueueStatus",
    "ACTIVE_STATUSES",
    "TERMINAL_STATUSES",
    "SLOT_STATUSES",
    "is_active_status",
    "occupies_capture_slot",
    "ConsentPurpose",
    "CONSENT_PURPOSES",
    "CaptureJobStatus",
    "CAPTURE_JOB_STATUSES",
    "CaptureJobFallback",
    "CAPTURE_JOB_FALLBACKS",
    "PhotoStatus",
    "PHOTO_STATUSES",
    "PhotoVariant",
    "PHOTO_VARIANTS",
    "ComponentStatus",
    "COMPONENT_STATUSES",
    "PrintJobStatus",
    "PRINT_JOB_STATUSES",
    "GestureValue",
    "GESTURE_VALUES",
    "PortraitMode",
    "PORTRAIT_MODES",
    # 时间与令牌工具
    "to_utc_iso",
    "parse_utc_iso",
    "utc_now_iso",
    "is_valid_photo_token",
    "canonical_payload",
    "payload_fingerprint",
    "legacy_payload_fingerprint",
    # 数据模型（规格 Data models 全量）
    "QueueEntry",
    "QueueSubmission",
    "ConsentRecord",
    "CaptureJob",
    "PhotoRecord",
    "ComponentHealth",
    "SystemHealth",
    "PrintJob",
    "GestureResult",
]

_UTC = timezone.utc
_ISO_SECONDS_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
_PHOTO_TOKEN_RE = re.compile(r"[A-Za-z0-9_-]{22}")


# --------------------------------------------------------------------------- #
# 时间与令牌工具
# --------------------------------------------------------------------------- #

def to_utc_iso(value: datetime) -> str:
    """datetime -> ISO 8601 UTC 字符串（秒精度、``Z`` 结尾，与规格一致）。

    naive datetime 按 UTC 解释；秒以下精度截断。调用方若需要"call_at + 超时"这类
    计算，请先用 parse_utc_iso 把已落库的字符串读回来再算，保证两侧口径一致。
    """
    if not isinstance(value, datetime):
        raise ValueError(f"需要 datetime，得到 {type(value).__name__}")
    if value.tzinfo is None:
        value = value.replace(tzinfo=_UTC)
    return value.astimezone(_UTC).strftime(_ISO_SECONDS_FORMAT)


def parse_utc_iso(value: str) -> datetime:
    """ISO 8601 字符串 -> 带时区的 UTC datetime；无法解析抛 ValueError。

    接受 ``…Z``、``+00:00`` 等带偏移写法；不带时区的字符串按 UTC 解释。
    """
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"需要 ISO 8601 时间字符串，得到 {value!r}")
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(f"无法解析 ISO 8601 时间：{value!r}") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=_UTC)
    return parsed.astimezone(_UTC)


def utc_now_iso() -> str:
    """当前时刻的 ISO 8601 UTC 字符串。"""
    return to_utc_iso(datetime.now(_UTC))


def is_valid_photo_token(token: object) -> bool:
    """取图令牌格式：恰好 22 个 URL-safe 字符（secrets.token_urlsafe(16) 的长度）。"""
    return isinstance(token, str) and _PHOTO_TOKEN_RE.fullmatch(token) is not None


def canonical_payload(*, style_id: str, optional_fields: Mapping[str, str] | None = None,
                      portrait_mode: str = "standard", ai_watermark: bool = True) -> str:
    """幂等比较用的规范化载荷：键序固定，包含风格、人物模式与可选字段。

    request_id 本身不进载荷（它就是比较键）；capture_consent_id 不进载荷
    （重试可能携带新同意的令牌，不应因此判为不同请求）。
    """
    fields_map = dict(optional_fields or {})
    ordered = {key: fields_map[key] for key in sorted(fields_map)}
    if not isinstance(portrait_mode, str) or portrait_mode not in PORTRAIT_MODES:
        raise ValueError("portrait_mode 必须是 standard 或 chibi。")
    if not isinstance(ai_watermark, bool):
        raise ValueError("ai_watermark 必须是布尔值。")
    payload = {"style_id": style_id, "optional_fields": ordered, "portrait_mode": portrait_mode}
    if not ai_watermark:
        payload["ai_watermark"] = False
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def payload_fingerprint(*, style_id: str, optional_fields: Mapping[str, str] | None = None,
                        portrait_mode: str = "standard", ai_watermark: bool = True) -> str:
    """规范化载荷的 SHA-256 指纹（``sha256:<hex>``）。"""
    digest = hashlib.sha256(
        canonical_payload(style_id=style_id, optional_fields=optional_fields,
                          portrait_mode=portrait_mode, ai_watermark=ai_watermark).encode("utf-8")
    ).hexdigest()
    return f"sha256:{digest}"


def legacy_payload_fingerprint(*, style_id: str,
                               optional_fields: Mapping[str, str] | None = None) -> str:
    """旧版无 portrait_mode 的指纹，仅用于标准模式的兼容重放。"""
    payload = json.dumps(
        {"style_id": style_id, "optional_fields": dict(optional_fields or {})},
        ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    )
    return "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------- #
# 状态枚举与取值域
# --------------------------------------------------------------------------- #

PortraitMode = Literal["standard", "chibi"]
PORTRAIT_MODES: frozenset[str] = frozenset({"standard", "chibi"})

class QueueStatus(str, Enum):
    """排队状态（规格 Data models 的 QueueStatus，逐字）。"""

    QUEUED = "queued"
    CALLED = "called"
    READY = "ready"
    CAPTURING = "capturing"
    PROCESSING = "processing"
    READY_FOR_PICKUP = "ready_for_pickup"
    EXPIRED = "expired"
    CANCELLED = "cancelled"
    FAILED = "failed"

    @classmethod
    def from_value(cls, value: object) -> "QueueStatus":
        """字符串/枚举 -> QueueStatus；未知取值抛 ValueError。"""
        if isinstance(value, cls):
            return value
        if isinstance(value, str) and value in cls._value2member_map_:
            return cls(value)
        raise ValueError(f"未知 QueueStatus：{value!r}")

    @property
    def is_terminal(self) -> bool:
        """终态：不再计入容量、不会再有状态推进。"""
        return self in TERMINAL_STATUSES

    @property
    def is_active(self) -> bool:
        """活动（非终态）：计入 queue.max_active 容量。"""
        return not self.is_terminal

    @property
    def occupies_capture_slot(self) -> bool:
        """是否占用唯一拍摄位：到号、已开始、拍摄中。"""
        return self in SLOT_STATUSES


# 容量口径（T-1 合同）：active = 尚未完成排队/拍摄/处理的条目；
# ready_for_pickup 保留在台账供取图查询，但不再占用新用户排队名额。
TERMINAL_STATUSES: frozenset[QueueStatus] = frozenset(
    {QueueStatus.READY_FOR_PICKUP, QueueStatus.EXPIRED, QueueStatus.CANCELLED,
     QueueStatus.FAILED}
)
ACTIVE_STATUSES: frozenset[QueueStatus] = frozenset(
    status for status in QueueStatus if status not in TERMINAL_STATUSES
)
# 拍摄位口径：已有这些状态的条目时不再叫下一位（活动拍摄位独占）
SLOT_STATUSES: frozenset[QueueStatus] = frozenset(
    {QueueStatus.CALLED, QueueStatus.READY, QueueStatus.CAPTURING}
)


def is_active_status(value: QueueStatus | str) -> bool:
    """QueueStatus 或状态字符串 -> 是否活动（非终态）。"""
    return QueueStatus.from_value(value).is_active


def occupies_capture_slot(value: QueueStatus | str) -> bool:
    """QueueStatus 或状态字符串 -> 是否占用拍摄位。"""
    return QueueStatus.from_value(value).occupies_capture_slot


ConsentPurpose = Literal["capture", "external_processing"]
CONSENT_PURPOSES: frozenset[str] = frozenset({"capture", "external_processing"})

CaptureJobStatus = Literal["created", "countdown", "capturing", "processing", "completed", "failed"]
CAPTURE_JOB_STATUSES: frozenset[str] = frozenset(
    {"created", "countdown", "capturing", "processing", "completed", "failed"}
)

CaptureJobFallback = Literal["none", "L0"]
CAPTURE_JOB_FALLBACKS: frozenset[str] = frozenset({"none", "L0"})

PhotoStatus = Literal["available", "deleted"]
PHOTO_STATUSES: frozenset[str] = frozenset({"available", "deleted"})

PhotoVariant = Literal["L0", "styled"]
PHOTO_VARIANTS: frozenset[str] = frozenset({"L0", "styled"})

ComponentStatus = Literal["ok", "degraded", "unavailable", "disabled"]
COMPONENT_STATUSES: frozenset[str] = frozenset({"ok", "degraded", "unavailable", "disabled"})

PrintJobStatus = Literal["unavailable", "queued", "submitted", "completed", "failed"]
PRINT_JOB_STATUSES: frozenset[str] = frozenset(
    {"unavailable", "queued", "submitted", "completed", "failed"}
)

GestureValue = Literal["ok", "none", "unknown"]
GESTURE_VALUES: frozenset[str] = frozenset({"ok", "none", "unknown"})


# --------------------------------------------------------------------------- #
# 序列化基础设施
# --------------------------------------------------------------------------- #

class _DictModel:
    """to_dict 的公共实现：枚举转值、嵌套模型递归、值为 None 的可选字段省略。"""

    __slots__ = ()

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for field in fields(self):  # type: ignore[arg-type]
            value = getattr(self, field.name)
            if value is None:
                continue
            if isinstance(value, Enum):
                value = value.value
            elif isinstance(value, _DictModel):
                value = value.to_dict()
            out[field.name] = value
        return out


def _require_mapping(data: object, cls_name: str) -> Mapping[str, Any]:
    if not isinstance(data, Mapping):
        raise ValueError(f"{cls_name}.from_dict 需要字典，得到 {type(data).__name__}")
    return data


def _require_str(data: Mapping[str, Any], key: str, *, what: str = "") -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{what or key} 必须是非空字符串，得到 {value!r}")
    return value


def _optional_str(data: Mapping[str, Any], key: str, *, what: str = "") -> str | None:
    """可选字符串：None/缺省 -> None；存在必须是字符串（允许空串，如 reason=""）。"""
    value = data.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{what or key} 若存在必须是字符串，得到 {value!r}")
    return value


def _require_bool(data: Mapping[str, Any], key: str, *, what: str = "") -> bool:
    value = data.get(key)
    if not isinstance(value, bool):
        raise ValueError(f"{what or key} 必须是布尔值，得到 {value!r}")
    return value


def _optional_bool(data: Mapping[str, Any], key: str, *, what: str = "") -> bool | None:
    value = data.get(key)
    if value is None:
        return None
    if not isinstance(value, bool):
        raise ValueError(f"{what or key} 若存在必须是布尔值，得到 {value!r}")
    return value


def _require_int(data: Mapping[str, Any], key: str, *, what: str = "") -> int:
    value = data.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{what or key} 必须是整数，得到 {value!r}")
    return value


def _optional_int(data: Mapping[str, Any], key: str, *, what: str = "") -> int | None:
    value = data.get(key)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{what or key} 若存在必须是整数，得到 {value!r}")
    return value


def _optional_float(data: Mapping[str, Any], key: str, *, what: str = "") -> float | None:
    value = data.get(key)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{what or key} 若存在必须是数字，得到 {value!r}")
    return float(value)


def _require_choice(
    data: Mapping[str, Any], key: str, allowed: frozenset[str], *, what: str = ""
) -> str:
    value = data.get(key)
    if not isinstance(value, str) or value not in allowed:
        raise ValueError(f"{what or key} 取值必须是 {sorted(allowed)} 之一，得到 {value!r}")
    return value


def _require_timestamp(data: Mapping[str, Any], key: str, *, what: str = "") -> str:
    """必填时间戳：存在、是字符串且可解析（口径与 parse_utc_iso 一致）。"""
    value = _require_str(data, key, what=what)
    try:
        parse_utc_iso(value)
    except ValueError as exc:
        raise ValueError(f"{what or key} 不是合法 ISO 8601 时间：{value!r}") from exc
    return value


def _optional_timestamp(data: Mapping[str, Any], key: str, *, what: str = "") -> str | None:
    value = _optional_str(data, key, what=what)
    if value is None:
        return None
    try:
        parse_utc_iso(value)
    except ValueError as exc:
        raise ValueError(f"{what or key} 不是合法 ISO 8601 时间：{value!r}") from exc
    return value


# --------------------------------------------------------------------------- #
# 数据模型（规格 Data models，逐字段）
# --------------------------------------------------------------------------- #

@dataclass
class QueueEntry(_DictModel):
    """排队条目。queue_id 是不透明随机标识，不是取图令牌；display_number 仅供展示。"""

    queue_id: str
    request_id: str
    status: QueueStatus
    style_id: str
    created_at: str
    updated_at: str
    called_at: str | None = None
    start_deadline: str | None = None
    started_at: str | None = None
    expired_at: str | None = None
    consent_id: str | None = None
    display_number: str | None = None
    portrait_mode: PortraitMode = "standard"
    name: str = ""
    handle: str = ""
    ai_watermark: bool = True

    def to_dict(self) -> dict[str, Any]:
        out = super().to_dict()
        out.pop("name", None)
        out.pop("handle", None)
        return out

    def to_storage_dict(self) -> dict[str, Any]:
        return super().to_dict()

    @classmethod
    def from_dict(cls, data: object) -> "QueueEntry":
        raw = _require_mapping(data, cls.__name__)
        return cls(
            queue_id=_require_str(raw, "queue_id"),
            request_id=_require_str(raw, "request_id"),
            status=QueueStatus.from_value(raw.get("status")),
            style_id=_require_str(raw, "style_id"),
            created_at=_require_timestamp(raw, "created_at"),
            updated_at=_require_timestamp(raw, "updated_at"),
            called_at=_optional_timestamp(raw, "called_at"),
            start_deadline=_optional_timestamp(raw, "start_deadline"),
            started_at=_optional_timestamp(raw, "started_at"),
            expired_at=_optional_timestamp(raw, "expired_at"),
            consent_id=_optional_str(raw, "consent_id"),
            display_number=_optional_str(raw, "display_number"),
            portrait_mode=_require_choice(
                {"portrait_mode": raw.get("portrait_mode", "standard")},
                "portrait_mode", PORTRAIT_MODES,
            ),
            name=str(raw.get("name") or "")[:24],
            handle=str(raw.get("handle") or "")[:32],
            ai_watermark=_require_bool({"ai_watermark": raw.get("ai_watermark", True)}, "ai_watermark"),
        )


@dataclass
class QueueSubmission(_DictModel):
    """客户端排队提交。request_id 是幂等键；不得作为取图或访问凭据。"""

    request_id: str
    style_id: str
    capture_consent_id: str
    optional_fields: dict[str, str] | None = None
    portrait_mode: PortraitMode = "standard"
    ai_watermark: bool = True

    @classmethod
    def from_dict(cls, data: object) -> "QueueSubmission":
        raw = _require_mapping(data, cls.__name__)
        unknown = sorted(set(raw) - {
            "request_id", "style_id", "optional_fields", "capture_consent_id", "portrait_mode", "ai_watermark"
        })
        if unknown:
            raise ValueError(f"QueueSubmission 含未知字段：{unknown}")
        optional_fields = raw.get("optional_fields")
        if optional_fields is None:
            cleaned: dict[str, str] | None = None
        else:
            if not isinstance(optional_fields, Mapping):
                raise ValueError(f"optional_fields 必须是字典，得到 {type(optional_fields).__name__}")
            cleaned = {}
            for key, value in optional_fields.items():
                if not isinstance(key, str) or not key.strip():
                    raise ValueError(f"optional_fields 的键必须是非空字符串，得到 {key!r}")
                if not isinstance(value, str):
                    raise ValueError(f"optional_fields[{key!r}] 必须是字符串，得到 {value!r}")
                cleaned[key] = value
        return cls(
            request_id=_require_str(raw, "request_id"),
            style_id=_require_str(raw, "style_id"),
            capture_consent_id=_require_str(raw, "capture_consent_id"),
            optional_fields=cleaned,
            ai_watermark=_require_bool({"ai_watermark": raw.get("ai_watermark", True)}, "ai_watermark"),
            portrait_mode=_require_choice(
                {"portrait_mode": raw.get("portrait_mode", "standard")},
                "portrait_mode", PORTRAIT_MODES,
            ),
        )


@dataclass
class ConsentRecord(_DictModel):
    """同意记录。拍摄同意与外置处理同意是独立记录，不可互相替代。"""

    consent_id: str
    purpose: ConsentPurpose
    granted: bool
    created_at: str
    queue_id: str | None = None
    job_id: str | None = None

    @classmethod
    def from_dict(cls, data: object) -> "ConsentRecord":
        raw = _require_mapping(data, cls.__name__)
        return cls(
            consent_id=_require_str(raw, "consent_id"),
            purpose=_require_choice(raw, "purpose", CONSENT_PURPOSES, what="purpose"),
            granted=_require_bool(raw, "granted", what="granted"),
            created_at=_require_timestamp(raw, "created_at"),
            queue_id=_optional_str(raw, "queue_id"),
            job_id=_optional_str(raw, "job_id"),
        )


@dataclass
class CaptureJob(_DictModel):
    """拍摄作业。photo_token 只在照片就绪后出现；失败作业不得带令牌。"""

    job_id: str
    queue_id: str
    status: CaptureJobStatus
    capture_consent_id: str
    style_id: str
    created_at: str
    updated_at: str
    external_processing_consent_id: str | None = None
    fallback: CaptureJobFallback = "none"
    error_code: str | None = None
    photo_token: str | None = None
    portrait_mode: PortraitMode = "standard"

    @classmethod
    def from_dict(cls, data: object) -> "CaptureJob":
        raw = _require_mapping(data, cls.__name__)
        photo_token = _optional_str(raw, "photo_token")
        if photo_token is not None and not is_valid_photo_token(photo_token):
            raise ValueError(f"photo_token 必须是 22 个 URL-safe 字符，得到 {photo_token!r}")
        return cls(
            job_id=_require_str(raw, "job_id"),
            queue_id=_require_str(raw, "queue_id"),
            status=_require_choice(raw, "status", CAPTURE_JOB_STATUSES, what="status"),
            capture_consent_id=_require_str(raw, "capture_consent_id"),
            external_processing_consent_id=_optional_str(raw, "external_processing_consent_id"),
            style_id=_require_str(raw, "style_id"),
            created_at=_require_timestamp(raw, "created_at"),
            updated_at=_require_timestamp(raw, "updated_at"),
            fallback=_require_choice(raw, "fallback", CAPTURE_JOB_FALLBACKS, what="fallback")
            if raw.get("fallback") is not None
            else "none",
            error_code=_optional_str(raw, "error_code"),
            photo_token=photo_token,
            portrait_mode=_require_choice(
                {"portrait_mode": raw.get("portrait_mode", "standard")},
                "portrait_mode", PORTRAIT_MODES,
            ),
        )


@dataclass
class PhotoRecord(_DictModel):
    """照片记录。photo_token 是唯一取图凭据；display_number 永不作为凭据。"""

    photo_token: str
    job_id: str
    created_at: str
    status: PhotoStatus
    variant: PhotoVariant
    display_number: str | None = None

    @classmethod
    def from_dict(cls, data: object) -> "PhotoRecord":
        raw = _require_mapping(data, cls.__name__)
        photo_token = _require_str(raw, "photo_token")
        if not is_valid_photo_token(photo_token):
            raise ValueError(f"photo_token 必须是 22 个 URL-safe 字符，得到 {photo_token!r}")
        return cls(
            photo_token=photo_token,
            job_id=_require_str(raw, "job_id"),
            created_at=_require_timestamp(raw, "created_at"),
            status=_require_choice(raw, "status", PHOTO_STATUSES, what="status"),
            variant=_require_choice(raw, "variant", PHOTO_VARIANTS, what="variant"),
            display_number=_optional_str(raw, "display_number"),
        )


@dataclass
class ComponentHealth(_DictModel):
    """单个组件的健康状态。"""

    status: ComponentStatus
    checked_at: str
    reason: str | None = None

    @classmethod
    def from_dict(cls, data: object) -> "ComponentHealth":
        raw = _require_mapping(data, cls.__name__)
        return cls(
            status=_require_choice(raw, "status", COMPONENT_STATUSES, what="status"),
            checked_at=_require_timestamp(raw, "checked_at"),
            reason=_optional_str(raw, "reason"),
        )


@dataclass
class SystemHealth(_DictModel):
    """系统级健康快照：六个组件（规格字段名逐字）。"""

    camera: ComponentHealth
    gesture: ComponentHealth
    queue: ComponentHealth
    storage: ComponentHealth
    aigc: ComponentHealth
    printer: ComponentHealth

    @classmethod
    def from_dict(cls, data: object) -> "SystemHealth":
        raw = _require_mapping(data, cls.__name__)
        return cls(
            camera=_component_health(raw.get("camera"), "camera"),
            gesture=_component_health(raw.get("gesture"), "gesture"),
            queue=_component_health(raw.get("queue"), "queue"),
            storage=_component_health(raw.get("storage"), "storage"),
            aigc=_component_health(raw.get("aigc"), "aigc"),
            printer=_component_health(raw.get("printer"), "printer"),
        )


def _component_health(value: object, field_name: str) -> ComponentHealth:
    if isinstance(value, ComponentHealth):
        return value
    try:
        return ComponentHealth.from_dict(value)
    except ValueError as exc:
        raise ValueError(f"SystemHealth.{field_name} 无效：{exc}") from exc


@dataclass
class PrintJob(_DictModel):
    """打印任务。unavailable 是设备/协议未验证时的诚实状态，不得伪报成功。"""

    print_job_id: str
    photo_token: str
    status: PrintJobStatus
    created_at: str
    updated_at: str
    error_code: str | None = None

    @classmethod
    def from_dict(cls, data: object) -> "PrintJob":
        raw = _require_mapping(data, cls.__name__)
        photo_token = _require_str(raw, "photo_token")
        if not is_valid_photo_token(photo_token):
            raise ValueError(f"photo_token 必须是 22 个 URL-safe 字符，得到 {photo_token!r}")
        return cls(
            print_job_id=_require_str(raw, "print_job_id"),
            photo_token=photo_token,
            status=_require_choice(raw, "status", PRINT_JOB_STATUSES, what="status"),
            created_at=_require_timestamp(raw, "created_at"),
            updated_at=_require_timestamp(raw, "updated_at"),
            error_code=_optional_str(raw, "error_code"),
        )


@dataclass
class GestureResult(_DictModel):
    """手势观测结果。available=False 时下游一律不得触发拍摄。"""

    available: bool
    gesture: GestureValue
    stable_frames: int
    observed_at: str
    confidence: float | None = None
    person_count: int | None = None
    in_frame: bool | None = None
    reason: str | None = None

    @classmethod
    def from_dict(cls, data: object) -> "GestureResult":
        raw = _require_mapping(data, cls.__name__)
        return cls(
            available=_require_bool(raw, "available", what="available"),
            gesture=_require_choice(raw, "gesture", GESTURE_VALUES, what="gesture"),
            stable_frames=_require_int(raw, "stable_frames", what="stable_frames"),
            observed_at=_require_timestamp(raw, "observed_at"),
            confidence=_optional_float(raw, "confidence"),
            person_count=_optional_int(raw, "person_count"),
            in_frame=_optional_bool(raw, "in_frame"),
            reason=_optional_str(raw, "reason"),
        )
