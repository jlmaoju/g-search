from __future__ import annotations

import argparse
import json
import os
import sys
import time
import uuid
from pathlib import Path
from typing import Any
from urllib import error, request


SUBMIT_URL = "https://openspeech.bytedance.com/api/v3/auc/bigmodel/submit"
QUERY_URL = "https://openspeech.bytedance.com/api/v3/auc/bigmodel/query"
FLASH_URL = "https://openspeech.bytedance.com/api/v3/auc/bigmodel/recognize/flash"
IDLE_SUBMIT_URL = "https://openspeech.bytedance.com/api/v3/auc/bigmodel/idle/submit"
IDLE_QUERY_URL = "https://openspeech.bytedance.com/api/v3/auc/bigmodel/idle/query"
SUCCESS_CODE = "20000000"
RUNNING_CODES = {"20000001", "20000002"}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run Volcengine bigmodel AUC transcription for a public audio URL")
    parser.add_argument("--input-url", default=None, help="Publicly reachable audio URL")
    parser.add_argument("--output", required=True, help="Output JSON path")
    parser.add_argument("--mode", choices=("standard", "flash", "idle"), default="standard")
    parser.add_argument("--action", choices=("run", "submit", "query"), default="run")
    parser.add_argument("--request-id", default=None, help="Existing async request id for query mode")
    parser.add_argument("--app-id", default=os.environ.get("VOLCENGINE_APP_ID"), help="Volcengine App ID")
    parser.add_argument(
        "--access-key",
        default=os.environ.get("VOLCENGINE_ACCESS_KEY"),
        help="Volcengine access token/key for AUC",
    )
    parser.add_argument("--resource-id", default=None)
    parser.add_argument("--language", default="zh-CN")
    parser.add_argument("--uid", default="codex-gcores")
    parser.add_argument("--format", default="mp3")
    parser.add_argument("--rate", type=int, default=16000)
    parser.add_argument("--bits", type=int, default=16)
    parser.add_argument("--channel", type=int, default=1)
    parser.add_argument("--poll-interval", type=float, default=3.0)
    parser.add_argument("--timeout-seconds", type=int, default=900)
    parser.add_argument("--disable-itn", action="store_true")
    parser.add_argument("--enable-punc", action="store_true")
    parser.add_argument("--enable-ddc", action="store_true")
    parser.add_argument("--enable-speaker-info", action="store_true")
    parser.add_argument("--enable-lid", action="store_true")
    parser.add_argument("--enable-emotion-detection", action="store_true")
    parser.add_argument("--enable-gender-detection", action="store_true")
    parser.add_argument("--show-utterances", action="store_true")
    parser.add_argument("--show-speech-rate", action="store_true")
    parser.add_argument("--show-volume", action="store_true")
    parser.add_argument("--vad-segment", action="store_true")
    parser.add_argument("--end-window-size", type=int, default=None)
    parser.add_argument("--ssd-version", default=None)
    parser.add_argument("--hotword-file", default=None, help="Optional newline-delimited hotword file")
    parser.add_argument("--boosting-table-name", default=None)
    parser.add_argument("--correct-table-name", default=None)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if not args.app_id:
        raise SystemExit("Missing --app-id or VOLCENGINE_APP_ID")
    if not args.access_key:
        raise SystemExit("Missing --access-key or VOLCENGINE_ACCESS_KEY")
    if args.action in {"run", "submit"} and not args.input_url:
        raise SystemExit("Missing --input-url for run/submit")
    if args.action == "query" and not args.request_id:
        raise SystemExit("Missing --request-id for query")

    output_path = Path(args.output).resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    resource_id = resolve_resource_id(args)

    if args.action == "submit":
        if args.mode == "flash":
            raise SystemExit("flash mode does not support submit/query separation")
        result = submit_async_mode(
            args=args,
            resource_id=resource_id,
            submit_url=IDLE_SUBMIT_URL if args.mode == "idle" else SUBMIT_URL,
            mode=args.mode,
        )
        output_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        if not result.get("ok"):
            raise SystemExit(f"submit failed: {result.get('submit', {}).get('headers', {}).get('X-Api-Message')}")
        return 0
    if args.action == "query":
        if args.mode == "flash":
            raise SystemExit("flash mode does not support submit/query separation")
        result = query_async_mode(
            args=args,
            request_id=args.request_id,
            resource_id=resource_id,
            query_url=IDLE_QUERY_URL if args.mode == "idle" else QUERY_URL,
            mode=args.mode,
        )
        output_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        if not result.get("ok") and result.get("state") != "processing":
            raise SystemExit(f"query failed: {result.get('query', {}).get('headers', {}).get('X-Api-Message')}")
        return 0

    if args.mode == "flash":
        result = run_flash(args=args, resource_id=resource_id)
        output_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        if not result.get("ok"):
            raise SystemExit(f"flash failed: {result.get('query', {}).get('headers', {}).get('X-Api-Message')}")
        return 0
    if args.mode == "idle":
        result = run_async_mode(
            args=args,
            resource_id=resource_id,
            submit_url=IDLE_SUBMIT_URL,
            query_url=IDLE_QUERY_URL,
            mode="idle",
        )
        output_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        if not result.get("ok"):
            raise SystemExit(f"idle failed: {result.get('query', {}).get('headers', {}).get('X-Api-Message')}")
        return 0

    result = run_async_mode(
        args=args,
        resource_id=resource_id,
        submit_url=SUBMIT_URL,
        query_url=QUERY_URL,
        mode="standard",
    )
    output_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    if not result.get("ok"):
        raise SystemExit(f"standard failed: {result.get('query', {}).get('headers', {}).get('X-Api-Message')}")
    return 0


