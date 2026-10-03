from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from services.publishing import PublishingService
from database.db import (
    create_post,
    create_publish_result,
)

router = APIRouter(
    prefix="/youtube",
    tags=["YouTube"],
)


class YouTubeUploadRequest(BaseModel):
    video_path: str
    title: str
    description: str = ""
    privacy: str = "private"


@router.post("/upload")
def upload_video(data: YouTubeUploadRequest):
    post_id = create_post(
        video_path=data.video_path,
        title=data.title,
        description=data.description,
    )

    try:
        service = PublishingService()

        result = service.publish(
            platform="youtube",
            video_path=data.video_path,
            title=data.title,
            description=data.description,
            privacy=data.privacy,
        )

        create_publish_result(
            post_id=post_id,
            platform="youtube",
            status="published",
            remote_post_id=result["video_id"],
            remote_url=result["url"],
        )

        result["post_id"] = post_id

        return result

    except Exception as exc:
        create_publish_result(
            post_id=post_id,
            platform="youtube",
            status="failed",
            error=str(exc),
        )

        raise HTTPException(
            status_code=500,
            detail=str(exc),
        )