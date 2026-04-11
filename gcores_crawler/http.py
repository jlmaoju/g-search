from __future__ import annotations

import json
import subprocess
import time
from dataclasses import dataclass
from typing import Dict, Optional

from .subprocess_utils import quiet_subprocess_kwargs


STATUS_MARKER = "__GC_STATUS__:"
CONTENT_TYPE_MARKER = "__GC_CONTENT_TYPE__:"
EFFECTIVE_URL_MARKER = "__GC_EFFECTIVE_URL__:"


class HttpError(RuntimeError):
    """Raised when the upstream response is not usable."""


@dataclass
class HttpResponse:
    status_code: int
    content_type: str
    effective_url: str
    text: str

    @property
    def is_jsonish(self) -> bool:
        lowered = self.content_type.lower()
        return "json" in lowered or self.text.lstrip().startswith("{")


class CurlHttpClient:
    def __init__(
        self,
        timeout_seconds: int = 60,
        retries: int = 3,
        retry_backoff_seconds: float = 1.5,
        user_agent: str = (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/123.0.0.0 Safari/537.36"
        ),
    ) -> None:
        self.timeout_seconds = timeout_seconds
        self.retries = retries
        self.retry_backoff_seconds = retry_backoff_seconds
        self.user_agent = user_agent

    def get_text(self, url: str, headers: Optional[Dict[str, str]] = None) -> HttpResponse:
        last_error: Optional[Exception] = None
        for attempt in range(1, self.retries + 1):
            try:
                return self._run_curl(url, headers=headers or {})
            except Exception as exc:  # noqa: BLE001
                last_error = exc
                if attempt == self.retries:
                    break
                time.sleep(self.retry_backoff_seconds * attempt)
        raise HttpError(f"Failed to fetch {url}: {last_error}") from last_error

    def get_json(self, url: str, headers: Optional[Dict[str, str]] = None) -> dict:
        response = self.get_text(url, headers=headers)
        if response.status_code < 200 or response.status_code >= 300:
            snippet = response.text[:500].replace("\n", " ")
            raise HttpError(
                f"Non-success response for {url}: {response.status_code} {response.content_type} {snippet}"
            )
        if not response.is_jsonish:
            snippet = response.text[:500].replace("\n", " ")
            raise HttpError(
                f"Expected JSON for {url}, got {response.content_type}: {snippet}"
            )
        try:
            return json.loads(response.text)
        except json.JSONDecodeError as exc:
            snippet = response.text[:500].replace("\n", " ")
            raise HttpError(f"Invalid JSON from {url}: {snippet}") from exc

    def _run_curl(self, url: str, headers: Dict[str, str]) -> HttpResponse:
        command = [
            "curl.exe",
            "--compressed",
            "-sS",
            "-L",
            "--max-time",
            str(self.timeout_seconds),
            "-A",
            self.user_agent,
            "-w",
            (
                f"\n{STATUS_MARKER}%{{http_code}}"
                f"\n{CONTENT_TYPE_MARKER}%{{content_type}}"
                f"\n{EFFECTIVE_URL_MARKER}%{{url_effective}}\n"
            ),
            url,
        ]
        for key, value in headers.items():
            command.extend(["-H", f"{key}: {value}"])

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
            stderr = completed.stderr.strip()
            raise HttpError(f"curl.exe failed for {url}: {stderr}")
        return self._parse_curl_output(completed.stdout)

    def _parse_curl_output(self, output: str) -> HttpResponse:
        status_index = output.rfind(f"\n{STATUS_MARKER}")
        ct_index = output.rfind(f"\n{CONTENT_TYPE_MARKER}")
        effective_index = output.rfind(f"\n{EFFECTIVE_URL_MARKER}")
        if min(status_index, ct_index, effective_index) == -1:
            raise HttpError("Could not parse curl markers from output")

        body = output[:status_index]
        status_line = output[status_index + 1 : output.find("\n", status_index + 1)]
        ct_line = output[ct_index + 1 : output.find("\n", ct_index + 1)]
        effective_line = output[effective_index + 1 : output.find("\n", effective_index + 1)]

        status_code = int(status_line.split(":", 1)[1].strip())
        content_type = ct_line.split(":", 1)[1].strip()
        effective_url = effective_line.split(":", 1)[1].strip()
        return HttpResponse(
            status_code=status_code,
            content_type=content_type,
            effective_url=effective_url,
            text=body,
        )
