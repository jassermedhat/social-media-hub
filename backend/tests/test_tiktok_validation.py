import json
import sqlite3
import sys
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch


BACKEND_DIR = Path(__file__).resolve().parents[1]

if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from api import history, posts
from database import db
from integrations import tiktok as tiktok_integration
from integrations.tiktok import (
    TIKTOK_MAX_CAPTION_UTF16_UNITS,
    TIKTOK_MAX_VIDEO_SIZE_BYTES,
    TikTokPublisher,
    count_utf16_units,
)


class TikTokPublisherValidationTests(unittest.TestCase):
    def publish(self, video_path, caption=""):
        return TikTokPublisher().publish(
            video_path=str(video_path),
            title="Test title",
            description=caption,
        )

    def test_valid_readable_file_is_ready_but_unpublished(self):
        with tempfile.TemporaryDirectory() as directory:
            video = Path(directory) / "sample.MP4"
            video.write_bytes(b"local-validation-only")

            result = self.publish(video, caption="A safe caption")

        self.assertTrue(result["success"])
        self.assertFalse(result["published"])
        self.assertTrue(result["dry_run"])
        self.assertEqual(result["status"], "ready")
        self.assertTrue(result["validation"]["local_only"])
        self.assertEqual(result["validation"]["container_extension"], "mp4")
        self.assertEqual(
            result["validation"]["unverified_media_properties"],
            ["codec", "resolution", "frame_rate", "duration"],
        )
        self.assertIsNone(result["video_id"])
        self.assertIsNone(result["url"])

    def test_missing_file_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            video = Path(directory) / "missing.mp4"

            with self.assertRaisesRegex(ValueError, "does not exist"):
                self.publish(video)

    def test_non_file_path_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError, "not a regular file"):
                self.publish(Path(directory))

    def test_empty_file_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            video = Path(directory) / "empty.mov"
            video.touch()

            with self.assertRaisesRegex(ValueError, "empty"):
                self.publish(video)

    def test_unsupported_extension_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            video = Path(directory) / "sample.avi"
            video.write_bytes(b"not-empty")

            with self.assertRaisesRegex(ValueError, "MP4, MOV, or WebM"):
                self.publish(video)

    def test_oversized_file_is_rejected_without_creating_one(self):
        fake_path = Mock()
        fake_path.exists.return_value = True
        fake_path.is_file.return_value = True
        fake_path.suffix = ".mp4"
        fake_path.stat.return_value = SimpleNamespace(
            st_size=TIKTOK_MAX_VIDEO_SIZE_BYTES + 1
        )

        with (
            patch.object(tiktok_integration, "Path", return_value=fake_path),
            self.assertRaisesRegex(ValueError, "exceeds the 4 GB"),
        ):
            self.publish("oversized.mp4")

        fake_path.open.assert_not_called()

    def test_unreadable_file_is_rejected(self):
        fake_path = Mock()
        fake_path.exists.return_value = True
        fake_path.is_file.return_value = True
        fake_path.suffix = ".webm"
        fake_path.stat.return_value = SimpleNamespace(st_size=100)
        fake_path.open.side_effect = PermissionError("denied")

        with (
            patch.object(tiktok_integration, "Path", return_value=fake_path),
            self.assertRaisesRegex(ValueError, "cannot be opened for reading"),
        ):
            self.publish("unreadable.webm")

    def test_caption_limit_counts_utf16_code_units(self):
        with tempfile.TemporaryDirectory() as directory:
            video = Path(directory) / "sample.mp4"
            video.write_bytes(b"not-empty")

            ascii_boundary = "a" * TIKTOK_MAX_CAPTION_UTF16_UNITS
            self.assertEqual(
                self.publish(video, ascii_boundary)["status"],
                "ready",
            )

            with self.assertRaisesRegex(ValueError, "2,200 UTF-16"):
                self.publish(video, ascii_boundary + "a")

            emoji_boundary = "\U0001f600" * 1100
            self.assertEqual(count_utf16_units(emoji_boundary), 2200)
            self.assertEqual(
                self.publish(video, emoji_boundary)["status"],
                "ready",
            )

            with self.assertRaisesRegex(ValueError, "2,200 UTF-16"):
                self.publish(video, emoji_boundary + "\U0001f600")

    def test_remote_publish_flag_is_rejected_before_file_access(self):
        with self.assertRaisesRegex(ValueError, "not implemented"):
            TikTokPublisher().publish(
                video_path="does-not-need-to-exist.mp4",
                title="Test",
                publish=True,
            )

    def test_local_validation_makes_no_http_request(self):
        with tempfile.TemporaryDirectory() as directory:
            video = Path(directory) / "sample.webm"
            video.write_bytes(b"not-empty")

            with patch("requests.sessions.Session.request") as request:
                result = self.publish(video)

        self.assertEqual(result["status"], "ready")
        request.assert_not_called()


