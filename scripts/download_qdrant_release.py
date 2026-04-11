from __future__ import annotations

import argparse
import json
import shutil
import tempfile
import urllib.request
import zipfile
from pathlib import Path


GITHUB_API_TEMPLATE = "https://api.github.com/repos/qdrant/qdrant/releases/tags/{tag}"
LATEST_RELEASE_URL = "https://api.github.com/repos/qdrant/qdrant/releases/latest"

ASSET_NAMES = {
    "win-x64": "qdrant-x86_64-pc-windows-msvc.zip",
    "darwin-x64": "qdrant-x86_64-apple-darwin.tar.gz",
    "darwin-arm64": "qdrant-aarch64-apple-darwin.tar.gz",
    "linux-x64": "qdrant-x86_64-unknown-linux-gnu.tar.gz",
    "linux-arm64": "qdrant-aarch64-unknown-linux-musl.tar.gz",
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Download an official Qdrant binary for Viewer distribution")
    parser.add_argument("--version", default="v1.17.1", help="Release tag, or 'latest'")
    parser.add_argument("--platform", default="win-x64", choices=sorted(ASSET_NAMES))
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--overwrite", action="store_true")
    return parser.parse_args()


def fetch_release(version: str) -> dict:
    url = LATEST_RELEASE_URL if str(version).strip().lower() == "latest" else GITHUB_API_TEMPLATE.format(tag=version)
    request = urllib.request.Request(url, headers={"User-Agent": "gsearch-viewer-builder"})
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.load(response)


def download(url: str, destination: Path) -> None:
    request = urllib.request.Request(url, headers={"User-Agent": "gsearch-viewer-builder"})
    with urllib.request.urlopen(request, timeout=60) as response, destination.open("wb") as handle:
        shutil.copyfileobj(response, handle)


def main() -> int:
    args = parse_args()
    release = fetch_release(args.version)
    asset_name = ASSET_NAMES[args.platform]
    asset = next((item for item in release.get("assets", []) if item.get("name") == asset_name), None)
    if asset is None:
        raise RuntimeError(f"Could not find asset {asset_name!r} in release {release.get('tag_name')}")

    output_dir = Path(args.output_dir).resolve()
    if output_dir.exists():
        if not args.overwrite:
            raise FileExistsError(f"Output dir already exists: {output_dir}")
        shutil.rmtree(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="gsearch-qdrant-") as temp_dir_str:
        temp_dir = Path(temp_dir_str)
        archive_path = temp_dir / asset_name
        download(str(asset.get("browser_download_url") or ""), archive_path)
        if asset_name.endswith(".zip"):
            with zipfile.ZipFile(archive_path) as archive:
                archive.extractall(temp_dir / "unzipped")
        else:
            raise RuntimeError(f"Unsupported archive format for {asset_name}")
        extracted_root = temp_dir / "unzipped"
        binaries = list(extracted_root.rglob("qdrant.exe"))
        if not binaries:
            raise RuntimeError(f"Could not find qdrant.exe in {archive_path}")
        source_binary = binaries[0]
        target_binary = output_dir / source_binary.name
        shutil.copy2(source_binary, target_binary)

    manifest = {
        "release_tag": release.get("tag_name"),
        "asset_name": asset_name,
        "asset_url": asset.get("browser_download_url"),
        "binary_path": str((output_dir / "qdrant.exe").resolve()),
    }
    (output_dir / "qdrant_release.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(json.dumps(manifest, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
