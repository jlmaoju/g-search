from __future__ import annotations

import json
import re
import sys
import uuid
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence

from .catalog import Catalog
from .daily_runtime import DailyRuntimeReporter
from .lexicon import dedupe_preserve_order, extract_hotwords_from_text, load_alias_map, replace_aliases
from .query_core import runtime as query_runtime
from .search_providers import create_embedding_provider, create_glm_client

try:
    from qdrant_client import QdrantClient, models
except ImportError:  # pragma: no cover
    QdrantClient = None
    models = None


DEFAULT_COLLECTION = "gcores_memory_v1"
DEFAULT_QDRANT_PATH = "qdrant"
DEFAULT_EMBEDDING_PROVIDER = "zhipu"
DEFAULT_EMBEDDING_MODEL = "embedding-3"
DEFAULT_EMBEDDING_BATCH_SIZE = 64
DEFAULT_EMBEDDING_CONCURRENCY = 8
DEFAULT_GLM_PROVIDER = "zhipu"
DEFAULT_GLM_MODEL = "glm-5"
BASE_DOC_TYPES = ("item_title", "timeline_note", "evidence_atom")

ATOM_TARGET_MS = 25_000
ATOM_HARD_MAX_MS = 30_000
SCENE_HARD_MAX_MS = 300_000
SCENE_MIN_MS = 45_000
TIMELINE_LEAD_MS = 8_000
TIMELINE_WINDOW_MS = 12_000
WINDOW_ATOMS = 14
WINDOW_OVERLAP = 3
ATOM_LIGHT_CONTEXT_MAX_CHARS = 30


def safe_print_line(message: Any, *, flush: bool = False) -> None:
    text = str(message)
    try:
        sys.stdout.write(text + "\n")
    except UnicodeEncodeError:
        encoding = getattr(sys.stdout, "encoding", None) or "utf-8"
        if hasattr(sys.stdout, "buffer"):
            sys.stdout.buffer.write((text + "\n").encode(encoding, errors="backslashreplace"))
        else:
            fallback = (text + "\n").encode("ascii", errors="backslashreplace").decode("ascii")
            sys.stdout.write(fallback)
    if flush:
        try:
            sys.stdout.flush()
        except Exception:
            pass

TRANSITION_PHRASES = (
    "接下来",
    "另外一个",
    "另外一条",
    "再说回",
    "下面聊",
    "下一条",
    "下一条新闻",
    "最后一个",
    "然后再",
    "说回",
    "再聊聊",
)
STRUCTURAL_ANCHORS = (
    "欢迎收听",
    "欢迎回来",
    "感谢收听",
    "下期再见",
    "广告之后",
    "片头",
    "片尾",
)
REPEATED_CHAR_RE = re.compile(r"(.)\1{11,}")
GENERIC_CLUES = {"游戏", "电台", "播客", "节目", "机核", "Gadio", "Gcores"}
QUERY_SPLIT_RE = re.compile(r"[\s,，。！？、/|]+")
QUERY_CONNECTOR_SPLIT_RE = re.compile(r"[和与及跟]")
DOC_TYPE_PRIORS = {
    "item_title": 0.18,
    "episode_card": 0.16,
    "scene_summary": 0.12,
    "timeline_note": 0.06,
    "evidence_atom": 0.0,
}


@dataclass
class SearchIndexConfig:
    root: str = "data"
    qdrant_path: str = DEFAULT_QDRANT_PATH
    qdrant_url: Optional[str] = None
    qdrant_api_key: Optional[str] = None
    collection_name: str = DEFAULT_COLLECTION
    embedding_provider: str = DEFAULT_EMBEDDING_PROVIDER
    embedding_model: str = DEFAULT_EMBEDDING_MODEL
    embedding_api_key: Optional[str] = None
    embedding_dimension: Optional[int] = None
    embedding_batch_size: int = DEFAULT_EMBEDDING_BATCH_SIZE
    embedding_concurrency: int = DEFAULT_EMBEDDING_CONCURRENCY
    glm_provider: str = DEFAULT_GLM_PROVIDER
    glm_model: str = DEFAULT_GLM_MODEL
    glm_api_key: Optional[str] = None
    alias_map_path: Optional[str] = None
    summary_mode: str = "none"
    summary_fallback_allowed: bool = False
    eligible_only: bool = True
    require_transcript: bool = True

    @property
    def root_path(self) -> Path:
        return Path(self.root)

    @property
    def qdrant_root(self) -> Path:
        path = Path(self.qdrant_path)
        if not path.is_absolute():
            path = self.root_path / path
        return path


@dataclass
class SearchTuning:
    candidate_limit: int = 150
    rerank_enabled: bool = True
    diversify_enabled: bool = True
    second_pass_atom_enabled: bool = True
    second_pass_atom_limit: int = 60
    doc_type_prior_weight: float = 1.0
    keyword_overlap_weight: float = 1.0
    title_weight: float = 1.0
    timeline_title_weight: float = 1.0
    participant_weight: float = 1.0
    multi_hit_weight: float = 1.0


def build_search_index(
    root: str = "data",
    *,
    qdrant_path: str = DEFAULT_QDRANT_PATH,
    qdrant_url: Optional[str] = None,
    qdrant_api_key: Optional[str] = None,
    collection_name: str = DEFAULT_COLLECTION,
    embedding_provider: str = DEFAULT_EMBEDDING_PROVIDER,
    embedding_model: str = DEFAULT_EMBEDDING_MODEL,
    embedding_api_key: Optional[str] = None,
    embedding_dimension: Optional[int] = None,
    embedding_batch_size: int = DEFAULT_EMBEDDING_BATCH_SIZE,
    embedding_concurrency: int = DEFAULT_EMBEDDING_CONCURRENCY,
    glm_provider: str = DEFAULT_GLM_PROVIDER,
    glm_model: str = DEFAULT_GLM_MODEL,
    glm_api_key: Optional[str] = None,
    alias_map_path: Optional[str] = None,
    summary_mode: str = "none",
    summary_fallback_allowed: bool = False,
    eligible_only: bool = True,
    require_transcript: bool = True,
    item_keys: Optional[Sequence[str]] = None,
    item_ids: Optional[Sequence[str]] = None,
    limit: int = 0,
    verbose: bool = False,
) -> dict:
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
        glm_provider=glm_provider,
        glm_model=glm_model,
        glm_api_key=glm_api_key,
        alias_map_path=alias_map_path,
        summary_mode=summary_mode,
        summary_fallback_allowed=summary_fallback_allowed,
        eligible_only=eligible_only,
        require_transcript=require_transcript,
    )
    catalog = Catalog(root)
    qdrant = SearchQdrantStore(config)
    try:
        qdrant.reset_collection()
        catalog.clear_search_documents(collection_name=collection_name)
        summary = _process_search_items(
            catalog=catalog,
            qdrant=qdrant,
            config=config,
            updated_after=None,
            item_keys=item_keys,
            item_ids=item_ids,
            limit=limit or None,
            verbose=verbose,
        )
        catalog.set_state(search_watermark_key(collection_name), {"updated_at": utc_now_iso()})
        summary["mode"] = "build"
        return summary
    finally:
        qdrant.close()


