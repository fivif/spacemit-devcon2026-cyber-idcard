"""现场控制面：HTTP 服务 + 拍摄编排 + 合规门禁。

一只进程干三件事：抓帧（camera.py）、调推理子进程（infer_client.py）、合成成片（render/）。
推理为什么必须另起进程：cv2 与 spacemit_ort 同进程会崩（docs/spec/系统规格-v2.md §1.1）。

路由契约冻结在 docs/spec/系统规格-v2.md §4。纯标准库（http.server），零 pip 安装。

★ 三条硬约束（都来自真机实测，不是洁癖）：
  1) 未同意不得拍摄：/api/capture 没有有效 consent 一律 403，测试里锁死；
  2) 推理失败绝不让参会者空手离开：超时/报错就走 L0（整幅风格化，不出掩码）照常出图；
  3) 大屏上的耗时与 EP 徽章必须是真实读数，拿不到 AI 核就显示 CPU。
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import math
import os
import queue
import re
import secrets
import socket
import threading
import time
import traceback
import uuid
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, unquote
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from aigc_catalog import CatalogError, CatalogOperationError, STYLE_SEEDS, StyleCatalog, build_portrait_prompt, portrait_mode_prompt
from aigc_channels import AIGCChannels, ChannelConfigError

HERE = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("BOOTH_DATA", HERE / "data"))
STATIC_DIR = HERE / "static"
ASSETS_DIR = HERE / "assets"
PORT = int(os.environ.get("BOOTH_PORT", "8090"))
ACCESS_LOG = os.environ.get("BOOTH_ACCESS_LOG", "0") == "1"
# 操作员在面板上填的服务端配置（当前只有一项：公网取图地址）。
# 放数据目录而不是代码目录：换域名、回滚配置都不该动代码，也不该进部署包。
SETTINGS_PATH = DATA_DIR / "settings.json"
CONSENT_TTL_S = int(os.environ.get("BOOTH_CONSENT_TTL", "600"))
INFER_TIMEOUT_S = float(os.environ.get("BOOTH_INFER_TIMEOUT", "2.5"))
# 一次队列拍摄从倒计时归零起的硬上限（秒）。超过就判定卡死、把队列条目
# 落 failed 并释放拍摄位 —— 否则一个卡住的拍摄会让后面所有人叫不到号。
# 正常一次 40~90 秒（含 AIGC 风格化），这里给足余量再兜底。
CAPTURE_HARD_TIMEOUT_S = float(os.environ.get("BOOTH_CAPTURE_TIMEOUT", "240"))
DEFAULT_AIGC_HANDOFF_DELAY_SECONDS = 8.0
# 「确定没人」的门槛：presence 连续这么多次阴性才算数。与 presence.py 里把
# present 置为 False 的阈值同源（那里是硬编码的 3）。倒计时用它区分「刚到场、
# 还没测出来」和「真的没人」——只看 present 会把真人当成空椅子取消掉。
PRESENCE_STABLE_NEGATIVES = int(os.environ.get("BOOTH_PRESENCE_STABLE_NEGATIVES", "3"))
# 可选风格 —— **以 render/stylize.STYLES 为唯一来源**，这里不再抄一份。
# 抄过就会分弆（坑 23：默认底图名写死成 "official" 就是同一个病）。
# 当前只有「原色」一档：证件照就该是本人的颜色。
_FALLBACK_STYLES = ("natural",)
_STYLES_CACHE: tuple | None = None


def _raise_print_unavailable():
    """打印未启用时的统一出口：抛 ApiError，由 _queue_result 转成嵌套 503 错误体。"""
    _ensure_app_package_root()
    from app.auto_flow import errors as auto_flow_errors

    raise auto_flow_errors.ApiError(
        auto_flow_errors.PRINT_UNAVAILABLE,
        "打印未启用：请在服务配置里设置 BOOTH_PRINT_ENABLED=true 并接好打印机。",
    )


def styles() -> tuple:
    """可选风格（读一次就缓存）。读不到就退回常量 —— 状态页少一行，好过挂掉。"""
    global _STYLES_CACHE
    if _STYLES_CACHE is None:
        try:
            import importlib

            _STYLES_CACHE = tuple(importlib.import_module("render.stylize").STYLES)
        except Exception as exc:  # noqa: BLE001
            log("风格清单读取失败，退回 %s：%s" % (_FALLBACK_STYLES, exc))
            _STYLES_CACHE = _FALLBACK_STYLES
    return _STYLES_CACHE


def default_style() -> str:
    """选择环境指定风格，否则优先选择目录里的默认 AIGC 风格。"""
    want = os.environ.get("BOOTH_STYLE", "").strip()
    catalog = StyleCatalog(DATA_DIR).snapshot()
    valid = set(styles()) | {item["id"] for item in catalog["styles"] if item.get("enabled")}
    if want in valid:
        return want
    aigc_ready = (os.environ.get("AIGC_ENABLED", "0") == "1"
                  and os.environ.get("AIGC_BACKEND", "disabled") == "public_api")
    if aigc_ready:
        for item in catalog["styles"]:
            if item.get("enabled") and item.get("is_default"):
                return item["id"]
    return styles()[0]


# 默认「原色」：参会者拿到的是自己的证件照，颜色得是他本人的颜色。
# 暗调四档仍在，现场可用拍摄位的风格按钮临时切。
# 成片底图（主视觉）默认用哪套。
# 它只是**默认值**：拍摄时参会者可以在界面上选另一套。
# **以 render/keyvisual.py 的 DEFAULT_KEYVISUAL 为准**，这个常量只是
# 「注册表读不到」时的兜底。它曾经写死成 "official"，
# 换素材后没跟着变，于是 /api/state 报的默认底图是一个不存在的名字
# （出图没出事，因为 resolve() 会回退；但前端就选不中任何一项）。
BACKGROUND_DEFAULT = "main"
# 抠像 alpha 回传的最大边。0 = 原尺寸（推荐）：alpha 是往成片上贴的边，
# 缩一次再放大等于白抠 —— 现场量过，640 的 alpha 放大 3 倍后头发是一团糊的。
MATTE_MAX_SIDE = int(os.environ.get("BOOTH_MATTE_MAX_SIDE", "0"))
# 早期为了「一晚上不重样」做了风格轮转。现场跑下来它让成片时彩时灰，
# 同一晚的嘉宾照风格不一致 —— 默认关掉。要轮转就设 BOOTH_STYLE_ROTATE=1。
ROTATE_STYLE = os.environ.get("BOOTH_STYLE_ROTATE", "0") == "1"
BUILD = "devcon5-2026.09"


def _asset_stamp() -> str:
    """把 static/ 的「路径 + 大小 + mtime」揉成一个短指纹。

    为什么不手写版本号：坑 38 —— 取图入口从编号改成令牌之后，现场那些
    **一直开着**的页面还在跑老 JS，继续用编号拼 /api/photo/SM-2026-0001.jpg，
    一律 404，拍摄位上就是一张裂图，而且没有任何提示（编号与耗时是 JSON
    拿的，照常显示，所以看着「只是图没出来」）。指纹跟着文件走，页面自己
    就能发现手上这份 JS 过期了。
    """
    items = []
    for path in sorted(STATIC_DIR.rglob("*")):
        if path.is_file():
            stat = path.stat()
            items.append("%s:%d:%d" % (path.relative_to(STATIC_DIR).as_posix(),
                                       stat.st_size, int(stat.st_mtime)))
    return hashlib.sha1("\n".join(items).encode("utf-8")).hexdigest()[:8]


# 页面版本 = 语义版本 + 静态资源指纹。HTML 里的 __BUILD__ 占位符换成它，
# 页面每 20 秒问一次 /api/build，对不上就自己刷新（见 static/js/net.js）。
PAGE_BUILD = "%s+%s" % (BUILD, _asset_stamp())


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def log(message: str) -> None:
    print("[booth %s] %s" % (datetime.now().strftime("%H:%M:%S"), message), flush=True)


def lan_ip() -> str:
    """本机在局域网里的地址（用于二维码里的取图链接；不依赖外部网络）。

    只回答「本机 IP 是多少」。**公网地址不在这里掺和** —— 以前这里读过
    BOOTH_PUBLIC_HOST，填了它之后 lan_base() 会拼成 http://https://域名:8090，
    一个看着像 URL、点开打不开的字符串。地址的优先级收在 public_base() 一处。
    """
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect(("10.255.255.255", 1))
        return probe.getsockname()[0]
    except Exception:
        return "127.0.0.1"
    finally:
        probe.close()


# --------------------------------------------------------------------------
# 同意门禁（合规红线：人脸是敏感个人信息，必须单独同意）
# --------------------------------------------------------------------------
class ConsentBook:
    """每次拍摄前的单独同意。令牌由服务端签发，TTL 内有效；一键恢复时全部作废。"""

    PURPOSE = "拍摄并生成 SpacemiT DevCon 2026 开发者证件照，用于现场即时分享与领取"
    RETENTION = "照片在本地保存 1 天，取图链接生成后 24 小时内有效，到期自动删除；参会者可随时要求删除"

    def __init__(self, ttl: int = CONSENT_TTL_S):
        self.ttl = ttl
        self._tokens: dict[str, float] = {}
        self._lock = threading.Lock()
        self._version = 0

    def grant(self, kind: str = "photo") -> dict:
        token = uuid.uuid4().hex
        expires = time.time() + self.ttl
        with self._lock:
            self._tokens[token] = expires
            self._version += 1
        return {"token": token, "expires_in": self.ttl, "kind": kind,
                "purpose": self.PURPOSE, "retention": self.RETENTION,
                "version": self._version, "at": _now()}

    def register(self, token: str) -> None:
        """登记一个**外部签发**的令牌（同意由 auto_flow 的 ConsentService 记录）。

        为什么需要它：/api/consent 在队列启用时写进 ConsentService，而
        /api/capture（拍摄位的手动拍摄）校验的是这本旧账 —— 两边不是一个存储，
        只写一边会让手动拍摄的同意永远校验不过（403）。这里把同一个令牌也记
        一份，两边都认。绝不新签令牌，只搬运。
        """
        if not token:
            return
        with self._lock:
            self._tokens[token] = time.time() + self.ttl
            self._version += 1

    def check(self, token: str | None) -> bool:
        if not token:
            return False
        with self._lock:
            expires = self._tokens.get(token)
            if expires is None:
                return False
            if expires < time.time():
                self._tokens.pop(token, None)
                return False
            return True

    def revoke_all(self) -> int:
        with self._lock:
            count = len(self._tokens)
            self._tokens.clear()
            return count

    def stats(self) -> dict:
        with self._lock:
            live = sum(1 for exp in self._tokens.values() if exp >= time.time())
        return {"live_tokens": live, "ttl_s": self.ttl, "grants": self._version}

# --------------------------------------------------------------------------
# 任务看板：大屏要「逐步动画」，所以编排过程必须可被轮询
# --------------------------------------------------------------------------
class Job:
    def __init__(self, job_id: str, style: str):
        self.id = job_id
        self.style = style
        self.portrait_mode = "standard"
        self.ai_watermark = True
        self.stage = "queued"
        self.ok: bool | None = None
        self.error: str | None = None
        self.record_id: str | None = None
        # 取图令牌：跟着这次拍摄生成，前端拿它拼取图 / 二维码 / 下载链接。
        # /api/job/<id> 会把它一起吐出来 —— 局域网内没问题（拍摄位、大屏、
        # 操作员面板都要用），公网那一侧本来就只放行 /p/ 和 /api/photo*。
        self.record_token: str | None = None
        self.degraded = False
        self.degraded_reason: str | None = None
        self.ep = False
        self.ep_threads: int | None = None
        self.infer_ms: float | None = None
        self.total_ms: float | None = None
        self.person_count = 0
        self.mask_applied = False
        self.matte_applied = False
        self.matte_ms = None
        self.matte_source: str | None = None
        self.countdown_remaining: int | None = None
        self.capture_preview_url: str | None = None
        self.cutout_url: str | None = None
        self.segmentation: dict | None = None
        self.portrait_timing: dict = {}
        self.style_preview_url: str | None = None
        self.presentation_mode, self.aigc_handoff_delay_seconds = presentation_runtime_config()
        self.aigc_started_at: float | None = None
        self.source = "camera"
        self.started_at = time.time()
        self.finished_at: float | None = None
        self.steps: list[dict] = []
        self._lock = threading.Lock()

    def step(self, key: str, label: str, ms: float | None = None, detail: str = "") -> None:
        with self._lock:
            self.steps.append({"key": key, "label": label,
                               "ms": round(ms, 1) if ms is not None else None,
                               "detail": detail, "at": round(time.time() - self.started_at, 3)})

    def snapshot(self) -> dict:
        with self._lock:
            handoff_ready = False
            if self.presentation_mode == "handoff" and self.aigc_started_at is not None:
                handoff_ready = (
                    self.stage in ("stylizing", "rendering", "storing")
                    and time.time() - self.aigc_started_at >= self.aigc_handoff_delay_seconds
                )
            return {
                "job": self.id, "stage": self.stage, "ok": self.ok, "error": self.error,
                "record_id": self.record_id, "token": self.record_token,
                "style": self.style, "degraded": self.degraded,
                "portrait_mode": self.portrait_mode,
                "ai_watermark": self.ai_watermark,
                "degraded_reason": self.degraded_reason, "ep": self.ep,
                "ep_threads": self.ep_threads, "infer_ms": self.infer_ms,
                "total_ms": self.total_ms, "person_count": self.person_count,
                "mask_applied": self.mask_applied, "steps": list(self.steps),
                # 抠像实时读数：大屏的抠像动画靠这三个字段显示真实进度，
                # 推理未返回时是 None —— 前端如实只显示文字，不编数字。
                "matte_applied": self.matte_applied, "matte_ms": self.matte_ms,
                "matte_source": self.matte_source,
                "countdown_remaining": self.countdown_remaining,
                "capture_preview_url": self.capture_preview_url,
                "cutout_url": self.cutout_url,
                "segmentation": self.segmentation,
                "portrait_timing": dict(self.portrait_timing),
                "style_preview_url": self.style_preview_url,
                "presentation_mode": self.presentation_mode,
                "aigc_started_at": self.aigc_started_at,
                "aigc_elapsed_ms": round((time.time() - self.aigc_started_at) * 1000, 1)
                    if self.aigc_started_at is not None else None,
                "handoff_ready": handoff_ready,
                "source": self.source,
                "elapsed_ms": round(((self.finished_at or time.time()) - self.started_at) * 1000, 1),
            }


class _BoothPhotoServiceAdapter:
    """取图服务适配器：把队列作业映射到 Booth.store 里的取图令牌。

    `/api/job/<queue_id>` 靠 ``photo_service.store.records()`` 找取图令牌。
    队列流程里作业名就是 queue_id（由 CaptureBridge 传入），这里据此查映射表。
    """

    def __init__(self, booth, job_index):
        self.booth = booth
        self.job_index = job_index  # {queue_id: Job}
        self.store = self

    def records(self):
        """队列流程已完成的作业视图。"""
        from types import SimpleNamespace

        views = [
            _JobRecordView(queue_id, job)
            for queue_id, job in list(self.job_index.items())
            if job.record_token and self.booth.store.photo_path(job.record_id).is_file()
        ]
        known = {view.job_id for view in views}
        for record in self.booth.store.all():
            queue_id = record.extra.get("queue_id")
            if not queue_id or queue_id in known or not record.token:
                continue
            if not self.booth.store.photo_path(record.id).is_file():
                continue
            views.append(SimpleNamespace(job_id=queue_id, photo_token=record.token,
                                         status="available", created_at=record.created_at,
                                         variant="L0" if record.degraded else "styled"))
        return views

    def require(self, token: str):
        """按**取图令牌**取成片记录；不存在或已删则抛 PHOTO_NOT_FOUND。

        `handle_print_submit` 依赖这个方法做打印前置校验（端口契约要求
        photo_service.require）。缺了它打印路由会直接 500 ——
        `AttributeError: '_BoothPhotoServiceAdapter' object has no attribute
        'require'`（实测踩过，是走真实 /api/print 才暴露出来的）。

        与 `_print_photo_path` 同一口径：认令牌不认编号（编号是连号的，
        认编号等于把"全部照片"挂到公网上），并且**要求文件真的在**——
        只查登记表的话，照片已被删但记录还在时会打出一张空白纸。
        """
        from app.auto_flow import errors

        value = str(token or "").strip()
        record = self.booth.store.by_token(value)
        if record is None:
            raise errors.ApiError(errors.PHOTO_NOT_FOUND)
        path = self.booth.store.photo_path(record.id)
        if not path.is_file():
            raise errors.ApiError(errors.PHOTO_NOT_FOUND)
        return record


class _JobRecordView:
    """一条作业记录视图（job_id = 队列作业名，即 queue_id）。"""

    def __init__(self, job_id, job):
        self.job_id = job_id
        self.photo_token = job.record_token
        self.status = "available"
        self.created_at = ""
        self.variant = "styled"


class _NoOpCameraAdapter:
    """旧同步测试/兼容路径的空操作相机适配器。

    生产队列已改用 ``BoothCameraAdapter``，确保倒计时终帧真正落盘；此类保留
    仅为不依赖硬件的旧装配和测试提供稳定占位。
    """

    def __init__(self):
        self.calls = 0

    def capture_authorized(self, consent_id: str):
        """返回占位符 CapturedFrame，commit() 会忽略它。"""
        from datetime import datetime, timezone
        from app.auto_flow.models import to_utc_iso
        from app.auto_flow.ports import CapturedFrame

        self.calls += 1
        return CapturedFrame(
            source_path="/tmp/placeholder-queue-capture.jpg",
            captured_at=to_utc_iso(datetime.now(timezone.utc)),
        )

    def last_frame_at(self):
        """健康探针：返回当前时间，让 CaptureBridge 认为相机可用。"""
        import time
        return time.time()

    def health(self):
        """健康探针：始终返回 ok。"""
        from datetime import datetime, timezone
        from app.auto_flow.models import to_utc_iso, ComponentHealth
        return ComponentHealth(
            status="ok",
            checked_at=to_utc_iso(datetime.now(timezone.utc)),
            reason=None,
        )


class _BoothCapturePhotoStore:
    """队列照片适配器：终帧先落盘，抠像和 AIGC 在后台完成。

    ``commit_async`` 只接管已经拍好的文件并启动后台任务；它不会再次打开相机，
    也不会占用队列拍摄位。AIGC 仍通过 ``booth.lock`` 串行执行，避免多个任务
    同时争用 K3 推理子进程和生成渠道。
    """

    def __init__(self, booth):
        self.booth = booth
        self.calls = 0

    def _prepare_job(self, job_id: str):
        """按队列条目创建作业，并登记到取图查询索引。"""
        queue_entry = self.booth.auto_flow.queue.get(job_id) if self.booth.auto_flow else None
        if queue_entry is None:
            raise RuntimeError(f"队列条目不存在：{job_id}")

        style = getattr(queue_entry, "style_id", None) or self.booth.next_style(None)
        meta = {
            "queue_id": job_id,
            "orientation": "vertical",
            "portrait_mode": getattr(queue_entry, "portrait_mode", "standard"),
            "ai_watermark": getattr(queue_entry, "ai_watermark", True),
            "name": getattr(queue_entry, "name", "")[:24] or "",
            "handle": getattr(queue_entry, "handle", "")[:32] or "",
            "seed": 7,
            "mnemonic": None,
            "source": "camera",
            # 只给自动队列启用 AIGC 自动重试；普通现场/后台任务行为不变。
            "aigc_retry_count": 2,
        }
        previous = self.booth.queue_jobs.get(job_id)
        if previous is not None and self.booth.active_countdown_job is previous:
            self.booth.active_countdown_job = None
        job = self.booth.jobs.create(style)
        job.portrait_mode = meta["portrait_mode"]
        job.ai_watermark = meta["ai_watermark"]
        job.source = "camera"
        self.booth.queue_jobs[job_id] = job
        return job, style, meta

    def _run_async(self, job_id: str, source_path: str, job, style: str, meta: dict) -> None:
        """后台复用终帧完成整套生成，并最终推进队列状态。"""
        from PIL import Image, ImageOps

        try:
            raw = Path(source_path).read_bytes()
            with Image.open(io.BytesIO(raw)) as source:
                source.load()
                photo = ImageOps.exif_transpose(source).convert("RGB")
            if not raw:
                raise RuntimeError("终帧文件为空")
            # 处理线程拿到锁后才进入推理/AIGC；锁不影响下一位抓取终帧。
            if not self.booth.lock.acquire(timeout=45):
                raise RuntimeError("生成任务等待拍摄管道超时")
            try:
                self.booth._run(job, style, meta, source_photo=photo, source_raw=raw)
            finally:
                self.booth.lock.release()

            if not job.snapshot().get("ok"):
                raise RuntimeError(job.snapshot().get("error") or "后台生成未完成")
            try:
                self.booth.auto_flow.queue.mark_ready_for_pickup(job_id)
            except Exception as exc:
                # 用户若在处理中被操作员取消，不再把已取消条目改回可取图。
                log("后台生成完成但队列状态未能置为可取图（%s）：%s" % (job_id[:12], exc))
                return
            log("后台生成完成：%s → 令牌 %s" % (
                job_id[:12], (job.record_token or "")[:8] + "…"))
        except Exception as exc:
            job.ok = False
            job.stage = "failed"
            job.error = "%s: %s" % (type(exc).__name__, exc)
            job.finished_at = time.time()
            try:
                self.booth.auto_flow.queue.mark_failed(
                    job_id, reason="processing_failed: %s" % type(exc).__name__)
            except Exception as queue_exc:
                log("后台生成失败且队列状态无法落 failed（%s）：%s" % (job_id[:12], queue_exc))
            self.booth.last_error = job.error
            self.booth.counters["errors"] += 1
            log("后台生成失败（%s）：%s" % (job_id[:12], traceback.format_exc(limit=4)))
        finally:
            capture_root = (self.booth.store.root / "auto_flow" / "captures").resolve()
            try:
                candidate = Path(source_path).resolve()
                if candidate.parent == capture_root and candidate.is_file():
                    candidate.unlink()
            except OSError:
                pass

    def commit_async(self, job_id: str, source_path: str, variant: str) -> None:
        """接管已落盘终帧，立即返回；后台完成后推进 processing -> ready_for_pickup。"""
        if variant not in ("L0", "styled"):
            raise ValueError(f"非法 variant：{variant!r}")
        if not isinstance(source_path, str) or not source_path:
            raise ValueError("终帧路径不能为空")
        job, style, meta = self._prepare_job(job_id)
        job.stage = "processing"
        thread = threading.Thread(
            target=self._run_async,
            args=(job_id, source_path, job, style, meta),
            name="auto-aigc-%s" % job_id[:8],
            daemon=True,
        )
        thread.start()
        self.calls += 1

    def commit(self, job_id: str, source_path: str, variant: str):
        """同步兼容入口，供旧测试/非队列调用使用。"""
        from datetime import datetime, timezone
        from app.auto_flow.models import PhotoRecord, to_utc_iso

        self.calls += 1
        job, style, meta = self._prepare_job(job_id)
        self.booth.active_countdown_job = job
        try:
            if not self.booth.lock.acquire(timeout=45):
                raise RuntimeError("另一个拍摄任务还在进行（拿不到 Booth.lock）")
            try:
                # 兼容旧同步端口：它没有终帧文件契约，沿用 Booth._run 的直接抓帧。
                self.booth._run(job, style, meta)
            finally:
                self.booth.lock.release()
        except Exception as exc:
            job.ok = False
            job.stage = "failed"
            job.error = "%s: %s" % (type(exc).__name__, exc)
            job.finished_at = time.time()
            raise RuntimeError(f"拍摄管道失败：{job.error}") from exc
        finally:
            if self.booth.active_countdown_job is job:
                self.booth.active_countdown_job = None

        # 检查 Job 结果
        snapshot = job.snapshot()
        if not snapshot["ok"] or snapshot["stage"] != "done":
            raise RuntimeError(
                f"拍摄管道失败：{snapshot.get('error') or '未完成'}"
            )

        # 从 Job 提取 PhotoRecord 所需字段
        record_id = snapshot["record_id"]
        token = snapshot["token"]

        if not token:
            raise RuntimeError("拍摄管道未产生取图令牌")

        # 构造 PhotoRecord（T-7 models.PhotoRecord 字段）
        return PhotoRecord(
            photo_token=token,
            job_id=job_id,
            variant="styled",  # 走了完整风格化管道
            status="available",
            created_at=to_utc_iso(datetime.now(timezone.utc)),
        )


class JobBoard:
    """最近若干次任务的内存台账（不落盘：现场只需要看当前这一次）。"""

    def __init__(self, keep: int = 40):
        self.keep = keep
        self._jobs: dict[str, Job] = {}
        self._order: list[str] = []
        self._lock = threading.Lock()

    def create(self, style: str) -> Job:
        job = Job(uuid.uuid4().hex[:12], style)
        with self._lock:
            self._jobs[job.id] = job
            self._order.append(job.id)
            while len(self._order) > self.keep:
                finished = next((job_id for job_id in self._order
                                 if self._jobs[job_id].finished_at is not None), None)
                if finished is None:
                    break
                self._order.remove(finished)
                self._jobs.pop(finished, None)
        return job

    def active_styles(self) -> set[str]:
        with self._lock:
            return {job.style for job in self._jobs.values()
                    if job.finished_at is None and job.stage not in ("done", "failed")}

    def get(self, job_id: str) -> Job | None:
        with self._lock:
            return self._jobs.get(job_id)

    def capture_preview(self, token: str) -> Job | None:
        if re.fullmatch(r"[A-Za-z0-9_-]{22}", token) is None:
            return None
        with self._lock:
            for job in self._jobs.values():
                if (job.record_token == token and job.capture_preview_url
                        and (job.finished_at is None or time.time() - job.finished_at < 600)):
                    return job
        return None

    def latest(self) -> Job | None:
        with self._lock:
            return self._jobs[self._order[-1]] if self._order else None


# --------------------------------------------------------------------------
# 手势闸门：用户点了「开始」之后，等比出稳定 OK 手势才开拍
# --------------------------------------------------------------------------
def _gesture_gate_allows(booth, queue_id: str, gate_state: dict, now) -> bool:
    """手势闸门：用户点了「开始」之后，是否已经比出稳定 OK 手势。

    返回 True = 放行（可以 begin 倒计时）；False = 再等一 tick。

    为什么要这道闸门：产品流程是「排到了 → 用户点开始 → **比 OK 手势** → 自动
    倒计时拍摄」。「点开始」只表明人已就位并给出同意，真正的开拍信号是手势 ——
    否则用户点完开始转身走开，机器也会把空椅子拍下来。

    开关（**默认关闭，关闭时行为与接线前逐字节一致**）：
      BOOTH_GESTURE_ENABLED 关 → 直接放行（0 延迟、零副作用）
      BOOTH_GESTURE_ENABLED 开 → 必须攒够 BOOTH_GESTURE_STABLE_FRAMES 次
                                 连续阳性才放行

    稳定帧计数：单帧手势会抖，而且 handpose 有"强行拟合"的历史（见
    gesture_rules 头部 2026-10-09 记录），所以要求连续 N 帧阳性 —— 任何一帧
    阴性/判不出都清零。

    探测频率：由 BOOTH_GESTURE_PROBE_INTERVAL（默认 1.5s）限流。tick 本身是
    0.5s 一步，但每步都跑 det（~0.5s 走 CPU）+ handpose 会在本已吃紧的 CPU 上
    空转 —— 现场实测闸门一开，日志每 2 秒刷一批"等待手势"，说明探测比排队快得多。
    限流后：稳定的 OK 手势约 (STABLE_FRAMES × interval) 秒被确认，
    默认 10 × 1.5s ≈ 15 秒。想更快就调小这两个值。

    取帧：用 PreviewHub 的最新预览帧（单生产者分发的那一份），**不**直接读相机
    —— 相机只有预览线程一个读者（camera.PreviewHub 存在的理由）。

    安全方向：任何异常（预览没帧、握手失败、worker 超时）都按**未确认**处理，
    绝不因为"测不了"就放行 —— 那等于绕过手势直接开拍。
    """
    # 手势总开关关着：不拦，行为与旧版一致。
    if getattr(booth, "gesture_port", None) is None:
        return True
    # 启动阶段先让分割模型完成独占预热。手势闸门和正式拍摄共用
    # InferWorker，不能在 EP 初始化窗口同时抢占它。
    inference_ready = getattr(booth, "_inference_ready", None)
    if inference_ready is not None and not inference_ready.is_set():
        return False

    state = gate_state.setdefault(
        queue_id, {"stable": 0, "last_log": None, "wait_logged": False,
                   "last_probe": 0.0})

    # 限流：两次探测之间至少间隔 probe_interval 秒（否则 0.5s 一 tick 会把
    # CPU 空转在 det+handpose 上）。
    import time as _time
    probe_interval = float(os.environ.get("BOOTH_GESTURE_PROBE_INTERVAL", "1.5") or 1.5)
    probe_interval = max(0.2, probe_interval)
    now_ts = _time.time()
    if now_ts - state["last_probe"] < probe_interval:
        return False
    state["last_probe"] = now_ts

    port = booth.gesture_port
    reused_observation = False
    result = None
    if getattr(port, "_engine", "") == "official_yolo":
        observation = getattr(booth, "last_gesture_obs", None)
        observed_at = float((observation or {}).get("at") or 0)
        if (observation and now_ts - observed_at <= 1.2
                and observed_at > float(state.get("last_obs_at") or 0)):
            from types import SimpleNamespace

            state["last_obs_at"] = observed_at
            result = SimpleNamespace(
                gesture="ok" if observation.get("is_ok") else "none",
                available=bool(observation.get("available")),
                in_frame=bool(observation.get("available")),
                stable_frames=0,
                reason=None,
            )
            reused_observation = True

    if result is None:
        try:
            probe = booth._latest_probe_frame()
        except Exception:
            probe = None
        if not probe:
            # 预览没帧（相机没起/刚掉线）：不算阳性也不算阴性，维持现状。
            if not state["wait_logged"]:
                state["wait_logged"] = True
                log("等待手势：预览暂无帧，稍后重试（%s）" % queue_id[:12])
            return False

        import tempfile
        from pathlib import Path

        tmp_path = None
        try:
            jpeg, _image_size = probe
            with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tmp:
                tmp.write(jpeg)
                tmp_path = tmp.name
            # 空闲阶段只跑官方手势模型；人形框验真延后到 OK 后的倒计时阶段，
            # 避免 seg 与 gesture 争用同一个 InferWorker。
            if getattr(port, "_engine", "") == "official_yolo":
                result = port.observe_visual(tmp_path)
            else:
                result = port.observe(tmp_path)
        except Exception as exc:  # noqa: BLE001 - 手势出错绝不许打断队列
            log("手势观测失败（按未确认处理）：%s: %s" % (type(exc).__name__, exc))
            state["failures"] = int(state.get("failures", 0)) + 1
            return False
        finally:
            if tmp_path:
                try:
                    Path(tmp_path).unlink(missing_ok=True)
                except Exception:
                    pass

    gesture = (getattr(result, "gesture", None) or "none")
    available = bool(getattr(result, "available", False))
    is_ok = available and gesture == "ok"
    if available:
        state["failures"] = 0
    else:
        state["failures"] = int(state.get("failures", 0)) + 1

    # 把最新一帧的 seg 框存到 booth 上，竖屏通过 /api/state 读取后实时绘框。
    # result 里没有框数据（GestureResult 只有 gesture/confidence/person_count），
    # 框数据藏在 _observe_yolo 调用的 _seg_provider 回包里。这里用 gesture_port
    # 的内部缓存（若存在）提取；退化情况不影响手势逻辑本身。
    if not reused_observation:
        try:
            _store_gesture_obs(booth, result)
        except Exception:
            pass  # 绘框是调试辅助，绝不能因此打断手势闸门
    # 「没找到手」与「明确不是 OK」分开算（2026-10-09 现场实测教训）：
    # 真人保持 OK 手势时，handpose 会偶发漏检一两帧（conf 跌破 0.30），
    # 把漏检当断连会让连击永远到不了 2 —— 实测 60 秒内 pinch 命中 5 次、
    # 最长连击只有 1，全是漏检帧隔断的。规则：
    #   阳性          → 连击 +1
    #   明确阴性      → 连击清零（真放下了手/换了手势：手找到了但捏合比不达标）
    #   没找到手      → 连击不变（宽容漏检；漏检不等于放弃了手势）
    no_hand = (not available) or (hasattr(result, "in_frame") and result.in_frame is False)
    if is_ok:
        state["stable"] += 1
    elif no_hand:
        pass                      # 漏检帧：不清零、也不加
    else:
        state["stable"] = 0

    stable_frames = int(os.environ.get("BOOTH_GESTURE_STABLE_FRAMES", "10") or 10)
    stable_frames = max(1, stable_frames)

    if is_ok:
        if state["stable"] >= stable_frames:
            if state["last_log"] != "ok":
                state["last_log"] = "ok"
                log("手势已确认（OK，连续 %d 帧），开始倒计时：%s" % (
                    state["stable"], queue_id[:12]))
            return True
        if state["last_log"] != "ok-progress":
            state["last_log"] = "ok-progress"
            log("手势确认中：连续 %d/%d 帧（%s）" % (
                state["stable"], stable_frames, queue_id[:12]))
        return False

    # 还没比出 OK：只在原因变化时打一条，别刷屏。
    reason = getattr(result, "reason", None) or (
        "未识别到 OK 手势" if available else "手势判不出")
    if state["last_log"] != reason:
        state["last_log"] = reason
        log("等待手势（%s）：%s" % (queue_id[:12], reason))
    state["wait_logged"] = True
    return False


def _store_gesture_obs(booth, result) -> None:
    """把最新手势观测的框信息存到 booth.last_gesture_obs，供 /api/state 下发。

    GestureResult 本身没有框坐标，但 _observe_yolo 调用时 seg 响应已在
    GestureClient._seg_provider 返回的 dict 里。最简单的方案：在
    GestureClient 上缓存最后一次 seg 响应，这里从中提取。
    没有缓存就设置空 obs（保持数据流连续，前端能看到"无检测"状态）。
    """
    import time as _time
    port = getattr(booth, "gesture_port", None)
    if port is None:
        return
    engine = str(getattr(port, "_engine", "yolo") or "yolo")
    if engine == "official_yolo":
        gesture = getattr(port, "_last_gesture_result", None)
        if not isinstance(gesture, dict) or not gesture.get("ok"):
            booth.last_gesture_obs = {
                "persons": [], "ok_boxes": [], "is_ok": False, "stable": 0,
                "img_w": 0, "img_h": 0, "at": _time.time(), "engine": engine,
            }
            return
        ok_boxes = []
        for det in gesture.get("detections") or []:
            if str(det.get("label") or "").lower() == "ok" and det.get("box"):
                ok_boxes.append([float(v) for v in det["box"][:4]])
        booth.last_gesture_obs = {
            "persons": [],
            "ok_boxes": ok_boxes,
            "is_ok": bool(getattr(result, "gesture", None) == "ok"
                          and getattr(result, "available", False)),
            "stable": getattr(result, "stable_frames", 0),
            "img_w": int(gesture.get("width") or 0),
            "img_h": int(gesture.get("height") or 0),
            "latency_ms": float(gesture.get("infer_ms") or 0),
            "at": _time.time(),
            "engine": engine,
        }
        return
    seg = getattr(port, "_last_seg_result", None)
    # seg 不可用时也设置空 obs，保持数据流
    if not isinstance(seg, dict) or not seg.get("ok"):
        booth.last_gesture_obs = {
            "persons": [],
            "ok_boxes": [],
            "is_ok": False,
            "stable": 0,
            "img_w": 0,
            "img_h": 0,
            "at": _time.time(),
            "engine": "yolo",
        }
        return
    persons, ok_boxes = [], []
    for det in seg.get("persons") or []:
        lbl = str(det.get("label") or "")
        box = det.get("box")
        if not box or len(box) < 4:
            continue
        if lbl == "person":
            persons.append([float(v) for v in box[:4]])
        elif "ok hand" in lbl.lower() or lbl == getattr(port, "_ok_prompt", "ok hand gesture"):
            ok_boxes.append([float(v) for v in box[:4]])
    is_ok = bool(getattr(result, "gesture", None) == "ok"
                 and getattr(result, "available", False))
    booth.last_gesture_obs = {
        "persons": persons,
        "ok_boxes": ok_boxes,
        "is_ok": is_ok,
        "stable": getattr(result, "stable_frames", 0),
        "img_w": int(seg.get("width") or 0),
        "img_h": int(seg.get("height") or 0),
        "at": _time.time(),
        "engine": "yolo",
    }


def _gesture_preview_needed(booth) -> bool:
    """只在用户点击开始、进入 ready 状态后持续跑手势可视检测。"""
    auto_flow = getattr(booth, "auto_flow", None)
    if auto_flow is None:
        return True
    try:
        entries = auto_flow.queue.snapshot().get("entries") or []
    except Exception:
        return False
    return any(entry.get("status") == "ready" for entry in entries)


# --------------------------------------------------------------------------
# 编排：抓帧 → 推理 → 合成 → 入库
# --------------------------------------------------------------------------
class Booth:
    # worker health 里有、而 seg/det 响应里没有的字段。preflight 与操作员面板都只读
    # /api/state，所以这几个必须原样转发：少了 det_model，明明加载好的检测模型会被
    # 读成「未加载」—— 那和虚报一样是不诚实，只不过方向相反。
    HEALTH_FIELDS = ("seg_model", "det_model", "det_enabled", "gesture_model", "det_ep", "det_requested_threads",
                     "gesture_ep", "gesture_requested_threads", "gesture_providers",
                     "gesture_device", "thread_budget", "ep_ladder", "ep_effective", "runtime_fail",
                     "last_runtime_error", "load_ms", "text_cache")

    def __init__(self, data_dir: Path | str = DATA_DIR):
        import sys

        if str(HERE) not in sys.path:
            sys.path.insert(0, str(HERE))

        from camera import Camera, PreviewHub
        from infer_client import InferWorker
        from store import Store

        self.camera = Camera()
        self.presentation_mode, self.aigc_handoff_delay_seconds = presentation_runtime_config()
        # 预览只有一条抓帧线程（PreviewHub），大屏和拍摄位共享它 ——
        # 以前每个连接各自读相机，两个页面同时开着就互相偷帧，现场看着就是断流。
        self.preview_hub = PreviewHub(
            self.camera,
            fps=int(os.environ.get("BOOTH_PREVIEW_FPS", "15")),
            quality=int(os.environ.get("BOOTH_PREVIEW_QUALITY", "70")),
            output_size=tuple(int(value) for value in os.environ.get(
                "BOOTH_PREVIEW_STREAM_SIZE", "1280x720").lower().split("x")),
        )
        self.preview_hub.start()  # 启动后台抓帧线程（队列健康检查需要）
        self.infer = InferWorker()
        self.store = Store(data_dir)
        self.style_catalog = StyleCatalog(self.store.root)
        self.aigc_channels = AIGCChannels(self.store.root)
        self.aigc_channels.start_monitor()
        for sub_dir in ("qr", "masks", "previews"):
            (self.store.root / sub_dir).mkdir(parents=True, exist_ok=True)
        self.consent = ConsentBook()
        self.jobs = JobBoard()
        # 队列作业索引：queue_id -> Job（队列流程的作业名就是 queue_id，
        # /api/job/<queue_id> 靠它找到取图令牌）
        self.queue_jobs: dict[str, Job] = {}
        self.active_countdown_job: Job | None = None
        self.started_at = time.time()
        self.lock = threading.Lock()
        # 手势探测专用锁：_seg_for_gesture 走同一个 InferWorker subprocess，
        # 不能并发调用（stdin/stdout 管道无法多路复用），preview 线程和手势闸门必须串行。
        self._gesture_seg_lock = threading.Lock()
        self._countdown_person_lock = threading.Lock()
        self._countdown_person_check: dict | None = None
        self._gesture_preview_pause = threading.Event()
        self.last_overlay: dict | None = None
        self.last_error: str | None = None
        # 最近一次手势观测的框数据，供竖屏实时绘框用。结构：
        # {"persons": [[x1,y1,x2,y2], ...], "ok_boxes": [[x1,y1,x2,y2], ...],
        #  "is_ok": bool, "img_w": int, "img_h": int, "at": float}
        self.last_gesture_obs: dict | None = None
        self.counters = {"captures": 0, "degraded": 0, "errors": 0, "cpu_infers": 0, "warmup_ms": None}
        self.ep_threads: int | None = None
        self.ep_info: dict = self._ep_base()
        self._style_seq = 0
        self._preloaded = False
        self._preload_error: str | None = None
        # 首轮模型预热完成前，手势预览与闸门不调用共享推理 worker。
        self._inference_ready = threading.Event()
        # ---- 在场检测（大屏「有人靠近自动唤醒」的判据来源） ----------------
        # 复用既有 det 模型与 worker 子进程：零新增 AI 核槽位、零新模型。
        # 拿不到检测时 presence.present 恒为 None，大屏维持现状，绝不误切。
        from presence import PresenceSensor

        self.presence = PresenceSensor(
            self.infer, self.preview_hub.latest_jpeg, self.store.root)
        # ---- T-2 接线：队列子系统（默认关闭，关闭时零副作用） ----------------
        # BOOTH_QUEUE_ENABLED 未开启时 auto_flow 恒为 None：不 import auto_flow、
        # 不调 load_config()、不创建任何文件 —— 真机（systemd 无
        # BOOTH_PHOTO_RETENTION_DAYS 等变量）启动行为与接线前完全一致。
        self.auto_flow = None
        self._auto_flow_lock = threading.Lock()
        self._init_auto_flow()
        if self.auto_flow is None:
            self.preview_hub.set_gate(True)
        self._init_print_adapter()
        self._init_gesture_adapter()
        # 手势识别和在场检测共用 InferWorker；队列手势开启时不再后台跑 seg，
        # 防止空闲阶段把手势推理挤成数秒一次。
        if not (self.auto_flow is not None and getattr(self, "gesture_port", None) is not None):
            self.presence.start()
        # 实时检测框后台线程：相机门开着就持续探测，更新 last_gesture_obs
        # 供前端 canvas 实时绘框，不依赖手势闸门是否激活。
        self._start_gesture_preview_thread()

    # ---- 打印（T-9 装配） ------------------------------------------------
    def _print_variant_path(self, token: str, variant: str = "watermarked"):
        """取图令牌和版本 -> 打印源文件；后台可选无标识版。"""
        try:
            record = self.store.by_token(token)
        except Exception:
            return None
        if record is None or record.extra.get("tombstone"):
            return None
        if variant == "clean":
            path = self.store.clean_photo_path(record.id)
            output_name = record.id + "-clean.jpg"
        else:
            path = self.store.photo_path(record.id)
            output_name = record.id + ".jpg"
        if not path.is_file():
            return None
        layout = os.environ.get("BOOTH_PRINT_LAYOUT", "").strip()
        if layout in ("card-54x86", "postcard-100x148"):
            from render.print_card import CARD_SIZE, POSTCARD_SIZE, prepare_card

            size = CARD_SIZE if layout == "card-54x86" else POSTCARD_SIZE
            path = prepare_card(path, self.store.root / "prints" / output_name, size=size)
        return path

    def _print_photo_path(self, token: str):
        """兼容旧打印端口注入：公共打印固定使用带标识版。"""
        return self._print_variant_path(token, "watermarked")

    def _init_print_adapter(self) -> None:
        """按 BOOTH_PRINT_ENABLED 装配打印端口；未启用时保持 None。

        未启用 = 两条打印路由按「未部署」处理（503 PRINT_UNAVAILABLE），与
        auto_flow 各开关同一口径：关着就是关着，绝不半启用。
        """
        self.print_port = None
        try:
            import print_adapter

            port = print_adapter.build_print_adapter(
                photo_path=self._print_photo_path)
        except Exception as exc:  # noqa: BLE001
            log("打印端口装配失败（按未启用处理）：%s" % exc)
            return
        if port is None:
            return
        self.print_port = port
        try:
            health = port.status()
            log("打印已启用：%s（%s）" % (health.status, health.reason or "无说明"))
        except Exception as exc:  # noqa: BLE001
            log("打印健康探测失败（仍保持启用）：%s" % exc)

    # ---- 手势识别（T-手势 装配） ------------------------------------------
    def _latest_probe_frame(self, max_width: int = 480):
        """取得检测专用小图；真机优先走 PreviewHub 原始帧快速路径。"""
        fast_probe = getattr(self.preview_hub, "latest_probe_jpeg", None)
        if callable(fast_probe):
            probe = fast_probe(max_width=max_width, quality=80)
            if probe:
                return probe

        jpeg = self.preview_hub.latest_jpeg()
        if not jpeg:
            return None
        from PIL import Image

        with Image.open(io.BytesIO(jpeg)) as image:
            rgb = image.convert("RGB")
            scale = min(1.0, float(max_width) / max(1, rgb.width))
            if scale < 1.0:
                rgb = rgb.resize((max(1, round(rgb.width * scale)),
                                  max(1, round(rgb.height * scale))))
            output = io.BytesIO()
            rgb.save(output, "JPEG", quality=80)
            return output.getvalue(), rgb.size

    def _person_boxes_for(self, image_path: str):
        """给手势验真用的人形框：改用 seg 的 person 检测（det 在 K3 上超时）。

        返回 [[x1,y1,x2,y2], ...] 或 None（seg 不可用 → 调用方判阴性）。
        仅 handpose 引擎需要；yolo 引擎的开放词汇判据天然抗假手。
        """
        try:
            result = self.infer.seg(
                image_path, prompts=["person"], conf=0.15,
                mask=False, mask_max_side=64, matte=False, matte_max_side=0,
                timeout=8.0)
        except Exception as exc:  # noqa: BLE001
            log("手势验真取人形框失败（按阴性处理）：%s" % exc)
            return None
        if not result.get("ok"):
            log("手势验真 seg 返回 ok=False")
            return None
        boxes = []
        for det in result.get("persons") or []:
            if det.get("label") == "person" and det.get("box"):
                boxes.append(list(det["box"]))
        log(f"手势验真取到 {len(boxes)} 个人形框")
        return boxes

    def _start_countdown_person_check(self, queue_id: str) -> None:
        """OK 放行后异步切换模型并显示人形，检测结果不控制快门。"""
        with self._countdown_person_lock:
            current = self._countdown_person_check
            if current and current.get("queue_id") == queue_id:
                return
            check = {"queue_id": queue_id, "status": "pending", "boxes": None,
                     "started_at": time.time(), "error": None}
            self._countdown_person_check = check

        def worker():
            import tempfile
            from pathlib import Path
            try:
                deadline = time.monotonic() + self.auto_flow.countdown.countdown_seconds
                for attempt in range(4):
                    with self._countdown_person_lock:
                        if self._countdown_person_check is not check:
                            return
                    job = self.queue_jobs.get(queue_id)
                    if job is None or job.stage != "countdown" or time.monotonic() >= deadline:
                        return
                    probe = self._latest_probe_frame()
                    if not probe:
                        raise RuntimeError("倒计时开始时暂无相机帧")
                    jpeg, image_size = probe
                    with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tmp:
                        tmp.write(jpeg)
                        probe_path = tmp.name
                    try:
                        boxes = self._person_boxes_for(probe_path)
                    finally:
                        Path(probe_path).unlink(missing_ok=True)
                    if self._record_countdown_person_result(check, boxes, image_size, time.monotonic()):
                        return
                    if attempt < 3:
                        time.sleep(0.8)
            except Exception as exc:
                with self._countdown_person_lock:
                    if self._countdown_person_check is check:
                        check.update({"status": "error", "error": str(exc)})
                log("倒计时人形确认失败（不阻塞拍摄）：%s" % exc)

        threading.Thread(target=worker, name="countdown-person-check", daemon=True).start()

    def _record_countdown_person_result(self, check: dict, boxes, image_size, moment: float) -> bool:
        """人形框仅辅助取景；未知或重复框不改变已确认的拍摄流程。"""
        with self._countdown_person_lock:
            if self._countdown_person_check is not check:
                return True
            job = self.queue_jobs.get(check["queue_id"])
            if job is None or job.stage != "countdown":
                return True
            if boxes is None:
                return False
            check.update({"boxes": boxes, "width": image_size[0], "height": image_size[1],
                          "status": "done"})
            count = len(boxes)
            self.last_gesture_obs = {
                "persons": boxes, "person_count": count,
                "ok_boxes": [], "hand_boxes": [], "palm_boxes": [],
                "hand_points": [], "is_ok": False, "available": True,
                "img_w": image_size[0], "img_h": image_size[1],
                "at": time.time(), "engine": "official_yolo",
            }
            return True

    def _seg_for_gesture(self, image_path: str):
        """给 yolo 手势引擎的分割调用：走服务里唯一的 InferWorker。

        AI 核独占，绝不能起第二个 EP 会话（实测并行第二个 worker 会
        `tcm buffer acquire failed` 把服务的 worker 挤死）。prompts 用
        ["person", "ok hand gesture"]——真人 OK 实测得分 0.59~0.67，
        远距离侧角度都稳。mask/matte 全关：只要框和分数，最快路径。
        _gesture_seg_lock 保证 preview 线程和手势闸门不并发调用 InferWorker。
        """
        # InferWorker 的 stdin/stdout 管道无法并发复用，必须串行。
        # acquire 带超时：拿不到锁就跳过这次（不阻塞等待），避免超时堆积。
        if not self._gesture_seg_lock.acquire(timeout=0.5):
            return {"ok": False, "error": "seg_busy"}
        try:
            return self.infer.seg(
                image_path, prompts=["person", "ok hand gesture"],
                conf=0.15, mask=False, mask_max_side=64, matte=False, matte_max_side=0,
                timeout=12.0)
        finally:
            self._gesture_seg_lock.release()

    def _gesture_for_gesture(self, image_path: str):
        if not self._gesture_seg_lock.acquire(timeout=0.5):
            return {"ok": False, "error": "gesture_busy"}
        try:
            return self.infer.gesture(
                image_path,
                conf=float(os.environ.get("BOOTH_GESTURE_MODEL_CONF", "0.25")),
                iou=float(os.environ.get("BOOTH_GESTURE_MODEL_IOU", "0.45")),
                timeout=8.0,
            )
        finally:
            self._gesture_seg_lock.release()

    def _init_gesture_adapter(self) -> None:
        """按 BOOTH_GESTURE_ENABLED 装配手势端口；未启用时保持 None。

        未启用 = 手势检测路由按「未部署」处理（503），与 auto_flow 各开关同一口径。
        """
        self.gesture_port = None
        try:
            from auto_flow.gesture_adapter import build_gesture_adapter

            port = build_gesture_adapter(
                person_boxes_provider=self._person_boxes_for,
                seg_provider=self._seg_for_gesture,
                gesture_provider=self._gesture_for_gesture)
            # 注入 log 函数给 yolo 引擎调试
            if hasattr(port, "_log"):
                port._log = log
        except Exception as exc:  # noqa: BLE001
            log("手势端口装配失败（按未启用处理）：%s" % exc)
            return
        if port is None:
            return
        self.gesture_port = port
        try:
            health = port.status()
            log("手势已启用：%s（%s）" % (health.status, health.reason or "无说明"))
        except Exception as exc:  # noqa: BLE001
            log("手势健康探测失败（仍保持启用）：%s" % exc)

    def _start_gesture_preview_thread(self) -> None:
        """后台持续探测线程：通过 gesture_port.observe() 调用手势识别。

        支持引擎切换（BOOTH_GESTURE_ENGINE: yolo / handpose）。
        1.5 秒一次，拍摄时跳过。手势功能未启用时此线程是空操作。
        """
        auto_enabled = os.environ.get("BOOTH_GESTURE_ENABLED", "").strip().lower() in ("true", "1", "yes")
        visual_enabled = os.environ.get("BOOTH_GESTURE_VISUAL_ENABLED", "").strip().lower() in ("true", "1", "yes")
        if getattr(self, "gesture_port", None) is None or not (auto_enabled or visual_enabled):
            return

        interval = float(os.environ.get("BOOTH_GESTURE_PREVIEW_INTERVAL", "0.50"))
        interval = max(0.25, interval)
        engine = os.environ.get("BOOTH_GESTURE_ENGINE", "yolo").strip().lower()

        def _loop():
            import tempfile, pathlib as _pl
            log(f"[gesture-preview] 线程已启动：interval={interval}s, engine={engine}")
            tick = 0
            last_visual_obs = None
            smooth_alpha = min(1.0, max(0.05, float(
                os.environ.get("BOOTH_GESTURE_VISUAL_SMOOTH_ALPHA", "0.58"))))
            hold_seconds = max(0.0, float(
                os.environ.get("BOOTH_GESTURE_VISUAL_HOLD_SECONDS", "0.25")))

            def _smooth_points(current, previous):
                if not previous or len(current) != len(previous):
                    return current
                return [[smooth_alpha * point[0] + (1.0 - smooth_alpha) * old[0],
                         smooth_alpha * point[1] + (1.0 - smooth_alpha) * old[1]]
                        for point, old in zip(current, previous)]

            def _smooth_boxes(current, previous):
                if not previous or len(current) != len(previous):
                    return current
                return [[smooth_alpha * value + (1.0 - smooth_alpha) * old_value
                         for value, old_value in zip(box, old_box)]
                        for box, old_box in zip(current, previous)]

            while True:
                cycle_started = time.monotonic()
                try:
                    tick += 1
                    if not self._inference_ready.is_set():
                        time.sleep(0.05)
                        continue
                    if not _gesture_preview_needed(self):
                        self.last_gesture_obs = None
                        last_visual_obs = None
                        continue
                    # 拍摄进行中：跳过
                    if self.lock.locked():
                        if tick % 10 == 1:
                            log(f"[gesture-preview] tick {tick}: 拍摄锁占用，跳过")
                        continue
                    # 倒计时期间让一次性人形确认独占 InferWorker；否则预览线程会
                    # 继续发起 gesture 请求，把分割确认拖到超时并造成明显卡顿。
                    if self._gesture_preview_pause.is_set():
                        time.sleep(0.05)
                        continue
                    # yolo 引擎需要检查 InferWorker 是否忙（handpose 用独立进程，不受影响）
                    if engine in ("yolo", "official_yolo") and self.infer._busy.is_set():
                        if tick % 10 == 1:
                            log(f"[gesture-preview] tick {tick}: InferWorker 忙，跳过")
                        continue
                    probe = self._latest_probe_frame()
                    if not probe:
                        if tick % 10 == 1:
                            log(f"[gesture-preview] tick {tick}: 无相机帧，跳过")
                        continue
                    jpeg, image_size = probe
                    tmp = None
                    try:
                        with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as f:
                            f.write(jpeg)
                            tmp = f.name
                        if visual_enabled and hasattr(self.gesture_port, "observe_visual"):
                            result = self.gesture_port.observe_visual(tmp)
                        else:
                            result = self.gesture_port.observe(tmp)
                        if tick % 10 == 1:
                            log(f"[gesture-preview] tick {tick}: observe 完成，gesture={result.gesture}, available={result.available}")
                        # 从 result 提取框数据（handpose 引擎没有框，只有 gesture 结果）
                        persons = []
                        ok_boxes = []
                        hand_boxes = []
                        palm_boxes = []
                        hand_points = []
                        img_w, img_h = image_size
                        # 开放词汇 yolo：从 seg 响应中提取框
                        if engine == "yolo" and hasattr(self.gesture_port, "_last_seg_result"):
                            seg = self.gesture_port._last_seg_result or {}
                            img_w = int(seg.get("width") or img_w)
                            img_h = int(seg.get("height") or img_h)
                            for det in seg.get("persons") or []:
                                lbl = str(det.get("label") or "")
                                box = det.get("box")
                                if box and len(box) >= 4:
                                    if lbl == "person":
                                        persons.append([float(v) for v in box[:4]])
                                    elif "hand" in lbl.lower() or "ok" in lbl.lower():
                                        ok_boxes.append([float(v) for v in box[:4]])
                        if engine == "official_yolo":
                            gesture = getattr(self.gesture_port, "_last_gesture_result", None) or {}
                            img_w = int(gesture.get("width") or img_w)
                            img_h = int(gesture.get("height") or img_h)
                            for det in gesture.get("detections") or []:
                                if str(det.get("label") or "").lower() == "ok" and det.get("box"):
                                    ok_boxes.append([float(v) for v in det["box"][:4]])
                        if engine == "handpose":
                            hand = getattr(self.gesture_port, "_last_hand_result", None) or {}
                            img_w = int(hand.get("image_width") or img_w)
                            img_h = int(hand.get("image_height") or img_h)
                            box = hand.get("box")
                            if box and len(box) >= 4:
                                hand_boxes.append([float(v) for v in box[:4]])
                            palm_box = hand.get("palm_box")
                            if palm_box and len(palm_box) >= 4:
                                palm_boxes.append([float(v) for v in palm_box[:4]])
                            for point in hand.get("points") or []:
                                if isinstance(point, (list, tuple)) and len(point) >= 2:
                                    hand_points.append([float(point[0]), float(point[1])])
                        now = time.time()
                        if hand_points and last_visual_obs:
                            hand_points = _smooth_points(
                                hand_points, last_visual_obs.get("hand_points"))
                            hand_boxes = _smooth_boxes(
                                hand_boxes, last_visual_obs.get("hand_boxes"))
                            palm_boxes = _smooth_boxes(
                                palm_boxes, last_visual_obs.get("palm_boxes"))
                        is_ok = auto_enabled and result.gesture in ("thumbs_up", "ok")
                        observation = {
                            "persons": persons, "ok_boxes": ok_boxes,
                            "hand_boxes": hand_boxes, "palm_boxes": palm_boxes,
                            "hand_points": hand_points,
                            "is_ok": is_ok, "available": result.available,
                            "latency_ms": (
                                float(hand.get("ms") or 0) if engine == "handpose" else
                                float((getattr(self.gesture_port, "_last_gesture_result", {}) or {}).get("infer_ms") or 0)
                                if engine == "official_yolo" else None
                            ),
                            "img_w": img_w, "img_h": img_h,
                            "at": now, "engine": engine, "held": False,
                        }
                        if hand_points:
                            observation["detected_at"] = now
                            last_visual_obs = dict(observation)
                        elif (last_visual_obs and
                              now - float(last_visual_obs.get("detected_at") or 0) <= hold_seconds):
                            observation = dict(last_visual_obs)
                            observation.update({
                                "at": now, "is_ok": False, "available": False,
                                "held": True,
                            })
                        else:
                            last_visual_obs = None
                        self.last_gesture_obs = observation
                    except Exception as ex:
                        if tick % 10 == 1:
                            log("[gesture-preview] observe 失败：%s" % ex)
                    finally:
                        if tmp:
                            try: _pl.Path(tmp).unlink(missing_ok=True)
                            except Exception: pass
                except Exception as ex:
                    log("[gesture-preview] 外层异常：%s" % ex)
                finally:
                    elapsed = time.monotonic() - cycle_started
                    time.sleep(max(0.03, interval - elapsed))

        threading.Thread(target=_loop, name="gesture-preview", daemon=True).start()

    def _init_auto_flow(self) -> None:
        """按 BOOTH_QUEUE_ENABLED 装配队列子系统（T-2）+ 三件套（scheduler/countdown/bridge）。

        开关语义（与 T-11 config._parse_bool 同词表）：
        - 未设置 / 空串 / false / 0 / no：**零副作用**——不 import auto_flow、
          不调 load_config()、不写任何文件；缺 BOOTH_PHOTO_RETENTION_DAYS 等
          生产变量也不会影响启动（T-11 的严格加载只在显式开启时才走）；
        - true / 1 / yes：走严格加载 load_config()，缺生产门（如保留策略）会在
          这里直接启动报错（T-11 的有意设计），绝不"半启用"；随后装配
          QueueService / ConsentService / 会话注册表 + 三件套（scheduler/countdown/bridge），
          常驻 self.auto_flow；
        - 其它未识别值：不静默当关闭，交给 queue_feature_enabled() 抛 ConfigError。
        测试注入：构造后直接赋值 ``booth.auto_flow = AutoFlowServices(...)`` 即可
        （见 app/auto_flow/http_handlers.AutoFlowServices）。
        """
        raw = str(os.environ.get("BOOTH_QUEUE_ENABLED", "")).strip().lower()
        if raw in ("", "false", "0", "no"):
            return
        _ensure_app_package_root()
        from app.auto_flow.capture_bridge import CaptureBridge
        from app.auto_flow.config import load_config
        from app.auto_flow.consent_service import ConsentService, ConsentStore
        from app.auto_flow.countdown_service import CountdownService
        from app.auto_flow.http_handlers import (
            AutoFlowServices,
            BoothCameraAdapter,
            QueueSessionRegistry,
            queue_feature_enabled,
        )
        from app.auto_flow.queue_scheduler import QueueScheduler
        from app.auto_flow.queue_service import QueueService
        from app.auto_flow.queue_store import QueueStore
        from app.auto_flow.recovery_service import RecoveryRegistry

        if not queue_feature_enabled():
            return  # 防御性：显式 false 已在快速路径返回；未识别值在上面抛错
        cfg = load_config()
        presentation = getattr(cfg, "presentation", None)
        if presentation is not None:
            self.presentation_mode = presentation.mode
            self.aigc_handoff_delay_seconds = presentation.aigc_handoff_delay_seconds
        # 操作后台保存的模式优先于环境变量，且不需要重启即可用于下一位。
        self.presentation_mode, self.aigc_handoff_delay_seconds = presentation_runtime_config()
        data_root = self.store.root
        queue_service = QueueService(QueueStore(data_root), cfg.queue)
        consent_service = ConsentService(ConsentStore(data_root))

        # 倒计时归零只抓一帧并落到临时目录；后台生成复用该文件，
        # 不再让 PhotoStore 重新打开相机导致“定格图”和最终图不一致。
        capture_dir = data_root / "auto_flow" / "captures"
        camera_adapter = BoothCameraAdapter(
            lambda: self.camera,
            capture_dir,
            freshness_timeout=2.0,
        )

        # PhotoStorePort 适配器：直接调用 Booth._run() 管道
        photo_store_adapter = _BoothCapturePhotoStore(self)

        # CaptureBridge：拍摄桥（状态机管理）
        bridge = CaptureBridge(
            queue_service=queue_service,
            camera=camera_adapter,
            photo_store=photo_store_adapter,
        )

        # 修复 #5: 统一相机探针逻辑（都用 freshness 判断）
        def camera_health_probe():
            """用户点击开始时才开预览，并等待一帧新鲜画面。"""
            deadline = time.monotonic() + 4.0
            self._camera_starting_until = deadline + 0.5
            set_gate = getattr(self.preview_hub, "set_gate", None)
            if callable(set_gate):
                set_gate(True)
            while time.monotonic() < deadline:
                stats = self.preview_hub.stats()
                age = stats.get("last_frame_s")
                if age is not None and age < 2.0:
                    return True
                time.sleep(0.05)
            self._camera_starting_until = 0.0
            if callable(set_gate):
                set_gate(False)
            return False

        countdown = CountdownService(
            queue_service=queue_service,
            consent_service=consent_service,
            countdown_seconds=cfg.capture.countdown_seconds,
            camera_probe=lambda now: camera_health_probe(),
        )

        # QueueScheduler：自动叫号
        # 注意：camera_last_frame_at 需要 **Unix 时间戳**，但 preview_hub.stats()
        # 的 last_frame_s 是 **年龄（秒）** —— 必须换算，否则会被算成 17 亿秒前。
        def preview_last_frame_at():
            import time
            age_s = self.preview_hub.stats().get("last_frame_s")
            if age_s is None:
                return None
            return time.time() - float(age_s)

        scheduler = QueueScheduler(
            queue_service,
            camera_last_frame_at=preview_last_frame_at,
            camera_timeout_seconds=2.0,
            gesture_enabled=cfg.gesture.enabled,
            gesture_required_for_auto_trigger=True,
            # 排队和叫号不需要打开相机；相机只在用户点击“开始拍摄”后
            # 由 camera_health_probe 打开。否则预览门关闭时队列会永久停在 queued。
            allow_queue_without_camera=True,
        )

        try:
            sessions = QueueSessionRegistry(data_root / "auto_flow" / "queue_sessions.json")
        except (OSError, ValueError) as exc:
            log("拍摄会话暂不能持久保存，使用当前页面继续：%s" % type(exc).__name__)
            sessions = QueueSessionRegistry()
        try:
            recovery = RecoveryRegistry(data_root / "auto_flow" / "mobile_recovery.json")
        except (OSError, ValueError) as exc:
            log("备用恢复暂不可用，保留原拍摄流程：%s" % type(exc).__name__)
            recovery = None

        self.auto_flow = AutoFlowServices(
            queue=queue_service,
            consent=consent_service,
            sessions=sessions,
            recovery=recovery,
            style_source=self.style_catalog,
            camera_probe=lambda: camera_health_probe(),
            gesture_enabled=cfg.gesture.enabled,
            # 取图服务：/api/job/<id> 用它解析照片令牌与取图链接
            photo_service=_BoothPhotoServiceAdapter(self, self.queue_jobs),
        )
        # 修复 #8: 正式挂载三件套（避免动态添加字段）
        self.auto_flow.scheduler = scheduler
        self.auto_flow.countdown = countdown
        self.auto_flow.bridge = bridge

        log(
            "队列子系统已启用：%s，起始期限 %ds，数据目录 %s"
            % ("不设排队上限" if cfg.queue.max_active is None
               else "max_active=%d" % cfg.queue.max_active,
               cfg.queue.start_timeout_seconds, data_root)
        )

        # 启动后台 tick 线程驱动 scheduler + countdown
        self._start_auto_flow_tick_thread()

    def _start_auto_flow_tick_thread(self) -> None:
        """启动后台 tick 线程：驱动 QueueScheduler 与 CountdownService。

        防重入：多次调用只保留一条线程（重复启动会让两个线程各持一份
        ``active_countdowns``，互相看不见、于是重复 begin 同一次倒计时）。

        单步（0.5s）：
        1. scheduler.tick —— 到期弃号 + 无占用时叫号；
        2. 看到 ready 的条目就登记并 begin 倒计时（登记后不再重复 begin）；
        3. tick 倒计时；仅在相位变化时打日志；capture_due 时另起线程拍照。
        """
        if self.auto_flow is None:
            return
        # 防重入
        guard = getattr(self, "_auto_flow_tick_thread", None)
        if guard is not None and guard.is_alive():
            return

        def tick_loop():
            from datetime import datetime, timezone
            import time

            log("队列后台 tick 线程已启动（驱动 scheduler + countdown）")
            active_countdowns: dict[str, dict] = {}
            last_phase: dict[str, str] = {}
            # 已终结的会话：cancel/completed/已触发拍摄的，不再 begin，
            # 否则「取消 → 下个 tick 又 begin」会变成每 0.5 秒一轮的死循环。
            finished: set[str] = set()
            # 手势闸门状态：queue_id -> {"ready_since": ts, "stable": n,
            #                          "last_log": str, "waited_logged": bool}
            # 用户点了「开始」（status=ready）之后**不立刻**倒计时，先等 OK 手势。
            # 见 _gesture_gate_allows。
            gesture_gate: dict[str, dict] = {}
            consecutive_errors = 0
            backoff_until = 0.0
            last_frozen_log = None

            while True:
                try:
                    now_ts = time.time()
                    if now_ts < backoff_until:
                        time.sleep(0.1)
                        continue

                    time.sleep(0.5)

                    if self.auto_flow is None:
                        break

                    now = datetime.now(timezone.utc)

                    # 1. 调度：弃号 + 叫号
                    scheduler = self.auto_flow.scheduler
                    if scheduler is not None:
                        try:
                            tick_result = scheduler.tick(now)
                            if tick_result.get("called"):
                                info = tick_result["called"]
                                log("自动叫号：%s（%s）" % (
                                    info["queue_id"], info.get("display_number")))
                            # 冻结/恢复只在状态翻转时打一条
                            frozen = bool(tick_result.get("frozen"))
                            if frozen != last_frozen_log:
                                last_frozen_log = frozen
                                if frozen:
                                    log("调度暂停叫号：%s" % (tick_result.get("camera_reason") or "无相机帧"))
                                else:
                                    log("调度恢复叫号：相机帧已新鲜")
                        except Exception as exc:
                            log("scheduler.tick 失败：%s" % exc)

                    # 2. 驱动倒计时
                    countdown = self.auto_flow.countdown
                    bridge = self.auto_flow.bridge
                    if countdown is None or bridge is None:
                        continue

                    snapshot = self.auto_flow.queue.snapshot()
                    ready_ids = {
                        e.get("queue_id") for e in snapshot.get("entries", [])
                        if e.get("status") == "ready"
                    }

                    for entry_dict in snapshot.get("entries", []):
                        queue_id = entry_dict.get("queue_id")
                        status = entry_dict.get("status")
                        if not queue_id:
                            continue

                        # 新出现的 ready：先过手势闸门（等用户比 OK），
                        # 过了才 begin 倒计时；没过就下一 tick 再看。
                        if (status == "ready" and queue_id not in active_countdowns
                                and queue_id not in finished):
                            if not _gesture_gate_allows(
                                    self, queue_id, gesture_gate, now):
                                continue
                            consent_id = entry_dict.get("consent_id")
                            try:
                                now = datetime.now(timezone.utc)
                                countdown.begin(
                                    session_id=queue_id,
                                    queue_id=queue_id,
                                    capture_consent_id=consent_id,
                                    now=now,
                                )
                                job = self.queue_jobs.get(queue_id)
                                if job is None:
                                    job = Job(queue_id, entry_dict.get("style_id") or "natural")
                                    job.portrait_mode = entry_dict.get("portrait_mode", "standard")
                                    job.ai_watermark = entry_dict.get("ai_watermark", True)
                                    self.queue_jobs[queue_id] = job
                                job.stage = "countdown"
                                job.countdown_remaining = int(countdown.countdown_seconds)
                                self.active_countdown_job = job
                                # begin 成功后再暂停手势预览并启动一次人形确认；
                                # begin 失败不能留下永远暂停的预览线程。
                                self._gesture_preview_pause.set()
                                self.last_gesture_obs = {
                                    "persons": [], "person_count": 0,
                                    "ok_boxes": [], "hand_boxes": [], "palm_boxes": [],
                                    "hand_points": [], "is_ok": False, "available": False,
                                    "img_w": 0, "img_h": 0, "at": time.time(),
                                    "engine": "countdown",
                                }
                                self._start_countdown_person_check(queue_id)
                                log("倒计时开始：%s（同意%s）" % (
                                    queue_id[:12], "已绑定" if consent_id else "缺失"))
                            except Exception as exc:
                                log("countdown.begin 失败（%s）：%s: %s" % (
                                    queue_id[:12], type(exc).__name__, exc))
                            # 成功失败都登记，避免每 tick 重复 begin
                            active_countdowns[queue_id] = entry_dict

                        # 已登记的会话：推进倒计时
                        if queue_id in active_countdowns:
                            try:
                                tick = countdown.tick(queue_id, now=datetime.now(timezone.utc))

                                # 把服务端倒计时投影到主作业，页面只读这个真实状态。
                                job = self.queue_jobs.get(queue_id) or self.jobs.get(queue_id)
                                if job is None:
                                    job = Job(queue_id, entry_dict.get("style_id") or "natural")
                                    job.portrait_mode = entry_dict.get("portrait_mode", "standard")
                                    job.ai_watermark = entry_dict.get("ai_watermark", True)
                                    self.queue_jobs[queue_id] = job
                                self.active_countdown_job = job
                                if job is not None:
                                    job.countdown_remaining = (
                                        math.ceil(tick.remaining_seconds)
                                        if tick.phase == "countdown" and tick.remaining_seconds is not None
                                        else None
                                    )
                                    if tick.phase == "countdown":
                                        job.stage = "countdown"
                                    elif tick.phase == "capturing":
                                        job.stage = "capturing"
                                    elif tick.phase in ("completed", "cancelled"):
                                        self.active_countdown_job = None

                                if last_phase.get(queue_id) != tick.phase:
                                    last_phase[queue_id] = tick.phase
                                    log("倒计时 %s → %s（剩余 %s，%s）" % (
                                        queue_id[:12], tick.phase,
                                        tick.remaining_seconds, tick.reason_code))

                                if tick.capture_due:
                                    log("倒计时归零，开始拍摄：%s" % queue_id[:12])
                                    consent_id = active_countdowns[queue_id].get("consent_id")
                                    finished.add(queue_id)  # 只拍一次

                                    # 看门狗：拍摄线程卡住也必须交出拍摄位。
                                    # 没有它时，一次卡住的拍摄会让条目永远停在
                                    # capturing，后面排队的人全部叫不到号
                                    # （实测卡过 900 秒，队列彻底堵死）。
                                    watchdog = {"done": False}

                                    def capture_worker(qid=queue_id, cid=consent_id,
                                                       wd=watchdog):
                                        try:
                                            result = bridge.run(
                                                queue_id=qid,
                                                capture_consent_id=cid,
                                                job_id=qid,
                                                variant="styled",
                                                capture_due=True,
                                            )
                                            log("拍摄完成：%s → 令牌 %s" % (
                                                qid[:12],
                                                result.photo.photo_token[:8] + "…"
                                                if result.photo else "无"))
                                        except Exception as exc:
                                            log("拍摄失败（%s）：%s: %s" % (
                                                qid[:12], type(exc).__name__, exc))
                                        finally:
                                            wd["done"] = True   # 先落标志，再清理
                                            active_countdowns.pop(qid, None)
                                            pending = self.active_countdown_job
                                            if pending is not None and pending.id == qid:
                                                self.active_countdown_job = None
                                            self._gesture_preview_pause.clear()
                                            with self._countdown_person_lock:
                                                if (self._countdown_person_check and
                                                        self._countdown_person_check.get("queue_id") == qid):
                                                    self._countdown_person_check = None
                                            last_phase.pop(qid, None)
                                            gesture_gate.pop(qid, None)

                                    def capture_watchdog(qid=queue_id, wd=watchdog):
                                        """超时未完成就落 failed 释放拍摄位（幂等）。"""
                                        waited = 0.0
                                        while not wd["done"] and waited < CAPTURE_HARD_TIMEOUT_S:
                                            time.sleep(1.0)
                                            waited += 1.0
                                        if wd["done"]:
                                            return
                                        log("拍摄超时 %ds，强制释放拍摄位：%s" % (
                                            int(CAPTURE_HARD_TIMEOUT_S), qid[:12]))
                                        try:
                                            self.auto_flow.queue.mark_failed(
                                                qid, reason="capture_timeout")
                                        except Exception as exc:
                                            log("强制释放失败（%s）：%s: %s" % (
                                                qid[:12], type(exc).__name__, exc))
                                        # 队列条目已释放；把作业也标失败，
                                        # 否则大屏会一直显示一个永远不动的"处理中"。
                                        job = self.queue_jobs.get(qid)
                                        if job is not None and job.stage != "done":
                                            job.ok = False
                                            job.stage = "failed"
                                            job.error = "capture_timeout"

                                    threading.Thread(
                                        target=capture_worker,
                                        name="auto-capture-%s" % queue_id[:8],
                                        daemon=True,
                                    ).start()
                                    threading.Thread(
                                        target=capture_watchdog,
                                        name="auto-capture-wd-%s" % queue_id[:8],
                                        daemon=True,
                                    ).start()

                                if tick.phase == "cancelled":
                                    # 必须把条目退掉：倒计时取消只改会话内部相位，
                                    # 条目会留在 ready 占着唯一拍摄位（调度器只对
                                    # called 过期、从不碰 ready），后面的队列就永久
                                    # 堵死。现场复现过：镜头前没人 → 取消 → 手机端
                                    # 永远停在"拍摄位已就绪"。
                                    # 记录真实原因码：第二次 tick 会打
                                    # countdown_noop，那只是"已离开倒计时"的复述。
                                    reason = (tick.reason_code or tick.reason or "").strip()
                                    log("倒计时取消（%s）：%s · %s，释放拍摄位" % (
                                        queue_id[:12], reason, tick.reason or ""))
                                    try:
                                        if reason in ("countdown_camera_invalid", "countdown_person_condition_invalid"):
                                            self.auto_flow.queue.mark_failed(queue_id, reason=reason)
                                        else:
                                            self.auto_flow.queue.mark_cancelled(
                                                queue_id, reason=reason or "countdown_cancelled")
                                    except Exception as exc:
                                        log("取消后释放拍摄位失败（%s）：%s: %s" % (
                                            queue_id[:12], type(exc).__name__, exc))
                                    active_countdowns.pop(queue_id, None)
                                    self._gesture_preview_pause.clear()
                                    with self._countdown_person_lock:
                                        if (self._countdown_person_check and
                                                self._countdown_person_check.get("queue_id") == queue_id):
                                            self._countdown_person_check = None
                                    last_phase.pop(queue_id, None)
                                    finished.add(queue_id)

                                elif tick.phase == "completed":
                                    log("倒计时结束：%s → completed" % queue_id[:12])
                                    active_countdowns.pop(queue_id, None)
                                    self._gesture_preview_pause.clear()
                                    with self._countdown_person_lock:
                                        if (self._countdown_person_check and
                                                self._countdown_person_check.get("queue_id") == queue_id):
                                            self._countdown_person_check = None
                                    last_phase.pop(queue_id, None)
                                    finished.add(queue_id)

                            except Exception as exc:
                                log("countdown.tick 失败（%s）：%s: %s" % (
                                    queue_id[:12], type(exc).__name__, exc))
                                active_countdowns.pop(queue_id, None)
                                self._gesture_preview_pause.clear()
                                with self._countdown_person_lock:
                                    if (self._countdown_person_check and
                                            self._countdown_person_check.get("queue_id") == queue_id):
                                        self._countdown_person_check = None

                    # 清理：已不在 ready 的登记（取消/过期）
                    for stale in [q for q in active_countdowns if q not in ready_ids]:
                        active_countdowns.pop(stale, None)
                        last_phase.pop(stale, None)

                    # 排队阶段不打开相机；用户点开始后进入 ready，才启动预览、
                    # 手势识别和倒计时。拍摄落盘后 processing 阶段也关闭相机。
                    # 门开关由状态变化触发（set_gate 内部判等，无变化是快路径）；
                    # 开着时 snapshot 拍照与 presence 在场检测照常工作。
                    # 注意门只管 preview 抓帧线程：有人用「现场直接拍摄」按快门时
                    # snapshot 自己会 open 相机，不受门影响。
                    required_statuses = ("ready", "capturing")
                    busy = any(e.get("status") in required_statuses
                               for e in snapshot.get("entries", []))
                    busy = busy or time.monotonic() < getattr(self, "_camera_starting_until", 0.0)
                    self.preview_hub.set_gate(busy)

                    consecutive_errors = 0

                except Exception as exc:
                    consecutive_errors += 1
                    backoff_seconds = min(2 ** consecutive_errors, 30)
                    backoff_until = time.time() + backoff_seconds
                    log("队列 tick 循环出错（第 %d 次）：%s；退避 %ds" % (
                        consecutive_errors, exc, backoff_seconds))

        self._auto_flow_tick_thread = threading.Thread(
            target=tick_loop, name="auto-flow-tick", daemon=True)
        self._auto_flow_tick_thread.start()


    def auto_flow_services(self):
        """返回队列子系统（AutoFlowServices）或 None。

        None 表示 BOOTH_QUEUE_ENABLED 未开启（或注入前）：三条队列路由按
        "未部署"处理（404 not_found，与未知路径同形）。
        """
        return self.auto_flow

    @staticmethod
    def _ep_base() -> dict:
        base = {"ep": None, "ep_threads": None, "ep_fallback": None,
                "providers": [], "ep_checked_at": None}
        base.update({key: None for key in Booth.HEALTH_FIELDS})
        # 抠像模型走 CPU，不在 EP 那套口径里，单独列出来 ——
        # 否则面板上只能看到分割模型，看不到「成片的边到底是谁给的」。
        base.update({"matte_model": None, "matte_device": None, "matte_load_ms": None})
        return base

    def note_ep(self, payload: dict | None) -> None:
        """把「到底有没有拿到 AI 核」记在 Booth 上 —— 大屏/操作员/预检读的就是这份。"""
        if not payload:
            return
        if "ep" not in payload:
            # worker 连模型都没加载起来（例如模型文件不在）：这不是「还没查到」，
            # 是明确的失败。必须把原因留下来，否则面板上只剩一个说不清的 None。
            if payload.get("ok") is False or payload.get("error"):
                self.ep_info = self._ep_base()
                self.ep_info.update({
                    "ep": False,
                    "ep_fallback": str(payload.get("error") or "worker 未就绪"),
                    "ep_checked_at": _now(),
                    "ok": False,
                })
            return
        # 从上一份开始改，而不是重建：seg/det 响应里没有 HEALTH_FIELDS，
        # 重建会把上一轮 health 报过的已知值冲成 None（原来那几行的 `or` 就是在防这个）。
        info = dict(self.ep_info)
        info.update({
            "ep": bool(payload.get("ep")),
            "ep_threads": payload.get("ep_threads", info.get("ep_threads")),
            "ep_fallback": payload.get("ep_fallback"),
            "providers": payload.get("providers") or [],
            "ep_checked_at": _now(),
            "ok": True,
        })
        for key in ("matte_model", "matte_device", "matte_load_ms", *self.HEALTH_FIELDS):
            if key in payload:
                info[key] = payload[key]
        self.ep_info = info
        if self.ep_info["ep"]:
            self.ep_threads = self.ep_info["ep_threads"]

    # ---- 启动预热 ------------------------------------------------------
    def preload(self) -> None:
        def work() -> None:
            try:
                health = self.infer.health()
                self._preloaded = True
                self.note_ep(health)
                log("worker 就绪：ep=%s threads=%s load=%sms providers=%s"
                    % (health.get("ep"), health.get("ep_threads"), health.get("load_ms"),
                       health.get("providers")))
                if not health.get("ep"):
                    log("警告：没拿到 AI 核，降级为 CPU（原因：%s）" % health.get("ep_fallback"))
                self.warmup()
            except Exception as exc:
                self._preload_error = str(exc)
                log("worker 预热失败：%s" % exc)

        thread = threading.Thread(target=work, name="preload", daemon=True)
        thread.start()

    def warmup(self) -> None:
        """用一张合成图真跑一遍分割与检测。

        模型冷启动很贵（实测首帧 ~1.2s，热身后 ~205ms），预热的意义是不让第一个
        参会者替所有人付这笔钱。预热失败不算故障，只记录。
        """
        import tempfile

        from PIL import Image

        path = Path(tempfile.gettempdir()) / "booth-warmup.jpg"
        canvas = Image.new("RGB", (960, 720), (26, 24, 22))
        for x in range(0, 960, 40):
            for y in range(0, 720, 40):
                canvas.putpixel((x, y), (58, 54, 48))
        canvas.save(path, "JPEG", quality=80)
        started = time.perf_counter()
        try:
            result = self.infer.seg(str(path), prompts=["person"], conf=0.25,
                                    mask_max_side=320, timeout=10.0)
            log("预热完成：seg %.0f ms（ep=%s，%d 人）"
                % (result.get("infer_ms") or 0, result.get("ep"), result.get("person_count", 0)))
        except Exception as exc:
            log("预热推理失败（不影响出图，仍会在首次拍摄时重试）：%s" % exc)
        finally:
            # 预热失败也要解除屏障，让正式拍摄路径自行重试并如实降级，
            # 不把闸门永久卡在等待状态。
            self._inference_ready.set()
        # det 预热已移除：K3 上 det 超时，改用 seg 的 person 检测
        self.counters["warmup_ms"] = round((time.perf_counter() - started) * 1000, 1)

    def overlay(self) -> dict:
        """大屏的叠加层。

        优先给「刚刚这一张」；如果这次进程还没拍过（刚重启、或者被别的进程写过记录），
        就退回**最近一张成片**的真实读数 —— 数字仍然全部来自 worker 当时的回报，
        只是它属于上一张而已，所以带上 restored 让大屏把话说清楚。
        """
        # 只有带令牌的记录才是"够得着"的：没有令牌的老记录谁也打不开
        records = [r for r in self.store.all()
                   if r.token and not r.extra.get("tombstone")
                   and not self.store.is_expired(r)
                   and self.store.thumb_path(r.id).is_file()]
        # 本次的读数只在「那张成片还在」时才算数：参会者刚把照片删掉的话，
        # 大屏再指着一条已不存在的记录，四个指标就永远停在「—」。
        if self.last_overlay and self.last_overlay.get("record_id") in {r.id for r in records}:
            return self.last_overlay
        if not records:
            return {"ok": False, "empty": True}
        last = records[-1]
        mask_applied = bool(last.extra.get("mask_applied"))
        return {
            "ok": True, "restored": True, "record_id": last.id, "at": last.created_at,
            "infer_ms": last.infer_ms, "ep": bool(last.ep),
            "ep_threads": last.extra.get("ep_threads"),
            "person_count": last.person_count, "degraded": bool(last.degraded),
            "degraded_reason": last.extra.get("degraded_reason"),
            "total_ms": last.extra.get("total_ms"),
            "persons": [], "mask_applied": mask_applied,
            "mask_url": "/api/mask/%s.png" % last.id if mask_applied else None,
            "matte_applied": bool(last.extra.get("matte_applied")),
            "matte_ms": last.extra.get("matte_ms"),
            "matte_source": last.extra.get("matte_source"),
            "alpha_url": "/api/mask/%s-alpha.png" % last.id
                if last.extra.get("matte_applied") else None,
            "cutout_url": "/api/mask/%s-cutout.png" % last.id
                if last.extra.get("matte_applied") else None,
            "width": None, "height": None, "providers": [],
        }


    def next_style(self, requested: str | None) -> str:
        """这一张用哪个风格。随机卡片从当前启用的 AIGC 风格中抽取。"""
        available = set(styles())
        if self._aigc_enabled():
            available.update(item["id"] for item in self.style_catalog.snapshot()["styles"]
                             if item.get("enabled"))
            if requested == "random-style":
                aigc_styles = sorted(item["id"] for item in self.style_catalog.snapshot()["styles"]
                                     if item.get("enabled") and item["id"] != "main-film-chip")
                if aigc_styles:
                    return secrets.choice(aigc_styles)
        if requested in available:
            return requested
        if not ROTATE_STYLE:
            return default_style()
        with self.style_catalog._lock:
            choices = tuple(sorted(available))
            style = choices[self._style_seq % len(choices)]
            self._style_seq += 1
            return style

    def aigc_style(self, style: str) -> dict | None:
        for item in self.style_catalog.snapshot()["styles"]:
            if item.get("id") == style and item.get("enabled"):
                return item
        return None

    def _aigc_enabled(self) -> bool:
        from aigc_client import enabled

        return enabled(getattr(self, "aigc_channels", None))

    def style_options(self) -> list[dict]:
        options = [{"name": "natural", "label": "原片", "description": "保留现场原色"}]
        if self._aigc_enabled():
            options.extend({"name": item["id"], "label": item["title"],
                            "description": item.get("description", "")}
                           for item in self.style_catalog.snapshot()["styles"]
                           if item.get("enabled"))
        return options

    # ---- 服务端配置（公网取图地址）------------------------------------
    def public_config(self) -> dict:
        """面板要的几个字段：现在生效的是哪个地址、从哪儿来的、是不是 https。"""
        configured = str(read_settings().get("public_base") or "").strip()
        effective = public_base()
        return {
            "ok": True,
            "public_base": effective,
            "configured": configured,
            "lan_base": lan_base(),
            "source": public_base_source(),
            "secure": effective.startswith("https://"),
            "env_host": str(os.environ.get("BOOTH_PUBLIC_HOST") or ""),
            "qr_cached": len(list((self.store.root / "qr").glob("*.png"))),
            "aigc_catalog_version": self.style_catalog.snapshot().get("version", 1),
            "presentation_mode": self.presentation_mode,
            "handoff_enabled": self.presentation_mode == "handoff",
            "aigc_handoff_delay_seconds": self.aigc_handoff_delay_seconds,
        }

    def set_presentation_config(self, mode: str | None = None,
                                handoff_enabled: bool | None = None) -> dict:
        """保存现场展示模式；handoff 固定在 AIGC 开始后 8 秒交回预览。"""
        if handoff_enabled is not None:
            normalized = "handoff" if bool(handoff_enabled) else "full"
        else:
            normalized = str(mode or "").strip().lower()
        if normalized not in ("full", "handoff"):
            raise ValueError("展示模式只能选择完整展示或 8 秒后后台生成")
        data = dict(read_settings())
        data["presentation_mode"] = normalized
        data["aigc_handoff_delay_seconds"] = DEFAULT_AIGC_HANDOFF_DELAY_SECONDS
        write_settings(data)
        self.presentation_mode = normalized
        self.aigc_handoff_delay_seconds = DEFAULT_AIGC_HANDOFF_DELAY_SECONDS
        log("现场展示模式 -> %s（AIGC 后 %.0f 秒交回预览）" % (
            "后台生成" if normalized == "handoff" else "完整展示",
            self.aigc_handoff_delay_seconds,
        ))
        return self.public_config()

    def aigc_catalog(self) -> dict:
        """操作台读取的完整目录；不会包含 API 密钥或外部服务凭据。"""
        return self.style_catalog.snapshot()

    def public_aigc_styles(self) -> dict:
        """参与者只看到启用风格的公开字段。"""
        catalog = self.style_catalog.snapshot()
        return {"ok": True, "version": catalog.get("version", 1),
                "styles": self.style_catalog.public()}

    def public_status(self) -> dict:
        """手机报名页只需要公开功能开关，不暴露设备、存储或现场状态。"""
        return {
            "ok": True,
            "queue_enabled": self.auto_flow_services() is not None,
            "aigc_enabled": (
                self._aigc_enabled()
            ),
            "presentation_mode": self.presentation_mode,
            "aigc_handoff_delay_seconds": self.aigc_handoff_delay_seconds,
        }

    def save_aigc_catalog(self, styles_payload) -> dict:
        if not isinstance(styles_payload, list):
            raise CatalogError("styles 必须是数组")
        with self.style_catalog._lock:
            self._guard_aigc_styles(styles_payload)
            return {"ok": True, **self.style_catalog.save(styles_payload)}

    def update_aigc_style(self, style_id: str | None, payload: dict) -> dict:
        if not isinstance(payload, dict):
            raise CatalogError("风格必须是对象")
        with self.style_catalog._lock:
            if style_id:
                candidate = self.style_catalog.snapshot()["styles"]
                for item in candidate:
                    if item["id"] == style_id:
                        item.update(payload)
                        item["id"] = style_id
                self._guard_aigc_styles(candidate)
            return {"ok": True, **self.style_catalog.upsert(payload, style_id)}

    def rollback_aigc_style(self, style_id: str, revision=None) -> dict:
        with self.style_catalog._lock:
            candidate = self.style_catalog.snapshot()["styles"]
            item = next((entry for entry in candidate if entry["id"] == style_id), None)
            if item is not None:
                history = item.get("history") or []
                target = (next((entry for entry in reversed(history)
                                if int(entry.get("revision", 0)) == int(revision)), None)
                          if revision is not None else history[-1] if history else None)
                if target is not None:
                    item["enabled"] = target.get("enabled", item.get("enabled"))
                self._guard_aigc_styles(candidate)
            return {"ok": True, **self.style_catalog.rollback(style_id, revision)}

    def _busy_aigc_styles(self) -> set[str]:
        busy = self.jobs.active_styles()
        services = self.auto_flow_services()
        if services is not None:
            busy.update(item.get("style_id") for item in services.queue.snapshot().get("entries", [])
                        if item.get("status") in ("queued", "called", "ready", "capturing", "processing"))
        return busy

    def _guard_aigc_styles(self, candidate: list[dict]) -> None:
        available = {item.get("id") for item in candidate
                     if isinstance(item, dict) and item.get("enabled", True)}
        current = {item["id"] for item in self.style_catalog.snapshot()["styles"]
                   if item.get("enabled")}
        if (current - available) & self._busy_aigc_styles():
            raise CatalogOperationError("该风格仍有排队或处理中的任务，请完成后再删除或停用",
                                        "STYLE_IN_USE", 409)

    def delete_aigc_style(self, style_id: str) -> dict:
        with self.style_catalog._lock:
            current = self.style_catalog.snapshot()
            if not any(item["id"] == style_id for item in current["styles"]):
                raise CatalogOperationError("找不到要删除的风格", "style_not_found", 404)
            if style_id in self._busy_aigc_styles():
                raise CatalogOperationError("该风格仍有排队或处理中的任务，请完成后再删除",
                                            "STYLE_IN_USE", 409)
            return {"ok": True, **self.style_catalog.delete(style_id)}

    def set_public_config(self, value: str) -> dict:
        """保存公网取图地址；传空字符串 = 清掉，回落到局域网地址。

        改完**必须**把二维码缓存清掉：二维码是按旧地址画好的 PNG，文件在就直读
        缓存（见 _qr）。不清的话，面板上写着新域名、发出去的二维码仍指向旧地址，
        而且全程不报错 —— 坑 25 的同一个病。
        """
        normalized = normalize_base(value)
        data = dict(read_settings())
        if normalized:
            data["public_base"] = normalized
        else:
            data.pop("public_base", None)
        write_settings(data)
        cleared = self.clear_qr_cache()
        log("公网取图地址 -> %s（来源 panel），清掉 %d 张二维码缓存"
            % (normalized or "（空，回落局域网）", cleared))
        result = self.public_config()
        result["qr_cleared"] = cleared
        return result

    def clear_qr_cache(self) -> int:
        removed = 0
        folder = self.store.root / "qr"
        if folder.is_dir():
            for png in sorted(folder.glob("*.png")):
                try:
                    png.unlink()
                    removed += 1
                except Exception:
                    pass
        return removed

    def photo_meta(self, token: str) -> dict | None:
        """取图页要的那一点点信息（编号 / EP 徽章）。不认识这个令牌就返回 None。

        编号由这里给出，**不是从 URL 里读的** —— 取图页只知道令牌，不知道编号。

        公网只能看到这一个端点，所以它**不能**顺手把编号清单、相机状态、
        磁盘余量带出去 —— /api/state 里那些东西一公开，等于把"现场还有哪些照片"
        直接摊给外面（编号还是连号的，见 docs/07）。
        """
        record = self.store.by_token(token)
        if record is None or record.extra.get("tombstone"):
            return None
        if not self.store.photo_path(record.id).is_file():
            return None
        print_available = False
        port = getattr(self, "print_port", None)
        if port is not None:
            try:
                print_available = port.status().status == "ok"
            except Exception:
                pass
        return {"ok": True, "id": record.id, "ep": bool(record.ep),
                "degraded": bool(record.degraded), "style": record.style,
                "portrait_mode": (record.extra or {}).get("portrait_mode", "standard"),
                "ai_watermark": (record.extra or {}).get("ai_watermark", True),
                "ai_generated": bool((record.extra or {}).get("ai_generated")),
                "aigc_channel_name": (record.extra or {}).get("aigc_channel_name"),
                "aigc_model": (record.extra or {}).get("aigc_model"),
                "at": record.created_at, "print_available": print_available,
                "print_media_label": {"card-54x86": "54×86mm 卡片",
                                      "postcard-100x148": "100×148mm 照片"}.get(
                    os.environ.get("BOOTH_PRINT_LAYOUT", "").strip(), "照片"),
                "print_unavailable_reason": "" if print_available else "打印暂时不可用，请找现场工作人员。",
                "download_watermarked_available": True,
                "download_clean_available": self.store.clean_photo_path(record.id).is_file(),
                "public_print_once": True}

    # ---- 状态 ----------------------------------------------------------
    def state(self) -> dict:
        camera_status = self.camera.status.__dict__.copy()
        infer_stats = self.infer.stats()

        def _queue_busy_count() -> int:
            """需要点亮的队列条目数（queued→processing；见 state 里的注释）。"""
            try:
                entries = self.auto_flow.queue.snapshot().get("entries") or []
            except Exception:
                return 0
            return sum(1 for e in entries
                       if e.get("status") in ("queued", "called", "ready",
                                              "capturing", "processing"))

        def _queue_slot() -> dict | None:
            """拍摄位上那一位：状态 + 叫号号码，竖屏据此区分「请上前」与「请比 OK」。

            只给状态和 display_number（叫号本来就要喊出来），不给 request_id 等私人字段。
            """
            try:
                entries = self.auto_flow.queue.snapshot().get("entries") or []
            except Exception:
                return None
            for status in ("capturing", "ready", "called"):
                for e in entries:
                    if e.get("status") == status:
                        return {"status": status, "number": str(e.get("display_number") or "")}
            return None

        try:
            usage = os.statvfs(str(self.store.root))
            disk = {"total_gb": round(usage.f_blocks * usage.f_frsize / 1e9, 1),
                    "free_gb": round(usage.f_bavail * usage.f_frsize / 1e9, 1)}
        except Exception:
            disk = {"total_gb": None, "free_gb": None}
        # 只列「成片还在盘上」的记录：参会者删掉的照片不该再出现在大屏/操作员面板上
        # 只有带令牌的记录才是"够得着"的：没有令牌的老记录谁也打不开
        records = [r for r in self.store.all()
                   if r.token and not r.extra.get("tombstone")
                   and self.store.thumb_path(r.id).is_file()]
        recent = records[-8:][::-1]
        measured = [r.infer_ms for r in records[-20:] if r.infer_ms]
        latest_job = self.jobs.latest()
        latest_snapshot = latest_job.snapshot() if latest_job is not None else None
        handoff_notice = None
        if latest_snapshot is not None and latest_snapshot.get("handoff_ready"):
            handoff_notice = {
                "job": latest_snapshot.get("job"),
                "style": latest_snapshot.get("style"),
                "message": "AIGC 已转后台生成",
                "detail": "请关注手机页面，生成完成后回来取图。",
            }
        active_job = latest_job
        if latest_snapshot is not None and latest_snapshot.get("handoff_ready"):
            # handoff 作业继续在后台跑，但不再占用大屏前台。
            active_job = None
        if active_job is None or active_job.stage in ("done", "failed"):
            active_job = self.active_countdown_job

        queue_entries = []
        if self.auto_flow is not None:
            try:
                queue_entries = self.auto_flow.queue.snapshot().get("entries") or []
            except Exception:
                queue_entries = []
        queue_capture_active = sum(
            1 for entry in queue_entries
            if entry.get("status") in ("queued", "called", "ready", "capturing")
        )
        queue_camera_active = sum(
            1 for entry in queue_entries
            if entry.get("status") in ("ready", "capturing")
        )
        queue_processing = sum(
            1 for entry in queue_entries if entry.get("status") == "processing"
        )
        return {
            "build": BUILD,
            "at": _now(),
            "uptime_s": round(time.time() - self.started_at, 1),
            "recent": [{"id": r.id, "token": r.token, "style": r.style, "ep": r.ep,
                        "portrait_mode": (r.extra or {}).get("portrait_mode", "standard"),
                        "ai_watermark": (r.extra or {}).get("ai_watermark", True),
                        "ai_generated": bool((r.extra or {}).get("ai_generated")),
                        "aigc_channel_id": (r.extra or {}).get("aigc_channel_id"),
                        "aigc_channel_name": (r.extra or {}).get("aigc_channel_name"),
                        "aigc_model": (r.extra or {}).get("aigc_model"),
                        "aigc_elapsed_ms": (r.extra or {}).get("aigc_elapsed_ms"),
                        "regenerate_available": self.store.capture_path(r.id).is_file() and not self.store.is_expired(r),
                        "infer_ms": r.infer_ms, "degraded": r.degraded,
                        "person_count": r.person_count,
                        "clean_available": self.store.clean_photo_path(r.id).is_file(),
                        "thumb": "/api/thumb/%s.jpg" % r.token} for r in recent],
            "avg_infer_ms": round(sum(measured) / len(measured), 1) if measured else None,
            "last_record": recent[0].id if recent else None,
            # 取图链接认令牌、不认编号（编号连号、可枚举）。操作员面板要画最近
            # 一张的二维码，就得拿到那一个令牌 —— 别再让它拿编号去拼 URL。
            "last_token": recent[0].token if recent else None,
            "active_job": active_job.snapshot() if active_job else None,
            "handoff_notice": handoff_notice,
            # 需要「点亮屏幕/开摄像头」的条目数：queued→processing。不含
            # ready_for_pickup（人拍完了，只剩出片与取图）。前端据此判断
            # 「有没有人排队」。只给计数不给条目（条目含 request_id 私人字段）。
            "queue_active": _queue_busy_count()
                            if self.auto_flow is not None else 0,
            "queue_capture_active": queue_capture_active,
            # 只有用户点击「开始拍摄」进入 ready 后才开相机；排队和叫号阶段
            # 只显示文字，不向浏览器提供实时视频流。
            "queue_camera_active": queue_camera_active,
            "queue_processing": queue_processing,
            "presentation_mode": self.presentation_mode,
            "aigc_handoff_delay_seconds": self.aigc_handoff_delay_seconds,
            "queue_slot": _queue_slot() if self.auto_flow is not None else None,
            # 倒计时秒数以现场配置为准（K3 上是 8 秒），页面别再写死
            "countdown_seconds": (int(self.auto_flow.countdown.countdown_seconds)
                                  if self.auto_flow is not None and self.auto_flow.countdown is not None
                                  else None),
            # 实时手势识别框（yolo 引擎）：竖屏 canvas 用它绘人形框 + OK 框。
            # 超过 5 秒未更新视为过期（手势闸门不活动时不更新），前端按 at 过滤。
            "gesture_obs": self.last_gesture_obs,
            "camera": camera_status,
            "preview": self.preview_hub.stats(),
            "presence": self.presence.snapshot(),
            # mock 源细分：static_photo=True 表示用固定照片当联调源 ——
            # 画面有真实内容，界面应照常显示（不是黑底+文字提示）。
            # camera_status 是 __dict__.copy() 出来的 dict，不是对象。
            "camera_static_photo": bool((camera_status or {}).get("static_photo")),
            "infer": {**infer_stats, **self.ep_info, "preloaded": self._preloaded,
                      "preload_error": self._preload_error},
            "consent": self.consent.stats(),
            "photos": self.store.peek_count(),
            "counters": dict(self.counters),
            "disk": disk,
            "styles": self.style_options(),
            "default_style": default_style(),
            "aigc_enabled": (os.environ.get("AIGC_ENABLED", "0") == "1"
                             and os.environ.get("AIGC_BACKEND", "disabled") == "public_api"),
            "backgrounds": self._backgrounds(),
            "default_background": self._default_background(),
            "background_random": self._background_random(),
            "link_base": public_base(),
            "share": {"public_base": public_base(), "lan_base": lan_base(),
                      "source": public_base_source(),
                      "secure": public_base().startswith("https://")},
            "queue": 0,
        }

    @staticmethod
    def _backgrounds() -> list:
        """可用的底图清单。读不到就返回空列表 —— 状态页少一行，好过整个 /api/state 挂掉。"""
        try:
            import importlib

            return importlib.import_module("render.keyvisual").catalog()
        except Exception as exc:  # noqa: BLE001
            log("底图清单读取失败（不影响出图）：%s" % exc)
            return []

    @staticmethod
    def _background_random() -> bool:
        """没点名底图时是否随机挑一套（BOOTH_KV_RANDOM）。判断权在 render/keyvisual.py。"""
        try:
            import importlib

            return bool(importlib.import_module("render.keyvisual").RANDOM)
        except Exception as exc:  # noqa: BLE001
            log("随机开关读取失败，按「不随机」处理：%s" % exc)
            return False

    @staticmethod
    def _default_background() -> str:
        """默认底图名 —— 以注册表为准，漏洞不会再出现第二次。

        命名权归 render/keyvisual.py：底图换一套，这里自动跟着变。
        """
        try:
            import importlib

            return importlib.import_module("render.keyvisual").DEFAULT_KEYVISUAL
        except Exception as exc:  # noqa: BLE001
            log("默认底图名读取失败，退回常量 %s：%s" % (BACKGROUND_DEFAULT, exc))
            return BACKGROUND_DEFAULT

    # ---- 合成 ----------------------------------------------------------
    @staticmethod
    def _aigc_request_size(spec: dict, orientation: str) -> str:
        """AIGC 送图画布尺寸：风格里与版式同向的第一个尺寸。"""
        sizes = [str(item) for item in (spec.get("sizes") or [])]
        preferred = [item for item in sizes
                     if (int(item.split("x")[0]) >= int(item.split("x")[1]))
                     == (orientation == "wide")]
        return str((preferred or sizes or
                    [os.environ.get("AIGC_DEFAULT_SIZE", "1024x1536")])[0])

    def _plate_subject(self, photo, alpha, spec: dict, orientation: str, size) -> dict:
        """AIGC 成片里人大概在哪：按送图摆位（plate guide）估计，给顶部品牌字避让用。

        样张实测（2026-10-14）：标准模式头顶基本落在送图位置，Q 版大头像最多上移约
        30px，所以多留 48px。不为此再跑一次推理 —— worker 有 seg / 手势两种独占模式，
        排版时多调一次可能把正在等 OK 的手势会话切掉。
        """
        import importlib

        from aigc_client import portrait_guide

        kv = importlib.import_module("render.keyvisual")
        try:
            guide = portrait_guide(photo, alpha, self._aigc_request_size(spec, orientation))
        except Exception as exc:  # noqa: BLE001
            log("品牌字避让：送图摆位不可用，按样稿尺寸：%s" % exc)
            return {}
        return {"subject_mask": kv._cover(guide, size), "subject_margin": 48}

    def _render(self, job: Job, photo, mask, style: str, record_id: str, meta: dict,
                alpha=None, aigc_scene=None):
        import importlib

        import numpy as np
        from PIL import Image

        stylize = importlib.import_module("render.stylize")
        card = importlib.import_module("render.card")

        started = time.perf_counter()
        aigc_spec = self.aigc_style(style)
        style_label = (aigc_spec or {}).get("title") or getattr(stylize, "STYLE_LABELS", {}).get(style, style)
        seed = int(meta.get("seed", 7))
        card_name = meta.get("card") or os.environ.get("BOOTH_CARD", "keyvisual")
        mock = job.source == "camera" and not self.camera.status.real
        if meta.get("upload_mode") == "original":
            kv = importlib.import_module("render.keyvisual")
            output_size = kv.WIDE if meta.get("orientation") == "wide" else kv.VERTICAL
            final = kv.compose_generated(photo, record_id=record_id,
                                         name=meta.get("name", ""), handle=meta.get("handle", ""),
                                         size=output_size)
            job.step("render", "原图排版", (time.perf_counter() - started) * 1000)
            return final, None, "原图", photo.copy().convert("RGB")

        # 官方主视觉背景板（默认）：把人贴进官方预留的人像窗
        if card_name == "keyvisual":
            kv = importlib.import_module("render.keyvisual")
            # 优先走抠像：只把人按 alpha 贴进官方预留窗（背景不进来、边是软的）。
            # 抠像不可用就退回「整幅取景」，再不行还有外面那层 L0 —— 出图不能断。
            cutout = None
            cutout_note = ""
            if alpha is not None:
                try:
                    cutout_style = "natural" if aigc_spec is not None else style
                    cutout = stylize.render_cutout(cutout_style, photo, alpha,
                                                   size=kv.PORTRAIT_SIZE, seed=seed)
                except Exception as exc:
                    log("抠像合成失败，退回整幅取景：%s" % exc)
                    cutout = None
                if cutout is None:
                    # 走到这儿有两种：alpha 是空的，或者**alpha 有内容但全在取景框外**
                    # （只有桌角一小块皮肤时会出现）。后者会静默交出一张没有人的成片，
                    # 所以这里必须留下痕迹 —— 操作员看得到，事后也查得到。
                    cutout_note = " · 整幅兜底"
                    log("抠像为空（%d 人），退回整幅取景 —— 成片里会有整幅取景，不是干净抠像"
                        % job.person_count)
            orientation = "wide" if meta.get("orientation") == "wide" else "vertical"
            output_size = kv.WIDE if orientation == "wide" else kv.VERTICAL
            if aigc_scene is not None:
                clean = kv.compose_generated(
                    aigc_scene, record_id=record_id, name=meta.get("name", ""),
                    handle=meta.get("handle", ""), size=output_size,
                    mock=mock, include_event_text=False)
                final = kv._draw_generated_text(clean.copy(), record_id=record_id,
                                        name=meta.get("name", ""),
                                        handle=meta.get("handle", ""))
                job.step("render", "合成成片", (time.perf_counter() - started) * 1000,
                         "%s / AIGC 动态简约背景" % style_label)
                return final, None, style_label, clean
            elif aigc_spec is not None:
                style_label = "原片（AIGC 未完成）"
                clean = kv.compose_generated(
                    photo, record_id=record_id, name=meta.get("name", ""),
                    handle=meta.get("handle", ""), size=output_size,
                    mock=mock, include_event_text=False)
                # 原片就是抠像那张图：alpha 按同样的 cover 裁到成片坐标，就是人像的准确位置
                subject = ({"subject_mask": kv._cover(alpha.convert("L").resize(photo.size),
                                                      output_size)}
                           if alpha is not None else {})
                final = kv._draw_generated_text(clean.copy(), record_id=record_id,
                                        name=meta.get("name", ""),
                                        handle=meta.get("handle", ""), **subject)
                job.step("render", "合成成片", (time.perf_counter() - started) * 1000,
                         "%s / 现场原片回退" % style_label)
                return final, None, style_label, clean
            else:
                portrait = (cutout[0] if cutout else
                            stylize.render(style, photo, mask, size=kv.PORTRAIT_SIZE, seed=seed))
            shared = dict(record_id=record_id, name=meta.get("name", ""),
                          mock=mock,
                          handle=meta.get("handle", ""), style_label=style_label,
                          mnemonic=meta.get("mnemonic"), ep=job.ep,
                          ep_threads=job.ep_threads, infer_ms=job.infer_ms,
                          person_count=job.person_count, seed=seed)
            # 成片就是官方主视觉这一张（16:9）：人贴在官方预留的人像窗里，其余版面不动。
            # 早期还额外出一张 4:5 竖版，下半部分那块信息区现场判定多余，已去掉。
            # pick() 而不是 resolve()：没点名时**每张随机**挑一套底图
            # （BOOTH_KV_RANDOM=1，见 render/keyvisual.pick）。点名了的按点名的来。
            kv_name = kv.pick(meta.get("background"))
            clean = kv.compose(portrait, cutout=cutout, size=output_size,
                              natural=(style == "natural"),
                              keyvisual=kv_name, include_event_text=False, **shared)
            final = kv._draw_local_text(clean.copy(), record_id=record_id, name=meta.get("name", ""),
                                handle=meta.get("handle", ""), kv_name=kv_name,
                                subject_mask=(kv.cutout_mask(cutout, kv_name, output_size)
                                              if cutout is not None else None))
            wide = None
            job.step("render", "合成成片", (time.perf_counter() - started) * 1000,
                     "%s / %s%s" % (style_label, kv.KEYVISUALS[kv_name]["label"],
                                    cutout_note))
            return final, wide, style_label, clean

        portrait = stylize.render(style, photo, mask, size=(1000, 1250), seed=seed)
        rng = np.random.default_rng(seed)
        card_name = getattr(card, "CARD_NAMES", ("blueprint",))[
            int(rng.integers(0, len(getattr(card, "CARD_NAMES", ("blueprint",)))))]

        final = card.compose(
            record_id, portrait, style=style, name=meta.get("name", ""),
            handle=meta.get("handle", ""), card=card_name,
            ep=job.ep, ep_threads=job.ep_threads, infer_ms=job.infer_ms,
            person_count=job.person_count, mnemonic=meta.get("mnemonic"),
            size=(1000, 1250), seed=seed,
        )
        wide = None
        try:
            wide = card.compose(
                record_id, portrait, style=style, name=meta.get("name", ""),
                handle=meta.get("handle", ""), card=card_name,
                ep=job.ep, ep_threads=job.ep_threads, infer_ms=job.infer_ms,
                person_count=job.person_count, mnemonic=meta.get("mnemonic"),
                size=(1600, 1000), seed=seed + 1,
            )
        except Exception as exc:  # 横版是加分项，失败不影响交付
            log("横版卡片合成失败（不影响出图）：%s" % exc)
        job.step("render", "合成成片", (time.perf_counter() - started) * 1000,
                 "%s / %s" % (style_label, card_name))
        return final, wide, style_label, final.copy()

    def _aigc_scene(self, style: str, photo, alpha, orientation: str = "vertical",
                    portrait_mode: str = "standard", result_meta: dict | None = None):
        from aigc_client import AIGCError, edit_portrait, enabled

        spec = self.aigc_style(style)
        if spec is None:
            return None
        if alpha is None:
            raise AIGCError("人像抠像不可用", retryable=False)
        channels = getattr(self, "aigc_channels", None)
        if not enabled(channels):
            raise AIGCError("AIGC 尚未配置", retryable=False)
        orientation = "wide" if orientation == "wide" else "vertical"
        size = self._aigc_request_size(spec, orientation)
        prompt = build_portrait_prompt(spec, orientation, portrait_mode)
        started = time.perf_counter()
        generated = edit_portrait(photo, alpha, prompt=prompt,
                                  model=os.environ.get("AIGC_MODEL", "gpt-image-1"),
                                  size=size, channels=channels, result_meta=result_meta)
        self._aigc_generated_ms = round((time.perf_counter() - started) * 1000, 1)
        return generated

    def _run(self, job: Job, style: str, meta: dict, source_photo=None,
             source_raw: bytes | None = None) -> None:
        from PIL import Image
        from store import Record

        deadline = time.time() + 60.0
        record_id = self.store.next_id()
        token = self.store.new_token()
        job.record_id = record_id
        job.record_token = token
        job.style = style
        job.portrait_mode = meta.get("portrait_mode", "standard")
        job.ai_watermark = meta.get("ai_watermark", True)

        # 队列后台任务已经在倒计时结束时抓完终帧；此处不能再把它显示成
        # “正在拍摄”，否则手机和现场屏幕都会误导用户。普通直接拍摄仍保留原阶段。
        job.stage = "capturing" if source_photo is None else "processing"
        started = time.perf_counter()
        if source_photo is None:
            preview_hub = getattr(self, "preview_hub", None)
            raw = (preview_hub.latest_capture_jpeg(quality=95, max_age=0.75)
                   if preview_hub is not None else None)
            if raw is None:
                raw = self.camera.snapshot(warm=1)
                capture_note = self.camera.last_snapshot_note
                capture_detail = ""
            else:
                capture_note = ""
                capture_detail = "（倒计时终帧）"
        else:
            raw = source_raw
            if not raw:
                buffer = io.BytesIO()
                source_photo.save(buffer, "JPEG", quality=95)
                raw = buffer.getvalue()
            capture_note = ""
            capture_detail = ("（已固定的倒计时终帧）"
                              if meta.get("source") == "camera" else "（后台上传）")
            job.source = meta.get("source", "operator_upload")
        job.step("capture", "抓帧", (time.perf_counter() - started) * 1000,
                 "%d B%s" % (len(raw), ("（降级：%s）" % capture_note
                                         if capture_note else capture_detail)))
        if capture_note:
            # 拍照是降级抓的（换分辨率失败、退回了当前分辨率）—— 大屏要如实标出来，
            # 不能让「这张其实只拍到预览分辨率」这件事没人知道。
            job.degraded = True
            job.degraded_reason = "; ".join(
                part for part in (job.degraded_reason, "capture_fallback: %s" % capture_note)
                if part)
        capture_path = self.store.capture_path(record_id)
        capture_path.write_bytes(raw)
        job.capture_preview_url = "/api/capture-preview/%s.jpg" % token

        photo = (source_photo.copy().convert("RGB") if source_photo is not None
                 else Image.open(io.BytesIO(raw)).convert("RGB"))
        mask = None
        subject_mask = None
        alpha = None
        mask_result = None
        infer_error = None

        # ② 推理（L1；失败就走 L0，绝不让参会者空手离开）
        original_upload = meta.get("upload_mode") == "original"
        job.stage = "rendering" if original_upload else "inferring"
        started = time.perf_counter()
        try:
            mask_result = (None if original_upload else
                           self._infer_uploaded_portrait(photo, job) if source_photo is not None else
                           self.infer.seg(str(capture_path), prompts=["person"], conf=0.20,
                                          mask_max_side=640, matte_max_side=MATTE_MAX_SIDE,
                                          on_progress=lambda progress: self._publish_segmentation(job, progress),
                                          timeout=INFER_TIMEOUT_S + 8.0))
            if not original_upload and not mask_result.get("ok", True):
                raise RuntimeError(mask_result.get("error") or "人像处理失败")
        except Exception as exc:
            infer_error = str(exc)
            mask_result = None
            job.degraded = True
            job.degraded_reason = "infer_failed: %s" % exc
            job.step("infer", "推理失败 · 走 L0", (time.perf_counter() - started) * 1000, str(exc))
            self.counters["errors"] += 1
        else:
            if original_upload:
                job.step("infer", "原图无需人像分割", 0, "已跳过")
                mask_result = {}
            else:
                job.infer_ms = mask_result.get("infer_ms")
                self._publish_segmentation(job, mask_result)
                job.portrait_timing["worker_ms"] = mask_result.get("total_ms")
                job.portrait_timing["matte_model_ms"] = mask_result.get("matte_ms")
                self.note_ep(mask_result)
                job.ep = bool(mask_result.get("ep"))
                job.person_count = int(mask_result.get("person_count", 0))
                job.ep_threads = self.ep_threads
                if not job.ep:
                    self.counters["cpu_infers"] += 1
                slow = (job.infer_ms or 0) > INFER_TIMEOUT_S * 1000
                if mask_result.get("mask_b64"):
                    mask = Image.open(io.BytesIO(base64.b64decode(mask_result["mask_b64"]))).convert("L")
                    job.mask_applied = mask.getextrema()[1] > 0
                if mask_result.get("subject_mask_b64"):
                    subject_mask = Image.open(io.BytesIO(
                        base64.b64decode(mask_result["subject_mask_b64"]))).convert("L")
                # 精细 alpha 来自专门的抠像模型（MODNet），和上面那份「框内多边形掩码」
                # 不是一回事：掩码用来数人、画大屏的框；这份才是往成片上贴的边。
                if mask_result.get("matte_b64"):
                    alpha = Image.open(io.BytesIO(base64.b64decode(mask_result["matte_b64"]))).convert("L")
                    if source_photo is not None and alpha.size != photo.size:
                        alpha = alpha.resize(photo.size, Image.Resampling.BILINEAR)
                    if alpha.getextrema()[1] > 0:
                        job.matte_applied = True
                        job.matte_ms = mask_result.get("matte_ms")
                        job.matte_source = mask_result.get("matte_source")
                    else:
                        alpha = None
                if mask_result.get("matte_error"):
                    log("抠像不可用（退回整幅取景）：%s" % mask_result["matte_error"])
                if alpha is not None and (mask is not None or job.person_count < 1):
                    from aigc_client import constrain_alpha

                    guard_started = time.perf_counter()
                    alpha = constrain_alpha(alpha, subject_mask if subject_mask is not None else mask)
                    job.portrait_timing["subject_guard_ms"] = round(
                        (time.perf_counter() - guard_started) * 1000, 1)
                    if alpha.getbbox() is None:
                        alpha = None
                        job.matte_applied = False
                if slow:
                    job.degraded = True
                    job.degraded_reason = "infer_timeout>%.1fs" % INFER_TIMEOUT_S
                job.step("infer", "AI 核人像分割" if job.ep else "CPU 人像分割",
                         (time.perf_counter() - started) * 1000,
                         "%.0f ms · %d 人 · %s" % (job.infer_ms or 0, job.person_count,
                                                   "EP" if job.ep else "CPU"))
        self.last_overlay = {
            "job": job.id, "record_id": record_id, "at": _now(),
            "infer_ms": job.infer_ms, "ep": job.ep, "ep_threads": job.ep_threads,
            "person_count": job.person_count, "degraded": job.degraded,
            "degraded_reason": job.degraded_reason,
            "persons": (mask_result or {}).get("persons", []),
            "mask_applied": job.mask_applied,
            "mask_url": "/api/mask/%s.png" % record_id if job.mask_applied else None,
            "width": (mask_result or {}).get("width"), "height": (mask_result or {}).get("height"),
            "providers": (mask_result or {}).get("providers", []),
            "matte_applied": job.matte_applied, "matte_ms": job.matte_ms,
            "matte_source": job.matte_source,
            "alpha_url": "/api/mask/%s-alpha.png" % record_id if job.matte_applied else None,
            "cutout_url": job.cutout_url,
        }
        if mask is not None and job.mask_applied:
            mask.save(self.store.root / "masks" / ("%s.png" % record_id))
        if alpha is not None:
            alpha.save(self.store.root / "masks" / ("%s-alpha.png" % record_id))
            cutout = photo.convert("RGBA")
            cutout.putalpha(alpha.resize(photo.size))
            cutout.save(self.store.root / "masks" / ("%s-cutout.png" % record_id))
            job.cutout_url = "/api/mask/%s-cutout.png" % record_id
            self.last_overlay["alpha_url"] = "/api/mask/%s-alpha.png" % record_id
            self.last_overlay["cutout_url"] = job.cutout_url
            job.stage = "matting"
            hold_s = float(os.environ.get("BOOTH_MATTE_PREVIEW_HOLD_S", "0.9"))
            if hold_s > 0:
                time.sleep(min(2.0, hold_s))

        aigc_scene = None
        aigc_result = {}
        if self.aigc_style(style) is not None:
            job.stage = "stylizing"
            # 以真正进入生图阶段的时刻计时；抠像完成前不会切回预览。
            job.aigc_started_at = time.time()
            started = time.perf_counter()
            try:
                if infer_error is not None:
                    raise RuntimeError("人像处理未完成，未开始风格生成")
                if job.person_count < 1:
                    coverage = (sum(alpha.histogram()[129:]) / (alpha.width * alpha.height)
                                if alpha is not None else 0.0)
                    if (not job.matte_applied
                            or job.matte_source not in ("modnet", "modnet-roi")
                            or not 0.01 <= coverage <= 0.99):
                        raise ValueError("未取得有效人像，请调整站位后重试")
                    log("人像检测未命中，使用 MODNet 有效抠像继续风格化（覆盖率 %.1f%%）"
                        % (coverage * 100))
                orientation = "wide" if meta.get("orientation") == "wide" else "vertical"
                from aigc_client import AIGCError

                # 渠道内部已经会做一次可用渠道切换；这里再对瞬时网络/服务波动
                # 做有限重试。400/404/422 等不可重试错误不重复请求，避免浪费额度。
                retry_count = max(0, min(3, int(meta.get("aigc_retry_count", 0) or 0)))
                attempts = retry_count + 1
                for attempt in range(attempts):
                    try:
                        aigc_scene = self._aigc_scene(
                            style, photo, alpha, orientation, job.portrait_mode,
                            result_meta=aigc_result)
                        break
                    except AIGCError as exc:
                        if not exc.retryable or attempt >= attempts - 1:
                            raise
                        retry_started = time.perf_counter()
                        job.step(
                            "aigc_retry", "AIGC 波动 · 自动重试",
                            (time.perf_counter() - retry_started) * 1000,
                            "%d/%d" % (attempt + 1, retry_count),
                        )
                        time.sleep(min(3.0, 0.8 * (attempt + 1)))
                preview_path = self.store.root / "previews" / ("%s-aigc.jpg" % record_id)
                aigc_scene.convert("RGB").save(preview_path, "JPEG", quality=90, optimize=True)
                job.style_preview_url = "/api/job-preview/%s-aigc.jpg" % record_id
                self.last_overlay["style_preview_url"] = job.style_preview_url
                hold_s = float(os.environ.get("BOOTH_STYLE_PREVIEW_HOLD_S", "0.9"))
                if hold_s > 0:
                    time.sleep(min(2.0, hold_s))
                style_title = (self.aigc_style(style) or {}).get("title", style)
                job.step("render", "AIGC 人像与背景生成", (time.perf_counter() - started) * 1000,
                         style_title)
            except Exception as exc:
                job.degraded = True
                job.degraded_reason = "; ".join(
                    part for part in (job.degraded_reason, "aigc_failed: %s" % exc) if part)
                job.step("render", "AIGC 未完成 · 使用原片", (time.perf_counter() - started) * 1000,
                         str(exc))
                log("AIGC 完整场景生成失败，保留原有成片：%s" % exc)

        # ③ 合成
        job.stage = "rendering"
        try:
            final, wide, style_label, clean_final = self._render(
                job, photo, mask, style, record_id, meta, alpha=alpha,
                aigc_scene=aigc_scene)
        except Exception as exc:
            job.degraded = True
            job.degraded_reason = "; ".join(
                part for part in (job.degraded_reason, "render_error: %s" % exc) if part)
            log("合成失败：%s" % traceback.format_exc(limit=3))
            final, wide, style_label, clean_final = None, None, style, None

        # ④ 入库（合成失败时退到「原片 + 水印」，仍然出图）
        job.stage = "storing"
        started = time.perf_counter()
        if final is None:
            final = self._fallback_image(photo, record_id, job, meta)
            clean_final = photo.copy().convert("RGB")
            clean_final.thumbnail((1000, 1000))
            style_label = "原片"
        if clean_final is None:
            clean_final = final.copy()
        ai_generated = aigc_scene is not None and style_label != "原片"
        if ai_generated and job.ai_watermark:
            from render.ai_label import apply_ai_label

            final = apply_ai_label(final)
            clean_final = apply_ai_label(clean_final)
            if wide is not None:
                wide = apply_ai_label(wide)
        source_token = meta.get("regenerate_from_token")
        if source_token and self.store.by_token(source_token) is None:
            raise ValueError("原照片已删除或过期，已停止重新生成")
        final.save(self.store.photo_path(record_id), "JPEG", quality=92, optimize=True)
        clean_final.save(self.store.clean_photo_path(record_id), "JPEG", quality=92, optimize=True)
        if wide is not None:
            wide.save(self.store.root / "photos" / ("%s-wide.jpg" % record_id),
                      "JPEG", quality=92, optimize=True)
        thumb = final.copy()
        thumb.thumbnail((480, 600))
        thumb.save(self.store.thumb_path(record_id), "JPEG", quality=88)
        job.step("store", "入库编号", (time.perf_counter() - started) * 1000, record_id)
        job.finished_at = job.finished_at or time.time()
        job.total_ms = round((job.finished_at - job.started_at) * 1000, 1)
        if self.last_overlay is not None:
            self.last_overlay["total_ms"] = job.total_ms

        record = Record(
            id=record_id, token=token, created_at=_now(), prompt="person", ep=job.ep,
            infer_ms=float(job.infer_ms or 0.0), person_count=job.person_count,
            degraded=job.degraded, seed=int(meta.get("seed", 7)), card=style_label,
            style=style,
            extra={"ep_threads": job.ep_threads, "mask_applied": job.mask_applied,
                   "portrait_timing": dict(job.portrait_timing),
                   "matte_applied": job.matte_applied, "matte_ms": job.matte_ms,
                   "matte_source": job.matte_source,
                   "cutout_url": job.cutout_url,
                   "style_preview_url": job.style_preview_url,
                   "degraded_reason": job.degraded_reason,
                   "camera_mock": job.source == "camera" and not self.camera.status.real,
                   "total_ms": job.total_ms, "job": job.id,
                   "queue_id": meta.get("queue_id"),
                   "source": job.source,
                   "portrait_mode": job.portrait_mode,
                   "ai_watermark": job.ai_watermark,
                   "ai_generated": ai_generated,
                   "ai_watermark_applied": ai_generated and job.ai_watermark,
                   "aigc_channel_id": aigc_result.get("channel_id"),
                   "aigc_channel_name": aigc_result.get("channel_name"),
                   "aigc_model": aigc_result.get("model"),
                   "aigc_elapsed_ms": aigc_result.get("elapsed_ms"),
                   "name": str(meta.get("name") or "")[:24],
                   "handle": str(meta.get("handle") or "")[:32],
                   "regenerated_from": meta.get("regenerated_from"),
                   "orientation": "wide" if meta.get("orientation") == "wide" else "vertical"},
        )
        self.store.append(record)

        self.counters["captures"] += 1
        if job.degraded:
            self.counters["degraded"] += 1
        job.ok = True
        job.stage = "done"
        log("出图 %s：ep=%s infer=%sms 总耗时=%.2fs 降级=%s"
            % (record_id, job.ep, job.infer_ms, job.total_ms / 1000, job.degraded))

    def _fallback_image(self, photo, record_id: str, job: Job, meta: dict | None = None):
        """最后的兜底：仍使用当前版式，不额外添加编号。"""
        from PIL import Image

        meta = meta or {}
        try:
            import importlib

            kv = importlib.import_module("render.keyvisual")
            size = kv.WIDE if meta.get("orientation") == "wide" else kv.VERTICAL
            return kv.compose_generated(
                photo, record_id=record_id, name=meta.get("name", ""),
                handle=meta.get("handle", ""), size=size,
                mock=job.source == "camera" and not self.camera.status.real)
        except Exception as exc:
            log("新版兜底版式失败，退回基础图：%s" % exc)

        image = photo.copy()
        image.thumbnail((1000, 1000))
        try:
            import importlib

            watermark = importlib.import_module("render.watermark")
            image = watermark.apply_watermark(image, record_id, ep=job.ep,
                                              style_label="原片", infer_ms=job.infer_ms,
                                              person_count=job.person_count)
        except Exception as exc:
            log("兜底水印也失败了（直接用原片）：%s" % exc)
        return image.convert("RGB")

    def capture(self, style: str | None, meta: dict) -> Job:
        portrait_mode_prompt(meta.get("portrait_mode", "standard"))
        if not isinstance(meta.get("ai_watermark", True), bool):
            raise ValueError("AI 标识选项无效")
        with self.style_catalog._lock:
            chosen = self.next_style(style)
            job = self.jobs.create(chosen)
            job.portrait_mode = meta.get("portrait_mode", "standard")
            job.ai_watermark = meta.get("ai_watermark", True)
        thread = threading.Thread(target=self._guarded_run, args=(job, chosen, meta),
                                  name="capture-%s" % job.id, daemon=True)
        thread.start()
        return job

    def submit_operator_upload(self, raw: bytes, style: str | None, meta: dict) -> Job:
        """后台提交一张图片，选择 AIGC 风格或原片后进入同一出图管线。"""
        from PIL import Image, ImageOps

        portrait_mode_prompt(meta.get("portrait_mode", "standard"))
        if not isinstance(meta.get("ai_watermark", True), bool):
            raise ValueError("AI 标识选项无效")
        if not raw or len(raw) > 20 * 1024 * 1024:
            raise ValueError("图片不能为空，且不能超过 20 MB")
        try:
            with Image.open(io.BytesIO(raw)) as source:
                if source.format not in ("JPEG", "PNG", "WEBP"):
                    raise ValueError("请选择 JPG、PNG 或 WEBP 图片")
                if source.width * source.height > 24_000_000:
                    raise ValueError("图片像素不能超过 2400 万")
                source.load()
                photo = ImageOps.exif_transpose(source).convert("RGB")
        except ValueError:
            raise
        except Exception as exc:
            raise ValueError("无法读取这张图片，请选择 JPG、PNG 或 WEBP") from exc

        mode = str(meta.get("upload_mode") or "aigc").strip().lower()
        if mode not in ("aigc", "original"):
            raise ValueError("请选择 AIGC 风格或原图")
        if mode == "original":
            chosen = "natural"
        else:
            chosen = str(style or "").strip()
        meta = dict(meta)
        meta["source"] = "operator_upload"
        meta["upload_mode"] = mode
        with self.style_catalog._lock:
            if mode == "aigc" and (not chosen or self.aigc_style(chosen) is None):
                raise ValueError("请选择一个已启用的 AIGC 风格")
            job = self.jobs.create(chosen)
            job.portrait_mode = meta.get("portrait_mode", "standard") if mode == "aigc" else "standard"
            meta["portrait_mode"] = job.portrait_mode
            job.source = "operator_upload"
            job.ai_watermark = meta.get("ai_watermark", True)
        thread = threading.Thread(
            target=self._guarded_uploaded_run,
            args=(job, chosen, meta, photo, raw),
            name="operator-upload-%s" % job.id,
            daemon=True,
        )
        thread.start()
        return job

    def _guarded_uploaded_run(self, job: Job, style: str, meta: dict,
                              photo, raw: bytes, *, lock_held: bool = False) -> None:
        if not lock_held and not self.lock.acquire(timeout=45):
            job.ok = False
            job.stage = "failed"
            job.error = "另一个拍摄任务还在进行"
            job.finished_at = time.time()
            return
        try:
            source_token = meta.get("regenerate_from_token")
            if source_token and self.store.by_token(source_token) is None:
                raise ValueError("原照片已删除或过期")
            self._run(job, style, meta, source_photo=photo, source_raw=raw)
        except Exception as exc:
            job.ok = False
            job.stage = "failed"
            job.error = "%s: %s" % (type(exc).__name__, exc)
            job.finished_at = time.time()
            self.last_error = job.error
            self.counters["errors"] += 1
            log("后台上传出图失败：%s" % traceback.format_exc(limit=4))
        finally:
            self.lock.release()

    def submit_operator_regeneration(self, token: str, style: str, portrait_mode: str,
                                     ai_watermark: bool = True) -> Job:
        from PIL import Image, ImageOps

        portrait_mode_prompt(portrait_mode)
        if not isinstance(ai_watermark, bool):
            raise ValueError("AI 标识选项无效")
        record = self.store.by_token(token)
        if record is None or not self.store.photo_path(record.id).is_file():
            raise ValueError("照片不存在、已删除或已过期")
        capture_path = self.store.capture_path(record.id)
        if not capture_path.is_file():
            raise ValueError("拍摄原图不存在，请通过上传图片重新生成")
        raw = capture_path.read_bytes()
        with Image.open(io.BytesIO(raw)) as source:
            source.load()
            photo = ImageOps.exif_transpose(source).convert("RGB")
        extra = record.extra or {}
        queue = getattr(getattr(self, "auto_flow", None), "queue", None)
        entry = queue.get(extra.get("queue_id")) if queue and extra.get("queue_id") else None
        meta = {
            "portrait_mode": portrait_mode, "ai_watermark": ai_watermark,
            "orientation": extra.get("orientation", "vertical"),
            "name": extra.get("name", getattr(entry, "name", "")),
            "handle": extra.get("handle", getattr(entry, "handle", "")),
            "seed": record.seed, "mnemonic": None, "upload_mode": "aigc",
            "source": "operator_regenerate", "regenerated_from": record.id,
            "regenerate_from_token": record.token,
        }
        with self.style_catalog._lock:
            if not style or self.aigc_style(style) is None:
                raise ValueError("请选择一个已启用的 AIGC 风格")
            if (getattr(self, "active_countdown_job", None) is not None
                    or (queue is not None and queue.snapshot().get("occupancy"))
                    or self.jobs.active_styles() or not self.lock.acquire(blocking=False)):
                raise RuntimeError("当前正在拍摄或生成，请完成后再试")
            try:
                job = self.jobs.create(style)
                job.portrait_mode = portrait_mode
                job.ai_watermark = ai_watermark
                job.source = "operator_regenerate"
                thread = threading.Thread(
                    target=self._guarded_uploaded_run,
                    args=(job, style, meta, photo, raw), kwargs={"lock_held": True},
                    name="operator-regenerate-%s" % job.id, daemon=True)
                thread.start()
            except Exception:
                self.lock.release()
                raise
        return job

    def _publish_segmentation(self, job: Job, result: dict) -> None:
        from PIL import Image

        if job.segmentation is not None or not result.get("ok", True):
            return
        encoded = result.get("subject_mask_b64")
        mask_url = None
        if encoded and job.record_id:
            with Image.open(io.BytesIO(base64.b64decode(encoded))) as mask:
                if mask.getextrema()[1] > 0:
                    folder = self.store.root / "masks"
                    folder.mkdir(parents=True, exist_ok=True)
                    mask.save(folder / ("%s-subject.png" % job.record_id))
                    mask_url = "/api/mask/%s-subject.png" % job.record_id
        job.ep = bool(result.get("ep"))
        job.infer_ms = result.get("infer_ms")
        job.person_count = int(result.get("person_count", 0))
        job.segmentation = {
            "mask_url": mask_url,
            "box": (result.get("subject") or {}).get("box"),
            "width": result.get("width"), "height": result.get("height"),
            "ep": job.ep, "infer_ms": job.infer_ms,
        }
        job.portrait_timing["seg_model_ms"] = job.infer_ms
        if job.stage == "inferring":
            job.stage = "matting"

    def _infer_uploaded_portrait(self, photo, job: Job) -> dict:
        import tempfile

        from PIL import Image

        processing = photo.copy()
        processing.thumbnail((1920, 1920), Image.Resampling.LANCZOS)
        with tempfile.TemporaryDirectory(prefix="booth-upload-infer-") as directory:
            path = Path(directory) / "portrait.jpg"
            processing.save(path, "JPEG", quality=95)
            with self._gesture_seg_lock:
                started = time.perf_counter()
                health = self.infer.switch_mode("seg", timeout=30.0)
                if not health.get("ok", False):
                    raise RuntimeError(health.get("error") or "人像模型准备失败")
                job.step("infer_ready", "准备人像模型", (time.perf_counter() - started) * 1000)
                result = self.infer.seg(str(path), prompts=["person"], conf=0.20,
                                       mask_max_side=640, matte_max_side=MATTE_MAX_SIDE,
                                       on_progress=lambda progress: self._publish_segmentation(job, progress),
                                       timeout=max(20.0, INFER_TIMEOUT_S + 8.0))
        if result.get("ok", True):
            scale_x = photo.width / processing.width
            scale_y = photo.height / processing.height

            def original_box(box):
                return [round(float(value) * (scale_x if index % 2 == 0 else scale_y), 1)
                        for index, value in enumerate(box)]

            for person in result.get("persons", []):
                if person.get("box"):
                    person["box"] = original_box(person["box"])
            if (result.get("subject") or {}).get("box"):
                result["subject"]["box"] = original_box(result["subject"]["box"])
            if result.get("matte_roi"):
                result["matte_roi"] = original_box(result["matte_roi"])
            result["processing_width"], result["processing_height"] = processing.size
            result["width"], result["height"] = photo.size
        return result

    def _guarded_run(self, job: Job, style: str, meta: dict) -> None:
        if not self.lock.acquire(timeout=45):
            job.ok = False
            job.stage = "failed"
            job.error = "另一个拍摄任务还在进行"
            job.finished_at = time.time()
            return
        try:
            self._run(job, style, meta)
        except Exception as exc:
            job.ok = False
            job.stage = "failed"
            job.error = "%s: %s" % (type(exc).__name__, exc)
            job.finished_at = time.time()
            self.last_error = job.error
            self.counters["errors"] += 1
            log("拍摄失败：%s" % traceback.format_exc(limit=4))
        finally:
            self.lock.release()

    # ---- 运维 ----------------------------------------------------------
    def usb_reset_bus(self) -> dict:
        """整条 USB 总线复位：相机从总线上掉下去时的自救。

        比「一键恢复」重：那条总线上的设备都会重新枚举，要十几秒。
        所以它不在 restore 里自动跑，得有人点 —— 或者由相机自愈逻辑在
        「节点确实没了」时自己触发（见 camera.Camera.recover）。
        """
        started = time.perf_counter()
        before = self.camera.status
        out = self.camera.usb_reset("操作员手工触发")
        status = None
        try:
            status = self.camera.open()
        except Exception as exc:
            out.setdefault("detail", "%s: %s" % (type(exc).__name__, exc))
        # worker 那边可能已经因为相机抖动被判定不可用了，顺手确认一下它还活着
        if not self.infer.running:
            try:
                self.infer.stop()
                self.infer.start()
            except Exception as exc:  # noqa: BLE001
                log("USB 复位后重启 worker 失败：%s" % exc)
        now = self.camera.status
        return {
            "ok": bool(out.get("ok") and now.ok),
            "host": getattr(self.camera, "_usb_host", None),
            "detail": out.get("detail") or "",
            "before": {"device": before.device, "ok": before.ok, "detail": before.detail},
            "after": {"device": now.device, "ok": now.ok, "real": now.real,
                      "fourcc": now.fourcc, "detail": now.detail},
            "elapsed_ms": round((time.perf_counter() - started) * 1000, 1),
            "at": _now(),
        }

    def restore(self) -> dict:
        """一键恢复到就绪：清同意令牌、重开相机、复活 worker、清最后一张的叠加层。"""
        actions = []
        revoked = self.consent.revoke_all()
        actions.append("清空同意令牌 %d 个" % revoked)
        self.last_overlay = None
        self.last_error = None
        try:
            self.camera.close()
            status = self.camera.open()
            actions.append("相机重开 %dx%d %s" % (status.width, status.height, status.fourcc or "-"))
        except Exception as exc:
            actions.append("相机重开失败：%s" % exc)
        try:
            if not self.infer.running:
                self.infer.stop()
                self.infer.start()
                actions.append("推理 worker 已重启")
            health = self.infer.health(timeout=180.0)
            self._preloaded = True
            self.note_ep(health)
            actions.append("worker ep=%s threads=%s" % (health.get("ep"), health.get("ep_threads")))
        except Exception as exc:
            actions.append("worker 不可用：%s" % exc)
        return {"ok": True, "actions": actions, "at": _now(), "state": self.state()}

    def purge(self) -> dict:
        result = self.store.purge_expired()
        return result

    def reset_for_event(self) -> dict:
        """彩排清场：把彩排产生的成片和编号清掉，回到「活动还没开始」。

        和「按留存期清库」不是一回事：那个按 7 天删，彩排当天拍的一律删不掉。
        彩排结束、活动开始前用它，大屏上的「已出图」才是活动的真实数字。
        """
        result = self.store.reset_for_event()
        result["tokens_revoked"] = self.consent.revoke_all()
        self.last_overlay = None
        self.last_error = None
        self.counters.update({"captures": 0, "degraded": 0, "errors": 0, "cpu_infers": 0})
        result["state"] = self.state()
        return result


# ---- 公网取图地址（操作员面板上填）----------------------------------------
# 现场默认跑局域网 IP。但明文 HTTP 的 IP 地址不是「安全上下文」，实测
# isSecureContext=false → navigator.share / navigator.clipboard 全是 undefined，
# 取图页上「分享」这类按钮点了等于没点（见 docs/07）。
# 指定一个公网 HTTPS 域名（K3 走隧道暴露，见自建反向代理 / 隧道）之后，
# 系统分享面板才可用 —— 那是唯一能真正跳到小红书/微信发布页的通道。
_SETTINGS_CACHE: dict | None = None
# host[:port]，host 首尾必须是字母数字
_HOST_RE = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9.\-]*[A-Za-z0-9])?(:[0-9]{1,5})?$")


def read_settings() -> dict:
    """读 settings.json（读一次缓存；写的时候整份替换）。读坏了按空配置继续。"""
    global _SETTINGS_CACHE
    if _SETTINGS_CACHE is None:
        data: dict = {}
        try:
            if SETTINGS_PATH.is_file():
                loaded = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    data = loaded
        except Exception as exc:  # noqa: BLE001
            log("settings.json 读不出来，按空配置继续：%s" % exc)
        _SETTINGS_CACHE = data
    return _SETTINGS_CACHE


def presentation_runtime_config() -> tuple[str, float]:
    """读取现场展示模式；操作后台保存的设置覆盖环境变量。"""
    mode = os.environ.get("BOOTH_PRESENTATION_MODE", "full").strip().lower()
    if mode not in ("full", "handoff"):
        mode = "full"
    try:
        delay = max(0.0, float(os.environ.get(
            "BOOTH_AIGC_HANDOFF_DELAY_SECONDS",
            str(DEFAULT_AIGC_HANDOFF_DELAY_SECONDS),
        )))
    except (TypeError, ValueError):
        delay = DEFAULT_AIGC_HANDOFF_DELAY_SECONDS
    saved = read_settings()
    saved_mode = saved.get("presentation_mode")
    if saved_mode in ("full", "handoff"):
        mode = saved_mode
    elif isinstance(saved.get("handoff_enabled"), bool):
        mode = "handoff" if saved["handoff_enabled"] else "full"
    saved_delay = saved.get("aigc_handoff_delay_seconds")
    if isinstance(saved_delay, (int, float)) and not isinstance(saved_delay, bool):
        delay = max(0.0, float(saved_delay))
    return mode, delay


def write_settings(data: dict) -> None:
    """整份覆盖写，先写临时文件再替换 —— 现场断电不能留下半份 JSON。"""
    global _SETTINGS_CACHE
    SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = SETTINGS_PATH.with_name(SETTINGS_PATH.name + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(SETTINGS_PATH)
    _SETTINGS_CACHE = dict(data)


def normalize_base(value: str) -> str:
    """把操作员填的那一行收成规范地址；不合法直接抛 ValueError。

    拦住的是最坏的一种错：填了个「看着像地址」的字符串，二维码照常生成、
    现场照常发出去，扫开却打不开 —— 那时候没有任何人会去查。
    只接受 http/https，且必须带主机名。
    """
    text = str(value or "").strip().rstrip("/")
    if not text:
        return ""
    parsed = urlparse(text if "://" in text else "http://" + text)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ValueError("要填成 https://域名 的形式（必须带 http:// 或 https://）")
    if parsed.query or parsed.fragment:
        raise ValueError("地址里不要带 ? 或 # 后面的东西")
    if parsed.path not in ("", "/"):
        raise ValueError("只填到域名就好，不要带路径")
    # urlparse 对主机名很宽容：`http://hello world` 也能解析出 netloc="hello world"。
    # 不自己再卡一道，就会存下一个「看着像地址、扫开打不开」的字符串 —— 现场最坏的一种错。
    if not _HOST_RE.match(parsed.netloc) or ".." in parsed.netloc:
        raise ValueError("主机名里只能有字母、数字、点、短横线，需要端口就写 :8080")
    try:
        port = parsed.port
    except ValueError:          # urlparse 对超出范围的端口会直接抛
        raise ValueError("端口要在 1~65535 之间")
    if port is not None and not 1 <= port <= 65535:
        raise ValueError("端口要在 1~65535 之间")
    return "%s://%s" % (parsed.scheme, parsed.netloc)


def lan_base() -> str:
    """局域网兜底地址（手机连上现场网络时可用，不需要公网）。"""
    return "http://%s:%d" % (lan_ip(), PORT)


def public_base() -> str:
    """取图链接用哪个地址。优先级：面板填的 > BOOTH_PUBLIC_HOST > 局域网 IP。"""
    configured = str(read_settings().get("public_base") or "").strip()
    if configured:
        return configured
    env_host = str(os.environ.get("BOOTH_PUBLIC_HOST") or "").strip()
    if env_host:
        # 兼容旧写法（只给主机名）也接受整条 URL
        return env_host.rstrip("/") if "://" in env_host else "http://%s:%d" % (env_host, PORT)
    return lan_base()


def public_base_source() -> str:
    """这个地址是从哪儿来的 —— 面板上要如实标，不然改不生效会查不出来。"""
    if str(read_settings().get("public_base") or "").strip():
        return "panel"
    if str(os.environ.get("BOOTH_PUBLIC_HOST") or "").strip():
        return "env"
    return "lan"

# --------------------------------------------------------------------------
# HTTP 层
# --------------------------------------------------------------------------
CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".ico": "image/x-icon",
    ".woff2": "font/woff2",
    ".txt": "text/plain; charset=utf-8",
    ".md": "text/plain; charset=utf-8",
    # 竖屏等待影片（见 static/js/create-flow.js）。
    # 不带这个类型会退回 application/octet-stream：浏览器多数仍能靠嗅探播出来，
    # 但那属于"碰巧能跑"，不该指望。（2026-10-15 部署后实测确认缺失。）
    ".mp4": "video/mp4",
}


def _ensure_app_package_root() -> None:
    """把项目根加入 sys.path，使 ``import app.auto_flow.*`` 可用（T-2 接线专用）。

    booth_server 以脚本方式启动时 sys.path[0] 是 app 目录（systemd:
    /opt/devcon/app），而 auto_flow 各模块用 ``app.auto_flow`` 绝对导入，
    需要项目根（app 的上一级）在 sys.path 上 —— 与 app/tests 下的测试运行方式
    保持一致。只在队列功能真正启用/路由被访问时调用；开关关闭时不改 sys.path。
    """
    import sys

    root = str(HERE.parent)
    if root not in sys.path:
        sys.path.append(root)


class Handler(BaseHTTPRequestHandler):
    server_version = "SpacemiTDevConBooth/" + BUILD
    booth: Booth = None  # type: ignore[assignment]

    # ---- 小工具 --------------------------------------------------------
    def log_message(self, fmt, *args):  # 默认不记 IP（合规：最小化个人信息）
        if ACCESS_LOG:
            log("%s %s" % (self.address_string(), fmt % args))

    def _send(self, status: int, body: bytes, content_type: str, extra: dict | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Referrer-Policy", "no-referrer")
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _json(self, payload: dict, status: int = 200, extra: dict | None = None) -> None:
        self._send(status, json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8", extra)

    def _error(self, status: int, code: str, detail: str = "") -> None:
        self._json({"ok": False, "error": code, "detail": detail}, status)

    def _body(self) -> dict:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return {}
        if length <= 0:
            return {}
        try:
            return json.loads(self.rfile.read(length).decode("utf-8") or "{}")
        except Exception:
            return {}

    def _multipart(self) -> tuple[dict, dict]:
        """读取后台图片上传：返回文本字段与文件字段。"""
        content_type = self.headers.get("Content-Type", "")
        match = re.search(r"boundary=(?:\"([^\"]+)\"|([^;]+))", content_type,
                          re.I)
        if not match:
            raise ValueError("上传格式不正确")
        boundary = (match.group(1) or match.group(2)).encode("utf-8")
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError as exc:
            raise ValueError("上传大小无法确认") from exc
        if length <= 0 or length > 21 * 1024 * 1024:
            raise ValueError("上传图片不能超过 20 MB")
        payload = self.rfile.read(length)
        fields: dict[str, str] = {}
        files: dict[str, tuple[str, bytes, str]] = {}
        marker = b"--" + boundary
        for part in payload.split(marker)[1:]:
            if part.startswith(b"--"):
                break
            part = part.lstrip(b"\r\n")
            if not part:
                continue
            header_blob, separator, value = part.partition(b"\r\n\r\n")
            if not separator:
                continue
            if value.endswith(b"\r\n"):
                value = value[:-2]
            headers = {}
            for line in header_blob.decode("utf-8", "replace").split("\r\n"):
                key, divider, item = line.partition(":")
                if divider:
                    headers[key.strip().lower()] = item.strip()
            disposition = headers.get("content-disposition", "")
            name_match = re.search(r'name="([^"]+)"', disposition)
            if not name_match:
                continue
            name = name_match.group(1)
            filename_match = re.search(r'filename="([^"]*)"', disposition)
            if filename_match and filename_match.group(1):
                files[name] = (filename_match.group(1), value,
                               headers.get("content-type", "application/octet-stream"))
            else:
                fields[name] = value.decode("utf-8", "replace").strip()
        return fields, files

    def _file(self, path: Path, default_type: str = "") -> None:
        if not path.is_file():
            self._error(404, "not_found", str(path.name))
            return
        ctype = default_type or CONTENT_TYPES.get(path.suffix.lower(), "application/octet-stream")
        self._send(200, path.read_bytes(), ctype)

    def _static(self, relative: str) -> None:
        base = STATIC_DIR
        if relative.startswith("assets/"):
            base, relative = ASSETS_DIR, relative[len("assets/"):]
        elif relative.startswith("cards/"):
            base, relative = ASSETS_DIR / "cards", relative[len("cards/"):]
        candidate = (base / unquote(relative)).resolve()
        if not str(candidate).startswith(str(base.resolve())):
            self._error(403, "forbidden_path")
            return
        self._file(candidate)

    def _html(self, name: str) -> None:
        """页面 HTML 里的 __BUILD__ 换成当前页面版本，再发出去。

        顺手把每个 /static/... 后面的 ?v=__BUILD__ 一起换掉：刷新一次必定
        拿到与这份 HTML 配套的 CSS/JS，不会出现「新 HTML + 老 JS」的错配。
        """
        path = STATIC_DIR / name
        if not path.is_file():
            self._error(404, "not_found", name)
            return
        body = path.read_text("utf-8").replace("__BUILD__", PAGE_BUILD).encode("utf-8")
        self._send(200, body, "text/html; charset=utf-8")

    # ---- 队列（T-2） -----------------------------------------------------
    # 纯逻辑在 app/auto_flow/http_handlers.py；这里只做：开关门禁 -> 取会话头
    # -> 调处理器 -> 用嵌套错误体输出。开关关闭时按"未部署"处理：404 not_found，
    # 与未知路径同形，不 import 任何 auto_flow 代码。

    def _request_id(self) -> str:
        """本次请求的关联 ID：优先 X-Request-Id 头，否则生成 12 位十六进制。"""
        return (self.headers.get("X-Request-Id") or "").strip() or uuid.uuid4().hex[:12]

    def _queue_services(self):
        """队列子系统或 None（BOOTH_QUEUE_ENABLED 默认关闭 -> None）。"""
        return self.booth.auto_flow_services()

    def _queue_result(self, call, *args, **kwargs) -> None:
        """执行队列处理器并输出：ApiError -> 嵌套错误体；意外异常 -> 500。"""
        _ensure_app_package_root()
        from app.auto_flow import errors as auto_flow_errors

        try:
            status, payload = call(*args, **kwargs)
        except auto_flow_errors.ApiError as exc:
            return self._json(exc.payload(self._request_id()), exc.http_status)
        except Exception:
            log("队列路由 %s 出错：%s" % (self.path, traceback.format_exc(limit=4)))
            return self._json(
                auto_flow_errors.error_payload(auto_flow_errors.INTERNAL_ERROR, self._request_id()),
                500,
            )
        return self._json(payload, status)

    def _queue_submit(self, path: str, body: dict) -> None:
        services = self._queue_services()
        if services is None:
            return self._error(404, "not_found", path)
        from app.auto_flow.http_handlers import handle_queue_submit

        from app.auto_flow import errors as auto_flow_errors

        try:
            with self.booth.style_catalog._lock:
                status, payload = handle_queue_submit(
                    body, queue=services.queue, consent=services.consent,
                    sessions=services.sessions, style_source=services.style_source)
        except auto_flow_errors.ApiError as exc:
            return self._json(exc.payload(self._request_id()), exc.http_status)
        except Exception:
            log("排队提交处理失败：%s" % traceback.format_exc(limit=4))
            return self._json(auto_flow_errors.error_payload(auto_flow_errors.INTERNAL_ERROR,
                                                            self._request_id()), 500)
        extra = None
        if services.recovery is not None:
            try:
                binding = services.recovery.bind(payload["entry"]["queue_id"], self._recovery_browser())
                payload["recovery_url"] = self._recovery_url(binding)
                extra = self._recovery_cookie(binding)
            except Exception as exc:
                log("备用恢复链接暂未保存：%s" % type(exc).__name__)
        return self._json(payload, status, extra)

    def _recovery_browser(self) -> str:
        from http.cookies import SimpleCookie, CookieError

        cookies = SimpleCookie()
        try:
            cookies.load(self.headers.get("Cookie", ""))
        except CookieError:
            return ""
        value = cookies.get("booth_browser")
        return value.value if value is not None else ""

    def _recovery_cookie(self, binding: dict) -> dict:
        cookie = "booth_browser=%s; Path=/; Max-Age=604800; HttpOnly; SameSite=Lax" % binding["browser_token"]
        if self.headers.get("X-Forwarded-Proto", "").lower() == "https":
            cookie += "; Secure"
        return {"Set-Cookie": cookie}

    @staticmethod
    def _recovery_url(binding: dict) -> str:
        return "/join#restore=" + binding["token"]

    def _recovery_get(self, path: str) -> None:
        try:
            return self._recovery_get_response(path)
        except Exception as exc:
            log("备用恢复查询暂不可用：%s" % type(exc).__name__)
            return self._error(503, "recovery_unavailable", "暂时无法读取拍摄进度，请稍后重试。")

    def _recovery_get_response(self, path: str) -> None:
        services = self._queue_services()
        if services is None or services.recovery is None:
            return self._error(404, "recovery_not_found", "拍摄链接不存在或已过期。")
        current = path == "/api/recovery/current"
        binding = (services.recovery.current(self._recovery_browser()) if current
                   else services.recovery.get(path[len("/api/recovery/"):]))
        entry = services.queue.get(binding["queue_id"]) if binding else None
        if entry is None:
            return self._error(404, "recovery_not_found", "拍摄链接不存在或已过期。")
        from app.auto_flow.http_handlers import _pick_photo_record

        status_value = str(getattr(entry.status, "value", entry.status))
        pickup_url = None
        if status_value == "ready_for_pickup":
            record = _pick_photo_record(services.photo_service, entry.queue_id, strict=True)
            if record is not None:
                photo = self.booth.store.by_token(record.photo_token)
                if photo is not None and self.booth.store.photo_path(photo.id).is_file():
                    pickup_url = "/p/" + record.photo_token
        if status_value == "ready_for_pickup" and not pickup_url:
            return self._error(404, "recovery_not_found", "照片不存在、已删除或已过期。")
        serialized = entry.to_dict()
        if not current:
            allowed = {"status", "style_id", "portrait_mode", "ai_watermark", "display_number", "created_at",
                       "updated_at", "called_at", "start_deadline", "started_at", "expired_at"}
            serialized = {key: value for key, value in serialized.items() if key in allowed}
        payload = {"ok": True, "entry": serialized, "read_only": not current,
                   "recovery_url": self._recovery_url(binding), "pickup_url": pickup_url}
        if current:
            session = services.sessions.get_existing(entry.queue_id)
            if session is None:
                return self._error(404, "recovery_not_found", "拍摄链接不存在或已过期。")
            payload.update(session=session, consent_id=services.queue.submission_consent_id(entry.queue_id))
        return self._json(payload)

    def _recovery_attach(self, body: dict) -> None:
        services = self._queue_services()
        queue_id = body.get("queue_id") if isinstance(body, dict) else None
        if (services is None or services.recovery is None or not isinstance(queue_id, str)
                or not services.sessions.check(self.headers.get("X-Queue-Session"), queue_id)
                or services.queue.get(queue_id) is None):
            return self._error(404, "recovery_not_found", "排队记录不存在。")
        try:
            binding = services.recovery.bind(queue_id, self._recovery_browser())
        except (OSError, ValueError) as exc:
            log("备用恢复链接暂未保存：%s" % type(exc).__name__)
            return self._error(503, "recovery_unavailable", "备用链接暂时无法保存，请保留当前拍摄页面。")
        return self._json({"ok": True, "recovery_url": self._recovery_url(binding)},
                          extra=self._recovery_cookie(binding))

    def _queue_get(self, path: str) -> None:
        services = self._queue_services()
        if services is None:
            return self._error(404, "not_found", path)
        from app.auto_flow.http_handlers import handle_queue_get

        self._queue_result(
            handle_queue_get,
            path[len("/api/queue/"):],
            queue=services.queue,
            sessions=services.sessions,
            session=self.headers.get("X-Queue-Session"),
        )

    def _queue_start(self, path: str, body: dict) -> None:
        services = self._queue_services()
        if services is None:
            return self._error(404, "not_found", path)
        from app.auto_flow.http_handlers import handle_queue_start

        self._queue_result(
            handle_queue_start,
            path[len("/api/queue/"):-len("/start")],
            body,
            queue=services.queue,
            consent=services.consent,
            sessions=services.sessions,
            session=self.headers.get("X-Queue-Session"),
            camera_probe=services.camera_probe,
            gesture_enabled=services.gesture_enabled,
            clock=services.clock,
        )

    def _operator_queue(self) -> None:
        """操作员专用队列视图：只返回现场处置所需字段。"""
        services = self._queue_services()
        if services is None:
            return self._error(404, "not_found", "/api/operator/queue")
        snapshot = services.queue.snapshot()
        visible_statuses = {"queued", "called", "ready", "capturing", "processing"}
        entries = [
            {
                "queue_id": item["queue_id"],
                "display_number": item.get("display_number"),
                "status": item.get("status"),
                "style_id": item.get("style_id"),
                "portrait_mode": item.get("portrait_mode", "standard"),
                "ai_watermark": item.get("ai_watermark", True),
                "created_at": item.get("created_at"),
                "updated_at": item.get("updated_at"),
            }
            for item in snapshot.get("entries", [])
            if item.get("status") in visible_statuses
        ]
        return self._json({
            "ok": True,
            "unlimited": snapshot.get("capacity", {}).get("max_active") is None,
            "max_active": snapshot.get("capacity", {}).get("max_active"),
            "active": snapshot.get("active", 0),
            "entries": entries,
        })

    def _operator_queue_cancel(self, path: str, body: dict) -> None:
        """取消尚未进入拍摄/处理的指定队列条目。"""
        services = self._queue_services()
        if services is None:
            return self._error(404, "not_found", path)
        from app.auto_flow.models import QueueStatus

        queue_id = unquote(path[len("/api/operator/queue/"):-len("/cancel")]).strip("/")
        entry = services.queue.get(queue_id)
        if entry is None:
            return self._json({"ok": False, "error": "queue_not_found",
                               "detail": "排队记录不存在。"}, 404)
        if entry.status not in {QueueStatus.QUEUED, QueueStatus.CALLED, QueueStatus.READY}:
            return self._json({"ok": False, "error": "queue_not_cancellable",
                               "detail": "该条目已经进入拍摄或处理阶段，不能取消。"}, 409)
        reason = str(body.get("reason") or "操作员取消排队").strip()[:120]
        cancelled = services.queue.mark_cancelled(queue_id, reason=reason)
        return self._json({"ok": True, "entry": cancelled.to_dict()})

    def _join_qr(self) -> None:
        _ensure_app_package_root()
        from app.auto_flow.http_handlers import QrUnavailableError, join_qr_png, public_join_url

        try:
            png = join_qr_png(public_join_url(public_base()))
        except QrUnavailableError as exc:
            return self._json({"ok": False, "error": "qr_unavailable",
                                "detail": str(exc)}, 503)
        return self._send(200, png, "image/png")

    # ---- 路由 ----------------------------------------------------------
    def do_GET(self):
        path = urlparse(self.path).path
        try:
            if path.startswith("/api/recovery/"):
                return self._recovery_get(path)
            if path == "/join":
                return self._html("join.html")
            if path in ("/", "/index.html"):
                # 默认页 = 竖屏（2026-10-09 定稿）：现场默认只有一块竖屏在工作，
                # 横屏大屏与操作员面板是工作人员主动打开的页面。
                return self._html("vertical.html")
            if path in ("/stage", "/stage/"):
                return self._html("stage.html")
            if path in ("/vertical", "/vertical/"):
                # 竖屏显示器的独立预览页：与 /stage（横屏算力视图）分工
                return self._html("vertical.html")
            if path in ("/operator", "/operator/", "/operator/queue", "/operator/photos",
                        "/operator/styles", "/operator/models", "/operator/settings", "/operator/system"):
                return self._html("operator.html")
            if path.startswith("/p/"):
                return self._html("pick.html")
            if path.startswith("/static/"):
                return self._static(path[len("/static/"):])
            if path.startswith("/assets/"):
                return self._static(path.lstrip("/"))
            if path == "/api/build":
                return self._json({"ok": True, "build": PAGE_BUILD})
            if path == "/api/join-qr.png":
                return self._join_qr()
            if path == "/api/state":
                return self._json(self.booth.state())
            if path == "/api/public/status":
                return self._json(self.booth.public_status())
            if path == "/api/operator/config":
                return self._json(self.booth.public_config())
            if path == "/api/operator/aigc/channels":
                return self._json(self.booth.aigc_channels.public_status())
            if path == "/api/operator/subject-rules":
                import subject_rules as _sr

                return self._json({"ok": True, **_sr.describe()})
            if path == "/api/operator/aigc/styles":
                return self._json({"ok": True, **self.booth.aigc_catalog()})
            if path == "/api/aigc/styles":
                return self._json(self.booth.public_aigc_styles())
            if path.startswith("/api/photo-meta/") and path.endswith(".json"):
                meta = self.booth.photo_meta(Path(path).stem)
                return self._json(meta, 200 if meta else 404)
            if path == "/api/overlay.json":
                return self._json(self.booth.overlay())
            if path.startswith("/api/job/"):
                job_id = path.rsplit("/", 1)[-1]
                job = self.booth.jobs.get(job_id)
                if job is not None:
                    return self._json(job.snapshot())
                # 旧 JobBoard 没有：可能是队列流程的作业（job_id == queue_id）。
                # 交给 T-16 处理器（含 X-Queue-Session 私人字段门控）。
                services = self._queue_services()
                if services is not None:
                    from app.auto_flow.http_handlers import handle_job_get

                    return self._queue_result(
                        handle_job_get,
                        job_id,
                        queue=services.queue,
                        sessions=services.sessions,
                        session=self.headers.get("X-Queue-Session"),
                        photo_service=services.photo_service,
                    )
                return self._json({"ok": False, "error": "unknown_job"}, 404)
            if path == "/api/operator/queue":
                return self._operator_queue()
            if path.startswith("/api/print/"):
                return self._print_get(path.rsplit("/", 1)[-1])
            if path.startswith("/api/queue/"):
                return self._queue_get(path)
            # 成片 / 缩略图 / 二维码 / 元信息：一律认**令牌**，不认编号。
            # 编号是连号的，认编号就等于把"全部照片"挂到公网上。
            photo_match = re.fullmatch(
                r"/api/photo/([A-Za-z0-9_-]{16,64})(\.clean)?\.jpg", path)
            if photo_match:
                token, clean_suffix = photo_match.groups()
                record = self.booth.store.by_token(token)
                photo = None
                if record is not None:
                    photo = (self.booth.store.clean_photo_path(record.id)
                             if clean_suffix else self.booth.store.photo_path(record.id))
                if photo is None or not photo.is_file():
                    return self._error(404, "gone", "照片不存在或已被删除")
                return self._file(photo, "image/jpeg")
            if path.startswith("/api/capture-preview/"):
                match = re.fullmatch(r"/api/capture-preview/([A-Za-z0-9_-]{22})\.jpg", path)
                job = self.booth.jobs.capture_preview(match.group(1)) if match else None
                photo = self.booth.store.capture_path(job.record_id) if job and job.record_id else None
                if photo is None or not photo.is_file():
                    return self._error(404, "gone", "抓拍预览不存在或已过期")
                return self._file(photo, "image/jpeg")
            if path.startswith("/api/thumb/") and path.endswith(".jpg"):
                record = self.booth.store.by_token(Path(path).stem)
                if record is None:
                    return self._error(404, "gone", "照片不存在或已被删除")
                return self._file(self.booth.store.thumb_path(record.id), "image/jpeg")
            if path.startswith("/api/mask/") and path.endswith(".png"):
                return self._file(self.booth.store.root / "masks" / Path(path).name, "image/png")
            if path.startswith("/api/job-preview/") and path.endswith(".jpg"):
                return self._file(self.booth.store.root / "previews" / Path(path).name, "image/jpeg")
            if path.startswith("/api/qr/") and path.endswith(".png"):
                return self._qr(Path(path).stem)
            if path == "/api/preview.mjpg":
                return self._preview()
            if path == "/api/health":
                components = {}
                # 手势组件健康
                if self.booth.gesture_port is not None:
                    try:
                        gh = self.booth.gesture_port.status()
                        components["gesture"] = {
                            "status": gh.status,
                            "reason": gh.reason,
                            "checked_at": gh.checked_at,
                        }
                    except Exception as exc:  # noqa: BLE001
                        components["gesture"] = {
                            "status": "error",
                            "reason": str(exc),
                            "checked_at": _now(),
                        }
                return self._json({"ok": True, "build": BUILD, "at": _now(), "components": components})
            if path == "/api/debug/gesture-probe":
                return self._gesture_debug_probe()
            return self._error(404, "not_found", path)
        except Exception as exc:
            log("GET %s 出错：%s" % (path, traceback.format_exc(limit=4)))
            return self._error(500, "internal", str(exc))

    def do_POST(self):
        path = urlparse(self.path).path
        if path == "/api/preview.whep":
            return self._preview_whep()
        multipart = self.headers.get("Content-Type", "").lower().startswith("multipart/form-data")
        upload_fields, upload_files = ({}, {})
        body = {}
        if multipart:
            try:
                upload_fields, upload_files = self._multipart()
            except ValueError as exc:
                return self._error(400, "invalid_upload", str(exc))
        else:
            body = self._body()
        try:
            if path == "/api/recovery/attach":
                return self._recovery_attach(body)
            if path == "/api/consent":
                # 修复：队列启用时使用 auto_flow.consent，否则使用旧的 self.booth.consent
                consent_service = (self.booth.auto_flow.consent
                                 if self.booth.auto_flow is not None
                                 else self.booth.consent)

                if not body.get("agree"):
                    return self._json({"ok": False, "error": "consent_declined"}, 200)

                # 队列的 ConsentService 和旧 ConsentBook 接口不同
                if hasattr(consent_service, 'record'):
                    # 新的 ConsentService（auto_flow）。它只认 capture /
                    # external_processing，而拍摄位页发的是旧式 kind:'photo' ——
                    # 直接透传会抛 ValueError（现场表现就是"同意登记不成功"）。
                    kind = str(body.get("kind") or "capture")
                    purpose = {"photo": "capture", "capture": "capture",
                               "external_processing": "external_processing"}.get(kind, kind)
                    try:
                        record = consent_service.record(
                            purpose=purpose, granted=True, subject_id="")
                    except ValueError as exc:
                        return self._json({"ok": False, "error": "invalid_purpose",
                                           "detail": str(exc)}, 400)
                    # 同一令牌也登记进旧账本，让 /api/capture 的校验认得它
                    # （两处不是同一存储，只写一边会让手动拍摄永远 403）。
                    try:
                        self.booth.consent.register(record.consent_id)
                    except Exception as exc:
                        log("登记旧同意账本失败（不影响队列流程）：%s" % exc)
                    granted = {
                        "token": record.consent_id,
                        "expires_in": CONSENT_TTL_S,
                        "kind": kind,
                        "purpose": record.purpose,
                        "retention": ConsentBook.RETENTION,
                        "version": 5,
                        "at": record.created_at,
                    }
                else:
                    # 旧的 ConsentBook
                    granted = consent_service.grant(str(body.get("kind") or "photo"))

                return self._json({"ok": True, **granted})
            if path == "/api/capture":
                token = self.headers.get("X-Booth-Session") or body.get("consent_token")
                if not self.booth.consent.check(token):
                    # 合规红线：未同意一律拦截，且不给任何可以绕过的提示
                    return self._json({"ok": False, "error": "consent_required",
                                       "detail": "拍摄前需要先获得参会者的单独同意"}, 403)
                portrait_mode = body.get("portrait_mode", "standard")
                if portrait_mode not in ("standard", "chibi"):
                    return self._json({"ok": False, "error": "invalid_request",
                                       "detail": "请选择写实版或Q版大头像"}, 400)
                ai_watermark = body.get("ai_watermark", True)
                if not isinstance(ai_watermark, bool):
                    return self._error(400, "invalid_watermark", "AI 标识选项无效")
                meta = {
                    "portrait_mode": portrait_mode,
                    "ai_watermark": ai_watermark,
                    "background": body.get("background"),
                    "orientation": "wide" if body.get("orientation") == "wide" else "vertical",
                    "name": str(body.get("name") or "")[:24],
                    "handle": str(body.get("handle") or "")[:32],
                    "seed": int(body.get("seed") or 7),
                    "mnemonic": body.get("mnemonic"),
                }
                job = self.booth.capture(body.get("style"), meta)
                return self._json({"ok": True, "job": job.id, "style": job.style,
                                   "portrait_mode": job.portrait_mode,
                                   "record_id": job.record_id, "poll": "/api/job/" + job.id}, 202)
            if path == "/api/queue":
                return self._queue_submit(path, body)
            if path.startswith("/api/operator/queue/") and path.endswith("/cancel"):
                return self._operator_queue_cancel(path, body)
            if path.startswith("/api/queue/") and path.endswith("/start"):
                return self._queue_start(path, body)
            if path == "/api/operator/config":
                try:
                    result = self.booth.public_config()
                    if "public_base" in body:
                        result = self.booth.set_public_config(body.get("public_base"))
                    if "presentation_mode" in body or "handoff_enabled" in body:
                        result = self.booth.set_presentation_config(
                            body.get("presentation_mode"),
                            body.get("handoff_enabled"),
                        )
                    return self._json(result)
                except ValueError as exc:
                    return self._json({"ok": False, "error": "bad_url",
                                       "detail": str(exc)}, 400)
            if path == "/api/operator/aigc/channels":
                try:
                    return self._json(self.booth.aigc_channels.save(body))
                except (ChannelConfigError, TypeError, ValueError) as exc:
                    return self._json({"ok": False, "error": "invalid_channels",
                                       "detail": str(exc)}, 400)
            if path == "/api/operator/aigc/channels/check":
                try:
                    return self._json(self.booth.aigc_channels.check(body.get("id")))
                except ChannelConfigError as exc:
                    return self._json({"ok": False, "error": "channel_not_found",
                                       "detail": str(exc)}, 404)
            if path == "/api/operator/aigc/channels/test":
                return self._operator_channel_test(upload_fields, upload_files)
            if path == "/api/operator/aigc/global-model":
                return self._error(410, "channel_model_required", "请在生图渠道中选择模型并保存")
            if path == "/api/operator/subject-rules":
                # 主体选择规则（谁算主体、AIGC 画布摆位）：现场按实景标定用。
                # 与 /api/operator/* 其余路由同一鉴权口径（当前无鉴权，仅内网）。
                import subject_rules as _sr

                try:
                    if "rules" in body and isinstance(body.get("rules"), dict):
                        return self._json({"ok": True, **{
                            "rules": _sr.write_rules(body["rules"]),
                            "note": "已保存并即时生效（不必重启服务）。",
                        }})
                    return self._json({"ok": True, **_sr.describe()})
                except Exception as exc:  # noqa: BLE001
                    return self._json({"ok": False, "error": "invalid_rules",
                                       "detail": str(exc)}, 400)
            if path == "/api/operator/aigc/styles":
                try:
                    if "styles" in body:
                        return self._json(self.booth.save_aigc_catalog(body.get("styles")))
                    return self._json(self.booth.update_aigc_style(None, body), 201)
                except (CatalogError, TypeError, ValueError) as exc:
                    return self._json({"ok": False, "error": getattr(exc, "code", "invalid_style"),
                                       "detail": str(exc)}, getattr(exc, "http_status", 400))
            if path.startswith("/api/operator/aigc/styles/"):
                suffix = path[len("/api/operator/aigc/styles/"):].strip("/")
                if suffix.endswith("/delete"):
                    style_id = unquote(suffix[:-len("/delete")])
                    try:
                        return self._json(self.booth.delete_aigc_style(style_id))
                    except CatalogError as exc:
                        return self._json({"ok": False, "error": getattr(exc, "code", "invalid_style"),
                                           "detail": str(exc)}, getattr(exc, "http_status", 400))
                if suffix.endswith("/rollback"):
                    style_id = suffix[:-len("/rollback")].strip("/")
                    try:
                        return self._json(self.booth.rollback_aigc_style(style_id, body.get("revision")))
                    except (CatalogError, TypeError, ValueError) as exc:
                        return self._json({"ok": False, "error": getattr(exc, "code", "invalid_style"),
                                           "detail": str(exc)}, getattr(exc, "http_status", 400))
                try:
                    return self._json(self.booth.update_aigc_style(unquote(suffix), body))
                except (CatalogError, TypeError, ValueError) as exc:
                    return self._json({"ok": False, "error": getattr(exc, "code", "invalid_style"),
                                       "detail": str(exc)}, getattr(exc, "http_status", 400))
            if path == "/api/operator/usb-reset":
                return self._json(self.booth.usb_reset_bus())
            if path == "/api/operator/restore":
                return self._json(self.booth.restore())
            if path == "/api/operator/purge":
                return self._json({"ok": True, **self.booth.purge()})
            if path == "/api/operator/reset":
                return self._json(self.booth.reset_for_event())
            if path == "/api/print":
                return self._print_submit(path, body)
            if path == "/api/operator/print":
                return self._queue_result(self._operator_print_submit, body)
            if path == "/api/operator/upload":
                return self._operator_upload(upload_fields, upload_files)
            if path == "/api/operator/photos/regenerate":
                try:
                    job = self.booth.submit_operator_regeneration(
                        body.get("token"), body.get("style"),
                        body.get("portrait_mode", "standard"), body.get("ai_watermark", True))
                except (TypeError, ValueError) as exc:
                    return self._error(400, "invalid_regeneration", str(exc))
                except RuntimeError as exc:
                    return self._error(409, "busy", str(exc))
                return self._json({"ok": True, "job": job.snapshot(),
                                   "record_id": job.record_id, "poll": "/api/job/" + job.id}, 202)
            if path == "/api/gesture/detect":
                return self._gesture_detect(body)
            return self._error(404, "not_found", path)
        except Exception as exc:
            log("POST %s 出错：%s" % (path, traceback.format_exc(limit=4)))
            return self._error(500, "internal", str(exc))

    def do_DELETE(self):
        path = urlparse(self.path).path
        try:
            if path.startswith("/api/photo/"):
                token = Path(path).stem
                # 令牌就是凭证：拿到这条链接的人才有删除权。
                # **必须走 by_token**：认编号的话，公网上任何人能删掉别人的照片。
                record = self.booth.store.by_token(token)
                if record is None:
                    return self._error(404, "gone", "照片不存在或已被删除")
                record_id = record.id
                removed = self.booth.store.delete(record_id)
                for stray in (self.booth.store.root / "qr" / ("%s.png" % token),
                              self.booth.store.root / "masks" / ("%s.png" % record_id)):
                    try:
                        stray.unlink(missing_ok=True)
                    except Exception:
                        pass
                return self._json({"ok": True, "id": record_id, "removed": removed})
            return self._error(404, "not_found", path)
        except Exception as exc:
            return self._error(500, "internal", str(exc))

    # ---- 打印（T-9） ----------------------------------------------------
    def _operator_channel_test(self, fields: dict, files: dict) -> None:
        """Generate one temporary preview through one exact AIGC channel."""
        from PIL import Image, ImageOps
        from aigc_client import AIGCError, edit_portrait_with_channel

        uploaded = files.get("image")
        if not uploaded:
            return self._error(400, "image_required", "请选择一张测试图片")
        channel_id = str(fields.get("id") or "").strip()
        requested_model = str(fields.get("model") or "").strip()
        try:
            channel = self.booth.aigc_channels.get(channel_id)
        except ChannelConfigError as exc:
            return self._error(404, "channel_not_found", str(exc))
        model = channel["model"]
        if requested_model and requested_model != model:
            return self._error(400, "invalid_model", "测试使用该渠道已保存的模型，请先保存渠道")
        _filename, raw, _content_type = uploaded
        if not raw or len(raw) > 20 * 1024 * 1024:
            return self._error(400, "invalid_image", "图片不能为空，且不能超过 20 MB")
        try:
            with Image.open(io.BytesIO(raw)) as source:
                if source.format not in ("JPEG", "PNG", "WEBP"):
                    raise ValueError("请选择 JPG、PNG 或 WEBP 图片")
                if source.width * source.height > 24_000_000:
                    raise ValueError("图片像素不能超过 2400 万")
                source.load()
                photo = ImageOps.exif_transpose(source).convert("RGB")
        except ValueError as exc:
            return self._error(400, "invalid_image", str(exc))
        except Exception:
            return self._error(400, "invalid_image", "无法读取这张图片")
        alpha = Image.new("L", photo.size, 255)
        prompt = (
            "Create a polished front-facing developer portrait from this reference. "
            "Preserve the person's identity, facial structure, hairstyle and original clothing. "
            "Use natural flattering light, a clean restrained background and no text or logos."
        )
        started = time.perf_counter()
        try:
            generated = edit_portrait_with_channel(
                photo, alpha, prompt=prompt, model=model, size="1024x1024",
                channel=channel, channels=self.booth.aigc_channels)
        except AIGCError as exc:
            return self._error(502, "generation_failed", str(exc))
        preview = io.BytesIO()
        generated.save(preview, "JPEG", quality=88, optimize=True)
        encoded = base64.b64encode(preview.getvalue()).decode("ascii")
        return self._json({
            "ok": True,
            "channel": {"id": channel["id"], "name": channel["name"]},
            "model": model,
            "elapsed_ms": round((time.perf_counter() - started) * 1000),
            "width": generated.width,
            "height": generated.height,
            "image": "data:image/jpeg;base64," + encoded,
        })

    def _operator_upload(self, fields: dict, files: dict) -> None:
        """后台图片入口：AIGC 风格或原片，生成后进入成片管理。"""
        uploaded = files.get("image")
        if not uploaded:
            return self._error(400, "image_required", "请选择一张图片")
        _filename, raw, _content_type = uploaded
        mode = str(fields.get("mode") or "aigc").strip().lower()
        if mode not in ("aigc", "original"):
            return self._error(400, "invalid_mode", "处理方式只能是 AIGC 或原片")
        orientation = "wide" if fields.get("orientation") == "wide" else "vertical"
        portrait_mode = fields.get("portrait_mode") if fields.get("portrait_mode") in ("standard", "chibi") else "standard"
        try:
            seed = int(fields.get("seed") or 7)
        except (TypeError, ValueError):
            return self._error(400, "invalid_seed", "图片参数无效，请重新提交")
        watermark_value = fields.get("ai_watermark", "true")
        if watermark_value not in ("true", "false"):
            return self._error(400, "invalid_watermark", "AI 标识选项无效")
        meta = {
            "orientation": orientation,
            "portrait_mode": portrait_mode,
            "ai_watermark": watermark_value == "true",
            "name": str(fields.get("name") or "")[:24],
            "handle": str(fields.get("handle") or "")[:32],
            "seed": seed,
            "mnemonic": None,
            "upload_mode": mode,
        }
        try:
            job = self.booth.submit_operator_upload(raw, fields.get("style"), meta)
        except (TypeError, ValueError) as exc:
            return self._error(400, "invalid_upload", str(exc))
        return self._json({"ok": True, "job": job.snapshot(),
                           "record_id": job.record_id,
                           "poll": "/api/job/" + job.id}, 202)

    def _print_submit(self, path: str, body: dict) -> None:
        """POST /api/print：提交打印（幂等键由适配器保证）。"""
        if self.booth.print_port is None:
            # 未启用：与未知路径同形太隐晦，这里明确 503 —— 调用方能区分
            # "没这功能"（404）和"有这功能但打印机没就绪"（503）。
            return self._queue_result(
                lambda: (_raise_print_unavailable()),
            )
        from app.auto_flow.http_handlers import handle_print_submit

        public_body = dict(body) if isinstance(body, dict) else body
        token = public_body.get("photo_token") if isinstance(public_body, dict) else None
        # 公共页面传来的 key 不作为权限凭据；服务端把同一照片固定成一个
        # 用户幂等键，避免换 key 绕过「每张照片只打印一次」。
        if isinstance(token, str) and token.strip():
            public_body["idempotency_key"] = "user:" + token.strip()
        return self._queue_result(
            handle_print_submit,
            public_body,
            photo_service=self._queue_services().photo_service
            if self._queue_services() is not None else None,
            print_port=self.booth.print_port,
        )

    def _operator_print_submit(self, body: dict) -> None:
        """POST /api/operator/print：后台按版本重复打印。"""
        from app.auto_flow import errors as auto_flow_errors

        if self.booth.print_port is None:
            raise auto_flow_errors.ApiError(auto_flow_errors.PRINT_UNAVAILABLE)
        if not isinstance(body, dict):
            raise auto_flow_errors.ApiError(auto_flow_errors.INVALID_REQUEST,
                                            "请求体必须是 JSON 对象。")
        token = body.get("photo_token")
        variant = str(body.get("variant") or "watermarked").strip().lower()
        if not isinstance(token, str) or not token.strip():
            raise auto_flow_errors.ApiError(auto_flow_errors.INVALID_REQUEST,
                                            "photo_token 必须是非空字符串。")
        if variant not in ("watermarked", "clean"):
            raise auto_flow_errors.ApiError(auto_flow_errors.INVALID_REQUEST,
                                            "variant 只能是 watermarked 或 clean。")
        token = token.strip()
        record = self.booth.store.by_token(token)
        if record is None or record.extra.get("tombstone"):
            raise auto_flow_errors.ApiError(auto_flow_errors.PHOTO_NOT_FOUND)
        source_path = self.booth._print_variant_path(token, variant)
        if source_path is None:
            raise auto_flow_errors.ApiError(auto_flow_errors.PHOTO_NOT_FOUND)
        key = str(body.get("idempotency_key") or "").strip()
        if not key:
            key = "operator:%s" % uuid.uuid4().hex
        job = self.booth.print_port.submit(token, key, source_path=source_path)
        return 202, {
            "print_job": job.to_dict() if callable(getattr(job, "to_dict", None)) else job,
            "variant": variant,
        }

    def _print_get(self, print_job_id: str) -> None:
        """GET /api/print/<id>：查询打印任务。"""
        if self.booth.print_port is None:
            return self._queue_result(lambda: (_raise_print_unavailable()))
        from app.auto_flow.http_handlers import handle_print_get

        return self._queue_result(handle_print_get, print_job_id,
                                  print_port=self.booth.print_port)

    # ---- 手势识别（T-手势） ---------------------------------------------
    def _gesture_detect(self, body: dict) -> None:
        """POST /api/gesture/detect：检测画面中的手势。"""
        if self.booth.gesture_port is None:
            return self._error(503, "GESTURE_UNAVAILABLE", "手势识别未启用")
        from auto_flow.http_handlers import handle_gesture_detect

        return self._queue_result(
            handle_gesture_detect,
            body,
            gesture_port=self.booth.gesture_port,
        )

    def _gesture_debug_probe(self) -> None:
        """GET /api/debug/gesture-probe[?path=/tmp/x.jpg]：原始分割结果，供阈值调试用。

        复用服务自己的 InferWorker（不另开 AI 核会话）。
        ?path= 指定本地文件时跳过相机帧，用于离线测试。
        """
        import tempfile, pathlib as _pl, time as _time
        from urllib.parse import parse_qs, urlparse as _up
        t0 = _time.time()

        qs = parse_qs(_up(self.path).query)
        test_path = (qs.get("path") or [None])[0]

        if test_path:
            # 离线模式：直接用指定文件，不检查 _busy（不抢相机帧）
            p = _pl.Path(test_path)
            if not p.exists():
                return self._json({"ok": False, "error": "file_not_found",
                                   "detail": test_path}, 404)
            seg_input = str(p)
            tmp = None
            frame_bytes = p.stat().st_size
        else:
            # 实时模式：抓预览帧
            if self.booth.infer._busy.is_set():
                return self._json({"ok": False, "error": "worker_busy",
                                   "detail": "InferWorker 正在处理其他请求，请 1–2 秒后重试"}, 503)
            jpeg = self.booth.preview_hub.latest_jpeg()
            if not jpeg:
                return self._json({"ok": False, "error": "no_frame",
                                   "detail": "PreviewHub 暂无帧（相机未就绪或门未开）"}, 503)
            with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as f:
                f.write(jpeg)
                tmp = f.name
            seg_input = tmp
            frame_bytes = len(jpeg)

        try:
            seg = self.booth.infer.seg(
                seg_input,
                prompts=["person", "ok hand gesture"],
                conf=0.10,
                mask=False, mask_max_side=64,
                matte=False, matte_max_side=0,
                timeout=20.0,
            )
        except Exception as exc:
            return self._json({"ok": False, "error": "seg_failed", "detail": str(exc)}, 500)
        finally:
            if not test_path and tmp:
                try: _pl.Path(tmp).unlink(missing_ok=True)
                except Exception: pass

        elapsed_ms = round((_time.time() - t0) * 1000)
        ok_prompt = os.environ.get("BOOTH_GESTURE_OK_PROMPT", "ok hand gesture")
        ok_min_score = float(os.environ.get("BOOTH_GESTURE_OK_SCORE", "0.20"))
        ok_min_side = float(os.environ.get("BOOTH_GESTURE_OK_MIN_SIDE", "12"))

        detections = []
        for det in (seg or {}).get("persons") or []:
            lbl = str(det.get("label") or "")
            score = float(det.get("score") or 0)
            box = [float(v) for v in (det.get("box") or [])[:4]]
            if len(box) < 4:
                continue
            side = min(box[2] - box[0], box[3] - box[1])
            is_ok_candidate = ok_prompt.lower() in lbl.lower()
            passed = is_ok_candidate and score >= ok_min_score and side >= ok_min_side
            detections.append({
                "label": lbl,
                "score": round(score, 4),
                "box": box,
                "short_side_px": round(side, 1),
                "passes_threshold": passed,
            })

        ok_passed = [d for d in detections if d["passes_threshold"]]
        return self._json({
            "ok": True,
            "elapsed_ms": elapsed_ms,
            "frame_bytes": frame_bytes,
            "source": test_path or "camera",
            "img_w": seg.get("width") or 0,
            "img_h": seg.get("height") or 0,
            "thresholds": {
                "ok_score": ok_min_score,
                "ok_min_side_px": ok_min_side,
                "conf_used": 0.10,
            },
            "detections": detections,
            "verdict": "ok" if ok_passed else "none",
            "ok_count": len(ok_passed),
            "person_count": sum(1 for d in detections if d["label"] == "person"),
        })

    # ---- 二维码 --------------------------------------------------------
    def _qr(self, token: str) -> None:
        """取图二维码。缓存文件名用**令牌**，编进二维码的也是令牌链接。"""
        record = self.booth.store.by_token(token)
        if record is None:
            return self._error(404, "gone", "照片不存在或已被删除")
        target = self.booth.store.root / "qr" / ("%s.png" % token)
        if not target.is_file():
            import importlib

            qr = importlib.import_module("render.qr")
            url = "%s/p/%s" % (public_base(), token)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(qr.make_qr_png(url, scale=10))
        self._file(target, "image/png")

    # ---- 预览流 --------------------------------------------------------
    def _preview_whep(self) -> None:
        """同源转发 WHEP SDP，避免浏览器跨端口/CORS 与 HTTPS 混合内容问题。"""
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = 0
        if length <= 0 or length > 64 * 1024:
            return self._error(400, "invalid_sdp", "WebRTC 协商内容无效")
        body = self.rfile.read(length)
        target = os.environ.get(
            "BOOTH_MEDIA_WHEP_URL", "http://127.0.0.1:8889/booth/whep")
        request = Request(target, data=body, method="POST", headers={
            "Content-Type": "application/sdp",
            "Accept": "application/sdp",
        })
        try:
            with urlopen(request, timeout=8.0) as response:
                answer = response.read(256 * 1024)
                status = int(response.status)
                content_type = response.headers.get(
                    "Content-Type", "application/sdp")
        except HTTPError as exc:
            detail = exc.read(4096)
            return self._send(exc.code, detail, "text/plain; charset=utf-8")
        except (URLError, TimeoutError, OSError) as exc:
            return self._error(503, "webrtc_unavailable", "WebRTC 视频服务未就绪：%s" % exc)
        return self._send(status, answer, content_type)

    def _preview(self) -> None:
        """发送低延迟 MJPEG；拥塞时只保留少量帧，恢复后直接追最新画面。"""
        send_buffer = max(64 * 1024, int(os.environ.get(
            "BOOTH_PREVIEW_SEND_BUFFER", str(128 * 1024))))
        try:
            self.connection.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, send_buffer)
            self.connection.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except OSError:
            pass
        self.send_response(200)
        self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        stop = threading.Event()
        try:
            for jpeg in self.booth.preview_hub.frames(stop):
                self.wfile.write(b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: "
                                 + str(len(jpeg)).encode() + b"\r\n\r\n" + jpeg + b"\r\n")
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            stop.set()


def main() -> int:
    import argparse
    import signal

    parser = argparse.ArgumentParser(description="SpacemiT DevCon 2026 装置5 控制面")
    parser.add_argument("--host", default=os.environ.get("BOOTH_HOST", "0.0.0.0"))
    parser.add_argument("--port", type=int, default=PORT)
    parser.add_argument("--mock-camera", action="store_true",
                        help="无相机时用合成源（现场排障/开发用）")
    args = parser.parse_args()

    if args.mock_camera:
        os.environ["BOOTH_CAMERA"] = "mock"

    booth = Booth()
    Handler.booth = booth
    (booth.store.root / "qr").mkdir(parents=True, exist_ok=True)
    (booth.store.root / "masks").mkdir(parents=True, exist_ok=True)
    log("数据目录 %s（留存 %d 小时）" % (booth.store.root, booth.store.retention_hours))

    if booth.auto_flow is not None:
        # 队列模式下排队/叫号不打开相机；用户点击「开始拍摄」后，
        # camera_health_probe 才打开抓帧门，由 PreviewHub 懒加载相机。
        log("队列待机：相机保持关闭，点击“开始拍摄”后开启取景")
    else:
        try:
            status = booth.camera.open()
            log("相机就绪：%s %dx%d %s %s" % (status.device, status.width, status.height,
                                              status.fourcc, status.detail))
        except Exception as exc:
            log("相机不可用（服务照常启动，抓帧时重试）：%s" % exc)

    booth.preload()
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    server.daemon_threads = True
    log("控制面已启动 http://%s:%d  （大屏 /stage · 操作员 /operator）"
        % (lan_ip(), args.port))

    retention_stop = threading.Event()

    def cleanup_expired():
        while not retention_stop.wait(60):
            try:
                result = booth.store.purge_expired()
                if result["removed_count"]:
                    log("已按 24 小时留存期清理 %d 张照片" % result["removed_count"])
            except Exception as exc:
                log("过期照片清理失败，将稍后重试：%s" % exc)

    threading.Thread(target=cleanup_expired, name="photo-retention", daemon=True).start()

    def shutdown(*_):
        log("收到退出信号，正在停止")
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)
    # 现场诊断开关：kill -USR1 <pid> 把全部线程的 Python 栈打到 stderr
    #（journalctl 可见）。排查"tick 线程卡死在哪个调用"用的；平时无副作用。
    try:
        import faulthandler

        signal.signal(signal.SIGUSR1, faulthandler.dump_traceback)
    except Exception:
        pass
    try:
        server.serve_forever(poll_interval=0.5)
    finally:
        retention_stop.set()
        server.server_close()
        booth.camera.close()
        booth.infer.stop()
        log("已停止")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
