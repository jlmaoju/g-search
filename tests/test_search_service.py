import asyncio
import io
import threading
import unittest
from email.message import Message
from types import SimpleNamespace
from unittest.mock import Mock, patch

import httpx
from fastapi import HTTPException

from gcores_crawler.search_service import (
    MediaAssetRedirectHandler,
    create_search_app,
    fetch_media_asset_bytes,
    validate_media_asset_url,
)


class MediaValidationTests(unittest.TestCase):
    def test_host_boundary_and_redirect_are_checked(self):
        validate_media_asset_url("https://alioss.gcores.com/cover.png")
        for url in ("https://fakegcores.com/a", "https://gcores.com.evil.example/a", "file:///tmp/a", "https://user@gcores.com/a", "https://gcores.com:1234/a"):
            with self.subTest(url=url), self.assertRaises(HTTPException):
                validate_media_asset_url(url)
        with self.assertRaises(HTTPException):
            MediaAssetRedirectHandler().redirect_request(None, None, 302, "Found", {}, "http://127.0.0.1/private")

    def test_image_type_and_size_are_bounded(self):
        for content_type, content, expected in (("image/png", b"png", None), ("text/html", b"html", 415), ("image/jpeg", b"12345", 413)):
            with self.subTest(content_type=content_type, content=content):
                response = io.BytesIO(content)
                response.headers = Message()
                response.headers["Content-Type"] = content_type
                opener = Mock()
                opener.open.return_value = response
                with patch("gcores_crawler.search_service.build_opener", return_value=opener), patch("gcores_crawler.search_service.MAX_MEDIA_ASSET_BYTES", 4):
                    if expected:
                        with self.assertRaises(HTTPException) as caught:
                            fetch_media_asset_bytes("https://gcores.com/cover.png")
                        self.assertEqual(caught.exception.status_code, expected)
                    else:
                        self.assertEqual(fetch_media_asset_bytes("https://gcores.com/cover.png"), (content, content_type))


class ServiceRegressionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.app = create_search_app()
        self.app.state.catalog = Mock()
        self.app.state.qdrant = SimpleNamespace(collection_exists=lambda: True)
        self.app.state.search_config = SimpleNamespace(collection_name="fixture", root="fixture")
        self.app.state.mode = "server"
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url="http://fixture")

    async def asyncTearDown(self):
        await self.client.aclose()

    async def test_old_limited_snapshot_is_completed_by_participants_api(self):
        people = [{"name": f"host-{i}", "count": 1} for i in range(645)]
        self.app.state.catalog.list_radio_participants.return_value = people
        snapshot = {"participants_count": 645, "participants": people[:160], "program_types": [{"name": "news"}]}
        with patch("gcores_crawler.search_service._collect_cached_meta_snapshot", return_value=snapshot):
            response = await self.client.get("/api/participants")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.json()["participants"]), 645)
        self.app.state.catalog.list_radio_participants.assert_called_once_with(limit=None)

    async def test_slow_asset_does_not_hold_up_health_request(self):
        started = threading.Event()
        release = threading.Event()
        main_thread = threading.get_ident()

        def slow_image(_url):
            self.assertNotEqual(threading.get_ident(), main_thread)
            started.set()
            release.wait(3)
            return b"png", "image/png"

        with patch("gcores_crawler.search_service.fetch_media_asset_bytes", side_effect=slow_image):
            asset = asyncio.create_task(self.client.get("/api/media-asset?url=https://gcores.com/image.png"))
            try:
                self.assertTrue(await asyncio.to_thread(started.wait, 2))
                health = await asyncio.wait_for(self.client.get("/api/health"), timeout=.5)
                self.assertEqual(health.status_code, 200)
                self.assertFalse(asset.done())
            finally:
                release.set()
                response = await asset
            self.assertEqual(response.content, b"png")


if __name__ == "__main__":
    unittest.main()
