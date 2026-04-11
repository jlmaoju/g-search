from __future__ import annotations

from typing import List, Optional, Sequence

from ..search_providers import create_embedding_provider
from .config import (
    DEFAULT_EMBEDDING_BATCH_SIZE,
    SearchIndexConfig,
)

try:
    from qdrant_client import QdrantClient, models
except ImportError:  # pragma: no cover
    QdrantClient = None
    models = None


class ReadOnlySearchQdrantStore:
    def __init__(self, config: SearchIndexConfig) -> None:
        if QdrantClient is None or models is None:
            raise RuntimeError("qdrant-client is not installed")
        self.config = config
        self._embedding_provider = None
        self._embedding_signature: Optional[tuple[object, ...]] = None
        if config.qdrant_url:
            self.client = QdrantClient(
                url=config.qdrant_url,
                api_key=config.qdrant_api_key,
                timeout=300,
            )
        else:
            self.client = QdrantClient(path=str(config.qdrant_root))
        self._query_embedding_cache: dict[str, List[float]] = {}

    def _embedding_batch_size(self) -> int:
        configured = int(self.config.embedding_batch_size or 0)
        if configured > 0:
            return configured
        return DEFAULT_EMBEDDING_BATCH_SIZE

    def _current_embedding_signature(self) -> tuple[object, ...]:
        return (
            self.config.embedding_provider,
            self.config.embedding_model,
            self.config.embedding_dimension,
            self._embedding_batch_size(),
            self.config.resolved_embedding_api_key,
        )

    def _resolve_embedding_provider(self):
        signature = self._current_embedding_signature()
        if self._embedding_provider is None or self._embedding_signature != signature:
            self._embedding_provider = create_embedding_provider(
                provider_name=self.config.embedding_provider,
                api_key=self.config.resolved_embedding_api_key,
                model=self.config.embedding_model,
                dimension=self.config.embedding_dimension,
                batch_size=self._embedding_batch_size(),
            )
            self._embedding_signature = signature
            self._query_embedding_cache.clear()
        return self._embedding_provider

    def collection_exists(self) -> bool:
        return bool(self.client.collection_exists(self.config.collection_name))

    def query(
        self,
        *,
        vector: List[float],
        limit: int,
        doc_types: Optional[Sequence[str]] = None,
        categories: Optional[Sequence[str]] = None,
        participants: Optional[Sequence[str]] = None,
    ) -> List[dict]:
        must_conditions: list[object] = []
        if doc_types:
            must_conditions.append(
                models.FieldCondition(
                    key="doc_type",
                    match=models.MatchAny(any=list(doc_types)),
                )
            )
        if categories:
            resolved_categories = [str(value).strip() for value in categories if str(value).strip()]
            if resolved_categories:
                must_conditions.append(
                    models.FieldCondition(
                        key="category",
                        match=models.MatchAny(any=resolved_categories),
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
        results: List[dict] = []
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
        provider = self._resolve_embedding_provider()
        vector = provider.embed_queries([query])[0]
        self._query_embedding_cache[query] = vector
        if len(self._query_embedding_cache) > 256:
            oldest_key = next(iter(self._query_embedding_cache))
            self._query_embedding_cache.pop(oldest_key, None)
        return vector

    def close(self) -> None:
        close = getattr(self.client, "close", None)
        if callable(close):
            close()
