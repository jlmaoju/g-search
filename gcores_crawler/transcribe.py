from __future__ import annotations

import gc
import json
import re
import subprocess
import sys
import tempfile
import time
import wave
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
from typing import List, Optional

from .catalog import Catalog
from .daily_runtime import get_daily_runtime_reporter
from .firered_local import FireRedRunner, FireRedRuntimeConfig, convert_to_wav, save_payload
from .indexing import build_transcript_documents
from .lexicon import (
    apply_alias_map,
    extract_hotwords_from_record,
    load_alias_map,
    load_hotwords,
    merge_hotwords,
    write_hotwords,
)
from .subprocess_utils import quiet_subprocess_kwargs


def build_firered_runtime_config(
    *,
    disable_lid: bool,
    disable_punc: bool,
    asr_batch_size: int,
    punc_batch_size: int,
) -> FireRedRuntimeConfig:
    return FireRedRuntimeConfig(
        disable_lid=disable_lid,
        disable_punc=disable_punc,
        asr_batch_size=asr_batch_size,
        punc_batch_size=punc_batch_size,
        use_half=False,
        beam_size=3,
        return_timestamp=True,
    )


def create_firered_runner(
    *,
    disable_lid: bool,
    disable_punc: bool,
    asr_batch_size: int,
    punc_batch_size: int,
) -> FireRedRunner:
    return FireRedRunner(
        build_firered_runtime_config(
            disable_lid=disable_lid,
            disable_punc=disable_punc,
            asr_batch_size=asr_batch_size,
            punc_batch_size=punc_batch_size,
        )
    )


def is_recoverable_firered_error(exc: Exception | str) -> bool:
    error_text = str(exc).lower()
    return any(
        token in error_text
        for token in (
            "illegal memory access",
            "cuda error",
            "device-side assert",
            "cublas",
            "out of memory",
            "misaligned address",
        )
    )


def classify_transcript_error(error: str | None) -> str:
    lowered = str(error or "").lower().strip()
    if lowered.startswith("bad allocation"):
        return "bad_allocation"
    if "cuda error" in lowered or "illegal memory access" in lowered:
        return "cuda_error"
    if re.match(r"^[a-z]/\('.*',\s*[\d.]+,\s*[\d.]+\)$", str(error or "").strip(), flags=re.IGNORECASE):
        return "hallucinated_repeat_token"
    if "output did not contain text" in lowered or "did not contain text" in lowered:
        return "empty_output"
    return "other"


def detect_repetitive_transcript_text(text: str) -> Optional[str]:
    compact = "".join(ch for ch in text.lower() if not ch.isspace())
    if not compact:
        return "empty transcript text"
    if len(compact) >= 180:
        unique_ratio = len(set(compact)) / len(compact)
        # Long Chinese transcripts naturally converge to a fairly low unique-char ratio,
        # so only treat this as suspicious when it is extremely low.
        if unique_ratio < 0.02:
            return f"suspiciously low unique-char ratio ({unique_ratio:.3f})"
        longest_run = 1
        current_run = 1
        for previous, current in zip(compact, compact[1:]):
            if previous == current:
                current_run += 1
                longest_run = max(longest_run, current_run)
            else:
                current_run = 1
        if longest_run >= 24:
            return f"suspicious repeated-char run ({longest_run})"
    tokens = re.findall(r"[\w\u4e00-\u9fff]+", text.lower())
    if len(tokens) >= 40:
        counts: dict[str, int] = {}
        for token in tokens:
            counts[token] = counts.get(token, 0) + 1
        most_common = max(counts.values())
        if most_common / len(tokens) > 0.45:
            return f"suspicious repeated-token ratio ({most_common}/{len(tokens)})"
    return None


def build_firered_rescue_plan(job: dict) -> dict:
    category = classify_transcript_error(job.get("transcript_error"))
    duration_seconds = float(job.get("duration") or 0.0)
    if category == "bad_allocation":
        return {
            "category": category,
            "strategy": "chunked",
            "chunk_seconds": 1500,
            "chunk_overlap_seconds": 3,
            "validate_repetition": True,
            "reset_runner_before": False,
            "reset_runner_each_chunk": False,
            "priority": 0,
        }
    if category == "hallucinated_repeat_token":
        return {
            "category": category,
            "strategy": "chunked" if duration_seconds >= 1800 else "isolated",
            "chunk_seconds": 300,
            "chunk_overlap_seconds": 2,
            "validate_repetition": True,
            "reset_runner_before": False,
            "reset_runner_each_chunk": True,
            "priority": 1,
        }
    if category == "cuda_error":
        return {
            "category": category,
            "strategy": "isolated",
            "chunk_seconds": 0,
            "chunk_overlap_seconds": 0,
            "validate_repetition": True,
            "reset_runner_before": True,
            "reset_runner_each_chunk": False,
            "priority": 2,
        }
    if duration_seconds >= 3600:
        # Once a rescue attempt fails, transcript_error may be overwritten by the
        # rescue failure message. Keep long-form audio on the safer chunked path.
        return {
            "category": category,
            "strategy": "chunked",
            "chunk_seconds": 1500,
            "chunk_overlap_seconds": 3,
            "validate_repetition": True,
            "reset_runner_before": False,
            "reset_runner_each_chunk": False,
            "priority": 1,
        }
    return {
        "category": category,
        "strategy": "isolated",
        "chunk_seconds": 0,
        "chunk_overlap_seconds": 0,
        "validate_repetition": True,
        "reset_runner_before": False,
        "reset_runner_each_chunk": False,
        "priority": 3,
    }


def split_pcm_wav(
    *,
    source_wav_path: Path,
    output_dir: Path,
    chunk_seconds: int,
    overlap_seconds: int,
) -> List[dict]:
    output_dir.mkdir(parents=True, exist_ok=True)
    chunks: List[dict] = []
    with wave.open(str(source_wav_path), "rb") as reader:
        params = reader.getparams()
        framerate = reader.getframerate()
        total_frames = reader.getnframes()
        chunk_frames = max(1, int(chunk_seconds * framerate))
        overlap_frames = max(0, int(overlap_seconds * framerate))
        start_frame = 0
        chunk_index = 0
        while start_frame < total_frames:
            end_frame = min(total_frames, start_frame + chunk_frames)
            reader.setpos(start_frame)
            frames = reader.readframes(end_frame - start_frame)
            chunk_path = output_dir / f"{source_wav_path.stem}__chunk{chunk_index:03d}.wav"
            with wave.open(str(chunk_path), "wb") as writer:
                writer.setparams(params)
                writer.writeframes(frames)
            chunks.append(
                {
                    "path": chunk_path,
                    "index": chunk_index,
                    "start_ms": int(round(start_frame * 1000 / framerate)),
                    "end_ms": int(round(end_frame * 1000 / framerate)),
                }
            )
            if end_frame >= total_frames:
                break
            next_start = end_frame - overlap_frames
            if next_start <= start_frame:
                next_start = end_frame
            start_frame = next_start
            chunk_index += 1
    return chunks


