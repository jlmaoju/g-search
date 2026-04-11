from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from gcores_crawler.firered_local import FireRedRunner, FireRedRuntimeConfig, save_payload


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="FireRedASR2 wrapper for local batch transcription")
    parser.add_argument("--input", required=True, help="Input media path")
    parser.add_argument("--output", required=True, help="Output JSON path")
    parser.add_argument("--uttid", default=None, help="Optional stable utterance id")
    parser.add_argument("--disable-lid", action="store_true")
    parser.add_argument("--disable-punc", action="store_true")
    parser.add_argument("--asr-batch-size", type=int, default=4)
    parser.add_argument("--punc-batch-size", type=int, default=8)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    input_path = Path(args.input).resolve()
    output_path = Path(args.output).resolve()

    runner = FireRedRunner(
        FireRedRuntimeConfig(
            disable_lid=args.disable_lid,
            disable_punc=args.disable_punc,
            asr_batch_size=args.asr_batch_size,
            punc_batch_size=args.punc_batch_size,
            use_half=False,
            beam_size=3,
            return_timestamp=True,
        )
    )
    payload = runner.transcribe_path(input_path, uttid=args.uttid or input_path.stem)
    save_payload(output_path, payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
