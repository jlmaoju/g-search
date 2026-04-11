from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterable, Iterator, List, Optional
from urllib.parse import urlparse

from .catalog import Catalog
from .daily_runtime import get_daily_runtime_reporter


SENTENCE_BREAK_RE = re.compile(r"(?<=[\u3002\uff01\uff1f!?；;])")


def snapshot_updated_at(path: Path) -> str:
    return datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc).isoformat()


def prepare_vector_inputs(
    root: str = "data",
    *,
    max_chars: int = 800,
    write_jsonl: bool = True,
    use_sqlite: bool = True,
    content_types: Optional[List[str]] = None,
    official_radios_only: bool = True,
    include_article_speech: bool = False,
) -> dict:
    root_path = Path(root)
    normalized_root = root_path / "normalized"
    if not normalized_root.exists():
        raise FileNotFoundError(f"No normalized crawl output found under {normalized_root}")

    index_root = root_path / "index"
    index_root.mkdir(parents=True, exist_ok=True)

    vector_path = index_root / "vector_documents.jsonl"
    media_jobs_path = index_root / "media_jobs.jsonl"
    temp_vector_path = vector_path.with_suffix(vector_path.suffix + ".tmp")
    temp_media_jobs_path = media_jobs_path.with_suffix(media_jobs_path.suffix + ".tmp")

    catalog = Catalog(str(root_path)) if use_sqlite else None
    reporter = get_daily_runtime_reporter()
    total_records = count_candidate_records(
        normalized_root,
        content_types=content_types,
    )
    scanned_records = 0
    record_count = 0
    document_count = 0
    media_job_count = 0
    content_type_counts: Dict[str, int] = {}

    vector_handle = temp_vector_path.open("w", encoding="utf-8-sig", newline="\n") if write_jsonl else None
    media_handle = temp_media_jobs_path.open("w", encoding="utf-8-sig", newline="\n") if write_jsonl else None

    try:
        for content_type, record_path, record in iter_normalized_records(
            normalized_root,
            content_types=content_types,
        ):
            scanned_records += 1
            if not record_matches_scope(
                record,
                content_types=content_types,
                official_radios_only=official_radios_only,
            ):
                reporter.update(
                    status="running",
                    current=scanned_records,
                    total=total_records or None,
                    unit="records",
                    message=(
                        f"processed={record_count} docs={document_count} "
                        f"media_jobs={media_job_count} current={record_path.name}"
                    ),
                )
                continue
            record_count += 1
            content_type_counts[content_type] = content_type_counts.get(content_type, 0) + 1

            documents = build_vector_documents(record, max_chars=max_chars)
            media_jobs = build_media_jobs(
                record,
                include_article_speech=include_article_speech,
            )

            if vector_handle is not None:
                for document in documents:
                    vector_handle.write(json.dumps(document, ensure_ascii=False) + "\n")
                    document_count += 1
            else:
                document_count += len(documents)

            if media_handle is not None:
                for media_job in media_jobs:
                    media_handle.write(json.dumps(media_job, ensure_ascii=False) + "\n")
                    media_job_count += 1
            else:
                media_job_count += len(media_jobs)

            if catalog is not None:
                normalized_relpath = record_path.relative_to(root_path).as_posix()
                catalog.replace_item_snapshot(
                    record=record,
                    normalized_relpath=normalized_relpath,
                    snapshot_updated_at=snapshot_updated_at(record_path),
                    documents=documents,
                    media_jobs=media_jobs,
                )
            reporter.update(
                status="running",
                current=scanned_records,
                total=total_records or None,
                unit="records",
                message=(
                    f"processed={record_count} docs={document_count} "
                    f"media_jobs={media_job_count} current={record_path.name}"
                ),
                extra={
                    "processed_records": record_count,
                    "vector_documents": document_count,
                    "media_jobs": media_job_count,
                },
            )

        if vector_handle is not None:
            vector_handle.close()
            temp_vector_path.replace(vector_path)
        if media_handle is not None:
            media_handle.close()
            temp_media_jobs_path.replace(media_jobs_path)
    except Exception:
        if vector_handle is not None and not vector_handle.closed:
            vector_handle.close()
            if temp_vector_path.exists():
                temp_vector_path.unlink()
        if media_handle is not None and not media_handle.closed:
            media_handle.close()
            if temp_media_jobs_path.exists():
                temp_media_jobs_path.unlink()
        raise

    summary = {
        "status": "prepared",
        "root": str(root_path),
        "records": record_count,
        "vector_documents": document_count,
        "media_jobs": media_job_count,
        "max_chars": max_chars,
        "content_types": content_type_counts,
        "write_jsonl": write_jsonl,
        "use_sqlite": use_sqlite,
        "content_scope": content_types,
        "official_radios_only": official_radios_only,
        "include_article_speech": include_article_speech,
    }
    if write_jsonl:
        summary["vector_documents_path"] = str(vector_path)
        summary["media_jobs_path"] = str(media_jobs_path)
    if catalog is not None:
        summary["catalog"] = catalog.stats()
    reporter.complete(
        message=(
            f"records={record_count} docs={document_count} "
            f"media_jobs={media_job_count}"
        ),
        extra={
            "processed_records": record_count,
            "vector_documents": document_count,
            "media_jobs": media_job_count,
        },
    )
    return summary