def run_async_mode(
    *,
    args: argparse.Namespace,
    resource_id: str,
    submit_url: str,
    query_url: str,
    mode: str,
) -> dict[str, Any]:
    submit_result = submit_async_mode(
        args=args,
        resource_id=resource_id,
        submit_url=submit_url,
        mode=mode,
    )
    if not submit_result.get("ok"):
        return submit_result
    request_id = str(submit_result["request_id"])
    submit_meta = submit_result["submit"]

    deadline = time.time() + args.timeout_seconds
    query_history: list[dict[str, Any]] = []
    last_query_meta: dict[str, Any] | None = None
    while True:
        query_result = query_async_mode(
            args=args,
            request_id=request_id,
            resource_id=resource_id,
            query_url=query_url,
            mode=mode,
        )
        query_history.append(query_result.get("query", {}))
        last_query_meta = query_result.get("query", {})
        if query_result.get("ok"):
            return {
                "submit": submit_meta,
                "query_attempts": len(query_history),
                **query_result,
            }
        if query_result.get("state") != "processing":
            query_result["submit"] = submit_meta
            query_result["query_attempts"] = len(query_history)
            return query_result
        if time.time() >= deadline:
            return {
                "ok": False,
                "mode": mode,
                "stage": "timeout",
                "request_id": request_id,
                "submit": submit_meta,
                "last_query": last_query_meta,
                "query_attempts": len(query_history),
            }
        time.sleep(args.poll_interval)


def submit_async_mode(
    *,
    args: argparse.Namespace,
    resource_id: str,
    submit_url: str,
    mode: str,
) -> dict[str, Any]:
    request_id = args.request_id or str(uuid.uuid4())
    submit_payload = build_audio_payload(args)
    submit_meta = post_json(
        url=submit_url,
        body=submit_payload,
        app_id=args.app_id,
        access_key=args.access_key,
        resource_id=resource_id,
        request_id=request_id,
        include_sequence=True,
    )
    status_code = submit_meta["headers"].get("X-Api-Status-Code")
    if status_code != SUCCESS_CODE:
        return {
            "ok": False,
            "mode": mode,
            "stage": "submit",
            "request_id": request_id,
            "submit": submit_meta,
        }
    return {
        "ok": True,
        "provider": "volcengine_auc",
        "mode": mode,
        "state": "submitted",
        "request_id": request_id,
        "resource_id": resource_id,
        "input_url": args.input_url,
        "submit": submit_meta,
    }


