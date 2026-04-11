from __future__ import annotations

import argparse
import json
from pathlib import Path

from qdrant_client import QdrantClient, models


DEFAULT_SOURCE_COLLECTION = "gcores_memory_v1"
DEFAULT_TARGET_COLLECTION = "gcores_memory_preview_v1"
DEFAULT_DOC_TYPES = ["item_title", "scene_summary", "timeline_note", "evidence_atom", "episode_card"]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Sync preview doc types from embedded Qdrant into a server-backed Qdrant collection.")
    parser.add_argument("--source-path", required=True)
    parser.add_argument("--target-url", required=True)
    parser.add_argument("--source-collection", default=DEFAULT_SOURCE_COLLECTION)
    parser.add_argument("--target-collection", default=DEFAULT_TARGET_COLLECTION)
    parser.add_argument("--batch-size", type=int, default=512)
    parser.add_argument("--doc-type", action="append", dest="doc_types")
    parser.add_argument("--item-key", action="append", dest="item_keys")
    parser.add_argument("--no-recreate", action="store_true")
    parser.add_argument("--max-points", type=int, default=0)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    source_path = Path(args.source_path).resolve()
    doc_types = args.doc_types or DEFAULT_DOC_TYPES
    item_keys = [value.strip() for value in (args.item_keys or []) if value and value.strip()]

    source = QdrantClient(path=str(source_path))
    target = QdrantClient(url=args.target_url)
    try:
        source_info = source.get_collection(args.source_collection)
        vectors_config = source_info.config.params.vectors
        if args.no_recreate:
            if not target.collection_exists(args.target_collection):
                target.recreate_collection(
                    collection_name=args.target_collection,
                    vectors_config=vectors_config,
                )
        else:
            target.recreate_collection(
                collection_name=args.target_collection,
                vectors_config=vectors_config,
            )

        must_conditions: list[models.Condition] = [
            models.FieldCondition(
                key="doc_type",
                match=models.MatchAny(any=list(doc_types)),
            )
        ]
        if item_keys:
            must_conditions.append(
                models.FieldCondition(
                    key="item_key",
                    match=models.MatchAny(any=item_keys),
                )
            )
            target.delete(
                collection_name=args.target_collection,
                points_selector=models.FilterSelector(
                    filter=models.Filter(
                        must=[
                            models.FieldCondition(
                                key="item_key",
                                match=models.MatchAny(any=item_keys),
                            )
                        ]
                    )
                ),
                wait=True,
            )

        scroll_filter = models.Filter(must=must_conditions)
        total = 0
        offset = None
        while True:
            points, offset = source.scroll(
                collection_name=args.source_collection,
                scroll_filter=scroll_filter,
                limit=max(1, args.batch_size),
                offset=offset,
                with_payload=True,
                with_vectors=True,
            )
            if not points:
                break
            upsert_points = []
            for point in points:
                upsert_points.append(
                    models.PointStruct(
                        id=point.id,
                        vector=point.vector,
                        payload=dict(point.payload or {}),
                    )
                )
            target.upsert(collection_name=args.target_collection, points=upsert_points, wait=True)
            total += len(upsert_points)
            print(json.dumps({"status": "batch", "points": len(upsert_points), "total": total}, ensure_ascii=False), flush=True)
            if args.max_points and total >= args.max_points:
                break
            if offset is None:
                break
        print(
            json.dumps(
                {
                    "status": "ok",
                    "total": total,
                    "doc_types": doc_types,
                    "item_keys": item_keys,
                    "no_recreate": bool(args.no_recreate),
                },
                ensure_ascii=False,
            ),
            flush=True,
        )
        return 0
    finally:
        source.close()
        target.close()


if __name__ == "__main__":
    raise SystemExit(main())
