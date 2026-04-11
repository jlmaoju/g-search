from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Optional


@dataclass
class ViewerPaths:
    app_root: Path
    data_root: Path
    config_dir: Path
    logs_dir: Path
    secrets_dir: Path


class SecureKeyStore:
    def is_supported(self) -> bool:
        return False

    def has_key(self, name: str) -> bool:
        return self.get_key(name) is not None

    def get_key(self, name: str) -> Optional[str]:
        return None

    def set_key(self, name: str, value: str) -> None:
        raise NotImplementedError

    def delete_key(self, name: str) -> None:
        raise NotImplementedError


class NoOpSecureKeyStore(SecureKeyStore):
    def set_key(self, name: str, value: str) -> None:
        raise RuntimeError("secure key storage is not available on this platform")

    def delete_key(self, name: str) -> None:
        return None


class PlatformAdapter:
    name = "unknown"

    def build_paths(self, *, app_root: Path, data_root: Path) -> ViewerPaths:
        config_dir = app_root / "config"
        logs_dir = app_root / "logs"
        secrets_dir = config_dir / "secrets"
        for path in (config_dir, logs_dir, secrets_dir):
            path.mkdir(parents=True, exist_ok=True)
        return ViewerPaths(
            app_root=app_root,
            data_root=data_root,
            config_dir=config_dir,
            logs_dir=logs_dir,
            secrets_dir=secrets_dir,
        )

    def create_secure_key_store(self, paths: ViewerPaths) -> SecureKeyStore:
        return NoOpSecureKeyStore()

    def open_browser(self, url: str) -> None:
        import webbrowser

        webbrowser.open(url, new=1)
