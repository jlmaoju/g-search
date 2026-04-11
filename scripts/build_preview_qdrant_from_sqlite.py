from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Iterable, List

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from gcores_crawler.catalog import Catalog
from gcores_crawler.query_core import SearchIndexConfig
from gcores_crawler.search_pipeline import SearchQdrantStore


DEFAULT_DOC_TYPES = ["item_title", "scene_summary", "timeline_note", "evidence_atom", "episode_card"]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Build preview Qdrant collection directly from SQLite search_documents.")
    parser.add_argument("--root", default="data")
    parser.add_argument("--target-url", required=True)
    parser.add_argument("--collection-name", default="gcores_memory_preview_v1")
    parser.add_argument("--embedding-provider", default="zhipu")
    parser.add_argument("--embedding-model", default="embedding-3")
    parser.add_argument("--embedding-api-key")
    parser.add_argument("--embedding-dimension", type=int, default=0)
    parser.add_argument("--doc-type", action="append", dest="doc_types")
    parser.add_argument("--limit-items", type=int, default=0)
    parser.add_argument("--start-after-item-key", default="")
    parser.add_argument("--reset", action="store_true")
    return parser


def iter_item_keys(catalog: Catalog, *, doc_types: Iterable[str]) -> List[str]:
    with catalog._connect() as connection:  # noqa: SLF001
        params = list(doc_types)
        query = f"""
            SELECT DISTINCT item_key
            FROM search_documents
            WHERE doc_type IN ({", ".join("?" for _ in params)})
            ORDER BY item_key ASC
        """
        rows = connection.execute(query, params).fetchall()
    return [str(row["item_key"]) for row in rows]


def row_to_document(row: dict) -> dict:
    payload = json.loads(row.get("payload_json") or "{}")
    return {
        "doc_id": row["doc_id"],
        "parent_doc_id": row.get("parent_doc_id"),
        "doc_type": row["doc_type"],
        "content_type": row["content_type"],
        "item_id": row["item_id"],
        "item_title": row.get("item_title"),
        "title": row.get("title"),
        "doc_index": int(row.get("doc_index") or 0),
        "start_ms": row.get("start_ms"),
        "end_ms": row.get("end_ms"),
        "timeline_ms": row.get("timeline_ms"),
        "text_raw": row.get("text_raw") or "",
        "text_normalized": row.get("text_normalized") or row.get("text_raw") or "",
        "text_search": row.get("text_search") or "",
        "source_updated_at": row.get("source_updated_at"),
        "payload": payload,
    }


def main() -> int:
    args = build_parser().parse_args()
    doc_types = args.doc_types or DEFAULT_DOC_TYPES
    catalog = Catalog(args.root)
    config = SearchIndexConfig(
        root=args.root,
        qdrant_url=args.target_url,
        collection_name=args.collection_name,
        embedding_provider=args.embedding_provider,
        embedding_model=args.embedding_model,
        embedding_api_key=args.embedding_api_key,
        embedding_dimension=args.embedding_dimension or None,
    )
    qdrant = SearchQdrantStore(config)
    try:
        if args.reset:
            qdrant.reset_collection()
        item_keys = iter_item_keys(catalog, doc_types=doc_types)
        if args.start_after_item_key:
            item_keys = [item_key for item_key in item_keys if item_key > args.start_after_item_key]
        if args.limit_items > 0:
            item_keys = item_keys[: args.limit_items]
        total_items = len(item_keys)
        total_docs = 0
        for index, item_key in enumerate(item_keys, start=1):
            rows = catalog.get_search_documents_for_item(item_key=item_key, doc_types=doc_types)
            documents = [row_to_document(row) for row in rows]
            qdrant.replace_item_documents(item_key=item_key, documents=documents)
            total_docs += len(documents)
            print(
                json.dumps(
                    {
                        "status": "item",
                        "item_key": item_key,
                        "item_index": index,
                        "total_items": total_items,
                        "documents": len(documents),
                        "total_documents": total_docs,
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )
        print(
            json.dumps(
                {
                    "status": "ok",
                    "items": total_items,
                    "documents": total_docs,
                    "doc_types": doc_types,
                    "collection_name": args.collection_name,
                },
                ensure_ascii=False,
            ),
            flush=True,
        )
        return 0
    finally:
        qdrant.close()


if __name__ == "__main__":
    raise SystemExit(main())
