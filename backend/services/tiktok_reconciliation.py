import asyncio
import os
from pathlib import Path
from typing import Any, Callable

from database import db
from integrations.tiktok import TikTokPublisher


BASE_DIR = Path(__file__).resolve().parent.parent
TRACKER_LOCK_FILE = BASE_DIR / ".tiktok_status_tracker.lock"
PROCESSING_PROVIDER_STATUSES = {
    "PROCESSING_UPLOAD",
    "PROCESSING_DOWNLOAD",
}


class TikTokReconciliationError(RuntimeError):
    def __init__(self, message: str, *, error_code: str):
        super().__init__(message)
        self.error_code = error_code


def map_provider_status(mode: str, provider_status: str) -> str | None:
    if provider_status in PROCESSING_PROVIDER_STATUSES:
        return "processing"
    if provider_status == "FAILED":
        return "failed"
    if provider_status == "SEND_TO_USER_INBOX":
        return "manual_completion" if mode == "manual_upload" else None
    if provider_status == "PUBLISH_COMPLETE":
        if mode == "direct_post":
            return "published"
        if mode == "manual_upload":
            return "manual_completion"
    return None


def _transition_allowed(
    *,
    mode: str,
    current_status: str,
    target_status: str,
    provider_status: str,
) -> bool:
    if current_status == target_status:
        return True
    if current_status == "published":
        return False
    if current_status == "failed":
        return (
            mode == "manual_upload"
            and target_status == "manual_completion"
            and provider_status in {
                "SEND_TO_USER_INBOX",
                "PUBLISH_COMPLETE",
            }
        ) or (
            mode == "direct_post"
            and target_status == "published"
            and provider_status == "PUBLISH_COMPLETE"
        )
    if current_status == "manual_completion":
        return target_status in {"manual_completion", "failed"}
    return current_status in {"processing", "ready"}


def reconcile_tiktok_post(
    post_id: int,
    *,
    publisher_factory: Callable[[], TikTokPublisher] = TikTokPublisher,
) -> dict[str, Any]:
    stored = db.get_publish_result(post_id, "tiktok")
    if not stored or not stored.get("remote_post_id"):
        raise TikTokReconciliationError(
            "No TikTok upload was found for this local post.",
            error_code="missing_publish_result",
        )

    mode = stored.get("mode")
    if mode not in {"manual_upload", "direct_post"}:
        raise TikTokReconciliationError(
            "The stored TikTok result cannot be reconciled safely.",
            error_code="unsupported_publish_mode",
        )

    provider = publisher_factory().fetch_upload_status(
        stored["remote_post_id"]
    )
    provider_status = provider["provider_status"]
    target_status = map_provider_status(mode, provider_status)
    previous_status = stored["status"]

    if target_status is None:
        database_result = {
            "updated": False,
            "reason": "provider_status_incompatible_with_mode",
            "status": previous_status,
            "published_at": stored.get("published_at"),
        }
    elif not _transition_allowed(
        mode=mode,
        current_status=previous_status,
        target_status=target_status,
        provider_status=provider_status,
    ):
        database_result = {
            "updated": False,
            "reason": "unsafe_or_stale_transition",
            "status": previous_status,
            "published_at": stored.get("published_at"),
        }
    else:
        database_result = db.reconcile_tiktok_publish_result(
            post_id=post_id,
            result_id=stored["id"],
            publish_id=stored["remote_post_id"],
            expected_status=previous_status,
            new_status=target_status,
            failure_reason=provider.get("failure_reason"),
        )

    return {
        "local_post_id": post_id,
        "mode": mode,
        "previous_local_status": previous_status,
        "local_status": database_result["status"],
        "database_updated": database_result["updated"],
        "reconciliation_reason": database_result["reason"],
        "published_at": database_result["published_at"],
        "provider_status": provider_status,
        "uploaded_bytes": provider.get("uploaded_bytes"),
        "downloaded_bytes": provider.get("downloaded_bytes"),
        "failure_reason": provider.get("failure_reason"),
        "manual_completion_required": (
            mode == "manual_upload"
            and provider_status == "SEND_TO_USER_INBOX"
        ),
        "provider_reports_user_posted": (
            mode == "manual_upload"
            and provider_status == "PUBLISH_COMPLETE"
        ),
        "publication_confirmed": (
            mode == "direct_post"
            and provider_status == "PUBLISH_COMPLETE"
        ),
    }


