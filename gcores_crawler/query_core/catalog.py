from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, List, Optional

from ..catalog import Catalog, normalize_search_collection_name, use_legacy_search_documents


class QueryCatalog:
    def __init__(self, root: str = "data", *, catalog: Optional[Catalog] = None) -> None:
        self.root = root
        self.root_path = Path(root)
        self._catalog = catalog or Catalog(root)
        self._normalized_cache: dict[str, Optional[dict[str, Any]]] = {}
        self._item_display_cache: dict[str, Optional[dict[str, Any]]] = {}
        self._json_cache: dict[str, tuple[int, Optional[dict[str, Any]]]] = {}

    @property
    def raw(self) -> Catalog:
        return self._catalog

    @property
    def db_path(self) -> Path:
        return self._catalog.db_path

    def load_manifest(self) -> Optional[dict]:
        manifest_path = self.root_path / "manifest.json"
        return self._load_cached_json(manifest_path, cache_key="manifest")

    def load_search_ui_meta_snapshot(self) -> Optional[dict]:
        snapshot_path = self.root_path / "reports" / "search_ui_meta.json"
        return self._load_cached_json(snapshot_path, cache_key="search_ui_meta")

    def _load_cached_json(self, path: Path, *, cache_key: str) -> Optional[dict[str, Any]]:
        if not path.exists():
            self._json_cache.pop(cache_key, None)
            return None
        try:
            stat = path.stat()
        except OSError:
            return None
        cached = self._json_cache.get(cache_key)
        if cached and cached[0] == stat.st_mtime_ns:
            return cached[1]
        try:
            payload = json.loads(path.read_text(encoding="utf-8-sig"))
        except Exception:
            payload = None
        self._json_cache[cache_key] = (stat.st_mtime_ns, payload)
        return payload

    def resolve_library_updated_at(self, manifest: Optional[dict] = None) -> Optional[str]:
        manifest_payload = manifest if manifest is not None else self.load_manifest()
        if manifest_payload:
            for key in ("library_updated_at", "dataset_published_at", "built_at"):
                value = str(manifest_payload.get(key) or "").strip()
                if value:
                    return value

        for relative_path in (
            "reports/search_release_meta.json",
            "reports/daily_incremental_summary.json",
            "catalog.sqlite",
        ):
            candidate = self.root_path / relative_path
            if not candidate.exists():
                continue
            if candidate.suffix.lower() == ".json":
                try:
                    payload = json.loads(candidate.read_text(encoding="utf-8-sig"))
                except Exception:
                    payload = None
                if isinstance(payload, dict):
                    for key in ("library_updated_at", "dataset_published_at", "built_at"):
                        value = str(payload.get(key) or "").strip()
                        if value:
                            return value
            return datetime.fromtimestamp(candidate.stat().st_mtime, tz=timezone.utc).isoformat(timespec="seconds")
        return None

    def load_normalized_record(self, *, normalized_relpath: str) -> Optional[dict[str, Any]]:
        normalized_key = str(normalized_relpath or "").strip()
        if not normalized_key:
            return None
        if normalized_key in self._normalized_cache:
            return self._normalized_cache[normalized_key]
        normalized_path = self.root_path / normalized_key
        if not normalized_path.exists():
            self._normalized_cache[normalized_key] = None
            return None
        try:
            payload = json.loads(normalized_path.read_text(encoding="utf-8-sig"))
        except Exception:
            payload = None
        self._normalized_cache[normalized_key] = payload
        return payload

    def get_item_display_record(self, *, item_key: str) -> Optional[dict[str, Any]]:
        cache_key = str(item_key or "").strip()
        if not cache_key:
            return None
        if cache_key in self._item_display_cache:
            return self._item_display_cache[cache_key]

        item_row = self.get_item_record(item_key=cache_key)
        if item_row is None:
            self._item_display_cache[cache_key] = None
            return None

        tags: list[str]
        users: list[str]
        try:
            tags = [str(value) for value in json.loads(item_row.get("tags_json") or "[]") if str(value)]
        except Exception:
            tags = []
        try:
            users = [str(value) for value in json.loads(item_row.get("users_json") or "[]") if str(value)]
        except Exception:
            users = []

        normalized = self.load_normalized_record(normalized_relpath=str(item_row.get("normalized_path") or ""))
        merged: dict[str, Any] = {
            **item_row,
            "tags": tags,
            "users": users,
            "cover_url": None,
            "thumb_url": None,
            "excerpt": None,
            "desc": None,
            "duration": None,
            "albums": [],
            "djs": [],
            "content_text": None,
        }
        if normalized:
            merged.update(
                {
                    "title": normalized.get("title") or merged.get("title"),
                    "url": normalized.get("url") or merged.get("url"),
                    "category": normalized.get("category") or merged.get("category"),
                    "published_at": normalized.get("published_at") or merged.get("published_at"),
                    "cover_url": normalized.get("cover_url"),
                    "thumb_url": normalized.get("thumb_url"),
                    "excerpt": normalized.get("excerpt"),
                    "desc": normalized.get("desc"),
                    "duration": normalized.get("duration"),
                    "albums": normalized.get("albums") or [],
                    "djs": normalized.get("djs") or [],
                    "content_text": normalized.get("content_text"),
                }
            )
        self._item_display_cache[cache_key] = merged
        return merged

    def get_item_record(self, *, item_key: str) -> Optional[dict]:
        return self._catalog.get_item_record(item_key=item_key)

    def list_radio_participants(self, *, limit: Optional[int] = None) -> List[dict]:
        return self._catalog.list_radio_participants(limit=limit)

    def count_radio_participants(self) -> int:
        return len(self._catalog.list_radio_participants(limit=None))

    def list_radio_categories(self, *, limit: Optional[int] = None) -> List[dict]:
        return self._catalog.list_radio_categories(limit=limit)

    def count_radio_categories(self) -> int:
        return len(self._catalog.list_radio_categories(limit=None))

    def get_search_documents_for_item(
        self,
        *,
        item_key: Optional[str] = None,
        item_id: Optional[str] = None,
        doc_types: Optional[Iterable[str]] = None,
        collection_name: Optional[str] = None,
    ) -> List[dict]:
        return self._catalog.get_search_documents_for_item(
            item_key=item_key,
            item_id=item_id,
            doc_types=doc_types,
            collection_name=collection_name,
        )

    def get_search_documents_by_doc_ids(
        self,
        *,
        doc_ids: Iterable[str],
        collection_name: Optional[str] = None,
    ) -> List[dict]:
        return self._catalog.get_search_documents_by_doc_ids(
            doc_ids=doc_ids,
            collection_name=collection_name,
        )

    def get_evidence_atoms_by_item_key_and_doc_index_range(
        self,
        *,
        item_key: str,
        start_index: int,
        end_index: int,
        collection_name: Optional[str] = None,
    ) -> List[dict]:
        return self._catalog.get_evidence_atoms_by_item_key_and_doc_index_range(
            item_key=item_key,
            start_index=start_index,
            end_index=end_index,
            collection_name=collection_name,
        )

    def get_evidence_atoms_by_item_key_and_time_range(
        self,
        *,
        item_key: str,
        start_ms: int,
        end_ms: int,
        collection_name: Optional[str] = None,
    ) -> List[dict]:
        return self._catalog.get_evidence_atoms_by_item_key_and_time_range(
            item_key=item_key,
            start_ms=start_ms,
            end_ms=end_ms,
            collection_name=collection_name,
        )

    def list_collection_doc_type_counts(self, *, collection_name: Optional[str]) -> dict[str, int]:
        if use_legacy_search_documents(collection_name):
            query = """
                SELECT doc_type, COUNT(*) AS c
                FROM search_documents
                GROUP BY doc_type
            """
            params: list[object] = []
        else:
            query = """
                SELECT doc_type, COUNT(*) AS c
                FROM search_documents_scoped
                WHERE collection_name = ?
                GROUP BY doc_type
            """
            params = [normalize_search_collection_name(collection_name)]
        with self._catalog._connect() as connection:
            rows = connection.execute(query, params).fetchall()
        return {str(row["doc_type"]): int(row["c"]) for row in rows}

    def count_eligible_items(self, *, collection_name: Optional[str]) -> int:
        rows = self._catalog.get_radio_items_for_search(
            updated_after=None,
            limit=None,
            item_keys=None,
            item_ids=None,
            eligible_only=True,
            require_transcript=True,
            collection_name=collection_name,
        )
        return len(rows)
