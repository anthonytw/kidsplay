"""Tests for the importer and import-queue API endpoints.

The app is built with only the built-in importers (as when the yt-dlp plugin
is not installed) plus, where needed, a fake importer, so the results do not
depend on which plugins happen to be installed.
"""

import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path

import aiosqlite
import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from kidsplay_models.media import MediaType
from kidsplay_models.queue import QueueStatus
from kidsplay_server.api.app import create_app
from kidsplay_server.database import (
    configure_conn,
    create_loudness_job,
    list_queue_items,
    update_queue_item,
)
from kidsplay_server.importers import (
    BaseImporter,
    FetchContext,
    FetchedItem,
    ImporterRegistry,
    ImportPreview,
    builtin_importers,
)


class _TubeImporter(BaseImporter):
    """Stands in for a plugin that normalizes URLs and needs the queue."""

    name = "tube"
    label = "Tube"
    requires_queue = True

    def can_handle(self, source: str) -> bool:
        return source.startswith("https://tube.example/")

    def normalize(self, source: str) -> str:
        return source.split("&", 1)[0]

    async def fetch(
        self, source: str, workdir: Path, ctx: FetchContext
    ) -> list[FetchedItem]:
        raise NotImplementedError


@pytest.fixture
def app(tmp_path: Path) -> FastAPI:
    registry = ImporterRegistry([_TubeImporter(), *builtin_importers()])
    return create_app(tmp_path / "test.db", tmp_path / "media", importers=registry)


@pytest.fixture
def core_app(tmp_path: Path) -> FastAPI:
    registry = ImporterRegistry(builtin_importers())
    return create_app(tmp_path / "core.db", tmp_path / "media", importers=registry)


@pytest.fixture
async def client(
    app: FastAPI, admin_headers: dict[str, str]
) -> AsyncIterator[AsyncClient]:
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test", headers=admin_headers
    ) as c:
        yield c


@pytest.fixture
async def core_client(
    core_app: FastAPI,
    admin_headers_for: Callable[[FastAPI], Awaitable[dict[str, str]]],
) -> AsyncIterator[AsyncClient]:
    async with AsyncClient(
        transport=ASGITransport(app=core_app),
        base_url="http://test",
        headers=await admin_headers_for(core_app),
    ) as c:
        yield c


_QUEUE_BODY = {"media_type": "music", "playlist_title": "Mix"}


# ---------------------------------------------------------------------------
# GET /importers, /importers/match
# ---------------------------------------------------------------------------


async def test_list_importers_core_only(core_client: AsyncClient) -> None:
    resp = await core_client.get("/api/v1/importers")
    assert resp.status_code == 200
    assert resp.json() == [
        {
            "name": "local",
            "label": "Local file or folder",
            "requires_queue": False,
            "supports_preview": False,
        },
        {
            "name": "http",
            "label": "Web URL",
            "requires_queue": False,
            "supports_preview": False,
        },
    ]


async def test_list_importers_includes_plugins_first(client: AsyncClient) -> None:
    resp = await client.get("/api/v1/importers")
    assert [i["name"] for i in resp.json()] == ["tube", "local", "http"]


async def test_match_importer(client: AsyncClient) -> None:
    resp = await client.get(
        "/api/v1/importers/match", params={"source": "https://tube.example/v=1"}
    )
    assert resp.status_code == 200
    assert resp.json()["name"] == "tube"

    resp = await client.get(
        "/api/v1/importers/match", params={"source": "https://example.com/a.mp3"}
    )
    assert resp.json()["name"] == "http"


async def test_match_importer_none(client: AsyncClient) -> None:
    resp = await client.get("/api/v1/importers/match", params={"source": "gopher://x"})
    assert resp.status_code == 404
    assert resp.json()["error_code"] == "NO_IMPORTER"


async def test_match_youtube_without_plugin_needs_the_plugin(
    core_client: AsyncClient,
) -> None:
    """A YouTube URL is not silently matched to the generic http importer."""
    resp = await core_client.get(
        "/api/v1/importers/match",
        params={"source": "https://www.youtube.com/watch?v=abc"},
    )
    assert resp.status_code == 422
    body = resp.json()
    assert body["error_code"] == "PLUGIN_REQUIRED"
    assert "yt-dlp plugin" in body["detail"]


# ---------------------------------------------------------------------------
# POST /media/preview without a previewing importer
# ---------------------------------------------------------------------------


async def test_preview_without_plugin_is_unsupported(core_client: AsyncClient) -> None:
    """Without the yt-dlp plugin a YouTube preview is refused, not crashed."""
    resp = await core_client.post(
        "/api/v1/media/preview",
        json={"url": "https://www.youtube.com/watch?v=abc"},
    )
    assert resp.status_code == 422
    assert resp.json()["error_code"] == "PREVIEW_UNSUPPORTED"


# ---------------------------------------------------------------------------
# POST /queue
# ---------------------------------------------------------------------------


