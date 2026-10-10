import asyncio
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import Mock, patch

from fastapi import HTTPException, Request


BACKEND_DIR = Path(__file__).resolve().parents[1]

if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from api import posts
from database import db
from integrations.tiktok import TikTokUploadError
from main import app
from services.tiktok_reconciliation import (
    TikTokStatusTracker,
    create_status_tracker_from_environment,
    map_provider_status,
    reconcile_tiktok_post,
)


PUBLISH_ID = "v_pub_file~v2.reconciliation-test"


@contextmanager
def temporary_database(path: Path):
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    try:
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def provider_status(status: str, reason: str | None = None):
    return {
        "provider_status": status,
        "uploaded_bytes": 8_734_468,
        "downloaded_bytes": None,
        "failure_reason": reason,
        "manual_completion_required": status == "SEND_TO_USER_INBOX",
        "publication_confirmed": status == "PUBLISH_COMPLETE",
    }


class FakePublisher:
    def __init__(self, result=None, error=None):
        self.result = result
        self.error = error
        self.calls = []

    def fetch_upload_status(self, publish_id):
        self.calls.append(publish_id)
        if self.error:
            raise self.error
        return dict(self.result)


class TikTokReconciliationDatabaseTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.database = Path(self.directory.name) / "reconciliation.db"
        self.connection_patch = patch.object(
            db,
            "get_connection",
            lambda: temporary_database(self.database),
        )
        self.connection_patch.start()
        db.init_db()

    def tearDown(self):
        self.connection_patch.stop()
        self.directory.cleanup()

    def create_result(self, status="processing", mode="direct_post"):
        post_id = db.create_post("sample.mp4", "Title", "Caption")
        result_id = db.create_publish_result(
            post_id,
            "tiktok",
            status,
            remote_post_id=PUBLISH_ID,
            error="Previous local failure" if status == "failed" else None,
            mode=mode,
        )
        return post_id, result_id

    def reconcile(self, post_id, status, reason=None):
        publisher = FakePublisher(provider_status(status, reason))
        result = reconcile_tiktok_post(
            post_id,
            publisher_factory=lambda: publisher,
        )
        self.assertEqual(publisher.calls, [PUBLISH_ID])
        return result

    def test_direct_processing_to_published_records_first_confirmation_time(self):
        post_id, _ = self.create_result()

        first = self.reconcile(post_id, "PUBLISH_COMPLETE")
        stored = db.get_publish_result(post_id, "tiktok")

        self.assertTrue(first["database_updated"])
        self.assertEqual(first["local_status"], "published")
        self.assertTrue(first["publication_confirmed"])
        self.assertIsNotNone(stored["published_at"])
        confirmed_at = stored["published_at"]

        second = self.reconcile(post_id, "PUBLISH_COMPLETE")
        stored_again = db.get_publish_result(post_id, "tiktok")
        self.assertFalse(second["database_updated"])
        self.assertEqual(stored_again["published_at"], confirmed_at)

    def test_processing_transitions_to_failed_with_sanitized_reason(self):
        post_id, _ = self.create_result()
        result = self.reconcile(
            post_id,
            "FAILED",
            "file_format_check_failed",
        )
        stored = db.get_publish_result(post_id, "tiktok")

        self.assertEqual(result["local_status"], "failed")
        self.assertEqual(
            stored["error"],
            "TikTok reported: file_format_check_failed",
        )
        self.assertIsNone(stored["published_at"])

    def test_processing_to_processing_is_idempotent(self):
        post_id, _ = self.create_result()
        result = self.reconcile(post_id, "PROCESSING_UPLOAD")
        self.assertFalse(result["database_updated"])
        self.assertEqual(result["reconciliation_reason"], "unchanged")

    def test_manual_failed_false_negative_corrects_only_to_manual_completion(self):
        post_id, _ = self.create_result("failed", "manual_upload")
        result = self.reconcile(post_id, "SEND_TO_USER_INBOX")
        stored = db.get_publish_result(post_id, "tiktok")

        self.assertEqual(result["local_status"], "manual_completion")
        self.assertTrue(result["manual_completion_required"])
        self.assertFalse(result["publication_confirmed"])
        self.assertIsNone(stored["error"])
        self.assertIsNone(stored["published_at"])

    def test_manual_publish_complete_never_becomes_local_published(self):
        post_id, _ = self.create_result("manual_completion", "manual_upload")
        result = self.reconcile(post_id, "PUBLISH_COMPLETE")
        stored = db.get_publish_result(post_id, "tiktok")

        self.assertEqual(result["local_status"], "manual_completion")
        self.assertFalse(result["publication_confirmed"])
        self.assertFalse(result["manual_completion_required"])
        self.assertTrue(result["provider_reports_user_posted"])
        self.assertIsNone(stored["published_at"])

    def test_stale_response_cannot_overwrite_terminal_published_state(self):
        post_id, result_id = self.create_result()
        self.reconcile(post_id, "PUBLISH_COMPLETE")

        stale = db.reconcile_tiktok_publish_result(
            post_id=post_id,
            result_id=result_id,
            publish_id=PUBLISH_ID,
            expected_status="processing",
            new_status="failed",
            failure_reason="internal",
        )
        stored = db.get_publish_result(post_id, "tiktok")

        self.assertFalse(stale["updated"])
        self.assertEqual(stale["reason"], "stale_status")
        self.assertEqual(stored["status"], "published")
        self.assertIsNotNone(stored["published_at"])

    def test_unknown_or_mode_incompatible_status_does_not_update(self):
        self.assertIsNone(map_provider_status("direct_post", "NEW_STATUS"))
        post_id, _ = self.create_result()
        result = self.reconcile(post_id, "SEND_TO_USER_INBOX")
        self.assertFalse(result["database_updated"])
        self.assertEqual(result["local_status"], "processing")

    def test_missing_publish_identifier_stops_before_provider_call(self):
        post_id = db.create_post("sample.mp4", "Title")
        db.create_publish_result(post_id, "tiktok", "processing")
        publisher = FakePublisher(provider_status("PROCESSING_UPLOAD"))

        with self.assertRaisesRegex(RuntimeError, "No TikTok upload"):
            reconcile_tiktok_post(
                post_id,
                publisher_factory=lambda: publisher,
            )
        self.assertEqual(publisher.calls, [])

    def test_provider_auth_rate_limit_and_transport_failures_do_not_mutate(self):
        cases = [
            ("scope_not_authorized", "permission"),
            ("rate_limit_exceeded", "rate"),
            ("status_lookup_interrupted", "transport"),
        ]
        for code, label in cases:
            with self.subTest(case=label):
                post_id, _ = self.create_result()
                publisher = FakePublisher(
                    error=TikTokUploadError("Safe error", error_code=code)
                )
                with self.assertRaises(TikTokUploadError):
                    reconcile_tiktok_post(
                        post_id,
                        publisher_factory=lambda: publisher,
                    )
                self.assertEqual(
                    db.get_publish_result(post_id, "tiktok")["status"],
                    "processing",
                )

    def test_transaction_rolls_back_on_database_failure(self):
        post_id, _ = self.create_result()
        with db.get_connection() as conn:
            conn.execute(
                """
                CREATE TRIGGER reject_tiktok_reconcile
                BEFORE UPDATE ON publish_results
                BEGIN
                    SELECT RAISE(ABORT, 'test rollback');
                END;
                """
            )

        with self.assertRaises(sqlite3.IntegrityError):
            self.reconcile(post_id, "PUBLISH_COMPLETE")

        stored = db.get_publish_result(post_id, "tiktok")
        self.assertEqual(stored["status"], "processing")
        self.assertIsNone(stored["published_at"])

    def test_only_exact_tiktok_result_is_updated(self):
        post_id, _ = self.create_result()
        db.create_publish_result(
            post_id,
            "youtube",
            "published",
            remote_post_id="youtube-safe-id",
            remote_url="https://youtube.example/video",
        )
        before = [
            row for row in db.get_publish_history()
            if row["platform"] == "youtube"
        ][0]

        self.reconcile(post_id, "PUBLISH_COMPLETE")
        after = [
            row for row in db.get_publish_history()
            if row["platform"] == "youtube"
        ][0]
        self.assertEqual(before, after)

    def test_only_recent_processing_rows_are_automatically_selected(self):
        failed_post, _ = self.create_result("failed", "manual_upload")
        manual_post, _ = self.create_result(
            "manual_completion",
            "manual_upload",
        )
        pending_post, _ = self.create_result("processing", "direct_post")
        expired_post, _ = self.create_result("processing", "direct_post")
        with db.get_connection() as conn:
            conn.execute(
                "UPDATE posts SET created_at = ? WHERE id = ?",
                ("2000-01-01T00:00:00+00:00", expired_post),
            )

        rows = db.get_pending_tiktok_publish_results(10)

        self.assertEqual([row["post_id"] for row in rows], [pending_post])
        self.assertNotIn(failed_post, [row["post_id"] for row in rows])
        self.assertNotIn(manual_post, [row["post_id"] for row in rows])
        self.assertNotIn(expired_post, [row["post_id"] for row in rows])


