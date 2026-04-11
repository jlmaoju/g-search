from __future__ import annotations

import json
from typing import Dict, List, Optional, Tuple

from .api import BASE_URL, absolute_url


IMAGE_BASE = "https://image.gcores.com"
ALIOSS_AUDIO_BASE = "https://alioss.gcores.com/uploads/audio"


def normalize_document(
    payload: dict,
    *,
    resolve_media: bool,
    api=None,
) -> dict:
    data = payload["data"]
    included = payload.get("included", [])
    included_index = index_included(included)

    item_type = str(data["type"])
    item_id = str(data["id"])
    attrs = data.get("attributes", {})

    normalized = {
        "id": item_id,
        "type": item_type,
        "url": public_entry_url(item_type, item_id),
        "title": attrs.get("title"),
        "excerpt": attrs.get("excerpt"),
        "desc": attrs.get("desc"),
        "published_at": attrs.get("published-at"),
        "created_at": attrs.get("created-at"),
        "duration": attrs.get("duration"),
        "is_free": attrs.get("is-free"),
        "is_require_privilege": attrs.get("is-require-privilege"),
        "is_limited_free": attrs.get("is-limited-free"),
        "owner_type": attrs.get("owner-type"),
        "option_is_official": attrs.get("option-is-official"),
        "is_official": attrs.get("is-official"),
        "likes_count": attrs.get("likes-count"),
        "comments_count": attrs.get("comments-count"),
        "plays": attrs.get("plays"),
        "cover_url": image_asset_url(attrs.get("cover")),
        "thumb_url": image_asset_url(attrs.get("thumb")),
        "speech_audio_url": audio_asset_url(attrs.get("speech-path")),
        "content_text": draftjs_to_text(attrs.get("content")),
        "content_blocks": draftjs_to_blocks(attrs.get("content")),
        "category": related_single_name(data, included_index, "category"),
        "tags": related_many_names(data, included_index, "tags"),
        "users": related_users(data, included_index),
        "albums": related_records(data, included_index, "albums"),
        "djs": related_records(data, included_index, "djs"),
    }

    normalized["media"] = related_media(
        parent=data,
        included_index=included_index,
        resolve_media=resolve_media,
        api=api,
    )

    if item_type == "albums":
        normalized["album"] = {
            "display_type": attrs.get("display-type"),
            "content_type": attrs.get("content-type"),
            "credit": attrs.get("credit"),
            "chapters_count": attrs.get("chapters-count"),
            "purchase_notes": attrs.get("purchase-notes"),
            "author": attrs.get("author"),
            "information": attrs.get("information"),
        }
        normalized["chapters"] = related_album_chapters(
            parent=data,
            included_index=included_index,
            resolve_media=resolve_media,
            api=api,
        )

    return normalized


def index_included(included: List[dict]) -> Dict[Tuple[str, str], dict]:
    index: Dict[Tuple[str, str], dict] = {}
    for item in included:
        index[(str(item["type"]), str(item["id"]))] = item
    return index


def draftjs_to_blocks(content: Optional[str]) -> List[dict]:
    if not content:
        return []
    try:
        parsed = json.loads(content)
    except (TypeError, json.JSONDecodeError):
        return []
    blocks = parsed.get("blocks", [])
    if not isinstance(blocks, list):
        return []
    return blocks


def draftjs_to_text(content: Optional[str]) -> str:
    blocks = draftjs_to_blocks(content)
    lines = [str(block.get("text", "")).strip() for block in blocks]
    lines = [line for line in lines if line]
    return "\n".join(lines)


def relationship_data(parent: dict, name: str):
    relationship = parent.get("relationships", {}).get(name, {})
    return relationship.get("data")


def related_single_name(parent: dict, included_index: Dict[Tuple[str, str], dict], name: str) -> Optional[str]:
    rel = relationship_data(parent, name)
    if not rel:
        return None
    key = (str(rel["type"]), str(rel["id"]))
    item = included_index.get(key)
    if not item:
        return None
    attrs = item.get("attributes", {})
    return attrs.get("name") or attrs.get("title")