def query_async_mode(
    *,
    args: argparse.Namespace,
    request_id: str,
    resource_id: str,
    query_url: str,
    mode: str,
) -> dict[str, Any]:
    query_meta = post_json(
        url=query_url,
        body={},
        app_id=args.app_id,
        access_key=args.access_key,
        resource_id=resource_id,
        request_id=request_id,
        include_sequence=False,
    )
    query_status = query_meta["headers"].get("X-Api-Status-Code")
    if query_status == SUCCESS_CODE:
        result = query_meta.get("json_body") or {}
        return {
            "ok": True,
            "provider": "volcengine_auc",
            "mode": mode,
            "state": "completed",
            "request_id": request_id,
            "resource_id": resource_id,
            "input_url": args.input_url,
            "query": query_meta,
            "text": ((result.get("result") or {}).get("text")) or "",
            "segments": normalize_segments((result.get("result") or {}).get("utterances") or []),
            "words": flatten_words((result.get("result") or {}).get("utterances") or []),
            "raw_result": result,
            "model_name": f"volcengine-{mode}",
            "language": args.language,
        }
    if query_status in RUNNING_CODES:
        return {
            "ok": False,
            "provider": "volcengine_auc",
            "mode": mode,
            "state": "processing",
            "request_id": request_id,
            "resource_id": resource_id,
            "input_url": args.input_url,
            "query": query_meta,
        }
    return {
        "ok": False,
        "provider": "volcengine_auc",
        "mode": mode,
        "stage": "query",
        "request_id": request_id,
        "resource_id": resource_id,
        "input_url": args.input_url,
        "query": query_meta,
    }


def resolve_resource_id(args: argparse.Namespace) -> str:
    if args.resource_id:
        return args.resource_id
    if args.mode == "flash":
        return "volc.bigasr.auc_turbo"
    if args.mode == "idle":
        return "volc.bigasr.auc_idle"
    return "volc.seedasr.auc"


def run_flash(*, args: argparse.Namespace, resource_id: str) -> dict[str, Any]:
    request_id = str(uuid.uuid4())
    payload = build_audio_payload(args)
    query_meta = post_json(
        url=FLASH_URL,
        body=payload,
        app_id=args.app_id,
        access_key=args.access_key,
        resource_id=resource_id,
        request_id=request_id,
        include_sequence=True,
    )
    query_status = query_meta["headers"].get("X-Api-Status-Code")
    if query_status != SUCCESS_CODE:
        return {
            "ok": False,
            "provider": "volcengine_auc",
            "mode": "flash",
            "request_id": request_id,
            "resource_id": resource_id,
            "input_url": args.input_url,
            "query": query_meta,
        }
    result = query_meta.get("json_body") or {}
    return {
        "ok": True,
        "provider": "volcengine_auc",
        "mode": "flash",
        "request_id": request_id,
        "resource_id": resource_id,
        "input_url": args.input_url,
        "query": query_meta,
        "text": ((result.get("result") or {}).get("text")) or "",
        "segments": normalize_segments((result.get("result") or {}).get("utterances") or []),
        "words": flatten_words((result.get("result") or {}).get("utterances") or []),
        "raw_result": result,
    }


def build_audio_payload(args: argparse.Namespace) -> dict[str, Any]:
    corpus: dict[str, Any] = {}
    hotwords = load_hotwords_from_file(args.hotword_file)
    if hotwords:
        corpus["context"] = json.dumps(
            {"hotwords": [{"word": term} for term in hotwords]},
            ensure_ascii=False,
        )
    if args.boosting_table_name:
        corpus["boosting_table_name"] = args.boosting_table_name
    if args.correct_table_name:
        corpus["correct_table_name"] = args.correct_table_name

    request_payload = {
        "model_name": "bigmodel",
        "enable_itn": not args.disable_itn,
        "enable_punc": args.enable_punc,
        "enable_ddc": args.enable_ddc,
        "enable_speaker_info": args.enable_speaker_info,
        "enable_lid": args.enable_lid,
        "enable_emotion_detection": args.enable_emotion_detection,
        "enable_gender_detection": args.enable_gender_detection,
        "show_utterances": args.show_utterances,
        "show_speech_rate": args.show_speech_rate,
        "show_volume": args.show_volume,
        "vad_segment": args.vad_segment,
    }
    if args.end_window_size is not None:
        request_payload["end_window_size"] = args.end_window_size
    if args.ssd_version:
        request_payload["ssd_version"] = args.ssd_version
    if corpus:
        request_payload["corpus"] = corpus

    return {
        "user": {"uid": args.uid},
        "audio": {
            "url": args.input_url,
            "language": args.language,
            "format": args.format,
            "rate": args.rate,
            "bits": args.bits,
            "channel": args.channel,
        },
        "request": request_payload,
    }


