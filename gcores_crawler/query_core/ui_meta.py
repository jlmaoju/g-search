from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .catalog import QueryCatalog


def default_search_ui_meta_path(root: str | Path) -> Path:
    root_path = Path(root)
    return root_path / "reports" / "search_ui_meta.json"


def build_search_ui_meta_snapshot(
    *,
    root: str,
    collection_name: str,
    participants_limit: Optional[int] = None,
) -> dict:
    catalog = QueryCatalog(root)
    manifest = catalog.load_manifest() or {}
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
    participants = catalog.list_radio_participants(limit=participants_limit)
    program_types = catalog.list_radio_categories(limit=None)
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "library_updated_at": catalog.resolve_library_updated_at(manifest=manifest),
        "eligible_items": eligible_items,
        "doc_type_counts": doc_type_counts,
        "participants_count": catalog.count_radio_participants(),
        "participants": participants,
        "participants_limit": participants_limit,
        "program_types_count": catalog.count_radio_categories(),
        "program_types": program_types,
        "database_size_bytes": catalog.db_path.stat().st_size if catalog.db_path.exists() else None,
    }


def write_search_ui_meta_snapshot(
    *,
    root: str,
    collection_name: str,
    output_path: Optional[str | Path] = None,
    participants_limit: Optional[int] = None,
) -> dict:
    snapshot = build_search_ui_meta_snapshot(
        root=root,
        collection_name=collection_name,
        participants_limit=participants_limit,
    )
    target_path = Path(output_path) if output_path else default_search_ui_meta_path(root)
    target_path.parent.mkdir(parents=True, exist_ok=True)
    target_path.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8")
    return {
        "output_path": str(target_path),
        "snapshot": snapshot,
    }
