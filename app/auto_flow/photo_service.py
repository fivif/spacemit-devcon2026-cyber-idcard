# -*- coding: utf-8 -*-
"""取图照片服务：令牌寻址的持久化、原子失效与打印取消（T-7）。

规格依据：`自动流程规格 v1（内部文档）`「Components · PhotoService」「Data models ·
PhotoRecord」「Interfaces · Internal module APIs」「Error handling」「Security」；
任务合同 `自动流程规格 v1（内部文档）` T-7。

文件布局（**根目录参数化**：生产由接线方传真实数据根，测试传系统临时目录）::

    <root>/photos/<token>.<ext>     # 成片（L0 或 styled）
    <root>/thumbs/<token>.jpg       # 缩略图（480×600 上限，质量 88，与既有成片缩略图同口径）
    <root>/qr/<token>.png           # 取图二维码（编码 <public_base>/p/<token>）
    <root>/state/photo-store.jsonl  # 追加式台账：photo / delete 两类事件（fsync 后返回）
    <root>/state/trash/             # 删除时的中转目录（见下）

删除语义（本实现的选择与理由）：
- **墓碑先行**：``begin_delete`` 先在内存与台账里写下墓碑（令牌与作业 ID 一起进
  撤销集合，台账 fsync）。任何读者从这一刻起拿不到照片：``get`` 返回 None，
  ``require`` 抛 410 PHOTO_DELETED；
- **再原子挪走文件**：照片、缩略图、二维码先 ``os.replace`` 挪进 ``state/trash/``
  （同一卷内原子换名，「按路径取图」的那一层不会看到删到一半的目录），再尽力删除；
  删不掉的残余留在 trash 并在 ``leftovers`` 里记账，绝不回到可取图目录；
- **晚到结果不复活**：``commit`` 与 ``begin_delete`` 共用同一把锁，提交在同一临界区里
  先查撤销集合再落盘；删除之后提交同一作业的结果一律拒绝（``PhotoRevokedError``，
  410 PHOTO_DELETED）。反向顺序（提交先完成）也不会漏：删除按 **job_id** 归一，
  把该作业名下的**全部**令牌一起失效并清文件；
- **重复删除幂等**：第二次返回 False，不重建任何文件；
- **并发删除进行中**：第二个删除调用抛 409 DELETE_IN_PROGRESS（``begin_delete``
  通过 ``_deleting`` 集合识别）。

打印取消钩子（T-9 之后接线）：``PhotoService.delete`` 在墓碑生效之后、文件清理之前
调用 ``print_cancel(photo_token)``，把「已排队但未提交」的打印任务先撤下；钩子异常
不阻断失效（照片已不可达），错误记入 ``print_cancel_errors`` 供诊断。

部署门控（T-7 逐字要求）：
- 生产物理删除在保留 / 备份策略未批准前**阻止部署删除能力**：``PhotoService`` 默认
  ``delete_approved=False, testing=False``，``delete()`` 抛 ``DeleteNotApprovedError``，
  文件原样保留；只有显式批准（构造参数或 ``BOOTH_PHOTO_DELETE_APPROVED`` 环境标志）
  或测试语义（``testing=True``）才放行。默认值方向是「拒绝」，生产漏配不会误删；
- 测试只操作临时合成数据：本模块不自带夹具，测试把根目录指向系统临时目录即可。

并发边界：线程安全以进程内 RLock 保护；现场只有一个服务进程写照片目录，跨进程
并发不在本模块契约内（与 QueueStore 的约定一致）。本模块不打印日志；``delete_approved_from_env``
只解析传入或进程的环境映射，不修改环境。
"""

from __future__ import annotations

import importlib
import json
import os
import shutil
import threading
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from PIL import Image, ImageOps

from app.auto_flow import errors
from app.auto_flow.errors import ApiError
from app.auto_flow.models import PHOTO_VARIANTS, PhotoRecord, to_utc_iso
from app.auto_flow.token_service import TokenService

__all__ = [
    "ENV_DELETE_APPROVED",
    "DeleteNotApprovedError",
    "DeleteTicket",
    "FilePhotoStore",
    "PhotoRevokedError",
    "PhotoService",
    "PhotoStoreError",
    "delete_approved_from_env",
]

