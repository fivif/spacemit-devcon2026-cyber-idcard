# -*- coding: utf-8 -*-
"""T-12 拍摄同意接入测试（ConsentService / consent_handlers / 前端脚本）。

运行（项目根目录）：
    python3 -m unittest app.tests.test_auto_flow_consent -v

覆盖验收（T-12 任务合同）：
- 个人字段留空可获取拍摄同意并入队提交（optional_fields 缺省，全程无个人数据）；
- 缺失、撤回、错误用途、跨预约复用同意均被拒绝；
- 同意元数据重启恢复（新 service 读同一存储 -> validate 仍通过）；
- 公共状态不返回同意内容（stats 聚合 + state_projection 白名单双重验证）。
另覆盖：旧格式 `{agree:true,kind:"photo"}` 请求/响应形状、拒绝同意 200
consent_declined、TTL 过期、并发首次绑定只有一个赢家、原子写入与旧文件不受影响、
损坏/版本不符文件报错且不被覆盖、前端脚本 node --check 与纯逻辑契约（有 node 时）。

约定：全部离线（tmp 目录 + 本文件 FakeClock），不联网、不碰生产路径、不写项目仓库；
只做人像分割、不做人脸识别——告知文案测试只断言「不做人脸识别」的否定表述。
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from app.auto_flow import errors
from app.auto_flow.consent_handlers import (
    LEGACY_KIND_TO_PURPOSE,
    LEGACY_PURPOSE_TEXT,
    LEGACY_RETENTION_TEXT,
    authorize_queue_submission,
    handle_consent_request,
    release_queue_binding,
    verify_queue_start,
)
from app.auto_flow.consent_service import (
    DEFAULT_TTL_SECONDS,
    LEGACY_TTL_ENV,
    ConsentBindingError,
    ConsentService,
    ConsentStore,
    ConsentStoreError,
    MemoryConsentStore,
    default_ttl_seconds,
)
from app.auto_flow.models import QueueSubmission
from app.auto_flow.queue_service import QueueService
from app.auto_flow.queue_store import QueueStore
from app.auto_flow.state_projection import public_state

UTC = timezone.utc
PROJECT_ROOT = Path(__file__).resolve().parents[2]
CONSENT_SCRIPT = PROJECT_ROOT / "app" / "static" / "auto-flow-consent.js"
NODE = shutil.which("node")
CONSENT_ID_RE = re.compile(r"^[0-9a-f]{32}$")


class FakeClock:
    """可控时钟：支持 .now() 与直接调用（与 T-14 ClockPort 两种用法兼容）。"""

    def __init__(self, start: datetime | None = None) -> None:
        self._now = start or datetime(2026, 9, 30, 0, 0, 0, tzinfo=UTC)

    def now(self) -> datetime:
        return self._now

    def __call__(self) -> datetime:
        return self._now

    def advance(self, seconds: float = 0.0) -> datetime:
        self._now = self._now + timedelta(seconds=seconds)
        return self._now


def make_service(
    root: Path | None = None,
    *,
    clock: FakeClock | None = None,
    ttl_seconds: int = 600,
    store: ConsentStore | MemoryConsentStore | None = None,
) -> ConsentService:
    if store is None:
        if root is None:
            store = MemoryConsentStore()
        else:
            store = ConsentStore(root)
    return ConsentService(store, ttl_seconds=ttl_seconds, clock=clock or FakeClock())


def make_queue(root: Path, clock: FakeClock) -> QueueService:
    return QueueService(
        QueueStore(root),
        max_active=8,
        start_timeout_seconds=120,
        idempotency_ttl_seconds=86400,
        optional_fields_allowlist=("note",),
        clock=clock,
        event_year=2026,
    )


def submission(
    request_id: str,
    consent_id: str,
    *,
    style_id: str = "style-a",
    optional_fields: dict[str, str] | None = None,
) -> QueueSubmission:
    return QueueSubmission(
        request_id=request_id,
        style_id=style_id,
        capture_consent_id=consent_id,
        optional_fields=optional_fields,
    )


class TempRootTest(unittest.TestCase):
    """所有用例统一用临时目录，保证不落任何生产路径。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="auto-flow-consent-")
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.clock = FakeClock()


