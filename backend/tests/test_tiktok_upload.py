import json
import os
import sqlite3
import sys
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import Mock, patch

import requests


BACKEND_DIR = Path(__file__).resolve().parents[1]

if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from api import history, posts
from database import db
from integrations.tiktok import (
    TIKTOK_DEFAULT_CHUNK_SIZE_BYTES,
    TikTokPublisher,
    TikTokUploadError,
    calculate_upload_plan,
    iter_chunk_ranges,
)
from services import publishing
from services.tiktok_oauth import TikTokReauthorizationRequired


UPLOAD_URL = (
    "https://open-upload.tiktokapis.com/video/"
    "?upload_id=test-id&upload_token=sensitive-upload-token"
)
PUBLISH_ID = "v_inbox_file~v2.test123"


class FakeResponse:
    def __init__(self, status_code=200, payload=None, headers=None):
        self.status_code = status_code
        self.payload = payload
        self.headers = dict(headers or {})

    def json(self):
        if self.payload is None:
            raise ValueError("No JSON body")
        return self.payload


class FakeHttp:
    def __init__(self, *, posts=None, puts=None):
        self.posts = list(posts or [])
        self.puts = list(puts or [])
        self.post_calls = []
        self.put_calls = []

    def post(self, url, **kwargs):
        self.post_calls.append((url, kwargs))
        if not self.posts:
            raise AssertionError("Unexpected POST request")
        response = self.posts.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    def put(self, url, **kwargs):
        self.put_calls.append(
            {
                "url": url,
                "headers": dict(kwargs.get("headers") or {}),
                "data_length": len(kwargs.get("data") or b""),
                "timeout": kwargs.get("timeout"),
                "allow_redirects": kwargs.get("allow_redirects"),
            }
        )
        if not self.puts:
            raise AssertionError("Unexpected PUT request")
        response = self.puts.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


class FakeOAuthService:
    def __init__(self, credentials=None, error=None):
        self.credentials = credentials or {
            "access_token": "test-access-token",
            "scopes": ["user.info.basic", "video.upload"],
        }
        self.error = error
        self.calls = 0

    def valid_credentials(self):
        self.calls += 1
        if self.error:
            raise self.error
        return dict(self.credentials)


def initialized_response(upload_url=UPLOAD_URL, publish_id=PUBLISH_ID):
    return FakeResponse(
        200,
        {
            "data": {
                "publish_id": publish_id,
                "upload_url": upload_url,
            },
            "error": {
                "code": "ok",
                "message": "",
                "log_id": "safe-log-id",
            },
        },
    )


def create_dummy_video(path: Path, size: int) -> None:
    with path.open("wb") as video_file:
        video_file.seek(size - 1)
        video_file.write(b"\0")