def merge_chunk_payloads(chunk_payloads: List[tuple[dict, int]]) -> dict:
    merged_segments: List[dict] = []
    merged_words: List[dict] = []
    text_parts: List[str] = []
    model_name = None
    language = None
    for payload, offset_ms in chunk_payloads:
        if not model_name:
            model_name = payload.get("model_name")
        if not language:
            language = payload.get("language")
        chunk_text = str(payload.get("text") or "").strip()
        if chunk_text:
            text_parts.append(chunk_text)
        for segment in payload.get("segments", []) or []:
            merged_segment = dict(segment)
            if merged_segment.get("start_ms") is not None:
                merged_segment["start_ms"] = int(merged_segment["start_ms"]) + offset_ms
            if merged_segment.get("end_ms") is not None:
                merged_segment["end_ms"] = int(merged_segment["end_ms"]) + offset_ms
            merged_segments.append(merged_segment)
        for word in payload.get("words", []) or []:
            merged_word = dict(word)
            if merged_word.get("start_ms") is not None:
                merged_word["start_ms"] = int(merged_word["start_ms"]) + offset_ms
            if merged_word.get("end_ms") is not None:
                merged_word["end_ms"] = int(merged_word["end_ms"]) + offset_ms
            merged_words.append(merged_word)
    return {
        "text": "\n".join(part for part in text_parts if part),
        "segments": merged_segments,
        "words": merged_words,
        "model_name": model_name or "FireRedASR2-AED",
        "language": language or "zh",
        "metadata": {
            "chunk_count": len(chunk_payloads),
        },
    }


def rebuild_firered_runner(
    *,
    current_runner: Optional[FireRedRunner],
    disable_lid: bool,
    disable_punc: bool,
    asr_batch_size: int,
    punc_batch_size: int,
    verbose: bool = False,
) -> FireRedRunner:
    if verbose:
        print("[firered] resetting runner after CUDA/runtime error")
    try:
        del current_runner
    except Exception:  # noqa: BLE001
        pass
    gc.collect()
    try:
        import torch

        if torch.cuda.is_available():
            try:
                torch.cuda.synchronize()
            except Exception:  # noqa: BLE001
                pass
            try:
                torch.cuda.empty_cache()
            except Exception:  # noqa: BLE001
                pass
            try:
                torch.cuda.ipc_collect()
            except Exception:  # noqa: BLE001
                pass
    except Exception:  # noqa: BLE001
        pass
    runner = create_firered_runner(
        disable_lid=disable_lid,
        disable_punc=disable_punc,
        asr_batch_size=asr_batch_size,
        punc_batch_size=punc_batch_size,
    )
    if verbose:
        print("[firered] models ready")
    return runner


def transcribe_media_jobs(
    root: str = "data",
    *,
    script: str,
    python_executable: Optional[str] = None,
    script_args: Optional[List[str]] = None,
    limit: int = 10,
    max_chars: int = 800,
    job_ids: Optional[List[str]] = None,
    verbose: bool = False,
) -> dict:
    root_path = Path(root)
    catalog = Catalog(str(root_path))
    jobs = catalog.get_transcription_jobs(limit=limit, job_ids=job_ids, include_errors=bool(job_ids))

    summary = {
        "status": "transcribed",
        "root": str(root_path),
        "limit": limit,
        "script": script,
        "python_executable": python_executable or sys.executable,
        "seen": len(jobs),
        "completed": [],
        "errors": [],
    }

    for job in jobs:
        try:
            if verbose:
                print(
                    f"[transcribe] start job={job['job_id']} duration={job.get('duration')} "
                    f"title={job.get('media_title')}"
                )
            result = transcribe_media_job(
                root_path=root_path,
                job=job,
                script=script,
                python_executable=python_executable,
                script_args=script_args or [],
                max_chars=max_chars,
            )
            catalog.save_transcript(
                job=job,
                transcript_path=result["transcript_path"],
                transcript_text=result["text"],
                transcript_segments=result["segments"],
                model_name=result["model_name"],
                language=result["language"],
                documents=result["documents"],
            )
            summary["completed"].append(
                {
                    "job_id": job["job_id"],
                    "transcript_path": result["transcript_path"],
                    "model_name": result["model_name"],
                    "language": result["language"],
                }
            )
            if verbose:
                print(
                    f"[transcribe] ok job={job['job_id']} model={result['model_name']} "
                    f"path={result['transcript_path']}"
                )
        except Exception as exc:  # noqa: BLE001
            catalog.mark_transcript_error(job_id=str(job["job_id"]), error=str(exc))
            summary["errors"].append({"job_id": job["job_id"], "error": str(exc)})
            if verbose:
                print(f"[transcribe] error job={job['job_id']} error={exc}")

    summary["catalog"] = catalog.stats()
    return summary


def transcribe_media_loop(
    root: str = "data",
    *,
    script: str,
    python_executable: Optional[str] = None,
    script_args: Optional[List[str]] = None,
    batch_size: int = 1,
    max_chars: int = 800,
    sleep_seconds: float = 5.0,
    max_idle_cycles: int = 3,
    job_ids: Optional[List[str]] = None,
    verbose: bool = False,
) -> dict:
    root_path = Path(root)
    catalog = Catalog(str(root_path))
    aggregate = {
        "status": "completed",
        "root": str(root_path),
        "script": script,
        "python_executable": python_executable or sys.executable,
        "batch_size": batch_size,
        "sleep_seconds": sleep_seconds,
        "max_idle_cycles": max_idle_cycles,
        "cycles": 0,
        "idle_cycles": 0,
        "seen": 0,
        "completed": 0,
        "errors": 0,
    }

    try:
        write_runtime_state(
            catalog,
            "runtime:transcribe_loop",
            {
                **aggregate,
                "status": "running",
            },
        )
        while True:
            summary = transcribe_media_jobs(
                root=root,
                script=script,
                python_executable=python_executable,
                script_args=script_args,
                limit=batch_size,
                max_chars=max_chars,
                job_ids=job_ids,
                verbose=verbose,
            )
            aggregate["cycles"] += 1
            aggregate["seen"] += int(summary.get("seen", 0))
            aggregate["completed"] += len(summary.get("completed", []))
            aggregate["errors"] += len(summary.get("errors", []))
            aggregate["catalog"] = summary.get("catalog")

            if not summary.get("seen"):
                aggregate["idle_cycles"] += 1
                if max_idle_cycles > 0 and aggregate["idle_cycles"] >= max_idle_cycles:
                    break
            else:
                aggregate["idle_cycles"] = 0

            if verbose:
                print(
                    f"[transcribe-loop] cycle={aggregate['cycles']} "
                    f"seen={summary.get('seen', 0)} completed={len(summary.get('completed', []))} "
                    f"errors={len(summary.get('errors', []))} idle={aggregate['idle_cycles']}"
                )
            write_runtime_state(
                catalog,
                "runtime:transcribe_loop",
                {
                    **aggregate,
                    "status": "running",
                },
            )
            if not summary.get("seen") and sleep_seconds > 0:
                time.sleep(sleep_seconds)
    except KeyboardInterrupt:
        aggregate["status"] = "paused"
        aggregate["catalog"] = catalog.stats()
        write_runtime_state(
            catalog,
            "runtime:transcribe_loop",
            {
                **aggregate,
                "status": "paused",
            },
        )
        return aggregate

    write_runtime_state(
        catalog,
        "runtime:transcribe_loop",
        {
            **aggregate,
            "status": "completed",
        },
    )
    return aggregate


