# -*- coding: utf-8 -*-
"""auto_flow 集中配置：类型化解析、范围校验与安全默认值（T-11）。

规格依据：`自动流程规格 v1（内部文档）`「Configuration」「Security」「Testing strategy」。
本模块只做一件事：把环境变量（或注入的环境字典）解析成不可变的类型化配置，并在启动期
把非法值、生产禁用项和缺失的合规策略全部拦下。失败方向一律是「启动报错」，不静默回退。

安全默认值（T-11 验收）：
- queue.enabled / aigc.enabled / gesture.enabled / printer.enabled 默认 false：
  排队、AIGC 外发、手势自动触发、打印四扇门全部先关上；
- logging.redact_tokens 默认 true：日志默认对取图令牌脱敏。
- presentation.mode 默认 full：完整展示一位用户；handoff 模式在 AIGC 开始后按延迟交回预览。

两条生产门（测试模式放行、生产必须满足）：
- 图片保留策略：BOOTH_PHOTO_RETENTION_DAYS 未设置时生产启动报错（不得无策略落盘）；
  测试模式按临时存储放行（配置值为 None）。
- 外发条款批准：生产启用 aigc.enabled 必须显式设置 BOOTH_AIGC_TERMS_APPROVED=1。
  该键不在 02-spec 配置表内，是 T-11 依任务要求（「生产启用外发要求条款批准」）补齐的显式门。

测试模式契约（仅测试启动器可用）：
- 开关：BOOTH_TEST_MODE、BOOTH_ALLOW_TEST_ADAPTERS，默认 false；
- load_config() 生产调用（默认参数）遇到任一开关为 true 一律报错；只有测试启动器
  （app/tests/run_auto_flow_fixture_server.py，T-14）可传 allow_test_mode=True 显式放行；
- 测试模式只放宽生产门（保留策略、条款批准、启用特性时的依赖要求），不放宽类型、
  范围、布尔枚举、JSON 与路径存在性校验。

取值规则：
- 布尔只接受 true/false/1/0/yes/no（大小写不敏感），其它值（含空串）报错；
- 整数/数字解析失败分别报「需要整数/需要数字」，越界给出明确上下限；
- 可选路径变量不设置时为 None；一旦设置，必须是存在的文件；
- BOOTH_OPTIONAL_FIELDS_ALLOWLIST 是 JSON 字符串数组（元素非空、去重、保序）；
- 相对路径按进程当前目录解析（systemd 的 WorkingDirectory 即 app 目录）。

与既有代码的关系：本模块不回写、不重命名现有环境变量（AIGC_ENABLED、BOOTH_PORT 等仍由
booth_server.py 等旧代码自行读取）；BOOTH_AIGC_ENABLED 与旧 AIGC_ENABLED 是两套门，
互不替代（见 02-spec 功能开关清单）。
"""

from __future__ import annotations

import json
import math
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

__all__ = [
    "ConfigError",
    "ServerConfig",
    "QueueConfig",
    "CaptureConfig",
    "PresentationConfig",
    "CameraConfig",
    "GestureConfig",
    "AigcConfig",
    "StorageConfig",
    "PrinterConfig",
    "LoggingConfig",
    "Config",
    "ENV_KEYS",
    "DEFAULT_CATALOG_PATH",
    "load_config",
]

_APP_DIR = Path(__file__).resolve().parents[1]
DEFAULT_CATALOG_PATH = _APP_DIR / "data" / "style-catalog.json"

_TRUE_VALUES = frozenset({"true", "1", "yes"})
_FALSE_VALUES = frozenset({"false", "0", "no"})
_BOOL_HINT = "只接受 true/false/1/0/yes/no（大小写不敏感）"
_INT_RE = re.compile(r"[+-]?\d+")
_URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)

