# TikTok private Direct Post UI contract

This checkpoint supports private TikTok Sandbox Direct Post only. The Flutter
client must not present public posting as available.

## Prepare the screen

1. Read `GET /accounts/tiktok/status` and display the connected account.
2. Read `GET /accounts/tiktok/creator-info` each time the Direct Post screen is
   rendered. Do not cache creator choices across publishing sessions.
3. Display `creator_nickname` and `creator_username`.
4. Render the returned `privacy_level_options`, but enable only values also in
   `checkpoint_privacy_levels`. Do not preselect a privacy value.
5. Render Allow Comment, Allow Duet, and Allow Stitch unchecked. Disable and
   grey out an option when its corresponding creator-info `*_disabled` value is
   true.
6. Preview the selected video and keep its caption and hashtags editable.
7. Do not enable submission if the video's verified duration exceeds
   `max_video_post_duration_sec`.

## Disclosures and consent

The commercial-content control starts off. If it is enabled, require at least
one of Your Brand or Branded Content. Branded Content is disabled in this
checkpoint because TikTok does not permit it with `SELF_ONLY` visibility.

Display the applicable TikTok Music Usage Confirmation and, when relevant,
Branded Content Policy declaration. Obtain explicit consent immediately before
submission; consent must never be persisted as a default.

## Submit

Send `POST /posts/publish` with `platforms` containing `tiktok` and all of:

- `tiktok_publish=true`
- `tiktok_mode="direct_post"`
- `tiktok_user_consent=true`
- `tiktok_privacy_level="SELF_ONLY"`
- the three explicit `tiktok_allow_*` choices
- `tiktok_commercial_content_disclosure`
- `tiktok_brand_content_toggle`
- `tiktok_brand_organic_toggle`
- `tiktok_is_aigc`

A successful transfer returns `status="processing"` and `published=false`.
Poll `GET /posts/{post_id}/tiktok/status` at a bounded interval. Only TikTok's
`PUBLISH_COMPLETE` status confirms that posting completed; it does not by
itself assert public visibility.

The backend safety checks complement but do not replace TikTok's required UI,
preview, disclosure, consent, account-privacy, and audit requirements.
