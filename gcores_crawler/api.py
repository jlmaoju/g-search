from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Iterable, Optional
from urllib.parse import urlencode, urljoin

from .http import CurlHttpClient


BASE_URL = "https://www.gcores.com"
JSON_API_ACCEPT = "application/vnd.api+json"


DEFAULT_INCLUDES: Dict[str, str] = {
    "articles": "user,category,tags",
    "radios": "media,media.timelines,user,category,tags,albums,djs,published-albums,latest-album",
    "videos": "media,user,category,tags,albums,published-albums",
    "albums": "tags,published-radios.media,published-videos.media",
    "talks": "user,tags",
    "topics": "",
    "games": "tags",
}


@dataclass
class GcoresAPI:
    http: CurlHttpClient

    def list_items(
        self,
        content_type: str,
        *,
        limit: int,
        offset: int,
        sort: Optional[str],
        include: Optional[str] = None,
    ) -> dict:
        params = {
            "page[limit]": limit,
            "page[offset]": offset,
        }
        if sort:
            params["sort"] = sort
        if include:
            params["include"] = include
        return self.http.get_json(
            self._build_url(f"/gapi/v1/{content_type}", params),
            headers={"Accept": JSON_API_ACCEPT},
        )

    def get_item(
        self,
        content_type: str,
        item_id: str,
        *,
        include: Optional[str] = None,
    ) -> dict:
        params = {}
        if include:
            params["include"] = include
        return self.http.get_json(
            self._build_url(f"/gapi/v1/{content_type}/{item_id}", params),
            headers={"Accept": JSON_API_ACCEPT},
        )

    def get_json_by_url(self, url: str, *, accept: Optional[str] = None) -> dict:
        headers = {}
        if accept:
            headers["Accept"] = accept
        return self.http.get_json(url, headers=headers)

    def resolve_taptap_playlist(self, playlist_url: str) -> dict:
        return self.get_json_by_url(absolute_url(playlist_url), accept="application/json, */*")

    def resolve_video_play_auth(self, video_id: str) -> dict:
        url = self._build_url(
            f"/gapi/v1/medias/protected/videos/{video_id}/play-auth",
            {},
        )
        return self.get_json_by_url(url, accept="application/json, */*")

    def _build_url(self, path: str, params: Dict[str, object]) -> str:
        url = urljoin(BASE_URL, path)
        if not params:
            return url
        return f"{url}?{urlencode(params)}"


def absolute_url(value: str) -> str:
    if value.startswith("http://") or value.startswith("https://"):
        return value
    return urljoin(BASE_URL, value)


def default_include_for(content_type: str) -> str:
    return DEFAULT_INCLUDES.get(content_type, "")


def core_content_types() -> Iterable[str]:
    return ("articles", "radios")