def sync_search_index(
    root: str = "data",
    *,
    qdrant_path: str = DEFAULT_QDRANT_PATH,
    qdrant_url: Optional[str] = None,
    qdrant_api_key: Optional[str] = None,
    collection_name: str = DEFAULT_COLLECTION,
    embedding_provider: str = DEFAULT_EMBEDDING_PROVIDER,
    embedding_model: str = DEFAULT_EMBEDDING_MODEL,
    embedding_api_key: Optional[str] = None,
    embedding_dimension: Optional[int] = None,
    embedding_batch_size: int = DEFAULT_EMBEDDING_BATCH_SIZE,
    embedding_concurrency: int = DEFAULT_EMBEDDING_CONCURRENCY,
    glm_provider: str = DEFAULT_GLM_PROVIDER,
    glm_model: str = DEFAULT_GLM_MODEL,
    glm_api_key: Optional[str] = None,
    alias_map_path: Optional[str] = None,
    summary_mode: str = "none",
    summary_fallback_allowed: bool = False,
    eligible_only: bool = True,
    require_transcript: bool = True,
    item_keys: Optional[Sequence[str]] = None,
    item_ids: Optional[Sequence[str]] = None,
    limit: int = 0,
    verbose: bool = False,
    progress_reporter: DailyRuntimeReporter | None = None,
) -> dict:
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
        glm_provider=glm_provider,
        glm_model=glm_model,
        glm_api_key=glm_api_key,
        alias_map_path=alias_map_path,
        summary_mode=summary_mode,
        summary_fallback_allowed=summary_fallback_allowed,
        eligible_only=eligible_only,
        require_transcript=require_transcript,
    )
    catalog = Catalog(root)
    watermark = catalog.get_state(search_watermark_key(collection_name)) or {}
    updated_after = watermark.get("updated_at")
    if item_keys or item_ids:
        updated_after = None
    qdrant = SearchQdrantStore(config)
    try:
        summary = _process_search_items(
            catalog=catalog,
            qdrant=qdrant,
            config=config,
            updated_after=updated_after,
            item_keys=item_keys,
            item_ids=item_ids,
            limit=limit or None,
            verbose=verbose,
            progress_reporter=progress_reporter,
        )
        if not item_keys and not item_ids:
            successful_updates = [value for value in summary.get("successful_item_updates", []) if value]
            if successful_updates:
                catalog.set_state(search_watermark_key(collection_name), {"updated_at": max(successful_updates)})
        summary["mode"] = "sync"
        summary["updated_after"] = updated_after
        return summary
    finally:
        qdrant.close()


def run_search_query(
    query: str,
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
    embedding_batch_size: int = DEFAULT_EMBEDDING_BATCH_SIZE,
    embedding_concurrency: int = DEFAULT_EMBEDDING_CONCURRENCY,
    alias_map_path: Optional[str] = None,
    limit: int = 10,
    scope: str = "content",
    doc_types: Optional[Sequence[str]] = None,
    categories: Optional[Sequence[str]] = None,
    participants: Optional[Sequence[str]] = None,
    tuning: Optional[SearchTuning] = None,
) -> dict:
    return query_runtime.run_search_query(
        query,
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
        limit=limit,
        scope=scope,
        doc_types=doc_types,
        categories=categories,
        participants=participants,
        tuning=tuning,
    )


def run_search_query_with_runtime(
    query: str,
    *,
    catalog: Catalog,
    qdrant: "SearchQdrantStore",
    config: SearchIndexConfig,
    alias_map: Optional[dict[str, str]] = None,
    limit: int = 10,
    scope: str = "content",
    doc_types: Optional[Sequence[str]] = None,
    categories: Optional[Sequence[str]] = None,
    participants: Optional[Sequence[str]] = None,
    tuning: Optional[SearchTuning] = None,
) -> dict:
    return query_runtime.run_search_query_with_runtime(
        query,
        catalog=catalog,
        qdrant=qdrant,
        config=config,
        alias_map=alias_map,
        limit=limit,
        scope=scope,
        doc_types=doc_types,
        categories=categories,
        participants=participants,
        tuning=tuning,
    )


def resolve_search_scope_doc_types(scope: str, requested_doc_types: Optional[Sequence[str]] = None) -> List[str]:
    return query_runtime.resolve_search_scope_doc_types(scope, requested_doc_types=requested_doc_types)


def should_run_atom_second_pass(*, scope: str, tuning: SearchTuning) -> bool:
    return query_runtime.should_run_atom_second_pass(scope=scope, tuning=tuning)


def resolve_atom_second_pass_limit(*, limit: int, tuning: SearchTuning) -> int:
    return query_runtime.resolve_atom_second_pass_limit(limit=limit, tuning=tuning)


def annotate_recall_hits(hits: List[dict], *, recall_stage: str) -> List[dict]:
    return query_runtime.annotate_recall_hits(hits, recall_stage=recall_stage)


def get_episode_details(item_id: str, *, root: str = "data", collection_name: Optional[str] = None) -> dict:
    return query_runtime.get_episode_details(item_id, root=root, collection_name=collection_name)


def _process_search_items(
    *,
    catalog: Catalog,
    qdrant: "SearchQdrantStore",
    config: SearchIndexConfig,
    updated_after: Optional[str],
    item_keys: Optional[Sequence[str]],
    item_ids: Optional[Sequence[str]],
    limit: Optional[int],
    verbose: bool,
    progress_reporter: DailyRuntimeReporter | None = None,
) -> dict:
    alias_map = resolve_alias_map(config)
    glm_client = None
    if str(config.summary_mode or "none").strip().lower() != "none":
        glm_client = create_glm_client(
            provider_name=config.glm_provider,
            api_key=config.glm_api_key,
            model=config.glm_model,
        )
        if glm_client is None:
            raise RuntimeError("summary_mode requires a configured GLM client")
    items = catalog.get_radio_items_for_search(
        updated_after=updated_after,
        limit=limit,
        item_keys=item_keys,
        item_ids=item_ids,
        eligible_only=config.eligible_only,
        require_transcript=config.require_transcript,
        collection_name=config.collection_name,
    )
    processed = 0
    errors: List[dict] = []
    doc_counts: Counter[str] = Counter()
    scene_sources: Counter[str] = Counter()
    successful_item_updates: List[str] = []
    total_items = len(items)
    if progress_reporter is not None:
        progress_reporter.update(
            status="running",
            current=0,
            total=total_items or None,
            unit="items",
            message=f"items={total_items} updated_after={updated_after or '-'}",
        )
    for index, item_row in enumerate(items, start=1):
        item_key = str(item_row["item_key"])
        transcript_rows = catalog.get_item_transcripts(item_key=item_key)
        transcript_id = transcript_rows[-1]["transcript_id"] if transcript_rows else None
        job_key = f"search:{item_key}"
        catalog.start_enrichment_job(
            job_key=job_key,
            item_key=item_key,
            job_type="search_index",
            transcript_id=transcript_id,
            payload={"updated_after": updated_after},
        )
        try:
            if verbose:
                safe_print_line(
                    f"[search-index] start item={item_key} "
                    f"title={item_row.get('title') or item_row.get('item_id')}",
                    flush=True,
                )
            documents, build_summary = build_search_documents_for_item(
                catalog=catalog,
                config=config,
                item_row=item_row,
                transcript_rows=transcript_rows,
                alias_map=alias_map,
                glm_client=glm_client,
            )
            target_doc_types = None
            if str(config.summary_mode or "none").strip().lower() == "none":
                target_doc_types = list(BASE_DOC_TYPES)
            catalog.replace_search_documents_for_item(
                item_key=item_key,
                documents=documents,
                collection_name=config.collection_name,
                target_doc_types=target_doc_types,
            )
            qdrant.replace_item_documents(
                item_key=item_key,
                documents=documents,
                target_doc_types=target_doc_types,
            )
            processed += 1
            doc_counts.update(build_summary["doc_counts"])
            scene_sources.update(build_summary["scene_sources"])
            successful_item_updates.append(search_item_updated_at(item_row))
            catalog.complete_enrichment_job(job_key=job_key, payload=build_summary)
            if verbose:
                safe_print_line(
                    f"[search-index] item={item_key} docs={sum(build_summary['doc_counts'].values())}",
                    flush=True,
                )
        except Exception as exc:  # noqa: BLE001
            errors.append(
                {
                    "item_key": item_key,
                    "item_id": item_row.get("item_id"),
                    "updated_at": search_item_updated_at(item_row),
                    "error": str(exc),
                }
            )
            catalog.mark_enrichment_job_error(job_key=job_key, error=str(exc))
            if verbose:
                safe_print_line(f"[search-index] error item={item_key} error={exc}", flush=True)
        if progress_reporter is not None:
            progress_reporter.update(
                status="running",
                current=index,
                total=total_items or None,
                unit="items",
                message=f"indexed={processed} errors={len(errors)} item={item_key}",
                extra={
                    "items_indexed": processed,
                    "errors": len(errors),
                    "doc_counts": dict(doc_counts),
                },
            )
    return {
        "status": "ok",
        "items_seen": len(items),
        "items_indexed": processed,
        "errors": errors,
        "doc_counts": dict(doc_counts),
        "scene_sources": dict(scene_sources),
        "successful_item_updates": successful_item_updates,
        "catalog": catalog.stats(),
    }


