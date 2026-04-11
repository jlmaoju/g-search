from __future__ import annotations

import shlex
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Sequence
from urllib.parse import urlparse
from urllib.error import URLError
from urllib.request import urlopen


@dataclass
class ManagedSidecar:
    qdrant_url: str
    process: Optional[subprocess.Popen[str]] = None

    def stop(self) -> None:
        if self.process is None:
            return
        if self.process.poll() is not None:
            return
        self.process.terminate()
        try:
            self.process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait(timeout=5)


def _is_qdrant_ready(qdrant_url: str) -> bool:
    try:
        with urlopen(f"{qdrant_url.rstrip('/')}/collections", timeout=2) as response:
            return int(getattr(response, "status", 0) or 0) < 500
    except (URLError, TimeoutError, ValueError):
        return False


class ConfiguredCommandSidecarLauncher:
    def __init__(self, *, startup_timeout_seconds: float = 20.0) -> None:
        self.startup_timeout_seconds = startup_timeout_seconds

    def start(
        self,
        *,
        qdrant_url: str,
        storage_path: Path,
        command: Optional[Sequence[str]] = None,
    ) -> ManagedSidecar:
        if _is_qdrant_ready(qdrant_url):
            return ManagedSidecar(qdrant_url=qdrant_url, process=None)
        if not command:
            raise RuntimeError(
                "Qdrant sidecar is not running and no sidecar command was provided. "
                "Set GCORES_VIEWER_QDRANT_COMMAND or pass --qdrant-sidecar-command."
            )
        rendered = [
            str(part).format(
                qdrant_url=qdrant_url,
                storage_path=str(storage_path),
            )
            for part in command
        ]
        process = subprocess.Popen(
            rendered,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            text=True,
        )
        deadline = time.time() + self.startup_timeout_seconds
        while time.time() < deadline:
            if process.poll() is not None:
                raise RuntimeError("Qdrant sidecar exited before becoming ready")
            if _is_qdrant_ready(qdrant_url):
                return ManagedSidecar(qdrant_url=qdrant_url, process=process)
            time.sleep(0.5)
        process.terminate()
        raise RuntimeError("Qdrant sidecar did not become ready before timeout")


def parse_sidecar_command(value: Optional[str]) -> list[str]:
    text = str(value or "").strip()
    if not text:
        return []
    return shlex.split(text, posix=False)


def resolve_qdrant_bind(qdrant_url: str) -> tuple[str, int]:
    parsed = urlparse(str(qdrant_url or "").strip())
    scheme = (parsed.scheme or "http").lower()
    if scheme not in {"http", ""}:
        raise RuntimeError(f"Bundled Qdrant sidecar only supports local http URLs, got: {qdrant_url}")
    host = parsed.hostname or "127.0.0.1"
    port = int(parsed.port or 6333)
    return host, port


def write_local_qdrant_config(
    *,
    config_path: Path,
    storage_path: Path,
    qdrant_url: str,
) -> Path:
    host, port = resolve_qdrant_bind(qdrant_url)
    resolved_storage = storage_path.resolve()
    snapshots_path = resolved_storage.parent / "qdrant_snapshots"
    resolved_storage.mkdir(parents=True, exist_ok=True)
    snapshots_path.mkdir(parents=True, exist_ok=True)
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_yaml = (
        "log_level: INFO\n\n"
        "storage:\n"
        f"  storage_path: {resolved_storage.as_posix()}\n"
        f"  snapshots_path: {snapshots_path.resolve().as_posix()}\n"
        "  on_disk_payload: true\n\n"
        "service:\n"
        f"  host: {host}\n"
        f"  http_port: {port}\n"
    )
    config_path.write_text(config_yaml, encoding="utf-8")
    return config_path


def build_bundled_qdrant_command(
    *,
    executable_path: Path,
    config_path: Path,
    storage_path: Path,
    qdrant_url: str,
) -> list[str]:
    if not executable_path.exists():
        return []
    write_local_qdrant_config(
        config_path=config_path,
        storage_path=storage_path,
        qdrant_url=qdrant_url,
    )
    return [
        str(executable_path),
        "--config-path",
        str(config_path),
        "--disable-telemetry",
    ]
