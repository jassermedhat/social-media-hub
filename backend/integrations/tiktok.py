import math
import os
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit

import requests
from dotenv import load_dotenv

from services.tiktok_credentials import SecureStorageError
from services.tiktok_oauth import (
    TikTokOAuthError,
    create_tiktok_service,
    parse_scopes,
)


BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")

TIKTOK_SUPPORTED_VIDEO_EXTENSIONS = frozenset({".mp4", ".mov", ".webm"})
TIKTOK_MAX_VIDEO_SIZE_BYTES = 4 * 1024 * 1024 * 1024
TIKTOK_MAX_CAPTION_UTF16_UNITS = 2200
TIKTOK_UPLOAD_INITIALIZE_URL = (
    "https://open.tiktokapis.com/v2/post/publish/inbox/video/init/"
)
TIKTOK_UPLOAD_STATUS_URL = (
    "https://open.tiktokapis.com/v2/post/publish/status/fetch/"
)
TIKTOK_CREATOR_INFO_URL = (
    "https://open.tiktokapis.com/v2/post/publish/creator_info/query/"
)
TIKTOK_DIRECT_POST_INITIALIZE_URL = (
    "https://open.tiktokapis.com/v2/post/publish/video/init/"
)
TIKTOK_MIN_CHUNK_SIZE_BYTES = 5 * 1024 * 1024
TIKTOK_DEFAULT_CHUNK_SIZE_BYTES = 10 * 1024 * 1024
TIKTOK_MAX_CHUNK_SIZE_BYTES = 64 * 1024 * 1024
TIKTOK_MAX_FINAL_CHUNK_SIZE_BYTES = 128 * 1024 * 1024
TIKTOK_MAX_CHUNK_COUNT = 1000
TIKTOK_CHUNK_RETRIES = 2
TIKTOK_PRIVACY_LEVELS = frozenset(
    {
        "PUBLIC_TO_EVERYONE",
        "MUTUAL_FOLLOW_FRIENDS",
        "FOLLOWER_OF_CREATOR",
        "SELF_ONLY",
    }
)

_SAFE_ERROR_CODE = re.compile(r"^[A-Za-z0-9._-]{1,100}$")
_CONTENT_RANGE = re.compile(
    r"^bytes\s+0\s*-\s*(\d+)\s*/\s*(\d+)$",
    re.IGNORECASE,
)

_VIDEO_MIME_TYPES = {
    ".mp4": "video/mp4",
    ".mov": "video/quicktime",
    ".webm": "video/webm",
}


def _valid_publish_id(value: Any) -> bool:
    return bool(
        isinstance(value, str)
        and 1 <= len(value) <= 64
        and value == value.strip()
        and not any(character.isspace() for character in value)
        and all(
            ord(character) >= 32 and ord(character) != 127
            for character in value
        )
    )


class TikTokUploadError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        error_code: str | None = None,
        publish_id: str | None = None,
    ):
        super().__init__(message)
        self.error_code = (
            error_code
            if isinstance(error_code, str)
            and _SAFE_ERROR_CODE.fullmatch(error_code)
            else None
        )
        self.publish_id = (
            publish_id
            if _valid_publish_id(publish_id)
            else None
        )


def count_utf16_units(value: str) -> int:
    """Return the number of UTF-16 code units TikTok uses for caption limits."""
    try:
        return len(value.encode("utf-16-le")) // 2
    except UnicodeEncodeError as exc:
        raise ValueError(
            "TikTok caption contains invalid Unicode characters."
        ) from exc


def calculate_upload_plan(
    video_size: int,
    chunk_size: int = TIKTOK_DEFAULT_CHUNK_SIZE_BYTES,
) -> tuple[int, int]:
    if video_size <= 0:
        raise ValueError("TikTok video size must be greater than zero.")

    if video_size < TIKTOK_MIN_CHUNK_SIZE_BYTES:
        return video_size, 1

    if not (
        TIKTOK_MIN_CHUNK_SIZE_BYTES
        <= chunk_size
        <= TIKTOK_MAX_CHUNK_SIZE_BYTES
    ):
        raise ValueError("TikTok chunk size must be between 5 MB and 64 MB.")

    if video_size <= chunk_size:
        return video_size, 1

    total_chunk_count = video_size // chunk_size

    if not 1 <= total_chunk_count <= TIKTOK_MAX_CHUNK_COUNT:
        raise ValueError("TikTok upload requires an invalid number of chunks.")

    final_chunk_size = video_size - (
        chunk_size * (total_chunk_count - 1)
    )

    if not (
        chunk_size
        <= final_chunk_size
        <= TIKTOK_MAX_FINAL_CHUNK_SIZE_BYTES
    ):
        raise ValueError("TikTok upload would produce an invalid final chunk.")

    return chunk_size, total_chunk_count


def iter_chunk_ranges(
    video_size: int,
    chunk_size: int,
    total_chunk_count: int,
):
    for index in range(total_chunk_count):
        start = index * chunk_size
        length = (
            video_size - start
            if index == total_chunk_count - 1
            else chunk_size
        )
        yield start, start + length - 1, length


