# -*- coding: utf-8 -*-
"""T-7 令牌取图与删除测试。

运行（项目根目录）::

    python3 -m unittest app.tests.test_auto_flow_photo -v

覆盖任务验收三条：
a) 100000 个测试令牌长度 = 22、URL-safe 字符合法、无重复（集合断言）；
b) 编号 SM-YYYY-NNNN（含大小写、补零到 22 字符等变体）不能通过 validate，
   也不能访问取图记录或删除入口；
c) 确认删除后图片 / 缩略图 / QR 均不可读，重复删除不恢复，晚到 commit 不复活
   （含真并发：删除与提交分线程竞争，以及删除进行中的 409 DELETE_IN_PROGRESS）。
另覆盖部署门控：生产语义（无批准）阻止物理删除，测试语义放行。

约定：只写系统临时目录里的合成小图（``FakeCamera`` 帧），不碰项目数据目录；
不联网；全部标准库 + Pillow。
"""

from __future__ import annotations

import inspect
import sys
import tempfile
import threading
import unittest
from pathlib import Path

from PIL import Image

try:  # K3 控制台是 UTF-8；Windows 控制台是 GBK —— 中文测试名不能把测试挡在门外
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # pragma: no cover
    pass

from app.auto_flow import errors
from app.auto_flow.config import load_config
from app.auto_flow.errors import ApiError
from app.auto_flow.models import PhotoRecord, is_valid_photo_token
from app.auto_flow.photo_service import (
    ENV_DELETE_APPROVED,
    DeleteNotApprovedError,
    FilePhotoStore,
    PhotoRevokedError,
    PhotoService,
    delete_approved_from_env,
)
from app.auto_flow.ports import PhotoStorePort
from app.auto_flow.token_service import TOKEN_LENGTH, TokenService, looks_like_display_number
from app.tests.auto_flow_fixtures import FakeCamera, FakeClock, fixture_env


class TokenServiceTests(unittest.TestCase):
    """a) 令牌签发与校验：100000 个不重复、合法；编号格式一律拒绝。"""

    def test_issue_100k_tokens_valid_urlsafe_and_unique(self):
        service = TokenService()
        tokens = [service.issue() for _ in range(100_000)]
        # 集合断言：无一重复
        self.assertEqual(len(set(tokens)), len(tokens))
        self.assertEqual(len(tokens), 100_000)
        # 长度恒为 22
        self.assertEqual({len(token) for token in tokens}, {TOKEN_LENGTH})
        # 字符集合法（与共享格式工具 models.is_valid_photo_token 一致）
        self.assertTrue(all(is_valid_photo_token(token) for token in tokens))
        # 没有任何一个撞上编号格式
        self.assertFalse(any(looks_like_display_number(token) for token in tokens))

    def test_issue_takes_no_input_and_is_not_deterministic(self):
        # issue() 不接受任何输入：结构上不存在「从编号 / 作业 ID / 个人信息推导」的入口
        self.assertEqual(list(inspect.signature(TokenService.issue).parameters), ["self"])
        first, second = TokenService(), TokenService()
        self.assertNotEqual(first.issue(), first.issue())
        self.assertNotEqual(first.issue(), second.issue())

    def test_validate_accepts_every_issued_token(self):
        service = TokenService()
        for _ in range(200):
            token = service.issue()
            self.assertTrue(service.validate(token), token)
            self.assertEqual(len(token), TOKEN_LENGTH)

    def test_validate_rejects_malformed_tokens(self):
        service = TokenService()
        bad_values = [
            None,
            12345,
            b"x" * 22,
            ["x"] * 22,
            "",
            "short",
            "x" * 21,
            "x" * 23,
            "x" * 21 + "!",
            "x" * 21 + " ",
            "x" * 20 + "\n",
            "中文" * 11,
        ]
        for value in bad_values:
            self.assertFalse(service.validate(value), repr(value))

    def test_validate_rejects_display_number_variants(self):
        service = TokenService()
        # 标准编号本身（12 字符）、大小写变体、非补零变体、补零到 22 字符的变体
        variants = [
            "SM-2026-0001",
            "sm-2026-0001",
            "Sm-2026-0001",
            "SM-2026-1",
            "SM-2026-00000000000001",
            "  SM-2026-0001  ",
        ]
        for value in variants:
            self.assertFalse(service.validate(value), value)
            self.assertTrue(looks_like_display_number(value), value)
        # 补零变体确实达到令牌长度，仍然无效
        self.assertEqual(len("SM-2026-00000000000001"), TOKEN_LENGTH)
        # 反向：合法随机令牌不会被误判为编号
        self.assertFalse(looks_like_display_number(service.issue()))


