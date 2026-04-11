from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timezone
import json
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
import sys
from typing import Optional
from urllib.parse import urlparse
from urllib.request import Request as UrlRequest, urlopen

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from .query_core import (
    DEFAULT_COLLECTION,
    DEFAULT_EMBEDDING_MODEL,
    DEFAULT_EMBEDDING_PROVIDER,
    DEFAULT_QDRANT_PATH,
    QueryCatalog,
    QueryRuntime,
    ReadOnlySearchQdrantStore,
    SearchIndexConfig,
    SearchTuning,
    resolve_alias_map,
)
from .viewer.service import ViewerLocalState


def _resolve_default_frontend_dir() -> Path:
    if getattr(sys, "frozen", False):
        meipass = getattr(sys, "_MEIPASS", "")
        if meipass:
            bundled = Path(meipass) / "gcores_crawler" / "frontend"
            if bundled.exists():
                return bundled
        executable_dir = Path(sys.executable).resolve().parent
        sibling = executable_dir / "gcores_crawler" / "frontend"
        if sibling.exists():
            return sibling
    return Path(__file__).resolve().parent / "frontend"


DEFAULT_SITE_TITLE = "机核电台记忆检索"
DEFAULT_SITE_SUBTITLE = "一句话找回那期节目"


def _create_search_audit_logger(root: str) -> logging.Logger:
    logs_dir = Path(root) / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    log_path = logs_dir / "online_search_queries.jsonl"
    logger_name = f"gcores.search.audit.{log_path.resolve()}"
    logger = logging.getLogger(logger_name)
    logger.setLevel(logging.INFO)
    logger.propagate = False
    if not logger.handlers:
        handler = RotatingFileHandler(
            log_path,
            maxBytes=10 * 1024 * 1024,
            backupCount=5,
            encoding="utf-8",
        )
        handler.setFormatter(logging.Formatter("%(message)s"))
        logger.addHandler(handler)
    return logger


def _extract_client_ip(request: Request) -> Optional[str]:
    forwarded_for = (request.headers.get("x-forwarded-for") or "").strip()
    if forwarded_for:
        return forwarded_for.split(",")[0].strip()
    real_ip = (request.headers.get("x-real-ip") or "").strip()
    if real_ip:
        return real_ip
    if request.client:
        return request.client.host
    return None


def _write_search_audit_log(
    logger: logging.Logger,
    request: Request,
    *,
    query: str,
    scope: str,
    limit: int,
    doc_types: Optional[list[str]],
    categories: Optional[list[str]],
    participants: Optional[list[str]],
    status: str,
    result_count: int = 0,
    error: Optional[str] = None,
) -> None:
    payload = {
        "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "mode": getattr(request.app.state, "mode", "unknown"),
        "query": query,
        "scope": scope,
        "limit": limit,
        "doc_types": doc_types or [],
        "categories": categories or [],
        "participants": participants or [],
        "status": status,
        "result_count": result_count,
        "client_ip": _extract_client_ip(request),
        "forwarded_for": request.headers.get("x-forwarded-for"),
        "host": request.headers.get("host"),
        "referer": request.headers.get("referer"),
        "user_agent": request.headers.get("user-agent"),
    }
    if error:
        payload["error"] = error
    try:
        logger.info(json.dumps(payload, ensure_ascii=False))
    except Exception:
        return


def _collect_live_meta_snapshot(request: Request) -> dict:
    catalog = request.app.state.catalog
    manifest = catalog.load_manifest() or {}
    collection_name = request.app.state.search_config.collection_name
    manifest_doc_type_counts = manifest.get("doc_type_counts") if isinstance(manifest, dict) else None
    doc_type_counts = (
        manifest_doc_type_counts
        if isinstance(manifest_doc_type_counts, dict) and manifest_doc_type_counts
        else catalog.list_collection_doc_type_counts(collection_name=collection_name)
    )
    manifest_eligible_items = manifest.get("eligible_items") if isinstance(manifest, dict) else None
    eligible_items = (
        int(manifest_eligible_items)
        if manifest_eligible_items is not None
        else catalog.count_eligible_items(collection_name=collection_name)
    )
    return {
        "manifest": manifest,
        "library_updated_at": catalog.resolve_library_updated_at(manifest=manifest),
        "eligible_items": eligible_items,
        "doc_type_counts": doc_type_counts,
        "participants_count": catalog.count_radio_participants(),
        "program_types_count": catalog.count_radio_categories(),
        "program_types": catalog.list_radio_categories(limit=None),
        "database_size_bytes": catalog.db_path.stat().st_size if catalog.db_path.exists() else None,
    }


