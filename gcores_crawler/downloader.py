from __future__ import annotations

import concurrent.futures
import contextlib
import shutil
import ssl
import subprocess
import time
from pathlib import Path
from typing import Dict, List
from urllib import request as urllib_request
from urllib.parse import urlparse

from .catalog import Catalog
from .daily_runtime import get_daily_runtime_reporter
from .subprocess_utils import quiet_subprocess_kwargs


class UnsupportedMediaError(RuntimeError):
    """Raised when a media job cannot be downloaded by the local worker."""


class PermanentDownloadError(RuntimeError):
    """Raised when the remote media is permanently unavailable."""


def download_media_jobs(
    root: str = "data",
    *,
    limit: int = 10,
    max_workers: int = 1,
    include_errors: bool = False,
    job_ids: List[str] | None = None,
    item_keys: List[str] | None = None,
    segment_types: List[str] | None = None,
    media_types: List[str] | None = None,
    verbose: bool = False,
) -> dict:
    root_path = Path(root)
    catalog = Catalog(str(root_path))
    jobs = catalog.get_download_jobs(
        limit=limit,
        include_errors=include_errors,
        job_ids=job_ids,
        item_keys=item_keys,
        segment_types=segment_types,
        media_types=media_types,
    )

    summary = {
        "status": "downloaded",
        "root": str(root_path),
        "limit": limit,
        "workers": max(1, int(max_workers)),
        "include_errors": include_errors,
        "job_ids": list(job_ids or []),
        "item_keys": list(item_keys or []),
        "segment_types": list(segment_types or []),
        "media_types": list(media_types or []),
        "seen": len(jobs),
        "downloaded": [],
        "skipped": [],
        "unsupported": [],
        "errors": [],
    }

    if max_workers <= 1 or len(jobs) <= 1:
        for job in jobs:
            handle_download_result(
                summary=summary,
                catalog=catalog,
                job=job,
                result_or_exc=run_download_job(root_path=root_path, job=job),
                verbose=verbose,
            )
    else:
        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
            future_map = {
                executor.submit(run_download_job, root_path=root_path, job=job): job
                for job in jobs
            }
            for future in concurrent.futures.as_completed(future_map):
                job = future_map[future]
                try:
                    result_or_exc = future.result()
                except Exception as exc:  # noqa: BLE001
                    result_or_exc = exc
                handle_download_result(
                    summary=summary,
                    catalog=catalog,
                    job=job,
                    result_or_exc=result_or_exc,
                    verbose=verbose,
                )

    summary["catalog"] = catalog.stats()
    return summary