# 物理删除能力的显式部署标志（值 true/1/yes，大小写不敏感；未设置即未批准）。
# 任务要求「生产物理删除及备份策略未批准前阻止部署删除能力」，这里选择
# **显式白名单**而不是「有保留策略就放行」——备份策略批准与否无法从保留天数推断。
ENV_DELETE_APPROVED = "BOOTH_PHOTO_DELETE_APPROVED"

_TRUE_VALUES = frozenset({"true", "1", "yes"})
_THUMB_SIZE = (480, 600)
_THUMB_QUALITY = 88


class PhotoStoreError(RuntimeError):
    """照片存储内部错误：台账损坏、二维码模块缺失、落盘失败等。"""


class PhotoRevokedError(ApiError):
    """晚到结果命中已删除的作业：拒绝落盘（410 PHOTO_DELETED）。"""

    def __init__(self, job_id: str, message: str | None = None) -> None:
        super().__init__(
            errors.PHOTO_DELETED,
            message or f"作业 {job_id} 的照片已被删除，晚到结果不再保留。",
        )


class DeleteNotApprovedError(ApiError):
    """删除能力未获批准：保留 / 备份策略批准前，生产环境不部署物理删除。"""

    def __init__(self, message: str | None = None) -> None:
        super().__init__(
            errors.INTERNAL_ERROR,
            message
            or "物理删除未获批准：保留与备份策略批准前，生产环境不部署删除能力。",
        )


def delete_approved_from_env(env: Mapping[str, str] | None = None) -> bool:
    """读取显式删除批准标志；未设置或非法值一律 False（失败方向是拒绝）。"""
    source = os.environ if env is None else env
    raw = source.get(ENV_DELETE_APPROVED)
    if raw is None:
        return False
    return str(raw).strip().lower() in _TRUE_VALUES


@dataclass(frozen=True)
class DeleteTicket:
    """一次已生效的删除：墓碑已写下，等待 :meth:`FilePhotoStore.finish_delete` 清文件。"""

    token: str
    job_id: str
    tokens: tuple[str, ...]
    paths: tuple[Path, ...]