# 本模块读取的全部环境变量（供契约核查与脱敏测试遍历）
ENV_KEYS: tuple[str, ...] = (
    "BOOTH_HOST",
    "BOOTH_PORT",
    "BOOTH_QUEUE_ENABLED",
    "BOOTH_QUEUE_MAX_ACTIVE",
    "BOOTH_QUEUE_START_TIMEOUT_SECONDS",
    "BOOTH_QUEUE_IDEMPOTENCY_TTL_SECONDS",
    "BOOTH_REQUEST_BODY_MAX_BYTES",
    "BOOTH_OPTIONAL_FIELDS_ALLOWLIST",
    "BOOTH_CAPTURE_COUNTDOWN_SECONDS",
    "BOOTH_PRESENTATION_MODE",
    "BOOTH_AIGC_HANDOFF_DELAY_SECONDS",
    "BOOTH_CAMERA_HEALTH_TIMEOUT_SECONDS",
    "BOOTH_GESTURE_ENABLED",
    "BOOTH_GESTURE_MODEL_PATH",
    "BOOTH_GESTURE_STABLE_FRAMES",
    "BOOTH_GESTURE_MIN_CONFIDENCE",
    "BOOTH_GESTURE_REQUIRE_SINGLE_PERSON",
    "BOOTH_GESTURE_REQUIRE_IN_FRAME",
    "BOOTH_AIGC_ENABLED",
    "BOOTH_AIGC_TERMS_APPROVED",
    "AIGC_ENDPOINT",
    "AIGC_API_KEY_FILE",
    "AIGC_MODEL",
    "AIGC_TIMEOUT",
    "AIGC_MAX_CONCURRENCY",
    "AIGC_MAX_UPLOAD_BYTES",
    "AIGC_STYLE_CATALOG_PATH",
    "BOOTH_PHOTO_RETENTION_DAYS",
    "BOOTH_PRINTER_ENABLED",
    "BOOTH_PRINTER_BACKEND",
    "BOOTH_REDACT_TOKENS",
    "BOOTH_TEST_MODE",
    "BOOTH_ALLOW_TEST_ADAPTERS",
)


class ConfigError(ValueError):
    """启动期配置错误；中文消息可直接进启动日志，env_var 指出出错的环境变量。"""

    def __init__(self, env_var: str, reason: str) -> None:
        self.env_var = env_var
        self.reason = reason
        super().__init__(f"配置错误 {env_var}：{reason}")


@dataclass(frozen=True)
class ServerConfig:
    host: str
    port: int


@dataclass(frozen=True)
class QueueConfig:
    enabled: bool
    max_active: int | None
    start_timeout_seconds: int
    idempotency_ttl_seconds: int
    request_body_max_bytes: int
    optional_fields_allowlist: tuple[str, ...]


@dataclass(frozen=True)
class CaptureConfig:
    countdown_seconds: int


@dataclass(frozen=True)
class PresentationConfig:
    mode: str
    aigc_handoff_delay_seconds: float


@dataclass(frozen=True)
class CameraConfig:
    health_timeout_seconds: float


@dataclass(frozen=True)
class GestureConfig:
    enabled: bool
    model_path: Path | None  # 已废弃：真实实现走 IPC，模型由 worker 加载
    stable_frames: int
    minimum_confidence: float
    require_single_person: bool
    require_in_frame: bool


@dataclass(frozen=True)
class AigcConfig:
    enabled: bool
    base_url: str | None
    api_key_file: Path | None
    model: str
    timeout_seconds: int
    max_concurrency: int
    max_upload_bytes: int
    catalog_path: Path | None
    terms_approved: bool


@dataclass(frozen=True)
class StorageConfig:
    photo_retention_days: int | None


@dataclass(frozen=True)
class PrinterConfig:
    enabled: bool
    backend: str


@dataclass(frozen=True)
class LoggingConfig:
    redact_tokens: bool


@dataclass(frozen=True)
class Config:
    """auto_flow 运行时配置（不可变）。字段名即 spec Configuration 表的键。"""

    server: ServerConfig
    queue: QueueConfig
    capture: CaptureConfig
    presentation: PresentationConfig
    camera: CameraConfig
    gesture: GestureConfig
    aigc: AigcConfig
    storage: StorageConfig
    printer: PrinterConfig
    logging: LoggingConfig
    test_mode: bool = False
    test_adapters_allowed: bool = False

    @property
    def testing(self) -> bool:
        """是否处于测试上下文（测试模式或允许假适配器）。仅测试启动器可打开。"""
        return self.test_mode or self.test_adapters_allowed


# ---- 解析辅助 ---------------------------------------------------------------

def _get(env: Mapping[str, str], name: str) -> str | None:
    """读取环境变量：未设置返回 None；设置了就 strip（空串留给各解析器报错）。"""
    value = env.get(name)
    if value is None:
        return None
    return str(value).strip()


