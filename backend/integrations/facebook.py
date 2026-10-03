import json
import os
import tempfile
from pathlib import Path

import requests
from dotenv import load_dotenv
from services.google_drive_media import (
    GoogleDriveMediaStorage,
    MediaAsset,
)


BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")

META_TOKEN_FILE = BASE_DIR / "meta_token.json"


class FacebookPublisher:
    def __init__(self):
        self.token_data = self._load_token_data()

        self.graph_version = self.token_data.get(
            "graph_api_version",
            os.getenv("META_GRAPH_VERSION", "v26.0"),
        )
        self.page_access_token = self.token_data.get("page_access_token")
        self.page_id = self.token_data.get("facebook_page_id")

        if not self.page_access_token:
            raise RuntimeError(
                "Meta Page access token is missing. "
                "Reconnect Meta through /accounts/meta/connect."
            )

        if not self.page_id:
            raise RuntimeError(
                "Facebook Page ID is missing. "
                "Reconnect Meta through /accounts/meta/connect."
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

    def _start_reel_upload(self):
        response = requests.post(
            self._graph_url("/me/video_reels"),
            params={
                "access_token": self.page_access_token,
                "upload_phase": "start",
            },
            timeout=60,
        )

        data = self._raise_for_meta_error(
            response,
            "Could not create Facebook Reel upload session",
        )

        video_id = data.get("video_id")
        upload_url = data.get("upload_url")

        if not video_id or not upload_url:
            raise RuntimeError(
                "Meta did not return both video_id and upload_url "
                "for the Facebook Reel upload session."
            )

        return str(video_id), str(upload_url)

    def _upload_local_reel(
        self,
        upload_url: str,
        video_path: Path,
    ):
        file_size = video_path.stat().st_size

        with video_path.open("rb") as handle:
            response = requests.post(
                upload_url,
                headers={
                    "Authorization": (
                        f"OAuth {self.page_access_token}"
                    ),
                    "offset": "0",
                    "file_size": str(file_size),
                    "Content-Type": "application/octet-stream",
                },
                data=handle,
                timeout=900,
            )

        data = self._raise_for_meta_error(
            response,
            "Could not upload the Facebook Reel video",
        )

        if not data.get("success"):
            raise RuntimeError(
                "Meta did not confirm the Facebook Reel upload."
            )

    def _finish_reel(
        self,
        video_id: str,
        title: str,
        description: str,
        publish: bool,
    ):
        video_state = "PUBLISHED" if publish else "DRAFT"

        response = requests.post(
            self._graph_url("/me/video_reels"),
            params={
                "access_token": self.page_access_token,
                "video_id": video_id,
                "upload_phase": "finish",
                "video_state": video_state,
                "title": title,
                "description": description,
            },
            timeout=60,
        )

        data = self._raise_for_meta_error(
            response,
            (
                "Could not publish Facebook Reel"
                if publish
                else "Could not save Facebook Reel as a draft"
            ),
        )

        if not data.get("success"):
            raise RuntimeError(
                "Meta did not confirm the Facebook Reel finish step."
            )

        return video_state

    def _get_status(self, video_id: str):
        # Best effort. Publishing/draft creation has already succeeded
        # when this is called, so status lookup should not reverse that.
        try:
            response = requests.get(
                self._graph_url(f"/{video_id}"),
                params={
                    "fields": "status",
                    "access_token": self.page_access_token,
                },
                timeout=30,
            )

            return self._raise_for_meta_error(
                response,
                "Could not retrieve Facebook Reel status",
            ).get("status")
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
        # Facebook has its own DRAFT/PUBLISHED state. YouTube privacy
        # does not apply here.
        del privacy

        path = Path(video_path)
        temporary_drive_path = None

        try:
            if media_asset is not None and media_asset.drive_file_id:
                suffix = Path(media_asset.filename).suffix

                with tempfile.NamedTemporaryFile(
                    mode="wb",
                    suffix=suffix,
                    delete=False,
                ) as temporary_file:
                    temporary_drive_path = Path(temporary_file.name)

                    for chunk in GoogleDriveMediaStorage().stream_file(
                        media_asset.drive_file_id
                    ):
                        temporary_file.write(chunk)

                path = temporary_drive_path
            elif not path.exists() or not path.is_file():
                raise FileNotFoundError(
                    f"Video file not found: {video_path}"
                )

            video_id, upload_url = self._start_reel_upload()

            self._upload_local_reel(
                upload_url=upload_url,
                video_path=path,
            )

            video_state = self._finish_reel(
                video_id=video_id,
                title=title,
                description=description,
                publish=publish,
            )

            current_status = self._get_status(video_id)

            return {
                "video_id": video_id,
                # Meta's Reels Publishing response does not return a
                # canonical public permalink in this flow, so keep it null.
                "url": None,
                "published": publish,
                "dry_run": not publish,
                "status": "published" if publish else "draft",
                "video_state": video_state,
                "facebook_status": current_status,
            }
        finally:
            if temporary_drive_path is not None:
                try:
                    temporary_drive_path.unlink()
                except OSError:
                    pass
