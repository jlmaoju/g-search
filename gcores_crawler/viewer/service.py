from __future__ import annotations

from typing import Optional

from .keystore import ViewerKeyManager


class ViewerLocalState:
    def __init__(self, *, platform_name: str, key_manager: ViewerKeyManager, app_version: str) -> None:
        self.platform_name = platform_name
        self.key_manager = key_manager
        self.app_version = app_version

    def resolve_embedding_api_key(self) -> Optional[str]:
        return self.key_manager.resolve_api_key()

    def set_api_key(self, api_key: str, *, session_only: bool = False) -> dict:
        self.key_manager.set_key(api_key, session_only=session_only)
        return self.status_payload()

    def clear_api_key(self) -> dict:
        self.key_manager.clear_key()
        return self.status_payload()

    def status_payload(self) -> dict:
        payload = self.key_manager.status_payload()
        payload.update(
            {
                "platform": self.platform_name,
                "app_version": self.app_version,
            }
        )
        return payload
