# -*- coding: utf-8 -*-
"""同意处理纯逻辑层（T-12）：POST /api/consent 与队列提交/开始的同意校验。

规格依据：`docs/spec/自动流程规格-v1.md`（路由表 `POST /api/consent`、
`POST /api/queue/<queue_id>/start`；Security「同意绑定」）与任务 T-12。

边界与约定：
- 不 import HTTP server（`booth_server.py`）、不读请求对象、不写响应流：只接收
  已解析的 JSON 载荷和已构造的 `ConsentService`，返回 ``(http_status, payload)``
  或抛 `errors.ApiError`，由路由层统一输出；
- 旧格式兼容：``{"agree": true, "kind": "photo"}`` 的成功响应保持
  ``{ok, token, expires_in, kind, purpose, retention, version, at}`` 形状，
  ``purpose`` / ``retention`` 文案与现有 ``ConsentBook.PURPOSE/RETENTION``
  （booth_server.py:156-157）逐字一致；拒绝同意仍是 200 + ``consent_declined``；
- 新用途：``{"agree": true, "purpose": "capture" | "external_processing"}``，
  响应额外携带 ``consent``（ConsentRecord 投影）与 ``consent_id``；旧键保留，
  不改变 ``{agree:true}`` 的既有成功语义；
- 令牌桥接说明：本模块签发的令牌进入 `ConsentService` 链路（validate / bind）。
  现有 ``/api/capture`` 仍只认 `ConsentBook` 的内存令牌；两套令牌如何桥接
  （例如 ``/api/capture`` 同时校验两个服务、或旧路由原样保留）属 T-2/T-16 的
  接线决策——本模块不 import、不修改 `booth_server.py`；
- 队列接入顺序（T-2）：1) 用 QueueService 提交拿到/重放队列条目；2) 调用
  `authorize_queue_submission` 把本次 consent 绑定到该 queue_id（幂等重放安全）；
  3) 绑定时若发现同意已绑到其它 queue，则把刚创建的新条目取消（mark_cancelled）
  后返回 403。开始拍摄前用 `verify_queue_start` 做严格校验（必须正好绑定本 queue）。
  `release_queue_binding` 仅用于补偿回滚。

隐私：不做人脸识别；本模块只处理同意元数据，不接收也不返回任何个人字段内容。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from app.auto_flow import errors
from app.auto_flow.consent_service import ConsentBindingError, ConsentService
from app.auto_flow.models import CONSENT_PURPOSES, ConsentRecord

__all__ = [
    "LEGACY_PURPOSE_TEXT",
    "LEGACY_RETENTION_TEXT",
    "LEGACY_KIND_TO_PURPOSE",
    "handle_consent_request",
    "authorize_queue_submission",
    "verify_queue_start",
    "release_queue_binding",
]

# 与 booth_server.py:156-157 的 ConsentBook.PURPOSE / RETENTION 逐字一致
# （booth_server.py 不可 import——会拉起整个服务模块；改文案必须两边同步）。
LEGACY_PURPOSE_TEXT = "拍摄并生成 SpacemiT DevCon 2026 开发者证件照，用于现场即时分享与领取"
LEGACY_RETENTION_TEXT = "照片在本地保存 1 天，取图链接生成后 24 小时内有效，到期自动删除；参会者可随时要求删除"

# 旧格式 kind -> 新用途；旧前端只发 "photo"。外置处理必须走新格式的 purpose 字段，
# 避免旧响应里的 purpose 文案对外置用途产生误导。
LEGACY_KIND_TO_PURPOSE: dict[str, str] = {
    "photo": "capture",
    "capture": "capture",
}


def handle_consent_request(
    payload: Mapping[str, Any] | None,
    service: ConsentService,
    *,
    subject_id: str = "",
) -> tuple[int, dict[str, Any]]:
    """处理 ``POST /api/consent``（旧格式 + 新用途），返回 ``(http_status, payload)``。

    旧格式成功：``{ok, token, expires_in, kind, purpose, retention, version, at}``；
    新格式成功：在旧键基础上加 ``consent`` / ``consent_id`` / 新 ``purpose`` 码；
    拒绝同意（agree/granted 非真值）：200 ``{ok: false, error: "consent_declined"}``，
    与现状一致、不落任何记录；
    请求体格式不对 / 未知 kind / 未知 purpose：抛 ``ApiError(INVALID_REQUEST)``（400）。

    ``subject_id`` 由路由层从服务端会话派生（不要取客户端 body，防伪造）；留空
    表示匿名同意（个人字段全留空也可正常拍摄）。
    """
    if not isinstance(payload, Mapping):
        raise errors.ApiError(errors.INVALID_REQUEST, "请求体必须是 JSON 对象。")

    if "agree" in payload:
        agree = payload.get("agree")
    elif isinstance(payload.get("granted"), bool):
        agree = payload.get("granted")
    else:
        agree = False
    if not agree:
        # 兼容旧语义：缺 agree 或任何非真值都算拒绝，200 + consent_declined
        return 200, {"ok": False, "error": "consent_declined"}

    if "purpose" in payload:
        purpose = payload.get("purpose")
        if not isinstance(purpose, str) or purpose not in CONSENT_PURPOSES:
            allowed = "、".join(sorted(CONSENT_PURPOSES))
            raise errors.ApiError(errors.INVALID_REQUEST, f"purpose 必须是 {allowed} 之一。")
        record = service.record(purpose, True, subject_id)
        return 200, {
            "ok": True,
            "consent": record.to_dict(),
            "consent_id": record.consent_id,
            "token": record.consent_id,
            "purpose": record.purpose,
            "kind": "photo" if record.purpose == "capture" else "external",
            "expires_in": service.ttl_seconds,
            "retention": LEGACY_RETENTION_TEXT,
            "version": service.stats()["issued"],
            "at": record.created_at,
        }

    raw_kind = payload.get("kind")
    kind = "photo" if raw_kind is None or not str(raw_kind).strip() else str(raw_kind).strip()
    purpose = LEGACY_KIND_TO_PURPOSE.get(kind)
    if purpose is None:
        raise errors.ApiError(
            errors.INVALID_REQUEST,
            f"kind 不支持：{kind!r}；外置处理同意请改用 purpose 字段。",
        )
    record = service.record(purpose, True, subject_id)
    return 200, {
        "ok": True,
        "token": record.consent_id,
        "expires_in": service.ttl_seconds,
        "kind": kind,
        "purpose": LEGACY_PURPOSE_TEXT,
        "retention": LEGACY_RETENTION_TEXT,
        "version": service.stats()["issued"],
        "at": record.created_at,
    }


def authorize_queue_submission(
    service: ConsentService,
    *,
    capture_consent_id: str,
    queue_id: str,
) -> ConsentRecord:
    """为队列提交校验 capture 同意并原子绑定到 queue_id。

    成功返回绑定后的 ``ConsentRecord``；失败抛 ``ApiError``：
    - 缺 / 空 ``capture_consent_id``、空 queue_id -> 400 ``INVALID_REQUEST``；
    - 未知 / 已拒绝 / 已撤回 / 已过期 / 用途不符 / 已绑其它预约 -> 403
      ``CONSENT_REQUIRED``（对外不区分原因，避免泄露令牌细节）；
    幂等重放（同一同意 + 同一 queue）直接返回原记录，不重复写盘。
    """
    try:
        return service.bind(capture_consent_id, queue_id)
    except ConsentBindingError as exc:
        if exc.reason == "invalid_request":
            raise errors.ApiError(errors.INVALID_REQUEST, exc.message) from exc
        raise errors.ApiError(errors.CONSENT_REQUIRED) from exc


def verify_queue_start(
    service: ConsentService,
    *,
    capture_consent_id: str,
    queue_id: str,
) -> ConsentRecord:
    """开始拍摄前的严格校验：同意必须有效，且正好绑定到本队列条目。

    与 ``authorize_queue_submission`` 不同（那里允许首次绑定），这里要求绑定
    已存在且等于 queue_id：未绑定、绑到别的 queue、撤回/过期一律 403。
    """
    consent_key = _require_nonempty(capture_consent_id, "capture_consent_id")
    queue_key = _require_nonempty(queue_id, "queue_id")
    if not service.validate(consent_key, "capture", queue_key):
        raise errors.ApiError(errors.CONSENT_REQUIRED)
    record = service.get(consent_key)
    if record is None or record.queue_id != queue_key:
        raise errors.ApiError(errors.CONSENT_REQUIRED)
    return record


def release_queue_binding(
    service: ConsentService,
    *,
    capture_consent_id: str,
    queue_id: str,
) -> bool:
    """补偿回滚：只有该同意正好绑定到该 queue 时才解除；返回是否解除。

    供「队列条目已创建但后续步骤失败并已取消条目」的清理路径使用，绝不用于
    正常流程；参数不合法时返回 False（清理路径不抛错）。
    """
    if not isinstance(capture_consent_id, str) or not capture_consent_id.strip():
        return False
    if not isinstance(queue_id, str) or not queue_id.strip():
        return False
    return service.release_binding(capture_consent_id, queue_id)


def _require_nonempty(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise errors.ApiError(errors.INVALID_REQUEST, f"{field} 必须是非空字符串。")
    return value.strip()
