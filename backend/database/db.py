import sqlite3
import re
from contextlib import contextmanager
from pathlib import Path
from datetime import datetime, timedelta, timezone


BASE_DIR = Path(__file__).resolve().parent.parent
DB_FILE = BASE_DIR / "social_publisher.db"
_SAFE_PROVIDER_FAILURE = re.compile(r"[A-Za-z0-9_.:-]{1,128}")


def utc_now():
    return datetime.now(timezone.utc).isoformat()


@contextmanager
def get_connection():
    connection = sqlite3.connect(DB_FILE)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    try:
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def init_db():
    with get_connection() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS connected_accounts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                platform TEXT NOT NULL,
                account_id TEXT NOT NULL,
                account_name TEXT NOT NULL,
                token_file TEXT,
                connected_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,

                UNIQUE(platform, account_id)
            );

            CREATE TABLE IF NOT EXISTS posts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                video_path TEXT NOT NULL,
                title TEXT NOT NULL,
                description TEXT DEFAULT '',
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS publish_results (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                post_id INTEGER NOT NULL,
                platform TEXT NOT NULL,
                status TEXT NOT NULL,
                remote_post_id TEXT,
                remote_url TEXT,
                error TEXT,
                published_at TEXT,
                mode TEXT,

                FOREIGN KEY(post_id)
                    REFERENCES posts(id)
                    ON DELETE CASCADE
            );
            """
        )

        publish_result_columns = {
            row["name"]
            for row in conn.execute(
                "PRAGMA table_info(publish_results)"
            ).fetchall()
        }

        if "mode" not in publish_result_columns:
            conn.execute(
                "ALTER TABLE publish_results ADD COLUMN mode TEXT"
            )


def upsert_connected_account(
    platform: str,
    account_id: str,
    account_name: str,
    token_file: str | None = None,
):
    now = utc_now()

    with get_connection() as conn:
        conn.execute(
            """
            INSERT INTO connected_accounts (
                platform,
                account_id,
                account_name,
                token_file,
                connected_at,
                updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?)

            ON CONFLICT(platform, account_id)
            DO UPDATE SET
                account_name = excluded.account_name,
                token_file = excluded.token_file,
                updated_at = excluded.updated_at
            """,
            (
                platform,
                account_id,
                account_name,
                token_file,
                now,
                now,
            ),
        )


def get_connected_account(platform: str):
    with get_connection() as conn:
        row = conn.execute(
            """
            SELECT *
            FROM connected_accounts
            WHERE platform = ?
            ORDER BY updated_at DESC
            LIMIT 1
            """,
            (platform,),
        ).fetchone()

    if row is None:
        return None

    return dict(row)


def delete_connected_account(platform: str):
    with get_connection() as conn:
        cursor = conn.execute(
            """
            DELETE FROM connected_accounts
            WHERE platform = ?
            """,
            (platform,),
        )

        return cursor.rowcount


def create_post(
    video_path: str,
    title: str,
    description: str = "",
):
    with get_connection() as conn:
        cursor = conn.execute(
            """
            INSERT INTO posts (
                video_path,
                title,
                description,
                created_at
            )
            VALUES (?, ?, ?, ?)
            """,
            (
                video_path,
                title,
                description,
                utc_now(),
            ),
        )

        return cursor.lastrowid


def create_publish_result(
    post_id: int,
    platform: str,
    status: str,
    remote_post_id: str | None = None,
    remote_url: str | None = None,
    error: str | None = None,
    mode: str | None = None,
):
    published_at = utc_now() if status == "published" else None

    with get_connection() as conn:
        cursor = conn.execute(
            """
            INSERT INTO publish_results (
                post_id,
                platform,
                status,
                remote_post_id,
                remote_url,
                error,
                published_at,
                mode
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                post_id,
                platform,
                status,
                remote_post_id,
                remote_url,
                error,
                published_at,
                mode,
            ),
        )

        return cursor.lastrowid


def get_publish_result(post_id: int, platform: str):
    with get_connection() as conn:
        row = conn.execute(
            """
            SELECT pr.*
            FROM publish_results pr
            INNER JOIN posts p
                ON p.id = pr.post_id
            WHERE pr.post_id = ?
              AND pr.platform = ?
              AND pr.remote_post_id IS NOT NULL
            ORDER BY pr.id DESC
            LIMIT 1
            """,
            (post_id, platform),
        ).fetchone()

    if row is None:
        return None

    return dict(row)


