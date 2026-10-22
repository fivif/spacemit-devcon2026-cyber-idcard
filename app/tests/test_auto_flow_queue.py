# -*- coding: utf-8 -*-
"""T-1 队列模型与存储测试（QueueStatus/QueueEntry/QueueStore/QueueService）。

运行（项目根目录）：
    python3 -m unittest app.tests.test_auto_flow_queue -v

约定：
- 全部离线：tmp 目录作为数据根 + 本文件内的 FakeClock，不联网、不碰生产路径；
- 覆盖 FIFO、容量、叫号/开始/超时/非法转换、幂等重放与冲突、并发同键提交、
  重启恢复（顺序/幂等/占用不重复）、旧文件不受影响、存储原子写与损坏报错。
"""

from __future__ import annotations

import hashlib
import json
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.auto_flow import errors
from app.auto_flow.models import (
    QueueEntry,
    QueueStatus,
    QueueSubmission,
    to_utc_iso,
)
from app.auto_flow.queue_service import (
    InvalidTransitionError,
    QueueService,
    normalize_submission,
)
from app.auto_flow.queue_store import MemoryQueueStore, QueueStore, QueueStoreError

UTC = timezone.utc
EVENT_YEAR = 2026
ALLOWLIST = ("note", "wechat")


class FakeClock:
    """可控时钟：同时支持 .now() 与直接调用（兼容 T-14 ClockPort 的两种用法）。"""

    def __init__(self, start: datetime | None = None) -> None:
        self._now = start or datetime(2026, 9, 30, 10, 0, 0, tzinfo=UTC)

    def now(self) -> datetime:
        return self._now

    def __call__(self) -> datetime:
        return self._now

    def advance(self, seconds: float = 0.0) -> datetime:
        self._now = self._now + timedelta(seconds=seconds)
        return self._now


def make_service(
    data_root: Path,
    *,
    clock: FakeClock,
    max_active: int = 8,
    start_timeout_seconds: int = 120,
    idempotency_ttl_seconds: int = 86400,
    store: QueueStore | MemoryQueueStore | None = None,
) -> QueueService:
    return QueueService(
        store if store is not None else QueueStore(data_root),
        max_active=max_active,
        start_timeout_seconds=start_timeout_seconds,
        idempotency_ttl_seconds=idempotency_ttl_seconds,
        optional_fields_allowlist=ALLOWLIST,
        clock=clock,
        event_year=EVENT_YEAR,
    )


def submission(
    request_id: str,
    *,
    style_id: str = "style-a",
    consent_id: str = "consent-1",
    optional_fields: dict[str, str] | None = None,
    portrait_mode: str = "standard",
) -> QueueSubmission:
    return QueueSubmission(
        request_id=request_id,
        style_id=style_id,
        capture_consent_id=consent_id,
        optional_fields=optional_fields,
        portrait_mode=portrait_mode,
    )


