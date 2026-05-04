from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from typing import Any, List, Optional, Sequence

from ..lexicon import dedupe_preserve_order, extract_hotwords_from_text, load_alias_map, replace_aliases
from .catalog import QueryCatalog
from .config import (
    DEFAULT_COLLECTION,
    DEFAULT_EMBEDDING_BATCH_SIZE,
    DEFAULT_EMBEDDING_CONCURRENCY,
    DEFAULT_EMBEDDING_MODEL,
    DEFAULT_EMBEDDING_PROVIDER,
    DEFAULT_QDRANT_PATH,
    SearchIndexConfig,
    SearchTuning,
)
from .store import ReadOnlySearchQdrantStore


QUERY_SPLIT_RE = re.compile(r"[\s,，。！？?|]+")
QUERY_CONNECTOR_SPLIT_RE = re.compile(r"[和与及跟]")
DOC_TYPE_PRIORS = {
    "item_title": 0.18,
    "episode_card": 0.16,
    "scene_summary": 0.12,
    "timeline_note": 0.06,
    "evidence_atom": 0.0,
}
TIMELINE_WINDOW_MS = 12_000


def normalize_text(text: str) -> str:
    return " ".join(str(text or "").replace("\r", " ").replace("\n", " ").split()).strip()


def resolve_alias_map(config: SearchIndexConfig) -> dict[str, str]:
    alias_map_path = config.alias_map_path
    if alias_map_path is None:
        alias_map_path = str(config.root_path / "lexicon" / "alias_map.json")
    return load_alias_map(alias_map_path)


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
        if not normalized or len(normalized) == 1:
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


def resolve_search_scope_doc_types(scope: str, requested_doc_types: Optional[Sequence[str]] = None) -> List[str]:
    lowered = (scope or "content").strip().lower()
    if lowered == "timeline":
        allowed = ["timeline_note"]
    elif lowered == "title":
        allowed = ["item_title"]
    else:
        allowed = ["scene_summary", "episode_card"]
    requested = [str(value).strip() for value in (requested_doc_types or []) if str(value).strip()]
    if not requested:
        return allowed
    filtered = [doc_type for doc_type in allowed if doc_type in requested]
    return filtered or allowed


def should_run_atom_second_pass(*, scope: str, tuning: SearchTuning) -> bool:
    lowered = (scope or "content").strip().lower()
    return lowered == "content" and bool(tuning.second_pass_atom_enabled)


def resolve_atom_second_pass_limit(*, limit: int, tuning: SearchTuning) -> int:
    configured = max(0, int(tuning.second_pass_atom_limit))
    if configured > 0:
        return configured
    return max(24, limit * 6)


def annotate_recall_hits(hits: List[dict], *, recall_stage: str) -> List[dict]:
    annotated: List[dict] = []
    for index, hit in enumerate(hits):
        annotated.append(
            {
                **hit,
                "recall_stage": recall_stage,
                "recall_rank": index,
            }
        )
    return annotated


def build_lexical_hits_from_catalog_rows(rows: Sequence[dict]) -> List[dict]:
    if not rows:
        return []
    max_lexical_score = max(float(row.get("lexical_score") or 0.0) for row in rows) or 1.0
    hits: List[dict] = []
    for row in rows:
        payload = dict(row.get("_payload") or {})
        payload.setdefault("doc_id", row.get("doc_id"))
        payload.setdefault("doc_type", row.get("doc_type"))
        payload.setdefault("item_key", row.get("item_key"))
        payload.setdefault("item_id", row.get("item_id"))
        payload.setdefault("item_title", row.get("item_title"))
        payload.setdefault("title", row.get("title"))
        payload.setdefault("start_ms", row.get("start_ms"))
        payload.setdefault("end_ms", row.get("end_ms"))
        payload.setdefault("timeline_ms", row.get("timeline_ms"))
        payload.setdefault("vector_text", row.get("vector_text"))
        payload.setdefault("keyword_text", row.get("keyword_text"))
        payload.setdefault("text_search", row.get("text_search"))
        payload.setdefault("text_raw", row.get("text_raw"))
        payload.setdefault("doc_index", row.get("doc_index", 0))
        lexical_score = float(row.get("lexical_score") or 0.0)
        hits.append(
            {
                "doc_id": row.get("doc_id"),
                "doc_type": row.get("doc_type"),
                "score": 0.42 + 0.40 * (lexical_score / max_lexical_score),
                "start_ms": row.get("start_ms"),
                "end_ms": row.get("end_ms"),
                "timeline_ms": row.get("timeline_ms"),
                "vector_text": row.get("vector_text"),
                "keyword_text": row.get("keyword_text"),
                "text_raw": row.get("text_raw"),
                "text_search": row.get("text_search"),
                "payload": payload,
            }
        )
    return hits


