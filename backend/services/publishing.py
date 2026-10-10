from integrations.facebook import FacebookPublisher
from integrations.instagram import InstagramPublisher
from integrations.linkedin import LinkedInPublisher
from integrations.tiktok import TikTokPublisher
from integrations.youtube import YouTubePublisher
from services.google_drive_media import MediaAsset

class PublishingService:
    def publish(
        self,
        platform: str,
        video_path: str,
        title: str,
        description: str = "",
        privacy: str = "private",
        instagram_publish: bool = False,
        facebook_publish: bool = False,
        linkedin_publish: bool = False,
        media_asset: MediaAsset | None = None,
        tiktok_publish: bool = False,
        tiktok_mode: str = "validation",
        tiktok_user_consent: bool = False,
        tiktok_privacy_level: str | None = None,
        tiktok_allow_comment: bool = False,
        tiktok_allow_duet: bool = False,
        tiktok_allow_stitch: bool = False,
        tiktok_commercial_content_disclosure: bool = False,
        tiktok_brand_content_toggle: bool = False,
        tiktok_brand_organic_toggle: bool = False,
        tiktok_is_aigc: bool = False,
    ):
        platform = platform.lower()

        if platform == "youtube":
            publisher = YouTubePublisher()

            return publisher.publish(
                video_path=video_path,
                title=title,
                description=description,
                privacy=privacy,
            )

        if platform == "instagram":
            publisher = InstagramPublisher()

            return publisher.publish(
                video_path=video_path,
                title=title,
                description=description,
                privacy=privacy,
                publish=instagram_publish,
                media_asset=media_asset,
            )

        if platform == "facebook":
            publisher = FacebookPublisher()

            return publisher.publish(
                video_path=video_path,
                title=title,
                description=description,
                privacy=privacy,
                publish=facebook_publish,
                media_asset=media_asset,
            )

        if platform == "linkedin":
            publisher = LinkedInPublisher()

            return publisher.publish(
                video_path=video_path,
                title=title,
                description=description,
                privacy=privacy,
                publish=linkedin_publish,
            )

        if platform == "tiktok":
            publisher = TikTokPublisher()

            return publisher.publish(
                video_path=video_path,
                title=title,
                description=description,
                privacy=privacy,
                publish=tiktok_publish,
                mode=tiktok_mode,
                user_consent=tiktok_user_consent,
                privacy_level=tiktok_privacy_level,
                allow_comment=tiktok_allow_comment,
                allow_duet=tiktok_allow_duet,
                allow_stitch=tiktok_allow_stitch,
                commercial_content_disclosure=(
                    tiktok_commercial_content_disclosure
                ),
                brand_content_toggle=tiktok_brand_content_toggle,
                brand_organic_toggle=tiktok_brand_organic_toggle,
                is_aigc=tiktok_is_aigc,
            )

        raise ValueError(
            f"Unsupported platform: {platform}"
        )
