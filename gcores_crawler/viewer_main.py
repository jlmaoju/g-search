from __future__ import annotations

import argparse
import ctypes
import msvcrt
import os
import platform
import socket
import sys
import threading
import time
from pathlib import Path
from urllib.error import URLError
from urllib.parse import urlparse
from urllib.request import urlopen

import uvicorn

from .query_core import DEFAULT_EMBEDDING_MODEL, DEFAULT_EMBEDDING_PROVIDER
from .search_service import create_search_app
from .viewer import VIEWER_APP_VERSION
from .viewer.keystore import ViewerKeyManager
from .viewer.logging import build_viewer_uvicorn_log_config, enable_windows_ansi_console
from .viewer.platform import get_platform_adapter
from .viewer.service import ViewerLocalState
from .viewer.sidecar import (
    ConfiguredCommandSidecarLauncher,
    build_bundled_qdrant_command,
    parse_sidecar_command,
    resolve_qdrant_bind,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Launch the local GSearch Viewer")
    parser.add_argument("--data-root", default="data/current")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8766)
    parser.add_argument("--qdrant-url", default="http://127.0.0.1:6341")
    parser.add_argument(
        "--qdrant-sidecar-command",
        default=os.environ.get("GCORES_VIEWER_QDRANT_COMMAND", ""),
        help="Optional command used to launch a local Qdrant sidecar. Supports {qdrant_url} and {storage_path}.",
    )
    parser.add_argument("--collection-name", default=os.environ.get("GCORES_QDRANT_COLLECTION", "gcores_memory_v1"))
    parser.add_argument("--embedding-provider", default=DEFAULT_EMBEDDING_PROVIDER)
    parser.add_argument("--embedding-model", default=DEFAULT_EMBEDDING_MODEL)
    parser.add_argument("--download-url", default="")
    parser.add_argument("--no-open-browser", action="store_true")
    return parser


def parse_version(value: str) -> tuple[int, ...]:
    parts = []
    for chunk in str(value or "").split("."):
        try:
            parts.append(int(chunk))
        except ValueError:
            parts.append(0)
    return tuple(parts)


def validate_manifest(data_root: Path) -> dict:
    manifest_path = data_root / "manifest.json"
    if not manifest_path.exists():
        raise RuntimeError(f"Missing manifest.json in {data_root}")

    import json

    manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
    required = [
        "app_min_version",
        "data_version",
        "built_at",
        "collection_name",
        "query_only",
        "doc_type_counts",
        "eligible_items",
        "embedding_provider",
        "embedding_model",
        "target_platform",
    ]
    missing = [key for key in required if key not in manifest]
    if missing:
        raise RuntimeError(f"manifest.json is missing required fields: {', '.join(missing)}")
    if parse_version(VIEWER_APP_VERSION) < parse_version(str(manifest.get("app_min_version") or "")):
        raise RuntimeError(
            f"Viewer {VIEWER_APP_VERSION} is too old for data bundle {manifest.get('data_version')}; "
            f"requires app_min_version={manifest.get('app_min_version')}"
        )
    target_platform = str(manifest.get("target_platform") or "").strip().lower()
    current_platform = current_target_platform()
    if target_platform and target_platform != current_platform:
        raise RuntimeError(
            f"Data bundle target_platform={target_platform} does not match this machine ({current_platform})"
        )
    return manifest


def current_target_platform() -> str:
    machine = platform.machine().lower()
    if sys.platform.startswith("win"):
        return "win-arm64" if "arm" in machine else "win-x64"
    if sys.platform == "darwin":
        return "darwin-arm64" if "arm" in machine else "darwin-x64"
    if sys.platform.startswith("linux"):
        return "linux-arm64" if "arm" in machine else "linux-x64"
    return f"{sys.platform}-{machine or 'unknown'}"


def resolve_app_root() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[1]


def resolve_data_root(value: str, *, app_root: Path) -> Path:
    candidate = Path(str(value or "data/current"))
    if not candidate.is_absolute():
        candidate = app_root / candidate
    return candidate.resolve()


def resolve_sidecar_command(
    *,
    app_root: Path,
    data_root: Path,
    qdrant_url: str,
    explicit_value: str,
    config_dir: Path,
) -> list[str]:
    explicit_command = parse_sidecar_command(explicit_value)
    if explicit_command:
        return explicit_command
    qdrant_binary_name = "qdrant.exe" if sys.platform.startswith("win") else "qdrant"
    bundled_binary = app_root / "vendor" / "qdrant" / qdrant_binary_name
    config_path = config_dir / "qdrant" / "viewer-qdrant.yaml"
    return build_bundled_qdrant_command(
        executable_path=bundled_binary,
        config_path=config_path,
        storage_path=data_root / "qdrant_storage",
        qdrant_url=qdrant_url,
    )


