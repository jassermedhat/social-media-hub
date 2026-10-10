import argparse
import asyncio
import json
import os
import sqlite3
import tempfile
import time
from contextlib import closing
from pathlib import Path
from typing import Any

import requests

from database import db
from integrations.tiktok import (
    TIKTOK_UPLOAD_STATUS_URL,
    TikTokPublisher,
)
from services.tiktok_oauth import create_tiktok_service, parse_scopes
from services.tiktok_reconciliation import (
    TRACKER_LOCK_FILE,
    TikTokStatusTracker,
    _ProcessFileLock,
    reconcile_tiktok_post,
)


POST_ID = 21
LIVE_CONFIRMATION = "ONE_STATUS_REQUEST_ONLY"
SAFETY_FLAGS = (
    "TIKTOK_MANUAL_UPLOAD_ENABLED",
    "TIKTOK_DIRECT_POST_ENABLED",
    "TIKTOK_PUBLIC_POSTING_ENABLED",
    "TIKTOK_STATUS_TRACKING_ENABLED",
)


class OneCycleVerificationError(RuntimeError):
    """A safe, non-sensitive verification failure."""


class _StaticOAuthService:
    def __init__(self, credentials: dict[str, Any]):
        self._credentials = dict(credentials)

    def valid_credentials(self) -> dict[str, Any]:
        return dict(self._credentials)


class OneStatusRequestHttpClient:
    """Allow exactly one status POST and reject every other outbound call."""

    def __init__(self, transport=requests):
        self.transport = transport
        self.status_request_count = 0

    def post(self, url: str, **kwargs):
        if url != TIKTOK_UPLOAD_STATUS_URL:
            raise OneCycleVerificationError(
                "The one-cycle harness blocked a non-status endpoint."
            )
        if self.status_request_count >= 1:
            raise OneCycleVerificationError(
                "The one-cycle harness blocked an additional status request."
            )
        self.status_request_count += 1
        return self.transport.post(url, **kwargs)

    def put(self, *args, **kwargs):
        raise OneCycleVerificationError(
            "The one-cycle harness blocked a media-transfer request."
        )

    def get(self, *args, **kwargs):
        raise OneCycleVerificationError(
            "The one-cycle harness blocked an unexpected GET request."
        )

    def delete(self, *args, **kwargs):
        raise OneCycleVerificationError(
            "The one-cycle harness blocked an unexpected DELETE request."
        )


def _environment_flag(name: str) -> bool:
    value = os.getenv(name, "false").strip().lower()
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    raise OneCycleVerificationError(
        f"{name} must be explicitly true or false."
    )


def verify_safety_environment() -> None:
    enabled = [name for name in SAFETY_FLAGS if _environment_flag(name)]
    if enabled:
        raise OneCycleVerificationError(
            "All TikTok publishing and tracking safety flags must be disabled."
        )
    if os.getenv("TIKTOK_ENVIRONMENT", "sandbox").strip().lower() != "sandbox":
        raise OneCycleVerificationError(
            "The one-cycle harness is restricted to TikTok Sandbox."
        )


def verify_distinct_paths(production_path: Path, temporary_path: Path) -> None:
    if production_path.resolve() == temporary_path.resolve():
        raise OneCycleVerificationError(
            "The temporary database path must differ from production."
        )


def _read_only_connection(path: Path):
    return sqlite3.connect(
        f"file:{path.resolve().as_posix()}?mode=ro",
        uri=True,
    )


def _read_post_snapshot(path: Path, post_id: int) -> tuple[Any, ...]:
    with closing(_read_only_connection(path)) as connection:
        row = connection.execute(
            """
            SELECT
                pr.id,
                pr.post_id,
                pr.mode,
                pr.status,
                pr.remote_post_id,
                pr.published_at
            FROM publish_results pr
            WHERE pr.post_id = ?
              AND pr.platform = 'tiktok'
              AND pr.remote_post_id IS NOT NULL
            ORDER BY pr.id DESC
            LIMIT 1
            """,
            (post_id,),
        ).fetchone()
    if row is None:
        raise OneCycleVerificationError(
            "Production post 21 has no stored TikTok result."
        )
    return tuple(row)


