from __future__ import annotations

from .base import NoOpSecureKeyStore, PlatformAdapter, SecureKeyStore, ViewerPaths


class MacOSPlatformAdapter(PlatformAdapter):
    name = "macos"

    def create_secure_key_store(self, paths: ViewerPaths) -> SecureKeyStore:
        return NoOpSecureKeyStore()