def build_search_documents_for_item(
    *,
    catalog: Catalog,
    config: SearchIndexConfig,
    item_row: dict,
    transcript_rows: List[dict],
    alias_map: dict[str, str],
    glm_client=None,
) -> tuple[List[dict], dict]:
    record = load_normalized_record(config.root_path, item_row)
    documents: List[dict] = [build_item_title_document(record=record, item_row=item_row, alias_map=alias_map)]
    timeline_docs = build_timeline_documents(record=record, item_row=item_row, alias_map=alias_map)
    documents.extend(timeline_docs)
    scene_sources: Counter[str] = Counter()
    latest_transcript_row = pick_latest_transcript_row(transcript_rows)
    scene_documents: List[dict] = []
    if latest_transcript_row:
        quality = assess_transcript_quality(latest_transcript_row.get("text") or "")
        if not quality["skip"]:
            atom_documents = build_evidence_atoms(
                transcript_row=latest_transcript_row,
                record=record,
                alias_map=alias_map,
            )
            for atom in atom_documents:
                atom["payload"]["quality_flags"] = list(quality["flags"])
            documents.extend(atom_documents)
            if str(config.summary_mode or "none").strip().lower() != "none":
                scenes, source_label = build_scene_documents(
                    atoms=atom_documents,
                    record=record,
                    transcript_row=latest_transcript_row,
                    timeline_docs=timeline_docs,
                    config=config,
                    alias_map=alias_map,
                    glm_client=glm_client,
                )
                scene_documents.extend(scenes)
                documents.extend(scene_documents)
                scene_sources[source_label] += len(scenes)
                documents.append(
                    build_episode_card_document(
                        record=record,
                        item_row=item_row,
                        scene_documents=scene_documents,
                        timeline_docs=timeline_docs,
                        config=config,
                        alias_map=alias_map,
                        glm_client=glm_client,
                    )
                )
    return (
        documents,
        {
            "doc_counts": Counter(document["doc_type"] for document in documents),
            "scene_sources": scene_sources,
        },
    )


def pick_latest_transcript_row(transcript_rows: Sequence[dict]) -> Optional[dict]:
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


def join_nonempty_lines(parts: Sequence[Any]) -> str:
    return "\n".join(str(part).strip() for part in parts if str(part).strip()).strip()


def build_keyword_text(*parts: Any, alias_map: dict[str, str]) -> str:
    text = join_nonempty_lines(parts)
    if not text:
        return ""
    normalized_text, _ = replace_aliases(text, alias_map)
    return normalized_text


def build_summary_vector_text(*, title: Any, summary: Any) -> str:
    return join_nonempty_lines([title, summary])


def build_summary_keyword_text(
    *,
    item_title: Any,
    title: Any,
    summary: Any,
    clues: Any,
    entities: Any,
    aliases: Any,
    quoted_phrases: Any,
    alias_map: dict[str, str],
) -> str:
    parts: List[str] = []
    for value in [item_title, title, summary]:
        if isinstance(value, str) and value.strip():
            parts.append(value.strip())
    for collection in [clues, entities, aliases, quoted_phrases]:
        values = [str(value).strip() for value in (collection or []) if str(value).strip()]
        if values:
            parts.append(" ".join(values))
    return build_keyword_text(*parts, alias_map=alias_map)


def build_item_title_document(*, record: dict, item_row: dict, alias_map: dict[str, str]) -> dict:
    raw_title = str(record.get("title") or item_row.get("title") or "").strip()
    normalized_title, replacements = replace_aliases(raw_title, alias_map)
    return {
        "doc_id": f"{item_row['item_key']}:title:0000",
        "parent_doc_id": None,
        "doc_type": "item_title",
        "content_type": "radios",
        "item_id": str(record["id"]),
        "item_title": raw_title,
        "doc_index": 0,
        "title": raw_title,
        "start_ms": None,
        "end_ms": None,
        "timeline_ms": None,
        "text_raw": raw_title,
        "text_normalized": normalized_title,
        "vector_text": raw_title,
        "keyword_text": normalized_title,
        "text_search": normalized_title,
        "source_updated_at": item_row.get("updated_at"),
        "payload": {
            "item_key": item_row["item_key"],
            "item_id": str(record["id"]),
            "item_title": raw_title,
            "item_url": record.get("url"),
            "published_at": record.get("published_at"),
            "category": record.get("category"),
            "tags": record.get("tags", []),
            "users": [user.get("nickname") for user in record.get("users", []) if user.get("nickname")],
            "owner_type": record.get("owner_type"),
            "option_is_official": bool(record.get("option_is_official")),
            "alias_replacements": dict(replacements),
            "title": raw_title,
        },
    }


def build_timeline_documents(*, record: dict, item_row: dict, alias_map: dict[str, str]) -> List[dict]:
    documents: List[dict] = []
    item_key = item_row["item_key"]
    timeline_index = 0
    for media in record.get("media", []):
        for timeline in media.get("timelines", []):
            raw_text = "\n".join(
                part for part in [timeline.get("title"), timeline.get("content")] if part
            ).strip()
            normalized_text, _ = replace_aliases(raw_text, alias_map)
            vector_text = raw_text
            keyword_text = build_keyword_text(record.get("title"), raw_text, alias_map=alias_map)
            timeline_ms = int(float(timeline.get("at") or 0) * 1000)
            documents.append(
                {
                    "doc_id": f"{item_key}:timeline:{timeline['id']}",
                    "parent_doc_id": None,
                    "doc_type": "timeline_note",
                    "content_type": "radios",
                    "item_id": str(record["id"]),
                    "item_title": record.get("title"),
                    "doc_index": timeline_index,
                    "title": timeline.get("title"),
                    "start_ms": max(0, timeline_ms - TIMELINE_LEAD_MS),
                    "end_ms": timeline_ms,
                    "timeline_ms": timeline_ms,
                    "text_raw": raw_text,
                    "text_normalized": normalized_text,
                    "vector_text": vector_text,
                    "keyword_text": keyword_text,
                    "text_search": keyword_text,
                    "source_updated_at": item_row.get("updated_at"),
                    "payload": {
                        "item_key": item_key,
                        "item_id": str(record["id"]),
                        "item_title": record.get("title"),
                        "item_url": record.get("url"),
                        "published_at": record.get("published_at"),
                        "category": record.get("category"),
                        "tags": record.get("tags", []),
                        "users": [user.get("nickname") for user in record.get("users", []) if user.get("nickname")],
                        "timeline_id": str(timeline["id"]),
                        "timeline_title": timeline.get("title"),
                        "timeline_content": timeline.get("content"),
                        "timeline_at": timeline.get("at"),
                        "anchor_ms": max(0, timeline_ms - TIMELINE_LEAD_MS),
                        "quote_href": timeline.get("quote_href"),
                        "asset_url": timeline.get("asset_url"),
                        "title": timeline.get("title"),
                    },
                }
            )
            timeline_index += 1
    return documents