async def test_queue_resolves_and_normalizes(client: AsyncClient) -> None:
    resp = await client.post(
        "/api/v1/queue",
        json={"url": "https://tube.example/v=1&junk=2", **_QUEUE_BODY},
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["importer"] == "tube"
    assert body["url"] == "https://tube.example/v=1"
    assert body["status"] == "pending"

    listed = await client.get("/api/v1/queue")
    assert [i["id"] for i in listed.json()] == [body["id"]]


async def test_queue_explicit_importer(client: AsyncClient) -> None:
    resp = await client.post(
        "/api/v1/queue",
        json={"url": "https://example.com/a.mp3", "importer": "http", **_QUEUE_BODY},
    )
    assert resp.status_code == 201
    assert resp.json()["importer"] == "http"


async def test_queue_unknown_importer(client: AsyncClient) -> None:
    resp = await client.post(
        "/api/v1/queue",
        json={"url": "https://example.com/a.mp3", "importer": "nope", **_QUEUE_BODY},
    )
    assert resp.status_code == 422
    assert resp.json()["error_code"] == "UNKNOWN_IMPORTER"


async def test_queue_no_importer(client: AsyncClient) -> None:
    resp = await client.post(
        "/api/v1/queue", json={"url": "gopher://x/y", **_QUEUE_BODY}
    )
    assert resp.status_code == 422
    assert resp.json()["error_code"] == "NO_IMPORTER"


@pytest.mark.parametrize(
    "url",
    [
        "https://www.youtube.com/watch?v=abc",
        "https://youtu.be/abc",
        "https://music.youtube.com/watch?v=abc",
        "https://www.youtube-nocookie.com/embed/abc",
    ],
)
async def test_queue_youtube_without_plugin_is_refused(
    core_client: AsyncClient, url: str
) -> None:
    """No job is queued for a source only the missing plugin can fetch."""
    resp = await core_client.post("/api/v1/queue", json={"url": url, **_QUEUE_BODY})
    assert resp.status_code == 422
    assert resp.json()["error_code"] == "PLUGIN_REQUIRED"
    assert (await core_client.get("/api/v1/queue")).json() == []


async def test_queue_youtube_refused_even_with_http_named(
    core_client: AsyncClient,
) -> None:
    resp = await core_client.post(
        "/api/v1/queue",
        json={
            "url": "https://www.youtube.com/watch?v=abc",
            "importer": "http",
            **_QUEUE_BODY,
        },
    )
    assert resp.status_code == 422
    assert resp.json()["error_code"] == "PLUGIN_REQUIRED"


async def test_ingest_youtube_url_without_plugin_reports_the_plugin(
    core_client: AsyncClient,
) -> None:
    resp = await core_client.post(
        "/api/v1/media/ingest",
        json={
            "source_url": "https://www.youtube.com/watch?v=abc",
            "media_type": "music",
            "playlist_title": "Mix",
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["failed"] == 1
    assert "yt-dlp plugin" in body["results"][0]["errors"][0]


async def test_queue_item_lifecycle(client: AsyncClient) -> None:
    created = await client.post(
        "/api/v1/queue", json={"url": "https://example.com/a.mp3", **_QUEUE_BODY}
    )
    item_id = created.json()["id"]

    got = await client.get(f"/api/v1/queue/{item_id}")
    assert got.status_code == 200
    # Only failed or cancelled items can be retried.
    retry = await client.post(f"/api/v1/queue/{item_id}/retry")
    assert retry.status_code == 409

    assert (await client.delete(f"/api/v1/queue/{item_id}")).status_code == 204
    assert (await client.get(f"/api/v1/queue/{item_id}")).status_code == 404


# ---------------------------------------------------------------------------
# Web import page
# ---------------------------------------------------------------------------


async def test_loudness_jobs_are_out_of_reach_of_the_queue_api(
    client: AsyncClient, tmp_path: Path
) -> None:
    """A loudness job is backfill progress, not an import: get, retry and
    delete answer 404 and leave the row alone."""
    await client.get("/api/v1/queue")  # the app has initialised its database
    media_id = uuid.uuid4()
    async with aiosqlite.connect(tmp_path / "test.db") as conn:
        await configure_conn(conn)
        job = await create_loudness_job(conn, media_id, MediaType.MUSIC)
        assert job is not None
        await update_queue_item(conn, job.id, status=QueueStatus.FAILED)
        await conn.commit()

    assert (await client.get(f"/api/v1/queue/{job.id}")).status_code == 404
    assert (await client.post(f"/api/v1/queue/{job.id}/retry")).status_code == 404
    assert (await client.delete(f"/api/v1/queue/{job.id}")).status_code == 404
    assert (await client.get("/api/v1/queue")).json() == []

    async with aiosqlite.connect(tmp_path / "test.db") as conn:
        await configure_conn(conn)
        (row,) = await list_queue_items(conn, include_loudness=True)
    assert (row.id, row.status) == (job.id, QueueStatus.FAILED)


async def test_import_page_without_preview_importer_hides_tab(
    core_client: AsyncClient,
) -> None:
    """No yt-dlp plugin: the page renders with no YouTube/preview tab."""
    resp = await core_client.get("/media/import")
    assert resp.status_code == 200
    assert 'id="tab-youtube"' not in resp.text
    assert "YouTube" not in resp.text
    assert 'id="tab-photo"' in resp.text


async def test_import_page_labels_tab_from_importers(
    tmp_path: Path,
    admin_headers_for: Callable[[FastAPI], Awaitable[dict[str, str]]],
) -> None:
    class _Previewing(_TubeImporter):
        async def preview(self, source: str, max_items: int) -> ImportPreview:
            raise NotImplementedError

    registry = ImporterRegistry([_Previewing(), *builtin_importers()])
    app = create_app(tmp_path / "t.db", tmp_path / "media", importers=registry)
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers=await admin_headers_for(app),
    ) as c:
        resp = await c.get("/media/import")
    assert resp.status_code == 200
    assert 'id="tab-youtube"' in resp.text
    assert "Tube URL (video or playlist)" in resp.text
