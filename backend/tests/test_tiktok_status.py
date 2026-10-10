import json
import sqlite3
import sys
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import Mock, patch

import requests
from fastapi import HTTPException


BACKEND_DIR = Path(__file__).resolve().parents[1]

if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from api import posts
from database import db
from integrations.tiktok import (
    TIKTOK_UPLOAD_STATUS_URL,
    TikTokPublisher,
    TikTokUploadError,
)
from main import app


PUBLISH_ID = "v_inbox_file~v2.existing-upload"


class FakeResponse:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self.payload = payload
        self.headers = {}

    def json(self):
        if self.payload is None:
            raise ValueError("No JSON body")
        return self.payload


class StatusHttp:
    def __init__(self, responses):
        self.responses = list(responses)
        self.post_calls = []
        self.put_calls = []

    def post(self, url, **kwargs):
        self.post_calls.append((url, kwargs))
        if not self.responses:
            raise AssertionError("Unexpected POST request")
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    def put(self, url, **kwargs):
        self.put_calls.append((url, kwargs))
        raise AssertionError("Status lookup must never transfer video bytes")


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


def status_response(status, *, uploaded_bytes=None, fail_reason=None):
    data = {"status": status}
    if uploaded_bytes is not None:
        data["uploaded_bytes"] = uploaded_bytes
    if fail_reason is not None:
        data["fail_reason"] = fail_reason
    return FakeResponse(
        200,
        {
            "data": data,
            "error": {"code": "ok", "message": "", "log_id": "safe"},
        },
    )


class TikTokStatusLookupTests(unittest.TestCase):
    def make_publisher(self, response, oauth=None):
        http = StatusHttp([response])
        oauth = oauth or FakeOAuthService()
        publisher = TikTokPublisher(
            oauth_service_factory=lambda: oauth,
            http_client=http,
        )
        return publisher, http, oauth

    def test_supported_provider_statuses_are_sanitized(self):
        cases = [
            (
                "PROCESSING_UPLOAD",
                4_000_000,
                None,
                False,
                False,
            ),
            (
                "PROCESSING_DOWNLOAD",
                None,
                None,
                False,
                False,
            ),
            (
                "SEND_TO_USER_INBOX",
                8_734_468,
                None,
                True,
                False,
            ),
            (
                "PUBLISH_COMPLETE",
                8_734_468,
                None,
                False,
                True,
            ),
            (
                "FAILED",
                8_734_468,
                "file_format_check_failed",
                False,
                False,
            ),
        ]

        for status, count, reason, manual, published in cases:
            with self.subTest(status=status):
                publisher, http, oauth = self.make_publisher(
                    status_response(
                        status,
                        uploaded_bytes=count,
                        fail_reason=reason,
                    )
                )
                result = publisher.fetch_upload_status(PUBLISH_ID)

                self.assertEqual(result["provider_status"], status)
                self.assertEqual(result["uploaded_bytes"], count)
                self.assertEqual(result["failure_reason"], reason)
                self.assertEqual(result["manual_completion_required"], manual)
                self.assertEqual(result["publication_confirmed"], published)
                self.assertEqual(oauth.calls, 1)
                self.assertEqual(len(http.post_calls), 1)
                self.assertEqual(http.post_calls[0][0], TIKTOK_UPLOAD_STATUS_URL)
                self.assertEqual(
                    http.post_calls[0][1]["json"],
                    {"publish_id": PUBLISH_ID},
                )
                self.assertEqual(http.put_calls, [])
                rendered = json.dumps(result)
                self.assertNotIn("access-token", rendered)
                self.assertNotIn(PUBLISH_ID, rendered)

    def test_invalid_local_publish_id_is_blocked_without_remote_calls(self):
        http = StatusHttp([])
        oauth = FakeOAuthService()
        publisher = TikTokPublisher(
            oauth_service_factory=lambda: oauth,
            http_client=http,
        )

        with self.assertRaisesRegex(TikTokUploadError, "identifier is invalid"):
            publisher.fetch_upload_status("not a safe/id")

        self.assertEqual(oauth.calls, 0)
        self.assertEqual(http.post_calls, [])
        self.assertEqual(http.put_calls, [])

    def test_provider_invalid_publish_id_is_safely_reported(self):
        publisher, http, _ = self.make_publisher(
            FakeResponse(
                400,
                {
                    "error": {
                        "code": "invalid_publish_id",
                        "message": "provider details must not escape",
                    }
                },
            )
        )

        with self.assertRaisesRegex(TikTokUploadError, "could not find") as ctx:
            publisher.fetch_upload_status(PUBLISH_ID)

        self.assertEqual(ctx.exception.error_code, "invalid_publish_id")
        self.assertEqual(http.put_calls, [])

    def test_valid_credentials_result_is_used_after_safe_refresh(self):
        oauth = FakeOAuthService(
            credentials={
                "access_token": "refreshed-sensitive-token",
                "scopes": ["video.upload"],
            }
        )
        publisher, http, _ = self.make_publisher(
            status_response("PROCESSING_UPLOAD", uploaded_bytes=10),
            oauth=oauth,
        )

        result = publisher.fetch_upload_status(PUBLISH_ID)

        self.assertEqual(oauth.calls, 1)
        self.assertEqual(
            http.post_calls[0][1]["headers"]["Authorization"],
            "Bearer refreshed-sensitive-token",
        )
        self.assertNotIn("refreshed-sensitive-token", json.dumps(result))

    def test_missing_permission_stops_before_http(self):
        oauth = FakeOAuthService(
            credentials={
                "access_token": "test-access-token",
                "scopes": ["user.info.basic"],
            }
        )
        http = StatusHttp([])
        publisher = TikTokPublisher(
            oauth_service_factory=lambda: oauth,
            http_client=http,
        )

        with self.assertRaisesRegex(TikTokUploadError, "cannot read") as ctx:
            publisher.fetch_upload_status(PUBLISH_ID)

        self.assertEqual(ctx.exception.error_code, "scope_not_authorized")
        self.assertEqual(http.post_calls, [])

    def test_rate_limit_and_transport_errors_are_sanitized(self):
        cases = [
            (
                FakeResponse(
                    429,
                    {
                        "error": {
                            "code": "rate_limit_exceeded",
                            "message": "signed=https://secret.example/token",
                        }
                    },
                ),
                "rate limited",
                "rate_limit_exceeded",
            ),
            (
                requests.ConnectionError(
                    "Bearer sensitive-token https://signed.example/upload"
                ),
                "interrupted",
                "status_lookup_interrupted",
            ),
        ]

        for response, message, code in cases:
            with self.subTest(code=code):
                publisher, http, _ = self.make_publisher(response)
                with self.assertRaisesRegex(TikTokUploadError, message) as ctx:
                    publisher.fetch_upload_status(PUBLISH_ID)
                rendered = str(ctx.exception)
                self.assertEqual(ctx.exception.error_code, code)
                self.assertNotIn("sensitive-token", rendered)
                self.assertNotIn("signed.example", rendered)
                self.assertEqual(http.put_calls, [])

    def test_unsafe_failure_reason_and_raw_fields_are_not_returned(self):
        response = FakeResponse(
            200,
            {
                "data": {
                    "status": "FAILED",
                    "uploaded_bytes": 100,
                    "fail_reason": "secret https://signed.example/upload",
                    "publicaly_available_post_id": [123456789],
                    "upload_url": "https://signed.example/upload?token=secret",
                },
                "error": {"code": "ok", "message": "raw secret"},
            },
        )
        publisher, http, _ = self.make_publisher(response)

        result = publisher.fetch_upload_status(PUBLISH_ID)

        self.assertIsNone(result["failure_reason"])
        rendered = json.dumps(result)
        self.assertNotIn("signed.example", rendered)
        self.assertNotIn("123456789", rendered)
        self.assertEqual(http.put_calls, [])


