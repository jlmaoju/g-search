import argparse
import json
from dataclasses import asdict, is_dataclass
from pathlib import Path
from time import perf_counter
from typing import Any

import librosa
import numpy as np
import torch
from qwen_asr import Qwen3ASRModel


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run Qwen3-ASR on a local audio file.")
    parser.add_argument("--input", required=True, help="Path to the input audio file.")
    parser.add_argument("--output", required=True, help="Path to the output JSON file.")
    parser.add_argument("--uttid", default=None, help="Optional utterance id.")
    parser.add_argument("--model", default="Qwen/Qwen3-ASR-1.7B")
    parser.add_argument("--aligner", default="Qwen/Qwen3-ForcedAligner-0.6B")
    parser.add_argument("--device", default="cuda:0")
    parser.add_argument("--dtype", default="bfloat16", choices=["bfloat16", "float16", "float32"])
    parser.add_argument("--max-inference-batch-size", type=int, default=8)
    parser.add_argument("--max-new-tokens", type=int, default=1024)
    parser.add_argument("--language", default=None, help="Optional forced language, e.g. Chinese.")
    parser.add_argument("--no-timestamps", action="store_true", help="Disable forced alignment timestamps.")
    parser.add_argument("--chunk-seconds", type=int, default=0, help="Split long audio into fixed-length chunks; 0 disables chunking.")
    parser.add_argument("--chunk-overlap-seconds", type=float, default=1.0, help="Overlap between chunks in seconds.")
    return parser.parse_args()


def torch_dtype(name: str) -> torch.dtype:
    mapping = {
        "bfloat16": torch.bfloat16,
        "float16": torch.float16,
        "float32": torch.float32,
    }
    return mapping[name]


def dataclass_to_jsonable(value: Any) -> Any:
    if is_dataclass(value):
        return {key: dataclass_to_jsonable(val) for key, val in asdict(value).items()}
    if isinstance(value, list):
        return [dataclass_to_jsonable(item) for item in value]
    if isinstance(value, dict):
        return {key: dataclass_to_jsonable(val) for key, val in value.items()}
    return value


def load_audio(path: Path, sample_rate: int = 16000) -> np.ndarray:
    waveform, _ = librosa.load(str(path), sr=sample_rate, mono=True)
    return np.asarray(waveform, dtype=np.float32)


def split_audio(
    waveform: np.ndarray,
    sample_rate: int,
    chunk_seconds: int,
    overlap_seconds: float,
) -> list[dict[str, Any]]:
    if chunk_seconds <= 0:
        return [{"start_ms": 0, "end_ms": int(round(len(waveform) / sample_rate * 1000)), "audio": (waveform, sample_rate)}]

    chunk_samples = max(1, int(round(chunk_seconds * sample_rate)))
    overlap_samples = max(0, int(round(overlap_seconds * sample_rate)))
    step = max(1, chunk_samples - overlap_samples)

    chunks: list[dict[str, Any]] = []
    start = 0
    total = len(waveform)
    while start < total:
        end = min(total, start + chunk_samples)
        chunks.append(
            {
                "start_ms": int(round(start / sample_rate * 1000)),
                "end_ms": int(round(end / sample_rate * 1000)),
                "audio": (waveform[start:end], sample_rate),
            }
        )
        if end >= total:
            break
        start += step
    return chunks


def dedupe_join(parts: list[str], max_overlap_chars: int = 80) -> str:
    merged = ""
    for part in parts:
        chunk = (part or "").strip()
        if not chunk:
            continue
        if not merged:
            merged = chunk
            continue

        max_overlap = min(max_overlap_chars, len(merged), len(chunk))
        overlap = 0
        for size in range(max_overlap, 0, -1):
            if merged[-size:] == chunk[:size]:
                overlap = size
                break
        if overlap:
            merged += chunk[overlap:]
        else:
            merged += "\n" + chunk
    return merged


def main() -> None:
    args = parse_args()
    input_path = Path(args.input).resolve()
    output_path = Path(args.output).resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)

    started = perf_counter()
    model = Qwen3ASRModel.from_pretrained(
        args.model,
        forced_aligner=None if args.no_timestamps else args.aligner,
        forced_aligner_kwargs=None if args.no_timestamps else {"device_map": args.device, "dtype": torch_dtype(args.dtype)},
        dtype=torch_dtype(args.dtype),
        device_map=args.device,
        max_inference_batch_size=args.max_inference_batch_size,
        max_new_tokens=args.max_new_tokens,
    )
    waveform = load_audio(input_path)
    chunks = split_audio(
        waveform=waveform,
        sample_rate=16000,
        chunk_seconds=args.chunk_seconds,
        overlap_seconds=args.chunk_overlap_seconds,
    )
    results = model.transcribe(
        audio=[chunk["audio"] for chunk in chunks],
        language=args.language,
        return_time_stamps=not args.no_timestamps,
    )
    elapsed = perf_counter() - started

    words = []
    segments = []
    stitched_text_parts = []
    raw_results = []
    languages = []

    for chunk_meta, result in zip(chunks, results):
        raw_results.append(dataclass_to_jsonable(result))
        stitched_text_parts.append(result.text)
        if result.language:
            languages.append(result.language)

        if result.time_stamps is not None:
            for item in result.time_stamps.items:
                start_ms = chunk_meta["start_ms"] + int(round(item.start_time * 1000))
                end_ms = chunk_meta["start_ms"] + int(round(item.end_time * 1000))
                word = {
                    "text": item.text,
                    "start_ms": start_ms,
                    "end_ms": end_ms,
                }
                words.append(word)
                segments.append(
                    {
                        "text": item.text,
                        "start_ms": start_ms,
                        "end_ms": end_ms,
                        "speaker_id": None,
                        "language": result.language or None,
                    }
                )

    merged_language = ",".join(sorted(set(languages))) if languages else None
    merged_text = dedupe_join(stitched_text_parts)

    payload = {
        "provider": "qwen3_asr",
        "model_name": args.model,
        "language": merged_language,
        "text": merged_text,
        "segments": segments,
        "words": words,
        "metadata": {
            "uttid": args.uttid,
            "input_path": str(input_path),
            "device": args.device,
            "dtype": args.dtype,
            "elapsed_seconds": round(elapsed, 3),
            "return_time_stamps": not args.no_timestamps,
            "max_inference_batch_size": args.max_inference_batch_size,
            "max_new_tokens": args.max_new_tokens,
            "chunk_seconds": args.chunk_seconds,
            "chunk_overlap_seconds": args.chunk_overlap_seconds,
            "chunk_count": len(chunks),
            "chunks": [{"start_ms": chunk["start_ms"], "end_ms": chunk["end_ms"]} for chunk in chunks],
            "raw_result": raw_results,
        },
    }
    output_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