def related_many_names(parent: dict, included_index: Dict[Tuple[str, str], dict], name: str) -> List[str]:
    rels = relationship_data(parent, name) or []
    output: List[str] = []
    for rel in rels:
        item = included_index.get((str(rel["type"]), str(rel["id"])))
        if not item:
            continue
        attrs = item.get("attributes", {})
        label = attrs.get("name") or attrs.get("title")
        if label:
            output.append(label)
    return output


def related_users(parent: dict, included_index: Dict[Tuple[str, str], dict]) -> List[dict]:
    output: List[dict] = []
    names = ("user", "djs")
    for name in names:
        rel = relationship_data(parent, name)
        if not rel:
            continue
        rels = rel if isinstance(rel, list) else [rel]
        for item_ref in rels:
            item = included_index.get((str(item_ref["type"]), str(item_ref["id"])))
            if not item:
                continue
            attrs = item.get("attributes", {})
            output.append(
                {
                    "id": str(item["id"]),
                    "type": str(item["type"]),
                    "nickname": attrs.get("nickname"),
                    "thumb_url": image_asset_url(attrs.get("thumb")),
                    "is_official": attrs.get("is-gcores-official"),
                }
            )
    return dedupe_records(output, ("type", "id"))


def related_records(parent: dict, included_index: Dict[Tuple[str, str], dict], name: str) -> List[dict]:
    rels = relationship_data(parent, name) or []
    if not isinstance(rels, list):
        rels = [rels]
    output: List[dict] = []
    for item_ref in rels:
        item = included_index.get((str(item_ref["type"]), str(item_ref["id"])))
        if not item:
            continue
        attrs = item.get("attributes", {})
        output.append(
            {
                "id": str(item["id"]),
                "type": str(item["type"]),
                "title": attrs.get("title"),
                "name": attrs.get("name"),
            }
        )
    return output


def related_media(parent: dict, included_index: Dict[Tuple[str, str], dict], resolve_media: bool, api=None) -> List[dict]:
    rel = relationship_data(parent, "media")
    if not rel:
        return []
    rels = rel if isinstance(rel, list) else [rel]
    parent_attrs = parent.get("attributes", {})
    output: List[dict] = []
    for media_ref in rels:
        media = included_index.get((str(media_ref["type"]), str(media_ref["id"])))
        if not media:
            continue
        output.append(
            normalize_media(
                parent_type=str(parent["type"]),
                parent_id=str(parent["id"]),
                parent_is_free=bool(parent_attrs.get("is-free")),
                media=media,
                included_index=included_index,
                resolve_media=resolve_media,
                api=api,
            )
        )
    return output


def normalize_media(
    parent_type: str,
    parent_id: str,
    parent_is_free: bool,
    media: dict,
    included_index: Dict[Tuple[str, str], dict],
    resolve_media: bool,
    api=None,
) -> dict:
    attrs = media.get("attributes", {})
    media_type = attrs.get("media-type")
    audio_value = attrs.get("audio")
    normalized = {
        "id": str(media["id"]),
        "type": str(media["type"]),
        "media_type": media_type,
        "title": attrs.get("title"),
        "duration": attrs.get("duration"),
        "created_at": attrs.get("created-at"),
        "audio_url": audio_asset_url(audio_value),
        "original_src": absolute_url(attrs["original-src"]) if attrs.get("original-src") else None,
        "playlist_url": absolute_url(attrs["playlist"]) if attrs.get("playlist") else None,
        "vod_file_id": attrs.get("vod-file-id"),
        "process_state": attrs.get("process-state"),
        "timelines": related_media_timelines(media, included_index),
    }

    if not resolve_media or api is None:
        return normalized

    if media_type == "taptap" and normalized["playlist_url"]:
        try:
            resolved = api.resolve_taptap_playlist(normalized["playlist_url"])
            normalized["resolved"] = {
                "provider": "taptap",
                "m3u8": resolved.get("m3u8"),
                "expires_at": resolved.get("expires-at"),
            }
        except Exception as exc:  # noqa: BLE001
            normalized["resolve_error"] = str(exc)
    elif media_type == "vod" and parent_type == "videos" and parent_is_free:
        try:
            resolved = api.resolve_video_play_auth(parent_id)
            normalized["resolved"] = {
                "provider": "vod",
                "play_auth_url": f"{BASE_URL}/gapi/v1/medias/protected/videos/{parent_id}/play-auth",
                "play_auth": resolved.get("play-auth"),
                "duration": resolved.get("duration"),
                "cover_url": resolved.get("cover-url"),
            }
        except Exception as exc:  # noqa: BLE001
            normalized["resolve_error"] = str(exc)

    return normalized


