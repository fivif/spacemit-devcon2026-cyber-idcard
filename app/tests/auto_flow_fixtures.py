# -*- coding: utf-8 -*-
"""T-14 测试依赖容器：假相机、假时钟与可脚本化假适配器（全部只写临时目录）。

规格依据：`自动流程规格 v1（内部文档）`「Testing strategy · Fixtures / Repository-verified
entry points and injection」「Security」「Error handling」。

边界（重要）：
- 本模块与 ``run_auto_flow_fixture_server.py`` 只服务测试；把端口注入生产 ``booth_server.py``
  属于 T-2/T-16，本任务不改任何生产文件。
- 所有图像与状态只能写临时目录；临时根落在项目仓库内会被直接拒绝（防止误写 app/data、BOOTH_DATA）。
- 拍摄处理器语义（夹具版）：先同意检查（简单令牌检查），通过后再调用注入相机；
  未同意一律 403 CONSENT_REQUIRED，且相机一次都不会被调用（可用 camera.calls 断言）。
- 合成 JPEG 依赖 Pillow（环境已装 Pillow 12），不联网、不依赖相机/AI 核/打印机硬件。

用法::

    from app.tests.auto_flow_fixtures import AutoFlowFixtures

    fx = AutoFlowFixtures(scenario="skeleton")
    result = fx.capture(fx.consent_token)   # 先同意检查，再调用注入相机
    fx.close()                              # 删除临时目录
"""

from __future__ import annotations

import json
import os
import random
import secrets
import shutil
import string
import tempfile
import time as _time
from collections import deque
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

from PIL import Image, ImageDraw, ImageOps

from app.auto_flow import errors
from app.auto_flow.config import ENV_KEYS, Config, load_config
from app.auto_flow.models import ComponentHealth, GestureResult, PhotoRecord, PrintJob, to_utc_iso
from app.auto_flow.ports import CapturedFrame

__all__ = [
    "PROJECT_ROOT",
    "FIXTURES_DIR",
    "HEALTH_FIXTURE_PATH",
    "HEALTH_COMPONENTS",
    "SCENARIO_STEPS",
    "DEFAULT_CLOCK_START",
    "ALLOWED_VARIANTS",
    "to_iso_utc",
    "new_photo_token",
    "load_health_fixture",
    "fixture_env",
    "FakeClock",
    "FakeCamera",
    "ScriptedGesture",
    "FakeGateway",
    "FakePrint",
    "FakePhotoStore",
    "AutoFlowFixtures",
]

PROJECT_ROOT = Path(__file__).resolve().parents[2]
FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"
HEALTH_FIXTURE_PATH = FIXTURES_DIR / "auto_flow_health.json"

# 固定健康夹具的六个组件（spec SystemHealth 字段，顺序即 JSON 文件顺序）
HEALTH_COMPONENTS = ("camera", "gesture", "queue", "storage", "aigc", "printer")

# 四个场景的步骤声明（启动器 --scenario 的取值以此为准）
SCENARIO_STEPS: dict[str, tuple[str, ...]] = {
    "skeleton": ("health", "consent", "capture", "store"),
    "entry": ("health", "entry_page", "optional_fields", "queue_submit"),
    "stage": ("health", "countdown", "gesture_gate", "capture"),
    "external-consent": ("health", "capture", "external_notice", "external_consent", "gateway_edit"),
}

DEFAULT_CLOCK_START = datetime(2026, 9, 30, 0, 0, 0, tzinfo=timezone.utc)
ALLOWED_VARIANTS = ("L0", "styled")
_TOKEN_ALPHABET = string.ascii_letters + string.digits + "-_"


def to_iso_utc(moment: datetime) -> str:
    """datetime -> ISO 8601 UTC 字符串（秒精度 Z 结尾，与 models.to_utc_iso 同一口径）。"""
    return to_utc_iso(moment)


def new_photo_token() -> str:
    """22 位随机 URL 安全照片令牌（测试用；生产令牌归 T-7 TokenService）。"""
    return "".join(secrets.choice(_TOKEN_ALPHABET) for _ in range(22))