class QueueTestCase(unittest.TestCase):
    """公共夹具：tmp 数据根 + FakeClock + 默认 8 容量 / 120 秒开始期限。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="auto-flow-queue-")
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.clock = FakeClock()
        self.service = make_service(self.root, clock=self.clock)

    def submit(self, request_id: str, **kwargs) -> QueueEntry:
        return self.service.submit(submission(request_id, **kwargs))

    def reload_service(self) -> QueueService:
        """新实例 + 同一数据根，模拟进程重启（顺序/幂等/占用必须保持）。"""
        return make_service(self.root, clock=self.clock)

    def assert_api_error(self, code: str, callable_, *args, **kwargs):
        with self.assertRaises(errors.ApiError) as ctx:
            callable_(*args, **kwargs)
        self.assertEqual(ctx.exception.code, code)
        return ctx.exception


class SubmitAndIdempotencyTest(QueueTestCase):
    """入队、展示编号、幂等重放与冲突。"""

    def test_mode_switch_conflicts_in_both_directions(self):
        for initial, changed in (("standard", "chibi"), ("chibi", "standard")):
            with self.subTest(initial=initial):
                original = self.submit("req-" + initial, portrait_mode=initial)
                replay = self.service.submit_result(submission("req-" + initial, portrait_mode=initial))
                self.assertFalse(replay.created)
                self.assertEqual(replay.entry, original)
                self.assert_api_error(
                    errors.IDEMPOTENCY_CONFLICT, self.service.submit,
                    submission("req-" + initial, portrait_mode=changed),
                )
                self.assertEqual(self.service.get(original.queue_id), original)
        self.assertEqual(self.service.snapshot()["total"], 2)

    def test_invalid_mode_on_direct_model_rejects_without_writes(self):
        before = QueueStore(self.root).load()
        for invalid in (None, "", "CHIBI", " chibi ", "adult", True, 1, []):
            with self.subTest(mode=invalid):
                self.assert_api_error(
                    errors.INVALID_REQUEST, self.service.submit,
                    submission("req-invalid", portrait_mode=invalid),
                )
        self.assertEqual(QueueStore(self.root).load(), before)
        self.assertFalse(QueueStore(self.root).path.exists())

    def test_submit_creates_queued_entry_with_display_number(self):
        entry = self.submit("req-1")
        self.assertEqual(entry.status, QueueStatus.QUEUED)
        self.assertEqual(entry.display_number, "SM-2026-0001")
        self.assertEqual(entry.created_at, "2026-09-30T10:00:00Z")
        self.assertEqual(entry.updated_at, "2026-09-30T10:00:00Z")
        self.assertEqual(len(entry.queue_id), 22)  # 不可猜测的随机 id
        self.assertNotEqual(entry.queue_id, entry.request_id)
        self.assertEqual(self.service.get(entry.queue_id), entry)
        self.assertTrue((self.root / "auto_flow" / "queue.json").is_file())

    def test_optional_identity_text_is_persisted_for_capture_pipeline(self):
        service = QueueService(
            QueueStore(self.root),
            optional_fields_allowlist=("display_name", "developer_id"),
            clock=self.clock,
            event_year=2026,
        )
        entry = service.submit(submission(
            "req-identity",
            optional_fields={
                "display_name": "  小明  ",
                "developer_id": "  @spacemit  ",
            },
        ))
        self.assertEqual(entry.name, "小明")
        self.assertEqual(entry.handle, "@spacemit")

        restored = QueueService(
            QueueStore(self.root),
            optional_fields_allowlist=("display_name", "developer_id"),
            clock=self.clock,
            event_year=2026,
        ).get(entry.queue_id)
        self.assertIsNotNone(restored)
        self.assertEqual(restored.name, "小明")
        self.assertEqual(restored.handle, "@spacemit")

    def test_display_numbers_increase_in_submit_order(self):
        first = self.submit("req-1")
        second = self.submit("req-2")
        self.assertEqual(first.display_number, "SM-2026-0001")
        self.assertEqual(second.display_number, "SM-2026-0002")

    def test_display_counter_can_be_injected_through_store(self):
        # 测试注入：先通过真实持久化适配预置计数器，再让服务读取
        state = QueueStore.empty_state()
        state["counter"] = 41
        QueueStore(self.root).save(state)
        service = self.reload_service()
        entry = service.submit(submission("req-x"))
        self.assertEqual(entry.display_number, "SM-2026-0042")

    def test_idempotent_replay_returns_original_entry(self):
        original = self.submit("req-1", optional_fields={"note": "a", "wechat": "b"})
        result = self.service.submit_result(
            submission("req-1", optional_fields={"note": "a", "wechat": "b"})
        )
        self.assertFalse(result.created)
        self.assertEqual(result.entry.queue_id, original.queue_id)
        self.assertEqual(result.entry.updated_at, original.updated_at)  # 原条目未被修改
        self.assertEqual(self.service.snapshot()["total"], 1)

    def test_idempotent_replay_is_key_order_and_none_insensitive(self):
        # 键序无关
        original = self.submit("req-1", optional_fields={"note": "a", "wechat": "b"})
        reordered = self.service.submit_result(
            submission("req-1", optional_fields={"wechat": "b", "note": "a"})
        )
        self.assertFalse(reordered.created)
        self.assertEqual(reordered.entry.queue_id, original.queue_id)
        # None 与空字典等价
        empty = self.submit("req-2")  # optional_fields=None
        empty_replay = self.service.submit_result(
            submission("req-2", optional_fields={})
        )
        self.assertFalse(empty_replay.created)
        self.assertEqual(empty_replay.entry.queue_id, empty.queue_id)

    def test_same_key_different_style_conflicts_and_preserves_original(self):
        original = self.submit("req-1", style_id="style-a")
        self.assert_api_error(
            errors.IDEMPOTENCY_CONFLICT,
            self.service.submit,
            submission("req-1", style_id="style-b"),
        )
        kept = self.service.get(original.queue_id)
        self.assertIsNotNone(kept)
        self.assertEqual(kept.style_id, "style-a")
        self.assertEqual(self.service.snapshot()["total"], 1)

    def test_same_key_different_optional_fields_conflicts(self):
        self.submit("req-1", optional_fields={"note": "a"})
        self.assert_api_error(
            errors.IDEMPOTENCY_CONFLICT,
            self.service.submit,
            submission("req-1", optional_fields={"note": "changed"}),
        )

    def test_different_request_ids_create_separate_entries(self):
        first = self.submit("req-1")
        second = self.submit("req-2")
        self.assertNotEqual(first.queue_id, second.queue_id)
        self.assertEqual(self.service.snapshot()["total"], 2)

    def test_invalid_submissions_reject_without_state_change(self):
        self.assert_api_error(
            errors.INVALID_REQUEST,
            self.service.submit,
            {"request_id": "r", "style_id": "s", "capture_consent_id": "c", "extra": 1},
        )
        self.assert_api_error(
            errors.INVALID_REQUEST,
            self.service.submit,
            submission("req-1", optional_fields={"not-allowed": "x"}),
        )
        self.assert_api_error(
            errors.INVALID_REQUEST,
            self.service.submit,
            submission("req-1", optional_fields={"note": 7}),  # type: ignore[dict-item]
        )
        self.assert_api_error(
            errors.INVALID_REQUEST,
            self.service.submit,
            submission(""),  # 空 request_id
        )
        self.assertEqual(self.service.snapshot()["total"], 0)
        self.assertFalse((self.root / "auto_flow" / "queue.json").exists())

    def test_idempotency_ttl_recycles_only_terminal_records(self):
        original = self.submit("req-1")
        # 条目仍活动：即使超过保留期，重放也必须返回原条目（防止重复排队）
        self.clock.advance(86401)
        replay = self.service.submit_result(submission("req-1"))
        self.assertFalse(replay.created)
        self.assertEqual(replay.entry.queue_id, original.queue_id)
        # 条目已终态：超过保留期后允许按新提交处理
        self.service.mark_failed(original.queue_id, reason="测试")
        self.clock.advance(86401)
        fresh = self.service.submit_result(submission("req-1"))
        self.assertTrue(fresh.created)
        self.assertNotEqual(fresh.entry.queue_id, original.queue_id)


class CapacityTest(QueueTestCase):
    """容量：活动（非终态）达到 max_active 即拒，终态释放名额。"""

    def test_capacity_counts_active_entries_only(self):
        service = make_service(self.root, clock=self.clock, max_active=2)
        self.service = service
        first = self.submit("req-1")
        self.submit("req-2")
        self.assert_api_error(errors.QUEUE_FULL, service.submit, submission("req-3"))
        # 终态（failed）不再占容量
        service.mark_failed(first.queue_id, reason="测试释放")
        created = service.submit(submission("req-3"))
        self.assertEqual(created.status, QueueStatus.QUEUED)

    def test_unlimited_capacity_accepts_more_than_previous_default(self):
        service = make_service(self.root, clock=self.clock, max_active=None)
        self.service = service
        for index in range(25):
            service.submit(submission("unlimited-%02d" % index))
        snapshot = service.snapshot()
        self.assertEqual(snapshot["active"], 25)
        self.assertIsNone(snapshot["capacity"]["max_active"])

    def test_expired_entry_does_not_occupy_capacity(self):
        service = make_service(self.root, clock=self.clock, max_active=2)
        self.service = service
        first = self.submit("req-1")
        self.submit("req-2")
        service.call_next()  # req-1 -> called
        self.clock.advance(121)
        expired = service.expire_due(self.clock.now())
        self.assertEqual([item.queue_id for item in expired], [first.queue_id])
        created = service.submit(submission("req-3"))
        self.assertEqual(created.status, QueueStatus.QUEUED)

    def test_ready_for_pickup_does_not_occupy_capacity(self):
        service = make_service(self.root, clock=self.clock, max_active=1)
        self.service = service
        first = self.submit("req-1")
        service.call_next()
        service.start(first.queue_id)
        service.mark_capturing(first.queue_id)
        service.mark_processing(first.queue_id)
        service.mark_ready_for_pickup(first.queue_id)

        created = service.submit(submission("req-2"))

        self.assertEqual(created.status, QueueStatus.QUEUED)
        self.assertEqual(service.snapshot()["capacity"], {"max_active": 1, "active": 1})

    def test_full_rejection_is_persisted_consistently(self):
        service = make_service(self.root, clock=self.clock, max_active=1)
        self.service = service
        self.submit("req-1")
        self.assert_api_error(errors.QUEUE_FULL, service.submit, submission("req-2"))
        reloaded = make_service(self.root, clock=self.clock, max_active=1)
        self.assertEqual(reloaded.snapshot()["total"], 1)


class CallAndStartTest(QueueTestCase):
    """FIFO 叫号、拍摄位独占、开始与超时。"""

    def test_call_next_is_fifo_and_sets_deadline(self):
        first = self.submit("req-1")
        second = self.submit("req-2")
        called = self.service.call_next()
        self.assertEqual(called.queue_id, first.queue_id)
        self.assertEqual(called.status, QueueStatus.CALLED)
        self.assertEqual(called.called_at, "2026-09-30T10:00:00Z")
        self.assertEqual(called.start_deadline, "2026-09-30T10:02:00Z")
        self.assertIsNone(self.service.get(second.queue_id).called_at)
        # 拍摄位被占用：不再叫下一位
        self.assertIsNone(self.service.call_next())

    def test_call_next_returns_none_when_queue_empty(self):
        self.assertIsNone(self.service.call_next())
        self.assertIsNone(self.service.get("no-such-id"))

    def test_call_next_exclusive_while_ready_and_capturing(self):
        first = self.submit("req-1")
        self.submit("req-2")
        self.service.call_next()
        self.service.start(first.queue_id)
        self.assertIsNone(self.service.call_next())  # ready 占用
        self.service.mark_capturing(first.queue_id)
        self.assertIsNone(self.service.call_next())  # capturing 占用
        self.service.mark_processing(first.queue_id)  # 释放拍摄位
        second_called = self.service.call_next()
        self.assertEqual(second_called.display_number, "SM-2026-0002")

    def test_start_requires_called_status(self):
        entry = self.submit("req-1")
        self.assert_api_error(errors.NOT_CALLED, self.service.start, entry.queue_id)
        self.assert_api_error(errors.QUEUE_NOT_FOUND, self.service.start, "no-such-id")
        called = self.service.call_next()
        started = self.service.start(called.queue_id)
        self.assertEqual(started.status, QueueStatus.READY)
        self.assertEqual(started.started_at, "2026-09-30T10:00:00Z")
        self.assertEqual(started.consent_id, "consent-1")  # 绑定提交时记录的同意
        # 已经是 ready 再 start：非法转换
        self.assert_api_error(errors.NOT_CALLED, self.service.start, called.queue_id)

    def test_start_with_explicit_consent_binds_that_value(self):
        self.submit("req-1")
        called = self.service.call_next()
        started = self.service.start(called.queue_id, "consent-fresh")
        self.assertEqual(started.consent_id, "consent-fresh")
        other = self.submit("req-2")
        self.assert_api_error(
            errors.INVALID_REQUEST, self.service.start, other.queue_id, ""
        )

    def test_start_before_deadline_succeeds(self):
        entry = self.submit("req-1")
        self.service.call_next()
        self.clock.advance(119)
        started = self.service.start(entry.queue_id)
        self.assertEqual(started.status, QueueStatus.READY)

    def test_start_at_or_after_deadline_marks_expired(self):
        entry = self.submit("req-1")
        self.service.call_next()
        self.clock.advance(120)  # now >= start_deadline 即视为超时
        self.assert_api_error(errors.QUEUE_EXPIRED, self.service.start, entry.queue_id)
        expired = self.service.get(entry.queue_id)
        self.assertEqual(expired.status, QueueStatus.EXPIRED)
        self.assertEqual(expired.expired_at, "2026-09-30T10:02:00Z")
        self.assertIsNone(self.service.snapshot()["occupancy"])
        # 已过期条目再次 start 仍是 QUEUE_EXPIRED
        self.assert_api_error(errors.QUEUE_EXPIRED, self.service.start, entry.queue_id)

    def test_expire_due_transitions_and_is_idempotent(self):
        first = self.submit("req-1")
        second = self.submit("req-2")
        self.service.call_next()
        self.clock.advance(121)
        expired = self.service.expire_due(self.clock.now())
        self.assertEqual([item.queue_id for item in expired], [first.queue_id])
        self.assertEqual(expired[0].status, QueueStatus.EXPIRED)
        self.assertEqual(expired[0].expired_at, to_utc_iso(self.clock.now()))
        self.assertEqual(self.service.expire_due(self.clock.now()), [])
        next_called = self.service.call_next()
        self.assertEqual(next_called.queue_id, second.queue_id)

    def test_expire_due_ignores_queued_and_ready_entries(self):
        queued = self.submit("req-1")
        self.service.call_next()
        self.service.start(queued.queue_id)
        self.clock.advance(100000)
        self.assertEqual(self.service.expire_due(self.clock.now()), [])
        self.assertEqual(self.service.get(queued.queue_id).status, QueueStatus.READY)
        self.assertIsNone(self.service.call_next())  # ready 仍独占

    def test_snapshot_counts_and_occupancy_are_read_only(self):
        self.submit("req-1")
        self.submit("req-2")
        self.service.call_next()
        snapshot = self.service.snapshot()
        self.assertEqual(snapshot["counts"]["called"], 1)
        self.assertEqual(snapshot["counts"]["queued"], 1)
        self.assertEqual(snapshot["total"], 2)
        self.assertEqual(snapshot["active"], 2)
        self.assertEqual(snapshot["capacity"]["max_active"], 8)
        occupancy_id = snapshot["occupancy"]["queue_id"]
        self.assertEqual(self.service.get(occupancy_id).status, QueueStatus.CALLED)
        # 修改快照不影响服务内部状态
        snapshot["entries"][0]["status"] = "failed"
        snapshot["counts"]["queued"] = 99
        self.assertEqual(self.service.snapshot()["counts"]["queued"], 1)
        self.assertEqual(self.service.get(occupancy_id).status, QueueStatus.CALLED)

    def test_mark_transitions_validate_and_release_slot(self):
        first = self.submit("req-1")
        second = self.submit("req-2")
        self.service.call_next()
        self.service.start(first.queue_id)
        self.assertIsNotNone(self.service.snapshot()["occupancy"])
        self.service.mark_capturing(first.queue_id)
        self.assertIsNotNone(self.service.snapshot()["occupancy"])
        # 非法转换：capturing 不能直接 ready_for_pickup
        with self.assertRaises(InvalidTransitionError):
            self.service.mark_ready_for_pickup(first.queue_id)
        self.service.mark_processing(first.queue_id)
        self.assertIsNone(self.service.snapshot()["occupancy"])
        self.service.mark_ready_for_pickup(first.queue_id)
        self.assertEqual(self.service.get(first.queue_id).status, QueueStatus.READY_FOR_PICKUP)
        # 终态没有出边
        with self.assertRaises(InvalidTransitionError):
            self.service.mark_failed(first.queue_id)
        # queued 不能直接进入拍摄流程
        with self.assertRaises(InvalidTransitionError):
            self.service.mark_capturing(second.queue_id)
        # 语义化失败原因进诊断 notes
        failed = self.service.mark_failed(second.queue_id, reason="相机故障")
        self.assertEqual(failed.status, QueueStatus.FAILED)
        self.assertTrue(
            any("相机故障" in note["reason"] for note in self.service.snapshot()["notes"])
        )


class ConcurrencyTest(QueueTestCase):
    """多线程并发：同键提交只留一条活动记录，不同键无丢失。"""

    def test_concurrent_same_request_id_single_active_entry(self):
        workers = 8
        barrier = threading.Barrier(workers)
        results: list = []
        errors_seen: list = []

        def worker() -> None:
            barrier.wait()
            try:
                results.append(
                    self.service.submit_result(
                        submission("req-shared", optional_fields={"note": "same"})
                    )
                )
            except BaseException as exc:  # pragma: no cover - 失败时给出可见原因
                errors_seen.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(workers)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(errors_seen, [])
        self.assertEqual(len(results), workers)
        self.assertEqual(len({result.entry.queue_id for result in results}), 1)
        self.assertEqual(sum(1 for result in results if result.created), 1)
        self.assertEqual(self.service.snapshot()["total"], 1)
        reloaded = self.reload_service()
        self.assertEqual(reloaded.snapshot()["total"], 1)
        self.assertEqual(reloaded.get(results[0].entry.queue_id).request_id, "req-shared")

    def test_concurrent_distinct_request_ids_no_lost_updates(self):
        workers = 8
        barrier = threading.Barrier(workers)
        errors_seen: list = []
        created: list = []

        def worker(index: int) -> None:
            barrier.wait()
            try:
                created.append(self.service.submit(submission(f"req-{index}")))
            except BaseException as exc:  # pragma: no cover
                errors_seen.append(exc)

        threads = [threading.Thread(target=worker, args=(index,)) for index in range(workers)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(errors_seen, [])
        self.assertEqual(len({entry.queue_id for entry in created}), workers)
        self.assertEqual(self.service.snapshot()["total"], workers)


class RestartRecoveryTest(QueueTestCase):
    """重启：保留顺序与幂等结果；不明中间态转可诊断 failed；不重复占位。"""

    def test_chibi_mode_persists_through_call_start_and_restart_recovery(self):
        original = self.submit("req-chibi", portrait_mode="chibi")
        persisted = QueueStore(self.root).load()
        self.assertEqual(persisted["entries"][0]["portrait_mode"], "chibi")
        restarted = self.reload_service()
        self.assertEqual(restarted.get(original.queue_id).portrait_mode, "chibi")
        self.assertEqual(restarted.call_next().portrait_mode, "chibi")
        self.assertEqual(restarted.start(original.queue_id).portrait_mode, "chibi")
        recovered = self.reload_service()
        self.assertEqual(recovered.get(original.queue_id).status, QueueStatus.FAILED)
        replay = recovered.submit_result(submission("req-chibi", portrait_mode="chibi"))
        self.assertFalse(replay.created)
        self.assertEqual(replay.entry.portrait_mode, "chibi")
        self.assertEqual(recovered.snapshot()["total"], 1)

    def test_old_fingerprint_matches_only_standard_and_unchanged_payload(self):
        original = self.submit("req-old", optional_fields={"note": "旧记录"})
        store = QueueStore(self.root)
        state = store.load()
        state["entries"][0].pop("portrait_mode")
        old_payload = '{"optional_fields":{"note":"旧记录"},"style_id":"style-a"}'
        old_fingerprint = "sha256:" + hashlib.sha256(old_payload.encode("utf-8")).hexdigest()
        state["idempotency"]["req-old"]["fingerprint"] = old_fingerprint
        store.save(state)
        restarted = self.reload_service()
        self.assertEqual(restarted.get(original.queue_id).portrait_mode, "standard")
        for mode_payload in ({}, {"portrait_mode": "standard"}):
            with self.subTest(payload=mode_payload):
                replay = restarted.submit_result({
                    "request_id": "req-old", "style_id": "style-a",
                    "capture_consent_id": "new-consent",
                    "optional_fields": {"note": "旧记录"}, **mode_payload,
                })
                self.assertFalse(replay.created)
                self.assertEqual(replay.entry.queue_id, original.queue_id)
        for changes in ({"portrait_mode": "chibi"}, {"style_id": "style-b"},
                        {"optional_fields": {"note": "已改"}}):
            with self.subTest(changes=changes):
                self.assert_api_error(
                    errors.IDEMPOTENCY_CONFLICT, restarted.submit,
                    submission("req-old", **{"optional_fields": {"note": "旧记录"}, **changes}),
                )
        self.assertEqual(restarted.snapshot()["total"], 1)
        self.assertEqual(store.load()["idempotency"]["req-old"]["fingerprint"], old_fingerprint)

    def test_missing_fingerprint_does_not_allow_mode_switch(self):
        for mode in ("standard", "chibi"):
            self.submit("req-missing-" + mode, portrait_mode=mode)
        store = QueueStore(self.root)
        state = store.load()
        state["entries"][0].pop("portrait_mode")
        state["idempotency"] = {}
        store.save(state)
        restarted = self.reload_service()
        for mode, changed in (("standard", "chibi"), ("chibi", "standard")):
            with self.subTest(mode=mode):
                replay = restarted.submit_result(submission("req-missing-" + mode, portrait_mode=mode))
                self.assertFalse(replay.created)
                self.assertEqual(replay.entry.portrait_mode, mode)
                self.assert_api_error(
                    errors.IDEMPOTENCY_CONFLICT, restarted.submit,
                    submission("req-missing-" + mode, portrait_mode=changed),
                )
        self.assertEqual(restarted.snapshot()["total"], 2)

    def test_restart_preserves_called_occupancy_order_and_idempotency(self):
        first = self.submit("req-1")
        second = self.submit("req-2")
        third = self.submit("req-3")
        self.service.call_next()  # req-1 叫号中

        restarted = self.reload_service()
        self.assertEqual(restarted.get(first.queue_id).status, QueueStatus.CALLED)
        self.assertEqual(restarted.snapshot()["occupancy"]["queue_id"], first.queue_id)
        self.assertIsNone(restarted.call_next())  # 不产生第二个拍摄位占用
        replay = restarted.submit_result(submission("req-1"))
        self.assertFalse(replay.created)
        self.assertEqual(replay.entry.queue_id, first.queue_id)
        self.assertEqual(restarted.snapshot()["total"], 3)

        # 叫号超时后按原顺序进入下一位
        self.clock.advance(121)
        expired = restarted.expire_due(self.clock.now())
        self.assertEqual([item.queue_id for item in expired], [first.queue_id])
        self.assertEqual(restarted.call_next().queue_id, second.queue_id)
        self.assertEqual(restarted.get(third.queue_id).status, QueueStatus.QUEUED)

    def test_restart_fails_started_and_midflight_entries_without_duplicates(self):
        first = self.submit("req-1")
        second = self.submit("req-2")
        third = self.submit("req-3")

        # ready 状态重启：会话失效 -> failed，拍摄位释放
        self.service.call_next()
        self.service.start(first.queue_id)
        restarted = self.reload_service()
        self.assertEqual(restarted.get(first.queue_id).status, QueueStatus.FAILED)
        self.assertIsNone(restarted.snapshot()["occupancy"])
        notes = restarted.snapshot()["notes"]
        self.assertTrue(
            any(
                note["kind"] == "restart_recovery"
                and note["queue_id"] == first.queue_id
                and note["from_status"] == "ready"
                for note in notes
            )
        )
        replay = restarted.submit_result(submission("req-1"))
        self.assertFalse(replay.created)  # 幂等结果保留，不因重启重复排队
        self.assertEqual(restarted.call_next().queue_id, second.queue_id)

        # capturing 状态重启：-> failed，仍不自动重拍
        restarted.start(second.queue_id)
        restarted.mark_capturing(second.queue_id)
        restarted = self.reload_service()
        self.assertEqual(restarted.get(second.queue_id).status, QueueStatus.FAILED)
        self.assertIsNone(restarted.snapshot()["occupancy"])

        # processing 状态重启：-> failed
        restarted.call_next()
        restarted.start(third.queue_id)
        restarted.mark_capturing(third.queue_id)
        restarted.mark_processing(third.queue_id)
        restarted = self.reload_service()
        self.assertEqual(restarted.get(third.queue_id).status, QueueStatus.FAILED)
        self.assertEqual(restarted.snapshot()["total"], 3)  # 没有重复条目
        # 重启恢复只写诊断，不生成新条目、不产生第二个占用
        for request_id in ("req-1", "req-2", "req-3"):
            entry = restarted.submit_result(submission(request_id))
            self.assertFalse(entry.created)
            self.assertEqual(entry.entry.request_id, request_id)

    def test_restart_resolves_two_occupants_to_single_slot(self):
        # 手工构造异常文档：两条 called（正常流程不可能出现），恢复必须收敛为一条
        clock_now = to_utc_iso(self.clock.now())
        first = QueueEntry(
            queue_id="q-first",
            request_id="req-1",
            status=QueueStatus.CALLED,
            style_id="style-a",
            created_at=clock_now,
            updated_at=clock_now,
            called_at=clock_now,
            start_deadline=to_utc_iso(self.clock.now() + timedelta(seconds=120)),
        )
        second = QueueEntry(
            queue_id="q-second",
            request_id="req-2",
            status=QueueStatus.CALLED,
            style_id="style-a",
            created_at=clock_now,
            updated_at=clock_now,
            called_at=clock_now,
            start_deadline=to_utc_iso(self.clock.now() + timedelta(seconds=120)),
        )
        state = QueueStore.empty_state()
        state["entries"] = [first.to_dict(), second.to_dict()]
        state["occupancy"] = {"queue_id": "q-second", "acquired_at": clock_now}
        QueueStore(self.root).save(state)

        service = self.reload_service()
        self.assertEqual(service.get("q-first").status, QueueStatus.CALLED)
        self.assertEqual(service.get("q-second").status, QueueStatus.FAILED)
        self.assertEqual(service.snapshot()["occupancy"]["queue_id"], "q-first")
        self.assertIsNone(service.call_next())
        self.assertTrue(
            any(note["kind"] == "slot_conflict" for note in service.snapshot()["notes"])
        )

    def test_restart_rebuilds_index_and_counter_for_legacy_entries(self):
        # 旧数据/手工数据：有条目但缺幂等索引、counter 落后于展示编号
        clock_now = to_utc_iso(self.clock.now())
        entry = QueueEntry(
            queue_id="q-legacy",
            request_id="req-legacy",
            status=QueueStatus.QUEUED,
            style_id="style-a",
            created_at=clock_now,
            updated_at=clock_now,
            display_number="SM-2026-0007",
        )
        state = QueueStore.empty_state()
        state["entries"] = [entry.to_dict()]
        state["counter"] = 0
        QueueStore(self.root).save(state)

        service = self.reload_service()
        # 缺索引 -> 按 request_id 重建；指纹未知 -> 重放返回原条目（不产生第二条）
        replay = service.submit_result(submission("req-legacy"))
        self.assertFalse(replay.created)
        self.assertEqual(replay.entry.queue_id, "q-legacy")
        # 计数器被提升到已用最大序号
        created = service.submit(submission("req-new"))
        self.assertEqual(created.display_number, "SM-2026-0008")


class StorageTest(unittest.TestCase):
    """QueueStore：缺失即空、原子写、损坏报错、不触碰其它文件。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="auto-flow-store-")
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.clock = FakeClock()

    def test_missing_file_loads_as_empty_state(self):
        store = QueueStore(self.root)
        self.assertFalse(store.path.exists())
        state = store.load()
        self.assertEqual(state["version"], QueueStore.SCHEMA_VERSION)
        self.assertEqual(state["entries"], [])
        self.assertEqual(state["counter"], 0)
        self.assertFalse(store.path.exists())  # load 不创建文件

    def test_save_is_atomic_and_leaves_no_temp_files(self):
        store = QueueStore(self.root)
        state = store.empty_state()
        state["counter"] = 5
        store.save(state)
        self.assertEqual(store.load()["counter"], 5)
        # 反复写入后目录里只有 queue.json，没有残留临时文件
        for value in range(1, 6):
            state["counter"] = value
            store.save(state)
        names = sorted(path.name for path in store.directory.iterdir())
        self.assertEqual(names, ["queue.json"])

    def test_corrupt_or_unknown_version_file_raises_and_is_preserved(self):
        store = QueueStore(self.root)
        store.directory.mkdir(parents=True, exist_ok=True)
        store.path.write_text("{ not json", encoding="utf-8")
        with self.assertRaises(QueueStoreError):
            store.load()
        self.assertEqual(store.path.read_text(encoding="utf-8"), "{ not json")

        store.path.write_text(
            json.dumps({"version": 999, "entries": [], "idempotency": {}, "counter": 0}),
            encoding="utf-8",
        )
        with self.assertRaises(QueueStoreError):
            store.load()
        self.assertIn("999", store.path.read_text(encoding="utf-8"))

    def test_service_does_not_touch_existing_data_files(self):
        old_records = self.root / "records.jsonl"
        old_counter = self.root / "counter.json"
        old_records.write_text('{"id": "SM-2026-0001"}\n', encoding="utf-8")
        old_counter.write_text('{"year": 2026, "value": 26}', encoding="utf-8")
        before = (old_records.read_bytes(), old_counter.read_bytes())

        service = make_service(self.root, clock=self.clock)
        service.submit(submission("req-1"))
        service.call_next()
        restarted = make_service(self.root, clock=self.clock)
        restarted.expire_due(self.clock.advance(121))

        self.assertEqual((old_records.read_bytes(), old_counter.read_bytes()), before)
        self.assertEqual(
            sorted(path.name for path in self.root.iterdir()),
            ["auto_flow", "counter.json", "records.jsonl"],
        )
        self.assertTrue((self.root / "auto_flow" / "queue.json").is_file())

    def test_memory_store_roundtrip_without_disk(self):
        store = MemoryQueueStore()
        service = make_service(Path("."), clock=self.clock, store=store)
        first = service.submit(submission("req-1"))
        service.call_next()
        restarted = make_service(Path("."), clock=self.clock, store=store)
        self.assertEqual(restarted.get(first.queue_id).status, QueueStatus.CALLED)
        replay = restarted.submit_result(submission("req-1"))
        self.assertFalse(replay.created)


