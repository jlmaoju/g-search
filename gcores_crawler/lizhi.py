from __future__ import annotations

import json
import re
import subprocess
from pathlib import PurePosixPath
from typing import List, Optional
from urllib.parse import urlparse

from .catalog import Catalog
from .http import CONTENT_TYPE_MARKER, EFFECTIVE_URL_MARKER, STATUS_MARKER, CurlHttpClient
from .subprocess_utils import quiet_subprocess_kwargs


OLD_LIZHI_URL_RE = re.compile(r"/(?P<voice_id>\d+)_([^.]+)\.(?:mp3|m4a)$")
VOICE_TRACK_RE = re.compile(
    r"voiceTrack.{0,64}?(?P<url>https?://[^\"\\]+)",
    re.IGNORECASE | re.DOTALL,
)
LIZHI_INFO_BASE_URL = "https://m.lizhi.fm/vodapi/voice/info/{voice_id}"
LIZHI_DESKTOP_VOICE_PAGE_URL = "https://www.lizhi.fm/voice/{voice_id}"
LIZHI_MOBILE_VOICE_PAGE_URL = "https://m.lizhi.fm/voice/{voice_id}"


class LizhiResolveError(RuntimeError):
    """Raised when an old Lizhi URL cannot be resolved to a downloadable voiceTrack."""


def rescue_lizhi_jobs(
    root: str = "data",
    *,
    limit: int = 20,
    job_ids: Optional[List[str]] = None,
    statuses: Optional[List[str]] = None,
    apply: bool = False,
    force_apply: bool = False,
    verbose: bool = False,
) -> dict:
    catalog = Catalog(root)
    http = CurlHttpClient()
    jobs = catalog.get_lizhi_rescue_jobs(
        limit=limit,
        job_ids=job_ids,
        statuses=statuses,
    )

    summary = {
        "status": "preview" if not apply else "applied",
        "root": root,
        "limit": limit,
        "apply": apply,
        "force_apply": force_apply,
        "seen": len(jobs),
        "resolved": [],
        "updated": [],
        "warnings": [],
        "errors": [],
    }

    for job in jobs:
        job_id = str(job["job_id"])
        media_url = str(job.get("media_url") or "")
        try:
            voice_id = extract_voice_id(media_url)
            voice_page_url = build_voice_page_url(voice_id)
            voice_info_error: Optional[str] = None
            try:
                voice_info = get_voice_info(voice_id, http=http)
            except Exception as exc:  # noqa: BLE001
                voice_info = None
                voice_info_error = str(exc)
            resolution = resolve_voice_track(
                voice_id=voice_id,
                http=http,
                voice_info=voice_info,
            )
            voice_track_url = resolution["voice_track_url"]
            local_relpath = rewrite_local_relpath(str(job.get("local_relpath") or ""), voice_track_url)
            record = {
                "job_id": job_id,
                "item_id": str(job.get("item_id") or ""),
                "voice_id": voice_id,
                "old_media_url": media_url,
                "voice_page_url": voice_page_url,
                "voice_info_url": build_voice_info_url(voice_id),
                "new_media_url": voice_track_url,
                "new_local_relpath": local_relpath,
                "resolution_method": resolution["method"],
                "resolution_source_url": resolution["source_url"],
                "resolution_attempts": resolution.get("attempts", []),
            }
            if voice_info:
                play_access = (
                    voice_info.get("data", {})
                    .get("userVoice", {})
                    .get("voicePlayProperty", {})
                    .get("playAccessProperty", {})
                )
                if play_access:
                    record["play_access"] = play_access
                token = voice_info.get("token")
                if token:
                    record["voice_info_token"] = token
            if voice_info_error:
                record["voice_info_error"] = voice_info_error

            probe_ok = False
            try:
                probe = probe_download_url(voice_track_url)
                record["probe_status"] = probe["status_code"]
                record["probe_content_type"] = probe["content_type"]
                probe_ok = True
            except Exception as exc:  # noqa: BLE001
                record["probe_error"] = str(exc)

            summary["resolved"].append(record)

            if apply and (probe_ok or force_apply):
                catalog.update_media_job_source(
                    job_id=job_id,
                    media_url=voice_track_url,
                    local_relpath=local_relpath,
                    provider="lizhi_voice_track",
                    access_mode="audio_url",
                    download_method="direct",
                    status="pending",
                    last_error=None,
                )
                summary["updated"].append(record)
            elif apply and not probe_ok:
                summary["warnings"].append(
                    {
                        "job_id": job_id,
                        "item_id": str(job.get("item_id") or ""),
                        "voice_id": voice_id,
                        "warning": "Resolved voiceTrack but did not apply because the download probe failed",
                        "new_media_url": voice_track_url,
                        "probe_error": record.get("probe_error"),
                    }
                )

            if verbose:
                action = "apply" if apply else "preview"
                print(
                    f"[lizhi-rescue] {action} job={job_id} voice_id={voice_id} "
                    f"status={probe['status_code']} new_url={voice_track_url}"
                )
        except Exception as exc:  # noqa: BLE001
            record = {
                "job_id": job_id,
                "item_id": str(job.get("item_id") or ""),
                "media_url": media_url,
                "error": str(exc),
            }
            try:
                record["voice_id"] = extract_voice_id(media_url)
            except Exception:  # noqa: BLE001
                pass
            summary["errors"].append(record)
            if verbose:
                print(f"[lizhi-rescue] error job={job_id} error={exc}")

    summary["catalog"] = catalog.stats()
    return summary