def make_request(
    client="127.0.0.1",
    host="127.0.0.1:8000",
    origin=None,
):
    headers = [(b"host", host.encode("ascii"))]
    if origin is not None:
        headers.append((b"origin", origin.encode("ascii")))
    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/posts/21/tiktok/reconcile",
            "headers": headers,
            "client": (client, 12345),
            "server": ("127.0.0.1", 8000),
            "scheme": "http",
        }
    )


class TikTokReconciliationRouteTests(unittest.TestCase):
    def test_loopback_request_calls_only_reconciliation_service(self):
        safe = {
            "local_post_id": 21,
            "local_status": "published",
            "provider_status": "PUBLISH_COMPLETE",
        }
        with patch.object(posts, "reconcile_tiktok_post", return_value=safe) as call:
            result = posts.reconcile_tiktok_status(21, make_request())
        self.assertEqual(result, safe)
        call.assert_called_once_with(21)

    def test_remote_host_and_cross_origin_requests_are_rejected(self):
        requests = [
            make_request(client="192.0.2.10"),
            make_request(host="smh.example"),
            make_request(origin="https://evil.example"),
        ]
        for request in requests:
            with self.subTest(scope=request.scope):
                with self.assertRaises(HTTPException) as ctx:
                    posts.reconcile_tiktok_status(21, request)
                self.assertEqual(ctx.exception.status_code, 403)

    def test_openapi_keeps_read_only_route_and_adds_explicit_reconcile(self):
        schema = app.openapi()
        read_only = schema["paths"]["/posts/{post_id}/tiktok/status"]
        reconcile = schema["paths"]["/posts/{post_id}/tiktok/reconcile"]
        self.assertEqual(set(read_only), {"get"})
        self.assertEqual(set(reconcile), {"post"})


