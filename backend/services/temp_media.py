import secrets
import time
from pathlib import Path

from database.db import get_connection


def _ensure_table(conn):
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS temporary_media (
            token TEXT PRIMARY KEY,
            file_path TEXT NOT NULL,
            expires_at REAL NOT NULL,
            drive_file_id TEXT,
            filename TEXT,
            mime_type TEXT
        )
        """
    )

    # Existing databases may already have the older table.
    columns = {
        row["name"]
        for row in conn.execute(
            "PRAGMA table_info(temporary_media)"
        ).fetchall()
    }

    if "drive_file_id" not in columns:
        conn.execute(
            """
            ALTER TABLE temporary_media
            ADD COLUMN drive_file_id TEXT
            """
        )

    if "filename" not in columns:
        conn.execute(
            """
            ALTER TABLE temporary_media
            ADD COLUMN filename TEXT
            """
        )

    if "mime_type" not in columns:
        conn.execute(
            """
            ALTER TABLE temporary_media
            ADD COLUMN mime_type TEXT
            """
        )


def _cleanup_expired(conn):
    conn.execute(
        "DELETE FROM temporary_media WHERE expires_at <= ?",
        (time.time(),),
    )


def register_temp_media(
    video_path: Path | str,
    ttl_seconds: int = 900,
    drive_file_id: str | None = None,
    filename: str | None = None,
    mime_type: str | None = None,
) -> str:
    path = Path(video_path).resolve()

    if not path.exists() or not path.is_file():
        raise FileNotFoundError(
            f"Media file not found: {path}"
        )

    token = secrets.token_urlsafe(32)
    expires_at = time.time() + ttl_seconds

    with get_connection() as conn:
        _ensure_table(conn)
        _cleanup_expired(conn)

        conn.execute(
            """
            INSERT INTO temporary_media (
                token,
                file_path,
                expires_at,
                drive_file_id,
                filename,
                mime_type
            )
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                token,
                str(path),
                expires_at,
                drive_file_id,
                filename or path.name,
                mime_type,
            ),
        )

    return token


def resolve_temp_media_record(token: str) -> dict | None:
    with get_connection() as conn:
        _ensure_table(conn)
        _cleanup_expired(conn)

        row = conn.execute(
            """
            SELECT
                token,
                file_path,
                drive_file_id,
                filename,
                mime_type,
                expires_at
            FROM temporary_media
            WHERE token = ?
              AND expires_at > ?
            """,
            (
                token,
                time.time(),
            ),
        ).fetchone()

    if row is None:
        return None

    return dict(row)


def resolve_temp_media(token: str) -> Path | None:
    record = resolve_temp_media_record(token)

    if record is None:
        return None

    file_path = record.get("file_path")

    if not file_path:
        return None

    path = Path(file_path)

    if not path.exists() or not path.is_file():
        # Do not delete the DB record if a Drive copy exists.
        if record.get("drive_file_id"):
            return None

        release_temp_media(token)
        return None

    return path


def release_temp_media(token: str):
    with get_connection() as conn:
        _ensure_table(conn)

        conn.execute(
            "DELETE FROM temporary_media WHERE token = ?",
            (token,),
        )