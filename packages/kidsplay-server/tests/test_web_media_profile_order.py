"""The media page lists each row's profile tags in name order.

Without an ``ORDER BY`` the tags follow the (random) profile UUIDs, so they
could flip between runs, which made the README screenshot unstable.
"""

import html
import io
import json
import re
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from PIL import Image

from kidsplay_server.api.app import create_app

# Created in reverse order, so neither creation order nor name order is luck.
NAMES = ["Zed", "Yara", "Mia", "Leo", "Ada", "Abe"]


@pytest.fixture
def app(tmp_path: Path) -> FastAPI:
    return create_app(tmp_path / "test.db", tmp_path / "media")


@pytest.fixture
async def client(
    app: FastAPI, admin_headers: dict[str, str]
) -> AsyncIterator[AsyncClient]:
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers=admin_headers,
    ) as c:
        yield c


async def test_profile_tags_are_sorted_by_name(
    client: AsyncClient, tmp_path: Path
) -> None:
    ids = []
    for name in NAMES:
        r = await client.post("/api/v1/profiles", json={"name": name})
        assert r.status_code == 201, r.text
        ids.append(r.json()["id"])
    photo = tmp_path / "p.png"
    buf = io.BytesIO()
    Image.new("RGB", (64, 64), (10, 120, 200)).save(buf, format="PNG")
    photo.write_bytes(buf.getvalue())
    r = await client.post(
        "/api/v1/media/ingest",
        json={
            "source_path": str(photo),
            "media_type": "photo",
            "playlist_title": "Pics",
            "profile_ids": ids,
        },
    )
    assert r.status_code == 200, r.text

    page = await client.get("/media")
    assert page.status_code == 200
    tagged = re.search(r'data-profiles="([^"]*)"', page.text)
    assert tagged is not None
    assert json.loads(html.unescape(tagged.group(1))) == sorted(NAMES)
    # The visible tags follow the same order.
    shown = re.findall(r'<span class="tag"[^>]*>\s*([^<]+?)\s*</span>', page.text)
    assert [n for n in shown if n in NAMES][: len(NAMES)] == sorted(NAMES)
