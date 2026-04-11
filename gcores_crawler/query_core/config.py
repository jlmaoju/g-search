from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional


DEFAULT_COLLECTION = "gcores_memory_v1"
DEFAULT_QDRANT_PATH = "qdrant"
DEFAULT_EMBEDDING_PROVIDER = "zhipu"
DEFAULT_EMBEDDING_MODEL = "embedding-3"
DEFAULT_EMBEDDING_BATCH_SIZE = 64
DEFAULT_EMBEDDING_CONCURRENCY = 8
DEFAULT_GLM_PROVIDER = "zhipu"
DEFAULT_GLM_MODEL = "glm-5"


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
    embedding_api_key_resolver: Optional[Callable[[], Optional[str]]] = field(
        default=None,
        repr=False,
        compare=False,
    )

    @property
    def root_path(self) -> Path:
        return Path(self.root)

    @property
    def qdrant_root(self) -> Path:
        path = Path(self.qdrant_path)
        if not path.is_absolute():
            path = self.root_path / path
        return path

    @property
    def resolved_embedding_api_key(self) -> Optional[str]:
        if self.embedding_api_key_resolver is not None:
            return self.embedding_api_key_resolver()
        return self.embedding_api_key


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
