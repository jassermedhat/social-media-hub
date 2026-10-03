from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse, StreamingResponse

from services.google_drive_media import GoogleDriveMediaStorage
from services.temp_media import (
    resolve_temp_media,
    resolve_temp_media_record,
)


router = APIRouter(
    prefix="/media",
    tags=["Temporary Media"],
)


@router.get("/{token}")
def get_temp_media(token: str):
    record = resolve_temp_media_record(token)

    if record is None:
        raise HTTPException(
            status_code=404,
            detail="Temporary media is unavailable or expired.",
        )

    drive_file_id = record.get("drive_file_id")

    if drive_file_id:
        try:
            storage = GoogleDriveMediaStorage()

            stream = storage.stream_file(
                drive_file_id
            )

            return StreamingResponse(
                stream,
                media_type=(
                    record.get("mime_type")
                    or "application/octet-stream"
                ),
                headers={
                    "Cache-Control": "no-store",
                    "X-Content-Type-Options": "nosniff",
                },
            )

        except Exception as exc:
            raise HTTPException(
                status_code=502,
                detail=(
                    "Could not retrieve temporary media "
                    f"from Google Drive: {exc}"
                ),
            )

    path = resolve_temp_media(token)

    if path is None:
        raise HTTPException(
            status_code=404,
            detail="Temporary media is unavailable or expired.",
        )

    return FileResponse(
        path=str(path),
        headers={
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )