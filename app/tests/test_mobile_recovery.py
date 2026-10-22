"""恢复模块与可选控制会话持久化测试，只使用临时目录与假时钟。"""

from __future__ import annotations

import json
import os
import secrets
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from app.auto_flow.http_handlers import AutoFlowServices, QueueSessionRegistry
from app.auto_flow.recovery_service import RecoveryRegistry


class RegistryTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="mobile-recovery-")
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / "nested" / "state.json"
        self.now = 1000.0

    def clock(self) -> float:
        return self.now

    def recovery(self, ttl_seconds: float = 604800) -> RecoveryRegistry:
        return RecoveryRegistry(self.path, clock=self.clock, ttl_seconds=ttl_seconds)

    def write_bad(self, payload: str) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(payload, encoding="utf-8")


class RecoveryRegistryTest(RegistryTestCase):
    def test_missing_storage_does_not_create_file(self) -> None:
        registry = self.recovery()
        self.assertIsNone(registry.get(secrets.token_urlsafe(24)))
        self.assertIsNone(registry.current(secrets.token_urlsafe(24)))
        self.assertFalse(self.path.exists())

    def test_restart_preserves_record_and_stable_bind(self) -> None:
        first = self.recovery().bind("queue-1")
        restarted = self.recovery()
        self.assertEqual(restarted.get(first["token"]), first)
        self.assertEqual(restarted.current(first["browser_token"]), first)
        self.now += 100
        self.assertEqual(restarted.bind("queue-1", first["browser_token"]), first)
        self.assertEqual(self.recovery().get(first["token"]), first)

    def test_lost_response_retry_associates_multiple_browsers(self) -> None:
        registry = self.recovery()
        first = registry.bind("queue-1")
        second = registry.bind("queue-1")
        self.assertEqual(first["token"], second["token"])
        self.assertEqual(first["expires_at"], second["expires_at"])
        self.assertNotEqual(first["browser_token"], second["browser_token"])
        restarted = self.recovery()
        self.assertEqual(restarted.current(first["browser_token"]), first)
        self.assertEqual(restarted.current(second["browser_token"]), second)
        self.assertEqual(restarted.get(first["token"]), second)
        self.assertEqual(restarted.bind("queue-1", first["browser_token"]), first)

    def test_current_and_history_are_separate_after_new_registration(self) -> None:
        registry = self.recovery()
        old = registry.bind("queue-old")
        new = registry.bind("queue-new", old["browser_token"])
        self.assertEqual(new["browser_token"], old["browser_token"])
        self.assertNotEqual(old["token"], new["token"])
        restarted = self.recovery()
        self.assertEqual(restarted.current(old["browser_token"]), new)
        self.assertEqual(restarted.get(old["token"]), old)
        self.assertEqual(restarted.get(new["token"]), new)
        records = json.loads(self.path.read_text(encoding="utf-8"))["records"]
        self.assertEqual(records, [old, new])

    def test_latest_binding_wins_even_with_same_clock(self) -> None:
        registry = self.recovery()
        first = registry.bind("queue-1")
        second = registry.bind("queue-2", first["browser_token"])
        third = registry.bind("queue-1", first["browser_token"])
        self.assertEqual(third["token"], first["token"])
        self.assertEqual(self.recovery().current(first["browser_token"]), third)
        self.assertEqual(self.recovery().get(second["token"]), second)

    def test_expiry_boundary_does_not_delete_or_rewrite_history(self) -> None:
        registry = self.recovery(ttl_seconds=10)
        record = registry.bind("queue-1")
        original = self.path.read_bytes()
        self.now = record["expires_at"] - 0.001
        self.assertEqual(registry.get(record["token"]), record)
        self.now = record["expires_at"]
        self.assertIsNone(registry.get(record["token"]))
        self.assertIsNone(registry.current(record["browser_token"]))
        self.assertIsNone(self.recovery().get(record["token"]))
        self.assertEqual(self.path.read_bytes(), original)
        new = registry.bind("queue-1", record["browser_token"])
        self.assertNotEqual(new["token"], record["token"])
        self.assertEqual(new["browser_token"], record["browser_token"])
        self.assertEqual(
            json.loads(self.path.read_text(encoding="utf-8"))["records"],
            [record, new],
        )
        self.assertIsNone(registry.get(record["token"]))

    def test_expired_latest_does_not_fall_back_to_live_history(self) -> None:
        long_lived = self.recovery(ttl_seconds=100)
        first = long_lived.bind("queue-1")
        short_lived = self.recovery(ttl_seconds=1)
        latest = short_lived.bind("queue-2", first["browser_token"])
        self.now = latest["expires_at"]
        restarted = self.recovery()
        self.assertIsNone(restarted.current(first["browser_token"]))
        self.assertEqual(restarted.get(first["token"]), first)

    def test_invalid_lookup_formats_and_unknown_tokens_return_none(self) -> None:
        registry = self.recovery()
        record = registry.bind("queue-1")
        values = [
            None, True, 12, [], {}, "", "a" * 31, "a" * 33,
            "!" * 32, "中" * 32, record["token"] + "\n",
            " " + record["token"], secrets.token_urlsafe(24),
        ]
        for value in values:
            with self.subTest(value=value):
                self.assertIsNone(registry.get(value))
                self.assertIsNone(registry.current(value))
        self.assertIsNone(registry.get(record["browser_token"]))
        self.assertIsNone(registry.current(record["token"]))

    def test_unknown_browser_is_not_adopted(self) -> None:
        registry = self.recovery()
        for supplied in [None, "bad", {}, secrets.token_urlsafe(24)]:
            with self.subTest(supplied=supplied):
                result = registry.bind("queue-1", supplied)
                self.assertNotEqual(result["browser_token"], supplied)
                self.assertRegex(result["browser_token"], r"^[A-Za-z0-9_-]{32}$")

    def test_random_tokens_are_independent_and_not_derived_from_queue(self) -> None:
        registry = self.recovery()
        original_random = secrets.token_urlsafe
        with patch(
            "app.auto_flow.recovery_service.secrets.token_urlsafe",
            wraps=original_random,
        ) as random_source:
            records = [registry.bind(f"queue-{index}") for index in range(64)]
        self.assertEqual(random_source.call_count, 128)
        self.assertTrue(all(call.args == (24,) for call in random_source.call_args_list))
        values = [record[field] for record in records for field in ("token", "browser_token")]
        self.assertEqual(len(set(values)), 128)
        for value in values:
            self.assertRegex(value, r"^[A-Za-z0-9_-]{32}$")

    def test_random_collisions_retry_across_both_token_namespaces(self) -> None:
        registry = self.recovery()
        with patch(
            "app.auto_flow.recovery_service.secrets.token_urlsafe",
            side_effect=["a" * 32, "a" * 32, "b" * 32, "b" * 32, "c" * 32, "c" * 32, "d" * 32],
        ):
            first = registry.bind("queue-1")
            second = registry.bind("queue-2")
        self.assertEqual(first["browser_token"], "b" * 32)
        self.assertEqual(second["token"], "c" * 32)
        self.assertEqual(second["browser_token"], "d" * 32)

    def test_return_values_are_copies_and_storage_has_no_personal_fields(self) -> None:
        registry = self.recovery()
        record = registry.bind("queue-1")
        original = dict(record)
        record["queue_id"] = "modified"
        fetched = registry.get(original["token"])
        fetched["queue_id"] = "modified-again"
        current = registry.current(original["browser_token"])
        current["queue_id"] = "modified-current"
        self.assertEqual(registry.get(original["token"]), original)
        self.assertEqual(set(original), {"queue_id", "token", "browser_token", "expires_at"})
        self.assertEqual(
            json.loads(self.path.read_text(encoding="utf-8")),
            {"version": 1, "records": [original]},
        )

    def test_concurrent_same_queue_binds_are_all_persisted(self) -> None:
        registry = self.recovery()
        with ThreadPoolExecutor(max_workers=8) as executor:
            records = list(executor.map(lambda index: registry.bind("queue-1"), range(32)))
        self.assertEqual(len({record["token"] for record in records}), 1)
        self.assertEqual(len({record["browser_token"] for record in records}), 32)
        restarted = self.recovery()
        for record in records:
            self.assertEqual(restarted.current(record["browser_token"]), record)
        self.assertEqual(len(json.loads(self.path.read_text(encoding="utf-8"))["records"]), 32)

    def test_atomic_replace_exposes_complete_documents_during_concurrent_writes(self) -> None:
        registry = self.recovery()
        registry.bind("initial")
        observed: list[int] = []
        original_replace = os.replace

        def observe_replace(source: Path, destination: Path) -> None:
            old = json.loads(self.path.read_text(encoding="utf-8"))
            proposed = json.loads(Path(source).read_text(encoding="utf-8"))
            self.assertEqual(Path(source).parent, self.path.parent)
            self.assertEqual(Path(destination), self.path)
            self.assertEqual(proposed["records"][:-1], old["records"])
            original_replace(source, destination)
            committed = json.loads(self.path.read_text(encoding="utf-8"))
            self.assertEqual(committed, proposed)
            observed.append(len(committed["records"]))

        with patch("app.auto_flow.recovery_service.os.replace", side_effect=observe_replace):
            with ThreadPoolExecutor(max_workers=8) as executor:
                records = list(executor.map(lambda index: registry.bind(f"queue-{index}"), range(24)))
        self.assertEqual(observed, list(range(2, 26)))
        self.assertEqual(len({record["token"] for record in records}), 24)
        self.assertEqual(len(json.loads(self.path.read_text(encoding="utf-8"))["records"]), 25)
        restarted = self.recovery()
        for record in records:
            self.assertEqual(restarted.get(record["token"]), record)

    def test_corrupt_storage_raises_without_overwriting(self) -> None:
        record = {
            "queue_id": "queue-1", "token": "a" * 32,
            "browser_token": "b" * 32, "expires_at": 2000,
        }
        invalid_records = [
            {}, {**record, "expires_at": "2000"}, {**record, "expires_at": True},
            {**record, "expires_at": float("nan")}, {**record, "expires_at": float("inf")},
            {**record, "queue_id": " "}, {**record, "queue_id": " queue-1"},
            {**record, "token": "bad"}, {**record, "browser_token": record["token"]},
            {**record, "name": "must-not-be-stored"},
        ]
        payloads = ["{", "[]", "null", "{}", '{"version":1,"version":1,"records":[]}']
        payloads.extend(json.dumps({"version": 1, "records": [bad]}) for bad in invalid_records)
        payloads.extend([
            json.dumps({"version": 2, "records": []}),
            json.dumps({"version": True, "records": []}),
            json.dumps({"version": 1, "records": {}}),
            json.dumps({"version": 1, "records": [record, {**record, "queue_id": "other"}]}),
            json.dumps({"version": 1, "records": [record, {**record, "expires_at": 3000}]}),
        ])
        for payload in payloads:
            with self.subTest(payload=payload):
                self.write_bad(payload)
                original = self.path.read_bytes()
                with self.assertRaises(ValueError):
                    self.recovery()
                self.assertEqual(self.path.read_bytes(), original)

    def test_unreadable_storage_raises(self) -> None:
        self.path.mkdir(parents=True)
        with self.assertRaises(OSError):
            self.recovery()

    def test_failed_replace_keeps_memory_disk_and_current_unchanged(self) -> None:
        registry = self.recovery()
        first = registry.bind("queue-1")
        original = self.path.read_bytes()
        with patch("app.auto_flow.recovery_service.os.replace", side_effect=OSError("write failed")):
            with self.assertRaises(OSError):
                registry.bind("queue-2", first["browser_token"])
            with self.assertRaises(OSError):
                registry.bind("queue-1")
        self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual(registry.current(first["browser_token"]), first)
        self.assertEqual(registry.get(first["token"]), first)
        self.assertEqual(self.recovery().current(first["browser_token"]), first)
        second = registry.bind("queue-2", first["browser_token"])
        self.assertEqual(json.loads(self.path.read_text(encoding="utf-8"))["records"], [first, second])

    def test_first_write_failure_does_not_retain_uncommitted_token(self) -> None:
        registry = self.recovery()
        for failure_point in ("os.fsync", "os.replace", "tempfile.NamedTemporaryFile"):
            with self.subTest(failure_point=failure_point):
                with patch("app.auto_flow.recovery_service." + failure_point, side_effect=OSError("failed")):
                    with patch(
                        "app.auto_flow.recovery_service.secrets.token_urlsafe",
                        side_effect=["a" * 32, "b" * 32],
                    ):
                        with self.assertRaises(OSError):
                            registry.bind("queue-1")
                self.assertFalse(self.path.exists())
                self.assertIsNone(registry.get("a" * 32))
                self.assertIsNone(registry.current("b" * 32))
        record = registry.bind("queue-1")
        self.assertEqual(json.loads(self.path.read_text(encoding="utf-8"))["records"], [record])

    def test_invalid_configuration_and_queue_id_fail_without_writes(self) -> None:
        for ttl in [0, -1, True, "10", None, float("nan"), float("inf")]:
            with self.subTest(ttl=ttl), self.assertRaises(ValueError):
                self.recovery(ttl)
        with self.assertRaises(TypeError):
            RecoveryRegistry(self.path, clock=None)
        registry = self.recovery()
        for queue_id in [None, "", " ", {}, 123]:
            with self.subTest(queue_id=queue_id), self.assertRaises(ValueError):
                registry.bind(queue_id)
        for timestamp in [True, "1000", float("nan"), float("inf")]:
            self.now = timestamp
            with self.subTest(timestamp=timestamp), self.assertRaises(ValueError):
                registry.bind("queue-1")
        self.assertFalse(self.path.exists())