def _assert_test_root(root: Path) -> Path:
    """只允许临时目录：拒绝把夹具根目录落在项目仓库内（含 app/data）。"""
    resolved = Path(root).resolve()
    if resolved == PROJECT_ROOT or PROJECT_ROOT in resolved.parents:
        raise ValueError(f"测试夹具拒绝写项目仓库路径：{resolved}（只允许临时目录）")
    return resolved


def load_health_fixture(path: Path | None = None) -> dict:
    """读取固定健康 JSON 并校验六个组件齐备。"""
    fixture_path = Path(path) if path is not None else HEALTH_FIXTURE_PATH
    data = json.loads(fixture_path.read_text(encoding="utf-8"))
    missing = [name for name in HEALTH_COMPONENTS if name not in data]
    if missing:
        raise ValueError(f"健康夹具缺少组件：{missing}（{fixture_path}）")
    return data


def fixture_env(host: str = "127.0.0.1", port: int = 18090, **extra: str) -> Mapping[str, str]:
    """构造仅测试用的配置环境。

    先剔除机器真实环境里的全部 auto_flow 配置键（保证确定性），再强制测试模式与回环绑定；
    这份 env 只能配 ``load_config(..., allow_test_mode=True)`` 使用——生产语义下会被拒绝。
    """
    env = {key: value for key, value in os.environ.items() if key not in ENV_KEYS}
    env.update(
        {
            "BOOTH_TEST_MODE": "1",
            "BOOTH_ALLOW_TEST_ADAPTERS": "1",
            "BOOTH_HOST": host,
            "BOOTH_PORT": str(port),
        }
    )
    env.update(extra)
    return env


