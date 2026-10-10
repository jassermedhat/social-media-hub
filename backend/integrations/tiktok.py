from pathlib import Path


TIKTOK_SUPPORTED_VIDEO_EXTENSIONS = frozenset({".mp4", ".mov", ".webm"})
TIKTOK_MAX_VIDEO_SIZE_BYTES = 4 * 1024 * 1024 * 1024
TIKTOK_MAX_CAPTION_UTF16_UNITS = 2200


def count_utf16_units(value: str) -> int:
    """Return the number of UTF-16 code units TikTok uses for caption limits."""
    try:
        return len(value.encode("utf-16-le")) // 2
    except UnicodeEncodeError as exc:
        raise ValueError(
            "TikTok caption contains invalid Unicode characters."
        ) from exc


class TikTokPublisher:
    """Validate a prospective TikTok video without making network requests."""

    def publish(
        self,
        video_path: str,
        title: str,
        description: str = "",
        privacy: str = "private",
        publish: bool = False,
    ):
        if publish:
            raise ValueError(
                "Remote TikTok publishing is not implemented. "
                "Set tiktok_publish=false to run local validation only."
            )

        path = Path(video_path)

        if not path.exists():
            raise ValueError("TikTok video file does not exist.")

        if not path.is_file():
            raise ValueError("TikTok video path is not a regular file.")

        extension = path.suffix.lower()
        if extension not in TIKTOK_SUPPORTED_VIDEO_EXTENSIONS:
            raise ValueError(
                "TikTok video must use an MP4, MOV, or WebM container."
            )

        try:
            file_size = path.stat().st_size
        except OSError as exc:
            raise ValueError("TikTok video file metadata could not be read.") from exc

        if file_size == 0:
            raise ValueError("TikTok video file is empty.")

        if file_size > TIKTOK_MAX_VIDEO_SIZE_BYTES:
            raise ValueError("TikTok video file exceeds the 4 GB maximum size.")

        try:
            with path.open("rb") as video_file:
                video_file.read(1)
        except OSError as exc:
            raise ValueError("TikTok video file cannot be opened for reading.") from exc

        caption = description or ""
        caption_length = count_utf16_units(caption)
        if caption_length > TIKTOK_MAX_CAPTION_UTF16_UNITS:
            raise ValueError(
                "TikTok caption exceeds the 2,200 UTF-16 unit limit "
                f"({caption_length} provided)."
            )

        return {
            "success": True,
            "platform": "tiktok",
            "video_id": None,
            "url": None,
            "published": False,
            "dry_run": True,
            "status": "ready",
            "message": (
                "TikTok local validation passed. "
                "No upload or publishing request was made."
            ),
            "validation": {
                "local_only": True,
                "file_name": path.name,
                "file_size_bytes": file_size,
                "container_extension": extension.removeprefix("."),
                "caption_utf16_units": caption_length,
                "max_caption_utf16_units": TIKTOK_MAX_CAPTION_UTF16_UNITS,
                "max_file_size_bytes": TIKTOK_MAX_VIDEO_SIZE_BYTES,
                "unverified_media_properties": [
                    "codec",
                    "resolution",
                    "frame_rate",
                    "duration",
                ],
            },
        }