class PhotoCase(unittest.TestCase):
    """公共夹具：系统临时根目录 + 合成源图 + 测试语义 PhotoService。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="auto-flow-photo-")
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.clock = FakeClock()
        self.camera = FakeCamera(self.root, self.clock)
        self.store = FilePhotoStore(self.root, clock=self.clock)
        self.service = PhotoService(self.store, testing=True)  # 测试语义：放行删除
        self.frame = self.camera.capture_authorized("consent-test")  # 合成 JPEG 源图

    def commit(self, job_id: str = "job-0001", variant: str = "L0", **kwargs) -> PhotoRecord:
        return self.store.commit(job_id, self.frame.source_path, variant, **kwargs)

    def assert_files_present(self, token: str) -> dict[str, Path]:
        paths = self.store.paths_for(token)
        self.assertIsNotNone(paths, "照片文件应当齐备")
        assert paths is not None
        for kind, path in paths.items():
            self.assertTrue(path.is_file(), f"{kind} 不存在：{path}")
        return dict(paths)

    def assert_files_gone(self, paths) -> None:
        for kind, path in paths.items():
            self.assertFalse(path.exists(), f"{kind} 应已删除：{path}")


class FilePhotoStoreTests(PhotoCase):
    """存储实现：落盘、端口契约、重启重放。"""

    def test_commit_persists_record_and_derivatives(self):
        record = self.commit(display_number="SM-2026-0007")
        self.assertIsInstance(record, PhotoRecord)
        self.assertEqual(record.job_id, "job-0001")
        self.assertEqual(record.variant, "L0")
        self.assertEqual(record.status, "available")
        self.assertEqual(record.created_at, self.clock.iso())
        self.assertEqual(record.display_number, "SM-2026-0007")
        self.assertTrue(is_valid_photo_token(record.photo_token))

        paths = self.assert_files_present(record.photo_token)
        self.assertEqual(paths["photo"].read_bytes(), Path(self.frame.source_path).read_bytes())
        with Image.open(paths["thumb"]) as thumb:
            self.assertEqual(thumb.format, "JPEG")
            self.assertLessEqual(thumb.width, 480)
            self.assertLessEqual(thumb.height, 600)
        self.assertTrue(paths["qr"].read_bytes().startswith(b"\x89PNG\r\n\x1a\n"))
        # 全部落盘都在参数化根目录内；没有 .part 残留
        for path in paths.values():
            self.assertTrue(str(path).startswith(str(self.root)), path)
        self.assertEqual(list(self.root.glob("**/*.part")), [])

    def test_commit_rejects_bad_inputs_without_half_records(self):
        with self.assertRaises(ValueError):
            self.store.commit("job-0002", self.frame.source_path, "L1")
        with self.assertRaises(FileNotFoundError):
            self.store.commit("job-0003", str(self.root / "missing.jpg"), "L0")
        self.assertEqual(self.store.records(), [])
        self.assertEqual(list(self.store.photos_dir.iterdir()), [])

    def test_store_satisfies_photo_store_port(self):
        self.assertIsInstance(self.store, PhotoStorePort)

    def test_journal_replay_survives_restart_and_revocation_sticks(self):
        record = self.commit(job_id="job-restart")
        restarted = FilePhotoStore(self.root, clock=self.clock)
        self.assertIsNotNone(restarted.find(record.photo_token))
        self.assertEqual(restarted.find(record.photo_token).variant, "L0")

        self.assertTrue(PhotoService(restarted, testing=True).delete(record.photo_token))
        again = FilePhotoStore(self.root, clock=self.clock)
        self.assertIsNone(again.find(record.photo_token))
        self.assertEqual(again.state_of(record.photo_token), "deleted")
        # 重启后晚到提交依旧被拒：撤销是持久化的，不靠内存里碰运气
        with self.assertRaises(PhotoRevokedError) as ctx:
            again.commit("job-restart", self.frame.source_path, "L0")
        self.assertEqual(ctx.exception.code, errors.PHOTO_DELETED)
        self.assertEqual(ctx.exception.http_status, 410)


class NumberNeverAuthorizesTests(PhotoCase):
    """b) SM-YYYY-NNNN 编号不能访问取图、图片路径或删除入口。"""

    def test_display_number_cannot_reach_photo_or_delete(self):
        number = "SM-2026-0001"
        record = self.commit(display_number=number)
        self.assertEqual(record.display_number, number)

        # 读取：None；存储层同样不认；状态是 unknown 而不是 deleted
        self.assertIsNone(self.service.get(number))
        self.assertIsNone(self.store.find(number))
        self.assertEqual(self.store.state_of(number), "unknown")

        # 取图路由语义：404 INVALID_TOKEN（不是 404 PHOTO_NOT_FOUND，更不是放行）
        with self.assertRaises(ApiError) as require_ctx:
            self.service.require(number)
        self.assertEqual(require_ctx.exception.code, errors.INVALID_TOKEN)
        self.assertEqual(require_ctx.exception.http_status, 404)

        # 删除入口：同样 INVALID_TOKEN，照片不受影响
        with self.assertRaises(ApiError) as delete_ctx:
            self.service.delete(number)
        self.assertEqual(delete_ctx.exception.code, errors.INVALID_TOKEN)
        self.assertIsNotNone(self.service.get(record.photo_token))

        # 磁盘上不存在以编号命名的照片文件（图片本身也不可按编号寻址）
        self.assertEqual(list(self.store.photos_dir.glob("SM-*")), [])

    def test_number_padded_to_token_length_is_still_invalid(self):
        padded = "SM-2026-00000000000001"
        self.assertEqual(len(padded), TOKEN_LENGTH)
        self.assertFalse(TokenService().validate(padded))
        self.assertIsNone(self.service.get(padded))
        with self.assertRaises(ApiError) as ctx:
            self.service.require(padded)
        self.assertEqual(ctx.exception.code, errors.INVALID_TOKEN)


class DeleteSemanticsTests(PhotoCase):
    """c) 删除原子失效、幂等、晚到不复活（含真并发）。"""

    def test_delete_invalidates_photo_thumb_qr_and_derivative_paths(self):
        record = self.commit()
        paths = self.assert_files_present(record.photo_token)
        self.assertTrue(self.service.delete(record.photo_token))

        self.assertIsNone(self.service.get(record.photo_token))
        self.assert_files_gone(paths)
        # 路由按令牌取路径的那一层同样拿不到入口
        self.assertIsNone(self.service.photo_path(record.photo_token))
        self.assertIsNone(self.service.thumb_path(record.photo_token))
        self.assertIsNone(self.service.qr_path(record.photo_token))
        with self.assertRaises(ApiError) as ctx:
            self.service.require(record.photo_token)
        self.assertEqual(ctx.exception.code, errors.PHOTO_DELETED)
        self.assertEqual(ctx.exception.http_status, 410)
        self.assertTrue(self.service.is_deleted(record.photo_token))

    def test_repeat_delete_is_idempotent_and_does_not_resurrect(self):
        record = self.commit()
        paths = self.assert_files_present(record.photo_token)
        self.assertTrue(self.service.delete(record.photo_token))
        self.assertFalse(self.service.delete(record.photo_token))
        self.assertFalse(self.service.delete(record.photo_token))
        self.assert_files_gone(paths)
        self.assertIsNone(self.service.get(record.photo_token))
        # 不重建任何文件
        for folder in (self.store.photos_dir, self.store.thumbs_dir, self.store.qr_dir):
            self.assertEqual(list(folder.glob(f"{record.photo_token}*")), [])

    def test_late_commit_for_deleted_job_is_rejected(self):
        record = self.commit(job_id="job-late")
        self.assertTrue(self.service.delete(record.photo_token))
        with self.assertRaises(PhotoRevokedError) as ctx:
            self.store.commit("job-late", self.frame.source_path, "styled")
        self.assertEqual(ctx.exception.code, errors.PHOTO_DELETED)
        self.assertEqual(ctx.exception.http_status, 410)
        self.assertEqual([r for r in self.store.records() if r.status == "available"], [])

    def test_delete_unknown_token_is_photo_not_found(self):
        with self.assertRaises(ApiError) as ctx:
            self.service.delete("a" * TOKEN_LENGTH)
        self.assertEqual(ctx.exception.code, errors.PHOTO_NOT_FOUND)
        self.assertEqual(ctx.exception.http_status, 404)

    def test_delete_invalid_token_is_invalid_token(self):
        for bad in ("SM-2026-0001", "short", "", "x" * 21 + "!"):
            with self.assertRaises(ApiError) as ctx:
                self.service.delete(bad)
            self.assertEqual(ctx.exception.code, errors.INVALID_TOKEN, bad)

    def test_concurrent_second_delete_reports_delete_in_progress(self):
        record = self.commit()
        entered = threading.Event()
        release = threading.Event()

        def slow_remove(_path: Path) -> None:
            entered.set()
            self.assertTrue(release.wait(5.0))

        self.store.remove_hook = slow_remove
        outcome: dict[str, object] = {}

        def do_delete() -> None:
            try:
                outcome["first"] = self.service.delete(record.photo_token)
            except BaseException as exc:  # pragma: no cover - 失败时把异常带回主线程断言
                outcome["first_error"] = exc

        thread = threading.Thread(target=do_delete)
        thread.start()
        try:
            self.assertTrue(entered.wait(5.0), "删除线程没有进入文件清理阶段")
            with self.assertRaises(ApiError) as ctx:
                self.service.delete(record.photo_token)
            self.assertEqual(ctx.exception.code, errors.DELETE_IN_PROGRESS)
            self.assertEqual(ctx.exception.http_status, 409)
        finally:
            release.set()
        thread.join(5.0)
        self.assertNotIn("first_error", outcome, outcome.get("first_error"))
        self.assertIs(outcome.get("first"), True)
        # 完成之后是幂等 False，而不是继续 409
        self.assertFalse(self.service.delete(record.photo_token))
        self.assertIsNone(self.service.get(record.photo_token))

    def test_concurrent_delete_and_late_commit_never_resurrects(self):
        """真并发：删除与同一作业的晚到提交分线程竞争，任何交错都不允许复活。"""
        for index in range(10):
            job_id = f"job-race-{index}"
            record = self.commit(job_id=job_id)
            paths = self.assert_files_present(record.photo_token)
            barrier = threading.Barrier(3)
            outcome: dict[str, object] = {}

            def do_delete() -> None:
                barrier.wait(5.0)
                try:
                    outcome["deleted"] = self.service.delete(record.photo_token)
                except BaseException as exc:  # pragma: no cover
                    outcome["delete_error"] = exc

            def do_commit() -> None:
                barrier.wait(5.0)
                try:
                    outcome["commit"] = self.store.commit(job_id, self.frame.source_path, "styled")
                except BaseException as exc:
                    outcome["commit_error"] = exc

            threads = [threading.Thread(target=do_delete), threading.Thread(target=do_commit)]
            for thread in threads:
                thread.start()
            barrier.wait(5.0)
            for thread in threads:
                thread.join(5.0)

            self.assertNotIn("delete_error", outcome, outcome.get("delete_error"))
            self.assertIs(outcome.get("deleted"), True)
            self.assertIsNone(self.service.get(record.photo_token))
            self.assert_files_gone(paths)
            if "commit" in outcome:
                late = outcome["commit"]
                assert isinstance(late, PhotoRecord)
                # 提交先于删除完成：同一作业的删除会连带清掉新令牌 —— 一样不复活
                self.assertIsNone(self.service.get(late.photo_token))
                for folder in (self.store.photos_dir, self.store.thumbs_dir, self.store.qr_dir):
                    self.assertEqual(list(folder.glob(f"{late.photo_token}*")), [])
            else:
                late_error = outcome.get("commit_error")
                self.assertIsInstance(late_error, PhotoRevokedError)
                self.assertEqual(late_error.code, errors.PHOTO_DELETED)
            available = [
                item
                for item in self.store.records()
                if item.job_id == job_id and self.store.find(item.photo_token)
            ]
            self.assertEqual(available, [], f"第 {index} 轮出现复活记录")


class PrintCancelHookTests(PhotoCase):
    """未提交打印取消钩子：墓碑之后、清文件之前调用；钩子失败不阻断失效。"""

    def test_hook_called_after_tombstone_before_files_removed(self):
        record = self.commit()
        paths = self.assert_files_present(record.photo_token)
        calls: list[tuple[str, bool, bool]] = []
        holder: dict[str, PhotoService] = {}

        def hook(token: str) -> None:
            # 调用时刻：墓碑已生效（get 无图），但文件还没被挪走
            calls.append((token, holder["service"].get(token) is None, paths["photo"].is_file()))

        service = PhotoService(self.store, print_cancel=hook, testing=True)
        holder["service"] = service
        self.assertTrue(service.delete(record.photo_token))
        self.assertEqual(calls, [(record.photo_token, True, True)])
        self.assertEqual(service.print_cancel_errors, [])
        self.assert_files_gone(paths)

    def test_hook_failure_does_not_block_delete(self):
        record = self.commit()
        paths = self.assert_files_present(record.photo_token)

        def hook(_token: str) -> None:
            raise RuntimeError("printer offline")

        service = PhotoService(self.store, print_cancel=hook, testing=True)
        self.assertTrue(service.delete(record.photo_token))
        self.assertIsNone(service.get(record.photo_token))
        self.assert_files_gone(paths)
        self.assertEqual(len(service.print_cancel_errors), 1)
        self.assertIn("printer offline", service.print_cancel_errors[0])

    def test_delete_without_hook_is_fine(self):
        record = self.commit()
        service = PhotoService(self.store, testing=True)
        self.assertIsNone(service.print_cancel)
        self.assertTrue(service.delete(record.photo_token))


class DeleteApprovalGateTests(PhotoCase):
    """部署门控：生产语义（无批准）阻止物理删除；测试语义 / 显式批准放行。"""

    def test_production_without_approval_blocks_physical_delete(self):
        record = self.commit()
        paths = self.assert_files_present(record.photo_token)
        service = PhotoService(self.store)  # 生产默认：未批准、非测试
        self.assertFalse(service.physical_delete_allowed)
        with self.assertRaises(DeleteNotApprovedError) as ctx:
            service.delete(record.photo_token)
        self.assertEqual(ctx.exception.code, errors.INTERNAL_ERROR)
        self.assertIn("未获批准", ctx.exception.message)
        # 访问能力与文件原样保留：门控只拦物理删除，不改动其它语义
        self.assertIsNotNone(service.get(record.photo_token))
        self.assert_files_present(record.photo_token)

    def test_explicit_approval_allows_delete(self):
        record = self.commit()
        service = PhotoService(self.store, delete_approved=True)
        self.assertTrue(service.physical_delete_allowed)
        self.assertTrue(service.delete(record.photo_token))
        self.assertIsNone(service.get(record.photo_token))

    def test_testing_semantics_allows_delete(self):
        record = self.commit()
        service = PhotoService(self.store, testing=True)
        self.assertTrue(service.physical_delete_allowed)
        self.assertTrue(service.delete(record.photo_token))

    def test_env_flag_parsing_defaults_to_refused(self):
        self.assertFalse(delete_approved_from_env({}))
        self.assertFalse(delete_approved_from_env({ENV_DELETE_APPROVED: "0"}))
        self.assertFalse(delete_approved_from_env({ENV_DELETE_APPROVED: "maybe"}))
        self.assertFalse(delete_approved_from_env({ENV_DELETE_APPROVED: ""}))
        self.assertTrue(delete_approved_from_env({ENV_DELETE_APPROVED: "1"}))
        self.assertTrue(delete_approved_from_env({ENV_DELETE_APPROVED: " true "}))
        self.assertTrue(delete_approved_from_env({ENV_DELETE_APPROVED: "YES"}))

    def test_from_config_testing_allows_and_production_blocks(self):
        record = self.commit()
        testing_config = load_config(env=fixture_env(), allow_test_mode=True)
        allowed = PhotoService.from_config(testing_config, self.store)
        self.assertTrue(allowed.physical_delete_allowed)

        production_config = load_config(env={"BOOTH_PHOTO_RETENTION_DAYS": "30"})
        blocked = PhotoService.from_config(production_config, self.store)
        self.assertFalse(blocked.physical_delete_allowed)
        with self.assertRaises(DeleteNotApprovedError):
            blocked.delete(record.photo_token)
        # 测试上下文放行后删除照常执行
        self.assertTrue(allowed.delete(record.photo_token))
        self.assertIsNone(self.service.get(record.photo_token))


class SpecInterfaceTests(unittest.TestCase):
    """规格接口签名核查（T-16 接线依赖的确切形状）。"""

    def test_token_service_signatures(self):
        self.assertEqual(list(inspect.signature(TokenService.issue).parameters), ["self"])
        self.assertEqual(list(inspect.signature(TokenService.validate).parameters), ["self", "token"])

    def test_photo_service_signatures(self):
        self.assertEqual(list(inspect.signature(PhotoService.get).parameters), ["self", "token"])
        self.assertEqual(list(inspect.signature(PhotoService.delete).parameters), ["self", "token"])

    def test_store_commit_signature_matches_port(self):
        parameters = list(inspect.signature(FilePhotoStore.commit).parameters)
        self.assertEqual(parameters[:4], ["self", "job_id", "source_path", "variant"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main(verbosity=2)
