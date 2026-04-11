from __future__ import annotations

import argparse
import json
from pathlib import Path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Mock ASR script for local pipeline testing")
    parser.add_argument("--input", required=True)
    parser.add_argument("--output", required=True)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    input_path = Path(args.input)
    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    payload = {
        "model_name": "mock-asr",
        "language": "zh",
        "text": f"Mock transcript for {input_path.name} with {input_path.stat().st_size} bytes.",
        "segments": [
            {
                "start": 0.0,
                "end": 1.0,
                "text": f"Mock transcript for {input_path.name}.",
            }
        ],
    }
    output_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8-sig")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