def count_candidate_records(normalized_root: Path, *, content_types: Optional[List[str]]) -> int:
    allowed = {str(value) for value in (content_types or []) if str(value)}
    total = 0
    for content_type_dir in normalized_root.iterdir():
        if not content_type_dir.is_dir():
            continue
        if allowed and content_type_dir.name not in allowed:
            continue
        total += sum(1 for _ in content_type_dir.glob("*.json"))
    return total


def iter_normalized_records(
    normalized_root: Path,
    *,
    content_types: Optional[List[str]] = None,
) -> Iterator[tuple[str, Path, dict]]:
    allowed = {str(value) for value in (content_types or []) if str(value)}
    for content_type_dir in sorted(normalized_root.iterdir(), key=lambda entry: entry.name):
        if not content_type_dir.is_dir():
            continue
        if allowed and content_type_dir.name not in allowed:
            continue
        for record_path in sorted(content_type_dir.glob("*.json"), key=lambda entry: entry.name):
            yield (
                content_type_dir.name,
                record_path,
                json.loads(record_path.read_text(encoding="utf-8-sig")),
            )


def build_vector_documents(record: dict, *, max_chars: int) -> List[dict]:
    documents = build_segment_documents(
        record=record,
        segment=record,
        segment_type="item",
        max_chars=max_chars,
    )

    for chapter_index, chapter in enumerate(record.get("chapters", []), start=1):
        documents.extend(
            build_segment_documents(
                record=record,
                segment=chapter,
                segment_type="chapter",
                chapter_index=chapter_index,
                max_chars=max_chars,
            )
        )
    return documents


def build_transcript_documents(
    *,
    job: dict,
    transcript_text: str,
    max_chars: int,
    model_name: Optional[str] = None,
    language: Optional[str] = None,
) -> List[dict]:
    header_parts = unique_non_empty(
        [
            job.get("item_title"),
            job.get("segment_title"),
        ]
    )
    header_text = "\n".join(header_parts).strip()
    body_budget = max_chars if not header_text else max(200, max_chars - len(header_text) - 2)
    body_chunks = split_text_to_chunks(transcript_text, max_chars=body_budget) or [transcript_text.strip()]

    documents: List[dict] = []
    for chunk_index, body_chunk in enumerate(body_chunks):
        text_parts = [part for part in (header_text, body_chunk.strip()) if part]
        text = "\n\n".join(text_parts).strip()
        if not text:
            continue
        documents.append(
            {
                "chunk_id": f"{job['job_id']}:transcript:{chunk_index:04d}",
                "source": "transcript",
                "content_type": job.get("content_type"),
                "item_id": job.get("item_id"),
                "item_title": job.get("item_title"),
                "segment_type": job.get("segment_type"),
                "segment_id": str(job.get("segment_id")),
                "segment_title": job.get("segment_title"),
                "segment_url": job.get("segment_url"),
                "published_at": job.get("published_at"),
                "category": None,
                "tags": [],
                "users": [],
                "is_free": None,
                "chunk_index": chunk_index,
                "chapter_index": job.get("chapter_index"),
                "model_name": model_name,
                "language": language,
                "text": text,
            }
        )
    return documents


