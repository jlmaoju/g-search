from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from gcores_crawler.query_core import write_search_ui_meta_snapshot


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build a precomputed UI meta snapshot for the search frontend.")
    parser.add_argument("--root", default=str(PROJECT_ROOT / "data"))
    parser.add_argument("--collection-name", default="gcores_memory_release_v3")
    parser.add_argument("--participants-limit", type=int, default=0, help="0 includes every participant")
    parser.add_argument(
        "--output-json",
        default=str(PROJECT_ROOT / "data" / "reports" / "search_ui_meta.json"),
    )
    return parser.parse_args()


def safe_print_json(payload: dict) -> None:
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    try:
        print(text)
    except UnicodeEncodeError:
        print(json.dumps(payload, ensure_ascii=True, indent=2))


def main() -> int:
    args = parse_args()
    payload = write_search_ui_meta_snapshot(
        root=args.root,
        collection_name=args.collection_name,
        participants_limit=args.participants_limit if args.participants_limit > 0 else None,
        output_path=args.output_json,
    )
    safe_print_json(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
