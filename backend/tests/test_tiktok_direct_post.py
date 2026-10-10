import json
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

from api import accounts, history, posts
from database import db
from integrations.tiktok import (
    TIKTOK_CREATOR_INFO_URL,
    TIKTOK_DEFAULT_CHUNK_SIZE_BYTES,
    TIKTOK_DIRECT_POST_INITIALIZE_URL,
    TikTokPublisher,
    TikTokUploadError,
)
from services import publishing
from services.tiktok_oauth import TikTokReauthorizationRequired


UPLOAD_URL = (
    "https://open-upload.tiktokapis.com/video/"
    "?upload_id=direct-test&upload_token=sensitive-direct-token"
)
PUBLISH_ID = "v_pub_file~v2.private-test"


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
            "access_token": "test-direct-access-token",
            "scopes": ["user.info.basic", "video.publish", "video.upload"],
        }
        self.error = error
        self.calls = 0

    def valid_credentials(self):
        self.calls += 1
        if self.error:
            raise self.error
        return dict(self.credentials)


def creator_response(
    *,
    username="jaylz51",
    privacy_levels=None,
    comment_disabled=False,
    duet_disabled=False,
    stitch_disabled=False,
    max_duration=300,
):
    if privacy_levels is None:
        privacy_levels = [
            "FOLLOWER_OF_CREATOR",
            "MUTUAL_FOLLOW_FRIENDS",
            "SELF_ONLY",
        ]
    return FakeResponse(
        200,
        {
            "data": {
                "creator_avatar_url": (
                    "https://example.invalid/temporary-avatar?secret=value"
                ),
                "creator_username": username,
                "creator_nickname": "Jay Sandbox",
                "privacy_level_options": privacy_levels,
                "comment_disabled": comment_disabled,
                "duet_disabled": duet_disabled,
                "stitch_disabled": stitch_disabled,
                "max_video_post_duration_sec": max_duration,
            },
            "error": {"code": "ok", "message": "", "log_id": "safe"},
        },
    )


def initialized_response():
    return FakeResponse(
        200,
        {
            "data": {
                "publish_id": PUBLISH_ID,
                "upload_url": UPLOAD_URL,
            },
            "error": {"code": "ok", "message": "", "log_id": "safe"},
        },
    )


def create_dummy_video(path: Path, size: int) -> None:
    with path.open("wb") as video_file:
        video_file.seek(size - 1)
        video_file.write(b"\0")


class TikTokCreatorInfoTests(unittest.TestCase):
    def test_creator_info_is_validated_and_sanitized_for_ui(self):
        http = FakeHttp(posts=[creator_response()])
        oauth = FakeOAuthService()
        publisher = TikTokPublisher(
            oauth_service_factory=lambda: oauth,
            http_client=http,
            expected_sandbox_username="@jaylz51",
        )

        result = publisher.fetch_creator_info()

        self.assertEqual(http.post_calls[0][0], TIKTOK_CREATOR_INFO_URL)
        self.assertEqual(oauth.calls, 1)
        self.assertEqual(result["creator_username"], "jaylz51")
        self.assertEqual(result["creator_nickname"], "Jay Sandbox")
        self.assertTrue(result["account_is_private"])
        self.assertTrue(result["sandbox_account_matches"])
        self.assertEqual(result["checkpoint_privacy_levels"], ["SELF_ONLY"])
        self.assertNotIn("creator_avatar_url", result)
        self.assertNotIn("access_token", json.dumps(result))
        self.assertNotIn("temporary-avatar", json.dumps(result))

    def test_creator_info_rate_limit_and_malformed_data_are_safe(self):
        cases = [
            (
                FakeResponse(
                    429,
                    {
                        "error": {
                            "code": "rate_limit_exceeded",
                            "message": "secret provider detail",
                        }
                    },
                ),
                "rate limited",
            ),
            (creator_response(max_duration="300"), "malformed"),
            (creator_response(privacy_levels=["UNKNOWN"]), "malformed"),
        ]

        for response, expected in cases:
            with self.subTest(expected=expected):
                http = FakeHttp(posts=[response])
                publisher = TikTokPublisher(
                    oauth_service_factory=lambda: FakeOAuthService(),
                    http_client=http,
                )
                with self.assertRaisesRegex(TikTokUploadError, expected) as ctx:
                    publisher.fetch_creator_info()
                self.assertNotIn("secret provider detail", str(ctx.exception))
                self.assertEqual(len(http.post_calls), 1)
                self.assertEqual(http.put_calls, [])

    def test_creator_info_route_returns_only_safe_fields(self):
        safe_info = {
            "creator_username": "jaylz51",
            "creator_nickname": "Jay Sandbox",
            "privacy_level_options": ["SELF_ONLY"],
            "comment_disabled": False,
            "duet_disabled": True,
            "stitch_disabled": True,
            "max_video_post_duration_sec": 300,
            "account_is_private": True,
            "sandbox_account_matches": True,
            "checkpoint_privacy_levels": ["SELF_ONLY"],
        }
        publisher = Mock()
        publisher.fetch_creator_info.return_value = safe_info

        with patch.object(
            accounts,
            "_tiktok_settings",
            return_value={"environment": "sandbox"},
        ), patch.object(
            accounts,
            "TikTokPublisher",
            return_value=publisher,
        ):
            result = accounts.tiktok_creator_info()

        self.assertEqual(result, {
            "platform": "tiktok",
            "environment": "sandbox",
            **safe_info,
        })
        publisher.fetch_creator_info.assert_called_once_with()