class TikTokManualUploadTests(unittest.TestCase):
    def make_publisher(
        self,
        *,
        http=None,
        oauth=None,
        enabled=True,
        retries=2,
        sleeper=None,
    ):
        oauth = oauth or FakeOAuthService()
        return TikTokPublisher(
            oauth_service_factory=lambda: oauth,
            http_client=http or FakeHttp(),
            manual_upload_enabled=enabled,
            max_chunk_retries=retries,
            sleeper=sleeper or (lambda _: None),
        )

    def upload(self, publisher, video):
        return publisher.publish(
            video_path=str(video),
            title="Local title",
            description="Caption retained only by SMH",
            publish=True,
            mode="manual_upload",
            user_consent=True,
        )

    def test_validation_mode_makes_no_remote_calls(self):
        http = FakeHttp()
        oauth_factory = Mock()

        with tempfile.TemporaryDirectory() as directory:
            video = Path(directory) / "sample.mp4"
            create_dummy_video(video, 10)
            publisher = TikTokPublisher(
                oauth_service_factory=oauth_factory,
                http_client=http,
                manual_upload_enabled=True,
            )
            result = publisher.publish(
                video_path=str(video),
                title="Test",
                publish=False,
                mode="manual_upload",
                user_consent=True,
            )

        self.assertEqual(result["status"], "ready")
        self.assertTrue(result["dry_run"])
        oauth_factory.assert_not_called()
        self.assertEqual(http.post_calls, [])
        self.assertEqual(http.put_calls, [])

    def test_disabled_live_flag_blocks_before_oauth_or_http(self):
        http = FakeHttp()
        oauth_factory = Mock()

        with tempfile.TemporaryDirectory() as directory:
            video = Path(directory) / "sample.mp4"
            create_dummy_video(video, 10)
            publisher = TikTokPublisher(
                oauth_service_factory=oauth_factory,
                http_client=http,
                manual_upload_enabled=None,
            )

            with (
                patch.dict(
                    os.environ,
                    {"TIKTOK_MANUAL_UPLOAD_ENABLED": "false"},
                ),
                self.assertRaisesRegex(TikTokUploadError, "disabled"),
            ):
                self.upload(publisher, video)

        oauth_factory.assert_not_called()
        self.assertEqual(http.post_calls, [])
        self.assertEqual(http.put_calls, [])

    def test_direct_post_is_disabled_without_remote_calls(self):
        http = FakeHttp()
        oauth_factory = Mock()

        with tempfile.TemporaryDirectory() as directory:
            video = Path(directory) / "sample.mp4"
            create_dummy_video(video, 10)
            publisher = TikTokPublisher(
                oauth_service_factory=oauth_factory,
                http_client=http,
                manual_upload_enabled=True,
                direct_post_enabled=False,
            )

            with self.assertRaisesRegex(TikTokUploadError, "disabled"):
                publisher.publish(
                    video_path=str(video),
                    title="Test",
                    publish=True,
                    mode="direct_post",
                    user_consent=True,
                )

        oauth_factory.assert_not_called()
        self.assertEqual(http.post_calls, [])
        self.assertEqual(http.put_calls, [])

    def test_explicit_consent_is_required_before_remote_calls(self):
        http = FakeHttp()
        oauth_factory = Mock()

        with tempfile.TemporaryDirectory() as directory:
            video = Path(directory) / "sample.mp4"
            create_dummy_video(video, 10)
            publisher = TikTokPublisher(
                oauth_service_factory=oauth_factory,
                http_client=http,
                manual_upload_enabled=True,
            )

            with self.assertRaisesRegex(TikTokUploadError, "consent"):
                publisher.publish(
                    video_path=str(video),
                    title="Test",
                    publish=True,
                    mode="manual_upload",
                    user_consent=False,
                )

        oauth_factory.assert_not_called()
        self.assertEqual(http.post_calls, [])

    def test_whole_file_upload_below_five_megabytes(self):
        video_size = 1024 * 1024
        http = FakeHttp(
            posts=[initialized_response()],
            puts=[
                FakeResponse(
                    201,
                    headers={
                        "Content-Range": f"bytes 0-{video_size}/{video_size}"
                    },
                )
            ],
        )

        with tempfile.TemporaryDirectory() as directory:
            video = Path(directory) / "sample.mp4"
            create_dummy_video(video, video_size)
            result = self.upload(self.make_publisher(http=http), video)

        source_info = http.post_calls[0][1]["json"]["source_info"]
        self.assertEqual(
            source_info,
            {
                "source": "FILE_UPLOAD",
                "video_size": video_size,
                "chunk_size": video_size,
                "total_chunk_count": 1,
            },
        )
        self.assertEqual(len(http.put_calls), 1)
        self.assertEqual(
            http.put_calls[0]["headers"]["Content-Range"],
            f"bytes 0-{video_size - 1}/{video_size}",
        )
        self.assertEqual(
            http.put_calls[0]["headers"]["Content-Length"],
            str(video_size),
        )
        self.assertEqual(http.put_calls[0]["data_length"], video_size)
        self.assertEqual(http.put_calls[0]["url"], UPLOAD_URL)
        self.assertFalse(http.put_calls[0]["allow_redirects"])
        self.assertEqual(result["status"], "manual_completion")
        self.assertFalse(result["published"])
        self.assertFalse(result["dry_run"])
        self.assertEqual(result["remote_publish_id"], PUBLISH_ID)
        self.assertIsNone(result["url"])
        self.assertFalse(result["caption_transferred"])
        self.assertNotIn("upload_url", json.dumps(result))
        self.assertNotIn("sensitive-upload-token", json.dumps(result))

    def test_chunked_upload_uses_exact_ranges_and_merged_remainder(self):
        video_size = (25 * 1024 * 1024) + 123
        first_end = TIKTOK_DEFAULT_CHUNK_SIZE_BYTES - 1
        final_end = video_size - 1
        http = FakeHttp(
            posts=[initialized_response()],
            puts=[
                FakeResponse(
                    206,
                    headers={
                        "Content-Range": (
                            f"bytes 0-{first_end + 1}/{video_size}"
                        )
                    },
                ),
                FakeResponse(
                    201,
                    headers={
                        "Content-Range": (
                            f"bytes 0-{final_end + 1}/{video_size}"
                        )
                    },
                ),
            ],
        )

        with tempfile.TemporaryDirectory() as directory:
            video = Path(directory) / "sample.webm"
            create_dummy_video(video, video_size)
            result = self.upload(self.make_publisher(http=http), video)

        source_info = http.post_calls[0][1]["json"]["source_info"]
        self.assertEqual(source_info["chunk_size"], 10 * 1024 * 1024)
        self.assertEqual(source_info["total_chunk_count"], 2)
        self.assertEqual(len(http.put_calls), 2)
        self.assertEqual(
            http.put_calls[0]["headers"]["Content-Range"],
            f"bytes 0-{first_end}/{video_size}",
        )
        self.assertEqual(
            http.put_calls[1]["headers"]["Content-Range"],
            f"bytes {first_end + 1}-{final_end}/{video_size}",
        )
        self.assertEqual(
            http.put_calls[1]["data_length"],
            video_size - TIKTOK_DEFAULT_CHUNK_SIZE_BYTES,
        )
        self.assertEqual(
            http.put_calls[0]["headers"]["Content-Type"],
            "video/webm",
        )
        self.assertEqual(result["status"], "manual_completion")

    def test_chunk_plan_matches_documented_floor_and_final_merge(self):
        video_size = 50_000_123
        chunk_size, count = calculate_upload_plan(
            video_size,
            chunk_size=10_000_000,
        )
        ranges = list(iter_chunk_ranges(video_size, chunk_size, count))

        self.assertEqual(chunk_size, 10_000_000)
        self.assertEqual(count, 5)
        self.assertEqual(ranges[0], (0, 9_999_999, 10_000_000))
        self.assertEqual(
            ranges[-1],
            (40_000_000, 50_000_122, 10_000_123),
        )

    def test_untrusted_upload_destination_is_rejected_with_session_id(self):
        cases = [
            initialized_response(upload_url=None),
            initialized_response(upload_url="http://example.com/upload?token=x"),
            initialized_response(
                upload_url="https://example.com/upload?token=x"
            ),
            initialized_response(upload_url="https://open-upload.tiktokapis.com/"),
        ]

        for response in cases:
            with self.subTest(response=response.payload):
                http = FakeHttp(posts=[response])
                with tempfile.TemporaryDirectory() as directory:
                    video = Path(directory) / "sample.mov"
                    create_dummy_video(video, 100)
                    with self.assertRaisesRegex(
                        TikTokUploadError,
                        "invalid upload",
                    ) as context:
                        self.upload(self.make_publisher(http=http), video)
                self.assertEqual(context.exception.publish_id, PUBLISH_ID)
                self.assertEqual(http.put_calls, [])

    def test_malformed_initialization_response_is_rejected(self):
        http = FakeHttp(
            posts=[initialized_response(publish_id="not a safe/id")]
        )

        with tempfile.TemporaryDirectory() as directory:
            video = Path(directory) / "sample.mp4"
            create_dummy_video(video, 100)
            with self.assertRaisesRegex(TikTokUploadError, "malformed"):
                self.upload(self.make_publisher(http=http), video)

        self.assertEqual(http.put_calls, [])

    def test_refreshed_access_token_is_used_and_scope_is_required(self):
        refreshed = FakeOAuthService(
            credentials={
                "access_token": "refreshed-access-token",
                "scopes": ["video.upload"],
            }
        )
        http = FakeHttp(
            posts=[initialized_response()],
            puts=[FakeResponse(201)],
        )

        with tempfile.TemporaryDirectory() as directory:
            video = Path(directory) / "sample.mp4"
            create_dummy_video(video, 100)
            self.upload(
                self.make_publisher(http=http, oauth=refreshed),
                video,
            )

        self.assertEqual(refreshed.calls, 1)
        self.assertEqual(
            http.post_calls[0][1]["headers"]["Authorization"],
            "Bearer refreshed-access-token",
        )

        missing_scope = FakeOAuthService(
            credentials={
                "access_token": "access-token",
                "scopes": ["user.info.basic"],
            }
        )
        blocked_http = FakeHttp()

        with tempfile.TemporaryDirectory() as directory:
            video = Path(directory) / "sample.mp4"
            create_dummy_video(video, 100)
            with self.assertRaisesRegex(TikTokUploadError, "video.upload"):
                self.upload(
                    self.make_publisher(
                        http=blocked_http,
                        oauth=missing_scope,
                    ),
                    video,
                )

        self.assertEqual(blocked_http.post_calls, [])

    def test_expired_or_failed_credentials_are_safely_reported(self):
        oauth = FakeOAuthService(
            error=TikTokReauthorizationRequired(
                "sensitive refresh failure",
                error_code="refresh_token_expired",
            )
        )
        http = FakeHttp()

        with tempfile.TemporaryDirectory() as directory:
            video = Path(directory) / "sample.mp4"
            create_dummy_video(video, 100)
            with self.assertRaises(TikTokUploadError) as context:
                self.upload(
                    self.make_publisher(http=http, oauth=oauth),
                    video,
                )

        self.assertIn("Reconnect TikTok", str(context.exception))
        self.assertNotIn("sensitive refresh failure", str(context.exception))
        self.assertEqual(http.post_calls, [])

    def test_initialization_429_and_5xx_are_not_retried(self):
        cases = [
            (
                FakeResponse(
                    429,
                    {"error": {"code": "rate_limit_exceeded"}},
                ),
                "rate limited",
            ),
            (
                FakeResponse(
                    503,
                    {"error": {"code": "internal_error"}},
                ),
                "duplicate session",
            ),
        ]

        for response, expected in cases:
            with self.subTest(status=response.status_code):
                http = FakeHttp(posts=[response])
                with tempfile.TemporaryDirectory() as directory:
                    video = Path(directory) / "sample.mp4"
                    create_dummy_video(video, 100)
                    with self.assertRaisesRegex(TikTokUploadError, expected):
                        self.upload(self.make_publisher(http=http), video)
                self.assertEqual(len(http.post_calls), 1)
                self.assertEqual(http.put_calls, [])

    def test_expired_session_and_invalid_range_stop_transfer(self):
        cases = [
            (403, "expired"),
            (416, "byte range"),
            (429, "rate limited"),
        ]

        for status_code, expected in cases:
            with self.subTest(status=status_code):
                http = FakeHttp(
                    posts=[initialized_response()],
                    puts=[FakeResponse(status_code)],
                )
                with tempfile.TemporaryDirectory() as directory:
                    video = Path(directory) / "sample.mp4"
                    create_dummy_video(video, 100)
                    with self.assertRaisesRegex(TikTokUploadError, expected) as ctx:
                        self.upload(self.make_publisher(http=http), video)
                self.assertEqual(ctx.exception.publish_id, PUBLISH_ID)
                self.assertEqual(len(http.put_calls), 1)

    def test_explicit_5xx_chunk_response_is_retried_with_a_bound(self):
        sleeps = []
        http = FakeHttp(
            posts=[initialized_response()],
            puts=[FakeResponse(500), FakeResponse(502), FakeResponse(201)],
        )

        with tempfile.TemporaryDirectory() as directory:
            video = Path(directory) / "sample.mp4"
            create_dummy_video(video, 100)
            result = self.upload(
                self.make_publisher(
                    http=http,
                    sleeper=sleeps.append,
                ),
                video,
            )

        self.assertEqual(result["status"], "manual_completion")
        self.assertEqual(len(http.put_calls), 3)
        self.assertEqual(sleeps, [1, 2])

        failed_http = FakeHttp(
            posts=[initialized_response()],
            puts=[FakeResponse(500), FakeResponse(502), FakeResponse(503)],
        )

        with tempfile.TemporaryDirectory() as directory:
            video = Path(directory) / "sample.mp4"
            create_dummy_video(video, 100)
            with self.assertRaisesRegex(TikTokUploadError, "bounded retries"):
                self.upload(
                    self.make_publisher(http=failed_http),
                    video,
                )

        self.assertEqual(len(failed_http.put_calls), 3)

    def test_interrupted_transfer_is_not_retried(self):
        http = FakeHttp(
            posts=[initialized_response()],
            puts=[requests.ConnectionError("sensitive upload URL details")],
        )

        with tempfile.TemporaryDirectory() as directory:
            video = Path(directory) / "sample.mp4"
            create_dummy_video(video, 100)
            with self.assertRaises(TikTokUploadError) as context:
                self.upload(self.make_publisher(http=http), video)

        self.assertIn("interrupted", str(context.exception))
        self.assertNotIn("sensitive upload URL", str(context.exception))
        self.assertEqual(len(http.put_calls), 1)

    def test_invalid_range_acknowledgement_is_rejected(self):
        invalid_headers = [
            "bytes 0-99/100",
            "bytes 0-101/100",
            "bytes 0-100/99",
            "bytes 1-100/100",
            "bytes 0-not-a-number/100",
            "items 0-100/100",
            "",
        ]

        for content_range in invalid_headers:
            with self.subTest(content_range=content_range):
                with self.assertRaisesRegex(
                    TikTokUploadError,
                    "invalid upload range",
                ):
                    TikTokPublisher._validate_acknowledged_range(
                        FakeResponse(
                            201,
                            headers={"Content-Range": content_range},
                        ),
                        expected_uploaded_bytes=100,
                        video_size=100,
                        publish_id=PUBLISH_ID,
                    )

    def test_acknowledgement_accepts_missing_and_harmless_whitespace(self):
        cases = [
            {},
            {"Content-Range": "bytes 0-100/100"},
            {"content-range": "  BYTES  0 - 100 / 100  "},
        ]

        for headers in cases:
            with self.subTest(headers=headers):
                TikTokPublisher._validate_acknowledged_range(
                    FakeResponse(201, headers=headers),
                    expected_uploaded_bytes=100,
                    video_size=100,
                    publish_id=PUBLISH_ID,
                )

    def test_live_file_size_uses_inclusive_request_and_byte_count_ack(self):
        video_size = 8_734_468
        http = FakeHttp(
            posts=[initialized_response()],
            puts=[
                FakeResponse(
                    201,
                    headers={
                        "Content-Range": f"bytes 0-{video_size}/{video_size}"
                    },
                )
            ],
        )

        with tempfile.TemporaryDirectory() as directory:
            video = Path(directory) / "sample.mp4"
            create_dummy_video(video, video_size)
            result = self.upload(self.make_publisher(http=http), video)

        self.assertEqual(len(http.put_calls), 1)
        self.assertEqual(
            http.put_calls[0]["headers"]["Content-Range"],
            "bytes 0-8734467/8734468",
        )
        self.assertEqual(result["status"], "manual_completion")

    def test_sensitive_initialization_body_and_upload_url_are_not_exposed(self):
        sensitive = "sensitive-upload-token"
        http = FakeHttp(
            posts=[
                FakeResponse(
                    400,
                    {
                        "data": {"upload_url": UPLOAD_URL},
                        "error": {
                            "code": "invalid_param",
                            "message": f"secret {sensitive}",
                        },
                    },
                )
            ]
        )

        with tempfile.TemporaryDirectory() as directory:
            video = Path(directory) / "sample.mp4"
            create_dummy_video(video, 100)
            with self.assertRaises(TikTokUploadError) as context:
                self.upload(self.make_publisher(http=http), video)

        rendered = str(context.exception)
        self.assertNotIn(sensitive, rendered)
        self.assertNotIn(UPLOAD_URL, rendered)