def load_hotwords_from_file(path: str | None) -> list[str]:
    if not path:
        return []
    hotword_path = Path(path)
    if not hotword_path.exists():
        return []
    hotwords: list[str] = []
    for line in hotword_path.read_text(encoding="utf-8-sig").splitlines():
        term = line.strip()
        if term:
            hotwords.append(term.split("|", 1)[0].strip())
    return hotwords[:5000]


def post_json(
    *,
    url: str,
    body: dict[str, Any],
    app_id: str,
    access_key: str,
    resource_id: str,
    request_id: str,
    include_sequence: bool,
) -> dict[str, Any]:
    payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "X-Api-App-Key": app_id,
        "X-Api-Access-Key": access_key,
        "X-Api-Resource-Id": resource_id,
        "X-Api-Request-Id": request_id,
    }
    if include_sequence:
        headers["X-Api-Sequence"] = "-1"
    last_error: str | None = None
    for attempt in range(3):
        req = request.Request(url, data=payload, headers=headers, method="POST")
        try:
            with request.urlopen(req, timeout=180) as response:
                body_bytes = response.read()
                status = response.getcode()
                response_headers = dict(response.headers.items())
            break
        except error.HTTPError as exc:
            body_bytes = exc.read()
            status = exc.code
            response_headers = dict(exc.headers.items())
            break
        except error.URLError as exc:
            last_error = str(exc)
            if attempt >= 2:
                return {
                    "http_status": None,
                    "headers": {},
                    "body_text": last_error,
                    "json_body": None,
                }
            time.sleep(1.5 * (attempt + 1))
    else:
        return {
            "http_status": None,
            "headers": {},
            "body_text": last_error or "request failed",
            "json_body": None,
        }

    body_text = body_bytes.decode("utf-8", errors="replace") if body_bytes else ""
    json_body = None
    if body_text:
        try:
            json_body = json.loads(body_text)
        except json.JSONDecodeError:
            json_body = None
    return {
        "http_status": status,
        "headers": response_headers,
        "body_text": body_text,
        "json_body": json_body,
    }


def normalize_segments(utterances: list[dict[str, Any]]) -> list[dict[str, Any]]:
    segments: list[dict[str, Any]] = []
    for utterance in utterances:
        additions = utterance.get("additions") or {}
        segments.append(
            {
                "start_ms": utterance.get("start_time"),
                "end_ms": utterance.get("end_time"),
                "text": utterance.get("text"),
                "speaker_id": additions.get("speaker_id") or additions.get("speaker"),
                "language": additions.get("lid_lang"),
                "emotion": additions.get("emotion"),
                "gender": additions.get("gender"),
                "volume": additions.get("volume"),
                "speech_rate": additions.get("speech_rate"),
            }
        )
    return segments


def flatten_words(utterances: list[dict[str, Any]]) -> list[dict[str, Any]]:
    words: list[dict[str, Any]] = []
    for index, utterance in enumerate(utterances):
        for word in utterance.get("words") or []:
            words.append(
                {
                    "segment_index": index,
                    "start_ms": word.get("start_time"),
                    "end_ms": word.get("end_time"),
                    "blank_duration": word.get("blank_duration"),
                    "text": word.get("text"),
                }
            )
    return words


if __name__ == "__main__":
    raise SystemExit(main())
