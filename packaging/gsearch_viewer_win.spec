# -*- mode: python ; coding: utf-8 -*-

from pathlib import Path

from PyInstaller.utils.hooks import collect_submodules


_spec_anchor = Path(globals().get("SPECPATH") or Path.cwd()).resolve()
if _spec_anchor.is_file():
    _spec_dir = _spec_anchor.parent
else:
    _spec_dir = _spec_anchor
PROJECT_ROOT = _spec_dir.parent if _spec_dir.name == "packaging" else _spec_dir
ENTRY = PROJECT_ROOT / "packaging" / "viewer_windows_entry.py"
FRONTEND_DIR = PROJECT_ROOT / "gcores_crawler" / "frontend"

datas = [
    (str(FRONTEND_DIR), "gcores_crawler/frontend"),
]

hiddenimports = []
for package_name in ("fastapi", "starlette", "uvicorn", "qdrant_client"):
    hiddenimports += collect_submodules(package_name)

a = Analysis(
    [str(ENTRY)],
    pathex=[str(PROJECT_ROOT)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="GSearchViewer",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=True,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name="GSearchViewer",
)
