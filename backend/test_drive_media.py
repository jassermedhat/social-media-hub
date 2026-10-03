import sys

from services.google_drive_media import GoogleDriveMediaStorage


if len(sys.argv) < 2:
    raise SystemExit(
        'Usage: python test_drive_media.py "C:/path/to/video.mp4"'
    )


local_path = sys.argv[1]

storage = GoogleDriveMediaStorage()

print("Preparing media asset...")

asset = storage.prepare(local_path)

print("MediaAsset created:")
print(asset)

print("Deleting Drive test file...")

storage.delete_file(asset.drive_file_id)

print("Delete successful.")