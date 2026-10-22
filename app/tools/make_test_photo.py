#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""造一张测试成片记录，用来验证「取图页二维码 + 打印」链路。

为什么需要它：现场还没有真实拍摄过，data/photos/ 是空的；而打印与取图
都要**真实令牌**才能走通（打印适配器会按令牌去 store 找文件）。直接手改
records.jsonl 容易写错格式，所以用 Store 自己的接口造记录。

    python3 app/tools/make_test_photo.py            # 造一张
    python3 app/tools/make_test_photo.py --clean    # 删掉造出来的测试记录

注意：造出来的记录编号带 "TEST" 前缀，便于和真实记录区分。
"""
from __future__ import annotations

import json
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

APP = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP))

from store import Record, Store  # noqa: E402

DATA = APP / "data"
TEST_MARKER = "TEST-"
SOURCE_IMAGE = DATA / "static-person.jpg"


def make() -> int:
    store = Store(DATA)
    if not SOURCE_IMAGE.is_file():
        print("缺少素材图：%s（先确认现场素材在）" % SOURCE_IMAGE)
        return 1

    import secrets
    record_id = TEST_MARKER + datetime.now().strftime("%H%M%S")
    # 令牌必须**恰好 22 个 URL-safe 字符**：PrintJob.from_dict 有 is_valid_photo_token
    # 校验，长度不对时打印提交会以"校验失败"告终（现场踩过）。token_urlsafe 给的是
    # 近似长度（末尾补 '=' 还会被剥掉），所以循环补足/截断到 22。
    token = ""
    while len(token) != 22:
        token = secrets.token_urlsafe(22).replace("=", "")[:22]

    photo_path = store.photo_path(record_id)
    photo_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(SOURCE_IMAGE, photo_path)
    thumb = store.thumb_path(record_id)
    shutil.copyfile(SOURCE_IMAGE, thumb)

    record = Record(
        id=record_id,
        created_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        prompt="测试记录（非真实拍摄）",
        ep=True,
        infer_ms=0.0,
        person_count=1,
        degraded=False,
        seed=0,
        card="test",
        style="soft-chibi",
        token=token,
    )
    store.append(record)
    print("已造测试记录：编号 %s，令牌 %s" % (record_id, token))
    print("取图页地址：/p/%s" % token)
    print("打印测试：curl -X POST http://127.0.0.1:8090/api/print \\")
    print('  -H "Content-Type: application/json" \\')
    print('  -d \'{"photo_token": "%s", "idempotency_key": "test:%s"}\'' % (token, token))
    return 0


def clean() -> int:
    store = Store(DATA)
    removed = 0
    keep = []
    for record in store.all():
        if record.id.startswith(TEST_MARKER):
            removed += 1
            for path in (store.photo_path(record.id), store.thumb_path(record.id),
                         store.capture_path(record.id),
                         store.root / "qr" / (record.token + ".png")):
                try:
                    if path.is_file():
                        path.unlink()
                except OSError:
                    pass
            continue
        keep.append(record)

    if not removed:
        print("没有测试记录需要清理")
        return 0

    # 重写 records.jsonl（先备份）
    backup = store.records_path.with_suffix(
        ".jsonl.bak-testsync-%s" % datetime.now().strftime("%Y%m%d_%H%M%S"))
    shutil.copyfile(store.records_path, backup)
    with store.records_path.open("w", encoding="utf-8") as handle:
        for record in keep:
            handle.write(json.dumps(record.__dict__, ensure_ascii=False) + "\n")
    print("已清理 %d 条测试记录（备份 %s）" % (removed, backup.name))
    return 0


def main() -> int:
    if "--clean" in sys.argv:
        return clean()
    return make()


if __name__ == "__main__":
    sys.exit(main())
