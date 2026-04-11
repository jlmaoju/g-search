from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from gcores_crawler.catalog import Catalog  # noqa: E402
from gcores_crawler.daily_runtime import get_daily_runtime_reporter  # noqa: E402
from gcores_crawler.transcribe import build_transcript_relpath, finalize_transcript, safe_filename  # noqa: E402


DEFAULT_WRAPPER = PROJECT_ROOT / "scripts" / "zhipu_glm_asr_wrapper.py"
DEFAULT_MAX_CHARS = 800


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Rescue remaining transcript failures with Zhipu GLM-ASR chunked wrapper.")
    parser.add_argument("--root", default=str(PROJECT_ROOT / "data"))
    parser.add_argument("--wrapper", default=str(DEFAULT_WRAPPER))
    parser.add_argument("--api-key", default=os.environ.get("ZHIPU_API_KEY"))
    parser.add_argument("--max-chars", type=int, default=DEFAULT_MAX_CHARS)
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--item-key", action="append", default=[])
    parser.add_argument("--job-id", action="append", default=[])
    parser.add_argument("--verbose", action="store_true")
    return parser.parse_args()


def fetch_jobs(*, catalog: Catalog, item_keys: list[str], job_ids: list[str], limit: int) -> list[dict[str, Any]]:
    with catalog._connect() as connection:  # noqa: SLF001
        rows = connection.execute(
            """
            SELECT *
            FROM media_jobs
            WHERE content_type = 'radios'
              AND segment_type = 'item'
              AND transcript_status = 'error'
            ORDER BY item_key
            """
        ).fetchall()
    jobs = [dict(row) for row in rows]
    if item_keys:
        wanted = set(item_keys)
        jobs = [job for job in jobs if str(job.get("item_key")) in wanted]
    if job_ids:
        wanted = set(job_ids)
        jobs = [job for job in jobs if str(job.get("job_id")) in wanted]
    if limit > 0:
        jobs = jobs[:limit]
    return jobs


def run_wrapper(
    *,
    wrapper_path: Path,
    api_key: str,
    input_path: Path,
    output_path: Path,
    job: dict[str, Any],
) -> None:
    command = [
        sys.executable,
        str(wrapper_path),
        "--input",
        str(input_path),
        "--output",
        str(output_path),
        "--api-key",
        api_key,
        "--uttid",
        safe_filename(str(job["job_id"])),
    ]
    completed = subprocess.run(
        command,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
        cwd=str(PROJECT_ROOT),
    )
    if completed.returncode != 0:
        stderr = completed.stderr.strip() or completed.stdout.strip() or f"wrapper exited {completed.returncode}"
        raise RuntimeError(stderr)


def main() -> int:
    args = parse_args()
    if not args.api_key:
        raise SystemExit("Missing --api-key or ZHIPU_API_KEY")
    root_path = Path(args.root).resolve()
    wrapper_path = Path(args.wrapper).resolve()
    catalog = Catalog(str(root_path))
    reporter = get_daily_runtime_reporter()
    jobs = fetch_jobs(catalog=catalog, item_keys=list(args.item_key), job_ids=list(args.job_id), limit=args.limit)
    summary: dict[str, Any] = {
        "status": "ok",
        "root": str(root_path),
        "wrapper": str(wrapper_path),
        "seen": len(jobs),
        "completed": [],
        "errors": [],
    }
    if not jobs:
        reporter.complete(message="seen=0 completed=0 errors=0")
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return 0

    total_jobs = len(jobs)
    for index, job in enumerate(jobs, start=1):
        job_id = str(job["job_id"])
        try:
            downloaded_path = root_path / str(job.get("downloaded_path") or "")
            if not downloaded_path.exists():
                raise FileNotFoundError(f"Downloaded media missing: {downloaded_path}")
            transcript_relpath = build_transcript_relpath(job)
            transcript_path = root_path / transcript_relpath
            transcript_path.parent.mkdir(parents=True, exist_ok=True)
            if args.verbose:
                print(f"[zhipu-rescue] start job={job_id} item={job['item_key']} path={downloaded_path}", flush=True)
            run_wrapper(
                wrapper_path=wrapper_path,
                api_key=args.api_key,
                input_path=downloaded_path,
                output_path=transcript_path,
                job=job,
            )
            result = finalize_transcript(
                root_path=root_path,
                job=job,
                transcript_relpath=transcript_relpath,
                transcript_path=transcript_path,
                max_chars=args.max_chars,
            )
            catalog.save_transcript(
                job=job,
                transcript_path=result["transcript_path"],
                transcript_text=result["text"],
                transcript_segments=result["segments"],
                model_name=result["model_name"],
                language=result["language"],
                documents=result["documents"],
            )
            summary["completed"].append(
                {
                    "job_id": job_id,
                    "item_key": job["item_key"],
                    "transcript_path": result["transcript_path"],
                    "model_name": result["model_name"],
                }
            )
            if args.verbose:
                print(f"[zhipu-rescue] ok job={job_id} model={result['model_name']} path={result['transcript_path']}", flush=True)
        except Exception as exc:  # noqa: BLE001
            message = str(exc)
            catalog.mark_transcript_error(job_id=job_id, error=message)
            summary["errors"].append({"job_id": job_id, "item_key": job["item_key"], "error": message})
            if args.verbose:
                print(f"[zhipu-rescue] error job={job_id} error={message}", flush=True)
        reporter.update(
            status="running",
            current=index,
            total=total_jobs,
            unit="jobs",
            message=(
                f"completed={len(summary['completed'])} errors={len(summary['errors'])} "
                f"job={job_id}"
            ),
            extra={
                "completed": len(summary["completed"]),
                "errors": len(summary["errors"]),
            },
        )

    reporter.complete(
        message=(
            f"seen={summary['seen']} completed={len(summary['completed'])} "
            f"errors={len(summary['errors'])}"
        ),
        extra={
            "seen": summary["seen"],
            "completed": len(summary["completed"]),
            "errors": len(summary["errors"]),
        },
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if not summary["errors"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