def transcribe_firered_jobs(
    root: str = "data",
    *,
    limit: int = 10,
    max_chars: int = 800,
    job_ids: Optional[List[str]] = None,
    verbose: bool = False,
    disable_lid: bool = True,
    disable_punc: bool = False,
    asr_batch_size: int = 4,
    punc_batch_size: int = 8,
    runner: Optional[FireRedRunner] = None,
) -> dict:
    root_path = Path(root)
    catalog = Catalog(str(root_path))
    jobs = catalog.get_transcription_jobs(limit=limit, job_ids=job_ids, include_errors=bool(job_ids))
    if runner is None and verbose:
        print(
            "[firered] loading models "
            f"disable_lid={disable_lid} disable_punc={disable_punc} "
            f"asr_batch_size={asr_batch_size} punc_batch_size={punc_batch_size}"
        )
    firered_runner = runner or create_firered_runner(
        disable_lid=disable_lid,
        disable_punc=disable_punc,
        asr_batch_size=asr_batch_size,
        punc_batch_size=punc_batch_size,
    )
    if runner is None and verbose:
        print("[firered] models ready")

    summary = {
        "status": "transcribed",
        "root": str(root_path),
        "limit": limit,
        "seen": len(jobs),
        "completed": [],
        "errors": [],
        "config": {
            "disable_lid": disable_lid,
            "disable_punc": disable_punc,
            "asr_batch_size": asr_batch_size,
            "punc_batch_size": punc_batch_size,
        },
    }

    for job in jobs:
        prepared = None
        try:
            prepared = prepare_firered_job(root_path=root_path, job=job, verbose=verbose, label="inline")

            if verbose:
                print(
                    f"[firered] start job={job['job_id']} duration={job.get('duration')} "
                    f"title={job.get('media_title')}"
                )
            result = transcribe_prepared_firered_job(
                root_path=root_path,
                catalog=catalog,
                firered_runner=firered_runner,
                job=job,
                prepared=prepared,
                max_chars=max_chars,
            )
            summary["completed"].append(result)
            if verbose:
                print(
                    f"[firered] ok job={job['job_id']} model={result['model_name']} "
                    f"path={result['transcript_path']}"
                )
        except Exception as exc:  # noqa: BLE001
            catalog.mark_transcript_error(job_id=str(job["job_id"]), error=str(exc))
            summary["errors"].append({"job_id": job["job_id"], "error": str(exc)})
            if verbose:
                print(f"[firered] error job={job['job_id']} error={exc}")
            if is_recoverable_firered_error(exc):
                firered_runner = rebuild_firered_runner(
                    current_runner=firered_runner,
                    disable_lid=disable_lid,
                    disable_punc=disable_punc,
                    asr_batch_size=asr_batch_size,
                    punc_batch_size=punc_batch_size,
                    verbose=verbose,
                )
        finally:
            cleanup_prepared_wav(prepared)

    summary["catalog"] = catalog.stats()
    return summary


