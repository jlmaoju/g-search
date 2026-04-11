from __future__ import annotations

import json
import sqlite3
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .query_core import QueryCatalog, SearchIndexConfig, build_search_ui_meta_snapshot
from .viewer import VIEWER_APP_VERSION


QUERY_ONLY_UNUSED_TABLES = (
    "chunks",
    "media_jobs",
    "transcripts",
    "sync_state",
    "search_documents",
    "enrichment_jobs",
)


def _replace_with_retry(source: Path, target: Path, *, attempts: int = 10, delay_seconds: float = 0.5) -> None:
    last_error: Optional[OSError] = None
    for attempt in range(attempts):
        try:
            source.replace(target)
            return
        except OSError as exc:
            last_error = exc
            if attempt == attempts - 1:
                break
            time.sleep(delay_seconds)
    if last_error is not None:
        raise last_error


def _qdrant_storage_has_collection(storage_root: Path, collection_name: str) -> bool:
    normalized = str(collection_name or "").strip()
    if not normalized:
        return False
    candidate_dirs = [
        storage_root / "collections" / normalized,
        storage_root / "collection" / normalized,
    ]
    if any(path.exists() for path in candidate_dirs):
        return True
    meta_path = storage_root / "meta.json"
    if not meta_path.exists():
        return False
    try:
        payload = json.loads(meta_path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return False
    collections = payload.get("collections")
    return isinstance(collections, dict) and normalized in collections


def _prune_query_only_catalog(
    *,
    source_catalog: Path,
    target_catalog: Path,
    collection_name: str,
) -> dict[str, int]:
    normalized_collection = str(collection_name or "").strip()
    if not normalized_collection:
        raise ValueError("collection_name is required for query-only bundle export")

    in_place = source_catalog == target_catalog
    working_catalog = (
        target_catalog.with_name(f"{target_catalog.name}.tmp")
        if in_place
        else target_catalog
    )
    if working_catalog.exists():
        working_catalog.unlink()

    source_bytes = source_catalog.stat().st_size
    with sqlite3.connect(source_catalog, timeout=300.0) as source_connection:
        with sqlite3.connect(working_catalog, timeout=300.0) as target_connection:
            source_connection.backup(target_connection)
            target_connection.commit()

    try:
        with sqlite3.connect(working_catalog, timeout=300.0) as connection:
            connection.execute("PRAGMA journal_mode=DELETE")
            connection.execute("PRAGMA synchronous=OFF")
            connection.execute("PRAGMA temp_store=MEMORY")
            connection.execute(
                "DELETE FROM search_documents_scoped WHERE collection_name <> ?",
                (normalized_collection,),
            )
            for table_name in QUERY_ONLY_UNUSED_TABLES:
                connection.execute(f"DELETE FROM {table_name}")
            connection.commit()
            connection.execute("VACUUM")
            connection.commit()
        if in_place:
            _replace_with_retry(working_catalog, target_catalog)
    except Exception:
        if working_catalog.exists() and working_catalog != target_catalog:
            try:
                working_catalog.unlink()
            except OSError:
                pass
        raise

    return {
        "source_bytes": source_bytes,
        "target_bytes": target_catalog.stat().st_size,
    }


def build_viewer_manifest(
    *,
    root: str,
    collection_name: str,
    embedding_provider: str,
    embedding_model: str,
    target_platform: str,
    app_min_version: str = VIEWER_APP_VERSION,
    data_version: Optional[str] = None,
) -> dict:
    catalog = QueryCatalog(root)
    built_at = datetime.now(timezone.utc).isoformat()
    return {
        "app_min_version": app_min_version,
        "data_version": data_version or built_at,
        "built_at": built_at,
        "collection_name": collection_name,
        "query_only": True,
        "doc_type_counts": catalog.list_collection_doc_type_counts(collection_name=collection_name),
        "eligible_items": catalog.count_eligible_items(collection_name=collection_name),
        "embedding_provider": embedding_provider,
        "embedding_model": embedding_model,
        "target_platform": target_platform,
    }


def export_viewer_bundle(
    *,
    root: str = "data",
    output_dir: str,
    qdrant_path: str,
    collection_name: str,
    embedding_provider: str,
    embedding_model: str,
    target_platform: str,
    app_min_version: str = VIEWER_APP_VERSION,
    data_version: Optional[str] = None,
    overwrite: bool = False,
) -> dict:
    config = SearchIndexConfig(
        root=root,
        qdrant_path=qdrant_path,
        collection_name=collection_name,
        embedding_provider=embedding_provider,
        embedding_model=embedding_model,
    )
    source_root = config.root_path
    source_catalog = source_root / "catalog.sqlite"
    source_alias_map = source_root / "lexicon" / "alias_map.json"
    source_qdrant = config.qdrant_root
    if not source_catalog.exists():
        raise FileNotFoundError(f"Missing catalog.sqlite at {source_catalog}")
    if not source_qdrant.exists():
        raise FileNotFoundError(f"Missing qdrant storage at {source_qdrant}")
    if not _qdrant_storage_has_collection(source_qdrant, collection_name):
        raise FileNotFoundError(
            f"Qdrant storage at {source_qdrant} does not contain collection {collection_name}"
        )
    bundle_root = Path(output_dir)
    if bundle_root.exists():
        if not overwrite:
            raise FileExistsError(f"Bundle output already exists: {bundle_root}")
        shutil.rmtree(bundle_root)
    (bundle_root / "lexicon").mkdir(parents=True, exist_ok=True)
    catalog_stats = _prune_query_only_catalog(
        source_catalog=source_catalog,
        target_catalog=bundle_root / "catalog.sqlite",
        collection_name=collection_name,
    )
    if source_alias_map.exists():
        shutil.copy2(source_alias_map, bundle_root / "lexicon" / "alias_map.json")
    shutil.copytree(source_qdrant, bundle_root / "qdrant_storage")
    manifest = build_viewer_manifest(
        root=root,
        collection_name=collection_name,
        embedding_provider=embedding_provider,
        embedding_model=embedding_model,
        target_platform=target_platform,
        app_min_version=app_min_version,
        data_version=data_version,
    )
    (bundle_root / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    ui_meta = build_search_ui_meta_snapshot(
        root=root,
        collection_name=collection_name,
        participants_limit=160,
    )
    (bundle_root / "reports").mkdir(parents=True, exist_ok=True)
    (bundle_root / "reports" / "search_ui_meta.json").write_text(
        json.dumps(ui_meta, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return {
        "bundle_root": str(bundle_root),
        "catalog_path": str(bundle_root / "catalog.sqlite"),
        "qdrant_storage_path": str(bundle_root / "qdrant_storage"),
        "manifest_path": str(bundle_root / "manifest.json"),
        "ui_meta_path": str(bundle_root / "reports" / "search_ui_meta.json"),
        "target_platform": target_platform,
        "data_version": manifest["data_version"],
        "catalog_source_bytes": catalog_stats["source_bytes"],
        "catalog_target_bytes": catalog_stats["target_bytes"],
    }
