from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from gcores_crawler.catalog import Catalog
from gcores_crawler.indexing import build_transcript_documents
from gcores_crawler.transcribe import detect_repetitive_transcript_text


def segment_text(segment: dict[str, Any]) -> str:
    return str(segment.get("text") or segment.get("punc_text") or "").strip()


def longest_same_char_run(text: str) -> int:
    compact = "".join(ch for ch in text.lower() if not ch.isspace())
    if not compact:
        return 0
    best = 1
    current = 1
    for prev, cur in zip(compact, compact[1:]):
        if prev == cur:
            current += 1
            best = max(best, current)
        else:
            current = 1
    return best


def repeated_token_ratio(text: str) -> float:
    tokens = re.findall(r"[\w\u4e00-\u9fff]+", text.lower())
    if len(tokens) < 8:
        return 0.0
    counts: dict[str, int] = {}
    for token in tokens:
        counts[token] = counts.get(token, 0) + 1
    return max(counts.values()) / len(tokens)


def cjk_count(text: str) -> int:
    return sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff")


def ascii_alpha_count(text: str) -> int:
    return sum(1 for ch in text if ch.isascii() and ch.isalpha())


BOUNDARY_NOISE_RE = re.compile(
    r"\b(da|na|yeah|baby|move|club|yoga|touch|nobody|know|bae|oh|trip|monday|fasaka)\b",
    re.IGNORECASE,
)


def should_drop_segment(*, text: str, idx: int, total: int, confidence: float | None) -> bool:
    if not text:
        return True
    lower = text.lower()
    boundary = idx < 24 or idx >= max(0, total - 24)

    if "<unk>" in lower:
        return True

    if longest_same_char_run(text) >= 20:
        return True

    if repeated_token_ratio(text) > 0.6:
        return True

    ascii_letters = ascii_alpha_count(text)
    cjk_letters = cjk_count(text)
    conf = confidence if confidence is not None else 1.0

    if boundary and BOUNDARY_NOISE_RE.search(lower):
        return True

    if boundary and ascii_letters >= 16 and ascii_letters > cjk_letters * 2 and conf < 0.9:
        return True

    return False


def clean_segments(segments: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    kept: list[dict[str, Any]] = []
    removed: list[dict[str, Any]] = []
    total = len(segments)
    for idx, segment in enumerate(segments):
        text = segment_text(segment)
        confidence_raw = segment.get("asr_confidence")
        confidence = float(confidence_raw) if confidence_raw is not None else None
        if should_drop_segment(text=text, idx=idx, total=total, confidence=confidence):
            removed.append(
                {
                    "index": idx,
                    "start_ms": segment.get("start_ms"),
                    "end_ms": segment.get("end_ms"),
                    "text": text,
                    "asr_confidence": confidence,
                }
            )
            continue
        copy = dict(segment)
        copy["text"] = text
        kept.append(copy)
    return kept, removed


def rebuild_text(segments: list[dict[str, Any]]) -> str:
    return "".join(segment_text(segment) for segment in segments).strip()


def repair_job(root: Path, catalog: Catalog, job: dict[str, Any], *, max_chars: int) -> dict[str, Any]:
    transcript_relpath = Path(str(job["transcript_path"]))
    transcript_path = root / transcript_relpath
    payload = json.loads(transcript_path.read_text(encoding="utf-8"))

    original_segments = payload.get("segments") or []
    if not isinstance(original_segments, list) or not original_segments:
        raise RuntimeError("Transcript payload has no segments")

    cleaned_segments, removed = clean_segments(original_segments)
    if not removed:
        raise RuntimeError("No suspicious segments removed")

    cleaned_text = rebuild_text(cleaned_segments)
    if not cleaned_text:
        raise RuntimeError("Cleaned transcript text is empty")

    validation_error = detect_repetitive_transcript_text(cleaned_text)
    if validation_error:
        raise RuntimeError(f"Still invalid after cleaning: {validation_error}")

    original_text = str(payload.get("text") or "")
    if len(cleaned_text) < max(1000, int(len(original_text) * 0.5)):
        raise RuntimeError("Cleaned transcript text shrank too much")

    documents = build_transcript_documents(
        job=job,
        transcript_text=cleaned_text,
        max_chars=max_chars,
        model_name=payload.get("model_name"),
        language=payload.get("language"),
    )

    backup_path = transcript_path.with_suffix(transcript_path.suffix + ".pre_repair.bak")
    if not backup_path.exists():
        shutil.copy2(transcript_path, backup_path)

    repaired_payload = {
        "text": cleaned_text,
        "segments": cleaned_segments,
        "words": [],
        "model_name": payload.get("model_name"),
        "language": payload.get("language"),
        "metadata": {
            **(payload.get("metadata") or {}),
            "repair": {
                "removed_segment_count": len(removed),
                "removed_segments": removed[:50],
                "backup_path": backup_path.name,
            },
        },
    }
    transcript_path.write_text(json.dumps(repaired_payload, ensure_ascii=False, indent=2), encoding="utf-8")

    catalog.save_transcript(
        job=job,
        transcript_path=transcript_relpath.as_posix(),
        transcript_text=cleaned_text,
        transcript_segments=cleaned_segments,
        model_name=payload.get("model_name"),
        language=payload.get("language"),
        documents=documents,
    )

    return {
        "job_id": job["job_id"],
        "item_key": job["item_key"],
        "removed_segment_count": len(removed),
        "original_text_len": len(original_text),
        "cleaned_text_len": len(cleaned_text),
        "transcript_path": transcript_relpath.as_posix(),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", default="J:/G-Search/data")
    parser.add_argument("--max-chars", type=int, default=800)
    parser.add_argument("--limit", type=int, default=50)
    parser.add_argument("--job-id", action="append", default=[])
    args = parser.parse_args()

    root = Path(args.root)
    catalog = Catalog(str(root))
    stats: list[dict[str, Any]] = []

    with catalog._connect() as connection:  # noqa: SLF001
        connection.row_factory = sqlite3.Row  # type: ignore[name-defined]
        if args.job_id:
            placeholders = ", ".join("?" for _ in args.job_id)
            rows = connection.execute(
                f"""
                SELECT *
                FROM media_jobs
                WHERE job_id IN ({placeholders})
                """,
                tuple(args.job_id),
            ).fetchall()
        else:
            rows = connection.execute(
                """
                SELECT *
                FROM media_jobs
                WHERE content_type = 'radios'
                  AND media_type = 'audio'
                  AND transcript_status = 'error'
                  AND transcript_path IS NOT NULL
                  AND (
                    transcript_error LIKE 'Rescue validation failed:%'
                    OR transcript_error LIKE 'suspiciously low unique-char ratio%'
                  )
                ORDER BY updated_at DESC
                LIMIT ?
                """,
                (args.limit,),
            ).fetchall()

    for row in rows:
        job = dict(row)
        try:
            repaired = repair_job(root, catalog, job, max_chars=args.max_chars)
            stats.append({"status": "completed", **repaired})
            print(f"[repair] ok job={job['job_id']} removed={repaired['removed_segment_count']}")
        except Exception as exc:  # noqa: BLE001
            stats.append({"status": "error", "job_id": job["job_id"], "error": str(exc)})
            print(f"[repair] error job={job['job_id']} error={exc}")

    report_path = root / "logs" / "repair_failed_transcripts_report.json"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(stats, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[repair] report={report_path}")
    return 0


if __name__ == "__main__":
    import sqlite3

    raise SystemExit(main())