def transcribe_firered_loop(
    root: str = "data",
    *,
    batch_size: int = 1,
    max_chars: int = 800,
    sleep_seconds: float = 5.0,
    max_idle_cycles: int = 3,
    job_ids: Optional[List[str]] = None,
    verbose: bool = False,
    disable_lid: bool = True,
    disable_punc: bool = False,
    asr_batch_size: int = 4,
    punc_batch_size: int = 8,
) -> dict:
    root_path = Path(root)
    catalog = Catalog(str(root_path))
    reporter = get_daily_runtime_reporter()
    if verbose:
        print(
            "[firered] loading models "
            f"disable_lid={disable_lid} disable_punc={disable_punc} "
            f"asr_batch_size={asr_batch_size} punc_batch_size={punc_batch_size}"
        )
    runner = create_firered_runner(
        disable_lid=disable_lid,
        disable_punc=disable_punc,
        asr_batch_size=asr_batch_size,
        punc_batch_size=punc_batch_size,
    )
    if verbose:
        print("[firered] models ready")
    target_total = len(job_ids) if job_ids else None
    aggregate = {
        "status": "completed",
        "root": str(root_path),
        "batch_size": batch_size,
        "sleep_seconds": sleep_seconds,
        "max_idle_cycles": max_idle_cycles,
        "cycles": 0,
        "idle_cycles": 0,
        "seen": 0,
        "completed": 0,
        "errors": 0,
        "config": {
            "disable_lid": disable_lid,
            "disable_punc": disable_punc,
            "asr_batch_size": asr_batch_size,
            "punc_batch_size": punc_batch_size,
        },
    }

    prefetched: Optional[dict] = None
    prefetch_future: Optional[Future] = None
    prefetched_job_id: Optional[str] = None
    prefetch_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="firered-prep")

    try:
        write_runtime_state(
            catalog,
            "runtime:transcribe_loop",
            {
                **aggregate,
                "status": "running",
            },
        )
        reporter.update(
            status="running",
            current=0,
            total=target_total,
            unit="jobs",
            message="starting firered loop",
        )
        while True:
            summary = {
                "seen": 0,
                "completed": [],
                "errors": [],
                "catalog": None,
            }
            processed_in_cycle = 0

            while processed_in_cycle < max(1, batch_size):
                if prefetched is None:
                    if prefetch_future is not None:
                        try:
                            prefetched = prefetch_future.result()
                        except Exception as exc:  # noqa: BLE001
                            if prefetched_job_id:
                                catalog.mark_transcript_error(job_id=prefetched_job_id, error=str(exc))
                                summary["seen"] += 1
                                summary["errors"].append({"job_id": prefetched_job_id, "error": str(exc)})
                                if verbose:
                                    print(f"[firered] error job={prefetched_job_id} error={exc}")
                            prefetched = None
                        finally:
                            prefetch_future = None
                            prefetched_job_id = None

                if prefetched is None:
                    jobs = catalog.get_transcription_jobs(
                        limit=1,
                        job_ids=job_ids,
                        include_errors=bool(job_ids),
                    )
                    if not jobs:
                        break
                    current_job = jobs[0]
                    try:
                        prefetched = prepare_firered_job(
                            root_path=root_path,
                            job=current_job,
                            verbose=verbose,
                            label="current",
                        )
                    except Exception as exc:  # noqa: BLE001
                        catalog.mark_transcript_error(job_id=str(current_job["job_id"]), error=str(exc))
                        summary["seen"] += 1
                        summary["errors"].append({"job_id": current_job["job_id"], "error": str(exc)})
                        if verbose:
                            print(f"[firered] error job={current_job['job_id']} error={exc}")
                        processed_in_cycle += 1
                        continue

                current_job = prefetched["job"]
                if prefetch_future is None:
                    lookahead_jobs = catalog.get_transcription_jobs(
                        limit=2,
                        job_ids=job_ids,
                        include_errors=bool(job_ids),
                    )
                    next_job = next(
                        (job for job in lookahead_jobs if str(job["job_id"]) != str(current_job["job_id"])),
                        None,
                    )
                    if next_job is not None:
                        if verbose:
                            print(f"[firered] prefetch job={next_job['job_id']}")
                        prefetch_future = prefetch_executor.submit(
                            prepare_firered_job,
                            root_path=root_path,
                            job=next_job,
                            verbose=verbose,
                            label="prefetch",
                        )
                        prefetched_job_id = str(next_job["job_id"])

                try:
                    if verbose:
                        print(
                            f"[firered] start job={current_job['job_id']} duration={current_job.get('duration')} "
                            f"title={current_job.get('media_title')}"
                        )
                    result = transcribe_prepared_firered_job(
                        root_path=root_path,
                        catalog=catalog,
                        firered_runner=runner,
                        job=current_job,
                        prepared=prefetched,
                        max_chars=max_chars,
                    )
                    summary["completed"].append(result)
                    if verbose:
                        print(
                            f"[firered] ok job={current_job['job_id']} model={result['model_name']} "
                            f"path={result['transcript_path']}"
                        )
                except Exception as exc:  # noqa: BLE001
                    catalog.mark_transcript_error(job_id=str(current_job["job_id"]), error=str(exc))
                    summary["errors"].append({"job_id": current_job["job_id"], "error": str(exc)})
                    if verbose:
                        print(f"[firered] error job={current_job['job_id']} error={exc}")
                    if is_recoverable_firered_error(exc):
                        runner = rebuild_firered_runner(
                            current_runner=runner,
                            disable_lid=disable_lid,
                            disable_punc=disable_punc,
                            asr_batch_size=asr_batch_size,
                            punc_batch_size=punc_batch_size,
                            verbose=verbose,
                        )
                finally:
                    summary["seen"] += 1
                    processed_in_cycle += 1
                    cleanup_prepared_wav(prefetched)
                    prefetched = None
            summary["catalog"] = catalog.stats()
            aggregate["cycles"] += 1
            aggregate["seen"] += int(summary.get("seen", 0))
            aggregate["completed"] += len(summary.get("completed", []))
            aggregate["errors"] += len(summary.get("errors", []))
            aggregate["catalog"] = summary.get("catalog")

            if not summary.get("seen"):
                aggregate["idle_cycles"] += 1
                if max_idle_cycles > 0 and aggregate["idle_cycles"] >= max_idle_cycles:
                    break
            else:
                aggregate["idle_cycles"] = 0

            if verbose:
                print(
                    f"[firered-loop] cycle={aggregate['cycles']} "
                    f"seen={summary.get('seen', 0)} completed={len(summary.get('completed', []))} "
                    f"errors={len(summary.get('errors', []))} idle={aggregate['idle_cycles']}"
                )
            write_runtime_state(
                catalog,
                "runtime:transcribe_loop",
                {
                    **aggregate,
                    "status": "running",
                },
            )
            reporter.update(
                status="running",
                current=aggregate["completed"] + aggregate["errors"],
                total=target_total,
                unit="jobs",
                message=(
                    f"completed={aggregate['completed']} errors={aggregate['errors']} "
                    f"idle={aggregate['idle_cycles']}"
                ),
                extra={
                    "completed": aggregate["completed"],
                    "errors": aggregate["errors"],
                    "seen": aggregate["seen"],
                    "idle_cycles": aggregate["idle_cycles"],
                },
            )
            if not summary.get("seen") and sleep_seconds > 0:
                time.sleep(sleep_seconds)
    except KeyboardInterrupt:
        aggregate["status"] = "paused"
        aggregate["catalog"] = catalog.stats()
        write_runtime_state(
            catalog,
            "runtime:transcribe_loop",
            {
                **aggregate,
                "status": "paused",
            },
        )
        reporter.update(status="paused", message="firered loop paused")
        return aggregate
    finally:
        cleanup_prepared_wav(prefetched)
        if prefetch_future is not None:
            try:
                prefetch_future.cancel()
            except Exception:  # noqa: BLE001
                pass
        prefetch_executor.shutdown(wait=False)

    write_runtime_state(
        catalog,
        "runtime:transcribe_loop",
        {
            **aggregate,
            "status": "completed",
        },
    )
    reporter.complete(
        message=(
            f"completed={aggregate['completed']} errors={aggregate['errors']} "
            f"seen={aggregate['seen']}"
        ),
        extra={
            "completed": aggregate["completed"],
            "errors": aggregate["errors"],
            "seen": aggregate["seen"],
        },
    )
    return aggregate


