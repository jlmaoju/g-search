from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import time
from datetime import datetime, timezone
from typing import Dict, List

import uvicorn

from .api import GcoresAPI, core_content_types
from .catalog import Catalog
from .daily_runtime import get_daily_runtime_reporter
from .downloader import download_media_jobs, download_media_loop
from .http import CurlHttpClient
from .indexing import prepare_vector_inputs
from .lexicon import build_lexicon
from .lizhi import rescue_lizhi_jobs
from .query_core import run_search_query
from .search_pipeline import (
    build_search_index,
    sync_search_index,
)
from .search_bridge import bridge_search_results
from .search_bridge import DEFAULT_RESULTS_DIR as DEFAULT_BRIDGE_RESULTS_DIR
from .search_service import create_search_app
from .storage import FileStore
from .sync import (
    create_backfill_state,
    fetch_and_store_item,
    list_item_matches_scope as item_matches_scope,
    run_backfill,
    run_sync_latest,
)
from .viewer_main import main as viewer_main_entry
from .viewer_bundle import export_viewer_bundle
from .transcribe import (
    poll_remote_transcription_jobs,
    submit_remote_transcription_jobs,
    transcribe_firered_jobs,
    transcribe_firered_loop,
    transcribe_firered_rescue_jobs,
    transcribe_firered_rescue_loop,
    transcribe_media_jobs,
    transcribe_media_loop,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Gcores crawler prototype")
    subparsers = parser.add_subparsers(dest="command", required=True)

    discover = subparsers.add_parser("discover", help="Discover latest item IDs for a content type")
    discover.add_argument("content_type", help="Content type, for example articles or radios")
    discover.add_argument("--limit", type=int, default=10)
    discover.add_argument("--offset", type=int, default=0)
    discover.add_argument("--sort", default="-published-at")
    discover.set_defaults(handler=handle_discover)

    fetch = subparsers.add_parser("fetch", help="Fetch one or more items by ID")
    fetch.add_argument("content_type")
    fetch.add_argument("item_ids", nargs="+")
    fetch.add_argument("--output-dir", default="data")
    fetch.add_argument("--no-resolve-media", action="store_true")
    fetch.set_defaults(handler=handle_fetch)

    crawl = subparsers.add_parser("crawl", help="Crawl latest items across one or more content types")
    crawl.add_argument(
        "--types",
        nargs="+",
        default=list(core_content_types()),
        help="Content types to crawl",
    )
    crawl.add_argument("--output-dir", default="data")
    crawl.add_argument("--page-size", type=int, default=10)
    crawl.add_argument("--max-items", type=int, default=20)
    crawl.add_argument("--start-offset", type=int, default=0)
    crawl.add_argument("--sort", default="-published-at")
    crawl.add_argument("--no-resolve-media", action="store_true")
    crawl.add_argument(
        "--include-join-podcasts",
        action="store_true",
        help="Include third-party joined podcasts instead of only Gcores-owned radios",
    )
    crawl.set_defaults(handler=handle_crawl)

    backfill = subparsers.add_parser(
        "backfill",
        help="Run a resumable full-site backfill over the default scope",
    )
    backfill.add_argument(
        "--types",
        nargs="+",
        default=list(core_content_types()),
        help="Content types to backfill",
    )
    backfill.add_argument("--output-dir", default="data")
    backfill.add_argument("--page-size", type=int, default=20)
    backfill.add_argument("--start-offset", type=int, default=0)
    backfill.add_argument("--sort", default="-published-at")
    backfill.add_argument("--no-resolve-media", action="store_true")
    backfill.add_argument(
        "--include-join-podcasts",
        action="store_true",
        help="Include third-party joined podcasts instead of only Gcores-owned radios",
    )
    backfill.add_argument(
        "--max-pages",
        type=int,
        default=0,
        help="Optional page cap for smoke tests; 0 means no cap",
    )
    backfill.add_argument(
        "--max-items",
        type=int,
        default=0,
        help="Optional per-type matched item cap; 0 means no cap",
    )
    backfill.set_defaults(handler=handle_backfill)

    resume = subparsers.add_parser("resume", help="Resume a previously interrupted crawl run")
    resume.add_argument("--output-dir", default="data")
    resume.set_defaults(handler=handle_resume)

    resume_backfill = subparsers.add_parser(
        "resume-backfill",
        help="Resume a previously interrupted full-site backfill",
    )
    resume_backfill.add_argument("--output-dir", default="data")
    resume_backfill.set_defaults(handler=handle_resume_backfill)

    status = subparsers.add_parser("status", help="Show crawl state for an output directory")
    status.add_argument("--output-dir", default="data")
    status.set_defaults(handler=handle_status)

    backfill_status = subparsers.add_parser(
        "backfill-status",
        help="Show backfill state for an output directory",
    )
    backfill_status.add_argument("--output-dir", default="data")
    backfill_status.set_defaults(handler=handle_backfill_status)

    sync_latest = subparsers.add_parser(
        "sync-latest",
        help="Incrementally sync the latest pages using durable watermarks",
    )
    sync_latest.add_argument(
        "--types",
        nargs="+",
        default=list(core_content_types()),
        help="Content types to sync",
    )
    sync_latest.add_argument("--output-dir", default="data")
    sync_latest.add_argument("--page-size", type=int, default=20)
    sync_latest.add_argument("--sort", default="-published-at")
    sync_latest.add_argument("--no-resolve-media", action="store_true")
    sync_latest.add_argument(
        "--include-join-podcasts",
        action="store_true",
        help="Include third-party joined podcasts instead of only Gcores-owned radios",
    )
    sync_latest.add_argument(
        "--refresh-pages",
        type=int,
        default=1,
        help="Refresh known items from the newest N pages before falling back to watermarks",
    )
    sync_latest.add_argument(
        "--max-pages",
        type=int,
        default=0,
        help="Optional page cap for smoke tests; 0 means no cap",
    )
    sync_latest.set_defaults(handler=handle_sync_latest)

    prepare_index = subparsers.add_parser(
        "prepare-index",
        help="Build vector-ready text chunks and media job manifests from normalized crawl output",
    )
    prepare_index.add_argument("--output-dir", default="data")
    prepare_index.add_argument(
        "--types",
        nargs="+",
        default=list(core_content_types()),
        help="Content types to include in vector-prep output",
    )
    prepare_index.add_argument("--max-chars", type=int, default=800)
    prepare_index.add_argument("--no-jsonl", action="store_true")
    prepare_index.add_argument("--no-sqlite", action="store_true")
    prepare_index.add_argument(
        "--include-join-podcasts",
        action="store_true",
        help="Include third-party joined podcasts in vector-prep output",
    )
    prepare_index.add_argument(
        "--include-article-speech",
        action="store_true",
        help="Include article speech-path audio as media jobs",
    )
    prepare_index.set_defaults(handler=handle_prepare_index)

    build_lexicon_parser = subparsers.add_parser(
        "build-lexicon",
        help="Build hotword and alias-map support files from normalized content",
    )
    build_lexicon_parser.add_argument("--output-dir", default="data")
    build_lexicon_parser.add_argument(
        "--types",
        nargs="+",
        default=["radios"],
        help="Content types to scan when building hotwords",
    )
    build_lexicon_parser.add_argument("--limit", type=int, default=2000)
    build_lexicon_parser.set_defaults(handler=handle_build_lexicon)

    download_media = subparsers.add_parser(
        "download-media",
        help="Download pending media jobs from the SQLite catalog",
    )
    download_media.add_argument("--output-dir", default="data")
    download_media.add_argument("--limit", type=int, default=10)
    download_media.add_argument("--job-id", action="append", default=[])
    download_media.add_argument("--item-key", action="append", default=[])
    download_media.add_argument("--segment-type", action="append", default=[])
    download_media.add_argument("--media-type", action="append", default=[])
    download_media.add_argument(
        "--include-errors",
        action="store_true",
        help="Retry transient download errors in the same command run",
    )
    download_media.set_defaults(handler=handle_download_media)

    download_media_loop_parser = subparsers.add_parser(
        "download-media-loop",
        help="Continuously download media jobs from the SQLite catalog",
    )
    download_media_loop_parser.add_argument("--output-dir", default="data")
    download_media_loop_parser.add_argument("--batch-size", type=int, default=50)
    download_media_loop_parser.add_argument("--max-workers", type=int, default=10)
    download_media_loop_parser.add_argument("--min-workers", type=int, default=2)
    download_media_loop_parser.add_argument("--backoff-error-threshold", type=int, default=3)
    download_media_loop_parser.add_argument("--backoff-sleep-seconds", type=float, default=30.0)
    download_media_loop_parser.add_argument("--recover-after-cycles", type=int, default=3)
    download_media_loop_parser.add_argument(
        "--include-errors",
        action="store_true",
        help="Retry transient download errors in the same loop run",
    )
    download_media_loop_parser.add_argument("--job-id", action="append", default=[])
    download_media_loop_parser.add_argument("--item-key", action="append", default=[])
    download_media_loop_parser.add_argument("--segment-type", action="append", default=[])
    download_media_loop_parser.add_argument("--media-type", action="append", default=[])
    download_media_loop_parser.add_argument("--sleep-seconds", type=float, default=5.0)
    download_media_loop_parser.add_argument(
        "--max-idle-cycles",
        type=int,
        default=3,
        help="Stop after this many empty polls; 0 means run forever",
    )
    download_media_loop_parser.add_argument("--verbose", action="store_true")
    download_media_loop_parser.set_defaults(handler=handle_download_media_loop)

    rescue_lizhi = subparsers.add_parser(
        "rescue-lizhi",
        help="Resolve old Lizhi-hosted radios to newer voiceTrack URLs without blocking the main downloader",
    )
    rescue_lizhi.add_argument("--output-dir", default="data")
    rescue_lizhi.add_argument("--limit", type=int, default=20)
    rescue_lizhi.add_argument("--job-id", action="append", default=[])
    rescue_lizhi.add_argument(
        "--status",
        action="append",
        default=[],
        help="Job status to include, for example unsupported or error; repeatable",
    )
    rescue_lizhi.add_argument(
        "--apply",
        action="store_true",
        help="Write the resolved voiceTrack URL back into SQLite and reset the jobs to pending",
    )
    rescue_lizhi.add_argument(
        "--force-apply",
        action="store_true",
        help="Apply even when the lightweight download probe fails",
    )
    rescue_lizhi.add_argument("--verbose", action="store_true")
    rescue_lizhi.set_defaults(handler=handle_rescue_lizhi)

    transcribe_media = subparsers.add_parser(
        "transcribe-media",
        help="Run a local transcription script over downloaded media jobs",
    )
    transcribe_media.add_argument("--output-dir", default="data")
    transcribe_media.add_argument("--script", required=True)
    transcribe_media.add_argument(
        "--python-executable",
        default=None,
        help="Optional Python executable used to run the transcription wrapper",
    )
    transcribe_media.add_argument("--script-arg", action="append", default=[])
    transcribe_media.add_argument("--limit", type=int, default=10)
    transcribe_media.add_argument("--max-chars", type=int, default=800)
    transcribe_media.add_argument("--job-id", action="append", default=[])
    transcribe_media.add_argument("--verbose", action="store_true")
    transcribe_media.set_defaults(handler=handle_transcribe_media)

    transcribe_media_loop_parser = subparsers.add_parser(
        "transcribe-media-loop",
        help="Continuously run a local transcription script over downloaded media jobs",
    )
    transcribe_media_loop_parser.add_argument("--output-dir", default="data")
    transcribe_media_loop_parser.add_argument("--script", required=True)
    transcribe_media_loop_parser.add_argument(
        "--python-executable",
        default=None,
        help="Optional Python executable used to run the transcription wrapper",
    )
    transcribe_media_loop_parser.add_argument("--script-arg", action="append", default=[])
    transcribe_media_loop_parser.add_argument("--batch-size", type=int, default=1)
    transcribe_media_loop_parser.add_argument("--max-chars", type=int, default=800)
    transcribe_media_loop_parser.add_argument("--job-id", action="append", default=[])
    transcribe_media_loop_parser.add_argument("--sleep-seconds", type=float, default=5.0)
    transcribe_media_loop_parser.add_argument(
        "--max-idle-cycles",
        type=int,
        default=3,
        help="Stop after this many empty polls; 0 means run forever",
    )
    transcribe_media_loop_parser.add_argument("--verbose", action="store_true")
    transcribe_media_loop_parser.set_defaults(handler=handle_transcribe_media_loop)

    transcribe_firered = subparsers.add_parser(
        "transcribe-firered",
        help="Run local FireRed transcription directly without per-job model reloads",
    )
    transcribe_firered.add_argument("--output-dir", default="data")
    transcribe_firered.add_argument("--limit", type=int, default=10)
    transcribe_firered.add_argument("--max-chars", type=int, default=800)
    transcribe_firered.add_argument("--job-id", action="append", default=[])
    transcribe_firered.add_argument("--disable-lid", action="store_true")
    transcribe_firered.add_argument("--disable-punc", action="store_true")
    transcribe_firered.add_argument("--asr-batch-size", type=int, default=4)
    transcribe_firered.add_argument("--punc-batch-size", type=int, default=8)
    transcribe_firered.add_argument("--verbose", action="store_true")
    transcribe_firered.set_defaults(handler=handle_transcribe_firered)

    transcribe_firered_loop_parser = subparsers.add_parser(
        "transcribe-firered-loop",
        help="Continuously run local FireRed transcription with a persistent model worker",
    )
    transcribe_firered_loop_parser.add_argument("--output-dir", default="data")
    transcribe_firered_loop_parser.add_argument("--batch-size", type=int, default=1)
    transcribe_firered_loop_parser.add_argument("--max-chars", type=int, default=800)
    transcribe_firered_loop_parser.add_argument("--job-id", action="append", default=[])
    transcribe_firered_loop_parser.add_argument("--sleep-seconds", type=float, default=5.0)
    transcribe_firered_loop_parser.add_argument(
        "--max-idle-cycles",
        type=int,
        default=0,
        help="Stop after this many empty polls; 0 means run forever",
    )
    transcribe_firered_loop_parser.add_argument("--disable-lid", action="store_true")
    transcribe_firered_loop_parser.add_argument("--disable-punc", action="store_true")
    transcribe_firered_loop_parser.add_argument("--asr-batch-size", type=int, default=4)
    transcribe_firered_loop_parser.add_argument("--punc-batch-size", type=int, default=8)
    transcribe_firered_loop_parser.add_argument("--verbose", action="store_true")
    transcribe_firered_loop_parser.set_defaults(handler=handle_transcribe_firered_loop)

    transcribe_firered_rescue = subparsers.add_parser(
        "transcribe-firered-rescue",
        help="Run FireRed rescue strategies over failed transcription jobs",
    )
    transcribe_firered_rescue.add_argument("--output-dir", default="data")
    transcribe_firered_rescue.add_argument("--limit", type=int, default=10)
    transcribe_firered_rescue.add_argument("--max-chars", type=int, default=800)
    transcribe_firered_rescue.add_argument("--job-id", action="append", default=[])
    transcribe_firered_rescue.add_argument("--category", action="append", default=[])
    transcribe_firered_rescue.add_argument("--disable-lid", action="store_true")
    transcribe_firered_rescue.add_argument("--disable-punc", action="store_true")
    transcribe_firered_rescue.add_argument("--asr-batch-size", type=int, default=4)
    transcribe_firered_rescue.add_argument("--punc-batch-size", type=int, default=8)
    transcribe_firered_rescue.add_argument("--verbose", action="store_true")
    transcribe_firered_rescue.set_defaults(handler=handle_transcribe_firered_rescue)

    transcribe_firered_rescue_loop_parser = subparsers.add_parser(
        "transcribe-firered-rescue-loop",
        help="Continuously run FireRed rescue strategies over failed transcription jobs",
    )
    transcribe_firered_rescue_loop_parser.add_argument("--output-dir", default="data")
    transcribe_firered_rescue_loop_parser.add_argument("--batch-size", type=int, default=1)
    transcribe_firered_rescue_loop_parser.add_argument("--max-chars", type=int, default=800)
    transcribe_firered_rescue_loop_parser.add_argument("--job-id", action="append", default=[])
    transcribe_firered_rescue_loop_parser.add_argument("--category", action="append", default=[])
    transcribe_firered_rescue_loop_parser.add_argument("--sleep-seconds", type=float, default=5.0)
    transcribe_firered_rescue_loop_parser.add_argument(
        "--max-idle-cycles",
        type=int,
        default=0,
        help="Stop after this many empty polls; 0 means run forever",
    )
    transcribe_firered_rescue_loop_parser.add_argument("--disable-lid", action="store_true")
    transcribe_firered_rescue_loop_parser.add_argument("--disable-punc", action="store_true")
    transcribe_firered_rescue_loop_parser.add_argument("--asr-batch-size", type=int, default=4)
    transcribe_firered_rescue_loop_parser.add_argument("--punc-batch-size", type=int, default=8)
    transcribe_firered_rescue_loop_parser.add_argument("--verbose", action="store_true")
    transcribe_firered_rescue_loop_parser.set_defaults(handler=handle_transcribe_firered_rescue_loop)

    submit_volcengine_idle = subparsers.add_parser(
        "submit-volcengine-idle",
        help="Submit public audio jobs to Volcengine idle transcription",
    )
    submit_volcengine_idle.add_argument("--output-dir", default="data")
    submit_volcengine_idle.add_argument(
        "--script",
        default="scripts/volcengine_auc_transcribe.py",
    )
    submit_volcengine_idle.add_argument(
        "--python-executable",
        default=None,
        help="Optional Python executable used to run the Volcengine wrapper",
    )
    submit_volcengine_idle.add_argument("--script-arg", action="append", default=[])
    submit_volcengine_idle.add_argument("--limit", type=int, default=10)
    submit_volcengine_idle.add_argument("--job-id", action="append", default=[])
    submit_volcengine_idle.add_argument("--global-hotword-file", default=None)
    submit_volcengine_idle.add_argument("--hotword-limit", type=int, default=128)
    submit_volcengine_idle.set_defaults(handler=handle_submit_volcengine_idle)

    poll_volcengine_idle = subparsers.add_parser(
        "poll-volcengine-idle",
        help="Poll submitted Volcengine idle jobs and save completed transcripts",
    )
    poll_volcengine_idle.add_argument("--output-dir", default="data")
    poll_volcengine_idle.add_argument(
        "--script",
        default="scripts/volcengine_auc_transcribe.py",
    )
    poll_volcengine_idle.add_argument(
        "--python-executable",
        default=None,
        help="Optional Python executable used to run the Volcengine wrapper",
    )
    poll_volcengine_idle.add_argument("--script-arg", action="append", default=[])
    poll_volcengine_idle.add_argument("--limit", type=int, default=10)
    poll_volcengine_idle.add_argument("--max-chars", type=int, default=800)
    poll_volcengine_idle.add_argument("--job-id", action="append", default=[])
    poll_volcengine_idle.add_argument("--alias-map", default=None)
    poll_volcengine_idle.set_defaults(handler=handle_poll_volcengine_idle)

    catalog_status = subparsers.add_parser(
        "catalog-status",
        help="Show SQLite catalog counts and queue status",
    )
    catalog_status.add_argument("--output-dir", default="data")
    catalog_status.set_defaults(handler=handle_catalog_status)

    pipeline_status = subparsers.add_parser(
        "pipeline-status",
        help="Show combined download/transcription progress and recent failures",
    )
    pipeline_status.add_argument("--output-dir", default="data")
    pipeline_status.add_argument("--watch-seconds", type=float, default=0.0)
    pipeline_status.add_argument("--failure-limit", type=int, default=5)
    pipeline_status.set_defaults(handler=handle_pipeline_status)

    runtime_status = subparsers.add_parser(
        "runtime-status",
        help="Show the operational radio-audio view with download, transcription, and recent progress",
    )
    runtime_status.add_argument("--output-dir", default="data")
    runtime_status.add_argument("--failure-limit", type=int, default=5)
    runtime_status.set_defaults(handler=handle_runtime_status)

    build_search = subparsers.add_parser(
        "build-search-index",
        help="Build the local search documents and Qdrant index for official radios and timelines",
    )
    add_search_common_arguments(build_search)
    build_search.add_argument("--limit", type=int, default=0)
    build_search.add_argument("--verbose", action="store_true")
    build_search.set_defaults(handler=handle_build_search_index)

    sync_search = subparsers.add_parser(
        "sync-search-index",
        help="Incrementally sync new transcripts and timelines into the local search index",
    )
    add_search_common_arguments(sync_search)
    sync_search.add_argument("--limit", type=int, default=0)
    sync_search.add_argument("--verbose", action="store_true")
    sync_search.set_defaults(handler=handle_sync_search_index)

    sync_search_loop = subparsers.add_parser(
        "sync-search-loop",
        help="Continuously feed completed transcripts into the search index with retry and backoff",
    )
    add_search_common_arguments(sync_search_loop)
    sync_search_loop.add_argument("--batch-size", type=int, default=3)
    sync_search_loop.add_argument("--min-batch-size", type=int, default=1)
    sync_search_loop.add_argument("--sleep-seconds", type=float, default=20.0)
    sync_search_loop.add_argument("--backoff-sleep-seconds", type=float, default=90.0)
    sync_search_loop.add_argument("--max-backoff-seconds", type=float, default=900.0)
    sync_search_loop.add_argument("--recover-after-cycles", type=int, default=2)
    sync_search_loop.add_argument(
        "--max-idle-cycles",
        type=int,
        default=0,
        help="Stop after this many empty polls; 0 means run forever",
    )
    sync_search_loop.add_argument("--verbose", action="store_true")
    sync_search_loop.set_defaults(handler=handle_sync_search_loop)

    bridge_search = subparsers.add_parser(
        "bridge-search-results",
        help="Bridge completed extraction results into the main search index",
    )
    add_search_common_arguments(bridge_search)
    bridge_search.add_argument(
        "--results-dir",
        default=DEFAULT_BRIDGE_RESULTS_DIR,
        help="Directory containing completed extraction result JSON files",
    )
    bridge_search.add_argument(
        "--bridge-mode",
        choices=["summaries-only", "full"],
        default="summaries-only",
        help="`summaries-only` only replaces scene_summary/episode_card; `full` also rebuilds title/timeline/atom docs.",
    )
    bridge_search.add_argument("--limit", type=int, default=0)
    bridge_search.add_argument("--dry-run", action="store_true")
    bridge_search.add_argument("--verbose", action="store_true")
    bridge_search.set_defaults(handler=handle_bridge_search_results)

    bridge_search_loop = subparsers.add_parser(
        "bridge-search-loop",
        help="Continuously bridge completed extraction results into the main search index",
    )
    add_search_common_arguments(bridge_search_loop)
    bridge_search_loop.add_argument(
        "--results-dir",
        default=DEFAULT_BRIDGE_RESULTS_DIR,
        help="Directory containing completed extraction result JSON files",
    )
    bridge_search_loop.add_argument(
        "--bridge-mode",
        choices=["summaries-only", "full"],
        default="summaries-only",
        help="`summaries-only` only replaces scene_summary/episode_card; `full` also rebuilds title/timeline/atom docs.",
    )
    bridge_search_loop.add_argument("--batch-size", type=int, default=20)
    bridge_search_loop.add_argument("--sleep-seconds", type=float, default=20.0)
    bridge_search_loop.add_argument("--backoff-sleep-seconds", type=float, default=60.0)
    bridge_search_loop.add_argument("--max-backoff-seconds", type=float, default=600.0)
    bridge_search_loop.add_argument(
        "--max-idle-cycles",
        type=int,
        default=0,
        help="Stop after this many empty polls; 0 means run forever",
    )
    bridge_search_loop.add_argument("--verbose", action="store_true")
    bridge_search_loop.set_defaults(handler=handle_bridge_search_loop)

    search = subparsers.add_parser(
        "search",
        help="Run a local search query against the Qdrant-backed memory index",
    )
    add_search_common_arguments(search)
    search.add_argument("query")
    search.add_argument("--limit", type=int, default=10)
    search.set_defaults(handler=handle_search_query)

    serve_search = subparsers.add_parser(
        "serve-search",
        help="Serve the shared read-only query API and static frontend",
    )
    add_search_common_arguments(serve_search)
    serve_search.add_argument("--host", default="127.0.0.1")
    serve_search.add_argument("--port", type=int, default=8765)
    serve_search.add_argument("--download-url", default="")
    serve_search.set_defaults(handler=handle_serve_search)

    export_viewer = subparsers.add_parser(
        "export-viewer-bundle",
        help="Export a read-only viewer data bundle (catalog + qdrant snapshot + manifest)",
    )
    export_viewer.add_argument("--output-dir", required=True)
    export_viewer.add_argument("--source-root", default="data")
    export_viewer.add_argument("--qdrant-path", default=os.environ.get("GCORES_QDRANT_PATH", "qdrant"))
    export_viewer.add_argument(
        "--collection-name",
        default=os.environ.get("GCORES_QDRANT_COLLECTION", "gcores_memory_v1"),
    )
    export_viewer.add_argument("--embedding-provider", default="zhipu")
    export_viewer.add_argument("--embedding-model", default="embedding-3")
    export_viewer.add_argument("--target-platform", default="win-x64")
    export_viewer.add_argument("--app-min-version", default="0.1.0")
    export_viewer.add_argument("--data-version", default=None)
    export_viewer.add_argument("--overwrite", action="store_true")
    export_viewer.set_defaults(handler=handle_export_viewer_bundle)

    viewer_main = subparsers.add_parser(
        "viewer-main",
        help="Launch the local Viewer shell against a viewer data bundle",
    )
    viewer_main.add_argument("--data-root", default="data/current")
    viewer_main.add_argument("--host", default="127.0.0.1")
    viewer_main.add_argument("--port", type=int, default=8765)
    viewer_main.add_argument("--qdrant-url", default="http://127.0.0.1:6335")
    viewer_main.add_argument("--qdrant-sidecar-command", default=os.environ.get("GCORES_VIEWER_QDRANT_COMMAND", ""))
    viewer_main.add_argument("--collection-name", default=os.environ.get("GCORES_QDRANT_COLLECTION", "gcores_memory_v1"))
    viewer_main.add_argument("--embedding-provider", default="zhipu")
    viewer_main.add_argument("--embedding-model", default="embedding-3")
    viewer_main.add_argument("--download-url", default="")
    viewer_main.add_argument("--no-open-browser", action="store_true")
    viewer_main.set_defaults(handler=handle_viewer_main)

    return parser


def add_search_common_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--output-dir", default="data")
    parser.add_argument("--qdrant-path", default=os.environ.get("GCORES_QDRANT_PATH", "qdrant"))
    parser.add_argument("--qdrant-url", default=os.environ.get("GCORES_QDRANT_URL"))
    parser.add_argument("--qdrant-api-key", default=os.environ.get("GCORES_QDRANT_API_KEY"))
    parser.add_argument("--collection-name", default=os.environ.get("GCORES_QDRANT_COLLECTION", "gcores_memory_v1"))
    parser.add_argument("--embedding-provider", default="zhipu")
    parser.add_argument("--embedding-model", default="embedding-3")
    parser.add_argument("--embedding-api-key", default=None)
    parser.add_argument("--embedding-dimension", type=int, default=None)
    parser.add_argument("--embedding-batch-size", type=int, default=64)
    parser.add_argument("--embedding-concurrency", type=int, default=8)
    parser.add_argument("--glm-provider", default="zhipu")
    parser.add_argument("--glm-model", default="glm-5")
    parser.add_argument("--glm-api-key", default=None)
    parser.add_argument(
        "--summary-mode",
        choices=["none", "native"],
        default="none",
        help="`none` builds only base docs (`item_title`, `timeline_note`, `evidence_atom`); `native` also builds in-pipeline LLM summaries.",
    )
    parser.add_argument(
        "--allow-summary-fallback",
        action="store_true",
        help="Allow fallback summaries if native LLM summary generation fails.",
    )
    parser.add_argument(
        "--include-untranscribed",
        action="store_true",
        help="Include radios without completed transcripts in the item selection phase.",
    )
    parser.add_argument(
        "--include-noneligible",
        action="store_true",
        help="Include radios outside the eligible production ownership filter.",
    )
    parser.add_argument("--alias-map", default=None)
    parser.add_argument("--item-key", action="append", default=[])
    parser.add_argument("--item-id", action="append", default=[])


def create_api() -> GcoresAPI:
    return GcoresAPI(http=CurlHttpClient())


def handle_discover(args: argparse.Namespace) -> int:
    api = create_api()
    payload = api.list_items(
        args.content_type,
        limit=args.limit,
        offset=args.offset,
        sort=args.sort,
        include=None,
    )
    data = payload.get("data", [])
    rows = [
        {
            "id": item["id"],
            "type": item["type"],
            "title": item.get("attributes", {}).get("title"),
            "published_at": item.get("attributes", {}).get("published-at"),
        }
        for item in data
    ]
    safe_print_json(rows)
    return 0


def handle_fetch(args: argparse.Namespace) -> int:
    api = create_api()
    store = FileStore(args.output_dir)
    resolve_media = not args.no_resolve_media
    for item_id in args.item_ids:
        result = fetch_and_store_item(
            api=api,
            store=store,
            content_type=args.content_type,
            item_id=str(item_id),
            resolve_media=resolve_media,
            refresh=True,
        )
        safe_print_json(
            {
                "content_type": args.content_type,
                "id": str(item_id),
                "title": result.get("title"),
                "saved": result["saved"],
            },
            indent=None,
        )
    return 0


def handle_crawl(args: argparse.Namespace) -> int:
    api = create_api()
    store = FileStore(args.output_dir)
    resolve_media = not args.no_resolve_media
    state = create_new_state(
        types=args.types,
        page_size=args.page_size,
        max_items=args.max_items,
        start_offset=args.start_offset,
        sort=args.sort,
        resolve_media=resolve_media,
        official_radios_only=not args.include_join_podcasts,
    )
    store.save_state(state)
    return run_crawl(api=api, store=store, state=state)


def handle_resume(args: argparse.Namespace) -> int:
    store = FileStore(args.output_dir)
    if not store.has_state():
        raise SystemExit(f"No crawl state found under {store.state_path}")
    state = store.load_state()
    api = create_api()
    return run_crawl(api=api, store=store, state=state)


def handle_backfill(args: argparse.Namespace) -> int:
    api = create_api()
    store = FileStore(args.output_dir)
    resolve_media = not args.no_resolve_media
    state = create_backfill_state(
        types=args.types,
        page_size=args.page_size,
        start_offset=args.start_offset,
        sort=args.sort,
        resolve_media=resolve_media,
        official_radios_only=not args.include_join_podcasts,
        max_pages=args.max_pages or None,
        max_items=args.max_items or None,
    )
    store.save_backfill_state(state)
    return run_backfill(api=api, store=store, state=state)


def handle_resume_backfill(args: argparse.Namespace) -> int:
    store = FileStore(args.output_dir)
    if not store.has_backfill_state():
        raise SystemExit(f"No backfill state found under {store.backfill_state_path}")
    state = store.load_backfill_state()
    api = create_api()
    return run_backfill(api=api, store=store, state=state)


def handle_status(args: argparse.Namespace) -> int:
    store = FileStore(args.output_dir)
    if not store.has_state():
        safe_print_json({"output_dir": args.output_dir, "state": "missing"})
        return 0
    safe_print_json(store.load_state())
    return 0


def handle_backfill_status(args: argparse.Namespace) -> int:
    store = FileStore(args.output_dir)
    if not store.has_backfill_state():
        safe_print_json({"output_dir": args.output_dir, "state": "missing"})
        return 0
    safe_print_json(store.load_backfill_state())
    return 0


def handle_sync_latest(args: argparse.Namespace) -> int:
    api = create_api()
    store = FileStore(args.output_dir)
    summary = run_sync_latest(
        api=api,
        store=store,
        content_types=list(args.types),
        page_size=args.page_size,
        sort=args.sort,
        resolve_media=not args.no_resolve_media,
        official_radios_only=not args.include_join_podcasts,
        refresh_pages=max(0, int(args.refresh_pages)),
        max_pages=args.max_pages or None,
    )
    safe_print_json(summary)
    return 0


def handle_prepare_index(args: argparse.Namespace) -> int:
    summary = prepare_vector_inputs(
        args.output_dir,
        max_chars=args.max_chars,
        write_jsonl=not args.no_jsonl,
        use_sqlite=not args.no_sqlite,
        content_types=list(args.types),
        official_radios_only=not args.include_join_podcasts,
        include_article_speech=args.include_article_speech,
    )
    safe_print_json(summary)
    return 0


def handle_build_lexicon(args: argparse.Namespace) -> int:
    summary = build_lexicon(
        args.output_dir,
        content_types=list(args.types),
        limit=args.limit,
    )
    safe_print_json(summary)
    return 0


def handle_download_media(args: argparse.Namespace) -> int:
    summary = download_media_jobs(
        args.output_dir,
        limit=args.limit,
        max_workers=1,
        include_errors=args.include_errors,
        job_ids=list(args.job_id) or None,
        item_keys=list(args.item_key) or None,
        segment_types=list(args.segment_type) or None,
        media_types=list(args.media_type) or None,
        verbose=False,
    )
    safe_print_json(summary)
    return 0


def handle_download_media_loop(args: argparse.Namespace) -> int:
    summary = download_media_loop(
        args.output_dir,
        batch_size=args.batch_size,
        max_workers=args.max_workers,
        min_workers=args.min_workers,
        backoff_error_threshold=args.backoff_error_threshold,
        backoff_sleep_seconds=args.backoff_sleep_seconds,
        recover_after_cycles=args.recover_after_cycles,
        include_errors=args.include_errors,
        job_ids=list(args.job_id) or None,
        item_keys=list(args.item_key) or None,
        segment_types=list(args.segment_type) or None,
        media_types=list(args.media_type) or None,
        sleep_seconds=args.sleep_seconds,
        max_idle_cycles=args.max_idle_cycles,
        verbose=args.verbose,
    )
    safe_print_json(summary)
    return 0


def handle_rescue_lizhi(args: argparse.Namespace) -> int:
    statuses = list(args.status) or None
    summary = rescue_lizhi_jobs(
        args.output_dir,
        limit=args.limit,
        job_ids=list(args.job_id) or None,
        statuses=statuses,
        apply=args.apply,
        force_apply=args.force_apply,
        verbose=args.verbose,
    )
    safe_print_json(summary)
    return 0


def handle_transcribe_media(args: argparse.Namespace) -> int:
    summary = transcribe_media_jobs(
        args.output_dir,
        script=args.script,
        python_executable=args.python_executable,
        script_args=list(args.script_arg),
        limit=args.limit,
        max_chars=args.max_chars,
        job_ids=list(args.job_id) or None,
        verbose=args.verbose,
    )
    safe_print_json(summary)
    return 0


def handle_transcribe_media_loop(args: argparse.Namespace) -> int:
    summary = transcribe_media_loop(
        args.output_dir,
        script=args.script,
        python_executable=args.python_executable,
        script_args=list(args.script_arg),
        batch_size=args.batch_size,
        max_chars=args.max_chars,
        sleep_seconds=args.sleep_seconds,
        max_idle_cycles=args.max_idle_cycles,
        job_ids=list(args.job_id) or None,
        verbose=args.verbose,
    )
    safe_print_json(summary)
    return 0


def handle_transcribe_firered(args: argparse.Namespace) -> int:
    summary = transcribe_firered_jobs(
        args.output_dir,
        limit=args.limit,
        max_chars=args.max_chars,
        job_ids=list(args.job_id) or None,
        verbose=args.verbose,
        disable_lid=args.disable_lid,
        disable_punc=args.disable_punc,
        asr_batch_size=args.asr_batch_size,
        punc_batch_size=args.punc_batch_size,
    )
    safe_print_json(summary)
    return 0


def handle_transcribe_firered_loop(args: argparse.Namespace) -> int:
    summary = transcribe_firered_loop(
        args.output_dir,
        batch_size=args.batch_size,
        max_chars=args.max_chars,
        sleep_seconds=args.sleep_seconds,
        max_idle_cycles=args.max_idle_cycles,
        job_ids=list(args.job_id) or None,
        verbose=args.verbose,
        disable_lid=args.disable_lid,
        disable_punc=args.disable_punc,
        asr_batch_size=args.asr_batch_size,
        punc_batch_size=args.punc_batch_size,
    )
    safe_print_json(summary)
    return 0


def handle_transcribe_firered_rescue(args: argparse.Namespace) -> int:
    summary = transcribe_firered_rescue_jobs(
        args.output_dir,
        limit=args.limit,
        max_chars=args.max_chars,
        job_ids=list(args.job_id) or None,
        categories=list(args.category) or None,
        verbose=args.verbose,
        disable_lid=args.disable_lid,
        disable_punc=args.disable_punc,
        asr_batch_size=args.asr_batch_size,
        punc_batch_size=args.punc_batch_size,
    )
    safe_print_json(summary)
    return 0


def handle_transcribe_firered_rescue_loop(args: argparse.Namespace) -> int:
    summary = transcribe_firered_rescue_loop(
        args.output_dir,
        batch_size=args.batch_size,
        max_chars=args.max_chars,
        sleep_seconds=args.sleep_seconds,
        max_idle_cycles=args.max_idle_cycles,
        job_ids=list(args.job_id) or None,
        categories=list(args.category) or None,
        verbose=args.verbose,
        disable_lid=args.disable_lid,
        disable_punc=args.disable_punc,
        asr_batch_size=args.asr_batch_size,
        punc_batch_size=args.punc_batch_size,
    )
    safe_print_json(summary)
    return 0


def handle_submit_volcengine_idle(args: argparse.Namespace) -> int:
    summary = submit_remote_transcription_jobs(
        args.output_dir,
        script=args.script,
        python_executable=args.python_executable,
        script_args=list(args.script_arg),
        limit=args.limit,
        provider="volcengine_idle",
        job_ids=list(args.job_id) or None,
        global_hotword_file=args.global_hotword_file,
        hotword_limit=args.hotword_limit,
    )
    safe_print_json(summary)
    return 0


def handle_poll_volcengine_idle(args: argparse.Namespace) -> int:
    summary = poll_remote_transcription_jobs(
        args.output_dir,
        script=args.script,
        python_executable=args.python_executable,
        script_args=list(args.script_arg),
        limit=args.limit,
        max_chars=args.max_chars,
        provider="volcengine_idle",
        job_ids=list(args.job_id) or None,
        alias_map_path=args.alias_map,
    )
    safe_print_json(summary)
    return 0


def handle_catalog_status(args: argparse.Namespace) -> int:
    safe_print_json(Catalog(args.output_dir).stats())
    return 0


def handle_pipeline_status(args: argparse.Namespace) -> int:
    catalog = Catalog(args.output_dir)
    if args.watch_seconds and args.watch_seconds > 0:
        try:
            while True:
                safe_print_json(build_pipeline_status(catalog, failure_limit=args.failure_limit))
                time.sleep(args.watch_seconds)
        except KeyboardInterrupt:
            return 130
    safe_print_json(build_pipeline_status(catalog, failure_limit=args.failure_limit))
    return 0


def handle_runtime_status(args: argparse.Namespace) -> int:
    catalog = Catalog(args.output_dir)
    safe_print_json(build_runtime_status(catalog, failure_limit=args.failure_limit))
    return 0


def handle_build_search_index(args: argparse.Namespace) -> int:
    summary = build_search_index(
        root=args.output_dir,
        qdrant_path=args.qdrant_path,
        qdrant_url=args.qdrant_url,
        qdrant_api_key=args.qdrant_api_key,
        collection_name=args.collection_name,
        embedding_provider=args.embedding_provider,
        embedding_model=args.embedding_model,
        embedding_api_key=args.embedding_api_key,
        embedding_dimension=args.embedding_dimension,
        embedding_batch_size=args.embedding_batch_size,
        embedding_concurrency=args.embedding_concurrency,
        glm_provider=args.glm_provider,
        glm_model=args.glm_model,
        glm_api_key=args.glm_api_key,
        summary_mode=args.summary_mode,
        summary_fallback_allowed=args.allow_summary_fallback,
        eligible_only=not args.include_noneligible,
        require_transcript=not args.include_untranscribed,
        alias_map_path=args.alias_map,
        item_keys=args.item_key,
        item_ids=args.item_id,
        limit=args.limit,
        verbose=args.verbose,
    )
    safe_print_json(summary)
    return 0


def handle_sync_search_index(args: argparse.Namespace) -> int:
    reporter = get_daily_runtime_reporter()
    reporter.update(status="running", current=0, unit="items", message="starting search sync")
    summary = sync_search_index(
        root=args.output_dir,
        qdrant_path=args.qdrant_path,
        qdrant_url=args.qdrant_url,
        qdrant_api_key=args.qdrant_api_key,
        collection_name=args.collection_name,
        embedding_provider=args.embedding_provider,
        embedding_model=args.embedding_model,
        embedding_api_key=args.embedding_api_key,
        embedding_dimension=args.embedding_dimension,
        embedding_batch_size=args.embedding_batch_size,
        embedding_concurrency=args.embedding_concurrency,
        glm_provider=args.glm_provider,
        glm_model=args.glm_model,
        glm_api_key=args.glm_api_key,
        summary_mode=args.summary_mode,
        summary_fallback_allowed=args.allow_summary_fallback,
        eligible_only=not args.include_noneligible,
        require_transcript=not args.include_untranscribed,
        alias_map_path=args.alias_map,
        item_keys=args.item_key,
        item_ids=args.item_id,
        limit=args.limit,
        verbose=args.verbose,
        progress_reporter=reporter,
    )
    reporter.complete(
        message=(
            f"seen={summary.get('items_seen', 0)} indexed={summary.get('items_indexed', 0)} "
            f"errors={len(summary.get('errors', []))}"
        ),
        extra={
            "items_seen": summary.get("items_seen", 0),
            "items_indexed": summary.get("items_indexed", 0),
            "errors": len(summary.get("errors", [])),
        },
    )
    safe_print_json(summary)
    return 0


def handle_sync_search_loop(args: argparse.Namespace) -> int:
    catalog = Catalog(args.output_dir)
    current_batch_size = max(1, int(args.batch_size))
    min_batch_size = max(1, int(args.min_batch_size))
    recover_after_cycles = max(1, int(args.recover_after_cycles))
    base_backoff = max(1.0, float(args.backoff_sleep_seconds))
    max_backoff = max(base_backoff, float(args.max_backoff_seconds))
    sleep_seconds = max(0.0, float(args.sleep_seconds))
    max_idle_cycles = max(0, int(args.max_idle_cycles))

    aggregate = {
        "status": "initialized",
        "batch_size": current_batch_size,
        "min_batch_size": min_batch_size,
        "sleep_seconds": sleep_seconds,
        "backoff_sleep_seconds": base_backoff,
        "max_backoff_seconds": max_backoff,
        "recover_after_cycles": recover_after_cycles,
        "cycles": 0,
        "idle_cycles": 0,
        "success_cycles": 0,
        "items_seen": 0,
        "items_indexed": 0,
        "errors": 0,
        "backoff_count": 0,
        "last_summary": None,
    }
    stop_state = {"requested": False, "forced": False}

    previous_sigint = signal.getsignal(signal.SIGINT)

    def _handle_sigint(signum, frame):  # noqa: ARG001
        if not stop_state["requested"]:
            stop_state["requested"] = True
            if args.verbose:
                print(
                    "[search-loop] stop requested; current batch will finish before exit. "
                    "Press Ctrl+C again to force stop.",
                    flush=True,
                )
            return
        stop_state["forced"] = True
        raise KeyboardInterrupt

    signal.signal(signal.SIGINT, _handle_sigint)

    write_search_runtime_state(catalog, {**aggregate, "status": "running"})
    try:
        while True:
            if args.verbose:
                print(
                    f"[search-loop] cycle={aggregate['cycles'] + 1} starting "
                    f"batch={current_batch_size} idle={aggregate['idle_cycles']}",
                    flush=True,
                )
            summary = sync_search_index(
                root=args.output_dir,
                qdrant_path=args.qdrant_path,
                qdrant_url=args.qdrant_url,
                qdrant_api_key=args.qdrant_api_key,
                collection_name=args.collection_name,
                embedding_provider=args.embedding_provider,
                embedding_model=args.embedding_model,
                embedding_api_key=args.embedding_api_key,
                embedding_dimension=args.embedding_dimension,
                embedding_batch_size=args.embedding_batch_size,
                embedding_concurrency=args.embedding_concurrency,
                glm_provider=args.glm_provider,
                glm_model=args.glm_model,
                glm_api_key=args.glm_api_key,
                summary_mode=args.summary_mode,
                summary_fallback_allowed=args.allow_summary_fallback,
                eligible_only=not args.include_noneligible,
                require_transcript=not args.include_untranscribed,
                alias_map_path=args.alias_map,
                item_keys=args.item_key,
                item_ids=args.item_id,
                limit=current_batch_size,
                verbose=args.verbose,
            )

            aggregate["cycles"] += 1
            aggregate["items_seen"] += int(summary.get("items_seen", 0))
            aggregate["items_indexed"] += int(summary.get("items_indexed", 0))
            aggregate["errors"] += len(summary.get("errors", []))
            aggregate["last_summary"] = summary
            aggregate["batch_size"] = current_batch_size

            cycle_seen = int(summary.get("items_seen", 0))
            cycle_indexed = int(summary.get("items_indexed", 0))
            cycle_errors = summary.get("errors", []) or []

            if cycle_seen == 0:
                aggregate["idle_cycles"] += 1
                aggregate["success_cycles"] = 0
                if args.verbose:
                    print(
                        f"[search-loop] cycle={aggregate['cycles']} batch={current_batch_size} "
                        f"seen=0 indexed=0 errors=0 idle={aggregate['idle_cycles']}"
                    , flush=True)
                write_search_runtime_state(catalog, {**aggregate, "status": "running"})
                if max_idle_cycles > 0 and aggregate["idle_cycles"] >= max_idle_cycles:
                    break
                if sleep_seconds > 0:
                    time.sleep(sleep_seconds)
                continue

            aggregate["idle_cycles"] = 0

            if cycle_errors and all(is_recoverable_search_error(err.get("error", "")) for err in cycle_errors):
                aggregate["backoff_count"] += 1
                aggregate["success_cycles"] = 0
                current_batch_size = max(min_batch_size, current_batch_size // 2)
                backoff_seconds = min(max_backoff, base_backoff * (2 ** max(0, aggregate["backoff_count"] - 1)))
                if args.verbose:
                    print(
                        f"[search-loop] cycle={aggregate['cycles']} batch={current_batch_size} "
                        f"seen={cycle_seen} indexed={cycle_indexed} errors={len(cycle_errors)} "
                        f"backoff={backoff_seconds:.1f}s"
                    , flush=True)
                write_search_runtime_state(
                    catalog,
                    {
                        **aggregate,
                        "batch_size": current_batch_size,
                        "status": "running",
                        "last_backoff_seconds": backoff_seconds,
                    },
                )
                if stop_state["requested"]:
                    break
                time.sleep(backoff_seconds)
                continue

            if cycle_errors:
                aggregate["success_cycles"] = 0
            else:
                aggregate["success_cycles"] += 1
                if aggregate["backoff_count"] > 0 and aggregate["success_cycles"] >= recover_after_cycles:
                    aggregate["backoff_count"] = 0
                    current_batch_size = min(max(1, int(args.batch_size)), current_batch_size + 1)

            if args.verbose:
                print(
                    f"[search-loop] cycle={aggregate['cycles']} batch={current_batch_size} "
                    f"seen={cycle_seen} indexed={cycle_indexed} errors={len(cycle_errors)}"
                , flush=True)
            write_search_runtime_state(catalog, {**aggregate, "batch_size": current_batch_size, "status": "running"})
            if stop_state["requested"]:
                break
            if cycle_errors and sleep_seconds > 0:
                time.sleep(sleep_seconds)
    except KeyboardInterrupt:
        aggregate["status"] = "paused"
        if stop_state["forced"]:
            aggregate["forced"] = True
        write_search_runtime_state(catalog, aggregate)
        safe_print_json(aggregate)
        return 130
    finally:
        signal.signal(signal.SIGINT, previous_sigint)

    aggregate["status"] = "completed"
    if stop_state["requested"]:
        aggregate["status"] = "paused"
        aggregate["graceful_stop"] = True
    write_search_runtime_state(catalog, aggregate)
    safe_print_json(aggregate)
    return 0


def handle_bridge_search_results(args: argparse.Namespace) -> int:
    reporter = get_daily_runtime_reporter()
    reporter.update(status="running", current=0, unit="items", message="starting bridge sync")
    summary = bridge_search_results(
        root=args.output_dir,
        results_dir=args.results_dir,
        qdrant_path=args.qdrant_path,
        qdrant_url=args.qdrant_url,
        qdrant_api_key=args.qdrant_api_key,
        collection_name=args.collection_name,
        embedding_provider=args.embedding_provider,
        embedding_model=args.embedding_model,
        embedding_api_key=args.embedding_api_key,
        embedding_dimension=args.embedding_dimension,
        embedding_batch_size=args.embedding_batch_size,
        embedding_concurrency=args.embedding_concurrency,
        alias_map_path=args.alias_map,
        bridge_mode=args.bridge_mode,
        item_keys=list(args.item_key) or None,
        limit=args.limit,
        dry_run=args.dry_run,
        verbose=args.verbose,
        progress_reporter=reporter,
    )
    reporter.complete(
        message=(
            f"bridged={summary.get('bridged_count', 0)} skipped={summary.get('skipped_count', 0)} "
            f"errors={summary.get('error_count', 0)}"
        ),
        extra={
            "bridged": summary.get("bridged_count", 0),
            "skipped": summary.get("skipped_count", 0),
            "errors": summary.get("error_count", 0),
        },
    )
    safe_print_json(summary)
    return 0


def handle_bridge_search_loop(args: argparse.Namespace) -> int:
    catalog = Catalog(args.output_dir)
    batch_size = max(1, int(args.batch_size))
    sleep_seconds = max(0.0, float(args.sleep_seconds))
    base_backoff = max(1.0, float(args.backoff_sleep_seconds))
    max_backoff = max(base_backoff, float(args.max_backoff_seconds))
    max_idle_cycles = max(0, int(args.max_idle_cycles))

    aggregate = {
        "status": "initialized",
        "results_dir": args.results_dir,
        "batch_size": batch_size,
        "sleep_seconds": sleep_seconds,
        "backoff_sleep_seconds": base_backoff,
        "max_backoff_seconds": max_backoff,
        "cycles": 0,
        "idle_cycles": 0,
        "bridged": 0,
        "skipped": 0,
        "errors": 0,
        "backoff_count": 0,
        "last_summary": None,
    }
    stop_state = {"requested": False, "forced": False}

    previous_sigint = signal.getsignal(signal.SIGINT)

    def _handle_sigint(signum, frame):  # noqa: ARG001
        if not stop_state["requested"]:
            stop_state["requested"] = True
            if args.verbose:
                print(
                    "[bridge-loop] stop requested; current batch will finish before exit. "
                    "Press Ctrl+C again to force stop.",
                    flush=True,
                )
            return
        stop_state["forced"] = True
        raise KeyboardInterrupt

    signal.signal(signal.SIGINT, _handle_sigint)
    write_search_bridge_runtime_state(catalog, {**aggregate, "status": "running"})
    try:
        while True:
            if args.verbose:
                print(
                    f"[bridge-loop] cycle={aggregate['cycles'] + 1} starting "
                    f"batch={batch_size} idle={aggregate['idle_cycles']}",
                    flush=True,
                )
            summary = bridge_search_results(
                root=args.output_dir,
                results_dir=args.results_dir,
                qdrant_path=args.qdrant_path,
                qdrant_url=args.qdrant_url,
                qdrant_api_key=args.qdrant_api_key,
                collection_name=args.collection_name,
                embedding_provider=args.embedding_provider,
                embedding_model=args.embedding_model,
                embedding_api_key=args.embedding_api_key,
                embedding_dimension=args.embedding_dimension,
                embedding_batch_size=args.embedding_batch_size,
                embedding_concurrency=args.embedding_concurrency,
                alias_map_path=args.alias_map,
                bridge_mode=args.bridge_mode,
                item_keys=list(args.item_key) or None,
                limit=batch_size,
                dry_run=False,
                verbose=args.verbose,
            )

            aggregate["cycles"] += 1
            aggregate["bridged"] += int(summary.get("bridged_count", 0))
            aggregate["skipped"] += int(summary.get("skipped_count", 0))
            aggregate["errors"] += int(summary.get("error_count", 0))
            aggregate["last_summary"] = summary

            cycle_bridged = int(summary.get("bridged_count", 0))
            cycle_errors = summary.get("errors", []) or []

            if cycle_bridged == 0 and not cycle_errors:
                aggregate["idle_cycles"] += 1
                if args.verbose:
                    print(
                        f"[bridge-loop] cycle={aggregate['cycles']} bridged=0 errors=0 "
                        f"idle={aggregate['idle_cycles']}",
                        flush=True,
                    )
                write_search_bridge_runtime_state(catalog, {**aggregate, "status": "running"})
                if max_idle_cycles > 0 and aggregate["idle_cycles"] >= max_idle_cycles:
                    break
                if stop_state["requested"]:
                    break
                if sleep_seconds > 0:
                    time.sleep(sleep_seconds)
                continue

            aggregate["idle_cycles"] = 0

            if cycle_errors and all(is_recoverable_bridge_error(err.get("error", "")) for err in cycle_errors):
                aggregate["backoff_count"] += 1
                backoff_seconds = min(max_backoff, base_backoff * (2 ** max(0, aggregate["backoff_count"] - 1)))
                if args.verbose:
                    print(
                        f"[bridge-loop] cycle={aggregate['cycles']} bridged={cycle_bridged} "
                        f"errors={len(cycle_errors)} backoff={backoff_seconds:.1f}s",
                        flush=True,
                    )
                write_search_bridge_runtime_state(
                    catalog,
                    {
                        **aggregate,
                        "status": "running",
                        "last_backoff_seconds": backoff_seconds,
                    },
                )
                if stop_state["requested"]:
                    break
                time.sleep(backoff_seconds)
                continue

            if cycle_errors:
                aggregate["backoff_count"] += 1
            else:
                aggregate["backoff_count"] = 0

            if args.verbose:
                print(
                    f"[bridge-loop] cycle={aggregate['cycles']} bridged={cycle_bridged} "
                    f"errors={len(cycle_errors)}",
                    flush=True,
                )
            write_search_bridge_runtime_state(catalog, {**aggregate, "status": "running"})
            if stop_state["requested"]:
                break
            if cycle_errors and sleep_seconds > 0:
                time.sleep(sleep_seconds)
    except KeyboardInterrupt:
        aggregate["status"] = "paused"
        if stop_state["forced"]:
            aggregate["forced"] = True
        write_search_bridge_runtime_state(catalog, aggregate)
        safe_print_json(aggregate)
        return 130
    finally:
        signal.signal(signal.SIGINT, previous_sigint)

    aggregate["status"] = "completed"
    if stop_state["requested"]:
        aggregate["status"] = "paused"
        aggregate["graceful_stop"] = True
    write_search_bridge_runtime_state(catalog, aggregate)
    safe_print_json(aggregate)
    return 0


def handle_search_query(args: argparse.Namespace) -> int:
    payload = run_search_query(
        args.query,
        root=args.output_dir,
        qdrant_path=args.qdrant_path,
        qdrant_url=args.qdrant_url,
        qdrant_api_key=args.qdrant_api_key,
        collection_name=args.collection_name,
        embedding_provider=args.embedding_provider,
        embedding_model=args.embedding_model,
        embedding_api_key=args.embedding_api_key,
        embedding_dimension=args.embedding_dimension,
        embedding_batch_size=args.embedding_batch_size,
        embedding_concurrency=args.embedding_concurrency,
        alias_map_path=args.alias_map,
        limit=args.limit,
    )
    safe_print_json(payload)
    return 0


def handle_serve_search(args: argparse.Namespace) -> int:
    app = create_search_app(
        root=args.output_dir,
        qdrant_path=args.qdrant_path,
        qdrant_url=args.qdrant_url,
        qdrant_api_key=args.qdrant_api_key,
        collection_name=args.collection_name,
        embedding_provider=args.embedding_provider,
        embedding_model=args.embedding_model,
        embedding_api_key=args.embedding_api_key,
        embedding_dimension=args.embedding_dimension,
        embedding_batch_size=args.embedding_batch_size,
        embedding_concurrency=args.embedding_concurrency,
        alias_map_path=args.alias_map,
        download_url=args.download_url or None,
    )
    uvicorn.run(app, host=args.host, port=args.port)
    return 0


def handle_export_viewer_bundle(args: argparse.Namespace) -> int:
    payload = export_viewer_bundle(
        root=args.source_root,
        output_dir=args.output_dir,
        qdrant_path=args.qdrant_path,
        collection_name=args.collection_name,
        embedding_provider=args.embedding_provider,
        embedding_model=args.embedding_model,
        target_platform=args.target_platform,
        app_min_version=args.app_min_version,
        data_version=args.data_version,
        overwrite=bool(args.overwrite),
    )
    safe_print_json(payload)
    return 0


def handle_viewer_main(args: argparse.Namespace) -> int:
    argv: list[str] = [
        "--data-root",
        args.data_root,
        "--host",
        args.host,
        "--port",
        str(args.port),
        "--qdrant-url",
        args.qdrant_url,
        "--qdrant-sidecar-command",
        args.qdrant_sidecar_command,
        "--collection-name",
        args.collection_name,
        "--embedding-provider",
        args.embedding_provider,
        "--embedding-model",
        args.embedding_model,
    ]
    if args.download_url:
        argv.extend(["--download-url", args.download_url])
    if args.no_open_browser:
        argv.append("--no-open-browser")
    return viewer_main_entry(argv)


def run_crawl(*, api: GcoresAPI, store: FileStore, state: Dict[str, object]) -> int:
    state["status"] = "running"
    state["updated_at"] = utc_now_iso()
    store.save_state(state)

    summary = state["summary"]
    config = state["config"]
    resolve_media = bool(config["resolve_media"])
    official_radios_only = bool(config.get("official_radios_only", False))

    try:
        for content_type in config["types"]:
            content_state = state["types"][content_type]
            while content_state["processed"] < content_state["target"]:
                if not content_state["current_page_ids"]:
                    payload = api.list_items(
                        content_type,
                        limit=int(config["page_size"]),
                        offset=int(content_state["next_offset"]),
                        sort=str(config["sort"]),
                        include=None,
                    )
                    items = payload.get("data", [])
                    if not items:
                        content_state["done"] = True
                        break
                    ids = [
                        str(item["id"])
                        for item in items
                        if list_item_matches_scope(
                            item,
                            content_type=content_type,
                            official_radios_only=official_radios_only,
                        )
                    ]
                    if not ids:
                        content_state["next_offset"] += len(items)
                        content_state["updated_at"] = utc_now_iso()
                        state["updated_at"] = utc_now_iso()
                        store.save_state(state)
                        continue
                    content_state["current_page_ids"] = ids
                    content_state["current_page_index"] = 0
                    content_state["current_page_size"] = len(ids)
                    content_state["current_page_offset_advance"] = len(items)
                    content_state["updated_at"] = utc_now_iso()
                    state["updated_at"] = utc_now_iso()
                    store.save_state(state)

                item_id = content_state["current_page_ids"][content_state["current_page_index"]]
                result = {"content_type": content_type, "id": item_id}
                try:
                    fetched = fetch_and_store_item(
                        api=api,
                        store=store,
                        content_type=content_type,
                        item_id=item_id,
                        resolve_media=resolve_media,
                        refresh=False,
                    )
                    result["title"] = fetched.get("title")
                    result["saved"] = fetched["saved"]
                    if fetched.get("skipped"):
                        result["skipped"] = True
                    summary["items"].append(result)
                except Exception as exc:  # noqa: BLE001
                    error_record = {
                        "content_type": content_type,
                        "id": item_id,
                        "error": str(exc),
                    }
                    summary["errors"].append(error_record)
                    content_state["errors"].append(error_record)

                content_state["processed"] += 1
                content_state["current_page_index"] += 1
                content_state["updated_at"] = utc_now_iso()

                if content_state["current_page_index"] >= len(content_state["current_page_ids"]):
                    content_state["next_offset"] += int(content_state["current_page_offset_advance"])
                    content_state["current_page_ids"] = []
                    content_state["current_page_index"] = 0
                    content_state["current_page_size"] = 0
                    content_state["current_page_offset_advance"] = 0

                state["updated_at"] = utc_now_iso()
                store.save_state(state)

            content_state["done"] = True
            content_state["updated_at"] = utc_now_iso()
            state["updated_at"] = utc_now_iso()
            store.save_state(state)

    except KeyboardInterrupt:
        state["status"] = "paused"
        state["updated_at"] = utc_now_iso()
        summary["finished_at"] = utc_now_iso()
        summary["paused"] = True
        store.save_state(state)
        store.save_summary(summary)
        safe_print_json({"status": "paused", "state_file": str(store.state_path)})
        return 130

    state["status"] = "completed"
    state["updated_at"] = utc_now_iso()
    summary["finished_at"] = utc_now_iso()
    store.save_state(state)
    store.save_summary(summary)
    safe_print_json(summary)
    return 0


def create_new_state(
    *,
    types: List[str],
    page_size: int,
    max_items: int,
    start_offset: int,
    sort: str,
    resolve_media: bool,
    official_radios_only: bool,
) -> Dict[str, object]:
    now = utc_now_iso()
    return {
        "version": 1,
        "status": "initialized",
        "started_at": now,
        "updated_at": now,
        "config": {
            "types": types,
            "page_size": page_size,
            "max_items": max_items,
            "start_offset": start_offset,
            "sort": sort,
            "resolve_media": resolve_media,
            "official_radios_only": official_radios_only,
        },
        "types": {
            content_type: {
                "processed": 0,
                "target": max_items,
                "next_offset": start_offset,
                "current_page_ids": [],
                "current_page_index": 0,
                "current_page_size": 0,
                "current_page_offset_advance": 0,
                "done": False,
                "errors": [],
                "updated_at": now,
            }
            for content_type in types
        },
        "summary": {
            "started_at": now,
            "types": types,
            "page_size": page_size,
            "max_items": max_items,
            "sort": sort,
            "items": [],
            "errors": [],
        },
    }


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def list_item_matches_scope(item: dict, *, content_type: str, official_radios_only: bool) -> bool:
    return item_matches_scope(
        item,
        content_type=content_type,
        official_radios_only=official_radios_only,
    )


def safe_print_json(payload: object, indent: int | None = 2) -> None:
    text = json.dumps(payload, ensure_ascii=False, indent=indent)
    try:
        sys.stdout.write(text + "\n")
    except UnicodeEncodeError:
        fallback = json.dumps(payload, ensure_ascii=True, indent=indent)
        sys.stdout.write(fallback + "\n")


def build_pipeline_status(catalog: Catalog, *, failure_limit: int) -> dict:
    stats = catalog.stats()
    media_status = stats.get("media_status", {})
    transcript_status = stats.get("transcript_status", {})
    total_media = int(stats.get("media_jobs", 0))
    downloaded = int(media_status.get("downloaded", 0))
    unsupported = int(media_status.get("unsupported", 0))
    pending_download = int(media_status.get("pending", 0)) + int(media_status.get("error", 0))
    completed_transcripts = int(transcript_status.get("completed", 0))
    pending_transcripts = (
        int(transcript_status.get("pending", 0))
        + int(transcript_status.get("error", 0))
        + int(transcript_status.get("submitted", 0))
    )
    transcript_total = completed_transcripts + pending_transcripts
    return {
        "catalog": stats,
        "download_progress": {
            "total_media_jobs": total_media,
            "done": downloaded + unsupported,
            "downloaded": downloaded,
            "unsupported": unsupported,
            "remaining": pending_download,
            "percent": round(((downloaded + unsupported) / total_media) * 100, 2) if total_media else 0.0,
        },
        "transcript_progress": {
            "total_transcribable_jobs": transcript_total,
            "completed": completed_transcripts,
            "remaining": pending_transcripts,
            "percent": round((completed_transcripts / transcript_total) * 100, 2) if transcript_total else 0.0,
        },
        "runtime": {
            "download_loop": catalog.get_state("runtime:download_loop"),
            "transcribe_loop": catalog.get_state("runtime:transcribe_loop"),
            "search_sync_loop": catalog.get_state("runtime:search_sync_loop"),
            "search_bridge_loop": catalog.get_state("runtime:search_bridge_loop"),
        },
        "recent_failures": catalog.recent_failures(limit=failure_limit),
    }


def build_runtime_status(catalog: Catalog, *, failure_limit: int) -> dict:
    stats = catalog.stats()
    radio_audio = catalog.radio_audio_status()
    media_status = stats.get("media_status", {})
    audio_media_status = radio_audio.get("audio_media_status", {})
    audio_transcript_status = radio_audio.get("audio_transcript_status", {})

    total_media_jobs = int(stats.get("media_jobs", 0))
    total_audio_jobs = sum(int(value) for value in audio_media_status.values())
    download_done = int(media_status.get("downloaded", 0)) + int(media_status.get("unsupported", 0))
    download_remaining = int(media_status.get("pending", 0)) + int(media_status.get("error", 0))
    audio_completed = int(audio_transcript_status.get("completed", 0))
    audio_remaining = (
        int(audio_transcript_status.get("pending", 0))
        + int(audio_transcript_status.get("error", 0))
        + int(audio_transcript_status.get("submitted", 0))
    )
    return {
        "download": {
            "total_media_jobs": total_media_jobs,
            "downloaded": int(media_status.get("downloaded", 0)),
            "unsupported": int(media_status.get("unsupported", 0)),
            "remaining": download_remaining,
            "done": download_done,
            "percent": round((download_done / total_media_jobs) * 100, 2) if total_media_jobs else 0.0,
        },
        "radio_audio": {
            "total_jobs": total_audio_jobs,
            "download_status": audio_media_status,
            "transcript_status": audio_transcript_status,
            "completed": audio_completed,
            "remaining": audio_remaining,
            "percent": round((audio_completed / total_audio_jobs) * 100, 2) if total_audio_jobs else 0.0,
            "completed_audio_hours": radio_audio.get("completed_audio_hours", 0.0),
            "pending_audio_hours": radio_audio.get("pending_audio_hours", 0.0),
        },
        "runtime": {
            "download_loop": catalog.get_state("runtime:download_loop"),
            "transcribe_loop": catalog.get_state("runtime:transcribe_loop"),
            "search_bridge_loop": catalog.get_state("runtime:search_bridge_loop"),
        },
        "latest_completed_transcript": radio_audio.get("latest_completed_transcript"),
        "latest_audio_jobs": radio_audio.get("latest_audio_jobs", []),
        "recent_failures": catalog.recent_failures(limit=failure_limit),
    }


def write_search_runtime_state(catalog: Catalog, payload: dict) -> None:
    catalog.set_state("runtime:search_sync_loop", payload)


def write_search_bridge_runtime_state(catalog: Catalog, payload: dict) -> None:
    catalog.set_state("runtime:search_bridge_loop", payload)


def is_recoverable_search_error(error: str) -> bool:
    lowered = str(error or "").lower()
    tokens = (
        "429",
        "rate limit",
        "too many requests",
        "timeout",
        "timed out",
        "connection reset",
        "temporary",
        "temporarily",
        "503",
        "502",
        "504",
        "service unavailable",
        "upstream",
        "quota",
    )
    return any(token in lowered for token in tokens)


def is_recoverable_bridge_error(error: str) -> bool:
    lowered = str(error or "").lower()
    tokens = (
        "429",
        "rate limit",
        "too many requests",
        "timeout",
        "timed out",
        "connection reset",
        "temporary",
        "temporarily",
        "503",
        "502",
        "504",
        "service unavailable",
        "upstream",
        "quota",
        "readonly",
        "locked",
        "busy",
    )
    return any(token in lowered for token in tokens)


def main(argv: List[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.handler(args)
