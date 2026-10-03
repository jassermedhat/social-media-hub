from pathlib import Path
import requests
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
from dataclasses import dataclass
from typing import Optional

BASE_DIR = Path(__file__).resolve().parent.parent

DRIVE_TOKEN_FILE = BASE_DIR / "drive_token.json"

DRIVE_SCOPES = [
    "https://www.googleapis.com/auth/drive.file",
]


@dataclass
class MediaAsset:
    local_path: Optional[str]
    drive_file_id: Optional[str]
    media_url: Optional[str]
    filename: str
    mime_type: Optional[str]



class GoogleDriveMediaStorage:
    def _get_credentials(self) -> Credentials:
        if not DRIVE_TOKEN_FILE.exists():
            raise RuntimeError(
                "Google Drive is not connected. "
                "Connect it through /accounts/drive/connect first."
            )

        credentials = Credentials.from_authorized_user_file(
            str(DRIVE_TOKEN_FILE),
            DRIVE_SCOPES,
        )

        if credentials.expired and credentials.refresh_token:
            credentials.refresh(Request())

            DRIVE_TOKEN_FILE.write_text(
                credentials.to_json(),
                encoding="utf-8",
            )

        if not credentials.valid:
            raise RuntimeError(
                "Google Drive credentials are invalid. "
                "Reconnect Google Drive."
            )

        return credentials

    def _get_service(self):
        credentials = self._get_credentials()

        return build(
            "drive",
            "v3",
            credentials=credentials,
        )

    def upload_file(self, local_path: str) -> dict:
        path = Path(local_path)

        if not path.exists():
            raise FileNotFoundError(
                f"Media file does not exist: {local_path}"
            )

        service = self._get_service()

        media = MediaFileUpload(
            str(path),
            resumable=True,
        )

        result = (
            service.files()
            .create(
                body={
                    "name": path.name,
                },
                media_body=media,
                fields="id,name,mimeType,size",
            )
            .execute()
        )

        return result

    def delete_file(self, file_id: str) -> None:
        service = self._get_service()

        service.files().delete(
            fileId=file_id,
        ).execute()


    def prepare(self, local_path: str) -> MediaAsset:
        uploaded = self.upload_file(local_path)

        path = Path(local_path)

        return MediaAsset(
            local_path=str(path),
            drive_file_id=uploaded["id"],
            media_url=None,
            filename=uploaded.get("name", path.name),
            mime_type=uploaded.get("mimeType"),
        )
    
    def stream_file(
        self,
        file_id: str,
        chunk_size: int = 1024 * 1024,):
        credentials = self._get_credentials()

        response = requests.get(
            f"https://www.googleapis.com/drive/v3/files/{file_id}",
            params={
                "alt": "media",
            },
            headers={
                "Authorization": f"Bearer {credentials.token}",
            },
            stream=True,
            timeout=(30, 300),
        )

        response.raise_for_status()

        def iterator():
            try:
                for chunk in response.iter_content(
                    chunk_size=chunk_size
                ):
                    if chunk:
                        yield chunk
            finally:
                response.close()

        return iterator()