class QueueSessionPersistenceTest(RegistryTestCase):
    def test_volatile_issue_is_stable_memory_only_and_validates_queue_id(self) -> None:
        registry = QueueSessionRegistry(self.path)
        with patch("app.auto_flow.http_handlers._write_document", side_effect=AssertionError("不得写入")):
            token = registry.issue_volatile("queue-1")
            self.assertEqual(registry.issue_volatile(" queue-1 "), token)
            self.assertEqual(registry.issue("queue-1"), token)
            for queue_id in [None, "", " ", {}, 123]:
                with self.subTest(queue_id=queue_id), self.assertRaises(ValueError):
                    registry.issue_volatile(queue_id)
        self.assertRegex(token, r"^[A-Za-z0-9_-]{32}$")
        self.assertTrue(registry.check(token, "queue-1"))
        self.assertFalse(self.path.exists())
        self.assertIsNone(QueueSessionRegistry(self.path).get_existing("queue-1"))

    def test_volatile_issue_preserves_existing_durable_bindings(self) -> None:
        registry = QueueSessionRegistry(self.path)
        durable = registry.issue("queue-1")
        original = self.path.read_bytes()
        with patch("app.auto_flow.http_handlers._write_document", side_effect=OSError("failed")):
            with self.assertRaises(OSError):
                registry.issue("queue-2")
            volatile = registry.issue_volatile("queue-2")
            self.assertEqual(registry.issue_volatile("queue-1"), durable)
        self.assertNotEqual(volatile, durable)
        self.assertTrue(registry.check(volatile, "queue-2"))
        self.assertTrue(registry.check(durable, "queue-1"))
        self.assertEqual(self.path.read_bytes(), original)
        restarted = QueueSessionRegistry(self.path)
        self.assertEqual(restarted.get_existing("queue-1"), durable)
        self.assertIsNone(restarted.get_existing("queue-2"))

    def test_concurrent_durable_failure_and_volatile_fallback_share_one_token(self) -> None:
        registry = QueueSessionRegistry(self.path)

        def issue_with_fallback(index: int) -> str:
            try:
                return registry.issue("shared")
            except OSError:
                return registry.issue_volatile("shared")

        with patch("app.auto_flow.http_handlers._write_document", side_effect=OSError("failed")):
            with ThreadPoolExecutor(max_workers=8) as executor:
                tokens = list(executor.map(issue_with_fallback, range(32)))
        self.assertEqual(len(set(tokens)), 1)
        self.assertEqual(registry.get_existing("shared"), tokens[0])
        self.assertTrue(registry.check(tokens[0], "shared"))
        self.assertFalse(self.path.exists())

    def test_get_existing_is_read_only_for_missing_and_persisted_sessions(self) -> None:
        registry = QueueSessionRegistry(self.path)
        for queue_id in ["queue-1", None, {}, "", " "]:
            self.assertIsNone(registry.get_existing(queue_id))
        self.assertFalse(self.path.exists())
        token = registry.issue("queue-1")
        original = self.path.read_bytes()
        restarted = QueueSessionRegistry(self.path)
        with patch("app.auto_flow.http_handlers.secrets.token_urlsafe", side_effect=AssertionError("不得签发")):
            with patch("app.auto_flow.recovery_service.os.replace", side_effect=AssertionError("不得写入")):
                self.assertEqual(restarted.get_existing(" queue-1 "), token)
                self.assertIsNone(restarted.get_existing("missing"))
        self.assertEqual(self.path.read_bytes(), original)
        self.assertTrue(restarted.forget("queue-1"))
        self.assertIsNone(restarted.get_existing("queue-1"))
        self.assertIsNone(QueueSessionRegistry().get_existing("queue-1"))

    def test_default_registry_remains_memory_only(self) -> None:
        registry = QueueSessionRegistry()
        token = registry.issue("queue-1")
        self.assertEqual(registry.issue(" queue-1 "), token)
        self.assertTrue(registry.check(token, "queue-1"))
        self.assertFalse(QueueSessionRegistry().check(token, "queue-1"))
        self.assertFalse(self.path.exists())
        self.assertTrue(registry.forget("queue-1"))
        self.assertFalse(registry.forget("queue-1"))

    def test_restart_preserves_token_and_forget_is_durable(self) -> None:
        registry = QueueSessionRegistry(self.path)
        self.assertFalse(self.path.exists())
        token = registry.issue("queue-1")
        other = registry.issue("queue-2")
        self.assertEqual(json.loads(self.path.read_text(encoding="utf-8")), {token: "queue-1", other: "queue-2"})
        restarted = QueueSessionRegistry(self.path)
        self.assertTrue(restarted.check(token, "queue-1"))
        self.assertEqual(restarted.issue("queue-1"), token)
        self.assertFalse(restarted.check(token, "queue-2"))
        self.assertTrue(restarted.forget("queue-1"))
        final = QueueSessionRegistry(self.path)
        self.assertFalse(final.check(token, "queue-1"))
        self.assertTrue(final.check(other, "queue-2"))
        self.assertNotEqual(final.issue("queue-1"), token)

    def test_corrupt_session_storage_raises_without_overwriting(self) -> None:
        token = "a" * 32
        payloads = [
            "{", "null", "[]", '{"bad":"queue-1"}',
            json.dumps({token: ""}), json.dumps({token: " queue-1"}),
            json.dumps({token: 1}), json.dumps({token: "queue-1", "b" * 32: "queue-1"}),
            '{"' + token + '":"queue-1","' + token + '":"queue-2"}',
        ]
        for payload in payloads:
            with self.subTest(payload=payload):
                self.write_bad(payload)
                original = self.path.read_bytes()
                with self.assertRaises(ValueError):
                    QueueSessionRegistry(self.path)
                self.assertEqual(self.path.read_bytes(), original)

    def test_session_unreadable_storage_raises(self) -> None:
        self.path.mkdir(parents=True)
        with self.assertRaises(OSError):
            QueueSessionRegistry(self.path)

    def test_failed_issue_and_forget_preserve_memory_and_disk(self) -> None:
        registry = QueueSessionRegistry(self.path)
        token = registry.issue("queue-1")
        original = self.path.read_bytes()
        with patch("app.auto_flow.recovery_service.os.replace", side_effect=OSError("failed")):
            with self.assertRaises(OSError):
                registry.issue("queue-2")
            with self.assertRaises(OSError):
                registry.forget("queue-1")
        self.assertEqual(self.path.read_bytes(), original)
        self.assertTrue(registry.check(token, "queue-1"))
        self.assertTrue(QueueSessionRegistry(self.path).check(token, "queue-1"))
        new_token = registry.issue("queue-2")
        self.assertEqual(json.loads(self.path.read_text(encoding="utf-8")), {token: "queue-1", new_token: "queue-2"})

    def test_first_session_write_failure_does_not_issue_valid_token(self) -> None:
        registry = QueueSessionRegistry(self.path)
        with patch("app.auto_flow.recovery_service.os.replace", side_effect=OSError("failed")):
            with patch("app.auto_flow.http_handlers.secrets.token_urlsafe", return_value="a" * 32):
                with self.assertRaises(OSError):
                    registry.issue("queue-1")
        self.assertFalse(registry.check("a" * 32, "queue-1"))
        self.assertFalse(registry.forget("queue-1"))
        self.assertFalse(self.path.exists())

    def test_concurrent_sessions_are_stable_and_durable(self) -> None:
        registry = QueueSessionRegistry(self.path)
        with ThreadPoolExecutor(max_workers=8) as executor:
            shared = list(executor.map(lambda index: registry.issue("shared"), range(16)))
            distinct = list(executor.map(lambda index: registry.issue(f"queue-{index}"), range(24)))
        self.assertEqual(len(set(shared)), 1)
        self.assertEqual(len(set(distinct)), 24)
        restarted = QueueSessionRegistry(self.path)
        self.assertTrue(restarted.check(shared[0], "shared"))
        for index, token in enumerate(distinct):
            self.assertTrue(restarted.check(token, f"queue-{index}"))

    def test_recovery_and_control_sessions_are_not_interchangeable(self) -> None:
        registry = self.recovery()
        recovery = registry.bind("queue-1")
        sessions = QueueSessionRegistry(self.path.with_name("sessions.json"))
        session = sessions.issue("queue-1")
        self.assertNotEqual(session, recovery["token"])
        self.assertNotEqual(session, recovery["browser_token"])
        self.assertFalse(sessions.check(recovery["token"], "queue-1"))
        self.assertFalse(sessions.check(recovery["browser_token"], "queue-1"))
        self.assertIsNone(registry.get(session))
        self.assertIsNone(registry.current(session))

    def test_services_default_and_optional_recovery_field(self) -> None:
        sessions = QueueSessionRegistry()
        self.assertIsNone(AutoFlowServices(queue=None, consent=None, sessions=sessions).recovery)
        recovery = self.recovery()
        services = AutoFlowServices(queue=None, consent=None, sessions=sessions, recovery=recovery)
        self.assertIs(services.recovery, recovery)


if __name__ == "__main__":
    unittest.main()