def download_media_loop(
    root: str = "data",
    *,
    batch_size: int = 50,
    sleep_seconds: float = 5.0,
    max_idle_cycles: int = 3,
    max_workers: int = 1,
    min_workers: int = 1,
    backoff_error_threshold: int = 3,
    backoff_sleep_seconds: float = 30.0,
    recover_after_cycles: int = 3,
    include_errors: bool = False,
    job_ids: List[str] | None = None,
    item_keys: List[str] | None = None,
    segment_types: List[str] | None = None,
    media_types: List[str] | None = None,
    verbose: bool = False,
) -> dict:
    root_path = Path(root)
    catalog = Catalog(str(root_path))
    reporter = get_daily_runtime_reporter()
    current_workers = max(min_workers, max_workers)
    healthy_cycles = 0
    target_total = len(job_ids) if job_ids else None
    aggregate = {
        "status": "completed",
        "root": str(root_path),
        "batch_size": batch_size,
        "sleep_seconds": sleep_seconds,
        "max_idle_cycles": max_idle_cycles,
        "max_workers": max_workers,
        "min_workers": min_workers,
        "current_workers": current_workers,
        "backoff_error_threshold": backoff_error_threshold,
        "backoff_sleep_seconds": backoff_sleep_seconds,
        "recover_after_cycles": recover_after_cycles,
        "include_errors": include_errors,
        "job_ids": list(job_ids or []),
        "item_keys": list(item_keys or []),
        "segment_types": list(segment_types or []),
        "media_types": list(media_types or []),
        "cycles": 0,
        "idle_cycles": 0,
        "seen": 0,
        "downloaded": 0,
        "skipped": 0,
        "unsupported": 0,
        "errors": 0,
        "events": [],
    }

    try:
        write_runtime_state(
            catalog,
            "runtime:download_loop",
            {
                **aggregate,
                "status": "running",
            },
        )
        reporter.update(
            status="running",
            current=0,
            total=target_total,
            unit="jobs",
            message="starting download loop",
        )
        while True:
            summary = download_media_jobs(
                root,
                limit=batch_size,
                max_workers=current_workers,
                include_errors=include_errors,
                job_ids=job_ids,
                item_keys=item_keys,
                segment_types=segment_types,
                media_types=media_types,
                verbose=verbose,
            )
            aggregate["cycles"] += 1
            aggregate["seen"] += int(summary.get("seen", 0))
            aggregate["downloaded"] += len(summary.get("downloaded", []))
            aggregate["skipped"] += len(summary.get("skipped", []))
            aggregate["unsupported"] += len(summary.get("unsupported", []))
            aggregate["errors"] += len(summary.get("errors", []))
            aggregate["catalog"] = summary.get("catalog")
            aggregate["current_workers"] = current_workers
            resolved = (
                aggregate["downloaded"]
                + aggregate["skipped"]
                + aggregate["unsupported"]
                + aggregate["errors"]
            )

            if not summary.get("seen"):
                aggregate["idle_cycles"] += 1
                if max_idle_cycles > 0 and aggregate["idle_cycles"] >= max_idle_cycles:
                    break
            else:
                aggregate["idle_cycles"] = 0

            transient_errors = len(summary.get("errors", []))
            if transient_errors >= backoff_error_threshold and current_workers > min_workers:
                previous_workers = current_workers
                current_workers = max(min_workers, max(min_workers, current_workers // 2))
                healthy_cycles = 0
                event = {
                    "type": "backoff",
                    "cycle": aggregate["cycles"],
                    "previous_workers": previous_workers,
                    "current_workers": current_workers,
                    "transient_errors": transient_errors,
                }
                aggregate["events"].append(event)
                if verbose:
                    print(
                        f"[download-loop] backoff cycle={aggregate['cycles']} "
                        f"errors={transient_errors} workers={previous_workers}->{current_workers}"
                    )
                write_runtime_state(
                    catalog,
                    "runtime:download_loop",
                    {
                        **aggregate,
                        "status": "running",
                    },
                )
                reporter.update(
                    status="running",
                    current=resolved,
                    total=target_total,
                    unit="jobs",
                    message=(
                        f"downloaded={aggregate['downloaded']} skipped={aggregate['skipped']} "
                        f"unsupported={aggregate['unsupported']} errors={aggregate['errors']} "
                        f"workers={current_workers}"
                    ),
                )
                if backoff_sleep_seconds > 0:
                    time.sleep(backoff_sleep_seconds)
                continue

            if transient_errors == 0 and summary.get("seen"):
                healthy_cycles += 1
                if healthy_cycles >= recover_after_cycles and current_workers < max_workers:
                    previous_workers = current_workers
                    current_workers += 1
                    healthy_cycles = 0
                    event = {
                        "type": "recover",
                        "cycle": aggregate["cycles"],
                        "previous_workers": previous_workers,
                        "current_workers": current_workers,
                    }
                    aggregate["events"].append(event)
                    if verbose:
                        print(
                            f"[download-loop] recover cycle={aggregate['cycles']} "
                            f"workers={previous_workers}->{current_workers}"
                        )
            else:
                healthy_cycles = 0

            if verbose:
                print(
                    f"[download-loop] cycle={aggregate['cycles']} workers={current_workers} "
                    f"seen={summary.get('seen', 0)} downloaded={len(summary.get('downloaded', []))} "
                    f"skipped={len(summary.get('skipped', []))} unsupported={len(summary.get('unsupported', []))} "
                    f"errors={len(summary.get('errors', []))} idle={aggregate['idle_cycles']}"
                )
            write_runtime_state(
                catalog,
                "runtime:download_loop",
                {
                    **aggregate,
                    "status": "running",
                },
            )
            reporter.update(
                status="running",
                current=resolved,
                total=target_total,
                unit="jobs",
                message=(
                    f"downloaded={aggregate['downloaded']} skipped={aggregate['skipped']} "
                    f"unsupported={aggregate['unsupported']} errors={aggregate['errors']} "
                    f"workers={current_workers}"
                ),
                extra={
                    "downloaded": aggregate["downloaded"],
                    "skipped": aggregate["skipped"],
                    "unsupported": aggregate["unsupported"],
                    "errors": aggregate["errors"],
                    "workers": current_workers,
                },
            )
            if sleep_seconds > 0:
                time.sleep(sleep_seconds)
    except KeyboardInterrupt:
        aggregate["status"] = "paused"
        aggregate["catalog"] = catalog.stats()
        write_runtime_state(
            catalog,
            "runtime:download_loop",
            {
                **aggregate,
                "status": "paused",
            },
        )
        reporter.update(status="paused", message="download loop paused")
        return aggregate

    write_runtime_state(
        catalog,
        "runtime:download_loop",
        {
            **aggregate,
            "status": "completed",
        },
    )
    reporter.complete(
        message=(
            f"downloaded={aggregate['downloaded']} skipped={aggregate['skipped']} "
            f"unsupported={aggregate['unsupported']} errors={aggregate['errors']}"
        ),
        extra={
            "downloaded": aggregate["downloaded"],
            "skipped": aggregate["skipped"],
            "unsupported": aggregate["unsupported"],
            "errors": aggregate["errors"],
        },
    )
    return aggregate


def run_download_job(*, root_path: Path, job: dict) -> dict | Exception:
    try:
        return download_media_job(root_path=root_path, job=job)
    except Exception as exc:  # noqa: BLE001
        return exc


def handle_download_result(*, summary: dict, catalog: Catalog, job: dict, result_or_exc: dict | Exception, verbose: bool) -> None:
    if isinstance(result_or_exc, dict):
        try:
            catalog.mark_media_downloaded(
                job_id=str(job["job_id"]),
                downloaded_path=result_or_exc["downloaded_path"],
                file_size=result_or_exc["file_size"],
            )
        except Exception as exc:  # noqa: BLE001
            summary["errors"].append({"job_id": job["job_id"], "error": f"catalog write failed after download: {exc}"})
            if verbose:
                print(f"[download] error job={job['job_id']} error=catalog write failed after download: {exc}")
            return
        record = {
            "job_id": job["job_id"],
            "downloaded_path": result_or_exc["downloaded_path"],
            "file_size": result_or_exc["file_size"],
        }
        if result_or_exc["skipped"]:
            summary["skipped"].append(record)
            if verbose:
                print(f"[download] skipped job={job['job_id']} path={result_or_exc['downloaded_path']}")
        else:
            summary["downloaded"].append(record)
            if verbose:
                print(
                    f"[download] ok job={job['job_id']} size={result_or_exc['file_size']} "
                    f"path={result_or_exc['downloaded_path']}"
                )
        return

    exc = result_or_exc
    if isinstance(exc, PermanentDownloadError):
        try:
            catalog.mark_media_download_error(
                job_id=str(job["job_id"]),
                error=str(exc),
                status="unsupported",
            )
        except Exception as write_exc:  # noqa: BLE001
            summary["errors"].append({"job_id": job["job_id"], "error": f"catalog write failed after permanent error: {write_exc}"})
            if verbose:
                print(f"[download] error job={job['job_id']} error=catalog write failed after permanent error: {write_exc}")
            return
        summary["unsupported"].append({"job_id": job["job_id"], "error": str(exc)})
        if verbose:
            print(f"[download] unsupported job={job['job_id']} error={exc}")
        return
    if isinstance(exc, UnsupportedMediaError):
        try:
            catalog.mark_media_download_error(
                job_id=str(job["job_id"]),
                error=str(exc),
                status="unsupported",
            )
        except Exception as write_exc:  # noqa: BLE001
            summary["errors"].append({"job_id": job["job_id"], "error": f"catalog write failed after unsupported media: {write_exc}"})
            if verbose:
                print(f"[download] error job={job['job_id']} error=catalog write failed after unsupported media: {write_exc}")
            return
        summary["unsupported"].append({"job_id": job["job_id"], "error": str(exc)})
        if verbose:
            print(f"[download] unsupported job={job['job_id']} error={exc}")
        return

    try:
        catalog.mark_media_download_error(
            job_id=str(job["job_id"]),
            error=str(exc),
            status="error",
        )
    except Exception as write_exc:  # noqa: BLE001
        summary["errors"].append({"job_id": job["job_id"], "error": f"catalog write failed after transient error: {write_exc}"})
        if verbose:
            print(f"[download] error job={job['job_id']} error=catalog write failed after transient error: {write_exc}")
        return
    summary["errors"].append({"job_id": job["job_id"], "error": str(exc)})
    if verbose:
        print(f"[download] error job={job['job_id']} error={exc}")


def write_runtime_state(catalog: Catalog, state_key: str, payload: dict) -> None:
    catalog.set_state(state_key, payload)


def download_media_job(*, root_path: Path, job: dict) -> dict:
    output_path = root_path / str(job["local_relpath"])
    output_path.parent.mkdir(parents=True, exist_ok=True)

    if output_path.exists() and output_path.stat().st_size > 0:
        return {
            "downloaded_path": output_path.relative_to(root_path).as_posix(),
            "file_size": output_path.stat().st_size,
            "skipped": True,
        }

    access_mode = str(job.get("access_mode") or "")
    if access_mode in {"audio_url", "original_src", "asset_url"}:
        source_url = job.get("media_url")
        if not source_url:
            raise UnsupportedMediaError("Direct media job is missing media_url")
        download_direct(source_url=str(source_url), output_path=output_path)
    elif access_mode in {"m3u8", "playlist_url"}:
        source_url = job.get("media_url")
        if not source_url:
            raise UnsupportedMediaError("HLS media job is missing media_url")
        download_hls(source_url=str(source_url), output_path=output_path)
    elif access_mode == "play_auth":
        raise UnsupportedMediaError("play_auth download requires a provider-specific resolver")
    else:
        raise UnsupportedMediaError(f"Unsupported access mode: {access_mode}")

    return {
        "downloaded_path": output_path.relative_to(root_path).as_posix(),
        "file_size": output_path.stat().st_size,
        "skipped": False,
    }


def download_direct(*, source_url: str, output_path: Path) -> None:
    command = [
        "curl.exe",
        "--compressed",
        "-sS",
        "-L",
        "--fail",
        "--http1.1",
        "--tlsv1.2",
        "--retry",
        "5",
        "--retry-all-errors",
        "--retry-delay",
        "2",
        "--connect-timeout",
        "20",
        "--max-time",
        "1800",
        "--continue-at",
        "-",
        "-o",
        str(output_path),
        source_url,
    ]
    try:
        run_command(command, error_prefix="curl.exe download failed", source_url=source_url)
        return
    except RuntimeError as exc:
        message = str(exc).lower()
        if not should_fallback_to_python_download(source_url=source_url, error_message=message):
            raise
    download_direct_python(source_url=source_url, output_path=output_path)


def download_hls(*, source_url: str, output_path: Path) -> None:
    ffmpeg_path = shutil.which("ffmpeg")
    if not ffmpeg_path:
        raise UnsupportedMediaError("ffmpeg is required for m3u8 media jobs")

    temp_path = output_path.with_suffix(output_path.suffix + ".part")
    if temp_path.exists():
        temp_path.unlink()

    command = [
        ffmpeg_path,
        "-nostdin",
        "-loglevel",
        "error",
        "-y",
        "-i",
        source_url,
        "-c",
        "copy",
        str(temp_path),
    ]
    run_command(command, error_prefix="ffmpeg download failed", source_url=source_url)
    temp_path.replace(output_path)


def run_command(command: List[str], *, error_prefix: str, source_url: str | None = None) -> None:
    completed = subprocess.run(
        command,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
        **quiet_subprocess_kwargs(),
    )
    if completed.returncode != 0:
        stderr = completed.stderr.strip() or completed.stdout.strip()
        if is_permanent_http_error(stderr, source_url=source_url):
            raise PermanentDownloadError(f"{error_prefix}: {stderr}")
        raise RuntimeError(f"{error_prefix}: {stderr}")


def is_permanent_http_error(stderr: str, *, source_url: str | None = None) -> bool:
    lowered = stderr.lower()
    if "returned error: 404" in lowered or "returned error: 410" in lowered:
        return True
    if "returned error: 403" in lowered and source_url:
        parsed = urlparse(source_url)
        host = parsed.netloc.lower()
        if host.endswith("lizhi.fm") and "/audio/" in (parsed.path.lower() or ""):
            return True
    return False


def should_fallback_to_python_download(*, source_url: str, error_message: str) -> bool:
    parsed = urlparse(source_url)
    host = parsed.netloc.lower()
    if "schannel" not in error_message and "ssl/tls connection failed" not in error_message:
        return False
    return host.endswith("alioss.gcores.com") or host.endswith("aliyuncs.com")


def download_direct_python(*, source_url: str, output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = output_path.with_suffix(output_path.suffix + ".part")
    with contextlib.suppress(FileNotFoundError):
        temp_path.unlink()

    ssl_context = ssl.create_default_context()
    request = urllib_request.Request(
        source_url,
        headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) GSearch/1.0",
            "Accept": "*/*",
        },
    )
    try:
        with urllib_request.urlopen(request, timeout=60, context=ssl_context) as response, temp_path.open("wb") as handle:
            shutil.copyfileobj(response, handle, length=1024 * 1024)
        temp_path.replace(output_path)
    except Exception as exc:  # noqa: BLE001
        with contextlib.suppress(FileNotFoundError):
            temp_path.unlink()
        raise RuntimeError(f"python urllib download failed: {exc}") from exc
