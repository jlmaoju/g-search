from __future__ import annotations

import ctypes
from ctypes import wintypes
from pathlib import Path
from typing import Optional

from .base import PlatformAdapter, SecureKeyStore, ViewerPaths


class DATA_BLOB(ctypes.Structure):
    _fields_ = [
        ("cbData", wintypes.DWORD),
        ("pbData", ctypes.POINTER(ctypes.c_byte)),
    ]


class WindowsDpapiKeyStore(SecureKeyStore):
    def __init__(self, secrets_dir: Path) -> None:
        self.secrets_dir = secrets_dir
        self._crypt32 = ctypes.windll.crypt32
        self._kernel32 = ctypes.windll.kernel32

    def is_supported(self) -> bool:
        return True

    def _secret_path(self, name: str) -> Path:
        safe_name = str(name or "").strip().replace("/", "_").replace("\\", "_")
        return self.secrets_dir / f"{safe_name}.bin"

    def _protect(self, value: str) -> bytes:
        raw = value.encode("utf-8")
        input_bytes = ctypes.create_string_buffer(raw)
        input_blob = DATA_BLOB(len(raw), ctypes.cast(input_bytes, ctypes.POINTER(ctypes.c_byte)))
        output_blob = DATA_BLOB()
        if not self._crypt32.CryptProtectData(
            ctypes.byref(input_blob),
            "GSearch Viewer Key",
            None,
            None,
            None,
            0,
            ctypes.byref(output_blob),
        ):
            raise ctypes.WinError()
        try:
            return ctypes.string_at(output_blob.pbData, output_blob.cbData)
        finally:
            if output_blob.pbData:
                self._kernel32.LocalFree(output_blob.pbData)

    def _unprotect(self, payload: bytes) -> str:
        input_bytes = ctypes.create_string_buffer(payload)
        input_blob = DATA_BLOB(len(payload), ctypes.cast(input_bytes, ctypes.POINTER(ctypes.c_byte)))
        output_blob = DATA_BLOB()
        if not self._crypt32.CryptUnprotectData(
            ctypes.byref(input_blob),
            None,
            None,
            None,
            None,
            0,
            ctypes.byref(output_blob),
        ):
            raise ctypes.WinError()
        try:
            return ctypes.string_at(output_blob.pbData, output_blob.cbData).decode("utf-8")
        finally:
            if output_blob.pbData:
                self._kernel32.LocalFree(output_blob.pbData)

    def get_key(self, name: str) -> Optional[str]:
        secret_path = self._secret_path(name)
        if not secret_path.exists():
            return None
        try:
            return self._unprotect(secret_path.read_bytes())
        except Exception:
            return None

    def set_key(self, name: str, value: str) -> None:
        secret_path = self._secret_path(name)
        secret_path.parent.mkdir(parents=True, exist_ok=True)
        secret_path.write_bytes(self._protect(value))

    def delete_key(self, name: str) -> None:
        secret_path = self._secret_path(name)
        if secret_path.exists():
            secret_path.unlink()


class WindowsPlatformAdapter(PlatformAdapter):
    name = "windows"

    def create_secure_key_store(self, paths: ViewerPaths) -> SecureKeyStore:
        return WindowsDpapiKeyStore(paths.secrets_dir)