def extract_voice_id(media_url: str) -> str:
    match = OLD_LIZHI_URL_RE.search(media_url)
    if not match:
        raise LizhiResolveError(f"Could not derive Lizhi voice id from URL: {media_url}")
    return match.group("voice_id")


def build_voice_page_url(voice_id: str) -> str:
    return build_voice_page_urls(voice_id)[0]


def build_voice_page_urls(voice_id: str) -> List[str]:
    return [
        LIZHI_DESKTOP_VOICE_PAGE_URL.format(voice_id=voice_id),
        LIZHI_MOBILE_VOICE_PAGE_URL.format(voice_id=voice_id),
    ]


def build_voice_info_url(voice_id: str) -> str:
    return LIZHI_INFO_BASE_URL.format(voice_id=voice_id)


def get_voice_info(voice_id: str, *, http: CurlHttpClient) -> dict:
    url = build_voice_info_url(voice_id)
    payload = http.get_json(url)
    if payload.get("code") != 0:
        raise LizhiResolveError(
            f"Lizhi voice info returned non-zero code for {voice_id}: {json.dumps(payload, ensure_ascii=False)}"
        )
    user_voice = payload.get("data", {}).get("userVoice", {})
    if not user_voice:
        raise LizhiResolveError(f"Missing userVoice payload for Lizhi voice id {voice_id}")
    return payload


def resolve_voice_track(
    *,
    voice_id: str,
    http: CurlHttpClient,
    voice_info: Optional[dict] = None,
) -> dict:
    attempts: List[dict] = []
    if voice_info:
        track_url = (
            voice_info.get("data", {})
            .get("userVoice", {})
            .get("voicePlayProperty", {})
            .get("trackUrl")
        )
        if track_url:
            return {
                "voice_track_url": track_url,
                "method": "voice_info_api",
                "source_url": build_voice_info_url(voice_id),
                "attempts": attempts,
            }

    page_errors: List[str] = []
    for page_url in build_voice_page_urls(voice_id):
        response = http.get_text(page_url)
        attempts.append(
            {
                "source": "voice_page",
                "url": page_url,
                "status_code": response.status_code,
                "effective_url": response.effective_url,
            }
        )
        if response.status_code < 200 or response.status_code >= 300:
            page_errors.append(f"{page_url} -> {response.status_code}")
            continue

        match = VOICE_TRACK_RE.search(response.text)
        if match:
            return {
                "voice_track_url": match.group("url"),
                "method": "voice_page",
                "source_url": page_url,
                "attempts": attempts,
            }

        snippet = response.text[:500].replace("\n", " ")
        page_errors.append(f"{page_url} -> no voiceTrack :: {snippet}")

    joined = "; ".join(page_errors) if page_errors else "no fallback pages attempted"
    raise LizhiResolveError(f"Could not resolve Lizhi voiceTrack for {voice_id}: {joined}")


def rewrite_local_relpath(local_relpath: str, new_media_url: str) -> str:
    if not local_relpath:
        raise LizhiResolveError("Existing local_relpath is missing")

    current = PurePosixPath(local_relpath)
    suffix = media_suffix_from_url(new_media_url)
    if not suffix:
        return local_relpath
    return current.with_suffix(suffix).as_posix()


def media_suffix_from_url(url: str) -> str:
    path = urlparse(url).path or ""
    suffix = PurePosixPath(path).suffix
    return suffix or ""


def probe_download_url(url: str, *, timeout_seconds: int = 30) -> dict:
    command = [
        "curl.exe",
        "-L",
        "-sS",
        "--max-time",
        str(timeout_seconds),
        "--range",
        "0-255",
        "-A",
        (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/123.0.0.0 Safari/537.36"
        ),
        "-H",
        "Referer: https://www.lizhi.fm/",
        "-o",
        "NUL",
        "-w",
        (
            f"\n{STATUS_MARKER}%{{http_code}}"
            f"\n{CONTENT_TYPE_MARKER}%{{content_type}}"
            f"\n{EFFECTIVE_URL_MARKER}%{{url_effective}}\n"
        ),
        url,
    ]
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
        raise LizhiResolveError(f"Probe failed for {url}: {stderr}")

    status_code = parse_marker(completed.stdout, STATUS_MARKER)
    content_type = parse_marker(completed.stdout, CONTENT_TYPE_MARKER)
    effective_url = parse_marker(completed.stdout, EFFECTIVE_URL_MARKER)
    if status_code < 200 or status_code >= 300:
        raise LizhiResolveError(f"Probe returned {status_code} for {url}")
    return {
        "status_code": status_code,
        "content_type": content_type,
        "effective_url": effective_url,
    }


def parse_marker(output: str, marker: str) -> str | int:
    index = output.rfind(f"\n{marker}")
    if index == -1:
        raise LizhiResolveError(f"Missing curl marker {marker!r}")
    line = output[index + 1 : output.find("\n", index + 1)]
    value = line.split(":", 1)[1].strip()
    if marker == STATUS_MARKER:
        return int(value)
    return value
