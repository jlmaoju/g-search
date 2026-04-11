import argparse
import json
import os
import subprocess
import tempfile
from pathlib import Path
from time import perf_counter, sleep
from typing import Any

import numpy as np
import requests
import soundfile as sf


API_URL = "https://open.bigmodel.cn/api/paas/v4/audio/transcriptions"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run GLM-ASR-2512 on a local audio file by chunking long audio.")
    parser.add_argument("--input", required=True, help="Path to the input audio file.")
    parser.add_argument("--output", required=True, help="Path to the output JSON file.")
    parser.add_argument("--uttid", default=None, help="Optional utterance id.")
    parser.add_argument("--api-key", default=os.environ.get("ZHIPU_API_KEY"), help="Zhipu API key.")
    parser.add_argument("--model", default="glm-asr-2512")
    parser.add_argument("--chunk-seconds", type=float, default=29.0, help="Chunk duration; should remain under the 30 second API limit.")
    parser.add_argument("--chunk-overlap-seconds", type=float, default=1.0, help="Overlap between chunks.")
    parser.add_argument("--sample-rate", type=int, default=16000)
    parser.add_argument("--stream", choices=("true", "false"), default="false")
    parser.add_argument("--language", default=None, help="Optional language hint if the API supports it.")
    parser.add_argument("--no-stitch", action="store_true", help="Keep chunk outputs only and do not build merged text.")
    parser.add_argument("--timeout-seconds", type=int, default=300)
    parser.add_argument("--max-chunks", type=int, default=0, help="Optional limit for validation runs; 0 means all chunks.")
    return parser.parse_args()


def load_audio(path: Path, sample_rate: int) -> np.ndarray:
    with tempfile.TemporaryDirectory(prefix="glm_asr_decode_") as temp_dir_name:
        temp_dir = Path(temp_dir_name)
        wav_path = temp_dir / "decoded.wav"
        command = [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(path),
            "-ac",
            "1",
            "-ar",
            str(sample_rate),
            str(wav_path),
        ]
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        if completed.returncode != 0:
            stderr = completed.stderr.strip() or completed.stdout.strip() or f"ffmpeg exited {completed.returncode}"
            raise RuntimeError(f"ffmpeg decode failed: {stderr}")
        waveform, sr = sf.read(str(wav_path), dtype="float32", always_2d=False)
    if sr != sample_rate:
        raise RuntimeError(f"Decoded audio sample rate mismatch: expected {sample_rate}, got {sr}")
    if isinstance(waveform, np.ndarray) and waveform.ndim > 1:
        waveform = waveform.mean(axis=1)
    return np.asarray(waveform, dtype=np.float32)


def split_audio(
    waveform: np.ndarray,
    sample_rate: int,
    chunk_seconds: float,
    overlap_seconds: float,
) -> list[dict[str, Any]]:
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
                "waveform": waveform[start:end],
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


def extract_text(response_json: dict[str, Any]) -> str:
    for key in ("text", "transcript", "result"):
        value = response_json.get(key)
        if isinstance(value, str):
            return value
    if isinstance(response_json.get("segments"), list):
        texts = [segment.get("text") for segment in response_json["segments"] if isinstance(segment, dict) and segment.get("text")]
        if texts:
            return " ".join(texts)
    return ""


def transcribe_chunk(
    *,
    api_key: str,
    model: str,
    stream: str,
    wav_path: Path,
    timeout_seconds: int,
    language: str | None,
) -> dict[str, Any]:
    headers = {"Authorization": f"Bearer {api_key}"}
    data: dict[str, Any] = {"model": model, "stream": stream}
    if language:
        data["language"] = language

    last_error: Exception | None = None
    for attempt in range(1, 6):
        try:
            with wav_path.open("rb") as handle:
                files = {"file": (wav_path.name, handle, "audio/wav")}
                response = requests.post(
                    API_URL,
                    headers=headers,
                    data=data,
                    files=files,
                    timeout=timeout_seconds,
                )
            payload: dict[str, Any]
            try:
                payload = response.json()
            except Exception:
                payload = {"raw_text": response.text}
            if response.ok:
                return {
                    "status_code": response.status_code,
                    "ok": response.ok,
                    "json": payload,
                    "headers": dict(response.headers),
                }
            retriable_status = {408, 409, 425, 429, 500, 502, 503, 504}
            if response.status_code not in retriable_status or attempt == 5:
                return {
                    "status_code": response.status_code,
                    "ok": response.ok,
                    "json": payload,
                    "headers": dict(response.headers),
                }
            sleep(min(8.0, float(attempt * 2)))
        except requests.RequestException as exc:
            last_error = exc
            if attempt == 5:
                break
            sleep(min(8.0, float(attempt * 2)))
    raise RuntimeError(f"Zhipu STT request failed after retries: {last_error}")


def main() -> None:
    args = parse_args()
    if not args.api_key:
        raise SystemExit("Missing --api-key or ZHIPU_API_KEY")

    input_path = Path(args.input).resolve()
    output_path = Path(args.output).resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)

    waveform = load_audio(input_path, sample_rate=args.sample_rate)
    chunks = split_audio(
        waveform=waveform,
        sample_rate=args.sample_rate,
        chunk_seconds=args.chunk_seconds,
        overlap_seconds=args.chunk_overlap_seconds,
    )
    if args.max_chunks > 0:
        chunks = chunks[: args.max_chunks]

    started = perf_counter()
    chunk_results: list[dict[str, Any]] = []
    chunk_texts: list[str] = []

    with tempfile.TemporaryDirectory(prefix="glm_asr_") as temp_dir_name:
        temp_dir = Path(temp_dir_name)
        for index, chunk in enumerate(chunks):
            wav_path = temp_dir / f"chunk_{index:04d}.wav"
            sf.write(str(wav_path), chunk["waveform"], args.sample_rate, subtype="PCM_16")
            response = transcribe_chunk(
                api_key=args.api_key,
                model=args.model,
                stream=args.stream,
                wav_path=wav_path,
                timeout_seconds=args.timeout_seconds,
                language=args.language,
            )
            text = extract_text(response.get("json") or {})
            chunk_texts.append(text)
            chunk_results.append(
                {
                    "index": index,
                    "start_ms": chunk["start_ms"],
                    "end_ms": chunk["end_ms"],
                    "text": text,
                    "response": response,
                }
            )

    elapsed = perf_counter() - started
    merged_text = "" if args.no_stitch else dedupe_join(chunk_texts)

    payload = {
        "provider": "zhipu_glm_asr",
        "model_name": args.model,
        "language": args.language,
        "text": merged_text,
        "segments": [
            {
                "text": chunk["text"],
                "start_ms": chunk["start_ms"],
                "end_ms": chunk["end_ms"],
                "speaker_id": None,
                "language": args.language,
            }
            for chunk in chunk_results
        ],
        "words": [],
        "metadata": {
            "uttid": args.uttid,
            "input_path": str(input_path),
            "elapsed_seconds": round(elapsed, 3),
            "chunk_seconds": args.chunk_seconds,
            "chunk_overlap_seconds": args.chunk_overlap_seconds,
            "chunk_count": len(chunks),
            "stream": args.stream,
            "stitched": not args.no_stitch,
            "api_url": API_URL,
            "chunks": chunk_results,
        },
    }
    output_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