class TikTokPrivateDirectPostTests(unittest.TestCase):
    def make_publisher(
        self,
        *,
        http=None,
        oauth=None,
        direct_enabled=True,
        public_enabled=False,
        environment="sandbox",
        expected_username="jaylz51",
        duration=30.0,
    ):
        oauth = oauth or FakeOAuthService()
        duration_probe = (
            duration
            if callable(duration)
            else lambda _path: duration
        )
        return TikTokPublisher(
            oauth_service_factory=lambda: oauth,
            http_client=http or FakeHttp(),
            direct_post_enabled=direct_enabled,
            public_posting_enabled=public_enabled,
            environment=environment,
            expected_sandbox_username=expected_username,
            duration_probe=duration_probe,
            sleeper=lambda _: None,
        )

    def publish(self, publisher, video, **overrides):
        options = {
            "video_path": str(video),
            "title": "SMH title",
            "description": "Private Sandbox #test",
            "publish": True,
            "mode": "direct_post",
            "user_consent": True,
            "privacy_level": "SELF_ONLY",
            "allow_comment": False,
            "allow_duet": False,
            "allow_stitch": False,
            "commercial_content_disclosure": False,
            "brand_content_toggle": False,
            "brand_organic_toggle": False,
            "is_aigc": False,
        }
        options.update(overrides)
        return publisher.publish(**options)

    def video(self, directory, name="sample.mp4", size=100):
        path = Path(directory) / name
        create_dummy_video(path, size)
        return path

    def test_local_safety_gates_block_before_any_http(self):
        cases = [
            ({"user_consent": False}, {}, "consent"),
            ({}, {"direct_enabled": False}, "disabled"),
            ({}, {"public_enabled": True}, "public posting"),
            ({}, {"environment": "production"}, "Sandbox"),
            ({"privacy_level": None}, {}, "privacy selection"),
            (
                {"privacy_level": "PUBLIC_TO_EVERYONE"},
                {},
                "SELF_ONLY",
            ),
        ]

        for publish_options, publisher_options, expected in cases:
            with self.subTest(expected=expected):
                http = FakeHttp()
                oauth_factory = Mock()
                publisher = TikTokPublisher(
                    oauth_service_factory=oauth_factory,
                    http_client=http,
                    direct_post_enabled=publisher_options.get(
                        "direct_enabled",
                        True,
                    ),
                    public_posting_enabled=publisher_options.get(
                        "public_enabled",
                        False,
                    ),
                    environment=publisher_options.get(
                        "environment",
                        "sandbox",
                    ),
                    expected_sandbox_username="jaylz51",
                    duration_probe=lambda _path: 30.0,
                )
                with tempfile.TemporaryDirectory() as directory:
                    video = self.video(directory)
                    with self.assertRaisesRegex(TikTokUploadError, expected):
                        self.publish(publisher, video, **publish_options)
                oauth_factory.assert_not_called()
                self.assertEqual(http.post_calls, [])
                self.assertEqual(http.put_calls, [])

    def test_missing_scope_and_invalid_authorization_stop_before_http(self):
        cases = [
            (
                FakeOAuthService(
                    credentials={
                        "access_token": "token",
                        "scopes": ["video.upload"],
                    }
                ),
                "video.publish",
            ),
            (
                FakeOAuthService(
                    error=TikTokReauthorizationRequired(
                        "sensitive credential detail",
                        error_code="not_connected",
                    )
                ),
                "Reconnect TikTok",
            ),
        ]

        for oauth, expected in cases:
            with self.subTest(expected=expected):
                http = FakeHttp()
                publisher = self.make_publisher(http=http, oauth=oauth)
                with tempfile.TemporaryDirectory() as directory:
                    video = self.video(directory)
                    with self.assertRaisesRegex(TikTokUploadError, expected) as ctx:
                        self.publish(publisher, video)
                self.assertNotIn("sensitive credential", str(ctx.exception))
                self.assertEqual(http.post_calls, [])
                self.assertEqual(http.put_calls, [])

    def test_creator_info_must_match_private_sandbox_account(self):
        cases = [
            (creator_response(username="someone_else"), "unexpected"),
            (
                creator_response(
                    privacy_levels=[
                        "FOLLOWER_OF_CREATOR",
                        "MUTUAL_FOLLOW_FRIENDS",
                    ]
                ),
                "SELF_ONLY",
            ),
            (
                creator_response(
                    privacy_levels=[
                        "PUBLIC_TO_EVERYONE",
                        "MUTUAL_FOLLOW_FRIENDS",
                        "SELF_ONLY",
                    ]
                ),
                "private",
            ),
        ]

        for response, expected in cases:
            with self.subTest(expected=expected):
                http = FakeHttp(posts=[response])
                publisher = self.make_publisher(http=http)
                with tempfile.TemporaryDirectory() as directory:
                    video = self.video(directory)
                    with self.assertRaisesRegex(TikTokUploadError, expected):
                        self.publish(publisher, video)
                self.assertEqual(len(http.post_calls), 1)
                self.assertEqual(http.post_calls[0][0], TIKTOK_CREATOR_INFO_URL)
                self.assertEqual(http.put_calls, [])

    def test_creator_interaction_restrictions_are_enforced(self):
        cases = [
            ("allow_comment", {"comment_disabled": True}, "comments"),
            ("allow_duet", {"duet_disabled": True}, "Duet"),
            ("allow_stitch", {"stitch_disabled": True}, "Stitch"),
        ]

        for option, creator_options, expected in cases:
            with self.subTest(option=option):
                http = FakeHttp(posts=[creator_response(**creator_options)])
                publisher = self.make_publisher(http=http)
                with tempfile.TemporaryDirectory() as directory:
                    video = self.video(directory)
                    with self.assertRaisesRegex(TikTokUploadError, expected):
                        self.publish(publisher, video, **{option: True})
                self.assertEqual(len(http.post_calls), 1)
                self.assertEqual(http.put_calls, [])

    def test_commercial_disclosure_combinations_fail_closed(self):
        cases = [
            (
                {"commercial_content_disclosure": True},
                "at least one brand type",
            ),
            (
                {"brand_organic_toggle": True},
                "Enable commercial content disclosure",
            ),
            (
                {
                    "commercial_content_disclosure": True,
                    "brand_content_toggle": True,
                },
                "cannot use SELF_ONLY",
            ),
        ]

        for options, expected in cases:
            with self.subTest(options=options):
                http = FakeHttp()
                publisher = self.make_publisher(http=http)
                with tempfile.TemporaryDirectory() as directory:
                    video = self.video(directory)
                    with self.assertRaisesRegex(TikTokUploadError, expected):
                        self.publish(publisher, video, **options)
                self.assertEqual(http.post_calls, [])
                self.assertEqual(http.put_calls, [])

    def test_duration_must_be_verified_and_within_creator_limit(self):
        cases = [
            (
                lambda _path: (_ for _ in ()).throw(
                    TikTokUploadError(
                        "TikTok Direct Post requires ffprobe.",
                        error_code="duration_probe_unavailable",
                    )
                ),
                300,
                "ffprobe",
            ),
            (301.0, 300, "duration exceeds"),
        ]

        for duration, maximum, expected in cases:
            with self.subTest(expected=expected):
                http = FakeHttp(posts=[creator_response(max_duration=maximum)])
                publisher = self.make_publisher(
                    http=http,
                    duration=duration,
                )
                with tempfile.TemporaryDirectory() as directory:
                    video = self.video(directory)
                    with self.assertRaisesRegex(TikTokUploadError, expected):
                        self.publish(publisher, video)
                self.assertEqual(len(http.post_calls), 1)
                self.assertEqual(http.put_calls, [])

    def test_direct_post_initialization_and_whole_file_transfer(self):
        video_size = 8_734_468
        http = FakeHttp(
            posts=[creator_response(), initialized_response()],
            puts=[
                FakeResponse(
                    201,
                    headers={
                        "Content-Range": f"bytes 0-{video_size}/{video_size}"
                    },
                )
            ],
        )
        publisher = self.make_publisher(http=http)

        with tempfile.TemporaryDirectory() as directory:
            video = self.video(directory, size=video_size)
            result = self.publish(
                publisher,
                video,
                commercial_content_disclosure=True,
                brand_organic_toggle=True,
                is_aigc=True,
            )

        self.assertEqual(len(http.post_calls), 2)
        self.assertEqual(http.post_calls[0][0], TIKTOK_CREATOR_INFO_URL)
        self.assertEqual(
            http.post_calls[1][0],
            TIKTOK_DIRECT_POST_INITIALIZE_URL,
        )
        body = http.post_calls[1][1]["json"]
        self.assertEqual(
            body["source_info"],
            {
                "source": "FILE_UPLOAD",
                "video_size": video_size,
                "chunk_size": video_size,
                "total_chunk_count": 1,
            },
        )
        self.assertEqual(
            body["post_info"],
            {
                "title": "Private Sandbox #test",
                "privacy_level": "SELF_ONLY",
                "disable_comment": True,
                "disable_duet": True,
                "disable_stitch": True,
                "brand_content_toggle": False,
                "brand_organic_toggle": True,
                "is_aigc": True,
            },
        )
        self.assertEqual(
            http.put_calls[0]["headers"]["Content-Range"],
            "bytes 0-8734467/8734468",
        )
        self.assertEqual(result["status"], "processing")
        self.assertFalse(result["published"])
        self.assertFalse(result["dry_run"])
        self.assertEqual(result["mode"], "direct_post")
        self.assertEqual(result["remote_publish_id"], PUBLISH_ID)
        self.assertIsNone(result["url"])
        self.assertTrue(result["caption_transferred"])
        self.assertNotIn("upload_url", json.dumps(result))
        self.assertNotIn("sensitive-direct-token", json.dumps(result))

    def test_direct_post_reuses_chunked_206_and_201_transfer(self):
        video_size = (25 * 1024 * 1024) + 123
        first_count = TIKTOK_DEFAULT_CHUNK_SIZE_BYTES
        http = FakeHttp(
            posts=[creator_response(), initialized_response()],
            puts=[
                FakeResponse(
                    206,
                    headers={
                        "Content-Range": f"bytes 0-{first_count}/{video_size}"
                    },
                ),
                FakeResponse(
                    201,
                    headers={
                        "Content-Range": f"bytes 0-{video_size}/{video_size}"
                    },
                ),
            ],
        )
        publisher = self.make_publisher(http=http)

        with tempfile.TemporaryDirectory() as directory:
            video = self.video(directory, name="sample.webm", size=video_size)
            result = self.publish(publisher, video)

        self.assertEqual(result["status"], "processing")
        self.assertEqual(len(http.put_calls), 2)
        self.assertEqual(
            http.put_calls[0]["headers"]["Content-Range"],
            f"bytes 0-{first_count - 1}/{video_size}",
        )
        self.assertEqual(
            http.put_calls[1]["headers"]["Content-Range"],
            f"bytes {first_count}-{video_size - 1}/{video_size}",
        )

    def test_refreshed_token_is_used_for_creator_and_initialization(self):
        oauth = FakeOAuthService(
            credentials={
                "access_token": "refreshed-direct-token",
                "scopes": ["video.publish"],
            }
        )
        http = FakeHttp(
            posts=[creator_response(), initialized_response()],
            puts=[FakeResponse(201)],
        )
        publisher = self.make_publisher(http=http, oauth=oauth)

        with tempfile.TemporaryDirectory() as directory:
            video = self.video(directory)
            self.publish(publisher, video)

        self.assertEqual(oauth.calls, 1)
        for _, kwargs in http.post_calls:
            self.assertEqual(
                kwargs["headers"]["Authorization"],
                "Bearer refreshed-direct-token",
            )

    def test_transfer_failure_is_redacted_and_never_reinitializes(self):
        http = FakeHttp(
            posts=[creator_response(), initialized_response()],
            puts=[
                requests.ConnectionError(
                    "sensitive-direct-token https://signed.example/upload"
                )
            ],
        )
        publisher = self.make_publisher(http=http)

        with tempfile.TemporaryDirectory() as directory:
            video = self.video(directory)
            with self.assertRaises(TikTokUploadError) as ctx:
                self.publish(publisher, video)

        rendered = str(ctx.exception)
        self.assertNotIn("sensitive-direct-token", rendered)
        self.assertNotIn("signed.example", rendered)
        self.assertEqual(len(http.post_calls), 2)
        self.assertEqual(
            sum(
                call[0] == TIKTOK_DIRECT_POST_INITIALIZE_URL
                for call in http.post_calls
            ),
            1,
        )
        self.assertEqual(len(http.put_calls), 1)
        self.assertEqual(ctx.exception.publish_id, PUBLISH_ID)

    def test_caption_boundary_and_unsupported_media_fail_before_posting(self):
        cases = [
            ("a" * 2201, "sample.mp4", "caption exceeds"),
            ("caption", "sample.txt", "MP4, MOV, or WebM"),
        ]

        for caption, name, expected in cases:
            with self.subTest(name=name):
                http = FakeHttp()
                publisher = self.make_publisher(http=http)
                with tempfile.TemporaryDirectory() as directory:
                    video = self.video(directory, name=name)
                    with self.assertRaisesRegex(ValueError, expected):
                        self.publish(
                            publisher,
                            video,
                            description=caption,
                        )
                self.assertEqual(http.post_calls, [])
                self.assertEqual(http.put_calls, [])