class TikTokManualUploadWorkflowTests(unittest.TestCase):
    def test_manual_completion_is_stored_without_publication_timestamp(self):
        http = FakeHttp(
            posts=[initialized_response()],
            puts=[FakeResponse(201)],
        )
        publisher = TikTokPublisher(
            oauth_service_factory=lambda: FakeOAuthService(),
            http_client=http,
            manual_upload_enabled=True,
            sleeper=lambda _: None,
        )

        with tempfile.TemporaryDirectory() as directory:
            temp_dir = Path(directory)
            video = temp_dir / "sample.mp4"
            create_dummy_video(video, 100)
            database = temp_dir / "test.db"

            @contextmanager
            def temporary_connection():
                connection = sqlite3.connect(database)
                connection.row_factory = sqlite3.Row
                connection.execute("PRAGMA foreign_keys = ON")
                try:
                    yield connection
                    connection.commit()
                finally:
                    connection.close()

            request = posts.PublishPostRequest(
                video_path=str(video),
                title="Manual upload",
                caption="Caption remains in SMH history",
                platforms=["tiktok"],
                tiktok_publish=True,
                tiktok_mode="manual_upload",
                tiktok_user_consent=True,
            )

            with (
                patch.object(db, "get_connection", temporary_connection),
                patch.object(
                    publishing,
                    "TikTokPublisher",
                    return_value=publisher,
                ),
                patch.object(posts, "GoogleDriveMediaStorage") as drive,
            ):
                db.init_db()
                response = posts.publish_post(request)
                stored = history.publishing_history()["items"]

        result = response["results"]["tiktok"]
        self.assertTrue(result["success"])
        self.assertFalse(result["published"])
        self.assertFalse(result["dry_run"])
        self.assertEqual(result["status"], "manual_completion")
        self.assertEqual(result["mode"], "manual_upload")
        self.assertEqual(result["remote_publish_id"], PUBLISH_ID)
        self.assertIsNone(result["url"])
        self.assertFalse(result["caption_transferred"])
        self.assertNotIn("upload_url", json.dumps(response))
        self.assertNotIn("sensitive-upload-token", json.dumps(response))
        self.assertEqual(stored[0]["status"], "manual_completion")
        self.assertEqual(stored[0]["mode"], "manual_upload")
        self.assertEqual(stored[0]["remote_post_id"], PUBLISH_ID)
        self.assertEqual(
            stored[0]["description"],
            "Caption remains in SMH history",
        )
        self.assertIsNone(stored[0]["published_at"])
        drive.assert_not_called()

    def test_schema_migration_is_idempotent_and_preserves_existing_rows(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "legacy.db"

            @contextmanager
            def temporary_connection():
                connection = sqlite3.connect(database)
                connection.row_factory = sqlite3.Row
                connection.execute("PRAGMA foreign_keys = ON")
                try:
                    yield connection
                    connection.commit()
                finally:
                    connection.close()

            connection = sqlite3.connect(database)
            try:
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
                        published_at TEXT
                    );
                    INSERT INTO posts (
                        id, video_path, title, description, created_at
                    ) VALUES (1, 'legacy.mp4', 'Legacy', '', '2026-01-01');
                    INSERT INTO publish_results (
                        post_id, platform, status
                    ) VALUES (1, 'youtube', 'published');
                    """
                )
                connection.commit()
            finally:
                connection.close()

            with patch.object(db, "get_connection", temporary_connection):
                db.init_db()
                db.init_db()
                stored = db.get_publish_history()

            self.assertEqual(len(stored), 1)
            self.assertEqual(stored[0]["platform"], "youtube")
            self.assertEqual(stored[0]["status"], "published")
            self.assertIsNone(stored[0]["mode"])

    def test_openapi_exposes_manual_upload_safety_fields(self):
        from main import app

        schema = app.openapi()
        request_schema = schema["components"]["schemas"]["PublishPostRequest"]
        properties = request_schema["properties"]

        self.assertEqual(properties["tiktok_publish"]["default"], False)
        self.assertEqual(properties["tiktok_mode"]["default"], "validation")
        self.assertEqual(
            properties["tiktok_user_consent"]["default"],
            False,
        )


if __name__ == "__main__":
    unittest.main()