def _is_local_host(host: str) -> bool:
    normalized = str(host or "").strip().lower()
    return normalized in {"127.0.0.1", "localhost", "::1"}


def _is_port_available(host: str, port: int) -> bool:
    family = socket.AF_INET6 if ":" in str(host or "") else socket.AF_INET
    sock = socket.socket(family, socket.SOCK_STREAM)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((host, int(port)))
        return True
    except OSError:
        return False
    finally:
        sock.close()


def _find_available_port(host: str, preferred_port: int, *, search_span: int = 40) -> int:
    for candidate in range(int(preferred_port), int(preferred_port) + int(search_span)):
        if _is_port_available(host, candidate):
            return candidate
    raise RuntimeError(f"No available local port found near {host}:{preferred_port}")


def _rewrite_local_qdrant_url(qdrant_url: str, port: int) -> str:
    parsed = urlparse(str(qdrant_url or "").strip())
    scheme = parsed.scheme or "http"
    host = parsed.hostname or "127.0.0.1"
    return f"{scheme}://{host}:{int(port)}"


def _is_qdrant_http_ready(qdrant_url: str) -> bool:
    try:
        with urlopen(f"{str(qdrant_url).rstrip('/')}/collections", timeout=2) as response:
            return int(getattr(response, "status", 0) or 0) < 500
    except (URLError, TimeoutError, ValueError):
        return False


def resolve_runtime_ports(*, host: str, port: int, qdrant_url: str) -> tuple[int, str]:
    resolved_port = int(port)
    if _is_local_host(host) and not _is_port_available(host, resolved_port):
        new_port = _find_available_port(host, resolved_port + 1)
        print(f"[GSearchViewer] 本地网页端口 {resolved_port} 已被占用，自动改用 {new_port}。")
        resolved_port = new_port

    qdrant_host, qdrant_port = resolve_qdrant_bind(qdrant_url)
    resolved_qdrant_url = qdrant_url
    if _is_local_host(qdrant_host) and not _is_port_available(qdrant_host, qdrant_port):
        if _is_qdrant_http_ready(qdrant_url):
            print(f"[GSearchViewer] 复用已在运行的本地向量库: {qdrant_url}")
        else:
            new_qdrant_port = _find_available_port(qdrant_host, qdrant_port + 1)
            print(f"[GSearchViewer] 本地向量库端口 {qdrant_port} 已被占用，自动改用 {new_qdrant_port}。")
            resolved_qdrant_url = _rewrite_local_qdrant_url(qdrant_url, new_qdrant_port)
    return resolved_port, resolved_qdrant_url


def load_fallback_embedding_api_key(*, app_root: Path) -> str | None:
    env_value = str(os.environ.get("GSEARCH_VIEWER_FALLBACK_API_KEY") or "").strip()
    if env_value:
        return env_value
    fallback_path = app_root / "config" / "secrets" / "default_embedding_api_key.txt"
    if not fallback_path.exists():
        return None
    try:
        value = fallback_path.read_text(encoding="utf-8-sig").strip()
    except OSError:
        return None
    return value or None


def show_startup_error_dialog(message: str) -> None:
    if not sys.platform.startswith("win"):
        return
    try:
        ctypes.windll.user32.MessageBoxW(None, str(message), "GSearch Viewer", 0x10)
    except Exception:
        return


def set_console_title(title: str) -> None:
    if not sys.platform.startswith("win"):
        return
    try:
        ctypes.windll.kernel32.SetConsoleTitleW(str(title))
    except Exception:
        return


def console_note(message: str = "") -> None:
    print(message, flush=True)


def print_startup_banner(*, host: str, port: int | None = None) -> None:
    console_note("")
    console_note("GSearch Viewer 本地版")
    console_note("----------------------")
    console_note("[说明] 正在准备本地数据库和搜索服务，请稍等片刻。")
    console_note("[说明] 正常情况下，浏览器会在几秒到十几秒内自动打开。")
    if port is None:
        console_note("[说明] 如果没有自动打开，请稍后手动访问本地网页。")
    else:
        console_note(f"[说明] 如果没有自动打开，请手动访问: http://{host}:{port}")
    console_note("[说明] 下方灰色的 INFO / GET /api/... 日志属于正常运行信息。")
    console_note("[说明] 按任意键可展开帮助。")
    console_note("[说明] 关闭这个窗口后，本地搜索服务也会一起停止。")
    console_note("")


