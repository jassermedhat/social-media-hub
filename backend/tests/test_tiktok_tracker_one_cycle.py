import json
import os
import requests
import sqlite3
import sys
import tempfile
import time
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch


BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from integrations.tiktok import (
    TIKTOK_DIRECT_POST_INITIALIZE_URL,
    TIKTOK_UPLOAD_STATUS_URL,
)
from verify_tiktok_tracker_once import (
    OneCycleVerificationError,
    OneStatusRequestHttpClient,
    run_one_cycle_verification,
    verify_distinct_paths,
)


PUBLISH_ID = "v_pub_file~v2.one-cycle-existing"
ORIGINAL_PUBLISHED_AT = "2026-10-10T10:00:00+00:00"
SAFE_ENVIRONMENT = {
    "TIKTOK_ENVIRONMENT": "sandbox",
    "TIKTOK_MANUAL_UPLOAD_ENABLED": "false",
    "TIKTOK_DIRECT_POST_ENABLED": "false",
    "TIKTOK_PUBLIC_POSTING_ENABLED": "false",
    "TIKTOK_STATUS_TRACKING_ENABLED": "false",
}


class FakeResponse:
    def __init__(self, payload, status_code=200):
        self.payload = payload
        self.status_code = status_code
        self.headers = {}

    def json(self):
        return self.payload


class FakeTransport:
    def __init__(self, responses):
        self.responses = list(responses)
        self.post_calls = []

    def post(self, url, **kwargs):
        self.post_calls.append((url, kwargs))
        if not self.responses:
            raise AssertionError("Unexpected network request")
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def status_response(status):
    return FakeResponse(
        {
            "data": {
                "status": status,
                "uploaded_bytes": 8_734_468,
            },
            "error": {"code": "ok", "message": "", "log_id": "safe"},
        }
    )


def credentials():
    now = time.time()
    return {
        "access_token": "mock-sensitive-access-token",
        "refresh_token": "mock-sensitive-refresh-token",
        "open_id": "mock-open-id",
        "scopes": ["user.info.basic", "video.publish", "video.upload"],
        "access_expires_at": now + 3600,
        "refresh_expires_at": now + 86400,
    }


def create_production_database(path: Path):
    with closing(sqlite3.connect(path)) as connection:
        connection.executescript(
            """
            CREATE TABLE connected_accounts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                platform TEXT NOT NULL,
                account_id TEXT NOT NULL,
                account_name TEXT NOT NULL,
                token_file TEXT,
                connected_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(platform, account_id)
            );
            CREATE TABLE posts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                video_path TEXT NOT NULL,
                title TEXT NOT NULL,
                description TEXT DEFAULT '',
                created_at TEXT NOT NULL
            );
            CREATE TABLE publish_results (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                post_id INTEGER NOT NULL,
                platform TEXT NOT NULL,
                status TEXT NOT NULL,
                remote_post_id TEXT,
                remote_url TEXT,
                error TEXT,
                published_at TEXT,
                mode TEXT,
                FOREIGN KEY(post_id) REFERENCES posts(id) ON DELETE CASCADE
            );
            """
        )
        connection.execute(
            """
            INSERT INTO connected_accounts (
                platform, account_id, account_name, connected_at, updated_at
            ) VALUES ('tiktok', 'mock-open-id', 'safe-name', ?, ?)
            """,
            (ORIGINAL_PUBLISHED_AT, ORIGINAL_PUBLISHED_AT),
        )
        connection.execute(
            """
            INSERT INTO posts (
                id, video_path, title, description, created_at
            ) VALUES (21, 'safe.mp4', 'Title', 'Caption', ?)
            """,
            (ORIGINAL_PUBLISHED_AT,),
        )
        connection.execute(
            """
            INSERT INTO publish_results (
                post_id, platform, status, remote_post_id,
                published_at, mode
            ) VALUES (21, 'tiktok', 'published', ?, ?, 'direct_post')
            """,
            (PUBLISH_ID, ORIGINAL_PUBLISHED_AT),
        )
        connection.commit()


def production_snapshot(path: Path):
    with closing(
        sqlite3.connect(
            f"file:{path.resolve().as_posix()}?mode=ro",
            uri=True,
        )
    ) as connection:
        return connection.execute(
            """
            SELECT status, remote_post_id, published_at, mode
            FROM publish_results
            WHERE post_id = 21 AND platform = 'tiktok'
            """
        ).fetchone()


class TikTokOneCycleHarnessTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.production_database = self.root / "production.db"
        self.production_lock = self.root / "production-tracker.lock"
        create_production_database(self.production_database)
        self.before = production_snapshot(self.production_database)

    def tearDown(self):
        self.directory.cleanup()

    def run_harness(self, transport):
        with patch.dict(os.environ, SAFE_ENVIRONMENT, clear=False):
            return run_one_cycle_verification(
                production_database=self.production_database,
                credentials=credentials(),
                transport=transport,
                production_lock_path=self.production_lock,
                temporary_parent=self.root,
            )

    def assert_production_unchanged(self):
        self.assertEqual(
            production_snapshot(self.production_database),
            self.before,
        )

    def assert_temporary_files_cleaned(self):
        leftovers = [
            path.name
            for path in self.root.iterdir()
            if path.name.startswith("smh-tiktok-one-cycle-")
            or path.name.endswith(".lock")
        ]
        self.assertEqual(leftovers, [])

    def test_isolated_copy_reconciles_once_and_preserves_production(self):
        transport = FakeTransport([status_response("PUBLISH_COMPLETE")])

        result = self.run_harness(transport)

        self.assertEqual(result, {
            "verified": True,
            "local_post_id": 21,
            "provider_status": "PUBLISH_COMPLETE",
            "temporary_status": "published",
            "status_requests": 1,
            "production_unchanged": True,
        })
        self.assertEqual(len(transport.post_calls), 1)
        self.assertEqual(transport.post_calls[0][0], TIKTOK_UPLOAD_STATUS_URL)
        rendered = json.dumps(result)
        self.assertNotIn(PUBLISH_ID, rendered)
        self.assertNotIn("mock-sensitive", rendered)
        self.assert_production_unchanged()
        self.assert_temporary_files_cleaned()

    def test_production_path_cannot_be_used_as_temporary_path(self):
        with self.assertRaises(OneCycleVerificationError):
            verify_distinct_paths(
                self.production_database,
                self.production_database,
            )
        self.assert_production_unchanged()

    def test_http_guard_allows_at_most_one_status_request(self):
        transport = FakeTransport([status_response("PUBLISH_COMPLETE")])
        client = OneStatusRequestHttpClient(transport)
        client.post(TIKTOK_UPLOAD_STATUS_URL, json={})
        with self.assertRaises(OneCycleVerificationError):
            client.post(TIKTOK_UPLOAD_STATUS_URL, json={})
        self.assertEqual(len(transport.post_calls), 1)

    def test_http_guard_rejects_upload_and_transfer_endpoints(self):
        transport = FakeTransport([])
        client = OneStatusRequestHttpClient(transport)

        with self.assertRaises(OneCycleVerificationError):
            client.post(TIKTOK_DIRECT_POST_INITIALIZE_URL, json={})
        with self.assertRaises(OneCycleVerificationError):
            client.put("https://upload.example/signed", data=b"video")
        self.assertEqual(transport.post_calls, [])

    def test_unexpected_provider_status_fails_and_cleans_up(self):
        transport = FakeTransport([status_response("UNEXPECTED_STATUS")])

        with self.assertRaises(OneCycleVerificationError):
            self.run_harness(transport)

        self.assertEqual(len(transport.post_calls), 1)
        self.assert_production_unchanged()
        self.assert_temporary_files_cleaned()

    def test_network_failure_fails_and_cleans_up(self):
        transport = FakeTransport(
            [requests.ConnectionError("sensitive provider detail")]
        )

        with self.assertRaises(OneCycleVerificationError):
            self.run_harness(transport)

        self.assertEqual(len(transport.post_calls), 1)
        self.assert_production_unchanged()
        self.assert_temporary_files_cleaned()

    def test_enabled_safety_flag_aborts_before_network_or_copy(self):
        transport = FakeTransport([])
        unsafe = dict(SAFE_ENVIRONMENT)
        unsafe["TIKTOK_DIRECT_POST_ENABLED"] = "true"

        with patch.dict(os.environ, unsafe, clear=False):
            with self.assertRaises(OneCycleVerificationError):
                run_one_cycle_verification(
                    production_database=self.production_database,
                    credentials=credentials(),
                    transport=transport,
                    production_lock_path=self.production_lock,
                    temporary_parent=self.root,
                )

        self.assertEqual(transport.post_calls, [])
        self.assert_production_unchanged()
        self.assert_temporary_files_cleaned()

    def test_expiring_credentials_abort_without_refresh_or_network(self):
        transport = FakeTransport([])
        expiring = credentials()
        expiring["access_expires_at"] = time.time() + 30

        with patch.dict(os.environ, SAFE_ENVIRONMENT, clear=False):
            with self.assertRaises(OneCycleVerificationError):
                run_one_cycle_verification(
                    production_database=self.production_database,
                    credentials=expiring,
                    transport=transport,
                    production_lock_path=self.production_lock,
                    temporary_parent=self.root,
                )

        self.assertEqual(transport.post_calls, [])
        self.assert_production_unchanged()
        self.assert_temporary_files_cleaned()


if __name__ == "__main__":
    unittest.main()