def build_segment_documents(
    *,
    record: dict,
    segment: dict,
    segment_type: str,
    max_chars: int,
    chapter_index: Optional[int] = None,
) -> List[dict]:
    header_parts = build_header_parts(record=record, segment=segment, segment_type=segment_type)
    body_parts = build_body_parts(record=record, segment=segment, segment_type=segment_type)

    header_text = "\n".join(header_parts).strip()
    body_text = "\n\n".join(body_parts).strip()
    body_budget = max_chars if not header_text else max(200, max_chars - len(header_text) - 2)
    body_chunks = split_text_to_chunks(body_text, max_chars=body_budget) if body_text else [""]

    documents: List[dict] = []
    for chunk_index, body_chunk in enumerate(body_chunks):
        text_parts = [part for part in (header_text, body_chunk.strip()) if part]
        text = "\n\n".join(text_parts).strip()
        if not text:
            continue
        documents.append(
            {
                "chunk_id": chunk_id(
                    record_type=str(record["type"]),
                    record_id=str(record["id"]),
                    segment_type=segment_type,
                    segment_id=str(segment["id"]),
                    chunk_index=chunk_index,
                ),
                "source": "content",
                "content_type": record.get("type"),
                "item_id": record.get("id"),
                "item_title": record.get("title"),
                "segment_type": segment_type,
                "segment_id": str(segment.get("id")),
                "segment_title": segment.get("title"),
                "segment_url": segment.get("url") or record.get("url"),
                "published_at": segment.get("published_at") or record.get("published_at"),
                "category": record.get("category"),
                "tags": record.get("tags", []),
                "users": [user.get("nickname") for user in record.get("users", []) if user.get("nickname")],
                "is_free": segment.get("is_free", record.get("is_free")),
                "chunk_index": chunk_index,
                "chapter_index": chapter_index,
                "text": text,
            }
        )
    return documents


def build_header_parts(*, record: dict, segment: dict, segment_type: str) -> List[str]:
    if segment_type == "chapter":
        return unique_non_empty(
            [
                record.get("title"),
                segment.get("title"),
                segment.get("excerpt"),
            ]
        )
    return unique_non_empty(
        [
            record.get("title"),
            record.get("excerpt"),
        ]
    )


def build_body_parts(*, record: dict, segment: dict, segment_type: str) -> List[str]:
    if segment_type == "chapter":
        return unique_non_empty([segment.get("content_text")])
    return unique_non_empty([record.get("desc"), record.get("content_text")])


def split_text_to_chunks(text: str, *, max_chars: int) -> List[str]:
    cleaned = normalize_whitespace(text)
    if not cleaned:
        return []
    if len(cleaned) <= max_chars:
        return [cleaned]

    units = split_text_to_units(cleaned)
    if not units:
        return hard_wrap(cleaned, max_chars=max_chars)

    chunks: List[str] = []
    current = ""
    for unit in units:
        if len(unit) > max_chars:
            if current:
                chunks.append(current.strip())
                current = ""
            chunks.extend(hard_wrap(unit, max_chars=max_chars))
            continue

        if not current:
            current = unit
            continue

        candidate = f"{current}\n{unit}"
        if len(candidate) <= max_chars:
            current = candidate
            continue

        chunks.append(current.strip())
        current = unit

    if current:
        chunks.append(current.strip())
    return [chunk for chunk in chunks if chunk]


def split_text_to_units(text: str) -> List[str]:
    units: List[str] = []
    for paragraph in re.split(r"\n+", text):
        paragraph = paragraph.strip()
        if not paragraph:
            continue
        pieces = [piece.strip() for piece in SENTENCE_BREAK_RE.split(paragraph) if piece.strip()]
        units.extend(pieces or [paragraph])
    return units


def hard_wrap(text: str, *, max_chars: int) -> List[str]:
    return [
        text[index : index + max_chars].strip()
        for index in range(0, len(text), max_chars)
        if text[index : index + max_chars].strip()
    ]


def normalize_whitespace(text: str) -> str:
    lines = [line.strip() for line in text.replace("\r\n", "\n").split("\n")]
    return "\n".join(line for line in lines if line)


