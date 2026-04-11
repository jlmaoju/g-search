from __future__ import annotations

from datetime import datetime, timezone
from typing import Dict, List, Optional

from .api import GcoresAPI, default_include_for
from .catalog import Catalog
from .daily_runtime import get_daily_runtime_reporter
from .normalize import normalize_document
from .storage import FileStore


RECENT_ACTIVITY_LIMIT = 50


def create_backfill_state(
    *,
    types: List[str],
    page_size: int,
    start_offset: int,
    sort: str,
    resolve_media: bool,
    official_radios_only: bool,
    max_pages: Optional[int],
    max_items: Optional[int],
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
            "start_offset": start_offset,
            "sort": sort,
            "resolve_media": resolve_media,
            "official_radios_only": official_radios_only,
            "max_pages": max_pages,
            "max_items": max_items,
        },
        "types": {
            content_type: {
                "next_offset": start_offset,
                "pages_seen": 0,
                "pages_with_matches": 0,
                "processed": 0,
                "saved": 0,
                "skipped": 0,
                "current_page_ids": [],
                "current_page_index": 0,
                "current_page_size": 0,
                "current_page_offset_advance": 0,
                "seeded_watermark": False,
                "watermark": None,
                "done": False,
                "exhausted": False,
                "limit_reached": False,
                "errors": [],
                "updated_at": now,
            }
            for content_type in types
        },
        "summary": {
            "started_at": now,
            "types": types,
            "page_size": page_size,
            "start_offset": start_offset,
            "sort": sort,
            "pages_seen": 0,
            "processed": 0,
            "saved": 0,
            "skipped": 0,
            "error_count": 0,
            "recent_items": [],
            "recent_errors": [],
        },
    }


def run_backfill(*, api: GcoresAPI, store: FileStore, state: Dict[str, object]) -> int:
    catalog = Catalog(str(store.root))
    state["status"] = "running"
    state["updated_at"] = utc_now_iso()
    store.save_backfill_state(state)

    summary = state["summary"]
    config = state["config"]
    resolve_media = bool(config["resolve_media"])
    official_radios_only = bool(config.get("official_radios_only", False))

    try:
        for content_type in config["types"]:
            content_state = state["types"][content_type]
            if content_state.get("done"):
                continue

            while not content_state.get("done"):
                if backfill_limit_reached(content_state=content_state, config=config):
                    content_state["limit_reached"] = True
                    content_state["updated_at"] = utc_now_iso()
                    state["updated_at"] = utc_now_iso()
                    store.save_backfill_state(state)
                    break

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
                        content_state["exhausted"] = True
                        content_state["updated_at"] = utc_now_iso()
                        state["updated_at"] = utc_now_iso()
                        store.save_backfill_state(state)
                        break

                    matched_items = [
                        item
                        for item in items
                        if list_item_matches_scope(
                            item,
                            content_type=content_type,
                            official_radios_only=official_radios_only,
                        )
                    ]

                    content_state["pages_seen"] += 1
                    summary["pages_seen"] += 1

                    if matched_items and not content_state.get("seeded_watermark"):
                        watermark = build_page_watermark(matched_items)
                        if watermark is not None:
                            content_state["seeded_watermark"] = True
                            content_state["watermark"] = watermark
                            catalog.set_watermark(
                                content_type=content_type,
                                official_radios_only=official_radios_only,
                                published_at=str(watermark["published_at"]),
                                item_ids=[str(item_id) for item_id in watermark["item_ids"]],
                            )

                    if matched_items:
                        content_state["pages_with_matches"] += 1

                    matched_ids = [str(item["id"]) for item in matched_items]
                    content_state["current_page_ids"] = matched_ids
                    content_state["current_page_index"] = 0
                    content_state["current_page_size"] = len(matched_ids)
                    content_state["current_page_offset_advance"] = len(items)
                    content_state["updated_at"] = utc_now_iso()
                    state["updated_at"] = utc_now_iso()
                    store.save_backfill_state(state)

                    if not matched_ids:
                        content_state["next_offset"] += len(items)
                        content_state["current_page_offset_advance"] = 0
                        content_state["updated_at"] = utc_now_iso()
                        state["updated_at"] = utc_now_iso()
                        store.save_backfill_state(state)
                        continue

                item_id = content_state["current_page_ids"][content_state["current_page_index"]]
                try:
                    result = fetch_and_store_item(
                        api=api,
                        store=store,
                        content_type=content_type,
                        item_id=item_id,
                        resolve_media=resolve_media,
                        refresh=False,
                    )
                    content_state["processed"] += 1
                    summary["processed"] += 1

                    if result["saved"]:
                        content_state["saved"] += 1
                        summary["saved"] += 1
                    else:
                        content_state["skipped"] += 1
                        summary["skipped"] += 1

                    append_recent(
                        summary["recent_items"],
                        {
                            "content_type": content_type,
                            "id": item_id,
                            "title": result.get("title"),
                            "saved": result["saved"],
                            "updated_at": utc_now_iso(),
                        },
                    )
                except Exception as exc:  # noqa: BLE001
                    error_record = {
                        "content_type": content_type,
                        "id": item_id,
                        "error": str(exc),
                        "updated_at": utc_now_iso(),
                    }
                    content_state["errors"].append(error_record)
                    append_recent(summary["recent_errors"], error_record)
                    summary["error_count"] += 1
                finally:
                    content_state["current_page_index"] += 1
                    content_state["updated_at"] = utc_now_iso()

                    if content_state["current_page_index"] >= len(content_state["current_page_ids"]):
                        content_state["next_offset"] += int(content_state["current_page_offset_advance"])
                        content_state["current_page_ids"] = []
                        content_state["current_page_index"] = 0
                        content_state["current_page_size"] = 0
                        content_state["current_page_offset_advance"] = 0

                    state["updated_at"] = utc_now_iso()
                    store.save_backfill_state(state)

        if all(bool(state["types"][content_type]["done"]) for content_type in config["types"]):
            state["status"] = "completed"
        else:
            state["status"] = "paused"
        state["updated_at"] = utc_now_iso()
        summary["finished_at"] = utc_now_iso()
        store.save_backfill_state(state)
        store.save_summary(summary)
        return 0
    except KeyboardInterrupt:
        state["status"] = "paused"
        state["updated_at"] = utc_now_iso()
        summary["finished_at"] = utc_now_iso()
        summary["paused"] = True
        store.save_backfill_state(state)
        store.save_summary(summary)
        return 130