class ConsentServiceCoreTest(TempRootTest):
    def test_grant_and_validate_capture_with_blank_subject(self):
        service = make_service(clock=self.clock)
        record = service.record("capture", True)  # subject_id 留空 = 匿名同意
        self.assertRegex(record.consent_id, CONSENT_ID_RE)
        self.assertEqual(record.purpose, "capture")
        self.assertTrue(record.granted)
        self.assertIsNone(record.queue_id)
        self.assertTrue(service.validate(record.consent_id, "capture", ""))
        self.assertEqual(service.status(record.consent_id), "valid")
        self.assertEqual(service.stats()["live_capture"], 1)

    def test_missing_and_unknown_tokens_rejected(self):
        service = make_service(clock=self.clock)
        self.assertFalse(service.validate("", "capture", ""))
        self.assertFalse(service.validate("0" * 32, "capture", ""))
        self.assertFalse(service.validate(None, "capture", ""))  # type: ignore[arg-type]
        self.assertEqual(service.status("0" * 32), "missing")

    def test_revoked_consent_is_always_invalid(self):
        service = make_service(clock=self.clock)
        record = service.record("capture", True)
        self.assertTrue(service.revoke(record.consent_id))
        self.assertFalse(service.validate(record.consent_id, "capture", ""))
        self.assertFalse(service.validate(record.consent_id, "capture", "queue-1"))
        self.assertEqual(service.status(record.consent_id), "revoked")
        self.assertFalse(service.revoke(record.consent_id))  # 重复撤回无状态变化
        self.assertEqual(service.stats()["revoked"], 1)

    def test_purpose_isolation_both_directions(self):
        service = make_service(clock=self.clock)
        capture = service.record("capture", True)
        external = service.record("external_processing", True)
        self.assertFalse(service.validate(capture.consent_id, "external_processing", ""))
        self.assertFalse(service.validate(external.consent_id, "capture", ""))
        self.assertTrue(service.validate(capture.consent_id, "capture", ""))
        self.assertTrue(service.validate(external.consent_id, "external_processing", ""))

    def test_expired_consent_rejected_and_reported(self):
        service = make_service(clock=self.clock, ttl_seconds=10)
        record = service.record("capture", True)
        self.clock.advance(11)
        self.assertFalse(service.validate(record.consent_id, "capture", ""))
        self.assertEqual(service.status(record.consent_id), "expired")
        self.assertEqual(service.stats()["expired"], 1)

    def test_record_denied_is_never_valid(self):
        service = make_service(clock=self.clock)
        record = service.record("capture", False)
        self.assertFalse(service.validate(record.consent_id, "capture", ""))
        self.assertEqual(service.status(record.consent_id), "denied")

    def test_bad_arguments_raise_value_error(self):
        service = make_service(clock=self.clock)
        with self.assertRaises(ValueError):
            service.record("marketing", True)
        with self.assertRaises(ValueError):
            service.record("capture", "yes")  # type: ignore[arg-type]
        with self.assertRaises(ValueError):
            service.record("capture", True, subject_id=123)  # type: ignore[arg-type]
        with self.assertRaises(ValueError):
            service.validate("x", "marketing", "")
        with self.assertRaises(ValueError):
            make_service(clock=self.clock, ttl_seconds=0)

    def test_default_ttl_reads_legacy_env_and_defaults_to_600(self):
        self.assertEqual(default_ttl_seconds({}), DEFAULT_TTL_SECONDS)
        self.assertEqual(default_ttl_seconds({LEGACY_TTL_ENV: "900"}), 900)
        with self.assertRaises(ValueError):
            default_ttl_seconds({LEGACY_TTL_ENV: "abc"})
        with self.assertRaises(ValueError):
            default_ttl_seconds({LEGACY_TTL_ENV: "-1"})

    def test_constructor_reads_env_when_ttl_not_given(self):
        with mock.patch.dict(os.environ, {LEGACY_TTL_ENV: "777"}):
            service = ConsentService(MemoryConsentStore(), clock=self.clock)
        self.assertEqual(service.ttl_seconds, 777)

    def test_subject_bound_consent_requires_matching_subject(self):
        service = make_service(clock=self.clock)
        record = service.record("external_processing", True, "job-1")
        self.assertTrue(service.validate(record.consent_id, "external_processing", "job-1"))
        self.assertFalse(service.validate(record.consent_id, "external_processing", "job-2"))
        self.assertFalse(service.validate(record.consent_id, "external_processing", ""))
        self.assertEqual(record.job_id, "job-1")

    def test_unbound_consent_requires_blank_subject(self):
        service = make_service(clock=self.clock)
        record = service.record("capture", True)
        self.assertTrue(service.validate(record.consent_id, "capture", ""))
        self.assertFalse(service.validate(record.consent_id, "capture", "queue-x"))