def _environment_boolean(name: str, default: bool = False) -> bool:
    value = os.getenv(name)

    if value is None:
        return default

    normalized = value.strip().lower()

    if normalized in {"1", "true", "yes", "on"}:
        return True

    if normalized in {"0", "false", "no", "off"}:
        return False

    raise TikTokUploadError(
        f"{name} must be true or false.",
        error_code="invalid_configuration",
    )


def probe_video_duration_seconds(path: Path) -> float:
    """Read video duration with an existing ffprobe installation."""
    executable = shutil.which("ffprobe")
    if not executable:
        raise TikTokUploadError(
            "TikTok Direct Post requires ffprobe to verify video duration.",
            error_code="duration_probe_unavailable",
        )

    creation_flags = (
        getattr(subprocess, "CREATE_NO_WINDOW", 0)
        if os.name == "nt"
        else 0
    )

    try:
        completed = subprocess.run(
            [
                executable,
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                str(path),
            ],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
            creationflags=creation_flags,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise TikTokUploadError(
            "TikTok video duration could not be verified.",
            error_code="duration_probe_failed",
        ) from exc

    try:
        duration = float(completed.stdout.strip())
    except (TypeError, ValueError) as exc:
        raise TikTokUploadError(
            "TikTok video duration could not be verified.",
            error_code="duration_probe_failed",
        ) from exc

    if completed.returncode != 0 or not math.isfinite(duration) or duration <= 0:
        raise TikTokUploadError(
            "TikTok video duration could not be verified.",
            error_code="duration_probe_failed",
        )

    return duration


def _default_oauth_service():
    client_key = os.getenv("TIKTOK_CLIENT_KEY", "").strip()
    client_secret = os.getenv("TIKTOK_CLIENT_SECRET", "").strip()
    environment = os.getenv("TIKTOK_ENVIRONMENT", "sandbox").strip().lower()

    if not client_key or not client_secret or environment != "sandbox":
        raise TikTokUploadError(
            "TikTok Sandbox OAuth is not configured safely.",
            error_code="invalid_configuration",
        )

    return create_tiktok_service(
        {
            "client_key": client_key,
            "client_secret": client_secret,
        },
        BASE_DIR,
    )


class TikTokPublisher:
    """Validate locally or transfer a video for manual completion in TikTok."""

    def __init__(
        self,
        *,
        oauth_service_factory: Callable[[], Any] = _default_oauth_service,
        http_client=requests,
        manual_upload_enabled: bool | None = None,
        direct_post_enabled: bool | None = None,
        public_posting_enabled: bool | None = None,
        environment: str | None = None,
        expected_sandbox_username: str | None = None,
        duration_probe: Callable[[Path], float] = probe_video_duration_seconds,
        chunk_size: int = TIKTOK_DEFAULT_CHUNK_SIZE_BYTES,
        max_chunk_retries: int = TIKTOK_CHUNK_RETRIES,
        sleeper: Callable[[float], None] = time.sleep,
    ):
        self.oauth_service_factory = oauth_service_factory
        self.http_client = http_client
        self.manual_upload_enabled = manual_upload_enabled
        self.direct_post_enabled = direct_post_enabled
        self.public_posting_enabled = public_posting_enabled
        self.environment = environment
        self.expected_sandbox_username = expected_sandbox_username
        self.duration_probe = duration_probe
        self.chunk_size = chunk_size
        self.max_chunk_retries = max(0, int(max_chunk_retries))
        self.sleeper = sleeper

    def publish(
        self,
        video_path: str,
        title: str,
        description: str = "",
        privacy: str = "private",
        publish: bool = False,
        mode: str = "validation",
        user_consent: bool = False,
        privacy_level: str | None = None,
        allow_comment: bool = False,
        allow_duet: bool = False,
        allow_stitch: bool = False,
        commercial_content_disclosure: bool = False,
        brand_content_toggle: bool = False,
        brand_organic_toggle: bool = False,
        is_aigc: bool = False,
    ):
        del title, privacy
        path, validation = self._validate(video_path, description)

        if not publish:
            return {
                "success": True,
                "platform": "tiktok",
                "video_id": None,
                "remote_publish_id": None,
                "url": None,
                "published": False,
                "dry_run": True,
                "status": "ready",
                "mode": "validation",
                "message": (
                    "TikTok local validation passed. "
                    "No upload or publishing request was made."
                ),
                "validation": validation,
            }

        normalized_mode = str(mode or "").strip().lower()

        if normalized_mode == "direct_post":
            return self._publish_direct_post(
                path=path,
                validation=validation,
                caption=description or "",
                user_consent=user_consent,
                privacy_level=privacy_level,
                allow_comment=allow_comment,
                allow_duet=allow_duet,
                allow_stitch=allow_stitch,
                commercial_content_disclosure=commercial_content_disclosure,
                brand_content_toggle=brand_content_toggle,
                brand_organic_toggle=brand_organic_toggle,
                is_aigc=is_aigc,
            )

        if normalized_mode != "manual_upload":
            raise TikTokUploadError(
                "TikTok remote transfer requires tiktok_mode=manual_upload.",
                error_code="manual_upload_mode_required",
            )

        if not user_consent:
            raise TikTokUploadError(
                "Explicit user consent is required before transferring to TikTok.",
                error_code="user_consent_required",
            )

        live_enabled = self.manual_upload_enabled
        if live_enabled is None:
            live_enabled = _environment_boolean(
                "TIKTOK_MANUAL_UPLOAD_ENABLED",
                False,
            )

        if not live_enabled:
            raise TikTokUploadError(
                "TikTok manual upload is disabled by server configuration.",
                error_code="manual_upload_disabled",
            )

        credentials = self._valid_credentials()
        granted_scopes = parse_scopes(credentials.get("scopes") or [])

        if "video.upload" not in granted_scopes:
            raise TikTokUploadError(
                "TikTok authorization does not grant video.upload. Reconnect TikTok.",
                error_code="scope_not_authorized",
            )

        access_token = credentials.get("access_token")
        if not isinstance(access_token, str) or not access_token:
            raise TikTokUploadError(
                "TikTok authorization is invalid. Reconnect TikTok.",
                error_code="invalid_stored_credentials",
            )

        chunk_size, total_chunk_count = calculate_upload_plan(
            validation["file_size_bytes"],
            self.chunk_size,
        )

        try:
            with path.open("rb") as video_file:
                publish_id, upload_url = self._initialize_upload(
                    access_token=access_token,
                    video_size=validation["file_size_bytes"],
                    chunk_size=chunk_size,
                    total_chunk_count=total_chunk_count,
                )
                self._transfer_file(
                    video_file=video_file,
                    upload_url=upload_url,
                    publish_id=publish_id,
                    video_size=validation["file_size_bytes"],
                    chunk_size=chunk_size,
                    total_chunk_count=total_chunk_count,
                    mime_type=_VIDEO_MIME_TYPES[path.suffix.lower()],
                )
        except TikTokUploadError:
            raise
        except OSError as exc:
            raise TikTokUploadError(
                "TikTok file transfer was interrupted. Do not start a new upload "
                "until the existing session is checked or has expired.",
                error_code="file_transfer_interrupted",
                publish_id=locals().get("publish_id"),
            ) from exc

        return {
            "success": True,
            "platform": "tiktok",
            "video_id": None,
            "remote_publish_id": publish_id,
            "url": None,
            "published": False,
            "dry_run": False,
            "status": "manual_completion",
            "mode": "manual_upload",
            "message": (
                "The video was transferred to TikTok. Open TikTok's inbox "
                "notification to finish editing and publishing."
            ),
            "caption_transferred": False,
            "validation": {
                **validation,
                "local_only": False,
            },
        }

    def _publish_direct_post(
        self,
        *,
        path: Path,
        validation: dict[str, Any],
        caption: str,
        user_consent: bool,
        privacy_level: str | None,
        allow_comment: bool,
        allow_duet: bool,
        allow_stitch: bool,
        commercial_content_disclosure: bool,
        brand_content_toggle: bool,
        brand_organic_toggle: bool,
        is_aigc: bool,
    ) -> dict[str, Any]:
        if not user_consent:
            raise TikTokUploadError(
                "Explicit user consent is required before posting to TikTok.",
                error_code="user_consent_required",
            )

        environment = (
            self.environment
            if self.environment is not None
            else os.getenv("TIKTOK_ENVIRONMENT", "sandbox")
        )
        if str(environment).strip().lower() != "sandbox":
            raise TikTokUploadError(
                "TikTok Direct Post is restricted to the Sandbox environment.",
                error_code="sandbox_environment_required",
            )

        direct_post_enabled = self.direct_post_enabled
        if direct_post_enabled is None:
            direct_post_enabled = _environment_boolean(
                "TIKTOK_DIRECT_POST_ENABLED",
                False,
            )
        if not direct_post_enabled:
            raise TikTokUploadError(
                "TikTok Direct Post is disabled by server configuration.",
                error_code="direct_post_disabled",
            )

        public_posting_enabled = self.public_posting_enabled
        if public_posting_enabled is None:
            public_posting_enabled = _environment_boolean(
                "TIKTOK_PUBLIC_POSTING_ENABLED",
                False,
            )
        if public_posting_enabled:
            raise TikTokUploadError(
                "TikTok public posting must remain disabled for Sandbox testing.",
                error_code="public_posting_must_be_disabled",
            )

        if privacy_level is None:
            raise TikTokUploadError(
                "TikTok Direct Post requires an explicit privacy selection.",
                error_code="privacy_level_required",
            )
        if privacy_level != "SELF_ONLY":
            raise TikTokUploadError(
                "TikTok Sandbox Direct Post permits only SELF_ONLY visibility.",
                error_code="private_visibility_required",
            )

        self._validate_commercial_disclosures(
            commercial_content_disclosure=commercial_content_disclosure,
            brand_content_toggle=brand_content_toggle,
            brand_organic_toggle=brand_organic_toggle,
        )

        expected_username = self.expected_sandbox_username
        if expected_username is None:
            expected_username = os.getenv(
                "TIKTOK_SANDBOX_ACCOUNT_USERNAME",
                "",
            )
        expected_username = self._normalize_username(expected_username)
        if not expected_username:
            raise TikTokUploadError(
                "The expected TikTok Sandbox account is not configured.",
                error_code="sandbox_account_not_configured",
            )

        credentials = self._credentials_with_scope("video.publish")
        creator_info = self._query_creator_info(credentials["access_token"])

        if self._normalize_username(
            creator_info["creator_username"]
        ) != expected_username:
            raise TikTokUploadError(
                "TikTok is connected to an unexpected Sandbox account.",
                error_code="sandbox_account_mismatch",
            )

        if "SELF_ONLY" not in creator_info["privacy_level_options"]:
            raise TikTokUploadError(
                "TikTok creator information does not permit SELF_ONLY posting.",
                error_code="self_only_unavailable",
            )

        if creator_info["account_is_private"] is not True:
            raise TikTokUploadError(
                "TikTok Direct Post requires the Sandbox account to be private.",
                error_code="private_account_required",
            )

        restricted_interactions = {
            "comments": (allow_comment, creator_info["comment_disabled"]),
            "Duet": (allow_duet, creator_info["duet_disabled"]),
            "Stitch": (allow_stitch, creator_info["stitch_disabled"]),
        }
        for label, (requested, disabled) in restricted_interactions.items():
            if requested and disabled:
                raise TikTokUploadError(
                    f"TikTok creator settings do not allow {label}.",
                    error_code="creator_interaction_restricted",
                )

        try:
            duration_seconds = float(self.duration_probe(path))
        except TikTokUploadError:
            raise
        except Exception as exc:
            raise TikTokUploadError(
                "TikTok video duration could not be verified.",
                error_code="duration_probe_failed",
            ) from exc

        if not math.isfinite(duration_seconds) or duration_seconds <= 0:
            raise TikTokUploadError(
                "TikTok video duration could not be verified.",
                error_code="duration_probe_failed",
            )
        if duration_seconds > creator_info["max_video_post_duration_sec"]:
            raise TikTokUploadError(
                "TikTok video duration exceeds this creator's posting limit.",
                error_code="video_duration_exceeded",
            )

        chunk_size, total_chunk_count = calculate_upload_plan(
            validation["file_size_bytes"],
            self.chunk_size,
        )
        post_info = {
            "title": caption,
            "privacy_level": "SELF_ONLY",
            "disable_comment": not allow_comment,
            "disable_duet": not allow_duet,
            "disable_stitch": not allow_stitch,
            "brand_content_toggle": brand_content_toggle,
            "brand_organic_toggle": brand_organic_toggle,
            "is_aigc": is_aigc,
        }

        try:
            with path.open("rb") as video_file:
                publish_id, upload_url = self._initialize_upload(
                    access_token=credentials["access_token"],
                    video_size=validation["file_size_bytes"],
                    chunk_size=chunk_size,
                    total_chunk_count=total_chunk_count,
                    initialize_url=TIKTOK_DIRECT_POST_INITIALIZE_URL,
                    post_info=post_info,
                )
                self._transfer_file(
                    video_file=video_file,
                    upload_url=upload_url,
                    publish_id=publish_id,
                    video_size=validation["file_size_bytes"],
                    chunk_size=chunk_size,
                    total_chunk_count=total_chunk_count,
                    mime_type=_VIDEO_MIME_TYPES[path.suffix.lower()],
                )
        except TikTokUploadError:
            raise
        except OSError as exc:
            raise TikTokUploadError(
                "TikTok file transfer was interrupted. Do not initialize "
                "another post until the existing publish status is checked.",
                error_code="file_transfer_interrupted",
                publish_id=locals().get("publish_id"),
            ) from exc

        return {
            "success": True,
            "platform": "tiktok",
            "video_id": None,
            "remote_publish_id": publish_id,
            "url": None,
            "published": False,
            "dry_run": False,
            "status": "processing",
            "mode": "direct_post",
            "privacy_level": "SELF_ONLY",
            "message": (
                "The private TikTok Direct Post was transferred and is "
                "processing. Publication is not yet confirmed."
            ),
            "caption_transferred": True,
            "validation": {
                **validation,
                "local_only": False,
                "video_duration_seconds": duration_seconds,
                "creator_max_duration_seconds": creator_info[
                    "max_video_post_duration_sec"
                ],
            },
        }

    @staticmethod
    def _validate_commercial_disclosures(
        *,
        commercial_content_disclosure: bool,
        brand_content_toggle: bool,
        brand_organic_toggle: bool,
    ) -> None:
        any_brand = brand_content_toggle or brand_organic_toggle
        if commercial_content_disclosure and not any_brand:
            raise TikTokUploadError(
                "Commercial content disclosure requires at least one brand type.",
                error_code="commercial_disclosure_incomplete",
            )
        if not commercial_content_disclosure and any_brand:
            raise TikTokUploadError(
                "Enable commercial content disclosure before selecting a brand type.",
                error_code="commercial_disclosure_required",
            )
        if brand_content_toggle:
            raise TikTokUploadError(
                "Branded third-party content cannot use SELF_ONLY visibility.",
                error_code="branded_content_requires_non_private_visibility",
            )

    @staticmethod
    def _normalize_username(value: Any) -> str:
        if not isinstance(value, str):
            return ""
        return value.strip().removeprefix("@").casefold()

    def _validate(
        self,
        video_path: str,
        description: str,
    ) -> tuple[Path, dict[str, Any]]:
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

        return path, {
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
        }

    def _valid_credentials(self) -> dict[str, Any]:
        try:
            service = self.oauth_service_factory()
            credentials = service.valid_credentials()
        except TikTokOAuthError as exc:
            raise TikTokUploadError(
                "TikTok authorization is unavailable. Reconnect TikTok.",
                error_code=exc.error_code or "authorization_unavailable",
            ) from exc
        except SecureStorageError as exc:
            raise TikTokUploadError(
                "TikTok credentials could not be read securely.",
                error_code="secure_storage_unavailable",
            ) from exc

        if not isinstance(credentials, dict):
            raise TikTokUploadError(
                "TikTok authorization is invalid. Reconnect TikTok.",
                error_code="invalid_stored_credentials",
            )

        return credentials

    def _credentials_with_scope(self, required_scope: str) -> dict[str, Any]:
        credentials = self._valid_credentials()
        granted_scopes = parse_scopes(credentials.get("scopes") or [])

        if required_scope not in granted_scopes:
            raise TikTokUploadError(
                f"TikTok authorization does not grant {required_scope}. "
                "Reconnect TikTok.",
                error_code="scope_not_authorized",
            )

        access_token = credentials.get("access_token")
        if not isinstance(access_token, str) or not access_token:
            raise TikTokUploadError(
                "TikTok authorization is invalid. Reconnect TikTok.",
                error_code="invalid_stored_credentials",
            )

        return credentials

    @staticmethod
    def _safe_json(response) -> dict[str, Any]:
        try:
            payload = response.json()
        except (TypeError, ValueError):
            return {}

        return payload if isinstance(payload, dict) else {}

    def fetch_creator_info(self) -> dict[str, Any]:
        """Return the latest safe creator choices for a Direct Post UI."""
        credentials = self._credentials_with_scope("video.publish")
        creator_info = self._query_creator_info(credentials["access_token"])
        expected_username = self.expected_sandbox_username
        if expected_username is None:
            expected_username = os.getenv(
                "TIKTOK_SANDBOX_ACCOUNT_USERNAME",
                "",
            )
        normalized_expected = self._normalize_username(expected_username)

        return {
            **creator_info,
            "sandbox_account_matches": (
                self._normalize_username(creator_info["creator_username"])
                == normalized_expected
                if normalized_expected
                else None
            ),
            "checkpoint_privacy_levels": ["SELF_ONLY"],
        }

    def _query_creator_info(self, access_token: str) -> dict[str, Any]:
        try:
            response = self.http_client.post(
                TIKTOK_CREATOR_INFO_URL,
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "Content-Type": "application/json; charset=UTF-8",
                },
                timeout=(10, 30),
            )
        except requests.RequestException as exc:
            raise TikTokUploadError(
                "TikTok creator information lookup was interrupted. "
                "Try again later.",
                error_code="creator_info_interrupted",
            ) from exc

        payload = self._safe_json(response)
        error = payload.get("error")
        error = error if isinstance(error, dict) else {}
        error_code = error.get("code")
        safe_error_code = (
            error_code
            if isinstance(error_code, str)
            and _SAFE_ERROR_CODE.fullmatch(error_code)
            else None
        )
        status_code = int(getattr(response, "status_code", 0))

        if status_code != 200 or safe_error_code != "ok":
            self._raise_creator_info_error(status_code, safe_error_code)

        data = payload.get("data")
        data = data if isinstance(data, dict) else {}
        creator_username = data.get("creator_username")
        creator_nickname = data.get("creator_nickname")
        privacy_options = data.get("privacy_level_options")
        comment_disabled = data.get("comment_disabled")
        duet_disabled = data.get("duet_disabled")
        stitch_disabled = data.get("stitch_disabled")
        max_duration = data.get("max_video_post_duration_sec")

        if not self._safe_creator_text(creator_username):
            self._raise_malformed_creator_info()
        if not self._safe_creator_text(creator_nickname):
            self._raise_malformed_creator_info()
        if (
            not isinstance(privacy_options, list)
            or not privacy_options
            or len(privacy_options) > len(TIKTOK_PRIVACY_LEVELS)
            or any(
                not isinstance(option, str)
                or option not in TIKTOK_PRIVACY_LEVELS
                for option in privacy_options
            )
            or len(set(privacy_options)) != len(privacy_options)
        ):
            self._raise_malformed_creator_info()
        if any(
            not isinstance(value, bool)
            for value in (
                comment_disabled,
                duet_disabled,
                stitch_disabled,
            )
        ):
            self._raise_malformed_creator_info()
        if (
            isinstance(max_duration, bool)
            or not isinstance(max_duration, int)
            or not 1 <= max_duration <= 600
        ):
            self._raise_malformed_creator_info()

        option_set = set(privacy_options)
        if (
            "FOLLOWER_OF_CREATOR" in option_set
            and "PUBLIC_TO_EVERYONE" not in option_set
        ):
            account_is_private = True
        elif (
            "PUBLIC_TO_EVERYONE" in option_set
            and "FOLLOWER_OF_CREATOR" not in option_set
        ):
            account_is_private = False
        else:
            account_is_private = None

        return {
            "creator_username": creator_username,
            "creator_nickname": creator_nickname,
            "privacy_level_options": list(privacy_options),
            "comment_disabled": comment_disabled,
            "duet_disabled": duet_disabled,
            "stitch_disabled": stitch_disabled,
            "max_video_post_duration_sec": max_duration,
            "account_is_private": account_is_private,
        }

    @staticmethod
    def _safe_creator_text(value: Any) -> bool:
        return bool(
            isinstance(value, str)
            and 1 <= len(value) <= 100
            and value == value.strip()
            and all(ord(character) >= 32 for character in value)
        )

    @staticmethod
    def _raise_malformed_creator_info() -> None:
        raise TikTokUploadError(
            "TikTok returned malformed creator information.",
            error_code="malformed_creator_info",
        )

    @staticmethod
    def _raise_creator_info_error(
        status_code: int,
        error_code: str | None,
    ) -> None:
        if error_code == "scope_not_authorized":
            message = (
                "TikTok authorization does not grant video.publish. "
                "Reconnect TikTok."
            )
        elif error_code == "access_token_invalid" or status_code == 401:
            message = "TikTok rejected the authorization. Reconnect TikTok."
        elif error_code == "rate_limit_exceeded" or status_code == 429:
            message = (
                "TikTok creator information lookup was rate limited. "
                "Try again later."
            )
        elif error_code in {
            "spam_risk_too_many_posts",
            "spam_risk_user_banned_from_posting",
            "reached_active_user_cap",
        }:
            message = "TikTok reports that this creator cannot post right now."
        elif 500 <= status_code <= 599:
            message = "TikTok creator information is temporarily unavailable."
        else:
            message = "TikTok rejected the creator information request."

        raise TikTokUploadError(
            message,
            error_code=error_code or f"http_{status_code}",
        )

    def fetch_upload_status(self, publish_id: str) -> dict[str, Any]:
        """Fetch a previously initialized TikTok upload without mutating it."""
        if not _valid_publish_id(publish_id):
            raise TikTokUploadError(
                "The stored TikTok upload identifier is invalid.",
                error_code="invalid_publish_id",
            )

        credentials = self._valid_credentials()
        granted_scopes = parse_scopes(credentials.get("scopes") or [])

        if not granted_scopes.intersection({"video.upload", "video.publish"}):
            raise TikTokUploadError(
                "TikTok authorization cannot read upload status. Reconnect TikTok.",
                error_code="scope_not_authorized",
            )

        access_token = credentials.get("access_token")
        if not isinstance(access_token, str) or not access_token:
            raise TikTokUploadError(
                "TikTok authorization is invalid. Reconnect TikTok.",
                error_code="invalid_stored_credentials",
            )

        try:
            response = self.http_client.post(
                TIKTOK_UPLOAD_STATUS_URL,
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "Content-Type": "application/json; charset=UTF-8",
                },
                json={"publish_id": publish_id},
                timeout=(10, 30),
            )
        except requests.RequestException as exc:
            raise TikTokUploadError(
                "TikTok status lookup was interrupted. Try again later.",
                error_code="status_lookup_interrupted",
            ) from exc

        payload = self._safe_json(response)
        error = payload.get("error")
        error = error if isinstance(error, dict) else {}
        error_code = error.get("code")
        safe_error_code = (
            error_code
            if isinstance(error_code, str)
            and _SAFE_ERROR_CODE.fullmatch(error_code)
            else None
        )
        status_code = int(getattr(response, "status_code", 0))

        if status_code != 200 or safe_error_code != "ok":
            self._raise_status_error(status_code, safe_error_code)

        data = payload.get("data")
        data = data if isinstance(data, dict) else {}
        provider_status = data.get("status")
        allowed_statuses = {
            "PROCESSING_UPLOAD",
            "PROCESSING_DOWNLOAD",
            "SEND_TO_USER_INBOX",
            "PUBLISH_COMPLETE",
            "FAILED",
        }

        if provider_status not in allowed_statuses:
            raise TikTokUploadError(
                "TikTok returned a malformed upload status response.",
                error_code="malformed_status_response",
            )

        uploaded_bytes = data.get("uploaded_bytes")
        if uploaded_bytes is not None and (
            isinstance(uploaded_bytes, bool)
            or not isinstance(uploaded_bytes, int)
            or uploaded_bytes < 0
        ):
            raise TikTokUploadError(
                "TikTok returned a malformed upload status response.",
                error_code="malformed_status_response",
            )

        downloaded_bytes = data.get("downloaded_bytes")
        if downloaded_bytes is not None and (
            isinstance(downloaded_bytes, bool)
            or not isinstance(downloaded_bytes, int)
            or downloaded_bytes < 0
        ):
            raise TikTokUploadError(
                "TikTok returned a malformed upload status response.",
                error_code="malformed_status_response",
            )

        fail_reason = data.get("fail_reason")
        safe_fail_reason = (
            fail_reason
            if isinstance(fail_reason, str)
            and _SAFE_ERROR_CODE.fullmatch(fail_reason)
            else None
        )

        return {
            "provider_status": provider_status,
            "uploaded_bytes": uploaded_bytes,
            "downloaded_bytes": downloaded_bytes,
            "failure_reason": safe_fail_reason,
            "manual_completion_required": (
                provider_status == "SEND_TO_USER_INBOX"
            ),
            "publication_confirmed": provider_status == "PUBLISH_COMPLETE",
        }

    @staticmethod
    def _raise_status_error(
        status_code: int,
        error_code: str | None,
    ) -> None:
        if error_code == "invalid_publish_id":
            message = "TikTok could not find this upload."
        elif error_code == "token_not_authorized_for_specified_publish_id":
            message = "TikTok authorization does not match this upload."
        elif error_code == "scope_not_authorized":
            message = (
                "TikTok authorization cannot read upload status. "
                "Reconnect TikTok."
            )
        elif error_code == "access_token_invalid" or status_code == 401:
            message = "TikTok rejected the authorization. Reconnect TikTok."
        elif error_code == "rate_limit_exceeded" or status_code == 429:
            message = "TikTok status lookup was rate limited. Try again later."
        elif 500 <= status_code <= 599:
            message = "TikTok status lookup is temporarily unavailable."
        else:
            message = "TikTok rejected the status lookup request."

        raise TikTokUploadError(
            message,
            error_code=error_code or f"http_{status_code}",
        )

    def _initialize_upload(
        self,
        *,
        access_token: str,
        video_size: int,
        chunk_size: int,
        total_chunk_count: int,
        initialize_url: str = TIKTOK_UPLOAD_INITIALIZE_URL,
        post_info: dict[str, Any] | None = None,
    ) -> tuple[str, str]:
        if initialize_url not in {
            TIKTOK_UPLOAD_INITIALIZE_URL,
            TIKTOK_DIRECT_POST_INITIALIZE_URL,
        }:
            raise TikTokUploadError(
                "TikTok upload initialization is not configured safely.",
                error_code="invalid_configuration",
            )

        request_body = {
            "source_info": {
                "source": "FILE_UPLOAD",
                "video_size": video_size,
                "chunk_size": chunk_size,
                "total_chunk_count": total_chunk_count,
            }
        }
        if post_info is not None:
            request_body["post_info"] = post_info

        try:
            response = self.http_client.post(
                initialize_url,
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "Content-Type": "application/json; charset=UTF-8",
                },
                json=request_body,
                timeout=(10, 30),
            )
        except requests.RequestException as exc:
            raise TikTokUploadError(
                "TikTok upload initialization was interrupted. Retry manually "
                "because the session outcome is unknown.",
                error_code="initialization_interrupted",
            ) from exc

        payload = self._safe_json(response)
        error = payload.get("error")
        error = error if isinstance(error, dict) else {}
        error_code = error.get("code")
        safe_error_code = (
            error_code
            if isinstance(error_code, str)
            and _SAFE_ERROR_CODE.fullmatch(error_code)
            else None
        )
        status_code = int(getattr(response, "status_code", 0))

        if status_code != 200 or safe_error_code != "ok":
            self._raise_initialization_error(status_code, safe_error_code)

        data = payload.get("data")
        data = data if isinstance(data, dict) else {}
        publish_id = data.get("publish_id")
        upload_url = data.get("upload_url")

        if not _valid_publish_id(publish_id):
            raise TikTokUploadError(
                "TikTok returned a malformed upload initialization response.",
                error_code="malformed_initialization_response",
            )

        if (
            not isinstance(upload_url, str)
            or not self._valid_upload_url(upload_url)
        ):
            raise TikTokUploadError(
                "TikTok created an upload session but returned an invalid upload "
                "destination. Check that session before starting another upload.",
                error_code="invalid_upload_destination",
                publish_id=publish_id,
            )

        return publish_id, upload_url

    @staticmethod
    def _raise_initialization_error(
        status_code: int,
        error_code: str | None,
    ) -> None:
        if error_code == "scope_not_authorized":
            message = (
                "TikTok authorization does not grant video.upload. "
                "Reconnect TikTok."
            )
        elif error_code == "access_token_invalid" or status_code == 401:
            message = "TikTok rejected the authorization. Reconnect TikTok."
        elif error_code == "privacy_level_option_mismatch":
            message = "TikTok rejected the selected privacy level."
        elif error_code == "unaudited_client_can_only_post_to_private_accounts":
            message = (
                "TikTok requires this unaudited Sandbox account to remain private."
            )
        elif error_code in {
            "spam_risk_too_many_posts",
            "spam_risk_user_banned_from_posting",
            "reached_active_user_cap",
        }:
            message = "TikTok reports that this creator cannot post right now."
        elif error_code == "rate_limit_exceeded" or status_code == 429:
            message = (
                "TikTok upload initialization was rate limited. "
                "Try again later."
            )
        elif 500 <= status_code <= 599:
            message = (
                "TikTok upload initialization failed temporarily. Retry "
                "manually because an automatic retry could create a duplicate "
                "session."
            )
        else:
            message = "TikTok rejected the upload initialization request."

        raise TikTokUploadError(
            message,
            error_code=error_code or f"http_{status_code}",
        )

    @staticmethod
    def _valid_upload_url(upload_url: str) -> bool:
        try:
            parsed = urlsplit(upload_url)
            port = parsed.port
        except ValueError:
            return False

        hostname = (parsed.hostname or "").lower()
        return bool(
            len(upload_url) <= 256
            and parsed.scheme == "https"
            and hostname.endswith(".tiktokapis.com")
            and parsed.username is None
            and parsed.password is None
            and port in {None, 443}
            and parsed.path
            and parsed.query
            and not parsed.fragment
        )

    def _transfer_file(
        self,
        *,
        video_file,
        upload_url: str,
        publish_id: str,
        video_size: int,
        chunk_size: int,
        total_chunk_count: int,
        mime_type: str,
    ) -> None:
        for index, (start, end, length) in enumerate(
            iter_chunk_ranges(
                video_size,
                chunk_size,
                total_chunk_count,
            )
        ):
            chunk = video_file.read(length)

            if len(chunk) != length:
                raise TikTokUploadError(
                    "The video changed or became unreadable during transfer. "
                    "Do not start a new upload until the existing session is "
                    "checked or has expired.",
                    error_code="local_file_changed",
                    publish_id=publish_id,
                )

            is_final = index == total_chunk_count - 1
            self._send_chunk(
                upload_url=upload_url,
                publish_id=publish_id,
                chunk=chunk,
                start=start,
                end=end,
                video_size=video_size,
                mime_type=mime_type,
                is_final=is_final,
            )

        if video_file.read(1):
            raise TikTokUploadError(
                "The video changed during transfer. Do not start a new upload "
                "until the existing session is checked or has expired.",
                error_code="local_file_changed",
                publish_id=publish_id,
            )

    def _send_chunk(
        self,
        *,
        upload_url: str,
        publish_id: str,
        chunk: bytes,
        start: int,
        end: int,
        video_size: int,
        mime_type: str,
        is_final: bool,
    ) -> None:
        headers = {
            "Content-Type": mime_type,
            "Content-Length": str(len(chunk)),
            "Content-Range": f"bytes {start}-{end}/{video_size}",
        }
        attempt = 0

        while True:
            try:
                response = self.http_client.put(
                    upload_url,
                    headers=headers,
                    data=chunk,
                    timeout=(10, 120),
                    allow_redirects=False,
                )
            except requests.RequestException as exc:
                raise TikTokUploadError(
                    "TikTok file transfer was interrupted. Do not start a new "
                    "upload until the existing session is checked or has expired.",
                    error_code="file_transfer_interrupted",
                    publish_id=publish_id,
                ) from exc

            status_code = int(getattr(response, "status_code", 0))

            if 500 <= status_code <= 599 and attempt < self.max_chunk_retries:
                self.sleeper(min(2 ** attempt, 4))
                attempt += 1
                continue

            break

        expected_status = 201 if is_final else 206

        if status_code != expected_status:
            self._raise_chunk_error(
                status_code,
                publish_id=publish_id,
                is_final=is_final,
            )

        self._validate_acknowledged_range(
            response,
            expected_uploaded_bytes=end + 1,
            video_size=video_size,
            publish_id=publish_id,
        )

    @staticmethod
    def _raise_chunk_error(
        status_code: int,
        *,
        publish_id: str,
        is_final: bool,
    ) -> None:
        if status_code == 403:
            message = "The TikTok upload session expired before transfer completed."
            error_code = "upload_session_expired"
        elif status_code == 404:
            message = "TikTok could not find the upload session."
            error_code = "upload_session_not_found"
        elif status_code == 416:
            message = "TikTok rejected the upload byte range."
            error_code = "invalid_upload_range"
        elif status_code == 429:
            message = "TikTok rate limited the video transfer. Try again later."
            error_code = "rate_limit_exceeded"
        elif 500 <= status_code <= 599:
            message = (
                "TikTok could not accept the video chunk after bounded retries. "
                "Check the existing session before starting a new upload."
            )
            error_code = "upload_server_error"
        elif is_final and status_code == 206:
            message = "TikTok did not acknowledge the final video chunk."
            error_code = "incomplete_final_chunk"
        else:
            message = "TikTok rejected a video upload chunk."
            error_code = f"http_{status_code}"

        raise TikTokUploadError(
            message,
            error_code=error_code,
            publish_id=publish_id,
        )

    @staticmethod
    def _validate_acknowledged_range(
        response,
        *,
        expected_uploaded_bytes: int,
        video_size: int,
        publish_id: str,
    ) -> None:
        response_headers = getattr(response, "headers", {}) or {}
        content_range = None

        for name, value in response_headers.items():
            if str(name).lower() == "content-range":
                content_range = str(value).strip()
                break

        if content_range is None:
            return

        match = _CONTENT_RANGE.fullmatch(content_range)

        if (
            match is None
            or int(match.group(1)) != expected_uploaded_bytes
            or int(match.group(2)) != video_size
        ):
            raise TikTokUploadError(
                "TikTok returned an invalid upload range acknowledgement.",
                error_code="invalid_range_acknowledgement",
                publish_id=publish_id,
            )
