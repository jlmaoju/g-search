import argparse
import json
from pathlib import Path
from time import perf_counter
from typing import Any

from funasr import AutoModel


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run FunASR model on a local audio file.")
    parser.add_argument("--input", required=True, help="Path to the input audio file.")
    parser.add_argument("--output", required=True, help="Path to the output JSON file.")
    parser.add_argument("--uttid", default=None, help="Optional utterance id.")
    parser.add_argument(
        "--model",
        default="iic/speech_paraformer-large-vad-punc_asr_nat-zh-cn-16k-common-vocab8404-pytorch",
    )
    parser.add_argument("--vad-model", default="fsmn-vad")
    parser.add_argument("--punc-model", default="ct-punc")
    parser.add_argument("--spk-model", default="cam++")
    parser.add_argument("--device", default="cuda:0")
    parser.add_argument("--batch-size-s", type=int, default=300)
    parser.add_argument("--no-speaker", action="store_true", help="Disable speaker model.")
    return parser.parse_args()


def ms_to_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(round(float(value)))
    except Exception:
        return None


def pick_sentence_bounds(sentence: dict[str, Any]) -> tuple[int | None, int | None]:
    start = (
        sentence.get("start")
        or sentence.get("start_time")
        or sentence.get("bg")
        or sentence.get("begin_time")
    )
    end = (
        sentence.get("end")
        or sentence.get("end_time")
        or sentence.get("ed")
        or sentence.get("stop_time")
    )
    return ms_to_int(start), ms_to_int(end)


def main() -> None:
    args = parse_args()
    input_path = Path(args.input).resolve()
    output_path = Path(args.output).resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)

    model_kwargs = {
        "model": args.model,
        "vad_model": args.vad_model,
        "punc_model": args.punc_model,
        "device": args.device,
    }
    if not args.no_speaker:
        model_kwargs["spk_model"] = args.spk_model

    started = perf_counter()
    model = AutoModel(**model_kwargs)
    results = model.generate(
        input=str(input_path),
        batch_size_s=args.batch_size_s,
        sentence_timestamp=True,
        return_spk_res=not args.no_speaker,
    )
    elapsed = perf_counter() - started

    if not results:
        raise RuntimeError("FunASR returned no results")
    result = results[0]

    segments = []
    for sentence in result.get("sentence_info") or []:
        start_ms, end_ms = pick_sentence_bounds(sentence)
        speaker_id = None
        for key in ("spk", "speaker", "speaker_id", "spk_id"):
            if key in sentence and sentence.get(key) is not None:
                speaker_id = sentence.get(key)
                break
        segments.append(
            {
                "text": sentence.get("text"),
                "start_ms": start_ms,
                "end_ms": end_ms,
                "speaker_id": str(speaker_id) if speaker_id is not None else None,
                "language": result.get("lang"),
            }
        )

    words = []
    raw_timestamp = result.get("timestamp") or []
    if raw_timestamp:
        for token in raw_timestamp:
            if not isinstance(token, (list, tuple)) or len(token) < 3:
                continue
            words.append(
                {
                    "text": token[0],
                    "start_ms": ms_to_int(token[1]),
                    "end_ms": ms_to_int(token[2]),
                }
            )
    else:
        for sentence in result.get("sentence_info") or []:
            sentence_text = sentence.get("text") or ""
            token_times = sentence.get("timestamp") or []
            if not token_times:
                continue
            chars = [ch for ch in sentence_text if not ch.isspace()]
            for index, span in enumerate(token_times):
                if not isinstance(span, (list, tuple)) or len(span) < 2:
                    continue
                token_text = chars[index] if index < len(chars) else None
                words.append(
                    {
                        "text": token_text,
                        "start_ms": ms_to_int(span[0]),
                        "end_ms": ms_to_int(span[1]),
                    }
                )

    payload = {
        "provider": "funasr",
        "model_name": args.model,
        "language": result.get("lang"),
        "text": result.get("text"),
        "segments": segments,
        "words": words,
        "metadata": {
            "uttid": args.uttid,
            "input_path": str(input_path),
            "device": args.device,
            "elapsed_seconds": round(elapsed, 3),
            "batch_size_s": args.batch_size_s,
            "speaker_enabled": not args.no_speaker,
            "raw_result": result,
        },
    }
    output_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
