from __future__ import annotations

import sys

from .base import PlatformAdapter
from .macos import MacOSPlatformAdapter
from .windows import WindowsPlatformAdapter


def get_platform_adapter() -> PlatformAdapter:
    if sys.platform.startswith("win"):
        return WindowsPlatformAdapter()
    if sys.platform == "darwin":
        return MacOSPlatformAdapter()
    return PlatformAdapter()