class TikTokPrivateDirectPostWorkflowTests(unittest.TestCase):
    def test_processing_result_is_stored_without_published_timestamp_or_drive(self):
        http = FakeHttp(
            posts=[creator_response(), initialized_response()],
            puts=[FakeResponse(201)],
        )
        publisher = TikTokPublisher(
            oauth_service_factory=lambda: FakeOAuthService(),
            http_client=http,
            direct_post_enabled=True,
            public_posting_enabled=False,
            environment="sandbox",
            expected_sandbox_username="jaylz51",
            duration_probe=lambda _path: 30.0,
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
                title="Private Direct Post",
                caption="Private caption #sandbox",
                platforms=["tiktok"],
                tiktok_publish=True,
                tiktok_mode="direct_post",
                tiktok_user_consent=True,
                tiktok_privacy_level="SELF_ONLY",
                tiktok_is_aigc=True,
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
        self.assertEqual(result["status"], "processing")
        self.assertEqual(result["mode"], "direct_post")
        self.assertEqual(result["privacy_level"], "SELF_ONLY")
        self.assertEqual(result["remote_publish_id"], PUBLISH_ID)
        self.assertIsNone(result["url"])
        self.assertEqual(stored[0]["status"], "processing")
        self.assertEqual(stored[0]["mode"], "direct_post")
        self.assertEqual(stored[0]["remote_post_id"], PUBLISH_ID)
        self.assertEqual(stored[0]["description"], "Private caption #sandbox")
        self.assertIsNone(stored[0]["published_at"])
        drive.assert_not_called()

    def test_openapi_has_creator_info_and_explicit_direct_post_options(self):
        from main import app

        schema = app.openapi()
        request_schema = schema["components"]["schemas"][
            "PublishPostRequest"
        ]
        properties = request_schema["properties"]

        self.assertIn("get", schema["paths"]["/accounts/tiktok/creator-info"])
        self.assertNotIn(
            "tiktok_privacy_level",
            request_schema.get("required", []),
        )
        self.assertNotIn("default", properties["tiktok_privacy_level"])
        for field in (
            "tiktok_allow_comment",
            "tiktok_allow_duet",
            "tiktok_allow_stitch",
            "tiktok_commercial_content_disclosure",
            "tiktok_brand_content_toggle",
            "tiktok_brand_organic_toggle",
            "tiktok_is_aigc",
        ):
            self.assertFalse(properties[field]["default"])


if __name__ == "__main__":
    unittest.main()
