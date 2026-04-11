from __future__ import annotations

import ctypes
import logging
from copy import deepcopy

from uvicorn.config import LOGGING_CONFIG
from uvicorn.logging import AccessFormatter, DefaultFormatter


ANSI_RESET = "\x1b[0m"
ANSI_DIM_GRAY = "\x1b[90m"


def enable_windows_ansi_console() -> None:
    try:
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.GetStdHandle(-11)
        mode = ctypes.c_uint()
        if handle == 0 or handle == -1:
            return
        if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            return
        kernel32.SetConsoleMode(handle, mode.value | 0x0004)
    except Exception:
        return


class ViewerDefaultFormatter(DefaultFormatter):
    def format(self, record: logging.LogRecord) -> str:
        rendered = super().format(record)
        return f"{ANSI_DIM_GRAY}{rendered}{ANSI_RESET}"


class ViewerAccessFormatter(AccessFormatter):
    def format(self, record: logging.LogRecord) -> str:
        rendered = super().format(record)
        return f"{ANSI_DIM_GRAY}{rendered}{ANSI_RESET}"


def build_viewer_uvicorn_log_config() -> dict:
    config = deepcopy(LOGGING_CONFIG)
    config["formatters"]["default"]["()"] = "gcores_crawler.viewer.logging.ViewerDefaultFormatter"
    config["formatters"]["default"]["use_colors"] = False
    config["formatters"]["access"]["()"] = "gcores_crawler.viewer.logging.ViewerAccessFormatter"
    config["formatters"]["access"]["use_colors"] = False
    config["loggers"]["uvicorn"]["level"] = "WARNING"
    config["loggers"]["uvicorn.error"]["level"] = "WARNING"
    config["loggers"]["uvicorn.access"]["level"] = "INFO"
    return config