def run_sync_latest(
    *,
    api: GcoresAPI,
    store: FileStore,
    content_types: List[str],
    page_size: int,
    sort: str,
    resolve_media: bool,
    official_radios_only: bool,
    refresh_pages: int,
    max_pages: Optional[int],
) -> dict:
    catalog = Catalog(str(store.root))
    reporter = get_daily_runtime_reporter()
    now = utc_now_iso()
    summary = {
        "status": "synced",
        "started_at": now,
        "finished_at": None,
        "types": content_types,
        "page_size": page_size,
        "sort": sort,
        "refresh_pages": refresh_pages,
        "max_pages": max_pages,
        "official_radios_only": official_radios_only,
        "pages_seen": 0,
        "processed": 0,
        "saved": 0,
        "skipped": 0,
        "refreshed": 0,
        "error_count": 0,
        "recent_items": [],
        "recent_errors": [],
        "watermarks": {},
    }

    for content_type in content_types:
        offset = 0
        pages_seen = 0
        estimated_total_pages = max(1, int(max_pages or 0)) if max_pages is not None else max(1, int(refresh_pages) + 1)
        watermark = catalog.get_watermark(
            content_type=content_type,
            official_radios_only=official_radios_only,
        )
        candidate_watermark = None

        while True:
            if max_pages is not None and pages_seen >= max_pages:
                break

            reporter.update(
                status="running",
                current=summary["pages_seen"],
                total=estimated_total_pages,
                unit="pages",
                message=(
                    f"{content_type} page={pages_seen + 1} "
                    f"processed={summary['processed']} saved={summary['saved']} skipped={summary['skipped']}"
                ),
                extra={
                    "content_type": content_type,
                    "pages_seen": summary["pages_seen"],
                    "processed": summary["processed"],
                    "saved": summary["saved"],
                    "skipped": summary["skipped"],
                    "errors": summary["error_count"],
                },
            )

            payload = api.list_items(
                content_type,
                limit=page_size,
                offset=offset,
                sort=sort,
                include=None,
            )
            items = payload.get("data", [])
            if not items:
                break

            matched_items = [
                item
                for item in items
                if list_item_matches_scope(
                    item,
                    content_type=content_type,
                    official_radios_only=official_radios_only,
                )
            ]
            refresh_known = pages_seen < refresh_pages
            stale_page = bool(watermark) and not refresh_known

            if candidate_watermark is None and matched_items:
                candidate_watermark = build_page_watermark(matched_items)

            for item in matched_items:
                item_id = str(item["id"])
                published_at = item.get("attributes", {}).get("published-at")
                known = store.normalized_exists(content_type, item_id)
                at_or_before = item_at_or_before_watermark(
                    item_id=item_id,
                    published_at=published_at,
                    watermark=watermark,
                )

                if not (known and at_or_before):
                    stale_page = False

                should_fetch = (not known) or refresh_known or (not at_or_before)
                if not should_fetch:
                    summary["skipped"] += 1
                    append_recent(
                        summary["recent_items"],
                        {
                            "content_type": content_type,
                            "id": item_id,
                            "saved": False,
                            "skipped": True,
                            "updated_at": utc_now_iso(),
                        },
                    )
                    continue

                try:
                    result = fetch_and_store_item(
                        api=api,
                        store=store,
                        content_type=content_type,
                        item_id=item_id,
                        resolve_media=resolve_media,
                        refresh=True,
                    )
                    summary["processed"] += 1
                    if result["saved"]:
                        summary["saved"] += 1
                    else:
                        summary["skipped"] += 1
                    if known:
                        summary["refreshed"] += 1
                    append_recent(
                        summary["recent_items"],
                        {
                            "content_type": content_type,
                            "id": item_id,
                            "title": result.get("title"),
                            "saved": result["saved"],
                            "refreshed": known,
                            "updated_at": utc_now_iso(),
                        },
                    )
                except Exception as exc:  # noqa: BLE001
                    summary["error_count"] += 1
                    append_recent(
                        summary["recent_errors"],
                        {
                            "content_type": content_type,
                            "id": item_id,
                            "error": str(exc),
                            "updated_at": utc_now_iso(),
                        },
                    )

            pages_seen += 1
            summary["pages_seen"] += 1
            estimated_total_pages = max(estimated_total_pages, summary["pages_seen"] + 1)
            offset += len(items)

            if matched_items and stale_page:
                break

        selected_watermark = candidate_watermark or watermark
        if selected_watermark is not None:
            catalog.set_watermark(
                content_type=content_type,
                official_radios_only=official_radios_only,
                published_at=str(selected_watermark["published_at"]),
                item_ids=[str(item_id) for item_id in selected_watermark["item_ids"]],
            )
            summary["watermarks"][content_type] = selected_watermark

    summary["finished_at"] = utc_now_iso()
    reporter.complete(
        message=(
            f"pages={summary['pages_seen']} processed={summary['processed']} "
            f"saved={summary['saved']} skipped={summary['skipped']} errors={summary['error_count']}"
        ),
        extra={
            "pages_seen": summary["pages_seen"],
            "processed": summary["processed"],
            "saved": summary["saved"],
            "skipped": summary["skipped"],
            "errors": summary["error_count"],
        },
    )
    return summary


