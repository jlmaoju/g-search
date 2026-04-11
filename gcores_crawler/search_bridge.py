from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, List, Optional, Sequence

from .catalog import Catalog
from .daily_runtime import DailyRuntimeReporter
from .search_pipeline import (
    DEFAULT_COLLECTION,
    DEFAULT_EMBEDDING_MODEL,
    DEFAULT_EMBEDDING_PROVIDER,
    DEFAULT_QDRANT_PATH,
    SearchIndexConfig,
    SearchQdrantStore,
    assess_transcript_quality,
    build_evidence_atoms,
    build_item_title_document,
    build_summary_keyword_text,
    build_summary_vector_text,
    build_timeline_documents,
    load_normalized_record,
    resolve_alias_map,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_RESULTS_DIR = "data/extraction_results"
FULL_REPLACED_DOC_TYPES = {"item_title", "timeline_note", "evidence_atom", "scene_summary", "episode_card"}
SUMMARY_DOC_TYPES = {"scene_summary", "episode_card"}


@dataclass
class SearchBridgeConfig:
    root: str = "data"
    results_dir: str = DEFAULT_RESULTS_DIR
    qdrant_path: str = DEFAULT_QDRANT_PATH
    qdrant_url: Optional[str] = None
    qdrant_api_key: Optional[str] = None
    collection_name: str = DEFAULT_COLLECTION
    embedding_provider: str = DEFAULT_EMBEDDING_PROVIDER
    embedding_model: str = DEFAULT_EMBEDDING_MODEL
    embedding_api_key: Optional[str] = None
    embedding_dimension: Optional[int] = None
    embedding_batch_size: int = 64
    embedding_concurrency: int = 8
    alias_map_path: Optional[str] = None
    source_label: str = "external-extraction"
    bridge_mode: str = "summaries-only"
    dry_run: bool = False
    verbose: bool = False

    @property
    def results_root(self) -> Path:
        path = Path(self.results_dir)
        if not path.is_absolute():
            path = PROJECT_ROOT / path
        return path


def bridge_search_results(
    *,
    root: str = "data",
    results_dir: str = DEFAULT_RESULTS_DIR,
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
    bridge_mode: str = "summaries-only",
    item_keys: Optional[Sequence[str]] = None,
    limit: int = 0,
    dry_run: bool = False,
    verbose: bool = False,
    progress_reporter: DailyRuntimeReporter | None = None,
) -> dict[str, Any]:
    config = SearchBridgeConfig(
        root=root,
        results_dir=results_dir,
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
        bridge_mode=bridge_mode,
        dry_run=dry_run,
        verbose=verbose,
    )
    catalog = Catalog(root)
    alias_map = resolve_alias_map(
        SearchIndexConfig(
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
            glm_provider="none",
        )
    )
    qdrant = None
    if not dry_run:
        qdrant = SearchQdrantStore(
            SearchIndexConfig(
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
                glm_provider="none",
            )
        )
    collection_ready = bool(
        qdrant is not None and qdrant.client.collection_exists(config.collection_name)
    )

    requested_item_keys = {value.strip() for value in (item_keys or []) if value and value.strip()}
    files = sorted(
        config.results_root.glob("radios__*.json"),
        key=lambda path: (path.stat().st_mtime_ns, path.name),
    )

    bridged: List[dict[str, Any]] = []
    skipped: List[dict[str, Any]] = []
    errors: List[dict[str, Any]] = []
    target_total = len(requested_item_keys) if requested_item_keys else min(len(files), int(limit)) if limit > 0 else len(files)
    processed = 0

    try:
        for path in files:
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
                item = dict(payload.get("item") or {})
                item_key = str(item.get("item_key") or "").strip()
                if not item_key:
                    skipped.append({"path": str(path), "reason": "missing item_key"})
                    processed += 1
                    if progress_reporter is not None:
                        progress_reporter.update(
                            status="running",
                            current=processed,
                            total=target_total or None,
                            unit="items",
                            message=f"bridged={len(bridged)} skipped={len(skipped)} errors={len(errors)}",
                        )
                    continue
                if requested_item_keys and item_key not in requested_item_keys:
                    continue

                item_row = catalog.get_item_record(item_key=item_key)
                if not item_row:
                    skipped.append({"path": str(path), "item_key": item_key, "reason": "missing item record"})
                    processed += 1
                    if progress_reporter is not None:
                        progress_reporter.update(
                            status="running",
                            current=processed,
                            total=target_total or None,
                            unit="items",
                            message=f"bridged={len(bridged)} skipped={len(skipped)} errors={len(errors)}",
                        )
                    continue

                target_doc_types = sorted(SUMMARY_DOC_TYPES)
                if config.bridge_mode == "full":
                    target_doc_types = sorted(FULL_REPLACED_DOC_TYPES)
                existing_rows = catalog.get_search_documents_for_item(
                    item_key=item_key,
                    collection_name=config.collection_name,
                )
                signature = result_signature(path)
                if collection_ready and should_skip_bridge(
                    existing_rows=existing_rows,
                    signature=signature,
                    bridge_mode=config.bridge_mode,
                ):
                    expected_doc_count = sum(
                        1
                        for row in existing_rows
                        if str(row.get("doc_type") or "") in set(target_doc_types)
                    )
                    qdrant_doc_count = (
                        qdrant.count_item_documents(item_key=item_key, doc_types=target_doc_types)
                        if qdrant is not None
                        else expected_doc_count
                    )
                    if qdrant_doc_count >= expected_doc_count:
                        skipped.append({"path": str(path), "item_key": item_key, "reason": "up-to-date"})
                        processed += 1
                        if progress_reporter is not None:
                            progress_reporter.update(
                                status="running",
                                current=processed,
                                total=target_total or None,
                                unit="items",
                                message=f"bridged={len(bridged)} skipped={len(skipped)} errors={len(errors)}",
                            )
                        continue

                if config.bridge_mode == "full":
                    documents = build_full_documents_for_result(
                        catalog=catalog,
                        item_row=item_row,
                        result_payload=payload,
                        source_path=path,
                        signature=signature,
                        alias_map=alias_map,
                        source_label=config.source_label,
                    )
                else:
                    documents = build_replacement_documents(
                        result_payload=payload,
                        item_row=item_row,
                        source_path=path,
                        signature=signature,
                        source_label=config.source_label,
                        alias_map=alias_map,
                    )
                if not documents:
                    skipped.append({"path": str(path), "item_key": item_key, "reason": "no documents"})
                    processed += 1
                    if progress_reporter is not None:
                        progress_reporter.update(
                            status="running",
                            current=processed,
                            total=target_total or None,
                            unit="items",
                            message=f"bridged={len(bridged)} skipped={len(skipped)} errors={len(errors)}",
                        )
                    continue

                if verbose:
                    print(
                        f"[bridge-search] item={item_key} docs={len(documents)} "
                        f"file={path.name}",
                        flush=True,
                    )

                if not dry_run:
                    catalog.replace_search_documents_for_item(
                        item_key=item_key,
                        documents=documents,
                        collection_name=config.collection_name,
                        target_doc_types=target_doc_types,
                    )
                    if qdrant is not None:
                        qdrant.replace_item_documents(
                            item_key=item_key,
                            documents=documents,
                            target_doc_types=target_doc_types,
                        )
                        collection_ready = True

                bridged.append(
                    {
                        "item_key": item_key,
                        "item_id": item_row.get("item_id"),
                        "title": item_row.get("title"),
                        "path": str(path),
                        "signature": signature,
                        "bridge_mode": config.bridge_mode,
                        "doc_types": summarize_doc_types(documents),
                        "documents": len(documents),
                    }
                )
                processed += 1
                if progress_reporter is not None:
                    progress_reporter.update(
                        status="running",
                        current=processed,
                        total=target_total or None,
                        unit="items",
                        message=f"bridged={len(bridged)} skipped={len(skipped)} errors={len(errors)} item={item_key}",
                        extra={
                            "bridged": len(bridged),
                            "skipped": len(skipped),
                            "errors": len(errors),
                        },
                    )
                if limit > 0 and len(bridged) >= limit:
                    break
            except Exception as exc:  # noqa: BLE001
                errors.append({"path": str(path), "error": str(exc)})
                processed += 1
                if progress_reporter is not None:
                    progress_reporter.update(
                        status="running",
                        current=processed,
                        total=target_total or None,
                        unit="items",
                        message=f"bridged={len(bridged)} skipped={len(skipped)} errors={len(errors)}",
                    )
    finally:
        if qdrant is not None:
            qdrant.close()

    return {
        "status": "ok",
        "results_dir": str(config.results_root),
        "seen": len(files),
        "bridged_count": len(bridged),
        "skipped_count": len(skipped),
        "error_count": len(errors),
        "bridged": bridged,
        "skipped": skipped,
        "errors": errors,
        "bridge_mode": config.bridge_mode,
        "dry_run": dry_run,
        "catalog": catalog.stats(),
    }


def build_full_documents_for_result(
    *,
    catalog: Catalog,
    item_row: dict[str, Any],
    result_payload: dict[str, Any],
    source_path: Path,
    signature: str,
    alias_map: dict[str, str],
    source_label: str,
) -> list[dict[str, Any]]:
    record = load_normalized_record(Path(catalog.root), item_row)
    item_title_doc = build_item_title_document(record=record, item_row=item_row, alias_map=alias_map)
    timeline_docs = build_timeline_documents(record=record, item_row=item_row, alias_map=alias_map)
    transcript_rows = catalog.get_item_transcripts(item_key=str(item_row["item_key"]))
    atom_docs: list[dict[str, Any]] = []
    latest_transcript_row = pick_latest_transcript_row(transcript_rows)
    if latest_transcript_row:
        quality = assess_transcript_quality(latest_transcript_row.get("text") or "")
        if not quality["skip"]:
            atom_docs = build_evidence_atoms(
                transcript_row=latest_transcript_row,
                record=record,
                alias_map=alias_map,
            )
            for atom in atom_docs:
                atom["payload"]["quality_flags"] = list(quality["flags"])

    replacement_docs = build_replacement_documents(
        result_payload=result_payload,
        item_row=item_row,
        source_path=source_path,
        signature=signature,
        source_label=source_label,
        alias_map=alias_map,
    )
    documents = [item_title_doc] + timeline_docs + atom_docs + replacement_docs
    documents.sort(key=document_sort_key)
    return documents


def pick_latest_transcript_row(transcript_rows: list[dict[str, Any]]) -> Optional[dict[str, Any]]:
    if not transcript_rows:
        return None
    return sorted(
        transcript_rows,
        key=lambda row: (
            str(row.get("transcribed_at") or ""),
            str(row.get("updated_at") or ""),
            str(row.get("transcript_id") or ""),
        ),
    )[-1]


def build_replacement_documents(
    *,
    result_payload: dict[str, Any],
    item_row: dict[str, Any],
    source_path: Path,
    signature: str,
    source_label: str,
    alias_map: dict[str, str],
) -> list[dict[str, Any]]:
    item = dict(result_payload.get("item") or {})
    payload_defaults = build_item_defaults(item_row=item_row, item=item)
    source_updated_at = item_row.get("updated_at")
    documents: list[dict[str, Any]] = []

    for index, scene in enumerate(result_payload.get("scene_documents") or []):
        scene_payload = dict(scene.get("payload") or {})
        merged_payload = {
            **payload_defaults,
            **scene_payload,
            "source": source_label,
            "bridge_source_path": str(source_path),
            "bridge_result_signature": signature,
        }
        raw_text = build_raw_text(
            scene.get("title"),
            scene_payload.get("summary"),
            scene_payload.get("memory_clues"),
            scene_payload.get("quoted_phrases"),
        )
        keyword_text = build_summary_keyword_text(
            item_title=payload_defaults["item_title"],
            title=scene.get("title") or scene_payload.get("scene_title"),
            summary=scene_payload.get("summary"),
            clues=scene_payload.get("memory_clues"),
            entities=scene_payload.get("entities"),
            aliases=scene_payload.get("aliases"),
            quoted_phrases=scene_payload.get("quoted_phrases"),
            alias_map=alias_map,
        )
        vector_text = build_summary_vector_text(
            title=scene.get("title") or scene_payload.get("scene_title"),
            summary=scene_payload.get("summary"),
        )
        documents.append(
            {
                "doc_id": scene.get("doc_id"),
                "parent_doc_id": None,
                "doc_type": "scene_summary",
                "content_type": "radios",
                "item_id": payload_defaults["item_id"],
                "item_title": payload_defaults["item_title"],
                "doc_index": int(scene_payload.get("scene_index", index)),
                "title": scene.get("title") or scene_payload.get("scene_title"),
                "start_ms": scene.get("start_ms"),
                "end_ms": scene.get("end_ms"),
                "timeline_ms": None,
                "text_raw": raw_text,
                "text_normalized": raw_text,
                "vector_text": vector_text,
                "keyword_text": keyword_text,
                "text_search": keyword_text,
                "source_updated_at": source_updated_at,
                "payload": merged_payload,
            }
        )

    episode = dict(result_payload.get("episode_document") or {})
    if episode:
        episode_payload = dict(episode.get("payload") or {})
        merged_payload = {
            **payload_defaults,
            **episode_payload,
            "source": source_label,
            "bridge_source_path": str(source_path),
            "bridge_result_signature": signature,
        }
        raw_text = build_raw_text(
            episode.get("title") or episode_payload.get("scene_title"),
            episode_payload.get("summary"),
            episode_payload.get("memory_clues"),
            episode_payload.get("quoted_phrases"),
        )
        keyword_text = build_summary_keyword_text(
            item_title=payload_defaults["item_title"],
            title=episode.get("title") or episode_payload.get("scene_title"),
            summary=episode_payload.get("summary"),
            clues=episode_payload.get("memory_clues"),
            entities=episode_payload.get("entities"),
            aliases=episode_payload.get("aliases"),
            quoted_phrases=episode_payload.get("quoted_phrases"),
            alias_map=alias_map,
        )
        vector_text = build_summary_vector_text(
            title=episode.get("title") or episode_payload.get("scene_title"),
            summary=episode_payload.get("summary"),
        )
        documents.append(
            {
                "doc_id": episode.get("doc_id"),
                "parent_doc_id": None,
                "doc_type": "episode_card",
                "content_type": "radios",
                "item_id": payload_defaults["item_id"],
                "item_title": payload_defaults["item_title"],
                "doc_index": 0,
                "title": episode.get("title") or episode_payload.get("scene_title"),
                "start_ms": None,
                "end_ms": None,
                "timeline_ms": None,
                "text_raw": raw_text,
                "text_normalized": raw_text,
                "vector_text": vector_text,
                "keyword_text": keyword_text,
                "text_search": keyword_text,
                "source_updated_at": source_updated_at,
                "payload": merged_payload,
            }
        )
    return documents


def build_item_defaults(*, item_row: dict[str, Any], item: dict[str, Any]) -> dict[str, Any]:
    return {
        "item_key": item.get("item_key") or item_row.get("item_key"),
        "item_id": str(item.get("item_id") or item_row.get("item_id") or ""),
        "item_title": item.get("item_title") or item.get("title") or item_row.get("title"),
        "item_url": item.get("item_url") or item.get("url") or item_row.get("url"),
        "published_at": item.get("published_at") or item_row.get("published_at"),
        "category": item.get("category") or item_row.get("category"),
        "tags": parse_json_list(item_row.get("tags_json"), fallback=item.get("tags")),
        "users": parse_json_list(item_row.get("users_json"), fallback=item.get("users")),
        "owner_type": item.get("owner_type") or item_row.get("owner_type"),
        "option_is_official": item.get("option_is_official")
        if item.get("option_is_official") is not None
        else item_row.get("option_is_official"),
        "transcript_id": item.get("transcript_id"),
        "transcript_model": item.get("transcript_model"),
    }


def parse_json_list(value: Any, *, fallback: Any = None) -> list[Any]:
    if isinstance(value, list):
        return value
    if value is None or value == "":
        if isinstance(fallback, list):
            return fallback
        return []
    try:
        parsed = json.loads(value)
    except Exception:
        return fallback if isinstance(fallback, list) else []
    return parsed if isinstance(parsed, list) else (fallback if isinstance(fallback, list) else [])


def build_raw_text(title: Any, summary: Any, clues: Any, quotes: Any) -> str:
    parts: list[str] = []
    for value in [title, summary]:
        if isinstance(value, str) and value.strip():
            parts.append(value.strip())
    clues_list = [str(value).strip() for value in (clues or []) if str(value).strip()]
    quotes_list = [str(value).strip() for value in (quotes or []) if str(value).strip()]
    if clues_list:
        parts.append("记忆线索: " + "；".join(clues_list))
    if quotes_list:
        parts.append("记忆原句: " + "；".join(quotes_list))
    return "\n".join(parts).strip()


def result_signature(path: Path) -> str:
    stat = path.stat()
    return f"{stat.st_mtime_ns}:{stat.st_size}"


def should_skip_bridge(*, existing_rows: list[dict[str, Any]], signature: str, bridge_mode: str) -> bool:
    if not existing_rows:
        return False
    doc_type_counts = summarize_existing_doc_types(existing_rows)
    required = SUMMARY_DOC_TYPES if bridge_mode != "full" else FULL_REPLACED_DOC_TYPES
    for doc_type in required:
        if doc_type_counts.get(doc_type, 0) <= 0:
            return False
    seen_summary_types = {"scene_summary": False, "episode_card": False}
    for row in existing_rows:
        if row.get("doc_type") not in {"scene_summary", "episode_card"}:
            continue
        payload = parse_payload(row.get("payload_json"))
        doc_type = str(row.get("doc_type") or "")
        if payload.get("source") not in {"coding-max", "external-extraction"}:
            return False
        if payload.get("bridge_result_signature") != signature:
            return False
        if doc_type in seen_summary_types:
            seen_summary_types[doc_type] = True
    return all(seen_summary_types.values())


def parse_payload(raw_payload: Any) -> dict[str, Any]:
    if isinstance(raw_payload, dict):
        return raw_payload
    if not raw_payload:
        return {}
    try:
        payload = json.loads(str(raw_payload))
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def summarize_existing_doc_types(rows: Iterable[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in rows:
        doc_type = str(row.get("doc_type") or "")
        counts[doc_type] = counts.get(doc_type, 0) + 1
    return counts


def summarize_doc_types(documents: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for document in documents:
        doc_type = str(document.get("doc_type") or "")
        counts[doc_type] = counts.get(doc_type, 0) + 1
    return counts


def document_sort_key(document: dict[str, Any]) -> tuple[int, int, str]:
    doc_type_order = {
        "item_title": 0,
        "timeline_note": 1,
        "evidence_atom": 2,
        "scene_summary": 3,
        "episode_card": 4,
    }
    return (
        doc_type_order.get(str(document.get("doc_type")), 99),
        int(document.get("doc_index") or 0),
        str(document.get("doc_id") or ""),
    )