def submit_remote_transcription_jobs(
    root: str = "data",
    *,
    script: str,
    python_executable: Optional[str] = None,
    script_args: Optional[List[str]] = None,
    limit: int = 10,
    provider: str = "volcengine_idle",
    job_ids: Optional[List[str]] = None,
    global_hotword_file: Optional[str] = None,
    hotword_limit: int = 128,
) -> dict:
    root_path = Path(root)
    catalog = Catalog(str(root_path))
    jobs = catalog.get_remote_transcription_jobs(
        limit=limit,
        job_ids=job_ids,
        include_errors=bool(job_ids),
    )
    summary = {
        "status": "submitted",
        "provider": provider,
        "root": str(root_path),
        "limit": limit,
        "script": script,
        "python_executable": python_executable or sys.executable,
        "seen": len(jobs),
        "submitted": [],
        "errors": [],
    }

    for job in jobs:
        try:
            media_url = str(job.get("media_url") or "").strip()
            if not media_url:
                raise ValueError("Media job does not have a public media_url")

            state_relpath = build_remote_state_relpath(job=job, provider=provider)
            state_path = root_path / state_relpath
            state_path.parent.mkdir(parents=True, exist_ok=True)
            hotword_file_path = build_remote_hotword_path(job=job, provider=provider)
            merged_hotwords = build_job_hotwords(
                root_path=root_path,
                job=job,
                global_hotword_file=global_hotword_file,
                limit=hotword_limit,
            )
            if merged_hotwords:
                write_hotwords(root_path / hotword_file_path, merged_hotwords)

            command = [
                python_executable or sys.executable,
                script,
                *script_args_or_default(script_args),
                "--mode",
                "idle",
                "--action",
                "submit",
                "--input-url",
                media_url,
                "--output",
                str(state_path),
            ]
            if merged_hotwords:
                command.extend(["--hotword-file", str(root_path / hotword_file_path)])
            completed = subprocess.run(
                command,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
                **quiet_subprocess_kwargs(),
            )
            if completed.returncode != 0:
                stderr = completed.stderr.strip() or completed.stdout.strip()
                raise RuntimeError(f"Remote submit failed: {stderr}")

            payload = json.loads(state_path.read_text(encoding="utf-8"))
            if not payload.get("ok"):
                raise RuntimeError(payload_error_message(payload, default="Remote submit failed"))

            catalog.mark_transcript_submitted(job_id=str(job["job_id"]))
            catalog.set_state(
                remote_state_key(str(job["job_id"])),
                {
                    "provider": provider,
                    "request_id": payload.get("request_id"),
                    "resource_id": payload.get("resource_id"),
                    "input_url": media_url,
                    "state_relpath": state_relpath.as_posix(),
                    "hotword_relpath": hotword_file_path.as_posix() if merged_hotwords else None,
                    "hotword_count": len(merged_hotwords),
                },
            )
            summary["submitted"].append(
                {
                    "job_id": job["job_id"],
                    "request_id": payload.get("request_id"),
                    "resource_id": payload.get("resource_id"),
                    "hotword_count": len(merged_hotwords),
                }
            )
        except Exception as exc:  # noqa: BLE001
            catalog.mark_transcript_error(job_id=str(job["job_id"]), error=str(exc))
            summary["errors"].append({"job_id": job["job_id"], "error": str(exc)})

    summary["catalog"] = catalog.stats()
    return summary


