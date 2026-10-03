from pathlib import Path

from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload


BASE_DIR = Path(__file__).resolve().parent.parent

TOKEN_FILE = BASE_DIR / "youtube_token.json"

SCOPES = [
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube.readonly",
]


class YouTubePublisher:
    def __init__(self):
        self.credentials = self._load_credentials()

        self.youtube = build(
            "youtube",
            "v3",
            credentials=self.credentials,
        )

    def _load_credentials(self):
        if not TOKEN_FILE.exists():
            raise RuntimeError(
                "YouTube is not connected. OAuth authorization is required."
            )

        credentials = Credentials.from_authorized_user_file(
            str(TOKEN_FILE),
            SCOPES,
        )

        if credentials.expired and credentials.refresh_token:
            credentials.refresh(Request())

            TOKEN_FILE.write_text(
                credentials.to_json(),
                encoding="utf-8",
            )

        if not credentials.valid:
            raise RuntimeError(
                "YouTube credentials are invalid."
            )

        return credentials

    def publish(
        self,
        video_path: str,
        title: str,
        description: str = "",
        privacy: str = "private",
    ):
        path = Path(video_path)

        if not path.exists():
            raise FileNotFoundError(
                f"Video does not exist: {video_path}"
            )

        body = {
            "snippet": {
                "title": title,
                "description": description,
                "categoryId": "22",
            },
            "status": {
                "privacyStatus": privacy,
            },
        }

        media = MediaFileUpload(
            str(path),
            mimetype="video/*",
            resumable=True,
        )

        request = self.youtube.videos().insert(
            part="snippet,status",
            body=body,
            media_body=media,
        )

        response = None

        while response is None:
            status, response = request.next_chunk()

            if status:
                print(
                    f"YouTube upload: "
                    f"{int(status.progress() * 100)}%"
                )

        video_id = response["id"]

        return {
            "success": True,
            "platform": "youtube",
            "video_id": video_id,
            "url": f"https://www.youtube.com/watch?v={video_id}",
            "privacy": privacy,
        }