class _ProcessFileLock:
    def __init__(self, path: Path):
        self.path = path
        self._file = None

    def acquire(self) -> bool:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            lock_file = self.path.open("a+b")
        except OSError:
            return False
        try:
            lock_file.seek(0, os.SEEK_END)
            if lock_file.tell() == 0:
                lock_file.write(b"0")
                lock_file.flush()
            lock_file.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (OSError, BlockingIOError):
            lock_file.close()
            return False
        self._file = lock_file
        return True

    def release(self) -> None:
        if self._file is None:
            return
        try:
            self._file.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(self._file.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(self._file.fileno(), fcntl.LOCK_UN)
        finally:
            self._file.close()
            self._file = None


class TikTokStatusTracker:
    def __init__(
        self,
        *,
        enabled: bool,
        interval_seconds: float = 300.0,
        batch_size: int = 10,
        max_tracking_age_hours: int = 24,
        max_backoff_seconds: float = 900.0,
        pending_loader: Callable[[int, int], list[dict[str, Any]]] = (
            db.get_pending_tiktok_publish_results
        ),
        reconciler: Callable[[int], dict[str, Any]] = reconcile_tiktok_post,
        lock_path: Path = TRACKER_LOCK_FILE,
    ):
        self.enabled = enabled
        self.interval_seconds = max(float(interval_seconds), 0.01)
        self.batch_size = max(1, min(int(batch_size), 25))
        self.max_tracking_age_hours = max(
            1,
            min(int(max_tracking_age_hours), 168),
        )
        self.max_backoff_seconds = max(
            float(max_backoff_seconds),
            self.interval_seconds,
        )
        self.pending_loader = pending_loader
        self.reconciler = reconciler
        self._process_lock = _ProcessFileLock(lock_path)
        self._stop_event = asyncio.Event()
        self._task: asyncio.Task | None = None
        self._claims_guard = asyncio.Lock()
        self._claimed_result_ids: set[int] = set()
        self._error_streak = 0
        self.next_delay_seconds = self.interval_seconds

    async def start(self) -> bool:
        if not self.enabled:
            return False
        if self._task is not None and not self._task.done():
            return False
        if not self._process_lock.acquire():
            return False
        self._stop_event.clear()
        self._task = asyncio.create_task(self._run())
        return True

    async def stop(self) -> None:
        self._stop_event.set()
        if self._task is not None:
            try:
                await asyncio.wait_for(self._task, timeout=35)
            except asyncio.TimeoutError:
                self._task.cancel()
                await asyncio.gather(self._task, return_exceptions=True)
            self._task = None
        self._process_lock.release()

    async def run_cycle(self) -> dict[str, int]:
        try:
            rows = self.pending_loader(
                self.batch_size,
                self.max_tracking_age_hours,
            )
        except Exception:
            self._record_cycle_errors(1)
            return {
                "eligible": 0,
                "checked": 0,
                "updated": 0,
                "errors": 1,
            }
        checked = 0
        updated = 0
        errors = 0

        for row in rows:
            result_id = int(row["id"])
            async with self._claims_guard:
                if result_id in self._claimed_result_ids:
                    continue
                self._claimed_result_ids.add(result_id)

            try:
                result = await asyncio.to_thread(
                    self.reconciler,
                    int(row["post_id"]),
                )
                checked += 1
                updated += int(bool(result.get("database_updated")))
            except Exception:
                checked += 1
                errors += 1
            finally:
                async with self._claims_guard:
                    self._claimed_result_ids.discard(result_id)

        self._record_cycle_errors(errors)

        return {
            "eligible": len(rows),
            "checked": checked,
            "updated": updated,
            "errors": errors,
        }

    def _record_cycle_errors(self, errors: int) -> None:
        if errors:
            self._error_streak += 1
            self.next_delay_seconds = min(
                self.max_backoff_seconds,
                self.interval_seconds * (2 ** self._error_streak),
            )
        else:
            self._error_streak = 0
            self.next_delay_seconds = self.interval_seconds

    async def _run(self) -> None:
        while not self._stop_event.is_set():
            await self.run_cycle()
            try:
                await asyncio.wait_for(
                    self._stop_event.wait(),
                    timeout=self.next_delay_seconds,
                )
            except asyncio.TimeoutError:
                pass


def _environment_flag(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _environment_number(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)).strip())
    except (TypeError, ValueError):
        return default


def create_status_tracker_from_environment() -> TikTokStatusTracker:
    interval = max(
        60,
        _environment_number("TIKTOK_STATUS_TRACKING_INTERVAL_SECONDS", 300),
    )
    batch_size = max(
        1,
        min(
            _environment_number("TIKTOK_STATUS_TRACKING_BATCH_SIZE", 10),
            25,
        ),
    )
    max_backoff = max(
        interval,
        _environment_number(
            "TIKTOK_STATUS_TRACKING_MAX_BACKOFF_SECONDS",
            900,
        ),
    )
    max_tracking_age_hours = max(
        1,
        min(
            _environment_number(
                "TIKTOK_STATUS_TRACKING_MAX_AGE_HOURS",
                24,
            ),
            168,
        ),
    )
    return TikTokStatusTracker(
        enabled=_environment_flag("TIKTOK_STATUS_TRACKING_ENABLED", False),
        interval_seconds=interval,
        batch_size=batch_size,
        max_tracking_age_hours=max_tracking_age_hours,
        max_backoff_seconds=max_backoff,
    )