class TikTokStatusRouteTests(unittest.TestCase):
    def test_route_uses_stored_identifier_and_returns_no_identifier(self):
        safe_status = {
            "provider_status": "SEND_TO_USER_INBOX",
            "uploaded_bytes": 8_734_468,
            "failure_reason": None,
            "manual_completion_required": True,
            "publication_confirmed": False,
        }
        publisher = Mock()
        publisher.fetch_upload_status.return_value = safe_status

        with patch.object(
            posts,
            "get_publish_result",
            return_value={"remote_post_id": PUBLISH_ID},
        ), patch.object(posts, "TikTokPublisher", return_value=publisher):
            response = posts.tiktok_upload_status(20)

        publisher.fetch_upload_status.assert_called_once_with(PUBLISH_ID)
        self.assertEqual(response, {"local_post_id": 20, **safe_status})
        self.assertNotIn(PUBLISH_ID, json.dumps(response))

    def test_missing_local_post_or_result_returns_404_without_tiktok(self):
        cases = [None, {"remote_post_id": None}]

        for result in cases:
            with self.subTest(result=result), patch.object(
                posts,
                "get_publish_result",
                return_value=result,
            ), patch.object(posts, "TikTokPublisher") as publisher_class:
                with self.assertRaises(HTTPException) as ctx:
                    posts.tiktok_upload_status(999)

                self.assertEqual(ctx.exception.status_code, 404)
                publisher_class.assert_not_called()

    def test_safe_provider_errors_map_to_http_without_raw_data(self):
        publisher = Mock()
        publisher.fetch_upload_status.side_effect = TikTokUploadError(
            "TikTok status lookup was rate limited. Try again later.",
            error_code="rate_limit_exceeded",
        )

        with patch.object(
            posts,
            "get_publish_result",
            return_value={"remote_post_id": PUBLISH_ID},
        ), patch.object(posts, "TikTokPublisher", return_value=publisher):
            with self.assertRaises(HTTPException) as ctx:
                posts.tiktok_upload_status(20)

        self.assertEqual(ctx.exception.status_code, 429)
        self.assertNotIn(PUBLISH_ID, str(ctx.exception.detail))

    def test_openapi_exposes_read_only_status_route(self):
        operation = app.openapi()["paths"]["/posts/{post_id}/tiktok/status"]
        self.assertIn("get", operation)
        self.assertNotIn("post", operation)
        self.assertNotIn("put", operation)

    def test_local_result_lookup_does_not_change_history(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "status.db"

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

            with patch.object(db, "get_connection", temporary_connection):
                db.init_db()
                post_id = db.create_post("sample.mp4", "Title", "Caption")
                db.create_publish_result(
                    post_id,
                    "tiktok",
                    "failed",
                    remote_post_id=PUBLISH_ID,
                    error="Safe local failure",
                    mode="manual_upload",
                )
                before = db.get_publish_history()

                result = db.get_publish_result(post_id, "tiktok")

                after = db.get_publish_history()

        self.assertEqual(result["remote_post_id"], PUBLISH_ID)
        self.assertEqual(before, after)
        self.assertEqual(before[0]["status"], "failed")
        self.assertIsNone(before[0]["published_at"])


if __name__ == "__main__":
    unittest.main()