def related_media_timelines(media: dict, included_index: Dict[Tuple[str, str], dict]) -> List[dict]:
    rels = relationship_data(media, "timelines") or []
    if not isinstance(rels, list):
        rels = [rels]

    output: List[dict] = []
    for timeline_ref in rels:
        timeline = included_index.get((str(timeline_ref["type"]), str(timeline_ref["id"])))
        if not timeline:
            continue
        attrs = timeline.get("attributes", {})
        output.append(
            {
                "id": str(timeline["id"]),
                "type": str(timeline["type"]),
                "at": attrs.get("at"),
                "title": attrs.get("title"),
                "content": attrs.get("content"),
                "asset_url": image_asset_url(attrs.get("asset")),
                "quote_href": attrs.get("quote-href"),
                "comments_count": attrs.get("comments-count"),
            }
        )

    output.sort(key=lambda item: int(item.get("at") or 0))
    return output


def related_album_chapters(parent: dict, included_index: Dict[Tuple[str, str], dict], resolve_media: bool, api=None) -> List[dict]:
    rels = relationship_data(parent, "published-radios") or relationship_data(parent, "radios") or []
    output: List[dict] = []
    for rel in rels:
        radio = included_index.get((str(rel["type"]), str(rel["id"])))
        if not radio:
            continue
        attrs = radio.get("attributes", {})
        chapter = {
            "id": str(radio["id"]),
            "type": str(radio["type"]),
            "url": public_entry_url("radios", str(radio["id"])),
            "title": attrs.get("title"),
            "excerpt": attrs.get("excerpt"),
            "published_at": attrs.get("published-at"),
            "duration": attrs.get("duration"),
            "is_free": attrs.get("is-free"),
            "is_listable": attrs.get("is-listable"),
            "content_text": draftjs_to_text(attrs.get("content")),
            "media": related_media(
                parent=radio,
                included_index=included_index,
                resolve_media=resolve_media,
                api=api,
            ),
        }
        output.append(chapter)
    return output


def public_entry_url(item_type: str, item_id: str) -> Optional[str]:
    mapping = {
        "articles": f"{BASE_URL}/articles/{item_id}",
        "radios": f"{BASE_URL}/radios/{item_id}",
        "videos": f"{BASE_URL}/videos/{item_id}",
        "albums": f"{BASE_URL}/albums/{item_id}",
        "talks": f"{BASE_URL}/talks/{item_id}",
        "topics": f"{BASE_URL}/topics/{item_id}",
        "games": f"{BASE_URL}/games/{item_id}",
    }
    return mapping.get(item_type)


def image_asset_url(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    if value.startswith("http://") or value.startswith("https://"):
        return value
    return f"{IMAGE_BASE}/{value}"


def audio_asset_url(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    if value.startswith("http://") or value.startswith("https://"):
        return value
    return f"{ALIOSS_AUDIO_BASE}/{value}"


def dedupe_records(records: List[dict], key_fields: Tuple[str, ...]) -> List[dict]:
    seen = set()
    output = []
    for record in records:
        key = tuple(record.get(field) for field in key_fields)
        if key in seen:
            continue
        seen.add(key)
        output.append(record)
    return output