def _verify_connected_account(
    production_path: Path,
    credentials: dict[str, Any],
) -> None:
    with closing(_read_only_connection(production_path)) as connection:
        account = connection.execute(
            """
            SELECT account_id
            FROM connected_accounts
            WHERE platform = 'tiktok'
            ORDER BY updated_at DESC
            LIMIT 1
            """
        ).fetchone()
    if account is None or account[0] != credentials.get("open_id"):
        raise OneCycleVerificationError(
            "The stored TikTok account does not match the secure credentials."
        )


def validate_read_only_credentials(credentials: dict[str, Any]) -> None:
    if not isinstance(credentials, dict):
        raise OneCycleVerificationError(
            "TikTok secure credentials are unavailable."
        )
    required_strings = ("access_token", "open_id")
    if not all(
        isinstance(credentials.get(name), str) and credentials[name]
        for name in required_strings
    ):
        raise OneCycleVerificationError(
            "TikTok secure credentials are incomplete."
        )
    scopes = parse_scopes(credentials.get("scopes") or [])
    if "video.publish" not in scopes:
        raise OneCycleVerificationError(
            "TikTok video.publish permission is unavailable."
        )
    try:
        access_expires_at = float(credentials["access_expires_at"])
        refresh_expires_at = float(credentials["refresh_expires_at"])
    except (KeyError, TypeError, ValueError) as exc:
        raise OneCycleVerificationError(
            "TikTok credential expiration metadata is invalid."
        ) from exc
    now = time.time()
    if access_expires_at <= now + 120:
        raise OneCycleVerificationError(
            "TikTok access credentials require refresh; reconnect or refresh "
            "before the one-cycle verification."
        )
    if refresh_expires_at <= now:
        raise OneCycleVerificationError(
            "TikTok authorization has expired."
        )


def load_credentials_without_refresh(base_dir: Path) -> dict[str, Any]:
    client_key = os.getenv("TIKTOK_CLIENT_KEY", "").strip()
    client_secret = os.getenv("TIKTOK_CLIENT_SECRET", "").strip()
    if not client_key or not client_secret:
        raise OneCycleVerificationError(
            "TikTok Sandbox OAuth is not configured."
        )
    service = create_tiktok_service(
        {"client_key": client_key, "client_secret": client_secret},
        base_dir,
    )
    credentials = service.credential_store.load()
    validate_read_only_credentials(credentials)
    return credentials


def _copy_database_read_only(source: Path, destination: Path) -> None:
    verify_distinct_paths(source, destination)
    with closing(_read_only_connection(source)) as source_connection:
        with closing(sqlite3.connect(destination)) as destination_connection:
            source_connection.backup(destination_connection)


def _prepare_temporary_post(
    temporary_database: Path,
    *,
    post_id: int,
    expected_publish_id: str,
) -> None:
    with closing(sqlite3.connect(temporary_database)) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("BEGIN IMMEDIATE")
        cursor = connection.execute(
            """
            UPDATE publish_results
            SET status = 'processing',
                published_at = NULL,
                error = NULL
            WHERE id = (
                SELECT MAX(id)
                FROM publish_results
                WHERE post_id = ?
                  AND platform = 'tiktok'
                  AND remote_post_id IS NOT NULL
            )
              AND post_id = ?
              AND platform = 'tiktok'
              AND mode = 'direct_post'
              AND status = 'published'
              AND remote_post_id = ?
            """,
            (post_id, post_id, expected_publish_id),
        )
        post_cursor = connection.execute(
            "UPDATE posts SET created_at = ? WHERE id = ?",
            (db.utc_now(), post_id),
        )
        if cursor.rowcount != 1 or post_cursor.rowcount != 1:
            connection.rollback()
            raise OneCycleVerificationError(
                "The temporary post could not be prepared safely."
            )
        connection.commit()