def build_evidence_atoms(
    *,
    transcript_row: dict,
    record: dict,
    alias_map: dict[str, str],
) -> List[dict]:
    segments = parse_transcript_segments(transcript_row)
    if not segments:
        return []
    item_key = transcript_row["item_key"]
    atoms: List[dict] = []
    current: List[dict] = []
    atom_index = 0
    for segment in segments:
        if current and should_split_before_segment(current=current, next_segment=segment):
            atoms.append(
                finalize_atom(
                    item_key=item_key,
                    atom_index=atom_index,
                    transcript_row=transcript_row,
                    record=record,
                    alias_map=alias_map,
                    segments=current,
                )
            )
            atom_index += 1
            current = []
        current.append(segment)
        duration = int(current[-1]["end_ms"]) - int(current[0]["start_ms"])
        if duration >= ATOM_HARD_MAX_MS:
            atoms.append(
                finalize_atom(
                    item_key=item_key,
                    atom_index=atom_index,
                    transcript_row=transcript_row,
                    record=record,
                    alias_map=alias_map,
                    segments=current,
                )
            )
            atom_index += 1
            current = []
    if current:
        atoms.append(
            finalize_atom(
                item_key=item_key,
                atom_index=atom_index,
                transcript_row=transcript_row,
                record=record,
                alias_map=alias_map,
                segments=current,
            )
        )
    attach_timeline_hints(atoms=atoms, record=record)
    return atoms


def should_split_before_segment(*, current: List[dict], next_segment: dict) -> bool:
    current_start = int(current[0]["start_ms"])
    current_end = int(current[-1]["end_ms"])
    projected_end = int(next_segment["end_ms"])
    projected_duration = projected_end - current_start
    current_duration = current_end - current_start
    if projected_duration > ATOM_TARGET_MS and current_duration >= 12_000:
        return True
    last_text = str(current[-1].get("text") or "")
    next_text = str(next_segment.get("text") or "")
    if contains_anchor_phrase(last_text) and current_duration >= 8_000:
        return True
    if contains_anchor_phrase(next_text) and current_duration >= 10_000:
        return True
    last_speaker = current[-1].get("speaker_id")
    next_speaker = next_segment.get("speaker_id")
    if last_speaker and next_speaker and last_speaker != next_speaker and current_duration >= 12_000:
        return True
    return False


def finalize_atom(
    *,
    item_key: str,
    atom_index: int,
    transcript_row: dict,
    record: dict,
    alias_map: dict[str, str],
    segments: List[dict],
) -> dict:
    start_ms = int(segments[0]["start_ms"])
    end_ms = int(segments[-1]["end_ms"])
    raw_text = " ".join(
        str(segment.get("text") or "").strip()
        for segment in segments
        if str(segment.get("text") or "").strip()
    ).strip()
    normalized_text, replacements = replace_aliases(raw_text, alias_map)
    vector_text = raw_text
    if len(normalized_text) < ATOM_LIGHT_CONTEXT_MAX_CHARS:
        vector_text = join_nonempty_lines([record.get("title"), raw_text])
    keyword_text = build_keyword_text(record.get("title"), raw_text, alias_map=alias_map)
    return {
        "doc_id": f"{item_key}:atom:{atom_index:04d}",
        "parent_doc_id": str(transcript_row["transcript_id"]),
        "doc_type": "evidence_atom",
        "content_type": "radios",
        "item_id": str(record["id"]),
        "item_title": record.get("title"),
        "doc_index": atom_index,
        "title": f"片段 {atom_index + 1}",
        "start_ms": start_ms,
        "end_ms": end_ms,
        "timeline_ms": None,
        "text_raw": raw_text,
        "text_normalized": normalized_text,
        "vector_text": vector_text,
        "keyword_text": keyword_text,
        "text_search": keyword_text,
        "source_updated_at": transcript_row.get("updated_at"),
        "payload": {
            "item_key": item_key,
            "item_id": str(record["id"]),
            "item_title": record.get("title"),
            "item_url": record.get("url"),
            "published_at": record.get("published_at"),
            "category": record.get("category"),
            "tags": record.get("tags", []),
            "users": [user.get("nickname") for user in record.get("users", []) if user.get("nickname")],
            "transcript_id": str(transcript_row["transcript_id"]),
            "segment_type": transcript_row.get("segment_type"),
            "segment_id": transcript_row.get("segment_id"),
            "language": transcript_row.get("language"),
            "model_name": transcript_row.get("model_name"),
            "segments": segments,
            "alias_replacements": dict(replacements),
            "near_timeline_ids": [],
            "near_timeline_titles": [],
            "title": f"片段 {atom_index + 1}",
        },
    }


def attach_timeline_hints(*, atoms: List[dict], record: dict) -> None:
    timeline_entries: List[dict] = []
    for media in record.get("media", []):
        for timeline in media.get("timelines", []):
            timeline_ms = int(float(timeline.get("at") or 0) * 1000)
            timeline_entries.append(
                {
                    "timeline_id": str(timeline["id"]),
                    "title": timeline.get("title"),
                    "anchor_ms": max(0, timeline_ms - TIMELINE_LEAD_MS),
                }
            )
    for atom in atoms:
        start_ms = int(atom.get("start_ms") or 0)
        end_ms = int(atom.get("end_ms") or 0)
        matches = [
            entry
            for entry in timeline_entries
            if abs(start_ms - entry["anchor_ms"]) <= TIMELINE_WINDOW_MS
            or abs(end_ms - entry["anchor_ms"]) <= TIMELINE_WINDOW_MS
        ]
        payload = atom["payload"]
        payload["near_timeline_ids"] = [match["timeline_id"] for match in matches]
        payload["near_timeline_titles"] = [match["title"] for match in matches if match.get("title")]


def build_scene_documents(
    *,
    atoms: List[dict],
    record: dict,
    transcript_row: dict,
    timeline_docs: List[dict],
    config: SearchIndexConfig,
    alias_map: dict[str, str],
    glm_client=None,
) -> tuple[List[dict], str]:
    if not atoms:
        return [], "empty"
    boundaries: Optional[List[int]] = None
    source_label = "fallback"
    if glm_client is not None:
        boundaries = suggest_scene_boundaries(atoms=atoms, glm_client=glm_client)
        if boundaries is not None:
            source_label = "glm"
    groups = groups_from_boundaries(atoms=atoms, boundaries=boundaries) if boundaries is not None else fallback_scene_groups(atoms)
    if not groups:
        groups = fallback_scene_groups(atoms)
        source_label = "fallback"
    scenes: List[dict] = []
    for scene_index, group in enumerate(groups):
        scene_payload = summarize_scene(
            atoms=group,
            record=record,
            timeline_docs=timeline_docs,
            glm_client=glm_client,
            allow_fallback=config.summary_fallback_allowed,
        )
        raw_text = "\n".join(atom["text_raw"] for atom in group if atom.get("text_raw")).strip()
        normalized_text, replacements = replace_aliases(raw_text, alias_map)
        vector_text = build_summary_vector_text(
            title=scene_payload["scene_title"],
            summary=scene_payload["summary"],
        )
        keyword_text = build_summary_keyword_text(
            item_title=record.get("title"),
            title=scene_payload["scene_title"],
            summary=scene_payload["summary"],
            clues=scene_payload["memory_clues"],
            entities=scene_payload["entities"],
            aliases=scene_payload["aliases"],
            quoted_phrases=scene_payload["quoted_phrases"],
            alias_map=alias_map,
        )
        payload = {
            "item_key": transcript_row["item_key"],
            "item_id": str(record["id"]),
            "item_title": record.get("title"),
            "item_url": record.get("url"),
            "published_at": record.get("published_at"),
            "category": record.get("category"),
            "tags": record.get("tags", []),
            "users": [user.get("nickname") for user in record.get("users", []) if user.get("nickname")],
            "transcript_id": str(transcript_row["transcript_id"]),
            "scene_title": scene_payload["scene_title"],
            "summary": scene_payload["summary"],
            "memory_clues": scene_payload["memory_clues"],
            "entities": scene_payload["entities"],
            "aliases": scene_payload["aliases"],
            "quoted_phrases": scene_payload["quoted_phrases"],
            "atom_ids": [atom["doc_id"] for atom in group],
            "atom_indexes": [atom["doc_index"] for atom in group],
            "source": source_label,
            "alias_replacements": dict(replacements),
            "title": scene_payload["scene_title"],
        }
        scenes.append(
            {
                "doc_id": f"{transcript_row['item_key']}:scene:{scene_index:04d}",
                "parent_doc_id": str(transcript_row["transcript_id"]),
                "doc_type": "scene_summary",
                "content_type": "radios",
                "item_id": str(record["id"]),
                "item_title": record.get("title"),
                "doc_index": scene_index,
                "title": scene_payload["scene_title"],
                "start_ms": group[0]["start_ms"],
                "end_ms": group[-1]["end_ms"],
                "timeline_ms": None,
                "text_raw": raw_text,
                "text_normalized": normalized_text,
                "vector_text": vector_text,
                "keyword_text": keyword_text,
                "text_search": keyword_text,
                "source_updated_at": transcript_row.get("updated_at"),
                "payload": payload,
            }
        )
    return scenes, source_label


