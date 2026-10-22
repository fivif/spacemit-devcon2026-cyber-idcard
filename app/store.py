"""现场数据存储：编号、记录、留存与删除。

合规要求（见 02-spec §6）：人脸是敏感个人信息，必须能按期限删除、能应要求删除单张。
"""

from __future__ import annotations

import hmac
import json
import os
import secrets
import threading
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path


@dataclass
class Record:
    id: str
    created_at: str
    prompt: str
    ep: bool
    infer_ms: float
    person_count: int
    degraded: bool
    seed: int
    card: str
    style: str
    event: str = "SpacemiT DevCon 2026"
    # 取图令牌：**公网上取图只认它，不认编号**。
    # 编号是连号的（SM-2026-0001…），挂上公网就是「从 0001 数到 9999 把全场照片拉走」。
    # 老记录没有这个字段 -> 空串 -> 令牌路一律查不到，失败方向是"打不开"而不是"泄漏"。
    token: str = ""
    extra: dict = field(default_factory=dict)


class Store:
    """编号发号器 + 记录落盘。编号形如 SM-2026-0001，跨重启自增。"""

    def __init__(self, root: str | Path, retention_hours: int = 24, event_year: int | None = None):
        self.root = Path(root)
        self.retention_hours = retention_hours
        self.event_year = event_year or datetime.now().year
        for sub in ("photos", "thumbs", "captures"):
            (self.root / sub).mkdir(parents=True, exist_ok=True)
        self.records_path = self.root / "records.jsonl"
        self.counter_path = self.root / "counter.json"
        self._lock = threading.RLock()
        self._counter = self._load_counter()

    # ---- 编号 ----------------------------------------------------------
    def _load_counter(self) -> int:
        if self.counter_path.is_file():
            try:
                payload = json.loads(self.counter_path.read_text(encoding="utf-8"))
                if payload.get("year") == self.event_year:
                    return int(payload.get("value", 0))
            except Exception:
                pass
        return 0

    def _save_counter(self) -> None:
        tmp = self.counter_path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"year": self.event_year, "value": self._counter}),
                       encoding="utf-8")
        tmp.replace(self.counter_path)

    def next_id(self) -> str:
        with self._lock:
            self._counter += 1
            self._save_counter()
            return f"SM-{self.event_year}-{self._counter:04d}"

    def peek_count(self) -> int:
        return self._counter

    # ---- 路径 ----------------------------------------------------------
    @staticmethod
    def new_token() -> str:
        """取图令牌。22 个字符、128 位随机 —— 猜不出来，也数不过来。"""
        return secrets.token_urlsafe(16)

    def by_token(self, token: str) -> Record | None:
        """按令牌找记录。找不到就返回 None（调用方一律回 404，不区分"没这个令牌"和"删掉了"）。"""
        value = str(token or "").strip()
        if len(value) < 16:      # 真令牌至少 22 字符；短的连查都不用查
            return None
        for record in self.all():
            if record.token and hmac.compare_digest(record.token, value) and not self.is_expired(record):
                return record
        return None

    def is_expired(self, record: Record) -> bool:
        try:
            created = datetime.fromisoformat(record.created_at).timestamp()
        except (TypeError, ValueError, OverflowError):
            return True
        return created + self.retention_hours * 3600 <= time.time()

    def photo_path(self, record_id: str) -> Path:
        return self.root / "photos" / f"{record_id}.jpg"

    def clean_photo_path(self, record_id: str) -> Path:
        """同一成片的无本地活动信息版本。"""
        return self.root / "photos" / f"{record_id}-clean.jpg"

    def thumb_path(self, record_id: str) -> Path:
        return self.root / "thumbs" / f"{record_id}.jpg"

    def capture_path(self, record_id: str) -> Path:
        return self.root / "captures" / f"{record_id}.jpg"

    # ---- 记录 ----------------------------------------------------------
    def append(self, record: Record) -> None:
        with self._lock:
            with (self.records_path).open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(asdict(record), ensure_ascii=False) + "\n")

    def get(self, record_id: str) -> Record | None:
        if not self.records_path.is_file():
            return None
        with self._lock:
            for line in self.records_path.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                try:
                    payload = json.loads(line)
                except Exception:
                    continue
                if payload.get("id") == record_id:
                    known = {k: v for k, v in payload.items() if k in Record.__dataclass_fields__}
                    return Record(**known)
        return None

    def all(self) -> list[Record]:
        if not self.records_path.is_file():
            return []
        out: list[Record] = []
        with self._lock:
            for line in self.records_path.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                try:
                    payload = json.loads(line)
                    out.append(Record(**{k: v for k, v in payload.items()
                                         if k in Record.__dataclass_fields__}))
                except Exception:
                    continue
        return out

    def delete(self, record_id: str) -> bool:
        """删除单张照片（参会者的删除权）。记录本身保留一条 tombstone 以便审计。"""
        removed = False
        for path in (self.photo_path(record_id), self.clean_photo_path(record_id),
                     self.thumb_path(record_id), self.capture_path(record_id),
                     self.root / "masks" / (record_id + ".png"),
                     self.root / "masks" / (record_id + "-subject.png"),
                     self.root / "masks" / (record_id + "-alpha.png"),
                     self.root / "masks" / (record_id + "-cutout.png"),
                     self.root / "prints" / (record_id + ".jpg"),
                     self.root / "previews" / (record_id + "-aigc.jpg")):
            try:
                if path.is_file():
                    path.unlink()
                    removed = True
            except Exception:
                pass
        if removed:
            self.append(Record(id=record_id, created_at=_now(), prompt="", ep=False, infer_ms=0.0,
                               person_count=0, degraded=True, seed=0, card="", style="",
                               extra={"tombstone": True, "reason": "attendee_delete"}))
        return removed

    def reset_for_event(self) -> dict:
        """彩排清场：清掉彩排期间的成片、编号归零、记录归档留痕。

        为什么不直接 rm -rf：
          * 编号要归零。否则大屏上的「已出图」会把彩排的几百张算进活动成绩，
            那是个编出来的数字；
          * 合规上「删过哪些编号」要留痕，所以记录是归档成
            records.jsonl.bak-<时间戳>，不是抹掉。
        """
        with self._lock:
            removed = 0
            for sub in ("photos", "thumbs", "captures", "masks", "previews", "qr", "prints"):
                folder = self.root / sub
                if not folder.is_dir():
                    continue
                for path in sorted(folder.iterdir()):
                    if path.is_file():
                        try:
                            path.unlink()
                            removed += 1
                        except Exception:
                            pass
            archived = None
            if self.records_path.is_file() and self.records_path.stat().st_size:
                stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
                target = self.records_path.with_name("records.jsonl.bak-%s" % stamp)
                try:
                    self.records_path.replace(target)
                    archived = target.name
                except Exception:
                    archived = None
            self._counter = 0
            self._save_counter()
        return {"ok": True, "files_removed": removed, "archived": archived,
                "counter": self._counter, "at": _now()}

    def purge_expired(self) -> dict:
        """按留存期限清理。返回清理统计，供操作员面板显示。"""
        with self._lock:
            records = self.all()
            expired = {record.id: record for record in records if self.is_expired(record)}
            file_owners = {record.id + suffix: record.id for record in records
                           for suffix in ("", "-clean", "-alpha", "-cutout", "-aigc", "-subject")}
            file_owners.update({record.token: record.id for record in records if record.token})
            removed = set()
            failed = set()
            cutoff = time.time() - self.retention_hours * 3600
            for sub in ("photos", "thumbs", "captures", "prints", "masks", "previews", "qr"):
                folder = self.root / sub
                for path in folder.glob("*"):
                    if not path.is_file() or path.suffix.lower() not in (".jpg", ".jpeg", ".png"):
                        continue
                    owner = file_owners.get(path.stem)
                    try:
                        if owner in expired or (owner is None and path.stat().st_mtime <= cutoff):
                            path.unlink()
                            if sub == "photos" and not path.stem.endswith("-clean"):
                                removed.add(path.stem)
                    except OSError:
                        if owner in expired:
                            failed.add(owner)
            completed = expired.keys() - failed
            if completed and self.records_path.is_file():
                lines = self.records_path.read_text(encoding="utf-8").splitlines()
                retained = []
                for line in lines:
                    try:
                        if json.loads(line).get("id") in completed:
                            continue
                    except (ValueError, AttributeError):
                        pass
                    retained.append(line)
                temporary = self.records_path.with_suffix(".tmp")
                temporary.write_text("\n".join(retained) + ("\n" if retained else ""), encoding="utf-8")
                temporary.replace(self.records_path)
            kept = sum(self.photo_path(record.id).is_file() for record in records
                       if record.id not in expired and not record.extra.get("tombstone"))
            removed = sorted(removed)
        return {"removed": removed, "removed_count": len(removed), "kept": kept,
                "retention_hours": self.retention_hours, "at": _now()}


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")