class ConsentBindingTest(TempRootTest):
    def test_first_bind_then_idempotent_replay(self):
        service = make_service(clock=self.clock)
        record = service.record("capture", True)
        bound = service.bind(record.consent_id, "queue-1")
        self.assertEqual(bound.queue_id, "queue-1")
        replay = service.bind(record.consent_id, "queue-1")  # 幂等重放
        self.assertEqual(replay.queue_id, "queue-1")
        self.assertTrue(service.validate(record.consent_id, "capture", "queue-1"))
        self.assertEqual(service.binding_of(record.consent_id), "queue-1")

    def test_cross_queue_reuse_rejected(self):
        service = make_service(clock=self.clock)
        record = service.record("capture", True)
        service.bind(record.consent_id, "queue-1")
        with self.assertRaises(ConsentBindingError) as ctx:
            service.bind(record.consent_id, "queue-2")
        self.assertEqual(ctx.exception.reason, "bound_elsewhere")
        self.assertFalse(service.validate(record.consent_id, "capture", "queue-2"))
        self.assertTrue(service.validate(record.consent_id, "capture", "queue-1"))

    def test_bind_requires_capture_purpose(self):
        service = make_service(clock=self.clock)
        external = service.record("external_processing", True)
        with self.assertRaises(ConsentBindingError) as ctx:
            service.bind(external.consent_id, "queue-1")
        self.assertEqual(ctx.exception.reason, "wrong_purpose")
        bound = service.bind_job(external.consent_id, "job-1")
        self.assertEqual(bound.job_id, "job-1")
        with self.assertRaises(ConsentBindingError):
            service.bind_job(external.consent_id, "job-2")

    def test_bind_rejects_missing_revoked_expired_and_blank(self):
        service = make_service(clock=self.clock)
        with self.assertRaises(ConsentBindingError) as ctx:
            service.bind("0" * 32, "queue-1")
        self.assertEqual(ctx.exception.reason, "unknown")
        with self.assertRaises(ConsentBindingError) as ctx:
            service.bind("", "queue-1")
        self.assertEqual(ctx.exception.reason, "invalid_request")
        with self.assertRaises(ConsentBindingError) as ctx:
            service.bind("0" * 32, "")
        self.assertEqual(ctx.exception.reason, "invalid_request")
        revoked = service.record("capture", True)
        service.revoke(revoked.consent_id)
        with self.assertRaises(ConsentBindingError) as ctx:
            service.bind(revoked.consent_id, "queue-1")
        self.assertEqual(ctx.exception.reason, "invalid")
        expired = service.record("capture", True)
        self.clock.advance(601)
        with self.assertRaises(ConsentBindingError) as ctx:
            service.bind(expired.consent_id, "queue-1")
        self.assertEqual(ctx.exception.reason, "invalid")

    def test_release_binding_allows_rebind_to_other_queue(self):
        service = make_service(clock=self.clock)
        record = service.record("capture", True)
        service.bind(record.consent_id, "queue-1")
        self.assertFalse(service.release_binding(record.consent_id, "queue-2"))
        self.assertTrue(service.release_binding(record.consent_id, "queue-1"))
        self.assertIsNone(service.binding_of(record.consent_id))
        rebound = service.bind(record.consent_id, "queue-2")
        self.assertEqual(rebound.queue_id, "queue-2")

    def test_concurrent_first_bind_has_exactly_one_winner(self):
        service = make_service(clock=self.clock)
        record = service.record("capture", True)
        targets = [f"queue-{index}" for index in range(6)]
        barrier = threading.Barrier(len(targets))
        winners: list[str] = []
        losers: list[str] = []
        lock = threading.Lock()

        def attempt(queue_id: str) -> None:
            barrier.wait()
            try:
                service.bind(record.consent_id, queue_id)
                with lock:
                    winners.append(queue_id)
            except ConsentBindingError as exc:
                with lock:
                    losers.append(exc.reason)

        threads = [threading.Thread(target=attempt, args=(target,)) for target in targets]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(len(winners), 1)
        self.assertEqual(len(losers), len(targets) - 1)
        self.assertTrue(all(reason == "bound_elsewhere" for reason in losers))
        self.assertEqual(service.binding_of(record.consent_id), winners[0])


