import os
import tempfile
import threading
import time
import unittest
from datetime import datetime, timezone
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import urlopen
from unittest.mock import patch

from app.store import Record, Store


class StoreRetentionTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.store = Store(self.temporary.name)
        self.now = time.time()

    def record(self, record_id, age_hours):
        record = Record(
            id=record_id, created_at=datetime.fromtimestamp(
                self.now - age_hours * 3600, timezone.utc).isoformat(),
            prompt="", ep=False, infer_ms=0, person_count=1, degraded=False,
            seed=1, card="", style="test", token=Store.new_token(),
            extra={"name": "private-name"},
        )
        self.store.append(record)
        return record

    def test_token_expires_at_24_hours_even_before_cleanup(self):
        record = self.record("SM-2026-0001", 0)
        with patch("app.store.time.time", return_value=self.now + 86400 - 1):
            self.assertEqual(self.store.by_token(record.token), record)
        with patch("app.store.time.time", return_value=self.now + 86400):
            self.assertIsNone(self.store.by_token(record.token))
        self.assertEqual(self.store.retention_hours, 24)

    def test_invalid_timestamp_is_not_publicly_accessible(self):
        record = self.record("SM-2026-0001", 0)
        record.created_at = "invalid"
        self.assertTrue(self.store.is_expired(record))

    def test_cleanup_removes_all_variants_and_personal_record(self):
        expired = self.record("SM-2026-0001", 25)
        current = self.record("SM-2026-0002", 1)
        variants = (
            self.store.photo_path(expired.id), self.store.clean_photo_path(expired.id),
            self.store.capture_path(expired.id), self.store.thumb_path(expired.id),
            self.store.root / "masks" / (expired.id + "-alpha.png"),
            self.store.root / "masks" / (expired.id + "-subject.png"),
            self.store.root / "masks" / (expired.id + "-cutout.png"),
            self.store.root / "previews" / (expired.id + "-aigc.jpg"),
            self.store.root / "prints" / (expired.id + "-clean.jpg"),
            self.store.root / "qr" / (expired.token + ".png"),
        )
        for asset in (*variants, self.store.photo_path(current.id)):
            asset.parent.mkdir(parents=True, exist_ok=True)
            asset.write_bytes(b"test")
        result = self.store.purge_expired()
        self.assertEqual(result["removed"], [expired.id])
        self.assertEqual(result["kept"], 1)
        self.assertTrue(all(not asset.exists() for asset in variants))
        self.assertIsNone(self.store.get(expired.id))
        self.assertTrue(self.store.photo_path(current.id).exists())
        self.assertEqual(self.store.by_token(current.token), current)
        self.assertNotIn(expired.token, self.store.records_path.read_text(encoding="utf-8"))

    def test_orphaned_images_are_cleaned_by_age(self):
        asset = self.store.capture_path("orphan")
        asset.write_bytes(b"test")
        os.utime(asset, (self.now - 90000, self.now - 90000))
        self.store.purge_expired()
        self.assertFalse(asset.exists())

    def test_attendee_delete_removes_intermediate_subject_mask(self):
        record = self.record("SM-2026-0001", 1)
        subject = self.store.root / "masks" / (record.id + "-subject.png")
        subject.parent.mkdir(parents=True, exist_ok=True)
        subject.write_bytes(b"test")
        self.assertTrue(self.store.delete(record.id))
        self.assertFalse(subject.exists())

    def test_current_record_does_not_expire_from_old_file_timestamp(self):
        record = self.record("SM-2026-0001", 1)
        asset = self.store.photo_path(record.id)
        asset.write_bytes(b"test")
        os.utime(asset, (self.now - 90000, self.now - 90000))
        self.store.purge_expired()
        self.assertTrue(asset.exists())
        self.assertEqual(self.store.by_token(record.token), record)

    def test_failed_cleanup_keeps_record_for_retry(self):
        record = self.record("SM-2026-0001", 25)
        photo = self.store.photo_path(record.id)
        photo.write_bytes(b"test")
        with patch.object(Path, "unlink", side_effect=PermissionError("busy")):
            self.store.purge_expired()
        self.assertEqual(self.store.get(record.id), record)
        self.assertIsNone(self.store.by_token(record.token))
        self.store.purge_expired()
        self.assertFalse(photo.exists())
        self.assertIsNone(self.store.get(record.id))

    def test_http_download_variants_metadata_and_qr_expire_together(self):
        from app.booth_server import Booth, Handler

        record = self.record("SM-2026-0001", 0)
        for asset in (self.store.photo_path(record.id), self.store.clean_photo_path(record.id),
                      self.store.thumb_path(record.id)):
            asset.write_bytes(b"test")
        booth = Booth.__new__(Booth)
        booth.store = self.store
        booth.print_port = None
        handler = type("RetentionHandler", (Handler,), {"booth": booth})
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        base = "http://127.0.0.1:" + str(server.server_address[1])
        routes = ("/api/photo/" + record.token + ".jpg",
                  "/api/photo/" + record.token + ".clean.jpg",
                  "/api/thumb/" + record.token + ".jpg",
                  "/api/photo-meta/" + record.token + ".json")
        try:
            for route in routes:
                with urlopen(base + route, timeout=5) as response:
                    self.assertEqual(response.status, 200)
                    self.assertEqual(response.headers["Cache-Control"], "no-store")
            with patch("app.store.time.time", return_value=self.now + 86401):
                for route in (*routes, "/api/qr/" + record.token + ".png"):
                    with self.assertRaises(HTTPError) as result:
                        urlopen(base + route, timeout=5)
                    self.assertEqual(result.exception.code, 404)
                    result.exception.close()
            self.assertTrue(self.store.photo_path(record.id).exists())
        finally:
            server.shutdown()
            server.server_close()
            worker.join(timeout=5)


if __name__ == "__main__":
    unittest.main()