def build_media_jobs(record: dict, *, include_article_speech: bool) -> List[dict]:
    jobs: List[dict] = []

    if include_article_speech and record.get("speech_audio_url"):
        jobs.append(
            {
                "job_id": f"{record['type']}:{record['id']}:item:{record['id']}:speech",
                "content_type": record.get("type"),
                "item_id": record.get("id"),
                "item_title": record.get("title"),
                "segment_type": "item",
                "segment_id": str(record.get("id")),
                "segment_title": record.get("title"),
                "segment_url": record.get("url"),
                "published_at": record.get("published_at"),
                "chapter_index": None,
                "media_id": "speech",
                "media_title": record.get("title"),
                "media_type": "speech",
                "provider": "direct",
                "access_mode": "audio_url",
                "media_url": record.get("speech_audio_url"),
                "playlist_url": None,
                "play_auth": None,
                "duration": record.get("duration"),
                "download_method": "direct",
                "status": "pending",
                "transcript_status": "pending",
                "local_relpath": suggested_relpath(
                    record_type=str(record["type"]),
                    record_id=str(record["id"]),
                    segment_type="item",
                    segment_id=str(record["id"]),
                    media_id="speech",
                    media_type="speech",
                    access_mode="audio_url",
                    source_url=record.get("speech_audio_url"),
                ),
            }
        )

    jobs.extend(build_segment_media_jobs(record=record, segment=record, segment_type="item"))

    for chapter_index, chapter in enumerate(record.get("chapters", []), start=1):
        jobs.extend(
            build_segment_media_jobs(
                record=record,
                segment=chapter,
                segment_type="chapter",
                chapter_index=chapter_index,
            )
        )
    return jobs


def record_matches_scope(
    record: dict,
    *,
    content_types: Optional[List[str]],
    official_radios_only: bool,
) -> bool:
    allowed_types = set(content_types or [])
    record_type = str(record.get("type"))
    if allowed_types and record_type not in allowed_types:
        return False
    if record_type == "radios" and official_radios_only:
        return is_official_radio_record(record)
    return True


def is_official_radio_record(record: dict) -> bool:
    owner_type = record.get("owner_type")
    if owner_type:
        return owner_type == "gcores"
    if record.get("option_is_official") or record.get("is_official"):
        return True
    for user in record.get("users", []):
        if user.get("is_official"):
            return True
    return False


def build_segment_media_jobs(
    *,
    record: dict,
    segment: dict,
    segment_type: str,
    chapter_index: Optional[int] = None,
) -> List[dict]:
    jobs: List[dict] = []
    for media in segment.get("media", []):
        selected = select_media_source(media)
        if not selected:
            if media.get("timelines"):
                jobs.extend(
                    build_timeline_asset_jobs(
                        record=record,
                        segment=segment,
                        segment_type=segment_type,
                        media=media,
                        chapter_index=chapter_index,
                    )
                )
            continue
        jobs.append(
            {
                "job_id": f"{record['type']}:{record['id']}:{segment_type}:{segment['id']}:media:{media['id']}",
                "content_type": record.get("type"),
                "item_id": record.get("id"),
                "item_title": record.get("title"),
                "segment_type": segment_type,
                "segment_id": str(segment.get("id")),
                "segment_title": segment.get("title"),
                "segment_url": segment.get("url") or record.get("url"),
                "published_at": segment.get("published_at") or record.get("published_at"),
                "chapter_index": chapter_index,
                "media_id": str(media.get("id")),
                "media_title": media.get("title"),
                "media_type": media.get("media_type"),
                "provider": selected.get("provider"),
                "access_mode": selected.get("access_mode"),
                "media_url": selected.get("media_url"),
                "playlist_url": media.get("playlist_url"),
                "play_auth": selected.get("play_auth"),
                "duration": media.get("duration") or segment.get("duration"),
                "download_method": selected.get("download_method"),
                "status": "pending",
                "transcript_status": "pending",
                "local_relpath": suggested_relpath(
                    record_type=str(record["type"]),
                    record_id=str(record["id"]),
                    segment_type=segment_type,
                    segment_id=str(segment["id"]),
                    media_id=str(media["id"]),
                    media_type=media.get("media_type"),
                    access_mode=str(selected.get("access_mode")),
                    source_url=selected.get("media_url"),
                ),
            }
        )
        jobs.extend(
            build_timeline_asset_jobs(
                record=record,
                segment=segment,
                segment_type=segment_type,
                media=media,
                chapter_index=chapter_index,
            )
        )
    return jobs