def semantic_degradation_message(error: str) -> str:
    lowered = str(error or "").lower()
    if "1113" in lowered or "余额不足" in error or "资源包" in error:
        return "语义检索额度不足，当前显示的是关键词降级结果，相关性和排序质量会明显下降。"
    if "timed out" in lowered or "timeout" in lowered:
        return "语义检索服务响应超时，当前显示的是关键词降级结果，相关性和排序质量会下降。"
    return "语义检索服务暂时不可用，当前显示的是关键词降级结果，相关性和排序质量会下降。"


def get_episode_details(
    item_id: str,
    *,
    catalog: Optional[QueryCatalog] = None,
    root: str = "data",
    collection_name: Optional[str] = None,
) -> dict:
    active_catalog = catalog or QueryCatalog(root)
    item_docs = active_catalog.get_search_documents_for_item(item_id=item_id, collection_name=collection_name)
    grouped: dict[str, list[dict]] = defaultdict(list)
    for row in item_docs:
        payload = json.loads(row.get("payload_json") or "{}")
        grouped[str(row["doc_type"])].append(
            {
                "doc_id": row["doc_id"],
                "doc_type": row["doc_type"],
                "doc_index": row.get("doc_index"),
                "title": row.get("title"),
                "start_ms": row.get("start_ms"),
                "end_ms": row.get("end_ms"),
                "timeline_ms": row.get("timeline_ms"),
                "text_raw": row.get("text_raw"),
                "text_search": row.get("text_search"),
                "payload": payload,
            }
        )
    item_row = active_catalog.get_item_display_record(item_key=f"radios:{item_id}")
    return {"item": item_row, "documents": grouped}


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
    )
    catalog = QueryCatalog(root)
    qdrant = ReadOnlySearchQdrantStore(config)
    try:
        alias_map = resolve_alias_map(config)
        return run_search_query_with_runtime(
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
    finally:
        qdrant.close()


def run_search_query_with_runtime(
    query: str,
    *,
    catalog: QueryCatalog,
    qdrant: ReadOnlySearchQdrantStore,
    config: SearchIndexConfig,
    alias_map: Optional[dict[str, str]] = None,
    limit: int = 10,
    scope: str = "content",
    doc_types: Optional[Sequence[str]] = None,
    categories: Optional[Sequence[str]] = None,
    participants: Optional[Sequence[str]] = None,
    tuning: Optional[SearchTuning] = None,
) -> dict:
    resolved_alias_map = alias_map or resolve_alias_map(config)
    resolved_tuning = tuning or SearchTuning()
    normalized_query, replacements = replace_aliases(query, resolved_alias_map)
    query_terms = extract_query_terms(normalized_query)
    categories_list = [value.strip() for value in (categories or []) if str(value).strip()]
    participants_list = [value.strip() for value in (participants or []) if str(value).strip()]
    scope_doc_types = resolve_search_scope_doc_types(scope, requested_doc_types=doc_types)
    primary_limit = max(limit, int(resolved_tuning.candidate_limit))
    semantic_error: Optional[str] = None
    atom_second_pass_limit = 0
    try:
        embedding = qdrant.embed_query(normalized_query)
        primary_hits = annotate_recall_hits(
            qdrant.query(
                vector=embedding,
                limit=primary_limit,
                doc_types=scope_doc_types,
                categories=categories_list,
                participants=participants_list,
            ),
            recall_stage="primary",
        )
        raw_candidates = list(primary_hits)
        if should_run_atom_second_pass(scope=scope, tuning=resolved_tuning):
            atom_second_pass_limit = resolve_atom_second_pass_limit(limit=limit, tuning=resolved_tuning)
            if atom_second_pass_limit > 0:
                raw_candidates.extend(
                    annotate_recall_hits(
                        qdrant.query(
                            vector=embedding,
                            limit=atom_second_pass_limit,
                            doc_types=["evidence_atom"],
                            categories=categories_list,
                            participants=participants_list,
                        ),
                        recall_stage="second_pass_atom",
                    )
                )
    except Exception as exc:
        semantic_error = str(exc)
        primary_hits = annotate_recall_hits(
            build_lexical_hits_from_catalog_rows(
                catalog.search_documents_by_terms(
                    query_terms=query_terms,
                    collection_name=config.collection_name,
                    doc_types=scope_doc_types,
                    categories=categories_list,
                    participants=participants_list,
                    limit=primary_limit,
                )
            ),
            recall_stage="lexical_fallback",
        )
        raw_candidates = list(primary_hits)
    raw_hits = dedupe_hits_by_doc_id(raw_candidates)
    reranked = rerank_hits(raw_hits=raw_hits, query_terms=query_terms, tuning=resolved_tuning)
    diversified = diversify_hits(
        reranked=reranked,
        limit=max(1, limit),
        diversify_enabled=resolved_tuning.diversify_enabled,
    )
    neighbor_cache: dict[tuple[Any, ...], List[dict]] = {}
    results = [
        build_result_payload(
            hit=hit,
            catalog=catalog,
            collection_name=config.collection_name,
            neighbor_cache=neighbor_cache,
        )
        for hit in diversified
    ]
    payload = {
        "query": query,
        "normalized_query": normalized_query,
        "alias_replacements": replacements,
        "query_terms": query_terms,
        "scope": scope,
        "doc_types": scope_doc_types,
        "categories": categories_list,
        "participants": participants_list,
        "tuning": {
            "candidate_limit": resolved_tuning.candidate_limit,
            "rerank_enabled": resolved_tuning.rerank_enabled,
            "diversify_enabled": resolved_tuning.diversify_enabled,
            "second_pass_atom_enabled": resolved_tuning.second_pass_atom_enabled,
            "second_pass_atom_limit": resolved_tuning.second_pass_atom_limit,
            "doc_type_prior_weight": resolved_tuning.doc_type_prior_weight,
            "keyword_overlap_weight": resolved_tuning.keyword_overlap_weight,
            "title_weight": resolved_tuning.title_weight,
            "timeline_title_weight": resolved_tuning.timeline_title_weight,
            "participant_weight": resolved_tuning.participant_weight,
            "multi_hit_weight": resolved_tuning.multi_hit_weight,
        },
        "recall_stats": {
            "primary_hits": len(primary_hits),
            "second_pass_atom_hits": max(0, len(raw_candidates) - len(primary_hits)),
            "second_pass_atom_limit": atom_second_pass_limit,
            "merged_hits": len(raw_hits),
            "lexical_fallback": bool(semantic_error),
        },
        "results": results,
    }
    if semantic_error:
        payload["degraded"] = True
        payload["degraded_reason"] = "semantic_recall_unavailable"
        payload["degraded_message"] = semantic_degradation_message(semantic_error)
        payload["degraded_detail"] = "本次没有使用向量语义召回，只使用本地关键词索引进行降级检索。"
        payload["semantic_error"] = semantic_error[:500]
    return payload


def resolve_hit_keyword_text(hit: dict) -> str:
    payload = hit.get("payload") or {}
    return str(
        hit.get("keyword_text")
        or payload.get("keyword_text")
        or hit.get("text_search")
        or payload.get("text_search")
        or ""
    )


def rerank_hits(*, raw_hits: List[dict], query_terms: List[str], tuning: SearchTuning) -> List[dict]:
    item_counts = Counter(hit["payload"].get("item_key") for hit in raw_hits)
    reranked: List[dict] = []
    for hit in raw_hits:
        payload = hit["payload"]
        doc_type = str(hit.get("doc_type") or "")
        searchable_parts = [
            payload.get("title"),
            payload.get("item_title"),
            resolve_hit_keyword_text(hit),
        ]
        searchable = " ".join(str(part or "") for part in searchable_parts)
        score = float(hit["score"])
        if not tuning.rerank_enabled:
            reranked.append(
                {
                    **hit,
                    "rerank_score": score,
                    "why_matched": ["vector_only"],
                    "matched_terms": [],
                    "match_details": build_match_details(hit=hit, matched_terms=[]),
                }
            )
            continue
        reasons: List[str] = []
        matched_terms: List[str] = []
        score += DOC_TYPE_PRIORS.get(doc_type, 0.0) * tuning.doc_type_prior_weight
        overlaps = [term for term in query_terms if term and term in searchable]
        if overlaps:
            matched_terms = dedupe_preserve_order(overlaps[:8])
            overlap_bonus = 0.0
            for term in matched_terms:
                term_len = len(term)
                if term_len <= 2:
                    overlap_bonus += 0.02
                elif term_len <= 4:
                    overlap_bonus += 0.05
                else:
                    overlap_bonus += 0.08
            score += min(0.26, overlap_bonus) * tuning.keyword_overlap_weight
            reasons.append(f"命中词: {', '.join(matched_terms[:4])}")
        title = str(payload.get("title") or "")
        if title and any(term in title for term in query_terms):
            score += 0.06 * tuning.title_weight
            reasons.append("命中文档标题")
        timeline_title = str(payload.get("timeline_title") or "")
        if timeline_title and any(term in timeline_title for term in query_terms):
            score += 0.08 * tuning.timeline_title_weight
            reasons.append("命中时间轴标题")
        users = payload.get("users") or []
        if doc_type != "item_title" and any(term in " ".join(users) for term in query_terms):
            score += 0.05 * tuning.participant_weight
            reasons.append("命中参与者")
        frequency = item_counts.get(payload.get("item_key"), 0)
        if frequency > 1:
            score += min(0.08, 0.02 * (frequency - 1)) * tuning.multi_hit_weight
            reasons.append(f"同一期多处命中 ({frequency})")
        if hit.get("recall_stage") == "second_pass_atom":
            score -= 0.015
            reasons.append("补充召回: 转录片段")
        reranked.append(
            {
                **hit,
                "rerank_score": score,
                "why_matched": dedupe_preserve_order(reasons),
                "matched_terms": matched_terms,
                "match_details": build_match_details(hit=hit, matched_terms=matched_terms),
            }
        )
    reranked.sort(key=lambda row: row["rerank_score"], reverse=True)
    return reranked


def diversify_hits(*, reranked: List[dict], limit: int, diversify_enabled: bool = True) -> List[dict]:
    if limit <= 0:
        return []
    if not diversify_enabled:
        return reranked[:limit]
    chosen: List[dict] = []
    item_counts: Counter[str] = Counter()
    chosen_doc_ids: set[str] = set()
    for hit in reranked:
        item_key = str(hit["payload"].get("item_key") or "")
        doc_id = str(hit.get("doc_id") or "")
        if not item_key:
            continue
        if doc_id and doc_id in chosen_doc_ids:
            continue
        if item_counts[item_key] >= 1:
            continue
        chosen.append(hit)
        item_counts[item_key] += 1
        if doc_id:
            chosen_doc_ids.add(doc_id)
        if len(chosen) >= limit:
            return chosen
    for hit in reranked:
        item_key = str(hit["payload"].get("item_key") or "")
        doc_id = str(hit.get("doc_id") or "")
        if not item_key:
            continue
        if doc_id and doc_id in chosen_doc_ids:
            continue
        if item_counts[item_key] >= 2:
            continue
        chosen.append(hit)
        item_counts[item_key] += 1
        if doc_id:
            chosen_doc_ids.add(doc_id)
        if len(chosen) >= limit:
            return chosen
    return chosen[:limit]


def dedupe_hits_by_doc_id(hits: List[dict]) -> List[dict]:
    merged: dict[str, dict] = {}
    order: List[str] = []
    for hit in hits:
        doc_id = str(hit.get("doc_id") or "")
        if not doc_id:
            continue
        previous = merged.get(doc_id)
        if previous is None or float(hit.get("score") or 0.0) > float(previous.get("score") or 0.0):
            merged[doc_id] = hit
        if doc_id not in order:
            order.append(doc_id)
    return [merged[doc_id] for doc_id in order if doc_id in merged]


def build_match_details(*, hit: dict, matched_terms: Sequence[str]) -> List[dict]:
    details: List[dict] = []
    terms = dedupe_preserve_order([str(value) for value in matched_terms if str(value)])
    if terms:
        details.append(
            {
                "kind": "keyword",
                "label": "关键词",
                "terms": list(terms[:6]),
            }
        )
    semantic_excerpt = build_semantic_excerpt(hit=hit, matched_terms=terms)
    if semantic_excerpt:
        details.append(
            {
                "kind": "semantic",
                "label": "语义命中片段" if not terms else "相关片段",
                "text": semantic_excerpt,
            }
        )
    return details


def build_semantic_excerpt(*, hit: dict, matched_terms: Sequence[str], max_chars: int = 180) -> Optional[str]:
    payload = hit.get("payload") or {}
    text = normalize_text(
        str(
            resolve_hit_keyword_text(hit)
            or hit.get("text_raw")
            or payload.get("summary")
            or payload.get("timeline_content")
            or payload.get("title")
            or ""
        )
    )
    if not text:
        return None
    for term in matched_terms:
        index = text.find(term)
        if index >= 0:
            start = max(0, index - max_chars // 3)
            end = min(len(text), index + max_chars)
            snippet = text[start:end].strip()
            if start > 0:
                snippet = f"...{snippet}"
            if end < len(text):
                snippet = f"{snippet}..."
            return snippet
    if hit.get("recall_stage") == "second_pass_atom" or hit.get("doc_type") == "evidence_atom":
        if len(text) <= max_chars:
            return text
        return f"{text[:max_chars].rstrip()}..."
    return None


def build_result_payload(
    *,
    hit: dict,
    catalog: QueryCatalog,
    collection_name: Optional[str] = None,
    neighbor_cache: Optional[dict[tuple[Any, ...], List[dict]]] = None,
) -> dict:
    payload = hit["payload"]
    item_key = str(payload.get("item_key") or "")
    item_meta = catalog.get_item_display_record(item_key=item_key) if item_key else None
    doc_index = int(payload.get("doc_index", 0))
    neighbors = build_neighbor_atoms(
        hit=hit,
        catalog=catalog,
        doc_index=doc_index,
        collection_name=collection_name,
        neighbor_cache=neighbor_cache if neighbor_cache is not None else {},
    )
    display_text = ""
    if hit.get("doc_type") != "item_title":
        display_text = hit.get("text_raw") or payload.get("summary") or payload.get("timeline_content") or ""
    display_timestamp = format_ms(hit.get("start_ms") if hit.get("start_ms") is not None else hit.get("timeline_ms"))
    match_summary = build_match_summary(
        hit=hit,
        matched_terms=hit.get("matched_terms") or [],
        display_timestamp=display_timestamp,
    )
    return {
        "doc_id": hit["doc_id"],
        "doc_type": hit["doc_type"],
        "score": hit["score"],
        "rerank_score": hit["rerank_score"],
        "item_key": item_key,
        "item_id": payload.get("item_id"),
        "item_title": payload.get("item_title"),
        "title": payload.get("title"),
        "published_at": payload.get("published_at"),
        "category": payload.get("category"),
        "users": payload.get("users") or [],
        "tags": payload.get("tags") or [],
        "cover_url": item_meta.get("cover_url") if item_meta else None,
        "thumb_url": item_meta.get("thumb_url") if item_meta else None,
        "excerpt": item_meta.get("excerpt") if item_meta else None,
        "desc": item_meta.get("desc") if item_meta else None,
        "duration": item_meta.get("duration") if item_meta else None,
        "start_ms": hit.get("start_ms"),
        "end_ms": hit.get("end_ms"),
        "timeline_ms": hit.get("timeline_ms"),
        "display_timestamp": display_timestamp,
        "display_text": display_text,
        "timeline_title": payload.get("timeline_title"),
        "timeline_content": payload.get("timeline_content"),
        "timeline_at": payload.get("timeline_at"),
        "timeline_asset_url": payload.get("asset_url"),
        "timeline_quote_href": payload.get("quote_href"),
        "recall_stage": hit.get("recall_stage"),
        "why_matched": hit.get("why_matched") or [],
        "matched_terms": hit.get("matched_terms") or [],
        "match_details": hit.get("match_details") or [],
        "match_summary": match_summary,
        "source_url": payload.get("item_url") or (item_meta.get("url") if item_meta else None),
        "neighbor_atoms": neighbors,
    }


def build_neighbor_atoms(
    *,
    hit: dict,
    catalog: QueryCatalog,
    doc_index: int,
    neighbor_cache: dict[tuple[Any, ...], List[dict]],
    collection_name: Optional[str] = None,
) -> List[dict]:
    neighbors: List[dict] = []
    payload = hit["payload"]
    item_key = str(payload.get("item_key") or "")

    if hit["doc_type"] == "evidence_atom":
        cache_key = ("evidence_atom", item_key, doc_index - 1, doc_index + 1)
        rows = neighbor_cache.get(cache_key)
        if rows is None:
            rows = catalog.get_evidence_atoms_by_item_key_and_doc_index_range(
                item_key=item_key,
                start_index=max(0, doc_index - 1),
                end_index=doc_index + 1,
                collection_name=collection_name,
            )
            neighbor_cache[cache_key] = rows
        for row in rows:
            neighbors.append(
                {
                    "doc_id": row["doc_id"],
                    "start_ms": row.get("start_ms"),
                    "end_ms": row.get("end_ms"),
                    "text": row.get("text_raw"),
                    "display_timestamp": format_ms(row.get("start_ms")),
                }
            )
        return neighbors

    if hit["doc_type"] == "scene_summary":
        atom_ids = set((hit["payload"].get("atom_ids") or [])[:8])
        cache_key = ("scene_summary", tuple(sorted(atom_ids)))
        rows = neighbor_cache.get(cache_key)
        if rows is None:
            rows = catalog.get_search_documents_by_doc_ids(
                doc_ids=sorted(atom_ids),
                collection_name=collection_name,
            )
            neighbor_cache[cache_key] = rows
        for row in rows:
            neighbors.append(
                {
                    "doc_id": row["doc_id"],
                    "start_ms": row.get("start_ms"),
                    "end_ms": row.get("end_ms"),
                    "text": row.get("text_raw"),
                    "display_timestamp": format_ms(row.get("start_ms")),
                }
            )
        return neighbors[:6]

    if hit["doc_type"] == "timeline_note":
        anchor_ms = int(hit["payload"].get("anchor_ms") or hit.get("start_ms") or 0)
        cache_key = ("timeline_note", item_key, anchor_ms)
        rows = neighbor_cache.get(cache_key)
        if rows is None:
            rows = catalog.get_evidence_atoms_by_item_key_and_time_range(
                item_key=item_key,
                start_ms=max(0, anchor_ms - TIMELINE_WINDOW_MS * 2),
                end_ms=anchor_ms + TIMELINE_WINDOW_MS * 2,
                collection_name=collection_name,
            )
            neighbor_cache[cache_key] = rows
        ranked: List[tuple[int, dict]] = []
        for row in rows:
            row_start = int(row.get("start_ms") or 0)
            row_end = int(row.get("end_ms") or row_start)
            distance = min(abs(row_start - anchor_ms), abs(row_end - anchor_ms))
            if distance <= TIMELINE_WINDOW_MS * 2:
                ranked.append(
                    (
                        distance,
                        {
                            "doc_id": row["doc_id"],
                            "start_ms": row.get("start_ms"),
                            "end_ms": row.get("end_ms"),
                            "text": row.get("text_raw"),
                            "display_timestamp": format_ms(row.get("start_ms")),
                        },
                    )
                )
        ranked.sort(key=lambda item: (item[0], item[1]["start_ms"] or 0))
        return [item[1] for item in ranked[:4]]

    return []


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


def build_match_summary(*, hit: dict, matched_terms: Sequence[str], display_timestamp: Optional[str]) -> str:
    doc_type = str(hit.get("doc_type") or "")
    terms = [str(value) for value in matched_terms if str(value)]
    parts: List[str] = []
    if terms:
        parts.append(f"提到了 {'、'.join(terms[:3])}")
        if doc_type == "timeline_note":
            parts.append("命中时间轴记录")
        elif doc_type == "episode_card":
            parts.append("命中节目摘要")
        elif doc_type == "scene_summary":
            parts.append("命中节目片段摘要")
        elif doc_type == "evidence_atom":
            parts.append("命中原始转录片段")
        elif doc_type == "item_title":
            parts.append("命中节目标题")
    else:
        parts.append("语义命中")
        parts.append("匹配到相近描述")
    if display_timestamp and doc_type != "item_title":
        parts.append(f"位置约在 {display_timestamp}")
    if not parts:
        return "这是与你的线索最接近的一条结果"
    return "，".join(parts)


class QueryRuntime:
    def __init__(
        self,
        *,
        catalog: QueryCatalog,
        qdrant: ReadOnlySearchQdrantStore,
        config: SearchIndexConfig,
        alias_map: Optional[dict[str, str]] = None,
    ) -> None:
        self.catalog = catalog
        self.qdrant = qdrant
        self.config = config
        self.alias_map = alias_map or resolve_alias_map(config)

    def search(
        self,
        query: str,
        *,
        limit: int = 10,
        scope: str = "content",
        doc_types: Optional[Sequence[str]] = None,
        categories: Optional[Sequence[str]] = None,
        participants: Optional[Sequence[str]] = None,
        tuning: Optional[SearchTuning] = None,
    ) -> dict:
        return run_search_query_with_runtime(
            query,
            catalog=self.catalog,
            qdrant=self.qdrant,
            config=self.config,
            alias_map=self.alias_map,
            limit=limit,
            scope=scope,
            doc_types=doc_types,
            categories=categories,
            participants=participants,
            tuning=tuning,
        )

    def episode_details(self, item_id: str) -> dict:
        return get_episode_details(
            item_id,
            catalog=self.catalog,
            collection_name=self.config.collection_name,
        )