def build_episode_card_document(
    *,
    record: dict,
    item_row: dict,
    scene_documents: List[dict],
    timeline_docs: List[dict],
    config: SearchIndexConfig,
    alias_map: dict[str, str],
    glm_client=None,
) -> dict:
    summary_payload = summarize_episode(
        record=record,
        scene_documents=scene_documents,
        timeline_docs=timeline_docs,
        glm_client=glm_client,
        allow_fallback=config.summary_fallback_allowed,
    )
    raw_text = "\n".join(
        part for part in [record.get("title"), record.get("excerpt"), record.get("desc")] if part
    ).strip()
    normalized_text, replacements = replace_aliases(raw_text, alias_map)
    vector_text = build_summary_vector_text(
        title=summary_payload["scene_title"],
        summary=summary_payload["summary"],
    )
    keyword_text = build_summary_keyword_text(
        item_title=record.get("title"),
        title=summary_payload["scene_title"],
        summary=summary_payload["summary"],
        clues=summary_payload["memory_clues"],
        entities=summary_payload["entities"],
        aliases=summary_payload["aliases"],
        quoted_phrases=summary_payload["quoted_phrases"],
        alias_map=alias_map,
    )
    return {
        "doc_id": f"{item_row['item_key']}:episode:0000",
        "parent_doc_id": None,
        "doc_type": "episode_card",
        "content_type": "radios",
        "item_id": str(record["id"]),
        "item_title": record.get("title"),
        "doc_index": 0,
        "title": summary_payload["scene_title"],
        "start_ms": None,
        "end_ms": None,
        "timeline_ms": None,
        "text_raw": raw_text,
        "text_normalized": normalized_text,
        "vector_text": vector_text,
        "keyword_text": keyword_text,
        "text_search": keyword_text,
        "source_updated_at": item_row.get("updated_at"),
        "payload": {
            "item_key": item_row["item_key"],
            "item_id": str(record["id"]),
            "item_title": record.get("title"),
            "item_url": record.get("url"),
            "published_at": record.get("published_at"),
            "category": record.get("category"),
            "tags": record.get("tags", []),
            "users": [user.get("nickname") for user in record.get("users", []) if user.get("nickname")],
            "summary": summary_payload["summary"],
            "memory_clues": summary_payload["memory_clues"],
            "entities": summary_payload["entities"],
            "aliases": summary_payload["aliases"],
            "quoted_phrases": summary_payload["quoted_phrases"],
            "scene_titles": [scene.get("title") for scene in scene_documents if scene.get("title")],
            "timeline_titles": [timeline.get("title") for timeline in timeline_docs if timeline.get("title")],
            "alias_replacements": dict(replacements),
            "title": summary_payload["scene_title"],
        },
    }


def suggest_scene_boundaries(*, atoms: List[dict], glm_client) -> Optional[List[int]]:
    votes: Counter[int] = Counter()
    windows = atom_windows(atoms)
    successful = 0
    for window in windows:
        indexes = [atom["doc_index"] for atom in window]
        try:
            parsed = glm_client.json_chat(
                system_prompt=SCENE_BOUNDARY_SYSTEM_PROMPT,
                user_prompt=build_boundary_user_prompt(window),
                temperature=0.1,
                max_tokens=1200,
            )
            boundary_indexes = parsed.get("boundaries_after_atom_indexes")
            if not isinstance(boundary_indexes, list):
                continue
            for index in boundary_indexes:
                if isinstance(index, int) and indexes[0] <= index < indexes[-1]:
                    votes[int(index)] += 1
            successful += 1
        except Exception:
            continue
    if successful == 0:
        return None
    threshold = 2 if len(windows) > 1 else 1
    accepted = sorted(index for index, count in votes.items() if count >= threshold)
    if accepted:
        return accepted
    # Overlapping windows often choose slightly different but still useful
    # boundaries on edited podcast audio. When we got valid boundary proposals but
    # no repeated votes, fall back to single-vote boundaries and let the scene
    # duration validator merge/clip them into safe groups.
    if votes:
        return sorted(votes)
    return []


def atom_windows(atoms: List[dict]) -> List[List[dict]]:
    if len(atoms) <= WINDOW_ATOMS:
        return [atoms]
    windows: List[List[dict]] = []
    step = max(1, WINDOW_ATOMS - WINDOW_OVERLAP)
    start = 0
    while start < len(atoms):
        window = atoms[start : start + WINDOW_ATOMS]
        if window:
            windows.append(window)
        if len(window) < WINDOW_ATOMS:
            break
        start += step
    return windows


def groups_from_boundaries(*, atoms: List[dict], boundaries: Optional[List[int]]) -> List[List[dict]]:
    if not boundaries:
        return []
    boundary_set = set(boundaries)
    groups: List[List[dict]] = []
    current: List[dict] = []
    for atom in atoms:
        current.append(atom)
        duration = int(current[-1]["end_ms"]) - int(current[0]["start_ms"])
        if duration > SCENE_HARD_MAX_MS and len(current) > 1:
            overflow_atom = current.pop()
            groups.append(current)
            current = [overflow_atom]
            duration = int(current[-1]["end_ms"]) - int(current[0]["start_ms"])
        if atom["doc_index"] in boundary_set and duration >= SCENE_MIN_MS:
            groups.append(current)
            current = []
        elif duration >= SCENE_HARD_MAX_MS:
            groups.append(current)
            current = []
    if current:
        groups.append(current)
    groups = normalize_scene_groups(groups)
    return groups if validate_scene_groups(atoms=atoms, groups=groups) else []


def fallback_scene_groups(atoms: List[dict]) -> List[List[dict]]:
    groups: List[List[dict]] = []
    current: List[dict] = []
    for atom in atoms:
        current.append(atom)
        duration = int(current[-1]["end_ms"]) - int(current[0]["start_ms"])
        if len(current) >= 8 or duration >= 180_000:
            groups.append(current)
            current = []
    if current:
        if groups and len(current) < 4:
            groups[-1].extend(current)
        else:
            groups.append(current)
    groups = normalize_scene_groups(groups)
    return groups if validate_scene_groups(atoms=atoms, groups=groups) else [atoms]


def normalize_scene_groups(groups: List[List[dict]]) -> List[List[dict]]:
    if not groups:
        return []
    normalized: List[List[dict]] = []
    for group in groups:
        if not group:
            continue
        duration = int(group[-1]["end_ms"]) - int(group[0]["start_ms"])
        if normalized and duration < SCENE_MIN_MS:
            previous = normalized[-1]
            merged_duration = int(group[-1]["end_ms"]) - int(previous[0]["start_ms"])
            if merged_duration <= SCENE_HARD_MAX_MS:
                previous.extend(group)
                continue
        normalized.append(list(group))
    if len(normalized) >= 2:
        last = normalized[-1]
        duration = int(last[-1]["end_ms"]) - int(last[0]["start_ms"])
        if duration < SCENE_MIN_MS:
            merged_duration = int(last[-1]["end_ms"]) - int(normalized[-2][0]["start_ms"])
            if merged_duration <= SCENE_HARD_MAX_MS:
                normalized[-2].extend(last)
                normalized.pop()
    return normalized