def _range_hint(minimum: float | None, maximum: float | None, value: float) -> str:
    if minimum is not None and maximum is not None:
        return f"必须在 {minimum} 到 {maximum} 之间，实际为 {value}"
    if minimum is not None:
        return f"必须不小于 {minimum}，实际为 {value}"
    return f"必须不大于 {maximum}，实际为 {value}"


def _parse_bool(env: Mapping[str, str], name: str, default: bool) -> bool:
    raw = _get(env, name)
    if raw is None:
        return default
    lowered = raw.lower()
    if lowered in _TRUE_VALUES:
        return True
    if lowered in _FALSE_VALUES:
        return False
    raise ConfigError(name, f"{_BOOL_HINT}，实际值为 {raw!r}")


def _parse_int(
    env: Mapping[str, str],
    name: str,
    default: int,
    *,
    minimum: int | None = None,
    maximum: int | None = None,
) -> int:
    raw = _get(env, name)
    if raw is None:
        return default
    if not _INT_RE.fullmatch(raw):
        raise ConfigError(name, f"需要整数，实际值为 {raw!r}")
    value = int(raw)
    if minimum is not None and value < minimum:
        raise ConfigError(name, _range_hint(minimum, maximum, value))
    if maximum is not None and value > maximum:
        raise ConfigError(name, _range_hint(minimum, maximum, value))
    return value


def _parse_optional_int(
    env: Mapping[str, str],
    name: str,
    *,
    minimum: int | None = None,
    maximum: int | None = None,
) -> int | None:
    if _get(env, name) is None:
        return None
    return _parse_int(env, name, 0, minimum=minimum, maximum=maximum)


def _parse_float(
    env: Mapping[str, str],
    name: str,
    default: float,
    *,
    minimum: float | None = None,
    maximum: float | None = None,
    minimum_exclusive: bool = False,
) -> float:
    raw = _get(env, name)
    if raw is None:
        return default
    try:
        value = float(raw)
    except ValueError:
        raise ConfigError(name, f"需要数字，实际值为 {raw!r}") from None
    if not math.isfinite(value):
        raise ConfigError(name, f"需要有限数字，实际值为 {raw!r}")
    if minimum is not None and minimum_exclusive and value <= minimum:
        raise ConfigError(name, f"必须大于 {minimum}，实际为 {value}")
    if minimum is not None and not minimum_exclusive and value < minimum:
        raise ConfigError(name, _range_hint(minimum, maximum, value))
    if maximum is not None and value > maximum:
        raise ConfigError(name, _range_hint(minimum, maximum, value))
    return value


def _parse_text(env: Mapping[str, str], name: str, default: str) -> str:
    raw = _get(env, name)
    if raw is None:
        return default
    if not raw:
        raise ConfigError(name, "不能为空字符串；不需要该项时请删除该环境变量")
    return raw


def _parse_json_string_list(
    env: Mapping[str, str],
    name: str,
    default: tuple[str, ...],
) -> tuple[str, ...]:
    raw = _get(env, name)
    if raw is None:
        return default
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ConfigError(
            name,
            f'需要 JSON 字符串数组（例如 ["name","company"]），解析失败：{exc.msg}',
        ) from exc
    if not isinstance(payload, list):
        raise ConfigError(
            name,
            f'需要 JSON 数组（例如 ["name","company"]），实际是 {type(payload).__name__}',
        )
    items: list[str] = []
    for index, item in enumerate(payload, start=1):
        if not isinstance(item, str) or not item.strip():
            raise ConfigError(name, f"数组第 {index} 项必须是非空字符串，实际是 {item!r}")
        text = item.strip()
        if text not in items:
            items.append(text)
    return tuple(items)


def _parse_url(env: Mapping[str, str], name: str, default: str | None) -> str | None:
    raw = _get(env, name)
    if raw is None:
        return default
    if not raw:
        raise ConfigError(name, "不能为空字符串；不需要该项时请删除该环境变量")
    if not _URL_RE.fullmatch(raw):
        raise ConfigError(name, f"需要 http/https 地址，实际值为 {raw!r}")
    return raw