def build_timeline_asset_jobs(
    *,
    record: dict,
    segment: dict,
    segment_type: str,
    media: dict,
    chapter_index: Optional[int],
) -> List[dict]:
    jobs: List[dict] = []
    for timeline in media.get("timelines", []):
        asset_url = timeline.get("asset_url")
        if not asset_url:
            continue
        timeline_id = str(timeline.get("id"))
        jobs.append(
            {
                "job_id": f"{record['type']}:{record['id']}:{segment_type}:{segment['id']}:timeline:{timeline_id}:asset",
                "content_type": record.get("type"),
                "item_id": record.get("id"),
                "item_title": record.get("title"),
                "segment_type": "timeline",
                "segment_id": timeline_id,
                "segment_title": timeline.get("title") or media.get("title") or segment.get("title"),
                "segment_url": timeline.get("quote_href") or segment.get("url") or record.get("url"),
                "published_at": segment.get("published_at") or record.get("published_at"),
                "chapter_index": chapter_index,
                "media_id": f"{timeline_id}_asset",
                "media_title": timeline.get("title") or media.get("title"),
                "media_type": "timeline_asset",
                "provider": "direct",
                "access_mode": "asset_url",
                "media_url": asset_url,
                "playlist_url": None,
                "play_auth": None,
                "duration": None,
                "download_method": "direct",
                "status": "pending",
                "transcript_status": "skipped",
                "local_relpath": suggested_relpath(
                    record_type=str(record["type"]),
                    record_id=str(record["id"]),
                    segment_type="timeline",
                    segment_id=timeline_id,
                    media_id=f"{timeline_id}_asset",
                    media_type="timeline_asset",
                    access_mode="asset_url",
                    source_url=asset_url,
                ),
            }
        )
    return jobs


def select_media_source(media: dict) -> Optional[dict]:
    if media.get("audio_url"):
        return {
            "provider": "direct",
            "access_mode": "audio_url",
            "media_url": media.get("audio_url"),
            "download_method": "direct",
        }

    if media.get("media_type") == "protected_audio":
        return None

    resolved = media.get("resolved") or {}
    if resolved.get("provider") == "taptap" and resolved.get("m3u8"):
        return {
            "provider": "taptap",
            "access_mode": "m3u8",
            "media_url": resolved.get("m3u8"),
            "play_auth": None,
            "download_method": "ffmpeg",
        }

    if resolved.get("provider") == "vod" and resolved.get("play_auth"):
        return {
            "provider": "vod",
            "access_mode": "play_auth",
            "media_url": None,
            "play_auth": resolved.get("play_auth"),
            "download_method": "provider_api",
        }

    if media.get("original_src"):
        return {
            "provider": "direct",
            "access_mode": "original_src",
            "media_url": media.get("original_src"),
            "play_auth": None,
            "download_method": "direct",
        }

    if media.get("playlist_url"):
        return {
            "provider": "playlist",
            "access_mode": "playlist_url",
            "media_url": media.get("playlist_url"),
            "play_auth": None,
            "download_method": "ffmpeg",
        }

    return None


def suggested_relpath(
    *,
    record_type: str,
    record_id: str,
    segment_type: str,
    segment_id: str,
    media_id: str,
    media_type: Optional[str],
    access_mode: str,
    source_url: Optional[str],
) -> str:
    extension = guess_extension(
        source_url,
        access_mode=access_mode,
        media_type=media_type,
    )
    return (
        Path("downloads")
        / record_type
        / record_id
        / segment_type
        / segment_id
        / f"{media_id}{extension}"
    ).as_posix()


def guess_extension(source_url: Optional[str], *, access_mode: str, media_type: Optional[str]) -> str:
    if access_mode == "play_auth":
        return ".json"

    if source_url:
        suffix = Path(urlparse(source_url).path).suffix.lower()
        if suffix and suffix != ".m3u8" and len(suffix) <= 8:
            return suffix

    if access_mode in {"m3u8", "playlist_url"}:
        if media_type in {"taptap", "vod"}:
            return ".mp4"
        return ".media"

    if media_type == "speech":
        return ".mp3"

    if access_mode == "asset_url":
        return ".asset"

    if access_mode in {"audio_url", "original_src"}:
        return ".mp3"

    return ".bin"


def unique_non_empty(values: Iterable[Optional[str]]) -> List[str]:
    seen = set()
    output: List[str] = []
    for value in values:
        if not value:
            continue
        cleaned = str(value).strip()
        if not cleaned or cleaned in seen:
            continue
        seen.add(cleaned)
        output.append(cleaned)
    return output


def chunk_id(*, record_type: str, record_id: str, segment_type: str, segment_id: str, chunk_index: int) -> str:
    return f"{record_type}:{record_id}:{segment_type}:{segment_id}:{chunk_index:04d}"