class ConsentHandlersTest(TempRootTest):
    def test_legacy_request_keeps_legacy_response_shape(self):
        service = make_service(clock=self.clock)
        status, payload = handle_consent_request({"agree": True, "kind": "photo"}, service)
        self.assertEqual(status, 200)
        self.assertEqual(
            set(payload),
            {"ok", "token", "expires_in", "kind", "purpose", "retention", "version", "at"},
        )
        self.assertTrue(payload["ok"])
        self.assertRegex(payload["token"], CONSENT_ID_RE)
        self.assertEqual(payload["expires_in"], 600)
        self.assertEqual(payload["kind"], "photo")
        self.assertEqual(payload["purpose"], LEGACY_PURPOSE_TEXT)
        self.assertEqual(payload["retention"], LEGACY_RETENTION_TEXT)
        self.assertEqual(payload["version"], 1)
        self.assertTrue(payload["at"].endswith("Z"))
        self.assertTrue(service.validate(payload["token"], "capture", ""))

    def test_legacy_missing_kind_defaults_to_photo(self):
        service = make_service(clock=self.clock)
        status, payload = handle_consent_request({"agree": True}, service)
        self.assertEqual(status, 200)
        self.assertEqual(payload["kind"], "photo")
        self.assertTrue(service.validate(payload["token"], "capture", ""))

    def test_denied_or_missing_agree_returns_consent_declined_without_record(self):
        service = make_service(clock=self.clock)
        for body in ({"agree": False, "kind": "photo"}, {"kind": "photo"}, {"agree": 0}):
            with self.subTest(body=body):
                status, payload = handle_consent_request(body, service)
                self.assertEqual(status, 200)
                self.assertEqual(payload, {"ok": False, "error": "consent_declined"})
        self.assertEqual(service.stats()["issued"], 0)

    def test_new_purpose_capture_and_external_are_isolated(self):
        service = make_service(clock=self.clock)
        status, capture = handle_consent_request(
            {"agree": True, "purpose": "capture"}, service, subject_id=""
        )
        self.assertEqual(status, 200)
        self.assertEqual(capture["consent"]["purpose"], "capture")
        self.assertEqual(capture["consent_id"], capture["token"])
        self.assertEqual(capture["purpose"], "capture")
        self.assertTrue(service.validate(capture["token"], "capture", ""))
        self.assertFalse(service.validate(capture["token"], "external_processing", ""))

        status, external = handle_consent_request(
            {"agree": True, "purpose": "external_processing"}, service
        )
        self.assertEqual(status, 200)
        self.assertEqual(external["consent"]["purpose"], "external_processing")
        self.assertTrue(
            service.validate(external["token"], "external_processing", "")
        )
        self.assertFalse(service.validate(external["token"], "capture", ""))

    def test_invalid_purpose_and_kind_rejected_as_invalid_request(self):
        service = make_service(clock=self.clock)
        with self.assertRaises(errors.ApiError) as ctx:
            handle_consent_request({"agree": True, "purpose": "marketing"}, service)
        self.assertEqual(ctx.exception.code, errors.INVALID_REQUEST)
        with self.assertRaises(errors.ApiError) as ctx:
            handle_consent_request({"agree": True, "kind": "selfie"}, service)
        self.assertEqual(ctx.exception.code, errors.INVALID_REQUEST)
        with self.assertRaises(errors.ApiError) as ctx:
            handle_consent_request(None, service)
        self.assertEqual(ctx.exception.code, errors.INVALID_REQUEST)
        with self.assertRaises(errors.ApiError) as ctx:
            handle_consent_request(["agree"], service)  # type: ignore[arg-type]
        self.assertEqual(ctx.exception.code, errors.INVALID_REQUEST)
        self.assertEqual(service.stats()["issued"], 0)

    def test_granted_field_accepted_as_legacy_alias(self):
        service = make_service(clock=self.clock)
        status, payload = handle_consent_request({"granted": True}, service)
        self.assertEqual(status, 200)
        self.assertTrue(service.validate(payload["token"], "capture", ""))

    def test_blank_personal_fields_can_consent_and_submit(self):
        """验收 1：个人字段留空（不传 optional_fields）也能同意并入队、绑定成功。"""
        queue = make_queue(self.root, self.clock)
        service = make_service(self.root, clock=self.clock)
        status, payload = handle_consent_request({"agree": True, "kind": "photo"}, service)
        token = payload["token"]

        result = queue.submit_result(submission("req-1", token))
        self.assertTrue(result.created)
        entry = result.entry
        self.assertEqual(entry.status.value, "queued")
        self.assertIsNone(entry.consent_id)  # 提交阶段不写 consent，绑定由处理器做

        bound = authorize_queue_submission(
            service, capture_consent_id=token, queue_id=entry.queue_id
        )
        self.assertEqual(bound.queue_id, entry.queue_id)
        self.assertTrue(service.validate(token, "capture", entry.queue_id))
        started = verify_queue_start(service, capture_consent_id=token, queue_id=entry.queue_id)
        self.assertEqual(started.consent_id, token)

    def test_same_request_id_replay_is_allowed_other_queue_is_rejected(self):
        queue = make_queue(self.root, self.clock)
        service = make_service(self.root, clock=self.clock)
        _, payload = handle_consent_request({"agree": True}, service)
        token = payload["token"]

        first = queue.submit_result(submission("req-1", token))
        authorize_queue_submission(service, capture_consent_id=token, queue_id=first.entry.queue_id)
        replay = queue.submit_result(submission("req-1", token))
        self.assertFalse(replay.created)
        self.assertEqual(replay.entry.queue_id, first.entry.queue_id)
        again = authorize_queue_submission(
            service, capture_consent_id=token, queue_id=replay.entry.queue_id
        )
        self.assertEqual(again.queue_id, first.entry.queue_id)

        second = queue.submit_result(submission("req-2", token))
        self.assertTrue(second.created)
        with self.assertRaises(errors.ApiError) as ctx:
            authorize_queue_submission(
                service, capture_consent_id=token, queue_id=second.entry.queue_id
            )
        self.assertEqual(ctx.exception.code, errors.CONSENT_REQUIRED)
        cancelled = queue.mark_cancelled(second.entry.queue_id, reason="同意已绑定其它预约")
        self.assertEqual(cancelled.status.value, "cancelled")
        self.assertTrue(service.validate(token, "capture", first.entry.queue_id))

    def test_authorize_queue_submission_rejections(self):
        service = make_service(clock=self.clock)
        queue = make_queue(self.root, self.clock)
        with self.assertRaises(errors.ApiError) as ctx:
            authorize_queue_submission(service, capture_consent_id="", queue_id="queue-1")
        self.assertEqual(ctx.exception.code, errors.INVALID_REQUEST)
        with self.assertRaises(errors.ApiError) as ctx:
            authorize_queue_submission(service, capture_consent_id="0" * 32, queue_id="queue-1")
        self.assertEqual(ctx.exception.code, errors.CONSENT_REQUIRED)

        revoked = service.record("capture", True)
        service.revoke(revoked.consent_id)
        with self.assertRaises(errors.ApiError) as ctx:
            authorize_queue_submission(
                service, capture_consent_id=revoked.consent_id, queue_id="queue-1"
            )
        self.assertEqual(ctx.exception.code, errors.CONSENT_REQUIRED)

        external = service.record("external_processing", True)
        with self.assertRaises(errors.ApiError) as ctx:
            authorize_queue_submission(
                service, capture_consent_id=external.consent_id, queue_id="queue-1"
            )
        self.assertEqual(ctx.exception.code, errors.CONSENT_REQUIRED)

        capture = service.record("capture", True)
        authorize_queue_submission(service, capture_consent_id=capture.consent_id, queue_id="queue-1")
        with self.assertRaises(errors.ApiError) as ctx:
            authorize_queue_submission(
                service, capture_consent_id=capture.consent_id, queue_id="queue-2"
            )
        self.assertEqual(ctx.exception.code, errors.CONSENT_REQUIRED)

    def test_verify_queue_start_requires_bound_capture_consent(self):
        service = make_service(clock=self.clock)
        unbound = service.record("capture", True)
        with self.assertRaises(errors.ApiError) as ctx:
            verify_queue_start(service, capture_consent_id=unbound.consent_id, queue_id="queue-1")
        self.assertEqual(ctx.exception.code, errors.CONSENT_REQUIRED)

        bound = service.record("capture", True)
        service.bind(bound.consent_id, "queue-1")
        record = verify_queue_start(service, capture_consent_id=bound.consent_id, queue_id="queue-1")
        self.assertEqual(record.queue_id, "queue-1")
        with self.assertRaises(errors.ApiError) as ctx:
            verify_queue_start(service, capture_consent_id=bound.consent_id, queue_id="queue-2")
        self.assertEqual(ctx.exception.code, errors.CONSENT_REQUIRED)
        with self.assertRaises(errors.ApiError) as ctx:
            verify_queue_start(service, capture_consent_id="", queue_id="queue-1")
        self.assertEqual(ctx.exception.code, errors.INVALID_REQUEST)

    def test_release_queue_binding_helper(self):
        service = make_service(clock=self.clock)
        record = service.record("capture", True)
        service.bind(record.consent_id, "queue-1")
        self.assertFalse(release_queue_binding(service, capture_consent_id="", queue_id="queue-1"))
        self.assertTrue(
            release_queue_binding(service, capture_consent_id=record.consent_id, queue_id="queue-1")
        )
        self.assertIsNone(service.binding_of(record.consent_id))
        with self.assertRaises(errors.ApiError):
            verify_queue_start(service, capture_consent_id=record.consent_id, queue_id="queue-1")

    def test_legacy_kind_mapping_is_documented_subset(self):
        self.assertEqual(LEGACY_KIND_TO_PURPOSE, {"photo": "capture", "capture": "capture"})


