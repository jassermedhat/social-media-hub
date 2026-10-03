import json
import os
import time
from pathlib import Path

import requests
from dotenv import load_dotenv
from services.google_drive_media import MediaAsset
from services.temp_media import (
    register_temp_media,
    release_temp_media,
)


BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")

META_TOKEN_FILE = BASE_DIR / "meta_token.json"


class InstagramPublisher:
    def __init__(self):
        self.token_data = self._load_token_data()

        self.graph_version = self.token_data.get(
            "graph_api_version",
            os.getenv("META_GRAPH_VERSION", "v26.0"),
        )
        self.page_access_token = self.token_data.get("page_access_token")
        self.instagram_account_id = self.token_data.get(
            "instagram_business_account_id"
        )

        self.media_base_url = (
            os.getenv("META_MEDIA_BASE_URL", "")
            .strip()
            .rstrip("/")
        )

        if not self.page_access_token:
            raise RuntimeError(
                "Meta Page access token is missing. "
                "Reconnect Meta through /accounts/meta/connect."
            )

        if not self.instagram_account_id:
            raise RuntimeError(
                "Instagram Business account ID is missing. "
                "Reconnect Meta through /accounts/meta/connect."
            )

        if not self.media_base_url:
            raise RuntimeError(
                "META_MEDIA_BASE_URL is not configured. "
                "Instagram must be able to fetch the video from a "
                "public HTTPS URL."
            )

        lowered = self.media_base_url.lower()

        if not lowered.startswith("https://"):
            raise RuntimeError(
                "META_MEDIA_BASE_URL must be a public HTTPS URL."
            )

        if "localhost" in lowered or "127.0.0.1" in lowered:
            raise RuntimeError(
                "META_MEDIA_BASE_URL cannot point to localhost. "
                "Meta's servers must be able to reach it."
            )

    def _load_token_data(self):
        if not META_TOKEN_FILE.exists():
            raise RuntimeError(
                "Meta is not connected. "
                "Open /accounts/meta/connect first."
            )

        try:
            return json.loads(
                META_TOKEN_FILE.read_text(encoding="utf-8")
            )
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError(
                "Could not read backend/meta_token.json. "
                "Reconnect the Meta account."
            ) from exc

    def _graph_url(self, path: str) -> str:
        return (
            f"https://graph.facebook.com/"
            f"{self.graph_version}/{path.lstrip('/')}"
        )

    @staticmethod
    def _raise_for_meta_error(response, context: str):
        try:
            data = response.json()
        except ValueError:
            data = None

        if response.ok and not (
            isinstance(data, dict) and data.get("error")
        ):
            return data

        detail = None

        if isinstance(data, dict):
            error = data.get("error")
            if isinstance(error, dict):
                detail = error.get("message")

        if not detail:
            detail = response.text[:500] or (
                f"HTTP {response.status_code}"
            )

        raise RuntimeError(f"{context}: {detail}")

    def _create_reel_container(
        self,
        video_url: str,
        caption: str,
    ) -> str:
        response = requests.post(
            self._graph_url(
                f"/{self.instagram_account_id}/media"
            ),
            params={
                "media_type": "REELS",
                "video_url": video_url,
                "caption": caption,
                # Normal publishing behavior: show in Feed and Reels.
                "share_to_feed": "true",
                "access_token": self.page_access_token,
            },
            timeout=60,
        )

        data = self._raise_for_meta_error(
            response,
            "Could not create Instagram Reel container",
        )

        container_id = data.get("id")

        if not container_id:
            raise RuntimeError(
                "Meta did not return an Instagram container ID."
            )

        return str(container_id)

    def _wait_until_container_ready(
        self,
        container_id: str,
        timeout_seconds: int = 300,
        poll_seconds: int = 5,
    ):
        deadline = time.monotonic() + timeout_seconds
        last_status = None

        while time.monotonic() < deadline:
            response = requests.get(
                self._graph_url(f"/{container_id}"),
                params={
                    "fields": "status_code,status",
                    "access_token": self.page_access_token,
                },
                timeout=30,
            )

            data = self._raise_for_meta_error(
                response,
                "Could not check Instagram container status",
            )

            status_code = str(
                data.get("status_code") or ""
            ).upper()
            status_text = data.get("status") or ""
            last_status = {
                "status_code": status_code,
                "status": status_text,
            }

            if status_code == "FINISHED":
                return

            if status_code in {"ERROR", "EXPIRED"}:
                raise RuntimeError(
                    "Instagram media processing failed: "
                    f"{status_code}"
                    + (
                        f" - {status_text}"
                        if status_text
                        else ""
                    )
                )

            time.sleep(poll_seconds)

        raise RuntimeError(
            "Timed out waiting for Instagram to process the video. "
            f"Last status: {last_status}"
        )

    def _publish_container(self, container_id: str) -> str:
        response = requests.post(
            self._graph_url(
                f"/{self.instagram_account_id}/media_publish"
            ),
            params={
                "creation_id": container_id,
                "access_token": self.page_access_token,
            },
            timeout=60,
        )

        data = self._raise_for_meta_error(
            response,
            "Could not publish Instagram Reel",
        )

        media_id = data.get("id")

        if not media_id:
            raise RuntimeError(
                "Meta did not return the published Instagram media ID."
            )

        return str(media_id)

    def _get_permalink(self, media_id: str):
        # Best effort: publishing is still considered successful if
        # permalink lookup fails after Meta has already published the Reel.
        try:
            response = requests.get(
                self._graph_url(f"/{media_id}"),
                params={
                    "fields": "permalink",
                    "access_token": self.page_access_token,
                },
                timeout=30,
            )

            data = self._raise_for_meta_error(
                response,
                "Could not retrieve Instagram permalink",
            )

            return data.get("permalink")
        except Exception:
            return None

    def publish(
        self,
        video_path: str,
        title: str,
        description: str = "",
        privacy: str = "private",
        publish: bool = False,
        media_asset: MediaAsset | None = None,
    ):
        # Instagram ignores YouTube-specific title/privacy values.
        del title, privacy

        path = Path(video_path)

        if not path.exists() or not path.is_file():
            raise FileNotFoundError(
                f"Video file not found: {video_path}"
            )

        if media_asset and media_asset.drive_file_id:
            media_token = register_temp_media(
                video_path=path,
                ttl_seconds=900,
                drive_file_id=media_asset.drive_file_id,
                filename=media_asset.filename,
                mime_type=media_asset.mime_type,
            )
        else:
            media_token = register_temp_media(
                video_path=path,
                ttl_seconds=900,
            )

        public_video_url = (
            f"{self.media_base_url}/media/{media_token}"
        )

        try:
            container_id = self._create_reel_container(
                video_url=public_video_url,
                caption=description,
            )

            self._wait_until_container_ready(container_id)

            # Safety default: reaching FINISHED proves Meta accepted and
            # processed the media, but nothing is published unless the caller
            # explicitly opts in with publish=True.
            if not publish:
                return {
                    "video_id": container_id,
                    "url": None,
                    "container_id": container_id,
                    "published": False,
                    "dry_run": True,
                    "status": "ready",
                }

            media_id = self._publish_container(container_id)
            permalink = self._get_permalink(media_id)

            # Return "video_id" to match the existing generic
            # PublishingService/posts.py result contract.
            return {
                "video_id": media_id,
                "url": permalink,
                "container_id": container_id,
                "published": True,
                "dry_run": False,
                "status": "published",
            }

        finally:
            release_temp_media(media_token)
