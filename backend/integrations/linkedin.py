import json
import os
import time
from pathlib import Path
from urllib.parse import quote

import requests
from dotenv import load_dotenv


BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")

LINKEDIN_TOKEN_FILE = BASE_DIR / "linkedin_token.json"

API_BASE_URL = "https://api.linkedin.com/rest"
MAX_VIDEO_SIZE_BYTES = 5 * 1024 * 1024 * 1024
VIDEO_PROCESSING_TIMEOUT_SECONDS = 300
VIDEO_POLL_INTERVAL_SECONDS = 3


class LinkedInPublisher:
    def __init__(self):
        self.api_version = os.getenv(
            "LINKEDIN_API_VERSION",
            "202609",
        )
        self.organization_id = os.getenv(
            "LINKEDIN_ORGANIZATION_ID",
            "",
        ).strip()

    def _load_access_token(self) -> str:
        if not LINKEDIN_TOKEN_FILE.exists():
            raise RuntimeError(
                "LinkedIn is not connected. Complete LinkedIn OAuth first."
            )

        try:
            token_data = json.loads(
                LINKEDIN_TOKEN_FILE.read_text(encoding="utf-8")
            )
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError(
                f"Could not read LinkedIn token file: {exc}"
            ) from exc

        access_token = token_data.get("access_token")

        if not access_token:
            raise RuntimeError(
                "linkedin_token.json does not contain an access_token."
            )

        expires_at = token_data.get("expires_at")

        if expires_at is not None:
            try:
                if time.time() >= float(expires_at):
                    raise RuntimeError(
                        "LinkedIn access token has expired. Reconnect LinkedIn."
                    )
            except (TypeError, ValueError):
                pass

        return access_token

    def _organization_urn(self) -> str:
        if not self.organization_id:
            raise RuntimeError(
                "LINKEDIN_ORGANIZATION_ID is missing from backend/.env."
            )

        return f"urn:li:organization:{self.organization_id}"

    def _api_headers(
        self,
        access_token: str,
        *,
        content_type: bool = True,
    ) -> dict[str, str]:
        headers = {
            "Authorization": f"Bearer {access_token}",
            "Linkedin-Version": self.api_version,
            "X-Restli-Protocol-Version": "2.0.0",
        }

        if content_type:
            headers["Content-Type"] = "application/json"

        return headers

    @staticmethod
    def _raise_for_linkedin_error(
        response: requests.Response,
        context: str,
    ):
        if response.ok:
            return

        try:
            data = response.json()
        except ValueError:
            data = None

        detail = None

        if isinstance(data, dict):
            detail = (
                data.get("message")
                or data.get("error_description")
                or data.get("error")
            )

        if not detail:
            detail = response.text[:1000] or (
                f"HTTP {response.status_code}"
            )

        raise RuntimeError(
            f"{context}: {detail}"
        )

    @staticmethod
    def _validate_video(video_path: str) -> Path:
        path = Path(video_path)

        if not path.exists():
            raise FileNotFoundError(
                f"LinkedIn video file not found: {video_path}"
            )

        if not path.is_file():
            raise ValueError(
                f"LinkedIn video path is not a file: {video_path}"
            )

        file_size = path.stat().st_size

        if file_size <= 0:
            raise ValueError("LinkedIn video file is empty.")

        if file_size > MAX_VIDEO_SIZE_BYTES:
            raise ValueError(
                "LinkedIn video exceeds the Videos API 5 GB maximum."
            )

        return path

    def _initialize_upload(
        self,
        *,
        access_token: str,
        video_path: Path,
    ) -> dict:
        response = requests.post(
            f"{API_BASE_URL}/videos?action=initializeUpload",
            headers=self._api_headers(access_token),
            json={
                "initializeUploadRequest": {
                    "owner": self._organization_urn(),
                    "fileSizeBytes": video_path.stat().st_size,
                    "uploadCaptions": False,
                    "uploadThumbnail": False,
                }
            },
            timeout=30,
        )

        self._raise_for_linkedin_error(
            response,
            "LinkedIn initialize video upload failed",
        )

        data = response.json()
        value = data.get("value") or {}

        video_urn = value.get("video")
        upload_instructions = value.get("uploadInstructions") or []

        if not video_urn:
            raise RuntimeError(
                "LinkedIn initialize upload did not return a video URN."
            )

        if not upload_instructions:
            raise RuntimeError(
                "LinkedIn initialize upload returned no upload instructions."
            )

        return value

    def _upload_parts(
        self,
        *,
        video_path: Path,
        upload_instructions: list[dict],
    ) -> list[str]:
        uploaded_part_ids: list[str] = []

        with video_path.open("rb") as file_handle:
            for instruction in upload_instructions:
                upload_url = instruction.get("uploadUrl")
                first_byte = instruction.get("firstByte")
                last_byte = instruction.get("lastByte")

                if (
                    not upload_url
                    or first_byte is None
                    or last_byte is None
                ):
                    raise RuntimeError(
                        "LinkedIn returned an invalid upload instruction."
                    )

                first_byte = int(first_byte)
                last_byte = int(last_byte)
                chunk_size = last_byte - first_byte + 1

                if chunk_size <= 0:
                    raise RuntimeError(
                        "LinkedIn returned an invalid upload byte range."
                    )

                file_handle.seek(first_byte)
                chunk = file_handle.read(chunk_size)

                if len(chunk) != chunk_size:
                    raise RuntimeError(
                        "Could not read the complete LinkedIn upload part "
                        f"{first_byte}-{last_byte} from the local file."
                    )

                response = requests.put(
                    upload_url,
                    headers={
                        "Content-Type": "application/octet-stream",
                    },
                    data=chunk,
                    timeout=180,
                )

                self._raise_for_linkedin_error(
                    response,
                    "LinkedIn video part upload failed",
                )

                etag = response.headers.get("ETag")

                if not etag:
                    raise RuntimeError(
                        "LinkedIn upload response did not include an ETag."
                    )

                uploaded_part_ids.append(
                    etag.strip('"')
                )

        return uploaded_part_ids

    def _finalize_upload(
        self,
        *,
        access_token: str,
        video_urn: str,
        upload_token: str,
        uploaded_part_ids: list[str],
    ):
        response = requests.post(
            f"{API_BASE_URL}/videos?action=finalizeUpload",
            headers=self._api_headers(access_token),
            json={
                "finalizeUploadRequest": {
                    "video": video_urn,
                    "uploadToken": upload_token,
                    "uploadedPartIds": uploaded_part_ids,
                }
            },
            timeout=30,
        )

        self._raise_for_linkedin_error(
            response,
            "LinkedIn finalize video upload failed",
        )

    def _wait_until_available(
        self,
        *,
        access_token: str,
        video_urn: str,
    ) -> dict:
        encoded_video_urn = quote(
            video_urn,
            safe="",
        )
        deadline = time.monotonic() + VIDEO_PROCESSING_TIMEOUT_SECONDS
        last_data = None

        while time.monotonic() < deadline:
            response = requests.get(
                f"{API_BASE_URL}/videos/{encoded_video_urn}",
                headers=self._api_headers(access_token),
                timeout=30,
            )

            self._raise_for_linkedin_error(
                response,
                "Could not retrieve LinkedIn video processing status",
            )

            data = response.json()
            last_data = data
            status = data.get("status")

            if status == "AVAILABLE":
                return data

            if status == "PROCESSING_FAILED":
                reason = (
                    data.get("processingFailureReason")
                    or "Unknown processing failure."
                )
                raise RuntimeError(
                    "LinkedIn video processing failed: "
                    f"{reason}"
                )

            time.sleep(VIDEO_POLL_INTERVAL_SECONDS)

        status = (
            last_data.get("status")
            if isinstance(last_data, dict)
            else None
        )

        raise TimeoutError(
            "LinkedIn video did not become AVAILABLE within "
            f"{VIDEO_PROCESSING_TIMEOUT_SECONDS} seconds. "
            f"Last status: {status or 'unknown'}."
        )

    def _create_post(
        self,
        *,
        access_token: str,
        video_urn: str,
        title: str,
        description: str,
    ) -> str:
        commentary = (description or title or "").strip()

        payload = {
            "author": self._organization_urn(),
            "commentary": commentary,
            "visibility": "PUBLIC",
            "distribution": {
                "feedDistribution": "MAIN_FEED",
                "targetEntities": [],
                "thirdPartyDistributionChannels": [],
            },
            "content": {
                "media": {
                    "title": (title or video_urn).strip(),
                    "id": video_urn,
                }
            },
            "lifecycleState": "PUBLISHED",
            "isReshareDisabledByAuthor": False,
        }

        response = requests.post(
            f"{API_BASE_URL}/posts",
            headers=self._api_headers(access_token),
            json=payload,
            timeout=30,
        )

        self._raise_for_linkedin_error(
            response,
            "LinkedIn post creation failed",
        )

        post_urn = response.headers.get("x-restli-id")

        if not post_urn:
            raise RuntimeError(
                "LinkedIn created the post but did not return x-restli-id."
            )

        return post_urn

    def publish(
        self,
        video_path: str,
        title: str,
        description: str = "",
        privacy: str = "public",
        publish: bool = False,
    ):
        video_file = self._validate_video(video_path)

        # Safety default: validate the local input without touching LinkedIn.
        if not publish:
            return {
                "platform": "linkedin",
                "video_id": None,
                "url": None,
                "status": "ready",
                "dry_run": True,
                "message": (
                    "LinkedIn dry run completed. No video was uploaded "
                    "and no LinkedIn post was created."
                ),
                "local_file": str(video_file),
            }

        access_token = self._load_access_token()

        initialized = self._initialize_upload(
            access_token=access_token,
            video_path=video_file,
        )

        video_urn = initialized["video"]
        upload_instructions = initialized["uploadInstructions"]
        upload_token = initialized.get("uploadToken") or ""

        uploaded_part_ids = self._upload_parts(
            video_path=video_file,
            upload_instructions=upload_instructions,
        )

        self._finalize_upload(
            access_token=access_token,
            video_urn=video_urn,
            upload_token=upload_token,
            uploaded_part_ids=uploaded_part_ids,
        )

        self._wait_until_available(
            access_token=access_token,
            video_urn=video_urn,
        )

        post_urn = self._create_post(
            access_token=access_token,
            video_urn=video_urn,
            title=title,
            description=description,
        )

        return {
            "platform": "linkedin",
            # Keep the existing PublishingService/posts.py contract:
            # video_id is used as the generic remote_post_id field.
            "video_id": post_urn,
            "linkedin_video_urn": video_urn,
            "url": f"https://www.linkedin.com/feed/update/{post_urn}/",
            "status": "published",
            "dry_run": False,
        }