def start_help_hotkey_listener(*, host: str, port: int) -> None:
    if not sys.platform.startswith("win"):
        return

    def _wait_key() -> None:
        try:
            key = msvcrt.getwch()
        except Exception:
            return
        if not key:
            return
        console_note("")
        console_note("[帮助] 这是在你电脑本地运行的搜索页，不是远程网站后台。")
        console_note(f"[帮助] 如果浏览器没有自动打开，请手动访问: http://{host}:{port}")
        console_note("[帮助] 看到灰色的 GET /api/search 200 OK 日志，说明搜索请求已经成功。")
        console_note("[帮助] 如果搜索提示缺少 API Key，请在网页右上角设置里填写。")
        console_note("[帮助] 关闭这个窗口后，本地搜索服务也会一起停止。")
        console_note("")

    threading.Thread(target=_wait_key, daemon=True).start()


def start_startup_timeout_notice(*, host: str, port: int, timeout_seconds: float = 25.0) -> None:
    def _monitor() -> None:
        deadline = time.time() + timeout_seconds
        while time.time() < deadline:
            if not _is_port_available(host, port):
                return
            time.sleep(0.5)
        print("")
        print("[GSearchViewer] 启动时间比平时更长。")
        print(f"[GSearchViewer] 如果浏览器还没有打开，请手动访问: http://{host}:{port}")
        print("[GSearchViewer] 如果长时间都没有响应，可以关闭这个窗口后重新启动。")
        print("")

    threading.Thread(target=_monitor, daemon=True).start()


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    enable_windows_ansi_console()
    set_console_title("GSearch Viewer 本地版")
    print_startup_banner(host=args.host, port=args.port)

    platform_adapter = get_platform_adapter()
    app_root = resolve_app_root()
    data_root = resolve_data_root(args.data_root, app_root=app_root)
    manifest = validate_manifest(data_root)
    paths = platform_adapter.build_paths(app_root=app_root, data_root=data_root)
    resolved_port, resolved_qdrant_url = resolve_runtime_ports(
        host=args.host,
        port=args.port,
        qdrant_url=args.qdrant_url,
    )
    if resolved_port != args.port:
        console_note(f"[说明] 当前默认端口已被占用，本次改用: http://{args.host}:{resolved_port}")

    start_help_hotkey_listener(host=args.host, port=resolved_port)
    start_startup_timeout_notice(host=args.host, port=resolved_port)

    secure_store = platform_adapter.create_secure_key_store(paths)
    key_manager = ViewerKeyManager(
        secure_store,
        fallback_key=load_fallback_embedding_api_key(app_root=app_root),
    )
    viewer_state = ViewerLocalState(
        platform_name=platform_adapter.name,
        key_manager=key_manager,
        app_version=VIEWER_APP_VERSION,
    )

    sidecar_launcher = ConfiguredCommandSidecarLauncher()
    sidecar_command = resolve_sidecar_command(
        app_root=app_root,
        data_root=data_root,
        qdrant_url=resolved_qdrant_url,
        explicit_value=args.qdrant_sidecar_command,
        config_dir=paths.config_dir,
    )
    sidecar = sidecar_launcher.start(
        qdrant_url=resolved_qdrant_url,
        storage_path=data_root / "qdrant_storage",
        command=sidecar_command,
    )

    app = create_search_app(
        root=str(data_root),
        qdrant_path="qdrant_storage",
        qdrant_url=sidecar.qdrant_url,
        collection_name=str(manifest.get("collection_name") or args.collection_name),
        embedding_provider=str(manifest.get("embedding_provider") or args.embedding_provider),
        embedding_model=str(manifest.get("embedding_model") or args.embedding_model),
        mode="viewer",
        viewer_state=viewer_state,
        download_url=args.download_url or None,
    )

    if not args.no_open_browser:
        browser_url = f"http://{args.host}:{resolved_port}"

        def _open_browser() -> None:
            time.sleep(1.0)
            platform_adapter.open_browser(browser_url)

        threading.Thread(target=_open_browser, daemon=True).start()

    try:
        uvicorn.run(
            app,
            host=args.host,
            port=resolved_port,
            log_config=build_viewer_uvicorn_log_config(),
        )
    finally:
        sidecar.stop()
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        show_startup_error_dialog(str(exc))
        raise