def validate_scene_groups(*, atoms: List[dict], groups: List[List[dict]]) -> bool:
    flattened = [atom["doc_id"] for group in groups for atom in group]
    expected = [atom["doc_id"] for atom in atoms]
    if flattened != expected:
        return False
    for index, group in enumerate(groups):
        if not group:
            return False
        duration = int(group[-1]["end_ms"]) - int(group[0]["start_ms"])
        if duration > SCENE_HARD_MAX_MS:
            return False
        if duration < SCENE_MIN_MS and index not in {0, len(groups) - 1}:
            return False
    return True


def summarize_scene(
    *,
    atoms: List[dict],
    record: dict,
    timeline_docs: List[dict],
    glm_client,
    allow_fallback: bool = True,
) -> dict:
    if glm_client is None:
        if allow_fallback:
            return fallback_scene_summary(atoms=atoms, record=record)
        raise RuntimeError("scene summary requested without an active GLM client")
    try:
        payload = glm_client.json_chat(
            system_prompt=SCENE_SUMMARY_SYSTEM_PROMPT,
            user_prompt=build_scene_summary_user_prompt(atoms=atoms, record=record, timeline_docs=timeline_docs),
            temperature=0.2,
            max_tokens=1200,
        )
        return sanitize_summary_payload(payload, fallback_title=f"片段 {atoms[0]['doc_index'] + 1}")
    except Exception:
        if allow_fallback:
            return fallback_scene_summary(atoms=atoms, record=record)
        raise


def summarize_episode(
    *,
    record: dict,
    scene_documents: List[dict],
    timeline_docs: List[dict],
    glm_client,
    allow_fallback: bool = True,
) -> dict:
    if glm_client is None:
        if allow_fallback:
            return fallback_episode_summary(record=record, scene_documents=scene_documents, timeline_docs=timeline_docs)
        raise RuntimeError("episode summary requested without an active GLM client")
    try:
        payload = glm_client.json_chat(
            system_prompt=EPISODE_SUMMARY_SYSTEM_PROMPT,
            user_prompt=build_episode_summary_user_prompt(
                record=record,
                scene_documents=scene_documents,
                timeline_docs=timeline_docs,
            ),
            temperature=0.2,
            max_tokens=1500,
        )
        return sanitize_summary_payload(payload, fallback_title=record.get("title") or "节目记忆卡")
    except Exception:
        if allow_fallback:
            return fallback_episode_summary(record=record, scene_documents=scene_documents, timeline_docs=timeline_docs)
        raise


def fallback_scene_summary(*, atoms: List[dict], record: dict) -> dict:
    raw = " ".join(atom.get("text_normalized") or atom.get("text_raw") or "" for atom in atoms).strip()
    timeline_titles = [
        title
        for atom in atoms
        for title in atom["payload"].get("near_timeline_titles", [])
        if title
    ]
    clues = dedupe_preserve_order(
        timeline_titles
        + extract_hotwords_from_text(raw)[:10]
        + list(record.get("tags") or [])
    )
    preferred_clue = next((clue for clue in clues if clue not in GENERIC_CLUES), None)
    return {
        "scene_title": preferred_clue or (raw[:18] if raw else None) or f"{record.get('title') or '节目'}片段",
        "summary": raw[:280],
        "memory_clues": clues[:10],
        "entities": clues[:8],
        "aliases": [],
        "quoted_phrases": extract_quoted_phrases(raw),
    }


def fallback_episode_summary(*, record: dict, scene_documents: List[dict], timeline_docs: List[dict]) -> dict:
    scene_titles = [scene.get("title") for scene in scene_documents if scene.get("title")]
    timeline_titles = [timeline.get("title") for timeline in timeline_docs if timeline.get("title")]
    clues = dedupe_preserve_order((record.get("tags") or []) + scene_titles[:8] + timeline_titles[:8])
    summary = "；".join(part for part in [record.get("excerpt"), record.get("desc")] if part)[:400]
    if not summary:
        summary = "；".join(scene_titles[:5])[:400]
    return {
        "scene_title": record.get("title") or "节目记忆卡",
        "summary": summary,
        "memory_clues": clues[:12],
        "entities": clues[:10],
        "aliases": [],
        "quoted_phrases": extract_quoted_phrases(summary),
    }


def sanitize_summary_payload(payload: dict, *, fallback_title: str) -> dict:
    return {
        "scene_title": str(payload.get("scene_title") or fallback_title).strip(),
        "summary": str(payload.get("summary") or "").strip(),
        "memory_clues": sanitize_text_list(payload.get("memory_clues")),
        "entities": sanitize_text_list(payload.get("entities")),
        "aliases": sanitize_text_list(payload.get("aliases")),
        "quoted_phrases": sanitize_text_list(payload.get("quoted_phrases")),
    }


def build_memory_text(*, scene_payload: dict, raw_text: str) -> str:
    parts: List[str] = []
    for key in ("scene_title", "summary"):
        value = str(scene_payload.get(key) or "").strip()
        if value:
            parts.append(value)
    for key in ("memory_clues", "entities", "aliases", "quoted_phrases"):
        values = [str(value).strip() for value in scene_payload.get(key) or [] if str(value).strip()]
        if values:
            parts.append(" ".join(values))
    if raw_text:
        parts.append(raw_text[:800])
    return "\n".join(parts).strip()


def is_low_signal_transcript_segment(text: str) -> bool:
    normalized = normalize_text(text)
    if not normalized:
        return True
    if REPEATED_CHAR_RE.search(normalized):
        return True
    if "哈哈哈哈哈哈哈哈哈哈哈哈哈哈" in normalized:
        return True
    stripped = re.sub(r"[\W_]+", "", normalized, flags=re.UNICODE)
    if not stripped and len(normalized) <= 24:
        return True
    return False


def parse_transcript_segments(transcript_row: dict) -> List[dict]:
    raw = transcript_row.get("segments_json")
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return []
    segments: List[dict] = []
    for index, segment in enumerate(parsed):
        if not isinstance(segment, dict):
            continue
        text = normalize_text(str(segment.get("text") or ""))
        if not text or is_low_signal_transcript_segment(text):
            continue
        start_ms = int(segment.get("start_ms") or segment.get("start_time") or 0)
        end_ms = int(segment.get("end_ms") or segment.get("end_time") or start_ms)
        if end_ms < start_ms:
            end_ms = start_ms
        segments.append(
            {
                "segment_index": index,
                "start_ms": start_ms,
                "end_ms": end_ms,
                "text": text,
                "speaker_id": segment.get("speaker_id"),
                "language": segment.get("lang") or segment.get("language"),
                "asr_confidence": segment.get("asr_confidence"),
            }
        )
    return segments


def assess_transcript_quality(text: str) -> dict:
    cleaned = normalize_text(text)
    flags: List[str] = []
    skip = False
    if not cleaned:
        flags.append("empty")
        skip = True
    if REPEATED_CHAR_RE.search(cleaned):
        flags.append("repeated_chars")
    if len(cleaned) < 20:
        flags.append("short")
    if "哈哈哈哈哈哈哈哈哈哈哈哈哈哈" in cleaned:
        flags.append("laughter_run")
    if "empty" in flags:
        skip = True
    return {"skip": skip, "flags": flags}


def normalize_text(text: str) -> str:
    return " ".join(text.replace("\r", " ").replace("\n", " ").split()).strip()


def load_normalized_record(root_path: Path, item_row: dict) -> dict:
    normalized_path = root_path / str(item_row["normalized_path"])
    return json.loads(normalized_path.read_text(encoding="utf-8-sig"))


def contains_anchor_phrase(text: str) -> bool:
    normalized = normalize_text(text)
    return any(phrase in normalized for phrase in TRANSITION_PHRASES + STRUCTURAL_ANCHORS)


def sanitize_text_list(values: Any) -> List[str]:
    if not isinstance(values, list):
        return []
    cleaned = []
    for value in values:
        text = normalize_text(str(value))
        if text:
            cleaned.append(text)
    return dedupe_preserve_order(cleaned)


def extract_quoted_phrases(text: str) -> List[str]:
    candidates: List[str] = []
    for piece in re.findall(r"“([^”]{2,32})”|\"([^\"]{2,32})\"", text):
        for value in piece:
            normalized = normalize_text(value)
            if normalized:
                candidates.append(normalized)
    return dedupe_preserve_order(candidates)


