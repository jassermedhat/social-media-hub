from integrations.facebook import FacebookPublisher
from integrations.instagram import InstagramPublisher
from integrations.linkedin import LinkedInPublisher
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

        raise ValueError(
            f"Unsupported platform: {platform}"
        )
