# -*- coding: utf-8 -*-
"""auto_flow 稳定错误码、HTTP 状态映射与默认中文文案（T-11）。

规格依据：`docs/spec/自动流程规格-v1.md` 的「Error handling」与
「External HTTP and page contracts」。

- 「Error handling」表中的错误码为本模块权威集合，名称与规格逐字一致，
  HTTP 状态与默认中文文案同表落库；
- 路由表错误列出现、但不在上表的路由专属码（QUEUE_NOT_FOUND、JOB_NOT_FOUND、
  JOB_NOT_WAITING_FOR_CONSENT、INTERNAL_ONLY、DELETE_IN_PROGRESS、PRINT_JOB_NOT_FOUND）
  单独标注为「路由表补充码」，供 T-7/T-8/T-9/T-16 直接复用；
- 新增路由的响应形状固定为 {"error": {"code", "message", "request_id"}}，
  用 error_payload() 构造；
- 既有路由的扁平 {"ok": false, "error": "...", "detail": "..."} 与既有小写 code
  （consent_required / gone / not_found / unknown_job / internal / bad_url /
  invalid_style / consent_declined）不得改动，也不属于本模块。

错误码是普通字符串常量，路由代码可直接
`error_payload(QUEUE_FULL, request_id)` 或 `raise ApiError(QUEUE_EXPIRED)`。
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = [
    "ErrorSpec",
    "ERROR_SPECS",
    "ERROR_CODES",
    "HTTP_STATUS_BY_CODE",
    "MESSAGE_BY_CODE",
    "spec_for",
    "http_status_for",
    "default_message_for",
    "error_payload",
    "ApiError",
    # 「Error handling」表错误码
    "INVALID_REQUEST",
    "INVALID_TOKEN",
    "CONSENT_REQUIRED",
    "STYLE_NOT_FOUND",
    "IDEMPOTENCY_CONFLICT",
    "QUEUE_FULL",
    "QUEUE_EXPIRED",
    "NOT_CALLED",
    "CAMERA_UNAVAILABLE",
    "GESTURE_DISABLED",
    "CAPTURE_FAILED",
    "AIGC_FAILED",
    "AIGC_TIMEOUT",
    "PHOTO_NOT_FOUND",
    "PHOTO_DELETED",
    "PRINT_UNAVAILABLE",
    "INTERNAL_ERROR",
    # 路由表补充码
    "QUEUE_NOT_FOUND",
    "JOB_NOT_FOUND",
    "JOB_NOT_WAITING_FOR_CONSENT",
    "INTERNAL_ONLY",
    "DELETE_IN_PROGRESS",
    "PRINT_JOB_NOT_FOUND",
]


@dataclass(frozen=True)
class ErrorSpec:
    """一个稳定错误码的契约：错误码、HTTP 状态与默认中文文案。"""

    code: str
    http_status: int
    message: str


# ---- 「Error handling」表错误码（规格表逐字，不得改名） -------------------

INVALID_REQUEST = "INVALID_REQUEST"
INVALID_TOKEN = "INVALID_TOKEN"
CONSENT_REQUIRED = "CONSENT_REQUIRED"
STYLE_NOT_FOUND = "STYLE_NOT_FOUND"
IDEMPOTENCY_CONFLICT = "IDEMPOTENCY_CONFLICT"
QUEUE_FULL = "QUEUE_FULL"
QUEUE_EXPIRED = "QUEUE_EXPIRED"
NOT_CALLED = "NOT_CALLED"
CAMERA_UNAVAILABLE = "CAMERA_UNAVAILABLE"
GESTURE_DISABLED = "GESTURE_DISABLED"
CAPTURE_FAILED = "CAPTURE_FAILED"
AIGC_FAILED = "AIGC_FAILED"
AIGC_TIMEOUT = "AIGC_TIMEOUT"
PHOTO_NOT_FOUND = "PHOTO_NOT_FOUND"
PHOTO_DELETED = "PHOTO_DELETED"
PRINT_UNAVAILABLE = "PRINT_UNAVAILABLE"
INTERNAL_ERROR = "INTERNAL_ERROR"

# ---- 路由表补充码（spec 路由错误列出现，不在 Error handling 表） ----------

QUEUE_NOT_FOUND = "QUEUE_NOT_FOUND"                        # GET /api/queue/<queue_id>
JOB_NOT_FOUND = "JOB_NOT_FOUND"                            # /api/job/<id> 系列新契约
JOB_NOT_WAITING_FOR_CONSENT = "JOB_NOT_WAITING_FOR_CONSENT"
INTERNAL_ONLY = "INTERNAL_ONLY"                            # 内部手势入口
DELETE_IN_PROGRESS = "DELETE_IN_PROGRESS"
PRINT_JOB_NOT_FOUND = "PRINT_JOB_NOT_FOUND"


ERROR_SPECS: dict[str, ErrorSpec] = {
    INVALID_REQUEST: ErrorSpec(INVALID_REQUEST, 400, "请求格式不正确，请检查后重试。"),
    INVALID_TOKEN: ErrorSpec(INVALID_TOKEN, 404, "取图凭证无效。"),
    CONSENT_REQUIRED: ErrorSpec(CONSENT_REQUIRED, 403, "缺少有效的拍摄授权，请先同意拍摄。"),
    STYLE_NOT_FOUND: ErrorSpec(STYLE_NOT_FOUND, 404, "所选风格不存在。"),
    IDEMPOTENCY_CONFLICT: ErrorSpec(IDEMPOTENCY_CONFLICT, 409, "该请求编号已用于另一份提交，请刷新后重试。"),
    QUEUE_FULL: ErrorSpec(QUEUE_FULL, 429, "当前排队人数已满，请稍后再试。"),
    QUEUE_EXPIRED: ErrorSpec(QUEUE_EXPIRED, 409, "排队已超时，请重新提交。"),
    NOT_CALLED: ErrorSpec(NOT_CALLED, 409, "还未轮到本次拍摄，请等待叫号。"),
    CAMERA_UNAVAILABLE: ErrorSpec(CAMERA_UNAVAILABLE, 503, "相机暂时不可用，请找现场工作人员。"),
    GESTURE_DISABLED: ErrorSpec(GESTURE_DISABLED, 409, "手势拍照暂不可用，请用按钮开始。"),
    CAPTURE_FAILED: ErrorSpec(CAPTURE_FAILED, 503, "拍摄失败，未生成照片；请重试或找工作人员。"),
    AIGC_FAILED: ErrorSpec(AIGC_FAILED, 502, "风格化处理失败，已保留原图结果。"),
    AIGC_TIMEOUT: ErrorSpec(AIGC_TIMEOUT, 504, "风格化处理超时，已保留原图结果。"),
    PHOTO_NOT_FOUND: ErrorSpec(PHOTO_NOT_FOUND, 404, "照片不存在。"),
    PHOTO_DELETED: ErrorSpec(PHOTO_DELETED, 410, "照片已被删除。"),
    PRINT_UNAVAILABLE: ErrorSpec(PRINT_UNAVAILABLE, 503, "打印服务暂不可用。"),
    INTERNAL_ERROR: ErrorSpec(INTERNAL_ERROR, 500, "服务内部错误，请稍后重试。"),
    QUEUE_NOT_FOUND: ErrorSpec(QUEUE_NOT_FOUND, 404, "排队记录不存在。"),
    JOB_NOT_FOUND: ErrorSpec(JOB_NOT_FOUND, 404, "作业不存在。"),
    JOB_NOT_WAITING_FOR_CONSENT: ErrorSpec(JOB_NOT_WAITING_FOR_CONSENT, 409, "当前作业不在等待外置处理确认的状态。"),
    INTERNAL_ONLY: ErrorSpec(INTERNAL_ONLY, 403, "该接口仅供内部使用。"),
    DELETE_IN_PROGRESS: ErrorSpec(DELETE_IN_PROGRESS, 409, "删除正在进行，请稍后重试。"),
    PRINT_JOB_NOT_FOUND: ErrorSpec(PRINT_JOB_NOT_FOUND, 404, "打印任务不存在。"),
}

# 规格表顺序（测试与文案核查可按此顺序遍历）
ERROR_CODES: tuple[str, ...] = (
    INVALID_REQUEST,
    INVALID_TOKEN,
    CONSENT_REQUIRED,
    STYLE_NOT_FOUND,
    IDEMPOTENCY_CONFLICT,
    QUEUE_FULL,
    QUEUE_EXPIRED,
    NOT_CALLED,
    CAMERA_UNAVAILABLE,
    GESTURE_DISABLED,
    CAPTURE_FAILED,
    AIGC_FAILED,
    AIGC_TIMEOUT,
    PHOTO_NOT_FOUND,
    PHOTO_DELETED,
    PRINT_UNAVAILABLE,
    INTERNAL_ERROR,
    QUEUE_NOT_FOUND,
    JOB_NOT_FOUND,
    JOB_NOT_WAITING_FOR_CONSENT,
    INTERNAL_ONLY,
    DELETE_IN_PROGRESS,
    PRINT_JOB_NOT_FOUND,
)

HTTP_STATUS_BY_CODE: dict[str, int] = {code: spec.http_status for code, spec in ERROR_SPECS.items()}
MESSAGE_BY_CODE: dict[str, str] = {code: spec.message for code, spec in ERROR_SPECS.items()}


def spec_for(code: str) -> ErrorSpec:
    """取错误码契约；未知码直接报错，避免把拼写错误漏到线上。"""
    try:
        return ERROR_SPECS[code]
    except KeyError:
        raise KeyError(f"未知 auto_flow 错误码：{code}") from None


def http_status_for(code: str) -> int:
    """错误码 -> HTTP 状态（规格 Error handling 表）。"""
    return spec_for(code).http_status


def default_message_for(code: str) -> str:
    """错误码 -> 默认中文文案（路由未给自定义 message 时使用）。"""
    return spec_for(code).message


def error_payload(code: str, request_id: str = "", *, message: str | None = None) -> dict[str, dict[str, str]]:
    """构造规格规定的嵌套错误体：{"error": {code, message, request_id}}。

    request_id 由路由层传入本次请求的关联 ID；确实拿不到时允许空串占位。
    """
    spec = spec_for(code)
    return {
        "error": {
            "code": spec.code,
            "message": spec.message if message is None else message,
            "request_id": request_id,
        }
    }


class ApiError(Exception):
    """带稳定错误码与 HTTP 状态的业务异常；路由层捕获后用 payload() 输出。"""

    def __init__(self, code: str, message: str | None = None, *, request_id: str = "") -> None:
        spec = spec_for(code)
        self.code = spec.code
        self.http_status = spec.http_status
        self.message = spec.message if message is None else message
        self.request_id = request_id
        super().__init__(f"{self.code}: {self.message}")

    def payload(self, request_id: str | None = None) -> dict[str, dict[str, str]]:
        """错误响应体；request_id 优先用调用时传入的本次请求 ID。"""
        return error_payload(
            self.code,
            self.request_id if request_id is None else request_id,
            message=self.message,
        )