def _unlink_quiet(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


def _atomic_write_bytes(target: Path, data: bytes) -> None:
    tmp = target.with_name(target.name + ".part")
    try:
        tmp.write_bytes(data)
        os.replace(tmp, target)
    finally:
        _unlink_quiet(tmp)


def _make_qr_png(url: str) -> bytes:
    """渲染取图二维码 PNG。仓库自带纯标准库实现（app/render/qr.py），两种导入路径都试。"""
    module = None
    for name in ("app.render.qr", "render.qr"):
        try:
            module = importlib.import_module(name)
            break
        except ImportError:
            module = None
    if module is None:
        raise PhotoStoreError("二维码模块不可用（app/render/qr.py 未找到），拒绝落盘无二维码的取图记录")
    return module.make_qr_png(url, scale=10)


class FilePhotoStore:
    """文件系统照片存储：实现 T-14 的 ``PhotoStorePort``（``commit``）并承担失效与台账。

    构造参数：
    - ``root``：数据根（参数化）；生产传真实数据根，测试传临时目录；
    - ``token_service``：令牌签发器；默认新建 ``TokenService``；
    - ``clock``：实现 ``now() -> datetime`` 的时钟（T-14 ``Clock`` 端口）；默认系统 UTC；
    - ``qr_factory``：``url -> PNG bytes``；默认仓库自带二维码编码器；测试可注入假实现；
    - ``public_base``：二维码里编的取图地址前缀（如 ``http://192.0.2.10:8090``）；默认空
      （编码相对路径 ``/p/<token>``，同样只包含令牌）；
    - ``remove_hook``：删除前对每个待清理路径调用一次（测试注入用，生产保持 None）。
    """

    PHOTOS_DIR = "photos"
    THUMBS_DIR = "thumbs"
    QR_DIR = "qr"
    STATE_DIR = "state"
    TRASH_DIR = "trash"
    JOURNAL_NAME = "photo-store.jsonl"

    def __init__(
        self,
        root: str | Path,
        token_service: TokenService | None = None,
        clock: object | None = None,
        *,
        qr_factory: Callable[[str], bytes] | None = None,
        public_base: str = "",
        remove_hook: Callable[[Path], None] | None = None,
    ) -> None:
        self.root = Path(root)
        self.photos_dir = self.root / self.PHOTOS_DIR
        self.thumbs_dir = self.root / self.THUMBS_DIR
        self.qr_dir = self.root / self.QR_DIR
        self.state_dir = self.root / self.STATE_DIR
        self.trash_dir = self.state_dir / self.TRASH_DIR
        self.journal_path = self.state_dir / self.JOURNAL_NAME
        for folder in (self.photos_dir, self.thumbs_dir, self.qr_dir, self.trash_dir):
            folder.mkdir(parents=True, exist_ok=True)
        self.token_service = token_service or TokenService()
        self.clock = clock
        self.qr_factory = qr_factory or _make_qr_png
        self.public_base = str(public_base or "").rstrip("/")
        self.remove_hook = remove_hook
        self._lock = threading.RLock()
        self._records: dict[str, PhotoRecord] = {}
        self._paths: dict[str, dict[str, Path]] = {}
        self._revoked_tokens: set[str] = set()
        self._revoked_jobs: set[str] = set()
        self._deleting: set[str] = set()
        self.leftovers: list[str] = []
        self._load_journal()

    # ---- 端口实现：commit --------------------------------------------------

    def commit(
        self,
        job_id: str,
        source_path: str,
        variant: str,
        *,
        display_number: str | None = None,
    ) -> PhotoRecord:
        """落盘一份可取图记录并返回 ``PhotoRecord``（端口签名 + 只用于展示的编号）。

        删除之后提交同一 ``job_id`` 的结果会抛 ``PhotoRevokedError``（410），
        保证并发晚到的结果不会把已删除的照片复活。
        """
        job = str(job_id)
        if variant not in PHOTO_VARIANTS:
            raise ValueError(f"非法 variant：{variant!r}（允许 {sorted(PHOTO_VARIANTS)}）")
        source = Path(source_path)
        if not source.is_file():
            raise FileNotFoundError(f"源图不存在：{source}")
        number = None if display_number is None else str(display_number)
        with self._lock:
            if job in self._revoked_jobs:
                raise PhotoRevokedError(job)
            token = self._issue_token_locked()
            suffix = source.suffix.lower() or ".jpg"
            photo_path = self.photos_dir / f"{token}{suffix}"
            thumb_path = self.thumbs_dir / f"{token}.jpg"
            qr_path = self.qr_dir / f"{token}.png"
            self._atomic_copy(source, photo_path)
            try:
                self._write_thumbnail(photo_path, thumb_path)
                self._write_qr(token, qr_path)
            except BaseException:
                for path in (photo_path, thumb_path, qr_path):
                    _unlink_quiet(path)
                raise
            record = PhotoRecord(
                photo_token=token,
                job_id=job,
                created_at=self._now_iso(),
                status="available",
                variant=variant,
                display_number=number,
            )
            self._records[token] = record
            self._paths[token] = {"photo": photo_path, "thumb": thumb_path, "qr": qr_path}
            self._journal(
                {
                    "type": "photo",
                    "photo_token": token,
                    "job_id": job,
                    "created_at": record.created_at,
                    "variant": variant,
                    "display_number": number,
                    "photo_path": str(photo_path),
                    "thumb_path": str(thumb_path),
                    "qr_path": str(qr_path),
                }
            )
            return record

    # ---- 查询 --------------------------------------------------------------

    def find(self, token: object) -> PhotoRecord | None:
        """可取图记录（令牌格式由调用方校验）；不存在或已删除返回 None。"""
        if not isinstance(token, str) or not token:
            return None
        with self._lock:
            record = self._records.get(token)
            if record is None or record.status != "available":
                return None
            path = self._paths.get(token, {}).get("photo")
        if path is not None and path.is_file():
            return record
        return None

    def state_of(self, token: str) -> str:
        """令牌状态：``available`` / ``deleted`` / ``unknown``（路由用它区分 404/410）。"""
        with self._lock:
            if token in self._revoked_tokens:
                return "deleted"
            record = self._records.get(token)
            if record is None:
                return "unknown"
            if record.status != "available":
                return "deleted"
            path = self._paths.get(token, {}).get("photo")
        return "available" if path is not None and path.is_file() else "deleted"

    def photo_path(self, token: str) -> Path | None:
        return self._path_for(token, "photo")

    def thumb_path(self, token: str) -> Path | None:
        return self._path_for(token, "thumb")

    def qr_path(self, token: str) -> Path | None:
        return self._path_for(token, "qr")

    def paths_for(self, token: str) -> dict[str, Path] | None:
        if self.find(token) is None:
            return None
        with self._lock:
            return dict(self._paths.get(token, {}))

    def records(self) -> list[PhotoRecord]:
        """全部记录（含已删除墓碑，供诊断与测试断言）。"""
        with self._lock:
            return list(self._records.values())

    def is_revoked(self, token: str) -> bool:
        with self._lock:
            return token in self._revoked_tokens

    def is_job_revoked(self, job_id: str) -> bool:
        with self._lock:
            return str(job_id) in self._revoked_jobs

    # ---- 删除：墓碑先行 + 原子挪走文件 --------------------------------------

    def begin_delete(self, token: str) -> DeleteTicket | None:
        """写下墓碑并冻结本作业的全部令牌；未知 / 已删除返回 None。

        墓碑（内存撤销集合 + 台账 fsync）先于任何文件操作生效，所以调用方在
        ``finish_delete`` 之前就可以把「删除已生效」告诉参与者。同一令牌的
       删除正在进行中时抛 ``DELETE_IN_PROGRESS``（409）。
        """
        with self._lock:
            if token in self._deleting:
                raise ApiError(errors.DELETE_IN_PROGRESS)
            record = self._records.get(token)
            if record is None or record.status != "available":
                return None
            job_id = record.job_id
            tokens = tuple(t for t, item in self._records.items() if item.job_id == job_id)
            self._deleting.add(token)
            self._revoked_jobs.add(job_id)
            for item in tokens:
                self._revoked_tokens.add(item)
                self._records[item].status = "deleted"
            paths = tuple(
                path
                for item in tokens
                for path in self._paths.get(item, {}).values()
                if path.is_file()
            )
            self._journal(
                {
                    "type": "delete",
                    "photo_token": token,
                    "job_id": job_id,
                    "tokens": list(tokens),
                    "at": self._now_iso(),
                }
            )
            return DeleteTicket(token=token, job_id=job_id, tokens=tokens, paths=paths)

    def finish_delete(self, ticket: DeleteTicket) -> None:
        """清理墓碑对应的文件：原子挪进 trash 后删除；残余进 ``leftovers`` 记账。"""
        try:
            for path in ticket.paths:
                if self.remove_hook is not None:
                    self.remove_hook(path)
                self._retire(path)
        finally:
            with self._lock:
                self._deleting.discard(ticket.token)

    def revoke(self, token: str) -> bool:
        """一步删除（不经过打印取消钩子）；供存储级测试与内部使用。"""
        ticket = self.begin_delete(token)
        if ticket is None:
            return False
        self.finish_delete(ticket)
        return True

    # ---- 内部：文件与台账 --------------------------------------------------

    def _path_for(self, token: str, kind: str) -> Path | None:
        if self.find(token) is None:
            return None
        with self._lock:
            return self._paths.get(token, {}).get(kind)

    def _issue_token_locked(self) -> str:
        for _ in range(8):
            token = self.token_service.issue()
            if token not in self._records:
                return token
        raise PhotoStoreError("连续 8 次签发的令牌都与既有记录冲突，存储状态异常")

    def _now_iso(self) -> str:
        if self.clock is None:
            return to_utc_iso(datetime.now(timezone.utc))
        return to_utc_iso(self.clock.now())  # type: ignore[attr-defined]

    def _atomic_copy(self, source: Path, target: Path) -> None:
        tmp = target.with_name(target.name + ".part")
        try:
            shutil.copy2(source, tmp)
            os.replace(tmp, target)
        finally:
            _unlink_quiet(tmp)

    def _write_thumbnail(self, source: Path, target: Path) -> None:
        with Image.open(source) as opened:
            image = ImageOps.exif_transpose(opened) or opened
            thumb = image.copy()
        thumb.thumbnail(_THUMB_SIZE)
        if thumb.mode not in ("RGB", "L"):
            thumb = thumb.convert("RGB")
        tmp = target.with_name(target.name + ".part")
        try:
            thumb.save(tmp, "JPEG", quality=_THUMB_QUALITY)
            os.replace(tmp, target)
        finally:
            _unlink_quiet(tmp)

    def _write_qr(self, token: str, target: Path) -> None:
        data = self.qr_factory(f"{self.public_base}/p/{token}")
        if not isinstance(data, (bytes, bytearray)):
            raise PhotoStoreError(f"二维码渲染器返回的类型非法：{type(data).__name__}")
        _atomic_write_bytes(target, bytes(data))

    def _retire(self, path: Path) -> None:
        """把文件挪进 trash（原子）后删除；任何一步失败都记账，绝不还原到可取图目录。"""
        if not path.is_file():
            return
        target = path
        try:
            moved = self.trash_dir / f"{uuid.uuid4().hex[:8]}-{path.name}"
            os.replace(path, moved)
            target = moved
        except OSError:
            target = path  # 同卷内不该失败（Windows 文件被占用等）；退回直接删原路径
        try:
            target.unlink(missing_ok=True)
        except OSError:
            self.leftovers.append(str(target))

    def _journal(self, entry: dict) -> None:
        line = json.dumps(entry, ensure_ascii=False, sort_keys=True)
        with self.journal_path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
            handle.flush()
            os.fsync(handle.fileno())

    def _load_journal(self) -> None:
        if not self.journal_path.is_file():
            return
        try:
            text = self.journal_path.read_text(encoding="utf-8")
        except OSError as exc:
            raise PhotoStoreError(f"照片台账不可读：{self.journal_path}（{exc}）") from exc
        for lineno, line in enumerate(text.splitlines(), 1):
            if not line.strip():
                continue
            try:
                entry = json.loads(line)
            except ValueError as exc:
                raise PhotoStoreError(f"照片台账第 {lineno} 行不是合法 JSON：{exc}") from exc
            if not isinstance(entry, dict):
                raise PhotoStoreError(f"照片台账第 {lineno} 行不是 JSON 对象")
            kind = entry.get("type")
            if kind == "photo":
                self._load_photo_entry(entry, lineno)
            elif kind == "delete":
                token = str(entry.get("photo_token") or "")
                job_id = str(entry.get("job_id") or "")
                self._revoked_tokens.add(token)
                self._revoked_jobs.add(job_id)
                self._deleting.discard(token)
                record = self._records.get(token)
                if record is not None:
                    record.status = "deleted"
            else:
                raise PhotoStoreError(f"照片台账第 {lineno} 行事件类型未知：{kind!r}")
        # 台账说可读、照片文件却不在（外部清理 / 断电残留）：一律按已删除处理，失败方向是拒绝
        for token, record in self._records.items():
            if record.status != "available":
                continue
            path = self._paths.get(token, {}).get("photo")
            if path is None or not path.is_file():
                record.status = "deleted"
                self._revoked_tokens.add(token)

    def _load_photo_entry(self, entry: dict, lineno: int) -> None:
        token = str(entry.get("photo_token") or "")
        if not self.token_service.validate(token):
            raise PhotoStoreError(f"照片台账第 {lineno} 行令牌非法：{token!r}")
        variant = entry.get("variant")
        if variant not in PHOTO_VARIANTS:
            raise PhotoStoreError(f"照片台账第 {lineno} 行 variant 非法：{variant!r}")
        record = PhotoRecord(
            photo_token=token,
            job_id=str(entry.get("job_id") or ""),
            created_at=str(entry.get("created_at") or ""),
            status="available",
            variant=variant,
            display_number=entry.get("display_number"),
        )
        self._records[token] = record
        self._paths[token] = {
            "photo": Path(str(entry.get("photo_path") or "")),
            "thumb": Path(str(entry.get("thumb_path") or "")),
            "qr": Path(str(entry.get("qr_path") or "")),
        }


class PhotoService:
    """规格 ``PhotoService`` 接口：``get`` / ``delete``，外加令牌校验、部署门控与打印取消。

    构造参数：
    - ``store``：``FilePhotoStore``（或任何实现 ``find`` / ``state_of`` / ``begin_delete`` /
      ``finish_delete`` 的存储）；
    - ``token_service``：令牌服务；默认新建 ``TokenService``；
    - ``print_cancel``：可空注入的「未提交打印取消」钩子，签名 ``(photo_token) -> None``；
      T-9 的打印服务上线后由接线方传入；未注入时删除照常工作，只是不撤打印队列；
    - ``delete_approved``：显式删除批准（生产默认 False = 禁止物理删除）；
    - ``testing``：测试语义（临时合成数据）放行删除；两个都为 True/False 组合时以
      ``testing or delete_approved`` 为准。
    """

    def __init__(
        self,
        store: FilePhotoStore,
        token_service: TokenService | None = None,
        print_cancel: Callable[[str], None] | None = None,
        *,
        delete_approved: bool = False,
        testing: bool = False,
    ) -> None:
        self.store = store
        self.tokens = token_service or TokenService()
        self.print_cancel = print_cancel
        self.delete_approved = bool(delete_approved)
        self.testing = bool(testing)
        self.print_cancel_errors: list[str] = []

    @property
    def physical_delete_allowed(self) -> bool:
        """当前实例是否允许执行物理删除（生产默认 False 的方向 = 拒绝）。"""
        return self.testing or self.delete_approved

    @classmethod
    def from_config(
        cls,
        config: object,
        store: FilePhotoStore,
        token_service: TokenService | None = None,
        print_cancel: Callable[[str], None] | None = None,
        *,
        env: Mapping[str, str] | None = None,
    ) -> "PhotoService":
        """按 T-11 配置装配：测试上下文放行；生产必须显式 ``BOOTH_PHOTO_DELETE_APPROVED``。"""
        testing = bool(getattr(config, "testing", False))
        approved = testing or delete_approved_from_env(env)
        return cls(
            store,
            token_service,
            print_cancel,
            delete_approved=approved,
            testing=testing,
        )

    # ---- 规格接口 ----------------------------------------------------------

    def get(self, token: str) -> PhotoRecord | None:
        """可取图记录；令牌非法、不存在或已删除一律 None（规格签名逐字）。"""
        if not self.tokens.validate(token):
            return None
        return self.store.find(token)

    def require(self, token: str) -> PhotoRecord:
        """取图路由用：区分 INVALID_TOKEN(404) / PHOTO_NOT_FOUND(404) / PHOTO_DELETED(410)。"""
        if not self.tokens.validate(token):
            raise ApiError(errors.INVALID_TOKEN)
        record = self.store.find(token)
        if record is not None:
            return record
        if self.store.state_of(token) == "deleted":
            raise ApiError(errors.PHOTO_DELETED)
        raise ApiError(errors.PHOTO_NOT_FOUND)

    def delete(self, token: str) -> bool:
        """删除并原子失效图片 / 缩略图 / QR；第一次 True，重复删除 False（不复活）。

        未批准的生产语义抛 ``DeleteNotApprovedError``；并发删除抛
        ``DELETE_IN_PROGRESS``（409）；未知令牌抛 ``PHOTO_NOT_FOUND``（404）；
        编号等非法令牌抛 ``INVALID_TOKEN``（404）。
        """
        if not self.tokens.validate(token):
            raise ApiError(errors.INVALID_TOKEN)
        if not self.physical_delete_allowed:
            raise DeleteNotApprovedError()
        ticket = self.store.begin_delete(token)  # 并发删除在此抛 DELETE_IN_PROGRESS
        if ticket is None:
            if self.store.state_of(token) == "unknown":
                raise ApiError(errors.PHOTO_NOT_FOUND)
            return False
        try:
            self._cancel_pending_prints(ticket)
        finally:
            self.store.finish_delete(ticket)
        return True

    def is_deleted(self, token: str) -> bool:
        """令牌曾经有效、现在处于已删除状态（路由层 410 用）。"""
        return self.tokens.validate(token) and self.store.state_of(token) == "deleted"

    # ---- 路径访问（路由按令牌取图） ------------------------------------------

    def photo_path(self, token: str) -> Path | None:
        return self.store.photo_path(token)

    def thumb_path(self, token: str) -> Path | None:
        return self.store.thumb_path(token)

    def qr_path(self, token: str) -> Path | None:
        return self.store.qr_path(token)

    # ---- 内部 --------------------------------------------------------------

    def _cancel_pending_prints(self, ticket: DeleteTicket) -> None:
        if self.print_cancel is None:
            return
        for token in ticket.tokens:
            try:
                self.print_cancel(token)
            except Exception as exc:  # 钩子失败不阻断失效：照片此时已不可达
                self.print_cancel_errors.append(f"{token}: {exc!r}")