def poll_remote_transcription_jobs(
    root: str = "data",
    *,
    script: str,
    python_executable: Optional[str] = None,
    script_args: Optional[List[str]] = None,
    limit: int = 10,
    max_chars: int = 800,
    provider: str = "volcengine_idle",
    job_ids: Optional[List[str]] = None,
    alias_map_path: Optional[str] = None,
) -> dict:
    root_path = Path(root)
    catalog = Catalog(str(root_path))
    jobs = catalog.get_submitted_transcription_jobs(limit=limit, job_ids=job_ids)
    summary = {
        "status": "polled",
        "provider": provider,
        "root": str(root_path),
        "limit": limit,
        "script": script,
        "python_executable": python_executable or sys.executable,
        "seen": len(jobs),
        "completed": [],
        "processing": [],
        "errors": [],
    }

    for job in jobs:
        try:
            state = catalog.get_state(remote_state_key(str(job["job_id"])))
            if not state:
                raise RuntimeError("Missing remote transcription state for submitted job")

            state_relpath = Path(str(state["state_relpath"]))
            state_path = root_path / state_relpath
            state_path.parent.mkdir(parents=True, exist_ok=True)

            command = [
                python_executable or sys.executable,
                script,
                *script_args_or_default(script_args),
                "--mode",
                "idle",
                "--action",
                "query",
                "--request-id",
                str(state["request_id"]),
                "--output",
                str(state_path),
            ]
            completed = subprocess.run(
                command,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
                **quiet_subprocess_kwargs(),
            )
            if completed.returncode != 0:
                stderr = completed.stderr.strip() or completed.stdout.strip()
                raise RuntimeError(f"Remote query failed: {stderr}")

            payload = json.loads(state_path.read_text(encoding="utf-8"))
            if payload.get("state") == "processing":
                summary["processing"].append(
                    {
                        "job_id": job["job_id"],
                        "request_id": state.get("request_id"),
                    }
                )
                continue
            if not payload.get("ok"):
                raise RuntimeError(payload_error_message(payload, default="Remote query failed"))

            transcript_relpath = build_transcript_relpath(job)
            transcript_path = root_path / transcript_relpath
            transcript_path.parent.mkdir(parents=True, exist_ok=True)
            transcript_payload = {
                "text": payload.get("text", ""),
                "segments": payload.get("segments", []),
                "model_name": payload.get("model_name"),
                "language": payload.get("language"),
            }
            alias_map = load_alias_map(alias_map_path or (root_path / "lexicon" / "alias_map.json"))
            normalized_payload, alias_info = apply_alias_map(transcript_payload, alias_map)
            transcript_path.write_text(
                json.dumps(normalized_payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            result = finalize_transcript(
                root_path=root_path,
                job=job,
                transcript_relpath=transcript_relpath,
                transcript_path=transcript_path,
                max_chars=max_chars,
            )
            catalog.save_transcript(
                job=job,
                transcript_path=result["transcript_path"],
                transcript_text=result["text"],
                transcript_segments=result["segments"],
                model_name=result["model_name"],
                language=result["language"],
                documents=result["documents"],
            )
            summary["completed"].append(
                {
                    "job_id": job["job_id"],
                    "request_id": state.get("request_id"),
                    "transcript_path": result["transcript_path"],
                    "model_name": result["model_name"],
                    "alias_replacements": alias_info.get("replacements", {}),
                }
            )
        except Exception as exc:  # noqa: BLE001
            catalog.mark_transcript_error(job_id=str(job["job_id"]), error=str(exc))
            summary["errors"].append({"job_id": job["job_id"], "error": str(exc)})

    summary["catalog"] = catalog.stats()
    return summary


def transcribe_media_job(
    *,
    root_path: Path,
    job: dict,
    script: str,
    python_executable: Optional[str],
    script_args: List[str],
    max_chars: int,
) -> dict:
    downloaded_path = job.get("downloaded_path") or job.get("local_relpath")
    if not downloaded_path:
        raise FileNotFoundError("Media job does not have a downloaded path")

    input_path = root_path / str(downloaded_path)
    if not input_path.exists():
        raise FileNotFoundError(f"Downloaded media file is missing: {input_path}")

    transcript_relpath = (
        Path("transcripts")
        / str(job["content_type"])
        / str(job["item_id"])
        / f"{safe_filename(str(job['job_id']))}.json"
    )
    transcript_path = root_path / transcript_relpath
    transcript_path.parent.mkdir(parents=True, exist_ok=True)

    command = [
        python_executable or sys.executable,
        script,
        *script_args,
        "--input",
        str(input_path),
        "--output",
        str(transcript_path),
    ]
    completed = subprocess.run(
        command,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
        **quiet_subprocess_kwargs(),
    )
    if completed.returncode != 0:
        stderr = completed.stderr.strip() or completed.stdout.strip()
        raise RuntimeError(f"Transcription script failed: {stderr}")

    payload = load_transcript_payload(transcript_path)
    return build_transcript_result(
        job=job,
        transcript_relpath=transcript_relpath,
        payload=payload,
        max_chars=max_chars,
    )


def finalize_transcript(
    *,
    root_path: Path,
    job: dict,
    transcript_relpath: Path,
    transcript_path: Path,
    max_chars: int,
) -> dict:
    payload = load_transcript_payload(transcript_path)
    return build_transcript_result(
        job=job,
        transcript_relpath=transcript_relpath,
        payload=payload,
        max_chars=max_chars,
    )


def build_transcript_result(
    *,
    job: dict,
    transcript_relpath: Path,
    payload: dict,
    max_chars: int,
) -> dict:
    transcript_text = payload["text"].strip()
    if not transcript_text:
        raise ValueError("Transcript output did not contain text")

    documents = build_transcript_documents(
        job=job,
        transcript_text=transcript_text,
        max_chars=max_chars,
        model_name=payload.get("model_name"),
        language=payload.get("language"),
    )
    return {
        "job_id": job["job_id"],
        "transcript_path": transcript_relpath.as_posix(),
        "text": transcript_text,
        "segments": payload.get("segments", []),
        "model_name": payload.get("model_name"),
        "language": payload.get("language"),
        "documents": documents,
    }


def load_transcript_payload(path: Path) -> dict:
    raw_text = path.read_text(encoding="utf-8-sig").strip()
    if not raw_text:
        raise ValueError(f"Transcript file is empty: {path}")

    try:
        parsed = json.loads(raw_text)
    except json.JSONDecodeError:
        return {
            "text": raw_text,
            "segments": [],
            "model_name": None,
            "language": None,
        }

    if isinstance(parsed, str):
        return {
            "text": parsed,
            "segments": [],
            "model_name": None,
            "language": None,
        }

    if not isinstance(parsed, dict):
        raise ValueError(f"Transcript JSON must be an object or string: {path}")

    segments = parsed.get("segments") or []
    text = parsed.get("text") or " ".join(
        str(segment.get("text", "")).strip()
        for segment in segments
        if str(segment.get("text", "")).strip()
    )
    return {
        "text": text,
        "segments": segments,
        "model_name": parsed.get("model_name") or parsed.get("model"),
        "language": parsed.get("language"),
    }


def safe_filename(value: str) -> str:
    return (
        value.replace(":", "__")
        .replace("/", "_")
        .replace("\\", "_")
        .replace(" ", "_")
    )


def script_args_or_default(script_args: Optional[List[str]]) -> List[str]:
    return list(script_args or [])


def build_remote_state_relpath(*, job: dict, provider: str) -> Path:
    return (
        Path("runs")
        / "remote_transcripts"
        / provider
        / str(job["content_type"])
        / str(job["item_id"])
        / f"{safe_filename(str(job['job_id']))}.json"
    )


def build_transcript_relpath(job: dict) -> Path:
    return (
        Path("transcripts")
        / str(job["content_type"])
        / str(job["item_id"])
        / f"{safe_filename(str(job['job_id']))}.json"
    )


def build_firered_wav_relpath(job: dict) -> Path:
    return (
        Path("tmp")
        / "firered_wav"
        / str(job["content_type"])
        / str(job["item_id"])
        / f"{safe_filename(str(job['job_id']))}.wav"
    )


def resolve_downloaded_media_path(*, root_path: Path, job: dict) -> Path:
    downloaded_path = job.get("downloaded_path") or job.get("local_relpath")
    if not downloaded_path:
        raise FileNotFoundError("Media job does not have a downloaded path")
    input_path = root_path / str(downloaded_path)
    if not input_path.exists():
        raise FileNotFoundError(f"Downloaded media file is missing: {input_path}")
    return input_path


def prepare_firered_job(*, root_path: Path, job: dict, verbose: bool = False, label: str = "current") -> dict:
    input_path = resolve_downloaded_media_path(root_path=root_path, job=job)
    wav_relpath = build_firered_wav_relpath(job)
    wav_path = root_path / wav_relpath
    source_mtime = input_path.stat().st_mtime
    if not wav_path.exists() or wav_path.stat().st_size <= 0 or wav_path.stat().st_mtime < source_mtime:
        if verbose:
            print(f"[firered] {label}-convert job={job['job_id']} -> {wav_relpath.as_posix()}")
        convert_to_wav(input_path=input_path, wav_path=wav_path)
    elif verbose:
        print(f"[firered] {label}-reuse job={job['job_id']} -> {wav_relpath.as_posix()}")
    transcript_relpath = build_transcript_relpath(job)
    return {
        "job": job,
        "input_path": input_path,
        "wav_path": wav_path,
        "wav_relpath": wav_relpath,
        "transcript_relpath": transcript_relpath,
    }


def cleanup_prepared_wav(prepared: Optional[dict]) -> None:
    if not prepared:
        return
    wav_path = prepared.get("wav_path")
    if not wav_path:
        return
    try:
        Path(wav_path).unlink(missing_ok=True)
    except Exception:  # noqa: BLE001
        pass


def transcribe_prepared_firered_job(
    *,
    root_path: Path,
    catalog: Catalog,
    firered_runner: FireRedRunner,
    job: dict,
    prepared: dict,
    max_chars: int,
) -> dict:
    transcript_relpath = prepared["transcript_relpath"]
    transcript_path = root_path / transcript_relpath
    payload = firered_runner.transcribe_wav(
        Path(prepared["wav_path"]),
        uttid=safe_filename(str(job["job_id"])),
        input_path=Path(prepared["input_path"]),
    )
    save_payload(transcript_path, payload)
    result = build_transcript_result(
        job=job,
        transcript_relpath=transcript_relpath,
        payload=payload,
        max_chars=max_chars,
    )
    catalog.save_transcript(
        job=job,
        transcript_path=result["transcript_path"],
        transcript_text=result["text"],
        transcript_segments=result["segments"],
        model_name=result["model_name"],
        language=result["language"],
        documents=result["documents"],
    )
    return result


def transcribe_prepared_firered_job_chunked(
    *,
    root_path: Path,
    catalog: Catalog,
    firered_runner: FireRedRunner,
    job: dict,
    prepared: dict,
    max_chars: int,
    chunk_seconds: int,
    chunk_overlap_seconds: int,
    disable_lid: bool,
    disable_punc: bool,
    asr_batch_size: int,
    punc_batch_size: int,
    verbose: bool = False,
    reset_runner_each_chunk: bool = False,
) -> tuple[dict, FireRedRunner]:
    transcript_relpath = prepared["transcript_relpath"]
    transcript_path = root_path / transcript_relpath
    active_runner = firered_runner
    with tempfile.TemporaryDirectory(prefix="firered_chunked_") as temp_dir:
        chunk_dir = Path(temp_dir)
        chunks = split_pcm_wav(
            source_wav_path=Path(prepared["wav_path"]),
            output_dir=chunk_dir,
            chunk_seconds=chunk_seconds,
            overlap_seconds=chunk_overlap_seconds,
        )
        chunk_payloads: List[tuple[dict, int]] = []
        for chunk_index, chunk in enumerate(chunks):
            if reset_runner_each_chunk and chunk_index > 0:
                active_runner = rebuild_firered_runner(
                    current_runner=active_runner,
                    disable_lid=disable_lid,
                    disable_punc=disable_punc,
                    asr_batch_size=asr_batch_size,
                    punc_batch_size=punc_batch_size,
                    verbose=verbose,
                )
            payload = active_runner.transcribe_wav(
                Path(chunk["path"]),
                uttid=f"{safe_filename(str(job['job_id']))}__chunk{chunk['index']:03d}",
                input_path=Path(prepared["input_path"]),
            )
            chunk_payloads.append((payload, int(chunk["start_ms"])))
        payload = merge_chunk_payloads(chunk_payloads)
    save_payload(transcript_path, payload)
    result = build_transcript_result(
        job=job,
        transcript_relpath=transcript_relpath,
        payload=payload,
        max_chars=max_chars,
    )
    catalog.save_transcript(
        job=job,
        transcript_path=result["transcript_path"],
        transcript_text=result["text"],
        transcript_segments=result["segments"],
        model_name=result["model_name"],
        language=result["language"],
        documents=result["documents"],
    )
    return result, active_runner


def sort_firered_rescue_jobs(jobs: List[dict]) -> List[dict]:
    return sorted(
        jobs,
        key=lambda job: (
            build_firered_rescue_plan(job)["priority"],
            -(float(job.get("duration") or 0.0)),
            str(job.get("updated_at") or ""),
            str(job["job_id"]),
        ),
    )


def get_firered_rescue_jobs(
    *,
    catalog: Catalog,
    limit: int,
    job_ids: Optional[List[str]] = None,
    categories: Optional[List[str]] = None,
) -> List[dict]:
    fetch_limit = max(limit * 4, limit)
    if categories and not job_ids:
        # Category-specific rescue loops need a much wider scan window; otherwise a small
        # initial slice can miss matching jobs entirely and the loop will idle forever.
        fetch_limit = max(fetch_limit, 1000)
    jobs = catalog.get_failed_transcription_jobs(limit=fetch_limit, job_ids=job_ids)
    if categories:
        allowed = {category.strip() for category in categories if category.strip()}
        jobs = [job for job in jobs if build_firered_rescue_plan(job)["category"] in allowed]
    jobs = sort_firered_rescue_jobs(jobs)
    return jobs[:limit]


def transcribe_firered_rescue_jobs(
    root: str = "data",
    *,
    limit: int = 10,
    max_chars: int = 800,
    job_ids: Optional[List[str]] = None,
    categories: Optional[List[str]] = None,
    verbose: bool = False,
    disable_lid: bool = True,
    disable_punc: bool = False,
    asr_batch_size: int = 4,
    punc_batch_size: int = 8,
    runner: Optional[FireRedRunner] = None,
) -> dict:
    root_path = Path(root)
    catalog = Catalog(str(root_path))
    jobs = get_firered_rescue_jobs(
        catalog=catalog,
        limit=limit,
        job_ids=job_ids,
        categories=categories,
    )
    if runner is None and verbose:
        print(
            "[firered-rescue] loading models "
            f"disable_lid={disable_lid} disable_punc={disable_punc} "
            f"asr_batch_size={asr_batch_size} punc_batch_size={punc_batch_size}"
        )
    firered_runner = runner or create_firered_runner(
        disable_lid=disable_lid,
        disable_punc=disable_punc,
        asr_batch_size=asr_batch_size,
        punc_batch_size=punc_batch_size,
    )
    if runner is None and verbose:
        print("[firered-rescue] models ready")

    summary = {
        "status": "rescued",
        "root": str(root_path),
        "limit": limit,
        "seen": len(jobs),
        "completed": [],
        "errors": [],
        "config": {
            "disable_lid": disable_lid,
            "disable_punc": disable_punc,
            "asr_batch_size": asr_batch_size,
            "punc_batch_size": punc_batch_size,
        },
    }

    for job in jobs:
        prepared = None
        plan = build_firered_rescue_plan(job)
        try:
            if plan["reset_runner_before"]:
                firered_runner = rebuild_firered_runner(
                    current_runner=firered_runner,
                    disable_lid=disable_lid,
                    disable_punc=disable_punc,
                    asr_batch_size=asr_batch_size,
                    punc_batch_size=punc_batch_size,
                    verbose=verbose,
                )
            prepared = prepare_firered_job(root_path=root_path, job=job, verbose=verbose, label="rescue")
            if verbose:
                print(
                    f"[firered-rescue] start job={job['job_id']} category={plan['category']} "
                    f"strategy={plan['strategy']} duration={job.get('duration')} title={job.get('media_title')}"
                )
            if plan["strategy"] == "chunked":
                result, firered_runner = transcribe_prepared_firered_job_chunked(
                    root_path=root_path,
                    catalog=catalog,
                    firered_runner=firered_runner,
                    job=job,
                    prepared=prepared,
                    max_chars=max_chars,
                    chunk_seconds=int(plan["chunk_seconds"]),
                    chunk_overlap_seconds=int(plan["chunk_overlap_seconds"]),
                    disable_lid=disable_lid,
                    disable_punc=disable_punc,
                    asr_batch_size=asr_batch_size,
                    punc_batch_size=punc_batch_size,
                    verbose=verbose,
                    reset_runner_each_chunk=bool(plan.get("reset_runner_each_chunk")),
                )
            else:
                result = transcribe_prepared_firered_job(
                    root_path=root_path,
                    catalog=catalog,
                    firered_runner=firered_runner,
                    job=job,
                    prepared=prepared,
                    max_chars=max_chars,
                )
            if plan["validate_repetition"]:
                repetition_error = detect_repetitive_transcript_text(result["text"])
                if repetition_error:
                    raise ValueError(f"Rescue validation failed: {repetition_error}")
            summary["completed"].append(
                {
                    **result,
                    "category": plan["category"],
                    "strategy": plan["strategy"],
                }
            )
            if verbose:
                print(
                    f"[firered-rescue] ok job={job['job_id']} category={plan['category']} "
                    f"strategy={plan['strategy']} path={result['transcript_path']}"
                )
        except Exception as exc:  # noqa: BLE001
            catalog.mark_transcript_error(job_id=str(job["job_id"]), error=str(exc))
            summary["errors"].append(
                {
                    "job_id": job["job_id"],
                    "category": plan["category"],
                    "strategy": plan["strategy"],
                    "error": str(exc),
                }
            )
            if verbose:
                print(
                    f"[firered-rescue] error job={job['job_id']} category={plan['category']} "
                    f"strategy={plan['strategy']} error={exc}"
                )
            if is_recoverable_firered_error(exc):
                firered_runner = rebuild_firered_runner(
                    current_runner=firered_runner,
                    disable_lid=disable_lid,
                    disable_punc=disable_punc,
                    asr_batch_size=asr_batch_size,
                    punc_batch_size=punc_batch_size,
                    verbose=verbose,
                )
        finally:
            cleanup_prepared_wav(prepared)

    summary["catalog"] = catalog.stats()
    return summary


def transcribe_firered_rescue_loop(
    root: str = "data",
    *,
    batch_size: int = 1,
    max_chars: int = 800,
    sleep_seconds: float = 5.0,
    max_idle_cycles: int = 0,
    job_ids: Optional[List[str]] = None,
    categories: Optional[List[str]] = None,
    verbose: bool = False,
    disable_lid: bool = True,
    disable_punc: bool = False,
    asr_batch_size: int = 4,
    punc_batch_size: int = 8,
) -> dict:
    root_path = Path(root)
    catalog = Catalog(str(root_path))
    if verbose:
        print(
            "[firered-rescue] loading models "
            f"disable_lid={disable_lid} disable_punc={disable_punc} "
            f"asr_batch_size={asr_batch_size} punc_batch_size={punc_batch_size}"
        )
    runner = create_firered_runner(
        disable_lid=disable_lid,
        disable_punc=disable_punc,
        asr_batch_size=asr_batch_size,
        punc_batch_size=punc_batch_size,
    )
    if verbose:
        print("[firered-rescue] models ready")

    aggregate = {
        "status": "completed",
        "root": str(root_path),
        "batch_size": batch_size,
        "sleep_seconds": sleep_seconds,
        "max_idle_cycles": max_idle_cycles,
        "cycles": 0,
        "idle_cycles": 0,
        "seen": 0,
        "completed": 0,
        "errors": 0,
        "config": {
            "disable_lid": disable_lid,
            "disable_punc": disable_punc,
            "asr_batch_size": asr_batch_size,
            "punc_batch_size": punc_batch_size,
        },
    }

    try:
        write_runtime_state(
            catalog,
            "runtime:transcribe_rescue_loop",
            {
                **aggregate,
                "status": "running",
            },
        )
        while True:
            summary = transcribe_firered_rescue_jobs(
                root=root,
                limit=batch_size,
                max_chars=max_chars,
                job_ids=job_ids,
                categories=categories,
                verbose=verbose,
                disable_lid=disable_lid,
                disable_punc=disable_punc,
                asr_batch_size=asr_batch_size,
                punc_batch_size=punc_batch_size,
                runner=runner,
            )
            aggregate["cycles"] += 1
            aggregate["seen"] += int(summary.get("seen", 0))
            aggregate["completed"] += len(summary.get("completed", []))
            aggregate["errors"] += len(summary.get("errors", []))
            aggregate["catalog"] = summary.get("catalog")

            if not summary.get("seen"):
                aggregate["idle_cycles"] += 1
                if max_idle_cycles > 0 and aggregate["idle_cycles"] >= max_idle_cycles:
                    break
            else:
                aggregate["idle_cycles"] = 0

            if verbose:
                print(
                    f"[firered-rescue-loop] cycle={aggregate['cycles']} "
                    f"seen={summary.get('seen', 0)} completed={len(summary.get('completed', []))} "
                    f"errors={len(summary.get('errors', []))} idle={aggregate['idle_cycles']}"
                )
            write_runtime_state(
                catalog,
                "runtime:transcribe_rescue_loop",
                {
                    **aggregate,
                    "status": "running",
                },
            )
            if not summary.get("seen") and sleep_seconds > 0:
                time.sleep(sleep_seconds)
    except KeyboardInterrupt:
        aggregate["status"] = "paused"
        aggregate["catalog"] = catalog.stats()
        write_runtime_state(
            catalog,
            "runtime:transcribe_rescue_loop",
            {
                **aggregate,
                "status": "paused",
            },
        )
        return aggregate

    write_runtime_state(
        catalog,
        "runtime:transcribe_rescue_loop",
        {
            **aggregate,
            "status": "completed",
        },
    )
    return aggregate


def remote_state_key(job_id: str) -> str:
    return f"remote_transcript:{job_id}"


def payload_error_message(payload: dict, *, default: str) -> str:
    submit_headers = ((payload.get("submit") or {}).get("headers")) or {}
    submit_body = (payload.get("submit") or {}).get("body_text")
    query_headers = ((payload.get("query") or {}).get("headers")) or {}
    query_body = (payload.get("query") or {}).get("body_text")
    return (
        submit_headers.get("X-Api-Message")
        or submit_body
        or query_headers.get("X-Api-Message")
        or query_body
        or payload.get("body_text")
        or default
    )


def build_job_hotwords(
    *,
    root_path: Path,
    job: dict,
    global_hotword_file: Optional[str],
    limit: int,
) -> List[str]:
    dynamic_terms = extract_hotwords_from_job(root_path=root_path, job=job)
    global_terms = load_hotwords(global_hotword_file) if global_hotword_file else []
    return merge_hotwords(dynamic_terms, global_terms, limit=limit)


def extract_hotwords_from_job(*, root_path: Path, job: dict) -> List[str]:
    normalized_path = root_path / "normalized" / str(job["content_type"]) / f"{job['item_id']}.json"
    if not normalized_path.exists():
        return []
    record = json.loads(normalized_path.read_text(encoding="utf-8-sig"))
    return extract_hotwords_from_record(record)


def build_remote_hotword_path(*, job: dict, provider: str) -> Path:
    return (
        Path("runs")
        / "remote_transcripts"
        / provider
        / str(job["content_type"])
        / str(job["item_id"])
        / f"{safe_filename(str(job['job_id']))}.hotwords.txt"
    )


def write_runtime_state(catalog: Catalog, state_key: str, payload: dict) -> None:
    catalog.set_state(state_key, payload)
