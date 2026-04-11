from __future__ import annotations

import subprocess
from typing import Dict


def quiet_subprocess_kwargs() -> Dict[str, object]:
    kwargs: Dict[str, object] = {}
    create_no_window = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    startupinfo_cls = getattr(subprocess, "STARTUPINFO", None)
    startf_use_show_window = getattr(subprocess, "STARTF_USESHOWWINDOW", 0)

    if create_no_window:
        kwargs["creationflags"] = create_no_window

    if startupinfo_cls is not None and startf_use_show_window:
        startupinfo = startupinfo_cls()
        startupinfo.dwFlags |= startf_use_show_window
        kwargs["startupinfo"] = startupinfo

    return kwargs