def _collect_cached_meta_snapshot(request: Request) -> dict:
    catalog = request.app.state.catalog
    manifest = catalog.load_manifest() or {}
    snapshot = catalog.load_search_ui_meta_snapshot() or {}
    if not snapshot:
        return _collect_live_meta_snapshot(request)
    program_types_count = snapshot.get("program_types_count")
    return {
        "manifest": manifest,
        "library_updated_at": snapshot.get("library_updated_at") or catalog.resolve_library_updated_at(manifest=manifest),
        "eligible_items": snapshot.get("eligible_items"),
        "doc_type_counts": snapshot.get("doc_type_counts") or {},
        "participants_count": snapshot.get("participants_count", 0),
        "program_types_count": int(program_types_count) if program_types_count is not None else catalog.count_radio_categories(),
        "database_size_bytes": snapshot.get("database_size_bytes"),
        "participants": snapshot.get("participants") or [],
        "program_types": snapshot.get("program_types") or [],
    }


def create_search_app(
    *,
    root: str = "data",
    qdrant_path: str = DEFAULT_QDRANT_PATH,
    qdrant_url: Optional[str] = None,
    qdrant_api_key: Optional[str] = None,
    collection_name: str = DEFAULT_COLLECTION,
    embedding_provider: str = DEFAULT_EMBEDDING_PROVIDER,
    embedding_model: str = DEFAULT_EMBEDDING_MODEL,
    embedding_api_key: Optional[str] = None,
    embedding_dimension: Optional[int] = None,
    embedding_batch_size: int = 64,
    embedding_concurrency: int = 8,
    alias_map_path: Optional[str] = None,
    mode: str = "server",
    viewer_state: Optional[ViewerLocalState] = None,
    frontend_dir: Optional[str] = None,
    download_url: Optional[str] = None,
    site_title: str = DEFAULT_SITE_TITLE,
    site_subtitle: str = DEFAULT_SITE_SUBTITLE,
) -> FastAPI:
    resolved_mode = str(mode or "server").strip().lower()
    if resolved_mode not in {"server", "viewer"}:
        raise ValueError(f"Unsupported search service mode: {mode}")
    config = SearchIndexConfig(
        root=root,
        qdrant_path=qdrant_path,
        qdrant_url=qdrant_url,
        qdrant_api_key=qdrant_api_key,
        collection_name=collection_name,
        embedding_provider=embedding_provider,
        embedding_model=embedding_model,
        embedding_api_key=embedding_api_key,
        embedding_dimension=embedding_dimension,
        embedding_batch_size=embedding_batch_size,
        embedding_concurrency=embedding_concurrency,
        alias_map_path=alias_map_path,
        embedding_api_key_resolver=viewer_state.resolve_embedding_api_key if viewer_state else None,
    )
    resolved_frontend_dir = Path(frontend_dir) if frontend_dir else _resolve_default_frontend_dir()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        catalog = QueryCatalog(root)
        qdrant = ReadOnlySearchQdrantStore(config)
        runtime = QueryRuntime(
            catalog=catalog,
            qdrant=qdrant,
            config=config,
            alias_map=resolve_alias_map(config),
        )
        app.state.catalog = catalog
        app.state.qdrant = qdrant
        app.state.runtime = runtime
        app.state.search_config = config
        app.state.mode = resolved_mode
        app.state.viewer_state = viewer_state
        app.state.download_url = download_url
        app.state.site_title = site_title
        app.state.site_subtitle = site_subtitle
        app.state.search_audit_logger = _create_search_audit_logger(root)
        try:
            yield
        finally:
            qdrant.close()

    app = FastAPI(title="Gcores Query Service", version="0.5.0", lifespan=lifespan)
    app.mount("/assets", StaticFiles(directory=str(resolved_frontend_dir / "assets")), name="assets")

    @app.get("/", response_class=FileResponse)
    async def index() -> FileResponse:
        return FileResponse(resolved_frontend_dir / "index.html")

    @app.get("/api/search", response_class=JSONResponse)
    async def search(
        request: Request,
        q: str = Query(..., min_length=1),
        limit: int = Query(10, ge=1, le=50),
        scope: str = Query("content"),
        doc_types: Optional[list[str]] = Query(None),
        categories: Optional[list[str]] = Query(None),
        participants: Optional[list[str]] = Query(None),
        candidate_limit: int = Query(150, ge=10, le=500),
        rerank_enabled: bool = Query(True),
        diversify_enabled: bool = Query(True),
        second_pass_atom_enabled: bool = Query(True),
        second_pass_atom_limit: int = Query(60, ge=0, le=200),
        doc_type_prior_weight: float = Query(1.0, ge=0.0, le=5.0),
        keyword_overlap_weight: float = Query(1.0, ge=0.0, le=5.0),
        title_weight: float = Query(1.0, ge=0.0, le=5.0),
        timeline_title_weight: float = Query(1.0, ge=0.0, le=5.0),
        participant_weight: float = Query(1.0, ge=0.0, le=5.0),
        multi_hit_weight: float = Query(1.0, ge=0.0, le=5.0),
    ) -> JSONResponse:
        tuning = SearchTuning(
            candidate_limit=candidate_limit,
            rerank_enabled=rerank_enabled,
            diversify_enabled=diversify_enabled,
            second_pass_atom_enabled=second_pass_atom_enabled,
            second_pass_atom_limit=second_pass_atom_limit,
            doc_type_prior_weight=doc_type_prior_weight,
            keyword_overlap_weight=keyword_overlap_weight,
            title_weight=title_weight,
            timeline_title_weight=timeline_title_weight,
            participant_weight=participant_weight,
            multi_hit_weight=multi_hit_weight,
        )
        try:
            payload = request.app.state.runtime.search(
                q,
                limit=limit,
                scope=scope,
                doc_types=doc_types,
                categories=categories,
                participants=participants,
                tuning=tuning,
            )
            if request.app.state.mode == "server":
                _write_search_audit_log(
                    request.app.state.search_audit_logger,
                    request,
                    query=q,
                    scope=scope,
                    limit=limit,
                    doc_types=doc_types,
                    categories=categories,
                    participants=participants,
                    status="ok",
                    result_count=len(payload.get("results") or []),
                )
            return JSONResponse(payload)
        except ValueError as exc:
            if request.app.state.mode == "server":
                _write_search_audit_log(
                    request.app.state.search_audit_logger,
                    request,
                    query=q,
                    scope=scope,
                    limit=limit,
                    doc_types=doc_types,
                    categories=categories,
                    participants=participants,
                    status="bad_request",
                    error=str(exc),
                )
            return JSONResponse({"error": str(exc), "query": q}, status_code=400)
        except Exception as exc:
            if request.app.state.mode == "server":
                _write_search_audit_log(
                    request.app.state.search_audit_logger,
                    request,
                    query=q,
                    scope=scope,
                    limit=limit,
                    doc_types=doc_types,
                    categories=categories,
                    participants=participants,
                    status="error",
                    error=str(exc),
                )
            return JSONResponse(
                {
                    "error": str(exc),
                    "query": q,
                    "scope": scope,
                    "doc_types": doc_types or [],
                    "categories": categories or [],
                },
                status_code=500,
            )

    @app.get("/api/episodes/{item_id}", response_class=JSONResponse)
    async def episode(request: Request, item_id: str) -> JSONResponse:
        payload = request.app.state.runtime.episode_details(item_id)
        return JSONResponse(payload)

    @app.get("/api/participants", response_class=JSONResponse)
    async def participant_list(request: Request) -> JSONResponse:
        meta_snapshot = _collect_cached_meta_snapshot(request)
        participants = meta_snapshot.get("participants") or request.app.state.catalog.list_radio_participants(limit=160)
        program_types = meta_snapshot.get("program_types") or request.app.state.catalog.list_radio_categories(limit=None)
        return JSONResponse({"participants": participants, "program_types": program_types})

    @app.get("/api/meta", response_class=JSONResponse)
    async def meta(request: Request) -> JSONResponse:
        viewer_payload = None
        if request.app.state.viewer_state is not None:
            viewer_payload = request.app.state.viewer_state.status_payload()
        live_meta = _collect_cached_meta_snapshot(request)
        return JSONResponse(
            {
                "mode": request.app.state.mode,
                "mode_label": "本地版" if request.app.state.mode == "viewer" else "在线版",
                "title": request.app.state.site_title,
                "subtitle": request.app.state.site_subtitle,
                "manifest": live_meta["manifest"],
                "library_updated_at": live_meta["library_updated_at"],
                "eligible_items": live_meta["eligible_items"],
                "doc_type_counts": live_meta["doc_type_counts"],
                "participants_count": live_meta["participants_count"],
                "program_types_count": live_meta["program_types_count"],
                "database_size_bytes": live_meta["database_size_bytes"],
                "download_url": request.app.state.download_url,
                "viewer": viewer_payload,
                "capabilities": {
                    "local_key_api": request.app.state.viewer_state is not None,
                    "timeline_asset_proxy": True,
                },
            }
        )

    @app.get("/api/health", response_class=JSONResponse)
    async def health(request: Request) -> JSONResponse:
        try:
            collection_exists = request.app.state.qdrant.collection_exists()
            payload = {
                "status": "ok" if collection_exists else "degraded",
                "mode": request.app.state.mode,
                "collection_name": request.app.state.search_config.collection_name,
                "collection_exists": collection_exists,
                "data_root": request.app.state.search_config.root,
            }
            status_code = 200 if collection_exists else 503
            return JSONResponse(payload, status_code=status_code)
        except Exception as exc:
            return JSONResponse(
                {
                    "status": "error",
                    "mode": request.app.state.mode,
                    "error": str(exc),
                },
                status_code=503,
            )

    async def fetch_media_asset(url: str) -> Response:
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"}:
            raise HTTPException(status_code=400, detail="unsupported asset url")
        hostname = (parsed.hostname or "").lower()
        if not hostname.endswith("gcores.com"):
            raise HTTPException(status_code=400, detail="unsupported asset host")
        req = UrlRequest(
            url,
            headers={
                "User-Agent": "Mozilla/5.0",
                "Referer": "https://www.gcores.com/",
            },
        )
        try:
            with urlopen(req, timeout=20) as resp:
                content = resp.read()
                content_type = resp.headers.get_content_type() or "application/octet-stream"
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"asset fetch failed: {exc}") from exc
        return Response(
            content=content,
            media_type=content_type,
            headers={"Cache-Control": "public, max-age=86400"},
        )

    @app.get("/api/timeline-asset")
    async def timeline_asset(url: str = Query(..., min_length=1)) -> Response:
        return await fetch_media_asset(url)

    @app.get("/api/media-asset")
    async def media_asset(url: str = Query(..., min_length=1)) -> Response:
        return await fetch_media_asset(url)

    if viewer_state is not None:

        @app.get("/api/local/status", response_class=JSONResponse)
        async def local_status(request: Request) -> JSONResponse:
            return JSONResponse(request.app.state.viewer_state.status_payload())

        @app.post("/api/local/key", response_class=JSONResponse)
        async def save_local_key(request: Request) -> JSONResponse:
            body = await request.json()
            api_key = str(body.get("api_key") or "").strip()
            session_only = bool(body.get("session_only"))
            if not api_key:
                return JSONResponse({"error": "api_key is required"}, status_code=400)
            try:
                payload = request.app.state.viewer_state.set_api_key(api_key, session_only=session_only)
            except Exception as exc:
                return JSONResponse({"error": str(exc)}, status_code=400)
            return JSONResponse(payload)

        @app.delete("/api/local/key", response_class=JSONResponse)
        async def delete_local_key(request: Request) -> JSONResponse:
            payload = request.app.state.viewer_state.clear_api_key()
            return JSONResponse(payload)

    return app