def _remove_file(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


def run_one_cycle_verification(
    *,
    production_database: Path,
    credentials: dict[str, Any],
    transport=requests,
    post_id: int = POST_ID,
    production_lock_path: Path = TRACKER_LOCK_FILE,
    temporary_parent: Path | None = None,
) -> dict[str, Any]:
    verify_safety_environment()
    validate_read_only_credentials(credentials)
    production_database = production_database.resolve()
    if not production_database.is_file():
        raise OneCycleVerificationError(
            "The production SQLite database was not found."
        )

    before = _read_post_snapshot(production_database, post_id)
    result_id, _, mode, status, publish_id, published_at = before
    if (
        mode != "direct_post"
        or status != "published"
        or not isinstance(publish_id, str)
        or not publish_id
        or not published_at
    ):
        raise OneCycleVerificationError(
            "Production post 21 is not in the expected published state."
        )
    _verify_connected_account(production_database, credentials)

    production_lock_existed = production_lock_path.exists()
    production_guard = _ProcessFileLock(production_lock_path)
    if not production_guard.acquire():
        raise OneCycleVerificationError(
            "Another TikTok status tracker appears to be running."
        )

    original_database_path = db.DB_FILE
    isolation_error = None
    try:
        with tempfile.TemporaryDirectory(
            prefix="smh-tiktok-one-cycle-",
            dir=temporary_parent,
        ) as directory:
            temporary_root = Path(directory)
            temporary_database = temporary_root / "verification.db"
            temporary_lock_path = temporary_root / "verification.lock"
            verify_distinct_paths(production_database, temporary_database)
            _copy_database_read_only(production_database, temporary_database)
            _prepare_temporary_post(
                temporary_database,
                post_id=post_id,
                expected_publish_id=publish_id,
            )

            temporary_lock = _ProcessFileLock(temporary_lock_path)
            if not temporary_lock.acquire():
                raise OneCycleVerificationError(
                    "The temporary tracker lock could not be acquired."
                )

            guarded_http = OneStatusRequestHttpClient(transport)
            static_oauth = _StaticOAuthService(credentials)
            publisher = TikTokPublisher(
                oauth_service_factory=lambda: static_oauth,
                http_client=guarded_http,
            )
            tracker = TikTokStatusTracker(
                enabled=False,
                batch_size=1,
                max_tracking_age_hours=24,
                pending_loader=db.get_pending_tiktok_publish_results,
                reconciler=lambda selected_post_id: reconcile_tiktok_post(
                    selected_post_id,
                    publisher_factory=lambda: publisher,
                ),
                lock_path=temporary_lock_path,
            )

            try:
                db.DB_FILE = temporary_database
                cycle = asyncio.run(tracker.run_cycle())
                temporary_result = db.get_publish_result(post_id, "tiktok")
            finally:
                db.DB_FILE = original_database_path
                temporary_lock.release()
                _remove_file(temporary_lock_path)

            if guarded_http.status_request_count != 1:
                raise OneCycleVerificationError(
                    "The one-cycle harness did not make exactly one status request."
                )
            if cycle != {
                "eligible": 1,
                "checked": 1,
                "updated": 1,
                "errors": 0,
            }:
                raise OneCycleVerificationError(
                    "The one-cycle tracker result was not successful."
                )
            if (
                not temporary_result
                or temporary_result.get("id") != result_id
                or temporary_result.get("remote_post_id") != publish_id
                or temporary_result.get("status") != "published"
                or not temporary_result.get("published_at")
            ):
                raise OneCycleVerificationError(
                    "TikTok did not confirm publication for the temporary result."
                )

            return {
                "verified": True,
                "local_post_id": post_id,
                "provider_status": "PUBLISH_COMPLETE",
                "temporary_status": "published",
                "status_requests": guarded_http.status_request_count,
                "production_unchanged": True,
            }
    finally:
        db.DB_FILE = original_database_path
        production_guard.release()
        if not production_lock_existed:
            _remove_file(production_lock_path)
        try:
            after = _read_post_snapshot(production_database, post_id)
            if after != before:
                isolation_error = OneCycleVerificationError(
                    "Production post 21 changed during verification."
                )
        except Exception as exc:
            isolation_error = OneCycleVerificationError(
                "Production database isolation could not be verified."
            )
            isolation_error.__cause__ = exc
        if isolation_error is not None:
            raise isolation_error


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run one guarded TikTok tracker cycle against a temporary DB."
    )
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--confirm")
    args = parser.parse_args()

    if not args.live or args.confirm != LIVE_CONFIRMATION:
        parser.error(
            "Live verification requires --live --confirm "
            f"{LIVE_CONFIRMATION}"
        )

    try:
        base_dir = Path(__file__).resolve().parent
        verify_safety_environment()
        credentials = load_credentials_without_refresh(base_dir)
        result = run_one_cycle_verification(
            production_database=db.DB_FILE,
            credentials=credentials,
        )
    except Exception:
        print(
            "TikTok one-cycle verification stopped safely. "
            "No upload or publication operation was attempted."
        )
        return 1

    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