class ConsentPersistenceTest(TempRootTest):
    def test_restart_restores_validate_and_binding(self):
        first = make_service(self.root, clock=self.clock)
        record = first.record("capture", True)
        first.bind(record.consent_id, "queue-1")
        first.record("external_processing", True, "job-1")

        second = make_service(self.root, clock=self.clock)  # 新进程读同一存储
        self.assertTrue(second.validate(record.consent_id, "capture", "queue-1"))
        self.assertFalse(second.validate(record.consent_id, "capture", "queue-1".replace("1", "2")))
        self.assertEqual(second.binding_of(record.consent_id), "queue-1")
        self.assertEqual(second.get(record.consent_id).queue_id, "queue-1")
        restored = second.stats()
        self.assertEqual(restored["live_capture"], 1)
        self.assertEqual(restored["live_external"], 1)

    def test_restart_preserves_revocation(self):
        first = make_service(self.root, clock=self.clock)
        record = first.record("capture", True)
        first.revoke(record.consent_id)
        second = make_service(self.root, clock=self.clock)
        self.assertFalse(second.validate(record.consent_id, "capture", ""))
        self.assertEqual(second.status(record.consent_id), "revoked")
        self.assertFalse(second.revoke(record.consent_id))

    def test_restart_keeps_per_record_expiry(self):
        first = make_service(self.root, clock=self.clock, ttl_seconds=10)
        record = first.record("capture", True)
        later = FakeClock()
        later.advance(11)
        second = make_service(self.root, clock=later, ttl_seconds=9999)
        self.assertFalse(second.validate(record.consent_id, "capture", ""))
        self.assertEqual(second.status(record.consent_id), "expired")

    def test_stored_ttl_is_reused_when_not_configured(self):
        first = make_service(self.root, clock=self.clock, ttl_seconds=1234)
        first.record("capture", True)
        with mock.patch.dict(os.environ, {}, clear=True):
            second = ConsentService(ConsentStore(self.root), clock=self.clock)
        self.assertEqual(second.ttl_seconds, 1234)

    def test_atomic_write_and_legacy_files_untouched(self):
        legacy_records = self.root / "records.jsonl"
        legacy_counter = self.root / "counter.json"
        legacy_records.write_text('{"legacy":true}\n', encoding="utf-8")
        legacy_counter.write_text('{"year":2026,"value":3}', encoding="utf-8")
        before_records = legacy_records.read_bytes()
        before_counter = legacy_counter.read_bytes()

        service = make_service(self.root, clock=self.clock)
        record = service.record("capture", True)
        service.bind(record.consent_id, "queue-1")

        self.assertEqual(legacy_records.read_bytes(), before_records)
        self.assertEqual(legacy_counter.read_bytes(), before_counter)
        path = self.root / "auto_flow" / "consent.json"
        self.assertTrue(path.is_file())
        document = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(document["version"], ConsentStore.SCHEMA_VERSION)
        self.assertEqual(len(document["records"]), 1)
        self.assertEqual(document["records"][0]["consent_id"], record.consent_id)
        self.assertEqual(document["records"][0]["subject_id"], "queue-1")
        leftovers = list((self.root / "auto_flow").glob("*.tmp"))
        self.assertEqual(leftovers, [])

    def test_corrupt_file_raises_and_is_not_overwritten(self):
        directory = self.root / "auto_flow"
        directory.mkdir(parents=True)
        path = directory / "consent.json"
        path.write_text("{not json", encoding="utf-8")
        before = path.read_bytes()
        with self.assertRaises(ConsentStoreError):
            make_service(self.root, clock=self.clock)
        self.assertEqual(path.read_bytes(), before)

    def test_unsupported_version_rejected_without_overwrite(self):
        directory = self.root / "auto_flow"
        directory.mkdir(parents=True)
        path = directory / "consent.json"
        path.write_text(json.dumps({"version": 99, "records": []}), encoding="utf-8")
        with self.assertRaises(ConsentStoreError):
            make_service(self.root, clock=self.clock)
        self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["version"], 99)


