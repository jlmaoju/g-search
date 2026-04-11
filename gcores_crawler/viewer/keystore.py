from __future__ import annotations

from typing import Optional

from .platform.base import SecureKeyStore


DEFAULT_VIEWER_KEY_NAME = "zhipu_api_key"


class ViewerKeyManager:
    def __init__(
        self,
        secure_store: SecureKeyStore,
        *,
        key_name: str = DEFAULT_VIEWER_KEY_NAME,
        fallback_key: Optional[str] = None,
    ) -> None:
        self.secure_store = secure_store
        self.key_name = key_name
        self._session_key: Optional[str] = None
        self._fallback_key = str(fallback_key or "").strip() or None

    def resolve_api_key(self) -> Optional[str]:
        if self._session_key:
            return self._session_key
        stored = self.secure_store.get_key(self.key_name)
        if stored:
            return stored
        return self._fallback_key

    def has_key(self) -> bool:
        return self.resolve_api_key() is not None

    def set_key(self, api_key: str, *, session_only: bool = False) -> None:
        cleaned = str(api_key or "").strip()
        if not cleaned:
            raise ValueError("api_key is required")
        self._session_key = cleaned
        if session_only:
            return
        self.secure_store.set_key(self.key_name, cleaned)

    def clear_key(self) -> None:
        self._session_key = None
        self.secure_store.delete_key(self.key_name)

    def status_payload(self) -> dict:
        return {
            "secure_store_supported": self.secure_store.is_supported(),
            "session_key_loaded": self._session_key is not None,
            "stored_key_present": self.secure_store.has_key(self.key_name),
            "fallback_key_present": self._fallback_key is not None,
            "effective_key_present": self.has_key(),
        }