def fetch_and_store_item(
    *,
    api: GcoresAPI,
    store: FileStore,
    content_type: str,
    item_id: str,
    resolve_media: bool,
    refresh: bool,
) -> dict:
    exists = store.normalized_exists(content_type, item_id)
    if exists and not refresh:
        return {
            "content_type": content_type,
            "id": item_id,
            "title": None,
            "saved": False,
            "skipped": True,
        }

    payload = api.get_item(
        content_type,
        item_id,
        include=default_include_for(content_type),
    )
    normalized = normalize_document(payload, resolve_media=resolve_media, api=api)
    store.save_raw(content_type, item_id, payload)
    store.save_normalized(content_type, item_id, normalized)
    return {
        "content_type": content_type,
        "id": item_id,
        "title": normalized.get("title"),
        "saved": True,
        "skipped": False,
    }


def backfill_limit_reached(*, content_state: dict, config: dict) -> bool:
    max_pages = config.get("max_pages")
    if max_pages is not None and int(content_state["pages_seen"]) >= int(max_pages):
        return True
    max_items = config.get("max_items")
    if max_items is not None and int(content_state["processed"]) >= int(max_items):
        return True
    return False


def build_page_watermark(items: List[dict]) -> Optional[dict]:
    if not items:
        return None
    first_published_at = items[0].get("attributes", {}).get("published-at")
    if not first_published_at:
        return None
    item_ids: List[str] = []
    for item in items:
        published_at = item.get("attributes", {}).get("published-at")
        if published_at != first_published_at:
            break
        item_ids.append(str(item["id"]))
    return {
        "published_at": first_published_at,
        "item_ids": item_ids,
    }


def item_at_or_before_watermark(*, item_id: str, published_at: Optional[str], watermark: Optional[dict]) -> bool:
    if watermark is None:
        return False
    watermark_published_at = watermark.get("published_at")
    if not watermark_published_at or not published_at:
        return False
    if published_at < watermark_published_at:
        return True
    if published_at > watermark_published_at:
        return False
    return str(item_id) in {str(existing_id) for existing_id in watermark.get("item_ids", [])}


def list_item_matches_scope(item: dict, *, content_type: str, official_radios_only: bool) -> bool:
    if content_type != "radios" or not official_radios_only:
        return True
    attrs = item.get("attributes", {})
    owner_type = attrs.get("owner-type")
    if owner_type:
        return owner_type == "gcores"
    return bool(attrs.get("option-is-official") or attrs.get("is-official"))


def append_recent(bucket: List[dict], record: dict) -> None:
    bucket.append(record)
    overflow = len(bucket) - RECENT_ACTIVITY_LIMIT
    if overflow > 0:
        del bucket[:overflow]


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