def _parse_path(
    env: Mapping[str, str],
    name: str,
    default: Path | None,
    *,
    what: str = "文件",
) -> Path | None:
    """可选路径：未设置 -> default（可为 None）；设置了必须是存在的文件。"""
    raw = _get(env, name)
    if raw is None:
        path = default
    else:
        if not raw:
            raise ConfigError(name, "路径不能为空；不需要该项时请删除该环境变量")
        path = Path(raw).expanduser()
        if not path.is_absolute():
            path = Path.cwd() / path
    if path is None:
        return None
    path = path.resolve()
    if path.is_dir():
        raise ConfigError(name, f"{what}指向目录而不是文件：{path}")
    if not path.is_file():
        raise ConfigError(name, f"{what}不存在：{path}")
    return path


# ---- 主入口 -----------------------------------------------------------------

def load_config(
    env: Mapping[str, str] | None = None,
    *,
    allow_test_mode: bool = False,
) -> Config:
    """解析并校验 auto_flow 全部配置键。

    env: 环境变量映射；默认取 os.environ。测试请注入字典，不依赖机器真实环境变量。
    allow_test_mode: 仅测试启动器传 True；生产调用必须保持默认 False——
        此时 BOOTH_TEST_MODE / BOOTH_ALLOW_TEST_ADAPTERS 为 true 会直接报错。

    校验顺序：测试模式契约 -> 逐项类型/范围/路径 -> 特性依赖门 -> 生产保留策略门。
    任一失败抛 ConfigError（中文，含出错的环境变量名）。
    """
    if env is None:
        env = os.environ

    # 1) 测试模式契约：生产调用（默认）拒绝测试模式与假适配器
    test_mode = _parse_bool(env, "BOOTH_TEST_MODE", False)
    allow_test_adapters = _parse_bool(env, "BOOTH_ALLOW_TEST_ADAPTERS", False)
    if not allow_test_mode and (test_mode or allow_test_adapters):
        flag = "BOOTH_TEST_MODE" if test_mode else "BOOTH_ALLOW_TEST_ADAPTERS"
        raise ConfigError(
            flag,
            "生产配置拒绝测试模式与假适配器；只有测试启动器"
            "（app/tests/run_auto_flow_fixture_server.py）可传 allow_test_mode=True 显式放行。",
        )
    testing = test_mode or allow_test_adapters

    # 2) 逐项解析：类型 + 范围 + 路径存在性
    server = ServerConfig(
        host=_parse_text(env, "BOOTH_HOST", "0.0.0.0"),
        port=_parse_int(env, "BOOTH_PORT", 8090, minimum=1, maximum=65535),
    )
    queue = QueueConfig(
        enabled=_parse_bool(env, "BOOTH_QUEUE_ENABLED", False),
        max_active=_parse_optional_int(env, "BOOTH_QUEUE_MAX_ACTIVE", minimum=1),
        start_timeout_seconds=_parse_int(env, "BOOTH_QUEUE_START_TIMEOUT_SECONDS", 120, minimum=1),
        idempotency_ttl_seconds=_parse_int(env, "BOOTH_QUEUE_IDEMPOTENCY_TTL_SECONDS", 86400, minimum=1),
        request_body_max_bytes=_parse_int(env, "BOOTH_REQUEST_BODY_MAX_BYTES", 8192, minimum=1),
        optional_fields_allowlist=_parse_json_string_list(env, "BOOTH_OPTIONAL_FIELDS_ALLOWLIST", ()),
    )
    capture = CaptureConfig(
        countdown_seconds=_parse_int(env, "BOOTH_CAPTURE_COUNTDOWN_SECONDS", 8, minimum=0),
    )
    presentation_mode = _parse_text(env, "BOOTH_PRESENTATION_MODE", "full").lower()
    if presentation_mode not in ("full", "handoff"):
        raise ConfigError(
            "BOOTH_PRESENTATION_MODE",
            "只接受 full（完整展示）或 handoff（AIGC 8 秒后交回预览）。",
        )
    presentation = PresentationConfig(
        mode=presentation_mode,
        aigc_handoff_delay_seconds=_parse_float(
            env, "BOOTH_AIGC_HANDOFF_DELAY_SECONDS", 8.0, minimum=0.0
        ),
    )
    camera = CameraConfig(
        health_timeout_seconds=_parse_float(
            env,
            "BOOTH_CAMERA_HEALTH_TIMEOUT_SECONDS",
            2.0,
            minimum=0.0,
            minimum_exclusive=True,
        ),
    )
    gesture = GestureConfig(
        enabled=_parse_bool(env, "BOOTH_GESTURE_ENABLED", False),
        model_path=_parse_path(env, "BOOTH_GESTURE_MODEL_PATH", None, what="手势模型文件"),
        stable_frames=_parse_int(env, "BOOTH_GESTURE_STABLE_FRAMES", 10, minimum=1),
        minimum_confidence=_parse_float(
            env, "BOOTH_GESTURE_MIN_CONFIDENCE", 0.90, minimum=0.0, maximum=1.0
        ),
        require_single_person=_parse_bool(env, "BOOTH_GESTURE_REQUIRE_SINGLE_PERSON", True),
        require_in_frame=_parse_bool(env, "BOOTH_GESTURE_REQUIRE_IN_FRAME", True),
    )
    aigc = AigcConfig(
        enabled=_parse_bool(env, "BOOTH_AIGC_ENABLED", False),
        base_url=_parse_url(env, "AIGC_ENDPOINT", None),
        api_key_file=_parse_path(env, "AIGC_API_KEY_FILE", None, what="网关密钥文件"),
        model=_parse_text(env, "AIGC_MODEL", "gpt-image-1"),
        timeout_seconds=_parse_int(env, "AIGC_TIMEOUT", 60, minimum=1),
        max_concurrency=_parse_int(env, "AIGC_MAX_CONCURRENCY", 1, minimum=1),
        max_upload_bytes=_parse_int(env, "AIGC_MAX_UPLOAD_BYTES", 20971520, minimum=1),
        catalog_path=_parse_path(env, "AIGC_STYLE_CATALOG_PATH", DEFAULT_CATALOG_PATH, what="风格目录文件"),
        terms_approved=_parse_bool(env, "BOOTH_AIGC_TERMS_APPROVED", False),
    )
    storage = StorageConfig(
        photo_retention_days=_parse_optional_int(env, "BOOTH_PHOTO_RETENTION_DAYS", minimum=1),
    )
    printer = PrinterConfig(
        enabled=_parse_bool(env, "BOOTH_PRINTER_ENABLED", False),
        backend=_parse_text(env, "BOOTH_PRINTER_BACKEND", "none"),
    )
    logging_config = LoggingConfig(
        redact_tokens=_parse_bool(env, "BOOTH_REDACT_TOKENS", True),
    )

    # 3) 特性依赖门：启用外发/打印时的必要配置（测试模式适度放宽假适配器）
    if aigc.enabled and not testing:
        if not aigc.terms_approved:
            raise ConfigError(
                "BOOTH_AIGC_TERMS_APPROVED",
                "生产启用 AIGC 外发（BOOTH_AIGC_ENABLED=true）前必须确认外置处理告知、"
                "网关条款与留存，并显式设置 BOOTH_AIGC_TERMS_APPROVED=1。",
            )
        if aigc.base_url is None:
            raise ConfigError(
                "AIGC_ENDPOINT",
                "启用 AIGC 外发必须设置网关闭口地址（AIGC_ENDPOINT）。",
            )
        if aigc.api_key_file is None:
            raise ConfigError(
                "AIGC_API_KEY_FILE",
                "启用 AIGC 外发必须设置网关密钥文件路径（只存路径，不落明文 key）。",
            )
    if printer.enabled and printer.backend.strip().lower() == "none":
        raise ConfigError(
            "BOOTH_PRINTER_BACKEND",
            "启用打印（BOOTH_PRINTER_ENABLED=true）必须指定后端设备/协议；"
            "设备验收通过前请保持 BOOTH_PRINTER_ENABLED=false。",
        )

    # 4) 生产保留策略门：缺策略不得启动（测试模式按临时存储放行）
    if storage.photo_retention_days is None and not testing:
        raise ConfigError(
            "BOOTH_PHOTO_RETENTION_DAYS",
            "未设置照片保留策略：生产模式拒绝启动（不得无期限保留人脸照片）。"
            "请确认保留天数后设置 BOOTH_PHOTO_RETENTION_DAYS；"
            "仅测试模式（临时存储）可省略该值。",
        )

    return Config(
        server=server,
        queue=queue,
        capture=capture,
        presentation=presentation,
        camera=camera,
        gesture=gesture,
        aigc=aigc,
        storage=storage,
        printer=printer,
        logging=logging_config,
        test_mode=test_mode,
        test_adapters_allowed=allow_test_adapters,
    )
