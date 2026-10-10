from typing import Literal
from services.google_drive_media import GoogleDriveMediaStorage
from fastapi import APIRouter
from pydantic import BaseModel

from database.db import (
    create_post,
    create_publish_result,
)
from services.publishing import PublishingService


router = APIRouter(
    prefix="/posts",
    tags=["Posts"],
)


class PublishPostRequest(BaseModel):
    video_path: str
    title: str
    caption: str = ""
    platforms: list[str]

    youtube_privacy: Literal[
        "private",
        "unlisted",
        "public",
    ] = "private"

    # Safety defaults: external platforms do not publish unless explicitly enabled.
    instagram_publish: bool = False
    facebook_publish: bool = False
    linkedin_publish: bool = False
    tiktok_publish: bool = False


@router.post("/publish")
def publish_post(data: PublishPostRequest):
    # Create ONE local post for the whole publishing operation.
    post_id = create_post(
        video_path=data.video_path,
        title=data.title,
        description=data.caption,
    )

    service = PublishingService()

    results = {}

    # Avoid accidentally publishing twice to the same platform.
    platforms = list(dict.fromkeys(
        platform.lower()
        for platform in data.platforms
    ))

    media_asset = None
    drive_storage = None
    drive_prepare_error = None

    # Meta publishers share one Drive upload for this operation.
    drive_platforms = {"instagram", "facebook"}

    if any(
        platform in drive_platforms
        for platform in platforms
    ):
        try:
            drive_storage = GoogleDriveMediaStorage()
            media_asset = drive_storage.prepare(
                data.video_path
            )
        except Exception as exc:
            drive_prepare_error = exc

    for platform in platforms:
        try:
            if (
                platform in drive_platforms
                and drive_prepare_error is not None
            ):
                raise RuntimeError(
                    "Could not prepare shared Google Drive media: "
                    f"{drive_prepare_error}"
                )

            if platform == "tiktok" and data.tiktok_publish:
                raise ValueError(
                    "Remote TikTok publishing is not implemented. "
                    "Set tiktok_publish=false to run local validation only."
                )

            result = service.publish(
                platform=platform,
                video_path=data.video_path,
                title=data.title,
                description=data.caption,
                privacy=data.youtube_privacy,
                instagram_publish=data.instagram_publish,
                facebook_publish=data.facebook_publish,
                linkedin_publish=data.linkedin_publish,
                media_asset=media_asset,
                tiktok_publish=data.tiktok_publish,
            )

            is_dry_run = bool(result.get("dry_run"))
            is_published = result.get("published")
            if is_published is None:
                is_published = not is_dry_run
            else:
                is_published = bool(is_published)

            result_status = result.get("status") or (
                "ready" if is_dry_run else "published"
            )

            create_publish_result(
                post_id=post_id,
                platform=platform,
                status=result_status,
                remote_post_id=result.get("video_id"),
                remote_url=result.get("url"),
            )

            results[platform] = {
                "success": True,
                "published": is_published,
                "dry_run": is_dry_run,
                "status": result_status,
                "remote_post_id": result.get("video_id"),
                "url": result.get("url"),
            }

            if platform == "tiktok":
                results[platform]["message"] = result.get("message")
                results[platform]["validation"] = result.get("validation")

        except Exception as exc:
            create_publish_result(
                post_id=post_id,
                platform=platform,
                status="failed",
                error=str(exc),
            )

            results[platform] = {
                "success": False,
                "error": str(exc),
            }
    
    
    media_cleanup = {
        "drive_file_deleted": False,
    }

    if (
        media_asset is not None
        and drive_storage is not None
        and media_asset.drive_file_id
    ):
        all_succeeded = (
            bool(results)
            and all(
                result.get("success") is True
                for result in results.values()
            )
        )

        if all_succeeded:
            try:
                drive_storage.delete_file(
                    media_asset.drive_file_id
                )

                media_cleanup = {
                    "drive_file_deleted": True,
                }

            except Exception as exc:
                media_cleanup = {
                    "drive_file_deleted": False,
                    "error": str(exc),
                }

        else:
            media_cleanup = {
                "drive_file_deleted": False,
                "reason": (
                    "At least one platform failed. "
                    "Drive media retained for retry."
                ),
            }


    return {
        "post_id": post_id,
        "results": results,
        "media_cleanup": media_cleanup,
    }
