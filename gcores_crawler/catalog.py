from __future__ import annotations

import json
import sqlite3
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, TypeVar


SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;
PRAGMA temp_store=MEMORY;

CREATE TABLE IF NOT EXISTS items (
    item_key TEXT PRIMARY KEY,
    content_type TEXT NOT NULL,
    item_id TEXT NOT NULL,
    title TEXT,
    url TEXT,
    published_at TEXT,
    is_free INTEGER,
    category TEXT,
    tags_json TEXT,
    users_json TEXT,
    normalized_path TEXT,
    updated_at TEXT NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_items_type_id
ON items(content_type, item_id);

CREATE TABLE IF NOT EXISTS chunks (
    chunk_id TEXT PRIMARY KEY,
    item_key TEXT NOT NULL,
    transcript_id TEXT,
    source TEXT NOT NULL,
    content_type TEXT NOT NULL,
    item_id TEXT NOT NULL,
    segment_type TEXT NOT NULL,
    segment_id TEXT NOT NULL,
    chunk_index INTEGER NOT NULL,
    segment_title TEXT,
    segment_url TEXT,
    published_at TEXT,
    text TEXT NOT NULL,
    metadata_json TEXT,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_chunks_item_source
ON chunks(item_key, source);

CREATE INDEX IF NOT EXISTS idx_chunks_transcript_id
ON chunks(transcript_id);

CREATE TABLE IF NOT EXISTS media_jobs (
    job_id TEXT PRIMARY KEY,
    item_key TEXT NOT NULL,
    content_type TEXT NOT NULL,
    item_id TEXT NOT NULL,
    item_title TEXT,
    segment_type TEXT NOT NULL,
    segment_id TEXT NOT NULL,
    segment_title TEXT,
    segment_url TEXT,
    published_at TEXT,
    chapter_index INTEGER,
    media_id TEXT NOT NULL,
    media_title TEXT,
    media_type TEXT,
    provider TEXT,
    access_mode TEXT,
    media_url TEXT,
    playlist_url TEXT,
    play_auth TEXT,
    duration REAL,
    download_method TEXT,
    local_relpath TEXT,
    status TEXT NOT NULL,
    last_error TEXT,
    file_size INTEGER,
    downloaded_path TEXT,
    downloaded_at TEXT,
    transcript_status TEXT NOT NULL,
    transcript_error TEXT,
    transcript_path TEXT,
    transcribed_at TEXT,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_media_jobs_status
ON media_jobs(status);

CREATE INDEX IF NOT EXISTS idx_media_jobs_transcript_status
ON media_jobs(transcript_status);

CREATE INDEX IF NOT EXISTS idx_media_jobs_item
ON media_jobs(item_key);

CREATE TABLE IF NOT EXISTS transcripts (
    transcript_id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL UNIQUE,
    item_key TEXT NOT NULL,
    content_type TEXT NOT NULL,
    item_id TEXT NOT NULL,
    segment_type TEXT NOT NULL,
    segment_id TEXT NOT NULL,
    segment_title TEXT,
    language TEXT,
    model_name TEXT,
    text TEXT NOT NULL,
    segments_json TEXT,
    transcript_path TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_transcripts_item
ON transcripts(item_key);

CREATE TABLE IF NOT EXISTS sync_state (
    state_key TEXT PRIMARY KEY,
    payload_json TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS search_documents (
    doc_id TEXT PRIMARY KEY,
    item_key TEXT NOT NULL,
    parent_doc_id TEXT,
    doc_type TEXT NOT NULL,
    content_type TEXT NOT NULL,
    item_id TEXT NOT NULL,
    item_title TEXT,
    doc_index INTEGER NOT NULL,
    title TEXT,
    start_ms INTEGER,
    end_ms INTEGER,
    timeline_ms INTEGER,
    text_raw TEXT NOT NULL,
    text_normalized TEXT,
    vector_text TEXT,
    keyword_text TEXT,
    text_search TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    source_updated_at TEXT,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_search_documents_item
ON search_documents(item_key);

CREATE INDEX IF NOT EXISTS idx_search_documents_type
ON search_documents(doc_type);

CREATE INDEX IF NOT EXISTS idx_search_documents_item_type
ON search_documents(item_key, doc_type);

CREATE TABLE IF NOT EXISTS search_documents_scoped (
    collection_name TEXT NOT NULL,
    doc_id TEXT NOT NULL,
    item_key TEXT NOT NULL,
    parent_doc_id TEXT,
    doc_type TEXT NOT NULL,
    content_type TEXT NOT NULL,
    item_id TEXT NOT NULL,
    item_title TEXT,
    doc_index INTEGER NOT NULL,
    title TEXT,
    start_ms INTEGER,
    end_ms INTEGER,
    timeline_ms INTEGER,
    text_raw TEXT NOT NULL,
    text_normalized TEXT,
    vector_text TEXT,
    keyword_text TEXT,
    text_search TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    source_updated_at TEXT,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (collection_name, doc_id)
);

CREATE INDEX IF NOT EXISTS idx_search_documents_scoped_item
ON search_documents_scoped(collection_name, item_key);

CREATE INDEX IF NOT EXISTS idx_search_documents_scoped_type
ON search_documents_scoped(collection_name, doc_type);

CREATE INDEX IF NOT EXISTS idx_search_documents_scoped_item_type
ON search_documents_scoped(collection_name, item_key, doc_type);

CREATE TABLE IF NOT EXISTS enrichment_jobs (
    job_key TEXT PRIMARY KEY,
    item_key TEXT NOT NULL,
    transcript_id TEXT,
    job_type TEXT NOT NULL,
    status TEXT NOT NULL,
    attempt_count INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    payload_json TEXT,
    updated_at TEXT NOT NULL,
    completed_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_enrichment_jobs_status
ON enrichment_jobs(status);
"""


LEGACY_SEARCH_COLLECTION_PREFIXES = ("gcores_memory_preview",)
LEGACY_SEARCH_COLLECTION_NAMES = {"gcores_memory_v1"}


def normalize_search_collection_name(collection_name: Optional[str]) -> str:
    return str(collection_name or "").strip()


def use_legacy_search_documents(collection_name: Optional[str]) -> bool:
    normalized = normalize_search_collection_name(collection_name)
    if not normalized:
        return True
    if normalized in LEGACY_SEARCH_COLLECTION_NAMES:
        return True
    return any(normalized.startswith(prefix) for prefix in LEGACY_SEARCH_COLLECTION_PREFIXES)


class Catalog:
    def __init__(self, root: str = "data") -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.db_path = self.root / "catalog.sqlite"
        self._ensure_schema()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path, timeout=30.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=30000;")
        return connection

    def _ensure_schema(self) -> None:
        def action(connection: sqlite3.Connection) -> None:
            connection.executescript(SCHEMA)
            self._ensure_items_schema(connection)
            self._ensure_search_documents_schema(connection)

        self._write_with_retry(action)

    def _ensure_items_schema(self, connection: sqlite3.Connection) -> None:
        item_columns = {
            str(row["name"])
            for row in connection.execute("PRAGMA table_info(items)").fetchall()
        }
        if "owner_type" not in item_columns:
            connection.execute("ALTER TABLE items ADD COLUMN owner_type TEXT")
        if "option_is_official" not in item_columns:
            connection.execute("ALTER TABLE items ADD COLUMN option_is_official INTEGER")
        connection.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_items_radio_ownership
            ON items(content_type, owner_type, option_is_official)
            """
        )
        self._backfill_item_ownership(connection)

    def _ensure_search_documents_schema(self, connection: sqlite3.Connection) -> None:
        for table_name in ("search_documents", "search_documents_scoped"):
            columns = {
                str(row["name"])
                for row in connection.execute(f"PRAGMA table_info({table_name})").fetchall()
            }
            if "vector_text" not in columns:
                connection.execute(f"ALTER TABLE {table_name} ADD COLUMN vector_text TEXT")
            if "keyword_text" not in columns:
                connection.execute(f"ALTER TABLE {table_name} ADD COLUMN keyword_text TEXT")

    def _backfill_item_ownership(self, connection: sqlite3.Connection) -> None:
        rows = connection.execute(
            """
            SELECT item_key, normalized_path
            FROM items
            WHERE content_type = 'radios'
              AND (owner_type IS NULL OR option_is_official IS NULL)
            """
        ).fetchall()
        updates: list[tuple[Optional[str], Optional[int], str]] = []
        for row in rows:
            normalized_relpath = str(row["normalized_path"] or "").strip()
            if not normalized_relpath:
                continue
            path = self.root / normalized_relpath
            if not path.exists():
                continue
            try:
                record = json.loads(path.read_text(encoding="utf-8-sig"))
            except Exception:
                continue
            updates.append(
                (
                    record.get("owner_type"),
                    bool_to_int(record.get("option_is_official")),
                    str(row["item_key"]),
                )
            )
        if updates:
            connection.executemany(
                """
                UPDATE items
                SET owner_type = COALESCE(?, owner_type),
                    option_is_official = COALESCE(?, option_is_official)
                WHERE item_key = ?
                """,
                updates,
            )

    def _write_with_retry(self, action: Callable[[sqlite3.Connection], "T"]) -> "T":
        last_error: Optional[Exception] = None
        for attempt in range(1, 6):
            try:
                with self._connect() as connection:
                    result = action(connection)
                    connection.commit()
                    return result
            except sqlite3.OperationalError as exc:
                last_error = exc
                lowered = str(exc).lower()
                if not any(token in lowered for token in ("readonly", "locked", "busy")):
                    raise
                if attempt == 5:
                    break
                time.sleep(0.2 * attempt)
        raise last_error if last_error else RuntimeError("SQLite write failed without an exception")

    def replace_item_snapshot(
        self,
        *,
        record: dict,
        normalized_relpath: str,
        snapshot_updated_at: Optional[str] = None,
        documents: List[dict],
        media_jobs: List[dict],
    ) -> None:
        item_key = item_key_for(record["type"], record["id"])
        updated_at = str(snapshot_updated_at or utc_now_iso())
        users = [user.get("nickname") for user in record.get("users", []) if user.get("nickname")]

        def action(connection: sqlite3.Connection) -> None:
            connection.execute(
                """
                INSERT INTO items (
                    item_key,
                    content_type,
                    item_id,
                    title,
                    url,
                    published_at,
                    is_free,
                    category,
                    tags_json,
                    users_json,
                    owner_type,
                    option_is_official,
                    normalized_path,
                    updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(item_key) DO UPDATE SET
                    title = excluded.title,
                    url = excluded.url,
                    published_at = excluded.published_at,
                    is_free = excluded.is_free,
                    category = excluded.category,
                    tags_json = excluded.tags_json,
                    users_json = excluded.users_json,
                    owner_type = excluded.owner_type,
                    option_is_official = excluded.option_is_official,
                    normalized_path = excluded.normalized_path,
                    updated_at = excluded.updated_at
                """,
                (
                    item_key,
                    record.get("type"),
                    record.get("id"),
                    record.get("title"),
                    record.get("url"),
                    record.get("published_at"),
                    bool_to_int(record.get("is_free")),
                    record.get("category"),
                    json.dumps(record.get("tags", []), ensure_ascii=False),
                    json.dumps(users, ensure_ascii=False),
                    record.get("owner_type"),
                    bool_to_int(record.get("option_is_official")),
                    normalized_relpath,
                    updated_at,
                ),
            )

            connection.execute(
                "DELETE FROM chunks WHERE item_key = ? AND source = 'content'",
                (item_key,),
            )
            if documents:
                connection.executemany(
                    """
                    INSERT INTO chunks (
                        chunk_id,
                        item_key,
                        transcript_id,
                        source,
                        content_type,
                        item_id,
                        segment_type,
                        segment_id,
                        chunk_index,
                        segment_title,
                        segment_url,
                        published_at,
                        text,
                        metadata_json,
                        updated_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    [
                        chunk_row_from_document(
                            document=document,
                            item_key=item_key,
                            transcript_id=None,
                            updated_at=updated_at,
                        )
                        for document in documents
                    ],
                )

            existing_state = {
                row["job_id"]: dict(row)
                for row in connection.execute(
                    """
                    SELECT
                        job_id,
                        status,
                        last_error,
                        file_size,
                        downloaded_path,
                        downloaded_at,
                        transcript_status,
                        transcript_error,
                        transcript_path,
                        transcribed_at
                    FROM media_jobs
                    WHERE item_key = ?
                    """,
                    (item_key,),
                )
            }

            current_job_ids = [str(job["job_id"]) for job in media_jobs]
            if current_job_ids:
                placeholders = ", ".join("?" for _ in current_job_ids)
                connection.execute(
                    f"DELETE FROM media_jobs WHERE item_key = ? AND job_id NOT IN ({placeholders})",
                    (item_key, *current_job_ids),
                )
            else:
                connection.execute("DELETE FROM media_jobs WHERE item_key = ?", (item_key,))

            for job in media_jobs:
                preserved = existing_state.get(str(job["job_id"]), {})
                connection.execute(
                    """
                    INSERT INTO media_jobs (
                        job_id,
                        item_key,
                        content_type,
                        item_id,
                        item_title,
                        segment_type,
                        segment_id,
                        segment_title,
                        segment_url,
                        published_at,
                        chapter_index,
                        media_id,
                        media_title,
                        media_type,
                        provider,
                        access_mode,
                        media_url,
                        playlist_url,
                        play_auth,
                        duration,
                        download_method,
                        local_relpath,
                        status,
                        last_error,
                        file_size,
                        downloaded_path,
                        downloaded_at,
                        transcript_status,
                        transcript_error,
                        transcript_path,
                        transcribed_at,
                        updated_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(job_id) DO UPDATE SET
                        item_key = excluded.item_key,
                        content_type = excluded.content_type,
                        item_id = excluded.item_id,
                        item_title = excluded.item_title,
                        segment_type = excluded.segment_type,
                        segment_id = excluded.segment_id,
                        segment_title = excluded.segment_title,
                        segment_url = excluded.segment_url,
                        published_at = excluded.published_at,
                        chapter_index = excluded.chapter_index,
                        media_id = excluded.media_id,
                        media_title = excluded.media_title,
                        media_type = excluded.media_type,
                        provider = excluded.provider,
                        access_mode = excluded.access_mode,
                        media_url = excluded.media_url,
                        playlist_url = excluded.playlist_url,
                        play_auth = excluded.play_auth,
                        duration = excluded.duration,
                        download_method = excluded.download_method,
                        local_relpath = excluded.local_relpath,
                        status = excluded.status,
                        last_error = excluded.last_error,
                        file_size = excluded.file_size,
                        downloaded_path = excluded.downloaded_path,
                        downloaded_at = excluded.downloaded_at,
                        transcript_status = excluded.transcript_status,
                        transcript_error = excluded.transcript_error,
                        transcript_path = excluded.transcript_path,
                        transcribed_at = excluded.transcribed_at,
                        updated_at = excluded.updated_at
                    """,
                    media_job_row(
                        item_key=item_key,
                        job=job,
                        preserved=preserved,
                        updated_at=updated_at,
                    ),
                )

        self._write_with_retry(action)

    def get_download_jobs(
        self,
        *,
        limit: int,
        include_errors: bool = False,
        job_ids: Optional[List[str]] = None,
        item_keys: Optional[List[str]] = None,
        segment_types: Optional[List[str]] = None,
        media_types: Optional[List[str]] = None,
    ) -> List[dict]:
        conditions = ["status IN ('pending', 'error')" if include_errors else "status = 'pending'"]
        params: List[object] = []

        if job_ids:
            placeholders = ", ".join("?" for _ in job_ids)
            conditions.append(f"job_id IN ({placeholders})")
            params.extend(job_ids)

        if item_keys:
            placeholders = ", ".join("?" for _ in item_keys)
            conditions.append(f"item_key IN ({placeholders})")
            params.extend(item_keys)

        if segment_types:
            placeholders = ", ".join("?" for _ in segment_types)
            conditions.append(f"segment_type IN ({placeholders})")
            params.extend(segment_types)

        if media_types:
            placeholders = ", ".join("?" for _ in media_types)
            conditions.append(f"media_type IN ({placeholders})")
            params.extend(media_types)

        params.append(limit)

        with self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT *
                FROM media_jobs
                WHERE {' AND '.join(conditions)}
                ORDER BY
                    CASE
                        WHEN transcript_status = 'pending' THEN 0
                        ELSE 1
                    END,
                    CASE
                        WHEN segment_type = 'item' THEN 0
                        ELSE 1
                    END,
                    CASE
                        WHEN media_type IN ('audio', 'speech', 'protected_audio') THEN 0
                        ELSE 1
                    END,
                    published_at DESC,
                    CASE
                        WHEN status = 'pending' THEN 0
                        ELSE 1
                    END,
                    job_id ASC
                LIMIT ?
                """,
                params,
            ).fetchall()
        return [dict(row) for row in rows]

    def get_lizhi_rescue_jobs(
        self,
        *,
        limit: int,
        job_ids: Optional[List[str]] = None,
        statuses: Optional[List[str]] = None,
    ) -> List[dict]:
        active_statuses = list(statuses or ["unsupported", "error"])
        if not active_statuses:
            return []

        status_placeholders = ", ".join("?" for _ in active_statuses)
        parameters: List[object] = [*active_statuses]

        conditions = [
            f"status IN ({status_placeholders})",
            "media_url LIKE 'https://cdn.lizhi.fm/%'",
        ]

        if job_ids:
            job_placeholders = ", ".join("?" for _ in job_ids)
            conditions.append(f"job_id IN ({job_placeholders})")
            parameters.extend(job_ids)

        parameters.append(limit)

        with self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT *
                FROM media_jobs
                WHERE {' AND '.join(conditions)}
                ORDER BY
                    CASE
                        WHEN status = 'error' THEN 0
                        ELSE 1
                    END,
                    CASE
                        WHEN duration IS NULL THEN 1
                        ELSE 0
                    END,
                    duration ASC,
                    published_at DESC,
                    job_id ASC
                LIMIT ?
                """,
                parameters,
            ).fetchall()
        return [dict(row) for row in rows]

    def get_transcription_jobs(
        self,
        *,
        limit: int,
        job_ids: Optional[List[str]] = None,
        include_errors: bool = False,
    ) -> List[dict]:
        transcript_status_clause = (
            "transcript_status IN ('pending', 'error')" if include_errors else "transcript_status = 'pending'"
        )
        with self._connect() as connection:
            if job_ids:
                placeholders = ", ".join("?" for _ in job_ids)
                rows = connection.execute(
                    f"""
                    SELECT *
                    FROM media_jobs
                    WHERE status = 'downloaded'
                      AND {transcript_status_clause}
                      AND job_id IN ({placeholders})
                    ORDER BY
                      CASE WHEN duration IS NULL THEN 1 ELSE 0 END,
                      duration ASC,
                      published_at DESC,
                      job_id ASC
                    LIMIT ?
                    """,
                    (*job_ids, limit),
                ).fetchall()
            else:
                rows = connection.execute(
                    f"""
                    SELECT *
                    FROM media_jobs
                    WHERE status = 'downloaded'
                      AND {transcript_status_clause}
                    ORDER BY
                      CASE WHEN duration IS NULL THEN 1 ELSE 0 END,
                      duration ASC,
                      published_at DESC,
                      job_id ASC
                    LIMIT ?
                    """,
                    (limit,),
                ).fetchall()
        return [dict(row) for row in rows]

    def get_failed_transcription_jobs(
        self,
        *,
        limit: int,
        job_ids: Optional[List[str]] = None,
    ) -> List[dict]:
        with self._connect() as connection:
            if job_ids:
                placeholders = ", ".join("?" for _ in job_ids)
                rows = connection.execute(
                    f"""
                    SELECT *
                    FROM media_jobs
                    WHERE status = 'downloaded'
                      AND content_type = 'radios'
                      AND media_type = 'audio'
                      AND transcript_status = 'error'
                      AND job_id IN ({placeholders})
                    ORDER BY
                      updated_at DESC,
                      CASE WHEN duration IS NULL THEN 1 ELSE 0 END,
                      duration DESC,
                      published_at DESC,
                      job_id ASC
                    LIMIT ?
                    """,
                    (*job_ids, limit),
                ).fetchall()
            else:
                rows = connection.execute(
                    """
                    SELECT *
                    FROM media_jobs
                    WHERE status = 'downloaded'
                      AND content_type = 'radios'
                      AND media_type = 'audio'
                      AND transcript_status = 'error'
                    ORDER BY
                      updated_at DESC,
                      CASE WHEN duration IS NULL THEN 1 ELSE 0 END,
                      duration DESC,
                      published_at DESC,
                      job_id ASC
                    LIMIT ?
                    """,
                    (limit,),
                ).fetchall()
        return [dict(row) for row in rows]

    def get_submitted_transcription_jobs(self, *, limit: int, job_ids: Optional[List[str]] = None) -> List[dict]:
        with self._connect() as connection:
            if job_ids:
                placeholders = ", ".join("?" for _ in job_ids)
                rows = connection.execute(
                    f"""
                    SELECT *
                    FROM media_jobs
                    WHERE transcript_status = 'submitted'
                      AND job_id IN ({placeholders})
                    ORDER BY published_at DESC, job_id ASC
                    LIMIT ?
                    """,
                    (*job_ids, limit),
                ).fetchall()
            else:
                rows = connection.execute(
                    """
                    SELECT *
                    FROM media_jobs
                    WHERE transcript_status = 'submitted'
                    ORDER BY published_at DESC, job_id ASC
                    LIMIT ?
                    """,
                    (limit,),
                ).fetchall()
        return [dict(row) for row in rows]

    def get_remote_transcription_jobs(
        self,
        *,
        limit: int,
        job_ids: Optional[List[str]] = None,
        include_errors: bool = False,
    ) -> List[dict]:
        transcript_status_clause = (
            "transcript_status IN ('pending', 'error')" if include_errors else "transcript_status = 'pending'"
        )
        with self._connect() as connection:
            if job_ids:
                placeholders = ", ".join("?" for _ in job_ids)
                rows = connection.execute(
                    f"""
                    SELECT *
                    FROM media_jobs
                    WHERE media_type IN ('audio', 'speech')
                      AND media_url IS NOT NULL
                      AND {transcript_status_clause}
                      AND job_id IN ({placeholders})
                    ORDER BY published_at DESC, job_id ASC
                    LIMIT ?
                    """,
                    (*job_ids, limit),
                ).fetchall()
            else:
                rows = connection.execute(
                    f"""
                    SELECT *
                    FROM media_jobs
                    WHERE media_type IN ('audio', 'speech')
                      AND media_url IS NOT NULL
                      AND {transcript_status_clause}
                    ORDER BY published_at DESC, job_id ASC
                    LIMIT ?
                    """,
                    (limit,),
                ).fetchall()
        return [dict(row) for row in rows]

    def mark_media_downloaded(self, *, job_id: str, downloaded_path: str, file_size: int) -> None:
        updated_at = utc_now_iso()
        def action(connection: sqlite3.Connection) -> None:
            connection.execute(
                """
                UPDATE media_jobs
                SET
                    status = 'downloaded',
                    last_error = NULL,
                    file_size = ?,
                    downloaded_path = ?,
                    downloaded_at = ?,
                    updated_at = ?
                WHERE job_id = ?
                """,
                (file_size, downloaded_path, updated_at, updated_at, job_id),
            )
        self._write_with_retry(action)

    def mark_media_download_error(self, *, job_id: str, error: str, status: str = "error") -> None:
        updated_at = utc_now_iso()
        def action(connection: sqlite3.Connection) -> None:
            connection.execute(
                """
                UPDATE media_jobs
                SET
                    status = ?,
                    last_error = ?,
                    transcript_status = CASE
                        WHEN ? = 'unsupported'
                         AND media_type IN ('audio', 'protected_audio', 'speech')
                         AND transcript_status = 'pending'
                        THEN 'skipped'
                        ELSE transcript_status
                    END,
                    transcript_error = CASE
                        WHEN ? = 'unsupported'
                         AND media_type IN ('audio', 'protected_audio', 'speech')
                         AND transcript_status = 'pending'
                        THEN ?
                        ELSE transcript_error
                    END,
                    updated_at = ?
                WHERE job_id = ?
                """,
                (status, error, status, status, error, updated_at, job_id),
            )
        self._write_with_retry(action)

    def update_media_job_source(
        self,
        *,
        job_id: str,
        media_url: str,
        local_relpath: str,
        provider: str,
        access_mode: str = "audio_url",
        download_method: str = "direct",
        status: str = "pending",
        last_error: Optional[str] = None,
    ) -> None:
        updated_at = utc_now_iso()
        def action(connection: sqlite3.Connection) -> None:
            connection.execute(
                """
                UPDATE media_jobs
                SET
                    media_url = ?,
                    local_relpath = ?,
                    provider = ?,
                    access_mode = ?,
                    download_method = ?,
                    status = ?,
                    last_error = ?,
                    file_size = NULL,
                    downloaded_path = NULL,
                    downloaded_at = NULL,
                    transcript_status = CASE
                        WHEN ? = 'pending'
                         AND media_type IN ('audio', 'protected_audio', 'speech')
                        THEN 'pending'
                        ELSE transcript_status
                    END,
                    transcript_error = CASE
                        WHEN ? = 'pending'
                         AND media_type IN ('audio', 'protected_audio', 'speech')
                        THEN NULL
                        ELSE transcript_error
                    END,
                    updated_at = ?
                WHERE job_id = ?
                """,
                (
                    media_url,
                    local_relpath,
                    provider,
                    access_mode,
                    download_method,
                    status,
                    last_error,
                    status,
                    status,
                    updated_at,
                    job_id,
                ),
            )
        self._write_with_retry(action)

    def save_transcript(
        self,
        *,
        job: dict,
        transcript_path: str,
        transcript_text: str,
        transcript_segments: Optional[List[dict]],
        model_name: Optional[str],
        language: Optional[str],
        documents: List[dict],
    ) -> None:
        transcript_id = str(job["job_id"])
        item_key = item_key_for(job["content_type"], job["item_id"])
        updated_at = utc_now_iso()

        def action(connection: sqlite3.Connection) -> None:
            connection.execute(
                """
                INSERT INTO transcripts (
                    transcript_id,
                    job_id,
                    item_key,
                    content_type,
                    item_id,
                    segment_type,
                    segment_id,
                    segment_title,
                    language,
                    model_name,
                    text,
                    segments_json,
                    transcript_path,
                    created_at,
                    updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(transcript_id) DO UPDATE SET
                    language = excluded.language,
                    model_name = excluded.model_name,
                    text = excluded.text,
                    segments_json = excluded.segments_json,
                    transcript_path = excluded.transcript_path,
                    updated_at = excluded.updated_at
                """,
                (
                    transcript_id,
                    job["job_id"],
                    item_key,
                    job["content_type"],
                    job["item_id"],
                    job["segment_type"],
                    job["segment_id"],
                    job.get("segment_title"),
                    language,
                    model_name,
                    transcript_text,
                    json.dumps(transcript_segments or [], ensure_ascii=False),
                    transcript_path,
                    updated_at,
                    updated_at,
                ),
            )

            connection.execute(
                "DELETE FROM chunks WHERE transcript_id = ?",
                (transcript_id,),
            )
            if documents:
                connection.executemany(
                    """
                    INSERT INTO chunks (
                        chunk_id,
                        item_key,
                        transcript_id,
                        source,
                        content_type,
                        item_id,
                        segment_type,
                        segment_id,
                        chunk_index,
                        segment_title,
                        segment_url,
                        published_at,
                        text,
                        metadata_json,
                        updated_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    [
                        chunk_row_from_document(
                            document=document,
                            item_key=item_key,
                            transcript_id=transcript_id,
                            updated_at=updated_at,
                        )
                        for document in documents
                    ],
                )

            connection.execute(
                """
                UPDATE media_jobs
                SET
                    transcript_status = 'completed',
                    transcript_error = NULL,
                    transcript_path = ?,
                    transcribed_at = ?,
                    updated_at = ?
                WHERE job_id = ?
                """,
                (transcript_path, updated_at, updated_at, job["job_id"]),
            )
        self._write_with_retry(action)

    def mark_transcript_submitted(self, *, job_id: str, transcript_path: Optional[str] = None) -> None:
        updated_at = utc_now_iso()
        def action(connection: sqlite3.Connection) -> None:
            connection.execute(
                """
                UPDATE media_jobs
                SET
                    transcript_status = 'submitted',
                    transcript_error = NULL,
                    transcript_path = COALESCE(?, transcript_path),
                    updated_at = ?
                WHERE job_id = ?
                """,
                (transcript_path, updated_at, job_id),
            )
        self._write_with_retry(action)

    def mark_transcript_error(self, *, job_id: str, error: str) -> None:
        updated_at = utc_now_iso()
        def action(connection: sqlite3.Connection) -> None:
            connection.execute(
                """
                UPDATE media_jobs
                SET
                    transcript_status = 'error',
                    transcript_error = ?,
                    updated_at = ?
                WHERE job_id = ?
                """,
                (error, updated_at, job_id),
            )
        self._write_with_retry(action)

    def get_state(self, state_key: str) -> Optional[dict]:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT payload_json FROM sync_state WHERE state_key = ?",
                (state_key,),
            ).fetchone()
        if not row:
            return None
        return json.loads(row["payload_json"])

    def set_state(self, state_key: str, payload: dict) -> None:
        updated_at = utc_now_iso()
        def action(connection: sqlite3.Connection) -> None:
            connection.execute(
                """
                INSERT INTO sync_state (state_key, payload_json, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(state_key) DO UPDATE SET
                    payload_json = excluded.payload_json,
                    updated_at = excluded.updated_at
                """,
                (state_key, json.dumps(payload, ensure_ascii=False), updated_at),
            )
        self._write_with_retry(action)

    def delete_state(self, state_key: str) -> None:
        with self._connect() as connection:
            connection.execute(
                "DELETE FROM sync_state WHERE state_key = ?",
                (state_key,),
            )
            connection.commit()

    def get_watermark(self, *, content_type: str, official_radios_only: bool) -> Optional[dict]:
        return self.get_state(watermark_state_key(content_type, official_radios_only))

    def set_watermark(
        self,
        *,
        content_type: str,
        official_radios_only: bool,
        published_at: str,
        item_ids: List[str],
    ) -> None:
        self.set_state(
            watermark_state_key(content_type, official_radios_only),
            {
                "content_type": content_type,
                "official_radios_only": official_radios_only,
                "published_at": published_at,
                "item_ids": item_ids,
            },
        )

    def stats(self) -> dict:
        with self._connect() as connection:
            items = scalar(connection, "SELECT COUNT(*) FROM items")
            chunks = scalar(connection, "SELECT COUNT(*) FROM chunks")
            transcripts = scalar(connection, "SELECT COUNT(*) FROM transcripts")
            media_jobs = scalar(connection, "SELECT COUNT(*) FROM media_jobs")
            sync_states = scalar(connection, "SELECT COUNT(*) FROM sync_state")
            search_documents = scalar(connection, "SELECT COUNT(*) FROM search_documents")
            enrichment_jobs = scalar(connection, "SELECT COUNT(*) FROM enrichment_jobs")
            media_status = grouped_counts(connection, "SELECT status, COUNT(*) AS c FROM media_jobs GROUP BY status")
            transcript_status = grouped_counts(
                connection,
                "SELECT transcript_status, COUNT(*) AS c FROM media_jobs GROUP BY transcript_status",
            )
            search_doc_types = grouped_counts(
                connection,
                "SELECT doc_type, COUNT(*) AS c FROM search_documents GROUP BY doc_type",
            )
            enrichment_status = grouped_counts(
                connection,
                "SELECT status, COUNT(*) AS c FROM enrichment_jobs GROUP BY status",
            )

        return {
            "db_path": str(self.db_path),
            "items": items,
            "chunks": chunks,
            "media_jobs": media_jobs,
            "transcripts": transcripts,
            "sync_states": sync_states,
            "search_documents": search_documents,
            "enrichment_jobs": enrichment_jobs,
            "media_status": media_status,
            "transcript_status": transcript_status,
            "search_doc_types": search_doc_types,
            "enrichment_status": enrichment_status,
        }

    def recent_failures(self, *, limit: int = 10) -> dict:
        with self._connect() as connection:
            download_rows = connection.execute(
                """
                SELECT
                    job_id,
                    content_type,
                    item_id,
                    media_title,
                    last_error AS error,
                    updated_at
                FROM media_jobs
                WHERE status = 'error' AND last_error IS NOT NULL
                ORDER BY updated_at DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
            transcript_rows = connection.execute(
                """
                SELECT
                    job_id,
                    content_type,
                    item_id,
                    media_title,
                    transcript_error AS error,
                    updated_at
                FROM media_jobs
                WHERE transcript_status = 'error' AND transcript_error IS NOT NULL
                ORDER BY updated_at DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()

        return {
            "download_errors": [dict(row) for row in download_rows],
            "transcript_errors": [dict(row) for row in transcript_rows],
        }

    def radio_audio_status(self) -> dict:
        with self._connect() as connection:
            media_status = grouped_counts(
                connection,
                """
                SELECT status, COUNT(*) AS c
                FROM media_jobs
                WHERE content_type = 'radios' AND media_type = 'audio'
                GROUP BY status
                """,
            )
            transcript_status = grouped_counts(
                connection,
                """
                SELECT transcript_status, COUNT(*) AS c
                FROM media_jobs
                WHERE content_type = 'radios' AND media_type = 'audio'
                GROUP BY transcript_status
                """,
            )
            pending_hours = scalar(
                connection,
                """
                SELECT ROUND(COALESCE(SUM(duration), 0) / 3600.0, 2)
                FROM media_jobs
                WHERE content_type = 'radios'
                  AND media_type = 'audio'
                  AND transcript_status = 'pending'
                """,
            )
            completed_hours = scalar(
                connection,
                """
                SELECT ROUND(COALESCE(SUM(duration), 0) / 3600.0, 2)
                FROM media_jobs
                WHERE content_type = 'radios'
                  AND media_type = 'audio'
                  AND transcript_status = 'completed'
                """,
            )
            latest_rows = connection.execute(
                """
                SELECT
                    job_id,
                    transcript_status,
                    status,
                    item_title,
                    updated_at,
                    transcribed_at
                FROM media_jobs
                WHERE content_type = 'radios' AND media_type = 'audio'
                ORDER BY updated_at DESC
                LIMIT 5
                """
            ).fetchall()
            latest_transcript = connection.execute(
                """
                SELECT
                    job_id,
                    item_title,
                    transcript_path,
                    updated_at,
                    transcribed_at
                FROM media_jobs
                WHERE content_type = 'radios'
                  AND media_type = 'audio'
                  AND transcript_status = 'completed'
                ORDER BY COALESCE(transcribed_at, updated_at) DESC
                LIMIT 1
                """
            ).fetchone()

        return {
            "audio_media_status": media_status,
            "audio_transcript_status": transcript_status,
            "pending_audio_hours": pending_hours or 0.0,
            "completed_audio_hours": completed_hours or 0.0,
            "latest_audio_jobs": [dict(row) for row in latest_rows],
            "latest_completed_transcript": dict(latest_transcript) if latest_transcript else None,
        }

    def clear_search_documents(self, *, collection_name: Optional[str] = None) -> None:
        def action(connection: sqlite3.Connection) -> None:
            if use_legacy_search_documents(collection_name):
                connection.execute("DELETE FROM search_documents")
            else:
                connection.execute(
                    "DELETE FROM search_documents_scoped WHERE collection_name = ?",
                    (normalize_search_collection_name(collection_name),),
                )

        self._write_with_retry(action)

    def get_radio_items_for_search(
        self,
        *,
        updated_after: Optional[str] = None,
        limit: Optional[int] = None,
        item_keys: Optional[Iterable[str]] = None,
        item_ids: Optional[Iterable[str]] = None,
        eligible_only: bool = True,
        require_transcript: bool = True,
        collection_name: Optional[str] = None,
    ) -> List[dict]:
        with self._connect() as connection:
            params: list[object] = []
            query = """
                SELECT
                    i.*,
                    MAX(t.updated_at) AS transcript_updated_at,
                    MAX(m.transcribed_at) AS transcribed_at
                FROM items i
                LEFT JOIN transcripts t ON t.item_key = i.item_key
                LEFT JOIN media_jobs m ON m.item_key = i.item_key
                WHERE i.content_type = 'radios'
            """
            if eligible_only:
                query += """
                    AND COALESCE(i.owner_type, '') = 'gcores'
                    AND COALESCE(i.option_is_official, 0) = 1
                """
            if require_transcript:
                query += """
                    AND EXISTS (
                        SELECT 1
                        FROM transcripts tx_ready
                        WHERE tx_ready.item_key = i.item_key
                    )
                """
            if item_keys:
                item_keys_list = [str(value) for value in item_keys if str(value)]
                if item_keys_list:
                    query += f"""
                        AND i.item_key IN ({", ".join("?" for _ in item_keys_list)})
                    """
                    params.extend(item_keys_list)
            if item_ids:
                item_ids_list = [str(value) for value in item_ids if str(value)]
                if item_ids_list:
                    query += f"""
                        AND i.item_id IN ({", ".join("?" for _ in item_ids_list)})
                    """
                    params.extend(item_ids_list)
            if updated_after:
                if use_legacy_search_documents(collection_name):
                    query += """
                        AND (
                            i.updated_at > ?
                            OR EXISTS (
                                SELECT 1
                                FROM transcripts tx
                                WHERE tx.item_key = i.item_key AND tx.updated_at > ?
                            )
                            OR NOT EXISTS (
                                SELECT 1
                                FROM search_documents sd
                                WHERE sd.item_key = i.item_key
                                  AND sd.doc_type = 'item_title'
                            )
                        )
                    """
                    params.extend([updated_after, updated_after])
                else:
                    query += """
                        AND (
                            i.updated_at > ?
                            OR EXISTS (
                                SELECT 1
                                FROM transcripts tx
                                WHERE tx.item_key = i.item_key AND tx.updated_at > ?
                            )
                            OR NOT EXISTS (
                                SELECT 1
                                FROM search_documents_scoped sd
                                WHERE sd.collection_name = ?
                                  AND sd.item_key = i.item_key
                                  AND sd.doc_type = 'item_title'
                            )
                        )
                    """
                    params.extend(
                        [
                            updated_after,
                            updated_after,
                            normalize_search_collection_name(collection_name),
                        ]
                    )
            query += """
                GROUP BY i.item_key
                ORDER BY
                    CASE WHEN MAX(t.updated_at) IS NULL THEN 1 ELSE 0 END,
                    COALESCE(MAX(t.updated_at), i.updated_at) DESC,
                    i.item_key ASC
            """
            if limit:
                query += " LIMIT ?"
                params.append(limit)
            rows = connection.execute(query, params).fetchall()
        return [dict(row) for row in rows]

    def get_item_record(self, *, item_key: str) -> Optional[dict]:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT *
                FROM items
                WHERE item_key = ?
                """,
                (item_key,),
            ).fetchone()
        return dict(row) if row else None

    def list_radio_participants(self, *, limit: Optional[int] = None) -> List[dict]:
        counter: Counter[str] = Counter()
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT users_json
                FROM items
                WHERE content_type = 'radios'
                  AND users_json IS NOT NULL
                """
            ).fetchall()
        for row in rows:
            try:
                users = json.loads(row["users_json"] or "[]")
            except Exception:
                continue
            for user in users:
                if isinstance(user, str):
                    name = user.strip()
                    if name:
                        counter[name] += 1
        values = [{"name": name, "count": count} for name, count in counter.most_common(limit or None)]
        return values

    def list_radio_categories(self, *, limit: Optional[int] = None) -> List[dict]:
        query = """
            SELECT TRIM(category) AS name, COUNT(*) AS count
            FROM items
            WHERE content_type = 'radios'
              AND category IS NOT NULL
              AND TRIM(category) <> ''
            GROUP BY TRIM(category)
            ORDER BY count DESC, name COLLATE NOCASE ASC
        """
        params: list[object] = []
        if limit is not None:
            query += "\nLIMIT ?"
            params.append(int(limit))
        with self._connect() as connection:
            rows = connection.execute(query, params).fetchall()
        return [{"name": str(row["name"]), "count": int(row["count"])} for row in rows if row["name"]]

    def get_item_transcripts(self, *, item_key: str) -> List[dict]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT *
                FROM transcripts
                WHERE item_key = ?
                ORDER BY updated_at ASC, transcript_id ASC
                """,
                (item_key,),
            ).fetchall()
        return [dict(row) for row in rows]

    def replace_search_documents_for_item(
        self,
        *,
        item_key: str,
        documents: List[dict],
        collection_name: Optional[str] = None,
        target_doc_types: Optional[Iterable[str]] = None,
    ) -> None:
        updated_at = utc_now_iso()
        doc_types = [str(value) for value in (target_doc_types or []) if str(value)]

        def action(connection: sqlite3.Connection) -> None:
            if use_legacy_search_documents(collection_name):
                if doc_types:
                    placeholders = ", ".join("?" for _ in doc_types)
                    connection.execute(
                        f"DELETE FROM search_documents WHERE item_key = ? AND doc_type IN ({placeholders})",
                        (item_key, *doc_types),
                    )
                else:
                    connection.execute("DELETE FROM search_documents WHERE item_key = ?", (item_key,))
                if documents:
                    connection.executemany(
                        """
                        INSERT INTO search_documents (
                            doc_id,
                            item_key,
                            parent_doc_id,
                            doc_type,
                            content_type,
                            item_id,
                            item_title,
                            doc_index,
                            title,
                            start_ms,
                            end_ms,
                            timeline_ms,
                            text_raw,
                            text_normalized,
                            vector_text,
                            keyword_text,
                            text_search,
                            payload_json,
                            source_updated_at,
                            updated_at
                        )
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        [
                            search_document_row(document=document, item_key=item_key, updated_at=updated_at)
                            for document in documents
                        ],
                    )
                return

            scoped_collection = normalize_search_collection_name(collection_name)
            if doc_types:
                placeholders = ", ".join("?" for _ in doc_types)
                connection.execute(
                    f"DELETE FROM search_documents_scoped WHERE collection_name = ? AND item_key = ? AND doc_type IN ({placeholders})",
                    (scoped_collection, item_key, *doc_types),
                )
            else:
                connection.execute(
                    "DELETE FROM search_documents_scoped WHERE collection_name = ? AND item_key = ?",
                    (scoped_collection, item_key),
                )
            if documents:
                connection.executemany(
                    """
                    INSERT INTO search_documents_scoped (
                        collection_name,
                        doc_id,
                        item_key,
                        parent_doc_id,
                        doc_type,
                        content_type,
                        item_id,
                        item_title,
                        doc_index,
                        title,
                        start_ms,
                        end_ms,
                        timeline_ms,
                        text_raw,
                        text_normalized,
                        vector_text,
                        keyword_text,
                        text_search,
                        payload_json,
                        source_updated_at,
                        updated_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    [
                        scoped_search_document_row(
                            collection_name=scoped_collection,
                            document=document,
                            item_key=item_key,
                            updated_at=updated_at,
                        )
                        for document in documents
                    ],
                )

        self._write_with_retry(action)

    def get_search_documents_for_item(
        self,
        *,
        item_key: Optional[str] = None,
        item_id: Optional[str] = None,
        doc_types: Optional[Iterable[str]] = None,
        collection_name: Optional[str] = None,
    ) -> List[dict]:
        clauses: list[str] = []
        params: list[object] = []
        table_name = "search_documents"
        if not use_legacy_search_documents(collection_name):
            table_name = "search_documents_scoped"
            clauses.append("collection_name = ?")
            params.append(normalize_search_collection_name(collection_name))
        if item_key:
            clauses.append("item_key = ?")
            params.append(item_key)
        if item_id:
            clauses.append("item_id = ?")
            params.append(item_id)
        if doc_types:
            doc_types_list = list(doc_types)
            if doc_types_list:
                clauses.append(f"doc_type IN ({', '.join('?' for _ in doc_types_list)})")
                params.extend(doc_types_list)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        query = f"""
            SELECT *
            FROM {table_name}
            {where}
            ORDER BY item_key ASC, doc_type ASC, doc_index ASC
        """
        with self._connect() as connection:
            rows = connection.execute(query, params).fetchall()
        return [dict(row) for row in rows]

    def get_search_documents_by_doc_ids(
        self,
        *,
        doc_ids: Iterable[str],
        collection_name: Optional[str] = None,
    ) -> List[dict]:
        doc_id_list = [str(value) for value in doc_ids if str(value)]
        if not doc_id_list:
            return []
        if use_legacy_search_documents(collection_name):
            query = f"""
                SELECT *
                FROM search_documents
                WHERE doc_id IN ({', '.join('?' for _ in doc_id_list)})
            """
            params: list[object] = list(doc_id_list)
        else:
            query = f"""
                SELECT *
                FROM search_documents_scoped
                WHERE collection_name = ?
                  AND doc_id IN ({', '.join('?' for _ in doc_id_list)})
            """
            params = [normalize_search_collection_name(collection_name), *doc_id_list]
        with self._connect() as connection:
            rows = connection.execute(query, params).fetchall()
        by_id = {str(row["doc_id"]): dict(row) for row in rows}
        return [by_id[doc_id] for doc_id in doc_id_list if doc_id in by_id]

    def get_evidence_atoms_by_item_key_and_doc_index_range(
        self,
        *,
        item_key: str,
        start_index: int,
        end_index: int,
        collection_name: Optional[str] = None,
    ) -> List[dict]:
        if use_legacy_search_documents(collection_name):
            query = """
                SELECT *
                FROM search_documents
                WHERE item_key = ?
                  AND doc_type = 'evidence_atom'
                  AND doc_index BETWEEN ? AND ?
                ORDER BY doc_index ASC
            """
            params: tuple[object, ...] = (item_key, start_index, end_index)
        else:
            query = """
                SELECT *
                FROM search_documents_scoped
                WHERE collection_name = ?
                  AND item_key = ?
                  AND doc_type = 'evidence_atom'
                  AND doc_index BETWEEN ? AND ?
                ORDER BY doc_index ASC
            """
            params = (normalize_search_collection_name(collection_name), item_key, start_index, end_index)
        with self._connect() as connection:
            rows = connection.execute(query, params).fetchall()
        return [dict(row) for row in rows]

    def get_evidence_atoms_by_item_key_and_time_range(
        self,
        *,
        item_key: str,
        start_ms: int,
        end_ms: int,
        collection_name: Optional[str] = None,
    ) -> List[dict]:
        if use_legacy_search_documents(collection_name):
            query = """
                SELECT *
                FROM search_documents
                WHERE item_key = ?
                  AND doc_type = 'evidence_atom'
                  AND start_ms IS NOT NULL
                  AND end_ms IS NOT NULL
                  AND start_ms <= ?
                  AND end_ms >= ?
                ORDER BY doc_index ASC
            """
            params: tuple[object, ...] = (item_key, end_ms, start_ms)
        else:
            query = """
                SELECT *
                FROM search_documents_scoped
                WHERE collection_name = ?
                  AND item_key = ?
                  AND doc_type = 'evidence_atom'
                  AND start_ms IS NOT NULL
                  AND end_ms IS NOT NULL
                  AND start_ms <= ?
                  AND end_ms >= ?
                ORDER BY doc_index ASC
            """
            params = (normalize_search_collection_name(collection_name), item_key, end_ms, start_ms)
        with self._connect() as connection:
            rows = connection.execute(query, params).fetchall()
        return [dict(row) for row in rows]

    def start_enrichment_job(
        self,
        *,
        job_key: str,
        item_key: str,
        job_type: str,
        transcript_id: Optional[str] = None,
        payload: Optional[dict] = None,
    ) -> None:
        updated_at = utc_now_iso()

        def action(connection: sqlite3.Connection) -> None:
            connection.execute(
                """
                INSERT INTO enrichment_jobs (
                    job_key,
                    item_key,
                    transcript_id,
                    job_type,
                    status,
                    attempt_count,
                    last_error,
                    payload_json,
                    updated_at,
                    completed_at
                )
                VALUES (?, ?, ?, ?, 'running', 1, NULL, ?, ?, NULL)
                ON CONFLICT(job_key) DO UPDATE SET
                    item_key = excluded.item_key,
                    transcript_id = excluded.transcript_id,
                    job_type = excluded.job_type,
                    status = 'running',
                    attempt_count = enrichment_jobs.attempt_count + 1,
                    last_error = NULL,
                    payload_json = excluded.payload_json,
                    updated_at = excluded.updated_at,
                    completed_at = NULL
                """,
                (
                    job_key,
                    item_key,
                    transcript_id,
                    job_type,
                    json.dumps(payload or {}, ensure_ascii=False),
                    updated_at,
                ),
            )

        self._write_with_retry(action)

    def complete_enrichment_job(self, *, job_key: str, payload: Optional[dict] = None) -> None:
        updated_at = utc_now_iso()

        def action(connection: sqlite3.Connection) -> None:
            connection.execute(
                """
                UPDATE enrichment_jobs
                SET
                    status = 'completed',
                    last_error = NULL,
                    payload_json = ?,
                    updated_at = ?,
                    completed_at = ?
                WHERE job_key = ?
                """,
                (json.dumps(payload or {}, ensure_ascii=False), updated_at, updated_at, job_key),
            )

        self._write_with_retry(action)

    def mark_enrichment_job_error(self, *, job_key: str, error: str, payload: Optional[dict] = None) -> None:
        updated_at = utc_now_iso()

        def action(connection: sqlite3.Connection) -> None:
            connection.execute(
                """
                UPDATE enrichment_jobs
                SET
                    status = 'error',
                    last_error = ?,
                    payload_json = COALESCE(?, payload_json),
                    updated_at = ?,
                    completed_at = NULL
                WHERE job_key = ?
                """,
                (
                    error,
                    json.dumps(payload, ensure_ascii=False) if payload is not None else None,
                    updated_at,
                    job_key,
                ),
            )

        self._write_with_retry(action)


def scalar(connection: sqlite3.Connection, query: str) -> int:
    row = connection.execute(query).fetchone()
    return int(row[0]) if row else 0


def grouped_counts(connection: sqlite3.Connection, query: str) -> Dict[str, int]:
    return {
        str(row[0]): int(row[1])
        for row in connection.execute(query).fetchall()
        if row[0] is not None
    }


def bool_to_int(value: object) -> Optional[int]:
    if value is None:
        return None
    return 1 if bool(value) else 0


def item_key_for(content_type: object, item_id: object) -> str:
    return f"{content_type}:{item_id}"


def watermark_state_key(content_type: str, official_radios_only: bool) -> str:
    scope = "official" if official_radios_only else "all"
    return f"watermark:{content_type}:{scope}"


def chunk_row_from_document(
    *,
    document: dict,
    item_key: str,
    transcript_id: Optional[str],
    updated_at: str,
) -> tuple:
    metadata = {
        "item_title": document.get("item_title"),
        "category": document.get("category"),
        "tags": document.get("tags", []),
        "users": document.get("users", []),
        "is_free": document.get("is_free"),
        "chapter_index": document.get("chapter_index"),
        "model_name": document.get("model_name"),
        "language": document.get("language"),
    }
    return (
        document.get("chunk_id"),
        item_key,
        transcript_id,
        document.get("source"),
        document.get("content_type"),
        document.get("item_id"),
        document.get("segment_type"),
        str(document.get("segment_id")),
        int(document.get("chunk_index", 0)),
        document.get("segment_title"),
        document.get("segment_url"),
        document.get("published_at"),
        document.get("text"),
        json.dumps(metadata, ensure_ascii=False),
        updated_at,
    )


def search_document_row(
    *,
    document: dict,
    item_key: str,
    updated_at: str,
) -> tuple:
    payload = dict(document.get("payload") or {})
    payload["doc_id"] = document.get("doc_id")
    payload["doc_type"] = document.get("doc_type")
    payload["item_key"] = item_key
    vector_text, keyword_text, text_search = resolved_search_texts(document)
    return (
        document.get("doc_id"),
        item_key,
        document.get("parent_doc_id"),
        document.get("doc_type"),
        document.get("content_type"),
        document.get("item_id"),
        document.get("item_title"),
        int(document.get("doc_index", 0)),
        document.get("title"),
        document.get("start_ms"),
        document.get("end_ms"),
        document.get("timeline_ms"),
        document.get("text_raw") or "",
        document.get("text_normalized"),
        vector_text,
        keyword_text,
        text_search,
        json.dumps(payload, ensure_ascii=False),
        document.get("source_updated_at"),
        updated_at,
    )


def scoped_search_document_row(
    *,
    collection_name: str,
    document: dict,
    item_key: str,
    updated_at: str,
) -> tuple:
    payload = dict(document.get("payload") or {})
    payload["doc_id"] = document.get("doc_id")
    payload["doc_type"] = document.get("doc_type")
    payload["item_key"] = item_key
    payload["collection_name"] = collection_name
    vector_text, keyword_text, text_search = resolved_search_texts(document)
    return (
        collection_name,
        document.get("doc_id"),
        item_key,
        document.get("parent_doc_id"),
        document.get("doc_type"),
        document.get("content_type"),
        document.get("item_id"),
        document.get("item_title"),
        int(document.get("doc_index", 0)),
        document.get("title"),
        document.get("start_ms"),
        document.get("end_ms"),
        document.get("timeline_ms"),
        document.get("text_raw") or "",
        document.get("text_normalized"),
        vector_text,
        keyword_text,
        text_search,
        json.dumps(payload, ensure_ascii=False),
        document.get("source_updated_at"),
        updated_at,
    )


def resolved_search_texts(document: dict) -> tuple[str, str, str]:
    raw_text = str(document.get("text_raw") or "")
    normalized_text = str(document.get("text_normalized") or raw_text)
    keyword_text = str(document.get("keyword_text") or document.get("text_search") or normalized_text or raw_text)
    vector_text = str(document.get("vector_text") or keyword_text or normalized_text or raw_text)
    text_search = str(document.get("text_search") or keyword_text)
    return vector_text, keyword_text, text_search


def media_job_row(
    *,
    item_key: str,
    job: dict,
    preserved: dict,
    updated_at: str,
) -> tuple:
    return (
        job.get("job_id"),
        item_key,
        job.get("content_type"),
        job.get("item_id"),
        job.get("item_title"),
        job.get("segment_type"),
        str(job.get("segment_id")),
        job.get("segment_title"),
        job.get("segment_url"),
        job.get("published_at"),
        job.get("chapter_index"),
        str(job.get("media_id")),
        job.get("media_title"),
        job.get("media_type"),
        job.get("provider"),
        job.get("access_mode"),
        job.get("media_url"),
        job.get("playlist_url"),
        job.get("play_auth"),
        job.get("duration"),
        job.get("download_method"),
        job.get("local_relpath"),
        preserved.get("status", job.get("status", "pending")),
        preserved.get("last_error"),
        preserved.get("file_size"),
        preserved.get("downloaded_path"),
        preserved.get("downloaded_at"),
        preserved.get("transcript_status", job.get("transcript_status", "pending")),
        preserved.get("transcript_error"),
        preserved.get("transcript_path"),
        preserved.get("transcribed_at"),
        updated_at,
    )


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
