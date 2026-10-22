# -*- coding: utf-8 -*-
"""打印适配器：把成片交给 CUPS 打印（T-9 的默认禁用实现是唯一安全态，本模块是它的真实替换）。

设计立场（与项目其余部分一致：诚实优先）：
- **有打印机才算可用**。装配时先探一次（`lpstat -p` 或配置的队列名），探不到就
  ``status()`` 返回 unavailable、``submit()`` 抛 503 PRINT_UNAVAILABLE —— 绝不
  给一个"已排入"的假象，现场拿到一张空白纸比拿到明确报错更糟。
- 幂等由本模块保证：同一个 idempotency_key 重复提交返回**同一个** PrintJob，
  不会打出第二张（现场误点两次不该浪费两张相纸）。
- 只走命令行的 `lp`/`lpstat`/`ipptool`，不 import 任何图像库、不碰 cv2 / onnxruntime，
  因此不受"cv2 与 spacemit_ort 不得同进程"的约束，可以安全跑在 booth_server 进程里。

配置（环境变量，缺省即禁用）：
  BOOTH_PRINT_ENABLED   true/1/yes 才启用
  BOOTH_PRINT_BACKEND   目前支持 "cups"（默认值即 cups）
  BOOTH_PRINT_QUEUE     CUPS 队列名；留空用系统默认打印机
  BOOTH_PRINT_MEDIA     纸张规格（如 4x6 / A6），留空用队列默认
  BOOTH_PRINT_SCALING   print-scaling 选项，默认 fit
  BOOTH_PRINT_STATE_FILE 单进程任务台账路径；留空不持久化
  BOOTH_PRINT_DEVICE_URI 打印机 IPP 地址；设置后提交前确认设备在线
  BOOTH_PRINT_EXPECTED_MEDIA 设备应报告的 media-ready，避免使用错误耗材
  BOOTH_PRINT_TIMEOUT   单次调 lp 的超时秒数（默认 30）
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

# 导入 errors/模型的方式要跟着调用方的导入方式走，否则同一个文件会被加载成
# 两个模块对象、ApiError 也就成了两个类 —— 抛出的异常穿不过调用方的 except
# （实测踩过：测试里 `except errors.ApiError` 抓不住本模块抛的）。
# 优先用包路径；booth_server 以脚本方式运行时（`python3 booth_server.py`，
# 此时 app/ 在 sys.path 上）退回顶层模块名。
try:
    from app.auto_flow import errors
    from app.auto_flow.models import ComponentHealth, PrintJob, to_utc_iso
except ImportError:  # pragma: no cover - 取决于启动方式
    from auto_flow import errors                                # type: ignore[no-redef]
    from auto_flow.models import (                              # type: ignore[no-redef]
        ComponentHealth, PrintJob, to_utc_iso)

__all__ = ["CupsPrintAdapter", "build_print_adapter"]

_UTC = timezone.utc


def _now_iso() -> str:
    return to_utc_iso(datetime.now(_UTC))


def _env_bool(name: str, default: bool = False) -> bool:
    raw = str(os.environ.get(name, "")).strip().lower()
    if not raw:
        return default
    return raw in ("true", "1", "yes")


def _run(cmd: list[str], timeout: float) -> tuple[int, str, str]:
    """跑一条命令，返回 (exit_code, stdout, stderr)；不看 returncode 之外的东西。

    不抛异常：调用方按退出码判断，异常一律折算成失败码 + 原因文本。
    """
    try:
        env = os.environ.copy()
        env["LC_ALL"] = "C"
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              timeout=timeout, check=False, env=env)
        return proc.returncode, (proc.stdout or "").strip(), (proc.stderr or "").strip()
    except subprocess.TimeoutExpired:
        return 124, "", "命令超时（%s）" % " ".join(cmd[:2])
    except Exception as exc:  # noqa: BLE001
        return 127, "", "%s: %s" % (type(exc).__name__, exc)


class CupsPrintAdapter:
    """基于本机 CUPS 命令行（lp / lpstat / ipptool）的打印端口实现。

    线程安全：submit 全程持锁 —— 打印是有序动作，且幂等表必须一致。
    """

    def __init__(self, *, photo_path=None, queue: str = "", media: str = "",
                 timeout: float = 30.0, enabled: bool = True, clock=None,
                 scaling: str = "fit", state_file=None, device_uri: str = "",
                 expected_media: str = ""):
        # photo_path: callable(photo_token) -> Path | None；由 Booth 注入，
        # 用来把令牌解析成磁盘文件。缺省时 submit 直接判不可用。
        self._photo_path = photo_path
        self.queue = str(queue or "").strip()
        self.media = str(media or "").strip()
        self.scaling = str(scaling or "fit").strip()
        self.device_uri = str(device_uri or "").strip()
        self.expected_media = str(expected_media or "").strip()
        self.timeout = max(1.0, float(timeout))
        self.enabled = bool(enabled)
        self._lock = threading.RLock()
        self._jobs: dict[str, PrintJob] = {}          # print_job_id -> job
        self._by_key: dict[str, str] = {}             # idempotency_key -> print_job_id
        self._reason: str | None = None               # 最近一次不可用的原因
        self._checked_at: float = 0.0
        self._cups_requests: dict[str, str] = {}
        self._state_file = Path(state_file).resolve() if state_file else None
        self._state_error: str | None = None
        self._load_state()

    def _load_state(self) -> None:
        """台账不可读时关闭新增打印，保留原文件供人工检查。"""
        if self._state_file is None:
            return
        try:
            try:
                content = self._state_file.read_text(encoding="utf-8")
            except FileNotFoundError:
                return
            data = json.loads(content)
            if data["version"] != 1:
                raise ValueError("不支持的台账版本")
            jobs = {}
            for job_id, raw in data["jobs"].items():
                job = PrintJob(**raw)
                if (job.print_job_id != job_id or not job_id
                        or job.status not in {"unavailable", "queued", "submitted", "completed", "failed"}
                        or not all(isinstance(value, str) and value for value in
                                   (job.photo_token, job.created_at, job.updated_at))
                        or (job.error_code is not None and not isinstance(job.error_code, str))):
                    raise ValueError("无效的打印任务")
                jobs[job_id] = job
            by_key = data["by_key"]
            requests = data["cups_requests"]
            if (not isinstance(by_key, dict) or not isinstance(requests, dict)
                    or any(not key or job_id not in jobs for key, job_id in by_key.items())
                    or set(by_key.values()) != set(jobs)
                    or len(by_key) != len(jobs)
                    or any(job_id not in jobs or not isinstance(request, str)
                           or not re.fullmatch(r"\S+-[1-9][0-9]*", request)
                           for job_id, request in requests.items())):
                raise ValueError("无效的幂等或 CUPS 记录")
            self._jobs, self._by_key, self._cups_requests = jobs, by_key, requests
        except Exception as exc:
            self._state_error = "打印台账读取失败：%s" % exc

    def _save_state(self) -> bool:
        """提交前预留幂等键并原子落盘；提交后的写入失败不得再次调用 lp。"""
        if self._state_error:
            return False
        if self._state_file is None:
            return True
        try:
            self._state_file.parent.mkdir(parents=True, exist_ok=True)
            data = {"version": 1,
                    "jobs": {job_id: job.to_dict() for job_id, job in self._jobs.items()},
                    "by_key": self._by_key, "cups_requests": self._cups_requests}
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", delete=False,
                                             dir=self._state_file.parent,
                                             prefix=self._state_file.name + ".") as temporary:
                json.dump(data, temporary, ensure_ascii=False)
                temporary.flush()
                os.fsync(temporary.fileno())
            os.replace(temporary.name, self._state_file)
            if os.name != "nt":
                directory = os.open(self._state_file.parent, os.O_RDONLY)
                try:
                    os.fsync(directory)
                finally:
                    os.close(directory)
            return True
        except Exception as exc:
            self._state_error = "打印台账写入失败：%s" % exc
            return False

    # ---- 探测 ----------------------------------------------------------
    def probe(self) -> tuple[bool, str]:
        """打印机是否真的可用。返回 (可用, 原因)。不做任何打印动作。"""
        if not self.enabled:
            return False, "打印未启用（BOOTH_PRINT_ENABLED 未开启）"
        if self._state_error:
            return False, self._state_error
        if shutil.which("lp") is None:
            return False, "找不到 lp 命令（未安装 CUPS 客户端）"
        if shutil.which("ipptool") is None:
            return False, "找不到 ipptool 命令（无法确认打印状态）"
        code, out, err = _run(["lpstat", "-h", "localhost:631", "-p", *([self.queue] if self.queue else [])],
                              timeout=min(15.0, self.timeout))
        if code != 0:
            return False, "lpstat 查询失败：%s" % (err or out or "exit %d" % code)
        text = out
        if not text:
            return False, "没有配置任何打印机队列"
        # lpstat -p 输出形如 "printer XXX is idle.  enabled since ..."
        if "disabled" in text and self.queue and self.queue in text:
            return False, "打印机队列已停用：%s" % self.queue
        if self.device_uri:
            return self._probe_device()
        return True, "CUPS 队列就绪：%s" % (self.queue or "系统默认")

    def _probe_device(self) -> tuple[bool, str]:
        specification = """{
  OPERATION Get-Printer-Attributes
  GROUP operation-attributes-tag
  ATTR charset attributes-charset utf-8
  ATTR naturalLanguage attributes-natural-language en
  ATTR uri printer-uri $uri
  ATTR keyword requested-attributes printer-state,printer-state-reasons,printer-is-accepting-jobs,media-ready
  STATUS successful-ok
  EXPECT printer-state OF-TYPE enum
  EXPECT printer-is-accepting-jobs OF-TYPE boolean
  EXPECT media-ready
}
"""
        try:
            with tempfile.TemporaryDirectory(prefix="booth-printer-") as directory:
                test_path = Path(directory) / "get-printer.test"
                test_path.write_text(specification, encoding="utf-8")
                code, out, err = _run(["ipptool", "-tv", self.device_uri, str(test_path)],
                                      timeout=min(5.0, self.timeout))
        except Exception as exc:
            return False, "无法确认打印机状态：%s" % exc
        if code != 0:
            return False, "打印机未连接或状态不可读"
        state = re.search(r"(?m)^\s*printer-state \(enum\) = (\S+)", out)
        if state is None or state.group(1) not in ("idle", "processing", "3", "4"):
            return False, "打印机已暂停，请检查耗材或设备提示"
        if not re.search(r"(?m)^\s*printer-is-accepting-jobs \(boolean\) = true\s*$", out):
            return False, "打印机暂不接受任务"
        media = re.search(r"(?m)^\s*media-ready \([^\n]*?\) = ([^\n]+)", out)
        if self.expected_media and (media is None or self.expected_media not in
                                    [item.strip() for item in media.group(1).split(",")]):
            label = {"om_card_54x86mm": "54×86mm 卡片纸",
                     "jpn_hagaki_100x148mm": "100×148mm 相纸"}.get(
                         self.expected_media, "配置对应的相纸")
            return False, "相纸规格不匹配，请安装 " + label
        reasons = re.search(r"(?m)^\s*printer-state-reasons \([^\n]*?\) = ([^\n]+)", out)
        if reasons and any(term in reasons.group(1) for term in
                           ("media-empty", "media-needed", "media-jam", "cover-open", "marker-supply-empty")):
            return False, "打印机需要处理，请检查相纸、色带和仓盖"
        return True, "打印机在线且相纸就绪"

    def _refresh(self) -> None:
        ok, reason = self.probe()
        self._reason = None if ok else reason
        self._checked_at = time.time()
        self._available = ok

    # ---- 端口契约 ------------------------------------------------------
    def status(self) -> ComponentHealth:
        """打印组件健康。未启用/无设备时如实 unavailable，不伪报 ok。"""
        with self._lock:
            self._refresh()
            return ComponentHealth(
                status="ok" if self._available else "unavailable",
                checked_at=_now_iso(),
                reason=self._reason,
            )

    def submit(self, photo_token: str, idempotency_key: str, source_path=None) -> PrintJob:
        """提交打印。不可用一律 503；同幂等键重复提交返回同一个任务。"""
        if not isinstance(photo_token, str) or not photo_token.strip():
            raise errors.ApiError(errors.INVALID_REQUEST, "photo_token 必须是非空字符串。")
        key = str(idempotency_key or "").strip()
        if not key:
            raise errors.ApiError(errors.INVALID_REQUEST, "idempotency_key 必须是非空字符串。")

        with self._lock:
            existing = self._by_key.get(key)
            if existing is not None:
                job = self._jobs.get(existing)
                if job is not None and job.photo_token == photo_token:
                    return job          # 幂等重放：不重复打印
                raise errors.ApiError(errors.IDEMPOTENCY_CONFLICT)

            self._refresh()
            if not self._available:
                raise errors.ApiError(errors.PRINT_UNAVAILABLE, self._reason or "打印不可用")

            path = source_path
            if path is None and callable(self._photo_path):
                try:
                    path = self._photo_path(photo_token)
                except Exception:
                    path = None
            if path is None:
                raise errors.ApiError(errors.PHOTO_NOT_FOUND)
            path = str(path)
            if not os.path.isfile(path):
                raise errors.ApiError(errors.PHOTO_NOT_FOUND)

            job_id = uuid.uuid4().hex[:12]
            created = _now_iso()
            job = PrintJob(print_job_id=job_id, photo_token=photo_token,
                           status="queued", created_at=created, updated_at=created,
                           error_code="PRINT_SUBMISSION_UNKNOWN")
            self._jobs[job_id] = job
            self._by_key[key] = job_id
            if not self._save_state():
                job.status = "failed"
                job.error_code = "PRINT_STATE_UNAVAILABLE"
                raise errors.ApiError(errors.PRINT_UNAVAILABLE, self._state_error)

            cmd = ["lp", "-h", "localhost:631", "-n", "1"]
            if self.queue:
                cmd += ["-d", self.queue]
            if self.media:
                cmd += ["-o", "media=" + self.media]
            cmd += ["-o", "print-scaling=" + self.scaling, "--", path]
            code, out, err = _run(cmd, timeout=self.timeout)
            request = re.search(r"(?m)^request id is (\S+-[1-9][0-9]*)(?=\s|$)", out)
            if request:
                self._cups_requests[job_id] = request.group(1)

            if code == 0 and request:
                job.status = "submitted"
                job.error_code = None
            else:
                job.status = "failed"
                job.error_code = "PRINT_SUBMISSION_UNKNOWN" if code in (0, 124) or request else "PRINT_FAILED"
                self._reason = "lp 提交失败：%s" % (err or out or "exit %d" % code)

            job.updated_at = _now_iso()
            if not self._save_state():
                job.error_code = "PRINT_STATE_UNAVAILABLE"
            return job

    def get(self, print_job_id: str) -> PrintJob | None:
        """查询打印任务；未知返回 None（路由层转 404）。"""
        with self._lock:
            job = self._jobs.get(str(print_job_id or "").strip())
            if job is None or job.status == "completed":
                return job
            if job.status == "failed" and job.error_code == "PRINT_FAILED":
                return job
            request = self._cups_requests.get(job.print_job_id)
            if request is None:
                return job
            job_number = request.rsplit("-", 1)[1]
            specification = """{
  OPERATION Get-Job-Attributes
  GROUP operation-attributes-tag
  ATTR charset attributes-charset utf-8
  ATTR naturalLanguage attributes-natural-language en
  ATTR uri job-uri $uri
  ATTR name requesting-user-name $user
  ATTR keyword requested-attributes job-id,job-state
  STATUS successful-ok
  EXPECT job-id OF-TYPE integer WITH-VALUE %s
  EXPECT job-state OF-TYPE enum
}
""" % job_number
            try:
                with tempfile.TemporaryDirectory(prefix="booth-print-") as directory:
                    test_path = Path(directory) / "get-job.test"
                    test_path.write_text(specification, encoding="utf-8")
                    code, out, err = _run(
                        ["ipptool", "-tv", "ipp://localhost:631/jobs/" + job_number, str(test_path)],
                        timeout=self.timeout)
            except Exception as exc:
                code, out, err = 127, "", str(exc)
            match = re.search(r"(?m)^\s*job-state\s+\(enum\)\s*=\s*([^\s,]+)", out)
            states = {"3": "queued", "pending": "queued", "4": "queued", "held": "queued",
                      "pending-held": "queued",
                      "5": "submitted", "processing": "submitted", "6": "submitted",
                      "processing-stopped": "submitted", "7": "failed", "canceled": "failed",
                      "8": "failed", "aborted": "failed", "9": "completed", "completed": "completed"}
            state = states.get(match.group(1)) if match else None
            if code == 0 and state is not None:
                job.status = state
                job.error_code = "PRINT_FAILED" if state == "failed" else None
            else:
                job.error_code = "PRINT_STATUS_UNAVAILABLE"
                self._reason = "打印状态查询失败：%s" % (err or out or "exit %d" % code)
            job.updated_at = _now_iso()
            if not self._save_state():
                job.error_code = "PRINT_STATE_UNAVAILABLE"
            return job


def build_print_adapter(*, photo_path=None, clock=None):
    """按环境变量装配打印端口；未启用时返回 None（路由层按 PRINT_UNAVAILABLE 处理）。

    返回 None 而不是一个"永远 503 的适配器"，是为了让调用点能一眼看出"这扇门关着"。
    """
    if not _env_bool("BOOTH_PRINT_ENABLED", False):
        return None
    backend = str(os.environ.get("BOOTH_PRINT_BACKEND", "cups")).strip().lower() or "cups"
    if backend != "cups":
        raise ValueError("不支持的打印后端：%r（目前只有 cups）" % backend)
    return CupsPrintAdapter(
        photo_path=photo_path,
        queue=str(os.environ.get("BOOTH_PRINT_QUEUE", "")).strip(),
        media=str(os.environ.get("BOOTH_PRINT_MEDIA", "")).strip(),
        scaling=str(os.environ.get("BOOTH_PRINT_SCALING", "fit")).strip(),
        state_file=str(os.environ.get("BOOTH_PRINT_STATE_FILE", "")).strip() or None,
        device_uri=str(os.environ.get("BOOTH_PRINT_DEVICE_URI", "")).strip(),
        expected_media=str(os.environ.get("BOOTH_PRINT_EXPECTED_MEDIA", "")).strip(),
        timeout=float(os.environ.get("BOOTH_PRINT_TIMEOUT", "30")),
        enabled=True,
        clock=clock,
    )