class FakeClock:
    """可推进的假时钟；默认从 2026-09-30T00:00:00Z 开始。"""

    def __init__(self, start: datetime | None = None) -> None:
        self._now = start or DEFAULT_CLOCK_START
        if self._now.tzinfo is None:
            self._now = self._now.replace(tzinfo=timezone.utc)

    def now(self) -> datetime:
        return self._now

    def iso(self) -> str:
        return to_iso_utc(self._now)

    def advance(self, seconds: float) -> datetime:
        """向前推进 seconds 秒；返回推进后的时刻。"""
        self._now = self._now + timedelta(seconds=seconds)
        return self._now

    def set(self, moment: datetime) -> None:
        self._now = moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def _render_synthetic_frame(size: tuple[int, int], seed: int, label: str) -> Image.Image:
    """确定性的非真实人像合成帧：渐变底 + 人形轮廓 + 角标帧号。"""
    width, height = size
    rng = random.Random(seed)
    tint = (rng.randint(60, 200), rng.randint(60, 200), rng.randint(60, 200))
    ramp = Image.new("L", (1, 256))
    ramp.putdata(list(range(256)))
    gradient = ramp.resize((width, height))
    image = Image.merge(
        "RGB",
        (
            gradient.point(lambda value, channel=tint[0]: (value * channel) // 255),
            gradient.point(lambda value, channel=tint[1]: (value * channel) // 255),
            gradient.point(lambda value, channel=tint[2]: (value * channel) // 255),
        ),
    )
    draw = ImageDraw.Draw(image)
    center_x = width // 2
    head_radius = max(12, width // 14)
    head_center_y = int(height * 0.30)
    draw.ellipse(
        [center_x - head_radius, head_center_y - head_radius, center_x + head_radius, head_center_y + head_radius],
        fill=(238, 238, 244),
    )
    shoulder = max(head_radius * 2, width // 7)
    draw.rounded_rectangle(
        [center_x - shoulder, head_center_y + head_radius, center_x + shoulder, int(height * 0.92)],
        radius=max(4, head_radius // 2),
        fill=(214, 218, 228),
    )
    draw.rectangle([0, 0, width - 1, height - 1], outline=(255, 255, 255), width=2)
    draw.text((8, 8), label, fill=(24, 24, 24))
    return image


class FakeCamera:
    """合成相机：输出可解码 JPEG；行为可脚本化（指定帧/失败/延迟）。

    - 默认行为：每次抓帧生成 640x480 合成 JPEG（渐变底 + 人形轮廓 + 帧号），
      写入 ``<root>/captures/``，返回 CapturedFrame(source_path, captured_at=假时钟)。
    - ``script_frame(...)`` / ``script_failure(...)`` / ``script_delay(...)`` 按调用顺序消费；
      脚本用完后回到默认行为。
    - ``health()`` 可用 ``set_health()`` 脚本化。
    - ``capture_authorized()`` 只记录 consent_id，不校验同意语义（同意检查在调用方）；
      这样测试可以断言「未同意时相机零调用」。
    """

    def __init__(self, root: Path, clock: FakeClock, size: tuple[int, int] = (640, 480)) -> None:
        self.root = _assert_test_root(root)
        self.captures_dir = self.root / "captures"
        self.captures_dir.mkdir(parents=True, exist_ok=True)
        self.clock = clock
        self.size = size
        self.calls: list[str] = []
        self.last_frame: CapturedFrame | None = None
        self._behaviors: deque[dict[str, Any]] = deque()
        self._health: tuple[str, str | None] = ("ok", None)
        self._frame_seq = 0

    # ---- 脚本化 ----------------------------------------------------------
    def set_health(self, status: str = "ok", reason: str | None = None) -> None:
        self._health = (status, reason)

    def script_frame(
        self,
        *,
        size: tuple[int, int] | None = None,
        seed: int | None = None,
        label: str | None = None,
        copy_from: str | Path | None = None,
    ) -> None:
        """指定下一帧：合成参数或直接拷贝一份既有图像。"""
        self._behaviors.append(
            {"frame": {"size": size, "seed": seed, "label": label, "copy_from": copy_from}}
        )

    def script_failure(self, message: str = "脚本化故障：相机不可用", count: int = 1) -> None:
        for _ in range(max(1, int(count))):
            self._behaviors.append({"fail": str(message)})

    def script_delay(self, seconds: float, count: int = 1) -> None:
        for _ in range(max(1, int(count))):
            self._behaviors.append({"delay": float(seconds)})

    # ---- 端口实现 --------------------------------------------------------
    def health(self) -> ComponentHealth:
        status, reason = self._health
        return ComponentHealth(status=status, checked_at=self.clock.iso(), reason=reason)

    def capture_authorized(self, consent_id: str) -> CapturedFrame:
        self.calls.append(str(consent_id))
        behavior: dict[str, Any] = self._behaviors.popleft() if self._behaviors else {}
        delay = behavior.get("delay")
        if delay:
            _time.sleep(delay)
        if behavior.get("fail"):
            raise RuntimeError(str(behavior["fail"]))
        self._frame_seq += 1
        spec = dict(behavior.get("frame") or {})
        copy_from = spec.get("copy_from")
        if copy_from:
            source = Path(copy_from)
            if not source.is_file():
                raise FileNotFoundError(f"脚本化帧不存在：{source}")
            with Image.open(source) as image:
                image.load()
                frame_image = image.convert("RGB")
        else:
            seed = spec.get("seed")
            seed = (self._frame_seq * 7919) if seed is None else int(seed)
            label = spec.get("label") or f"frame-{self._frame_seq:03d}"
            frame_image = _render_synthetic_frame(tuple(spec.get("size") or self.size), int(seed), str(label))
        frame_path = self.captures_dir / f"frame-{self._frame_seq:04d}.jpg"
        frame_image.save(frame_path, format="JPEG", quality=88)
        self.last_frame = CapturedFrame(source_path=str(frame_path), captured_at=self.clock.iso())
        return self.last_frame


class ScriptedGesture:
    """按脚本序列输出 GestureResult 的假手势适配器。

    - ``script([...])`` 追加结果（dict 或 GestureResult）；``observe()`` 依次弹出，
      脚本耗尽后重复最后一条（没有脚本时返回 gesture="none" 的默认安全结果）。
    - ``status()`` 可按 spec 语义脚本化：未启用时必须如实返回 disabled。
    """

    def __init__(self, clock: FakeClock, enabled: bool = True) -> None:
        self.clock = clock
        self._enabled = bool(enabled)
        self._script: deque[Any] = deque()
        self._last: Any = None
        self._health_override: tuple[str, str | None] | None = None
        self.sessions: list[dict[str, Any]] = []

    def enable(self) -> None:
        self._enabled = True

    def disable(self) -> None:
        self._enabled = False

    def set_health(self, status: str, reason: str | None = None) -> None:
        self._health_override = (status, reason)

    @property
    def exhausted(self) -> bool:
        return not self._script

    def script(self, results: Iterable[Any]) -> None:
        for item in results:
            self._script.append(item)

    def status(self) -> ComponentHealth:
        if self._health_override is not None:
            status, reason = self._health_override
            return ComponentHealth(status=status, checked_at=self.clock.iso(), reason=reason)
        if not self._enabled:
            return ComponentHealth(status="disabled", checked_at=self.clock.iso(), reason="手势未启用（默认关闭）。")
        return ComponentHealth(status="ok", checked_at=self.clock.iso(), reason=None)

    def observe(self, session_id: str) -> GestureResult:
        self.sessions.append({"action": "observe", "session_id": session_id, "at": self.clock.iso()})
        item = self._script.popleft() if self._script else self._last
        if item is None:
            item = {"gesture": "none", "stable_frames": 0, "available": True}
        self._last = item
        if isinstance(item, GestureResult):
            return item
        payload = dict(item)
        payload.setdefault("observed_at", self.clock.iso())
        payload.setdefault("available", True)
        payload.setdefault("gesture", "none")
        payload.setdefault("stable_frames", 0)
        return GestureResult(**payload)

    def stop(self, session_id: str) -> None:
        self.sessions.append({"action": "stop", "session_id": session_id, "at": self.clock.iso()})


class FakeGateway:
    """记录请求、可脚本化响应/超时的假网关；永不联网，输出只写临时目录。

    - 每次 ``edit_image()`` 先记录请求（含 outcome），再按脚本行为：
      ``script_success()`` 生成反色/调色后的合成结果；``script_timeout()`` 抛 504 AIGC_TIMEOUT；
      ``script_error()`` 抛 502 AIGC_FAILED。
    - 请求记录只含图像路径与风格字段，不带任何个人数据/令牌（与 spec Security 外发允许清单一致）。
    """

    def __init__(self, root: Path, clock: FakeClock) -> None:
        self.root = _assert_test_root(root)
        self.clock = clock
        self.requests: list[dict[str, Any]] = []
        self._behaviors: deque[dict[str, Any]] = deque()
        self._health_override: tuple[str, str | None] | None = None
        self._counter = 0

    def set_health(self, status: str, reason: str | None = None) -> None:
        self._health_override = (status, reason)

    def health(self) -> ComponentHealth:
        if self._health_override is not None:
            status, reason = self._health_override
            return ComponentHealth(status=status, checked_at=self.clock.iso(), reason=reason)
        return ComponentHealth(status="ok", checked_at=self.clock.iso(), reason=None)

    def script_success(self, *, tone: tuple[int, int, int] | None = None, count: int = 1) -> None:
        for _ in range(max(1, int(count))):
            self._behaviors.append({"outcome": "ok", "tone": tone})

    def script_timeout(self, count: int = 1) -> None:
        for _ in range(max(1, int(count))):
            self._behaviors.append({"outcome": "timeout"})

    def script_error(self, code: str = errors.AIGC_FAILED, message: str | None = None, count: int = 1) -> None:
        for _ in range(max(1, int(count))):
            self._behaviors.append({"outcome": "error", "code": code, "message": message})

    def edit_image(self, source_path: str, style_id: str, output_path: str) -> str:
        self._counter += 1
        record: dict[str, Any] = {
            "index": self._counter,
            "source_path": str(source_path),
            "style_id": str(style_id),
            "output_path": str(output_path),
            "at": self.clock.iso(),
            "outcome": "pending",
        }
        self.requests.append(record)
        behavior = self._behaviors.popleft() if self._behaviors else {"outcome": "ok"}
        outcome = behavior.get("outcome", "ok")
        record["outcome"] = outcome
        if outcome == "timeout":
            raise errors.ApiError(errors.AIGC_TIMEOUT)
        if outcome == "error":
            raise errors.ApiError(behavior.get("code", errors.AIGC_FAILED), behavior.get("message"))
        source = Path(source_path)
        if not source.is_file():
            record["outcome"] = "error"
            raise errors.ApiError(errors.AIGC_FAILED, "源图不存在，风格化失败。")
        with Image.open(source) as image:
            image.load()
            gray = ImageOps.grayscale(image).convert("RGB")
        tint = behavior.get("tone") or (120, 200, 255)
        styled = Image.blend(gray, Image.new("RGB", gray.size, tuple(tint)), 0.45)
        output = Path(output_path)
        output.parent.mkdir(parents=True, exist_ok=True)
        styled.save(output, format="JPEG", quality=85)
        return str(output)


class FakePrint:
    """脚本化状态的假打印适配器；默认禁用（submit 抛 503 PRINT_UNAVAILABLE）。

    - ``script_statuses([...])`` 依次决定每次提交/推进后的 PrintJob.status；
    - 同一 idempotency_key 重复提交返回同一个任务（不重复打印）；
    - ``advance()`` 把一个任务的脚本状态推进到下一条。
    """

    _ALLOWED = ("unavailable", "queued", "submitted", "completed", "failed")

    def __init__(self, clock: FakeClock, enabled: bool = False) -> None:
        self.clock = clock
        self._enabled = bool(enabled)
        self._statuses: deque[str] = deque()
        self._jobs: dict[str, PrintJob] = {}
        self._by_key: dict[str, str] = {}
        self.submissions: list[dict[str, Any]] = []
        self._health_override: tuple[str, str | None] | None = None
        self._counter = 0

    def set_enabled(self, enabled: bool) -> None:
        self._enabled = bool(enabled)

    def set_health(self, status: str, reason: str | None = None) -> None:
        self._health_override = (status, reason)

    def script_statuses(self, statuses: Iterable[str]) -> None:
        for status in statuses:
            text = str(status)
            if text not in self._ALLOWED:
                raise ValueError(f"非法打印状态：{text}（允许 {self._ALLOWED}）")
            self._statuses.append(text)

    def status(self) -> ComponentHealth:
        if self._health_override is not None:
            status, reason = self._health_override
            return ComponentHealth(status=status, checked_at=self.clock.iso(), reason=reason)
        if not self._enabled:
            return ComponentHealth(status="disabled", checked_at=self.clock.iso(), reason="打印未启用（默认关闭）。")
        return ComponentHealth(status="ok", checked_at=self.clock.iso(), reason=None)

    def submit(self, photo_token: str, idempotency_key: str) -> PrintJob:
        self.submissions.append(
            {"photo_token": str(photo_token), "idempotency_key": str(idempotency_key), "at": self.clock.iso()}
        )
        if not self._enabled:
            raise errors.ApiError(errors.PRINT_UNAVAILABLE)
        if idempotency_key and idempotency_key in self._by_key:
            return self._jobs[self._by_key[idempotency_key]]
        self._counter += 1
        print_job_id = f"print-{self._counter:04d}"
        status = self._statuses.popleft() if self._statuses else "submitted"
        job = PrintJob(
            print_job_id=print_job_id,
            photo_token=str(photo_token),
            status=status,
            created_at=self.clock.iso(),
            updated_at=self.clock.iso(),
        )
        self._jobs[print_job_id] = job
        if idempotency_key:
            self._by_key[str(idempotency_key)] = print_job_id
        return job

    def get(self, print_job_id: str) -> PrintJob | None:
        return self._jobs.get(str(print_job_id))

    def advance(self, print_job_id: str | None = None, status: str | None = None) -> PrintJob | None:
        """推进一个打印任务的状态（缺省推进最近一次提交）。"""
        if print_job_id is None:
            if not self._jobs:
                return None
            print_job_id = next(reversed(self._jobs))
        job = self._jobs.get(str(print_job_id))
        if job is None:
            return None
        next_status = status or (self._statuses.popleft() if self._statuses else job.status)
        if next_status not in self._ALLOWED:
            raise ValueError(f"非法打印状态：{next_status}（允许 {self._ALLOWED}）")
        updated = PrintJob(
            print_job_id=job.print_job_id,
            photo_token=job.photo_token,
            status=next_status,
            created_at=job.created_at,
            updated_at=self.clock.iso(),
        )
        self._jobs[updated.print_job_id] = updated
        return updated


class FakePhotoStore:
    """只写临时目录的照片存储假实现：commit() 落盘拷贝并返回 PhotoRecord（22 位随机令牌）。"""

    def __init__(self, root: Path, clock: FakeClock) -> None:
        self.root = _assert_test_root(root)
        self.clock = clock
        self.photos_dir = self.root / "photos"
        self.state_dir = self.root / "state"
        self.photos_dir.mkdir(parents=True, exist_ok=True)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.records_path = self.state_dir / "photo-records.jsonl"
        self._records: dict[str, PhotoRecord] = {}
        self._paths: dict[str, Path] = {}

    def commit(self, job_id: str, source_path: str, variant: str) -> PhotoRecord:
        if variant not in ALLOWED_VARIANTS:
            raise ValueError(f"非法 variant：{variant!r}（允许 {ALLOWED_VARIANTS}）")
        source = Path(source_path)
        if not source.is_file():
            raise FileNotFoundError(f"源图不存在：{source}")
        token = new_photo_token()
        destination = self.photos_dir / f"{token}{source.suffix.lower() or '.jpg'}"
        shutil.copy2(source, destination)
        record = PhotoRecord(
            photo_token=token,
            job_id=str(job_id),
            created_at=self.clock.iso(),
            status="available",
            variant=variant,
        )
        self._records[token] = record
        self._paths[token] = destination
        with self.records_path.open("a", encoding="utf-8") as handle:
            handle.write(
                json.dumps(
                    {
                        "photo_token": token,
                        "job_id": str(job_id),
                        "variant": variant,
                        "status": record.status,
                        "created_at": record.created_at,
                        "path": str(destination),
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )
        return record

    def get(self, token: str) -> PhotoRecord | None:
        return self._records.get(str(token))

    def path_for(self, token: str) -> Path | None:
        return self._paths.get(str(token))

    def records(self) -> list[PhotoRecord]:
        return list(self._records.values())


class AutoFlowFixtures:
    """T-14 测试依赖容器：组装全部假适配器、临时目录与测试模式配置。

    容器负责夹具版拍摄处理链：同意检查（简单令牌）→ 注入相机抓帧 → 临时 PhotoStore.commit；
    启动器（``run_auto_flow_fixture_server.py``）直接复用本容器。
    """

    def __init__(
        self,
        scenario: str = "skeleton",
        root: Path | None = None,
        host: str = "127.0.0.1",
        port: int = 18090,
    ) -> None:
        if scenario not in SCENARIO_STEPS:
            raise ValueError(f"未知场景：{scenario!r}（允许 {sorted(SCENARIO_STEPS)}）")
        self.scenario = scenario
        if root is None:
            root = Path(tempfile.mkdtemp(prefix="auto-flow-fixtures-"))
        self.root = _assert_test_root(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.clock = FakeClock()
        self.camera = FakeCamera(self.root, self.clock)
        self.gesture = ScriptedGesture(self.clock)
        self.gateway = FakeGateway(self.root, self.clock)
        self.printer = FakePrint(self.clock)
        self.photo_store = FakePhotoStore(self.root, self.clock)
        # 仅测试方式启动：allow_test_mode=True + BOOTH_TEST_MODE=1；生产语义下这份 env 会被拒绝。
        # 配置校验要求端口在 1..65535；端口 0（自动分配）时配置记默认 18090，实际绑定端口以 httpd.server_address 为准。
        config_port = int(port) if int(port) >= 1 else 18090
        self.config: Config = load_config(env=fixture_env(host, config_port), allow_test_mode=True)
        self.steps: list[dict[str, str]] = [
            {"name": name, "status": "pending"} for name in SCENARIO_STEPS[scenario]
        ]
        self.requests = {
            "capture_total": 0,
            "capture_denied": 0,
            "capture_ok": 0,
            "external_total": 0,
            "external_denied": 0,
            "external_ok": 0,
        }
        self._consents: dict[str, str] = {}
        self._job_sources: dict[str, str] = {}
        self._job_seq = 0
        self._health = load_health_fixture()
        self.consent_token = self.grant_consent("capture")

    # ---- 同意（夹具版：简单明确的令牌检查，不等 T-12 ConsentService） ----
    def grant_consent(self, purpose: str = "capture") -> str:
        if purpose not in ("capture", "external_processing"):
            raise ValueError(f"未知同意目的：{purpose!r}")
        token = secrets.token_urlsafe(16)
        self._consents[token] = purpose
        return token

    def has_consent(self, consent_id: str | None, purpose: str) -> bool:
        return bool(consent_id) and self._consents.get(str(consent_id)) == purpose

    # ---- 夹具拍摄处理链 --------------------------------------------------
    def capture(self, consent_id: str | None = None, job_id: str | None = None) -> dict[str, Any]:
        """先同意检查，再调用注入相机，最后提交临时存储；未同意抛 403 CONSENT_REQUIRED。"""
        self.requests["capture_total"] += 1
        if not self.has_consent(consent_id, "capture"):
            self.requests["capture_denied"] += 1
            raise errors.ApiError(errors.CONSENT_REQUIRED)
        frame = self.camera.capture_authorized(str(consent_id))
        self._job_seq += 1
        job = job_id or f"job-{self._job_seq:04d}"
        record = self.photo_store.commit(job, frame.source_path, "L0")
        self._job_sources[job] = frame.source_path
        self.requests["capture_ok"] += 1
        self._mark_step_done("capture")
        return {
            "ok": True,
            "job_id": job,
            "source_path": frame.source_path,
            "captured_at": frame.captured_at,
            "photo": {
                "photo_token": record.photo_token,
                "variant": record.variant,
                "status": record.status,
            },
        }

    def process_external(
        self,
        job_id: str,
        external_consent_id: str | None = None,
        style_id: str = "neon",
        source_path: str | None = None,
    ) -> dict[str, Any]:
        """外置处理：外置同意必须独立（拍摄同意不能替代）；通过后调假网关。"""
        self.requests["external_total"] += 1
        if not self.has_consent(external_consent_id, "external_processing"):
            self.requests["external_denied"] += 1
            raise errors.ApiError(errors.CONSENT_REQUIRED)
        source = source_path or self._job_sources.get(str(job_id))
        if not source:
            raise errors.ApiError(errors.PHOTO_NOT_FOUND)
        self._mark_step_done("external_consent")
        output_path = str(self.root / "styled" / f"{job_id}-{style_id}.jpg")
        result_path = self.gateway.edit_image(source, style_id, output_path)
        record = self.photo_store.commit(job_id, result_path, "styled")
        self.requests["external_ok"] += 1
        self._mark_step_done("gateway_edit")
        return {
            "ok": True,
            "job_id": str(job_id),
            "style_id": style_id,
            "styled_path": result_path,
            "photo": {
                "photo_token": record.photo_token,
                "variant": record.variant,
                "status": record.status,
            },
        }

    # ---- 场景与健康 ------------------------------------------------------
    def _mark_step_done(self, name: str) -> None:
        for step in self.steps:
            if step["name"] == name:
                step["status"] = "done"

    def health_snapshot(self) -> dict:
        """固定健康 JSON 的深拷贝（相机无帧、手势/外发/打印禁用、队列/存储 ok）。"""
        return json.loads(json.dumps(self._health))

    def scenario_state(self) -> dict:
        return {
            "scenario": self.scenario,
            "steps": [dict(step) for step in self.steps],
            "requests": dict(self.requests),
            "camera_captures": len(self.camera.calls),
            "photo_records": len(self.photo_store.records()),
            "clock": self.clock.iso(),
            "test_mode": True,
        }

    # ---- 生命周期 --------------------------------------------------------
    def written_paths(self) -> list[Path]:
        """夹具写过的全部文件（用于断言只落在临时根目录内）。"""
        return [path for path in self.root.rglob("*") if path.is_file()]

    def close(self) -> None:
        """删除夹具自己的临时根目录（只对本次创建的临时目录生效）。"""
        shutil.rmtree(self.root, ignore_errors=True)