class NormalizationTest(unittest.TestCase):
    """规范化载荷的纯函数行为（幂等比较口径）。"""

    def test_allowlist_and_types_enforced(self):
        with self.assertRaises(ValueError):
            normalize_submission(
                submission("req-1", optional_fields={"x": "1"}),
                optional_fields_allowlist=ALLOWLIST,
            )
        normalized = normalize_submission(
            submission("req-1", style_id=" style-a ", optional_fields={"note": "a"}),
            optional_fields_allowlist=ALLOWLIST,
        )
        self.assertEqual(normalized.style_id, "style-a")
        self.assertEqual(normalized.capture_consent_id, "consent-1")
        self.assertTrue(normalized.fingerprint.startswith("sha256:"))

    def test_fingerprint_ignores_key_order(self):
        one = normalize_submission(
            submission("a", optional_fields={"note": "1", "wechat": "2"}),
            optional_fields_allowlist=ALLOWLIST,
        )
        two = normalize_submission(
            submission("b", optional_fields={"wechat": "2", "note": "1"}),
            optional_fields_allowlist=ALLOWLIST,
        )
        self.assertEqual(one.fingerprint, two.fingerprint)
        different = normalize_submission(
            submission("c", optional_fields={"note": "1"}),
            optional_fields_allowlist=ALLOWLIST,
        )
        self.assertNotEqual(one.fingerprint, different.fingerprint)


if __name__ == "__main__":
    unittest.main()
