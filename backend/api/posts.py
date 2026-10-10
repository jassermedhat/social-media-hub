import ipaddress
from typing import Literal
from urllib.parse import urlsplit

from services.google_drive_media import GoogleDriveMediaStorage
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from database.db import (
    create_post,
    create_publish_result,
    get_publish_result,
)
from integrations.tiktok import TikTokPublisher, TikTokUploadError
from services.publishing import PublishingService
from services.tiktok_reconciliation import (
    TikTokReconciliationError,
    reconcile_tiktok_post,
)


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
    tiktok_mode: Literal[
        "validation",
        "manual_upload",
        "direct_post",
    ] = "validation"
    tiktok_user_consent: bool = False
    tiktok_privacy_level: Literal[
        "PUBLIC_TO_EVERYONE",
        "MUTUAL_FOLLOW_FRIENDS",
        "FOLLOWER_OF_CREATOR",
        "SELF_ONLY",
    ] | None = None
    tiktok_allow_comment: bool = False
    tiktok_allow_duet: bool = False
    tiktok_allow_stitch: bool = False
    tiktok_commercial_content_disclosure: bool = False
    tiktok_brand_content_toggle: bool = False
    tiktok_brand_organic_toggle: bool = False
    tiktok_is_aigc: bool = False


_TIKTOK_STATUS_ERROR_HTTP_CODES = {
    "invalid_publish_id": 404,
    "token_not_authorized_for_specified_publish_id": 403,
    "scope_not_authorized": 403,
    "access_token_invalid": 401,
    "authorization_unavailable": 401,
    "not_connected": 401,
    "refresh_token_expired": 401,
    "missing_required_scopes": 403,
    "invalid_stored_credentials": 401,
    "rate_limit_exceeded": 429,
}


def _is_loopback_hostname(hostname: str | None) -> bool:
    if not hostname:
        return False
    normalized = hostname.strip().lower().rstrip(".")
    if normalized == "localhost":
        return True
    try:
        return ipaddress.ip_address(normalized).is_loopback
    except ValueError:
        return False


def _require_local_reconciliation_request(request: Request) -> None:
    client_host = request.client.host if request.client else None
    if not _is_loopback_hostname(client_host):
        raise HTTPException(
            status_code=403,
            detail="TikTok reconciliation is available only on loopback.",
        )

    host_header = request.headers.get("host")
    host_name = urlsplit(f"//{host_header}").hostname if host_header else None
    if not _is_loopback_hostname(host_name):
        raise HTTPException(
            status_code=403,
            detail="TikTok reconciliation requires a loopback Host header.",
        )

    origin = request.headers.get("origin")
    if origin:
        parsed_origin = urlsplit(origin)
        if (
            parsed_origin.scheme not in {"http", "https"}
            or not _is_loopback_hostname(parsed_origin.hostname)
        ):
            raise HTTPException(
                status_code=403,
                detail="Cross-origin TikTok reconciliation is not allowed.",
            )


@router.get("/{post_id}/tiktok/status")
def tiktok_upload_status(post_id: int):
    publish_result = get_publish_result(post_id, "tiktok")

    if not publish_result or not publish_result.get("remote_post_id"):
        raise HTTPException(
            status_code=404,
            detail="No TikTok upload was found for this local post.",
        )

    try:
        provider_status = TikTokPublisher().fetch_upload_status(
            publish_result["remote_post_id"]
        )
    except TikTokUploadError as exc:
        raise HTTPException(
            status_code=_TIKTOK_STATUS_ERROR_HTTP_CODES.get(
                exc.error_code,
                502,
            ),
            detail=str(exc),
        ) from exc

    return {
        "local_post_id": post_id,
        **provider_status,
    }


@router.post("/{post_id}/tiktok/reconcile")
def reconcile_tiktok_status(post_id: int, request: Request):
    _require_local_reconciliation_request(request)
    try:
        return reconcile_tiktok_post(post_id)
    except TikTokReconciliationError as exc:
        status_code = (
            404
            if exc.error_code == "missing_publish_result"
            else 409
        )
        raise HTTPException(status_code=status_code, detail=str(exc)) from exc
    except TikTokUploadError as exc:
        raise HTTPException(
            status_code=_TIKTOK_STATUS_ERROR_HTTP_CODES.get(
                exc.error_code,
                502,
            ),
            detail=str(exc),
        ) from exc


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
                tiktok_mode=data.tiktok_mode,
                tiktok_user_consent=data.tiktok_user_consent,
                tiktok_privacy_level=data.tiktok_privacy_level,
                tiktok_allow_comment=data.tiktok_allow_comment,
                tiktok_allow_duet=data.tiktok_allow_duet,
                tiktok_allow_stitch=data.tiktok_allow_stitch,
                tiktok_commercial_content_disclosure=(
                    data.tiktok_commercial_content_disclosure
                ),
                tiktok_brand_content_toggle=(
                    data.tiktok_brand_content_toggle
                ),
                tiktok_brand_organic_toggle=(
                    data.tiktok_brand_organic_toggle
                ),
                tiktok_is_aigc=data.tiktok_is_aigc,
            )

            is_dry_run = bool(result.get("dry_run"))
            is_published = result.get("published")
            if is_published is None:
                is_published = not is_dry_run
            else:
                is_published = bool(is_published)

            result_status = result.get("status") or (
                "published" if is_published else "ready"
            )
            remote_identifier = (
                result.get("remote_publish_id")
                or result.get("video_id")
            )

            create_publish_result(
                post_id=post_id,
                platform=platform,
                status=result_status,
                remote_post_id=remote_identifier,
                remote_url=result.get("url"),
                mode=(
                    result.get("mode")
                    if platform == "tiktok"
                    else None
                ),
            )

            results[platform] = {
                "success": True,
                "published": is_published,
                "dry_run": is_dry_run,
                "status": result_status,
                "remote_post_id": remote_identifier,
                "url": result.get("url"),
            }

            if platform == "tiktok":
                results[platform]["mode"] = result.get("mode")
                results[platform]["remote_publish_id"] = result.get(
                    "remote_publish_id"
                )
                results[platform]["message"] = result.get("message")
                results[platform]["validation"] = result.get("validation")
                results[platform]["caption_transferred"] = result.get(
                    "caption_transferred",
                    False,
                )
                results[platform]["privacy_level"] = result.get(
                    "privacy_level"
                )

        except Exception as exc:
            failed_publish_id = (
                getattr(exc, "publish_id", None)
                if platform == "tiktok"
                else None
            )
            create_publish_result(
                post_id=post_id,
                platform=platform,
                status="failed",
                remote_post_id=failed_publish_id,
                error=str(exc),
                mode=(
                    data.tiktok_mode
                    if platform == "tiktok"
                    else None
                ),
            )

            results[platform] = {
                "success": False,
                "error": str(exc),
            }

            if platform == "tiktok":
                results[platform].update(
                    {
                        "published": False,
                        "status": "failed",
                        "mode": data.tiktok_mode,
                        "remote_publish_id": failed_publish_id,
                    }
                )
    
    
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