class TikTokStatusTrackerTests(unittest.IsolatedAsyncioTestCase):
    async def test_worker_is_disabled_by_default(self):
        with patch.dict(os.environ, {}, clear=True):
            tracker = create_status_tracker_from_environment()
        self.assertFalse(tracker.enabled)
        self.assertEqual(tracker.interval_seconds, 300)
        self.assertEqual(tracker.max_tracking_age_hours, 24)
        self.assertFalse(await tracker.start())
        self.assertIsNone(tracker._task)

    async def test_bounded_batch_and_error_backoff(self):
        requested = []
        calls = []

        def loader(limit, max_age_hours):
            requested.append((limit, max_age_hours))
            return [
                {"id": 1, "post_id": 20},
                {"id": 2, "post_id": 21},
            ]

        def reconcile(post_id):
            calls.append(post_id)
            if post_id == 21:
                raise TikTokUploadError(
                    "Rate limited.",
                    error_code="rate_limit_exceeded",
                )
            return {"database_updated": True}

        tracker = TikTokStatusTracker(
            enabled=True,
            interval_seconds=1,
            batch_size=100,
            max_backoff_seconds=8,
            pending_loader=loader,
            reconciler=reconcile,
            lock_path=Path(self.id().replace(".", "_")),
        )
        result = await tracker.run_cycle()

        self.assertEqual(requested, [(25, 24)])
        self.assertEqual(calls, [20, 21])
        self.assertEqual(result, {
            "eligible": 2,
            "checked": 2,
            "updated": 1,
            "errors": 1,
        })
        self.assertEqual(tracker.next_delay_seconds, 2)

    async def test_claim_prevents_concurrent_check_of_same_result(self):
        calls = []
        tracker = TikTokStatusTracker(
            enabled=True,
            pending_loader=lambda limit, max_age: [
                {"id": 7, "post_id": 21}
            ],
            reconciler=lambda post_id: calls.append(post_id) or {
                "database_updated": False
            },
            lock_path=Path(self.id().replace(".", "_")),
        )
        tracker._claimed_result_ids.add(7)
        result = await tracker.run_cycle()
        self.assertEqual(result["checked"], 0)
        self.assertEqual(calls, [])

    async def test_start_is_singleton_and_shutdown_is_clean(self):
        with tempfile.TemporaryDirectory() as directory:
            lock_path = Path(directory) / "tracker.lock"
            first = TikTokStatusTracker(
                enabled=True,
                interval_seconds=0.01,
                pending_loader=lambda limit, max_age: [],
                lock_path=lock_path,
            )
            second = TikTokStatusTracker(
                enabled=True,
                interval_seconds=0.01,
                pending_loader=lambda limit, max_age: [],
                lock_path=lock_path,
            )

            self.assertTrue(await first.start())
            self.assertFalse(await first.start())
            self.assertFalse(await second.start())
            await asyncio.sleep(0.02)
            await first.stop()
            self.assertIsNone(first._task)
            self.assertTrue(await second.start())
            await second.stop()

    async def test_tracker_result_contains_no_sensitive_provider_data(self):
        tracker = TikTokStatusTracker(
            enabled=True,
            pending_loader=lambda limit, max_age: [
                {"id": 1, "post_id": 21}
            ],
            reconciler=lambda post_id: {
                "database_updated": False,
                "provider_status": "PROCESSING_UPLOAD",
            },
            lock_path=Path(self.id().replace(".", "_")),
        )
        result = await tracker.run_cycle()
        rendered = json.dumps(result)
        self.assertNotIn("access_token", rendered)
        self.assertNotIn(PUBLISH_ID, rendered)


if __name__ == "__main__":
    unittest.main()
