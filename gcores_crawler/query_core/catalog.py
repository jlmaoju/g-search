from __future__ import annotations

import json
import time
from collections import OrderedDict
from datetime import datetime, timezone
from pathlib import Path
from threading import RLock
from typing import Any, Iterable, List, Optional, Sequence

from ..catalog import Catalog, normalize_search_collection_name, use_legacy_search_documents


def escape_like_pattern(value: str) -> str:
    return str(value).replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def parse_payload_json(value: object) -> dict[str, Any]:
    try:
        payload = json.loads(str(value or "{}"))
    except Exception:
        payload = {}
    return payload if isinstance(payload, dict) else {}


class QueryCatalog:
    def __init__(self, root: str = "data", *, catalog: Optional[Catalog] = None) -> None:
        self.root = root
        self.root_path = Path(root)
        self._catalog = catalog or Catalog(root)
        self._cache: OrderedDict[tuple[str, ...], tuple[float, object, Any]] = OrderedDict()
        self._cache_lock = RLock()
        self._cache_capacity = 512
        self._cache_ttl_seconds = 30.0

    @staticmethod
    def _file_revision(path: Path) -> Optional[tuple[int, int]]:
        try:
            stat = path.stat()
            return stat.st_mtime_ns, stat.st_size
        except OSError:
            return None

    def _data_revision(self) -> tuple:
        return (
            self._file_revision(self.db_path),
            self._file_revision(self.db_path.with_name(self.db_path.name + "-wal")),
            self._file_revision(self.root_path / "reports" / "search_ui_meta.json"),
        )

    def _cached(self, key: tuple[str, ...], revision: object) -> Any:
        with self._cache_lock:
            entry = self._cache.get(key)
            if entry is not None:
                expires_at, cached_revision, value = entry
                if time.monotonic() < expires_at and cached_revision == revision:
                    self._cache.move_to_end(key)
                    return value
                self._cache.pop(key, None)
        return None

    def _remember(self, key: tuple[str, ...], revision: object, value: Any) -> None:
        # Missing/invalid files and absent items may appear during the next sync.
        if value is None:
            return
        with self._cache_lock:
            self._cache[key] = (time.monotonic() + self._cache_ttl_seconds, revision, value)
            self._cache.move_to_end(key)
            while len(self._cache) > self._cache_capacity:
                self._cache.popitem(last=False)

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
        revision = self._file_revision(path)
        if revision is None:
            return None
        key = ("json", cache_key)
        cached = self._cached(key, revision)
        if cached is not None:
            return cached
        try:
            payload = json.loads(path.read_text(encoding="utf-8-sig"))
        except Exception:
            payload = None
        if not isinstance(payload, dict):
            return None
        self._remember(key, revision, payload)
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
        normalized_path = self.root_path / normalized_key
        revision = self._file_revision(normalized_path)
        if revision is None:
            return None
        key = ("normalized", normalized_key)
        cached = self._cached(key, revision)
        if cached is not None:
            return cached
        try:
            payload = json.loads(normalized_path.read_text(encoding="utf-8-sig"))
        except Exception:
            payload = None
        if not isinstance(payload, dict):
            return None
        self._remember(key, revision, payload)
        return payload

    def get_item_display_record(self, *, item_key: str) -> Optional[dict[str, Any]]:
        cache_key = str(item_key or "").strip()
        if not cache_key:
            return None
        revision = self._data_revision()
        key = ("display", cache_key)
        cached = self._cached(key, revision)
        if cached is not None:
            normalized_path = str(cached.get("normalized_path") or "")
            if cached.get("_normalized_revision") == self._file_revision(self.root_path / normalized_path):
                return {k: v for k, v in cached.items() if k != "_normalized_revision"}

        item_row = self.get_item_record(item_key=cache_key)
        if item_row is None:
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
        normalized_revision = self._file_revision(self.root_path / str(item_row.get("normalized_path") or ""))
        self._remember(key, revision, {**merged, "_normalized_revision": normalized_revision})
        return merged

    def get_item_record(self, *, item_key: str) -> Optional[dict]:
        return self._catalog.get_item_record(item_key=item_key)

    def list_radio_participants(self, *, limit: Optional[int] = None) -> List[dict]:
        revision = self._data_revision()
        key = ("participants",)
        participants = self._cached(key, revision)
        if participants is None:
            participants = self._catalog.list_radio_participants(limit=None)
            self._remember(key, revision, participants)
        return participants[:limit] if limit is not None else participants

    def count_radio_participants(self) -> int:
        return len(self.list_radio_participants(limit=None))

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

    def search_documents_by_terms(
        self,
        *,
        query_terms: Sequence[str],
        collection_name: Optional[str] = None,
        doc_types: Optional[Iterable[str]] = None,
        categories: Optional[Iterable[str]] = None,
        participants: Optional[Iterable[str]] = None,
        limit: int = 200,
    ) -> List[dict]:
        if limit <= 0:
            return []
        terms: list[str] = []
        for value in query_terms:
            term = str(value or "").strip()
            if len(term) <= 1 or term in terms:
                continue
            terms.append(term)
            if len(terms) >= 24:
                break
        if not terms:
            return []

        table_name = "search_documents"
        clauses: list[str] = []
        params: list[object] = []
        if not use_legacy_search_documents(collection_name):
            table_name = "search_documents_scoped"
            clauses.append("collection_name = ?")
            params.append(normalize_search_collection_name(collection_name))

        doc_type_list = [str(value).strip() for value in (doc_types or []) if str(value).strip()]
        if doc_type_list:
            clauses.append(f"doc_type IN ({', '.join('?' for _ in doc_type_list)})")
            params.extend(doc_type_list)

        # Filter the complete candidate set before ORDER BY / LIMIT, including
        # legacy collections. Invalid payloads behave like an empty object.
        payload_expr = "CASE WHEN json_valid(payload_json) THEN payload_json ELSE '{}' END"
        category_values = sorted({str(value).strip() for value in (categories or []) if str(value).strip()})
        if category_values:
            clauses.append(f"json_extract({payload_expr}, '$.category') IN ({', '.join('?' for _ in category_values)})")
            params.extend(category_values)
        for participant in dict.fromkeys(str(value).strip() for value in (participants or []) if str(value).strip()):
            clauses.append(f"EXISTS (SELECT 1 FROM json_each({payload_expr}, '$.users') AS participant WHERE participant.value = ?)")
            params.append(participant)

        search_expr = (
            "COALESCE(title, '') || ' ' || "
            "COALESCE(item_title, '') || ' ' || "
            "COALESCE(keyword_text, '') || ' ' || "
            "COALESCE(text_search, '')"
        )
        score_params: list[object] = []
        score_parts: list[str] = []
        like_clauses: list[str] = []
        like_params: list[object] = []
        for term in terms:
            pattern = f"%{escape_like_pattern(term)}%"
            term_weight = min(12, max(2, len(term)))
            score_parts.append(f"(CASE WHEN {search_expr} LIKE ? ESCAPE '\\' THEN ? ELSE 0 END)")
            score_params.extend([pattern, term_weight])
            like_clauses.append(f"{search_expr} LIKE ? ESCAPE '\\'")
            like_params.append(pattern)
        clauses.append(f"({' OR '.join(like_clauses)})")

        sql_limit = int(limit)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        query = f"""
            SELECT *, ({' + '.join(score_parts)}) AS lexical_score
            FROM {table_name}
            {where}
            ORDER BY lexical_score DESC, doc_type ASC, item_key ASC, doc_index ASC
            LIMIT ?
        """
        with self._catalog._connect() as connection:
            rows = connection.execute(query, [*score_params, *params, *like_params, sql_limit]).fetchall()

        results: list[dict] = []
        for row in rows:
            payload = parse_payload_json(row["payload_json"])
            item = dict(row)
            item["_payload"] = payload
            results.append(item)
            if len(results) >= limit:
                break
        return results

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