class ConsentPublicProjectionTest(TempRootTest):
    def test_stats_and_public_state_never_return_consent_content(self):
        service = make_service(self.root, clock=self.clock)
        record = service.record("capture", True)
        service.bind(record.consent_id, "queue-secret-01")
        service.record("external_processing", True, "subject-secret")
        stats = service.stats()
        text = json.dumps(stats, ensure_ascii=False)
        for secret in (record.consent_id, "subject-secret", "queue-secret-01"):
            self.assertNotIn(secret, text)
        self.assertEqual(
            set(stats),
            {"issued", "live", "revoked", "expired", "live_capture", "live_external", "ttl_s"},
        )

        # 把内部条目（含同意 / 队列 / 编号等敏感值）喂给公共投影，输出必须脱敏
        internal_like = [
            {
                "status": "queued",
                "display_number": "SM-2026-0007",
                "queue_id": "queue-secret-01",
                "consent_id": record.consent_id,
                "subject_id": "subject-secret",
            }
        ]
        projected = public_state(queue_entries=internal_like, capture=None, health=None)
        projected_text = json.dumps(projected, ensure_ascii=False)
        for secret in (
            record.consent_id,
            "subject-secret",
            "queue-secret-01",
            "SM-2026-0007",
        ):
            self.assertNotIn(secret, projected_text)
        self.assertEqual(set(projected), {"queue", "capture", "capabilities"})


