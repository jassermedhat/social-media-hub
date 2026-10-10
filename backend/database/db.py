import sqlite3
from pathlib import Path
from datetime import datetime, timezone


BASE_DIR = Path(__file__).resolve().parent.parent
DB_FILE = BASE_DIR / "social_publisher.db"


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def get_connection():
    connection = sqlite3.connect(DB_FILE)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


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

                FOREIGN KEY(post_id)
                    REFERENCES posts(id)
                    ON DELETE CASCADE
            );
            """
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
                published_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                post_id,
                platform,
                status,
                remote_post_id,
                remote_url,
                error,
                published_at,
            ),
        )

        return cursor.lastrowid


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
                pr.published_at

            FROM posts p

            LEFT JOIN publish_results pr
                ON pr.post_id = p.id

            ORDER BY p.created_at DESC
            """
        ).fetchall()

    return [dict(row) for row in rows]