class TikTokPublishingWorkflowTests(unittest.TestCase):
    def request(self, video_path, platforms=None, **overrides):
        payload = {
            "video_path": str(video_path),
            "title": "Test title",
            "caption": "Test caption",
            "platforms": platforms or ["tiktok"],
        }
        payload.update(overrides)
        return posts.PublishPostRequest(**payload)

    def test_tiktok_publish_true_is_rejected_and_isolated(self):
        service = Mock()
        service.publish.return_value = {
            "video_id": "youtube-id",
            "url": "https://example.invalid/video",
        }

        with (
            patch.object(posts, "create_post", return_value=41),
            patch.object(posts, "create_publish_result") as record,
            patch.object(posts, "PublishingService", return_value=service),
            patch.object(posts, "GoogleDriveMediaStorage") as drive,
        ):
            response = posts.publish_post(
                self.request(
                    "unused.mp4",
                    platforms=["tiktok", "youtube"],
                    tiktok_publish=True,
                )
            )

        self.assertFalse(response["results"]["tiktok"]["success"])
        self.assertIn(
            "Remote TikTok publishing is not implemented",
            response["results"]["tiktok"]["error"],
        )
        self.assertTrue(response["results"]["youtube"]["success"])
        self.assertTrue(response["results"]["youtube"]["published"])
        self.assertEqual(service.publish.call_count, 1)
        self.assertEqual(service.publish.call_args.kwargs["platform"], "youtube")
        self.assertEqual(record.call_count, 2)
        drive.assert_not_called()

    def test_tiktok_failure_does_not_block_another_platform(self):
        service = Mock()

        def publish(**kwargs):
            if kwargs["platform"] == "tiktok":
                raise ValueError("TikTok local validation failed")
            return {
                "video_id": "youtube-id",
                "url": "https://example.invalid/video",
            }

        service.publish.side_effect = publish

        with (
            patch.object(posts, "create_post", return_value=42),
            patch.object(posts, "create_publish_result"),
            patch.object(posts, "PublishingService", return_value=service),
            patch.object(posts, "GoogleDriveMediaStorage") as drive,
        ):
            response = posts.publish_post(
                self.request("unused.mp4", platforms=["tiktok", "youtube"])
            )

        self.assertFalse(response["results"]["tiktok"]["success"])
        self.assertTrue(response["results"]["youtube"]["success"])
        self.assertEqual(service.publish.call_count, 2)
        drive.assert_not_called()

    def test_publisher_result_compatibility_and_explicit_published_false(self):
        service = Mock()

        def publish(**kwargs):
            if kwargs["platform"] == "youtube":
                return {
                    "video_id": "youtube-id",
                    "url": "https://example.invalid/video",
                }
            return {
                "published": False,
                "dry_run": False,
                "status": "processing",
            }

        service.publish.side_effect = publish

        with (
            patch.object(posts, "create_post", return_value=43),
            patch.object(posts, "create_publish_result"),
            patch.object(posts, "PublishingService", return_value=service),
        ):
            response = posts.publish_post(
                self.request("unused.mp4", platforms=["youtube", "linkedin"])
            )

        self.assertTrue(response["results"]["youtube"]["published"])
        self.assertEqual(response["results"]["youtube"]["status"], "published")
        self.assertFalse(response["results"]["linkedin"]["published"])
        self.assertEqual(response["results"]["linkedin"]["status"], "processing")

    def test_ready_result_is_stored_and_visible_in_history(self):
        with tempfile.TemporaryDirectory() as directory:
            temp_dir = Path(directory)
            video = temp_dir / "sample.mp4"
            video.write_bytes(b"dummy-local-file")
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

            with (
                patch.object(db, "get_connection", temporary_connection),
                patch.object(posts, "GoogleDriveMediaStorage") as drive,
            ):
                db.init_db()
                response = posts.publish_post(self.request(video))
                stored = history.publishing_history()["items"]

        tiktok_result = response["results"]["tiktok"]
        self.assertTrue(tiktok_result["success"])
        self.assertFalse(tiktok_result["published"])
        self.assertTrue(tiktok_result["dry_run"])
        self.assertEqual(tiktok_result["status"], "ready")
        self.assertTrue(tiktok_result["validation"]["local_only"])
        self.assertEqual(len(stored), 1)
        self.assertEqual(stored[0]["platform"], "tiktok")
        self.assertEqual(stored[0]["status"], "ready")
        self.assertIsNone(stored[0]["remote_post_id"])
        self.assertIsNone(stored[0]["remote_url"])
        self.assertIsNone(stored[0]["published_at"])
        drive.assert_not_called()

    def test_openapi_exposes_safe_tiktok_publish_default(self):
        from main import app

        schema = app.openapi()
        request_schema = schema["components"]["schemas"]["PublishPostRequest"]
        field = request_schema["properties"]["tiktok_publish"]

        self.assertEqual(field["default"], False)
        rendered = json.dumps(schema)
        self.assertIn("tiktok_publish", rendered)


if __name__ == "__main__":
    unittest.main()