def get_pending_tiktok_publish_results(
    limit: int = 10,
    max_age_hours: int = 24,
):
    safe_limit = max(1, min(int(limit), 25))
    safe_max_age_hours = max(1, min(int(max_age_hours), 168))
    created_after = (
        datetime.now(timezone.utc)
        - timedelta(hours=safe_max_age_hours)
    ).isoformat()

    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT pr.*
            FROM publish_results pr
            INNER JOIN posts p
                ON p.id = pr.post_id
            WHERE pr.platform = 'tiktok'
              AND pr.remote_post_id IS NOT NULL
              AND pr.mode IN ('manual_upload', 'direct_post')
              AND pr.status = 'processing'
              AND p.created_at >= ?
            ORDER BY pr.id ASC
            LIMIT ?
            """,
            (created_after, safe_limit),
        ).fetchall()

    return [dict(row) for row in rows]


def reconcile_tiktok_publish_result(
    *,
    post_id: int,
    result_id: int,
    publish_id: str,
    expected_status: str,
    new_status: str,
    failure_reason: str | None = None,
):
    """Compare-and-set one exact TikTok result in a single transaction."""
    allowed_statuses = {
        "processing",
        "manual_completion",
        "published",
        "failed",
    }
    if new_status not in allowed_statuses:
        raise ValueError("Unsupported TikTok reconciliation status.")

    safe_failure = (
        failure_reason
        if isinstance(failure_reason, str)
        and _SAFE_PROVIDER_FAILURE.fullmatch(failure_reason)
        else None
    )
    stored_error = (
        f"TikTok reported: {safe_failure}"
        if new_status == "failed" and safe_failure
        else (
            "TikTok reported that processing failed."
            if new_status == "failed"
            else None
        )
    )

    with get_connection() as conn:
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                """
                SELECT *
                FROM publish_results
                WHERE id = ?
                  AND post_id = ?
                  AND platform = 'tiktok'
                  AND remote_post_id = ?
                  AND id = (
                      SELECT MAX(newest.id)
                      FROM publish_results newest
                      WHERE newest.post_id = ?
                        AND newest.platform = 'tiktok'
                        AND newest.remote_post_id IS NOT NULL
                  )
                """,
                (result_id, post_id, publish_id, post_id),
            ).fetchone()

            if row is None:
                conn.rollback()
                return {
                    "updated": False,
                    "reason": "stale_result",
                    "status": None,
                    "published_at": None,
                }

            current = dict(row)
            current_status = current["status"]
            if current_status != expected_status:
                conn.rollback()
                return {
                    "updated": False,
                    "reason": "stale_status",
                    "status": current_status,
                    "published_at": current.get("published_at"),
                }

            if current_status == "published" and new_status != "published":
                conn.rollback()
                return {
                    "updated": False,
                    "reason": "terminal_published",
                    "status": current_status,
                    "published_at": current.get("published_at"),
                }

            published_at = current.get("published_at")
            if new_status == "published":
                if current.get("mode") != "direct_post":
                    conn.rollback()
                    return {
                        "updated": False,
                        "reason": "manual_upload_not_publishable",
                        "status": current_status,
                        "published_at": published_at,
                    }
                published_at = published_at or utc_now()

            if (
                current_status == new_status
                and current.get("error") == stored_error
                and current.get("published_at") == published_at
            ):
                conn.rollback()
                return {
                    "updated": False,
                    "reason": "unchanged",
                    "status": current_status,
                    "published_at": published_at,
                }

            cursor = conn.execute(
                """
                UPDATE publish_results
                SET status = ?,
                    error = ?,
                    published_at = ?
                WHERE id = ?
                  AND post_id = ?
                  AND platform = 'tiktok'
                  AND remote_post_id = ?
                  AND status = ?
                """,
                (
                    new_status,
                    stored_error,
                    published_at,
                    result_id,
                    post_id,
                    publish_id,
                    expected_status,
                ),
            )
            if cursor.rowcount != 1:
                conn.rollback()
                return {
                    "updated": False,
                    "reason": "stale_status",
                    "status": current_status,
                    "published_at": current.get("published_at"),
                }

            conn.commit()
            return {
                "updated": True,
                "reason": "updated",
                "status": new_status,
                "published_at": published_at,
            }
        except Exception:
            conn.rollback()
            raise


def get_publish_history():
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT
                p.id AS post_id,
                p.video_path,
                p.title,
                p.description,
                p.created_at,

                pr.id AS result_id,
                pr.platform,
                pr.status,
                pr.remote_post_id,
                pr.remote_url,
                pr.error,
                pr.published_at,
                pr.mode

            FROM posts p

            LEFT JOIN publish_results pr
                ON pr.post_id = p.id

            ORDER BY p.created_at DESC
            """
        ).fetchall()

    return [dict(row) for row in rows]