def extract_query_terms(query: str) -> List[str]:
    candidates = [normalize_text(query)]
    candidates.extend(part for part in QUERY_SPLIT_RE.split(query) if part)
    candidates.extend(extract_hotwords_from_text(query))
    reduced = normalize_query_for_terms(query)
    candidates.extend(part for part in QUERY_SPLIT_RE.split(reduced) if part)
    candidates.extend(part for part in QUERY_CONNECTOR_SPLIT_RE.split(reduced) if part)
    results: List[str] = []
    for candidate in candidates:
        normalized = normalize_text(candidate)
        if not normalized:
            continue
        if len(normalized) == 1:
            continue
        results.append(normalized)
        if " " not in normalized and all("\u4e00" <= char <= "\u9fff" for char in normalized) and 4 <= len(normalized) <= 12:
            for size in (2, 3, 4):
                if len(normalized) < size:
                    continue
                for index in range(0, len(normalized) - size + 1):
                    results.append(normalized[index : index + size])
    return dedupe_preserve_order(results)


def normalize_query_for_terms(query: str) -> str:
    normalized = normalize_text(query)
    for marker in ("那期节目", "那期", "节目", "那个", "这个", "做例子来说", "做例子", "来说", "举例来说", "举例", "比如说", "比如", "用"):
        normalized = normalized.replace(marker, " ")
    return normalize_text(normalized)


def resolve_alias_map(config: SearchIndexConfig) -> dict[str, str]:
    alias_map_path = config.alias_map_path
    if alias_map_path is None:
        alias_map_path = str(config.root_path / "lexicon" / "alias_map.json")
    return load_alias_map(alias_map_path)


def search_watermark_key(collection_name: str) -> str:
    return f"search_index:{collection_name}:watermark"


def parse_search_timestamp(value: Any) -> Optional[tuple[datetime, str]]:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed, text


def search_item_updated_at(item_row: dict) -> str:
    candidates = [
        parse_search_timestamp(item_row.get("updated_at")),
        parse_search_timestamp(item_row.get("transcript_updated_at")),
    ]
    parsed_values = [value for value in candidates if value is not None]
    if parsed_values:
        return max(parsed_values, key=lambda entry: entry[0])[1]
    return str(item_row.get("transcript_updated_at") or item_row.get("updated_at") or "")


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def build_boundary_user_prompt(window: List[dict]) -> str:
    atom_lines = []
    for atom in window:
        atom_lines.append(
            f"[atom_index={atom['doc_index']}] {format_ms(atom['start_ms'])}-{format_ms(atom['end_ms'])}\n"
            f"{atom['text_normalized'] or atom['text_raw']}"
        )
    return (
        "请阅读下面连续的播客原子片段，判断哪些 atom 之后应该切成新的语义场景。\n"
        "规则：只在明显话题切换、栏目切换、叙事转场处切；不要因为普通停顿切；只输出 JSON。\n\n"
        + "\n\n".join(atom_lines)
    )


def build_scene_summary_user_prompt(*, atoms: List[dict], record: dict, timeline_docs: List[dict]) -> str:
    nearby_timeline_titles = dedupe_preserve_order(
        title
        for atom in atoms
        for title in atom["payload"].get("near_timeline_titles", [])
        if title
    )
    atom_lines = []
    for atom in atoms:
        atom_lines.append(
            f"[{format_ms(atom['start_ms'])}-{format_ms(atom['end_ms'])}] {atom['text_normalized'] or atom['text_raw']}"
        )
    return (
        f"节目标题：{record.get('title')}\n"
        f"栏目：{record.get('category') or ''}\n"
        f"近邻时间轴：{' / '.join(nearby_timeline_titles[:6])}\n\n"
        "请生成适合“凭模糊回忆找出处”的结构化摘要，只输出 JSON，字段包括："
        "scene_title, summary, memory_clues, entities, aliases, quoted_phrases。\n\n"
        + "\n".join(atom_lines)
    )


def build_episode_summary_user_prompt(*, record: dict, scene_documents: List[dict], timeline_docs: List[dict]) -> str:
    scene_lines = []
    for scene in scene_documents[:24]:
        scene_lines.append(
            f"[{format_ms(scene['start_ms'])}-{format_ms(scene['end_ms'])}] {scene.get('title')}\n"
            f"{scene['payload'].get('summary', '')}"
        )
    timeline_titles = [timeline.get("title") for timeline in timeline_docs if timeline.get("title")]
    return (
        f"节目标题：{record.get('title')}\n"
        f"栏目：{record.get('category') or ''}\n"
        f"标签：{' / '.join(record.get('tags', []))}\n"
        f"参与者：{' / '.join(user.get('nickname') or '' for user in record.get('users', []))}\n"
        f"时间轴标题：{' / '.join(timeline_titles[:12])}\n\n"
        "请为整期节目生成适合搜索的记忆卡，只输出 JSON，字段包括："
        "scene_title, summary, memory_clues, entities, aliases, quoted_phrases。\n\n"
        + "\n\n".join(scene_lines)
    )