@unittest.skipUnless(NODE, "本机无 node，跳过前端脚本检查")
class ConsentScriptTest(unittest.TestCase):
    def _run_node(self, args: list[str]) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [NODE, *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
        )

    def test_node_syntax_check(self):
        result = self._run_node(["--check", str(CONSENT_SCRIPT)])
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_notice_copy_and_pure_logic_contract(self):
        script = "\n".join(
            [
                'const m = require(process.argv[2].replace(/\\\\/g, "/"));',
                'const lines = m.noticeLines().join("");',
                'const notice = m.noticeText();',
                'if (!notice.startsWith("拍摄告知（请阅读后点击“同意”）")) throw new Error("告知标题与指定原文不同");',
                'if (!lines.includes("开发者电子证件照，仅本人可领取")) throw new Error("告知缺少指定拍摄用途");',
                'if (!lines.includes("人像抠图（将人物从背景中分离）") || !lines.includes("不会进行人脸识别、身份比对或用于建任何人脸库或特征库")) throw new Error("告知处理范围与指定原文不同");',
                'if (!lines.includes("单独弹出一次同意提示") || !lines.includes("不同意不影响基础拍摄与取图")) throw new Error("告知缺少第三方处理说明");',
                'if (!lines.includes("链接自生成之日起【1】日内有效") || !lines.includes("活动不会留存任何副本") || !lines.includes("协助删除")) throw new Error("告知缺少指定留存与删除说明");',
                'if (!lines.includes("姓名、昵称等字段均可留空，仅作为排队标识")) throw new Error("告知缺少自愿填写说明");',
                'if (!notice.endsWith("点击“同意”即表示您已阅读并理解上述规则，同意后您将获取专属排队号码并加入排队。")) throw new Error("告知确认语与指定原文不同");',
                'const req = m.buildCaptureConsentRequest();',
                'if (req.agree !== true || req.purpose !== "capture") throw new Error("请求体错误");',
                'const ok = m.interpretConsentResponse(200, {ok:true, token:"t1", purpose:"capture"});',
                'if (!ok.ok || ok.token !== "t1") throw new Error("成功响应解析错误");',
                'const declined = m.interpretConsentResponse(200, {ok:false, error:"consent_declined"});',
                'if (!declined.declined) throw new Error("拒绝响应解析错误");',
                'm.createConsentController({request: (body) => Promise.resolve({status:200, payload:{ok:true, token:"c1", purpose:body.purpose}})})',
                '  .grant().then((s) => { if (s.phase !== "granted" || s.token !== "c1") throw new Error("状态机错误"); process.stdout.write("OK"); });',
            ]
        )
        with tempfile.TemporaryDirectory(prefix="auto-flow-consent-js-") as tmp:
            script_path = Path(tmp) / "check.js"
            script_path.write_text(script, encoding="utf-8")
            result = self._run_node([str(script_path), str(CONSENT_SCRIPT)])
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("OK", result.stdout)


if __name__ == "__main__":
    unittest.main()