def format_ms(value: Optional[int]) -> Optional[str]:
    if value is None:
        return None
    total_seconds = max(0, int(value) // 1000)
    hours = total_seconds // 3600
    minutes = (total_seconds % 3600) // 60
    seconds = total_seconds % 60
    if hours:
        return f"{hours:02d}:{minutes:02d}:{seconds:02d}"
    return f"{minutes:02d}:{seconds:02d}"


class SearchQdrantStore:
    def __init__(self, config: SearchIndexConfig) -> None:
        if QdrantClient is None or models is None:
            raise RuntimeError("qdrant-client is not installed")
        self.config = config
        self.embedding_provider = self._create_embedding_provider()
        self._collection_checked = False
        self._collection_ready = False
        if config.qdrant_url:
            self.client = QdrantClient(
                url=config.qdrant_url,
                api_key=config.qdrant_api_key,
                timeout=300,
            )
        else:
            self.client = QdrantClient(path=str(config.qdrant_root))
        self._query_embedding_cache: dict[str, List[float]] = {}

    def _create_embedding_provider(self):
        return create_embedding_provider(
            provider_name=self.config.embedding_provider,
            api_key=self.config.embedding_api_key,
            model=self.config.embedding_model,
            dimension=self.config.embedding_dimension,
            batch_size=max(1, int(self.config.embedding_batch_size or DEFAULT_EMBEDDING_BATCH_SIZE)),
        )

    def _embedding_batch_size(self) -> int:
        configured = int(self.config.embedding_batch_size or 0)
        if configured > 0:
            return configured
        provider_batch_size = int(getattr(self.embedding_provider, "batch_size", 0) or 0)
        if provider_batch_size > 0:
            return provider_batch_size
        return DEFAULT_EMBEDDING_BATCH_SIZE

    def _embedding_concurrency(self) -> int:
        return max(1, int(self.config.embedding_concurrency or DEFAULT_EMBEDDING_CONCURRENCY))

    def _supports_parallel_embedding(self) -> bool:
        lowered = str(self.config.embedding_provider or "").strip().lower()
        return lowered not in {"qwen3", "qwen", "qwen3-local"}

    def _embed_documents_in_worker(self, texts: List[str]) -> List[List[float]]:
        provider = self._create_embedding_provider()
        return provider.embed_documents(texts)

    def _embed_documents(self, texts: List[str]) -> List[List[float]]:
        if not texts:
            return []
        batch_size = self._embedding_batch_size()
        batches = [texts[index : index + batch_size] for index in range(0, len(texts), batch_size)]
        if len(batches) == 1:
            return self.embedding_provider.embed_documents(batches[0])
        if self._embedding_concurrency() <= 1 or not self._supports_parallel_embedding():
            vectors: List[List[float]] = []
            for batch in batches:
                vectors.extend(self.embedding_provider.embed_documents(batch))
            return vectors
        max_workers = min(self._embedding_concurrency(), len(batches))
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            vector_batches = list(executor.map(self._embed_documents_in_worker, batches))
        vectors: List[List[float]] = []
        for batch_vectors in vector_batches:
            vectors.extend(batch_vectors)
        return vectors

    def reset_collection(self) -> None:
        if self._collection_exists():
            self.client.delete_collection(self.config.collection_name)
        self._collection_checked = True
        self._collection_ready = False

    def ensure_collection(self, *, vector_size: int) -> None:
        if self._collection_exists():
            return
        self.client.create_collection(
            collection_name=self.config.collection_name,
            vectors_config=models.VectorParams(size=vector_size, distance=models.Distance.COSINE),
        )
        self._collection_checked = True
        self._collection_ready = True

    def replace_item_documents(
        self,
        *,
        item_key: str,
        documents: List[dict],
        target_doc_types: Optional[Sequence[str]] = None,
    ) -> None:
        doc_types = [str(value) for value in (target_doc_types or []) if str(value)]
        if self._collection_exists():
            must_conditions: List[object] = [
                models.FieldCondition(key="item_key", match=models.MatchValue(value=item_key))
            ]
            if doc_types:
                must_conditions.append(
                    models.FieldCondition(
                        key="doc_type",
                        match=models.MatchAny(any=doc_types),
                    )
                )
            self.client.delete(
                collection_name=self.config.collection_name,
                points_selector=models.FilterSelector(
                    filter=models.Filter(must=must_conditions)
                ),
            )
        if not documents:
            return
        vectors = self._embed_documents(
            [
                str(
                    document.get("vector_text")
                    or document.get("keyword_text")
                    or document.get("text_search")
                    or document.get("text_normalized")
                    or document.get("text_raw")
                    or ""
                )
                for document in documents
            ]
        )
        if not vectors or not vectors[0]:
            raise RuntimeError("Embedding provider returned empty vectors")
        self.ensure_collection(vector_size=len(vectors[0]))
        points = []
        for document, vector in zip(documents, vectors):
            payload = dict(document.get("payload") or {})
            payload.update(
                {
                    "doc_id": document["doc_id"],
                    "doc_type": document["doc_type"],
                    "item_key": item_key,
                    "item_id": document["item_id"],
                    "item_title": document.get("item_title"),
                    "title": document.get("title"),
                    "published_at": payload.get("published_at"),
                    "category": payload.get("category"),
                    "users": payload.get("users") or [],
                    "tags": payload.get("tags") or [],
                    "start_ms": document.get("start_ms"),
                    "end_ms": document.get("end_ms"),
                    "timeline_ms": document.get("timeline_ms"),
                    "vector_text": document.get("vector_text"),
                    "keyword_text": document.get("keyword_text"),
                    "text_search": document.get("text_search"),
                    "text_raw": document.get("text_raw"),
                    "doc_index": document.get("doc_index", 0),
                }
            )
            points.append(
                models.PointStruct(
                    id=str(uuid.uuid5(uuid.NAMESPACE_URL, document["doc_id"])),
                    vector=vector,
                    payload=payload,
                )
        )
        self.client.upsert(collection_name=self.config.collection_name, points=points, wait=True)

    def count_item_documents(
        self,
        *,
        item_key: str,
        doc_types: Optional[Sequence[str]] = None,
    ) -> int:
        if not self._collection_exists():
            return 0
        must_conditions: List[object] = [
            models.FieldCondition(key="item_key", match=models.MatchValue(value=item_key))
        ]
        doc_type_values = [str(value) for value in (doc_types or []) if str(value)]
        if doc_type_values:
            must_conditions.append(
                models.FieldCondition(
                    key="doc_type",
                    match=models.MatchAny(any=doc_type_values),
                )
            )
        result = self.client.count(
            collection_name=self.config.collection_name,
            count_filter=models.Filter(must=must_conditions),
            exact=True,
        )
        return int(getattr(result, "count", 0) or 0)

    def _collection_exists(self) -> bool:
        if self._collection_checked:
            return self._collection_ready
        self._collection_ready = bool(self.client.collection_exists(self.config.collection_name))
        self._collection_checked = True
        return self._collection_ready

    def query(
        self,
        *,
        vector: List[float],
        limit: int,
        doc_types: Optional[Sequence[str]] = None,
        participants: Optional[Sequence[str]] = None,
    ) -> List[dict]:
        must_conditions: List[object] = []
        if doc_types:
            must_conditions.append(
                models.FieldCondition(
                    key="doc_type",
                    match=models.MatchAny(any=list(doc_types)),
                )
            )
        if participants:
            for value in participants:
                participant = str(value).strip()
                if not participant:
                    continue
                must_conditions.append(
                    models.FieldCondition(
                        key="users",
                        match=models.MatchValue(value=participant),
                    )
                )
        query_filter = models.Filter(must=must_conditions) if must_conditions else None
        points = self.client.query_points(
            collection_name=self.config.collection_name,
            query=vector,
            limit=limit,
            with_payload=True,
            query_filter=query_filter,
        ).points
        results = []
        for point in points:
            payload = dict(point.payload or {})
            results.append(
                {
                    "doc_id": payload.get("doc_id"),
                    "doc_type": payload.get("doc_type"),
                    "score": float(point.score),
                    "start_ms": payload.get("start_ms"),
                    "end_ms": payload.get("end_ms"),
                    "timeline_ms": payload.get("timeline_ms"),
                    "vector_text": payload.get("vector_text"),
                    "keyword_text": payload.get("keyword_text"),
                    "text_raw": payload.get("text_raw"),
                    "text_search": payload.get("text_search"),
                    "payload": payload,
                }
            )
        return results

    def embed_query(self, query: str) -> List[float]:
        cached = self._query_embedding_cache.get(query)
        if cached is not None:
            return cached
        vector = self.embedding_provider.embed_queries([query])[0]
        self._query_embedding_cache[query] = vector
        if len(self._query_embedding_cache) > 256:
            oldest_key = next(iter(self._query_embedding_cache))
            self._query_embedding_cache.pop(oldest_key, None)
        return vector

    def close(self) -> None:
        close = getattr(self.client, "close", None)
        if callable(close):
            close()


SCENE_BOUNDARY_SYSTEM_PROMPT = """
你是播客结构标注助手。你的任务不是总结全文，而是判断连续 atom 之间哪里发生了明显的语义转场。

输出 JSON：
{
  "boundaries_after_atom_indexes": [整数列表]
}

规则：
1. 只在明显话题切换、栏目切换、叙事转场处切。
2. 不要因为普通停顿、口头禅、笑声、插科打诨切段。
3. 边界索引表示“在某个 atom 之后切开”。
4. 只输出 JSON，不要解释。
""".strip()


SCENE_SUMMARY_SYSTEM_PROMPT = """
你是播客片段检索增强助手。请把这段内容总结成适合“凭模糊回忆找出处”的结构化信息。

输出 JSON：
{
  "scene_title": "简短标题",
  "summary": "50-120字摘要",
  "memory_clues": ["用户可能记住的线索"],
  "entities": ["人名、游戏名、栏目名、梗"],
  "aliases": ["简称、误称、社区黑话"],
  "quoted_phrases": ["容易被记住的原话或短语"]
}

要求：
1. memory_clues 面向回忆，不是面向百科。
2. quoted_phrases 尽量短。
3. 只输出 JSON。
""".strip()


EPISODE_SUMMARY_SYSTEM_PROMPT = """
你是播客节目记忆卡助手。请根据节目元数据、场景摘要和时间轴，为整期节目生成适合检索的记忆卡。

输出 JSON：
{
  "scene_title": "节目记忆卡标题",
  "summary": "节目整体摘要",
  "memory_clues": ["用户可能会这样记住这期节目"],
  "entities": ["高频人名、作品名、栏目名"],
  "aliases": ["简称、黑话、误称"],
  "quoted_phrases": ["代表性短语"]
}

要求：
1. 不要杜撰。
2. 优先保留社区叫法和简称。
3. 只输出 JSON。
""".strip()
