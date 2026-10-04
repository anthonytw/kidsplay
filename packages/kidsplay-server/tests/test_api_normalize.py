"""Tests for the loudness endpoints and the loudness side of the API.

``POST/GET /api/v1/media/normalize`` (the backfill), the loudness fields on
``GET /media/{id}``, and the sync manifest leaving the kept original out.

Normalization runs on the import queue. The app's worker starts with its
lifespan, which the ASGI test client does not run, so ``_drain`` runs the
queue by hand, as the worker would.
"""

from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from kidsplay_server.api.app import create_app
from kidsplay_server.processing.loudness_backfill import LoudnessBackfill
from kidsplay_server.processing.queue_worker import drain_queue

MakeTone = Callable[..., Path]  # the ``make_tone`` fixture from conftest.py


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


async def _drain(app: FastAPI) -> int:
    """Run every queued job (the loudness normalization of what was ingested)."""
    return await drain_queue(
        app.state.db_path, app.state.media_store, app.state.importers
    )


async def _ingest(client: AsyncClient, path: Path, media_type: str = "music") -> str:
    r = await client.post(
        "/api/v1/media/ingest",
        json={
            "source_path": str(path),
            "media_type": media_type,
            "playlist_title": "Tones",
        },
    )
    assert r.status_code == 200, r.text
    result = r.json()["results"][0]
    assert result["processing_status"] == "ready", result
    return result["media_id"]


def test_app_has_a_backfill_runner(app: FastAPI) -> None:
    assert isinstance(app.state.loudness_backfill, LoudnessBackfill)


def test_create_app_takes_no_loudness_argument(tmp_path: Path) -> None:
    """Loudness targets are server settings (database + environment), not
    plumbing through ``create_app``."""
    with pytest.raises(TypeError):
        create_app(tmp_path / "a.db", tmp_path / "m", loudness=None)  # ty: ignore[unknown-argument]  # the argument was removed on purpose


async def test_ingest_returns_before_normalizing(
    client: AsyncClient, app: FastAPI, make_tone: MakeTone
) -> None:
    """Acceptance: the upload finishes with the file stored and playable; the
    normalization is a queued job, running is the worker's business."""
    media_id = await _ingest(client, make_tone("t.mp3", -18))
    item = (await client.get(f"/api/v1/media/{media_id}")).json()
    assert item["loudness_target_lufs"] is None
    files = (await client.get(f"/api/v1/media/{media_id}/files")).json()
    assert [f["file_type"] for f in files] == ["audio"]
    status = (await client.get("/api/v1/media/normalize")).json()
    assert (status["running"], status["total"]) == (True, 1)
    # Its job is progress of the audio processing, not an import.
    assert (await client.get("/api/v1/queue")).json() == []

    assert await _drain(app) == 1
    status = (await client.get("/api/v1/media/normalize")).json()
    assert (status["running"], status["normalized"]) == (False, 1)


async def test_ingest_normalizes_and_reports_loudness(
    client: AsyncClient, app: FastAPI, make_tone: MakeTone
) -> None:
    media_id = await _ingest(client, make_tone("t.mp3", -18))
    await _drain(app)
    item = (await client.get(f"/api/v1/media/{media_id}")).json()
    assert item["loudness_mode"] == "linear"
    assert item["loudness_target_lufs"] == -16.0
    assert item["loudness_target_true_peak_dbtp"] == -1.5
    assert item["loudness_source_lufs"] == pytest.approx(-39.75, abs=1.0)
    assert item["loudness_gain_db"] == pytest.approx(23.75, abs=1.0)
    files = (await client.get(f"/api/v1/media/{media_id}/files")).json()
    assert sorted(f["file_type"] for f in files) == ["audio", "audio_source"]


async def test_ingest_uses_the_loudness_environment(
    tmp_path: Path,
    make_tone: MakeTone,
    monkeypatch: pytest.MonkeyPatch,
    admin_headers_for: Callable[[FastAPI], Awaitable[dict[str, str]]],
) -> None:
    monkeypatch.setenv("KIDSPLAY_LOUDNORM", "disabled")
    app = create_app(tmp_path / "b.db", tmp_path / "mb")
    headers = await admin_headers_for(app)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test", headers=headers
    ) as c:
        media_id = await _ingest(c, make_tone("t.mp3", -18))
        item = (await c.get(f"/api/v1/media/{media_id}")).json()
    assert item["loudness_target_lufs"] is None


async def test_target_saved_in_settings_applies_to_the_next_ingest(
    client: AsyncClient, app: FastAPI, make_tone: MakeTone
) -> None:
    """Acceptance: a target changed through the settings API takes effect for
    new ingests without restarting the app (or its worker)."""
    first = await _ingest(client, make_tone("a.mp3", -18))
    await _drain(app)
    put = await client.put(
        "/api/v1/server-settings",
        json={
            "loudness_target_lufs": -20,
            "loudness_target_lufs_audiobook": -14,
        },
    )
    assert put.status_code == 200, put.text
    second = await _ingest(client, make_tone("b.mp3", -30))
    book = await _ingest(client, make_tone("c.mp3", -25), "audiobook")
    await _drain(app)

    targets = []
    for media_id in (first, second, book):
        item = (await client.get(f"/api/v1/media/{media_id}")).json()
        targets.append(item["loudness_target_lufs"])
    assert targets == [-16.0, -20.0, -14.0]


async def test_new_target_then_backfill_renormalizes_once(
    client: AsyncClient, app: FastAPI, make_tone: MakeTone
) -> None:
    """Acceptance (#34): change the target in the settings, run the library
    backfill, and the existing item moves to it; a second run leaves it be."""
    media_id = await _ingest(client, make_tone("a.mp3", -18))
    await _drain(app)
    item = (await client.get(f"/api/v1/media/{media_id}")).json()
    assert item["loudness_target_lufs"] == -16.0

    put = await client.put(
        "/api/v1/server-settings", json={"loudness_target_lufs_music": -20}
    )
    assert put.status_code == 200, put.text
    assert (
        await client.post("/api/v1/media/normalize", json={"all": True})
    ).status_code == 202
    await _drain(app)
    status = (await client.get("/api/v1/media/normalize")).json()
    assert (status["normalized"], status["skipped"]) == (1, 0)
    item = (await client.get(f"/api/v1/media/{media_id}")).json()
    assert item["loudness_target_lufs"] == -20.0

    assert (
        await client.post("/api/v1/media/normalize", json={"all": True})
    ).status_code == 202
    await _drain(app)
    status = (await client.get("/api/v1/media/normalize")).json()
    assert (status["normalized"], status["skipped"]) == (0, 1)
    item = (await client.get(f"/api/v1/media/{media_id}")).json()
    assert item["loudness_target_lufs"] == -20.0


async def test_environment_target_wins_over_a_saved_one(
    client: AsyncClient,
    app: FastAPI,
    make_tone: MakeTone,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("KIDSPLAY_LOUDNESS_TARGET_LUFS", "-18")
    assert (
        await client.put("/api/v1/server-settings", json={"loudness_target_lufs": -12})
    ).status_code == 409
    media_id = await _ingest(client, make_tone("a.mp3", -30))
    await _drain(app)
    item = (await client.get(f"/api/v1/media/{media_id}")).json()
    assert item["loudness_target_lufs"] == -18.0


class TestNormalizeEndpoints:
    async def test_status_before_any_run(self, client: AsyncClient) -> None:
        r = await client.get("/api/v1/media/normalize")
        assert r.status_code == 200
        body = r.json()
        assert body["running"] is False and body["started_at"] is None

    async def test_all_twice(
        self, client: AsyncClient, app: FastAPI, make_tone: MakeTone
    ) -> None:
        media_id = await _ingest(client, make_tone("t.mp3", -18))
        await _drain(app)  # the job the upload queued

        r = await client.post("/api/v1/media/normalize", json={"all": True})
        assert r.status_code == 202
        assert r.json()["total"] == 1
        assert r.json()["running"] is True
        await _drain(app)
        status = (await client.get("/api/v1/media/normalize")).json()
        # Ingest's job already normalized it, so the backfill skips it.
        assert (status["skipped"], status["normalized"]) == (1, 0)
        assert status["running"] is False

        r = await client.post("/api/v1/media/normalize", json={"media_ids": [media_id]})
        assert r.status_code == 202
        await _drain(app)
        assert (await client.get("/api/v1/media/normalize")).json()["skipped"] == 1

    @pytest.mark.parametrize(
        "body",
        [
            {},
            {"all": False},
            {"all": True, "media_ids": ["00000000-0000-0000-0000-000000000001"]},
            {"media_ids": ["not-a-uuid"]},
        ],
    )
    async def test_bad_request(self, client: AsyncClient, body: dict) -> None:
        r = await client.post("/api/v1/media/normalize", json=body)
        assert r.status_code == 422

    async def test_backfill_while_uploads_are_normalizing_is_not_refused(
        self, client: AsyncClient, app: FastAPI, make_tone: MakeTone
    ) -> None:
        """The queue takes both: no 409, and no item is queued twice."""
        await _ingest(client, make_tone("a.mp3", -18))
        r = await client.post("/api/v1/media/normalize", json={"all": True})
        assert r.status_code == 202
        assert r.json()["total"] == 1  # the upload's job, not a second one
        await _ingest(client, make_tone("b.mp3", -12, frequency=550))
        r = await client.post("/api/v1/media/normalize", json={"all": True})
        assert r.status_code == 202
        assert r.json()["total"] == 2
        assert await _drain(app) == 2
        status = (await client.get("/api/v1/media/normalize")).json()
        assert (status["normalized"], status["failed"], status["running"]) == (
            2,
            0,
            False,
        )

    async def test_requires_admin(self, anon_client: AsyncClient) -> None:
        assert (await anon_client.get("/api/v1/media/normalize")).status_code == 401
        r = await anon_client.post("/api/v1/media/normalize", json={"all": True})
        assert r.status_code == 401


async def test_manifest_sends_normalized_audio_not_original(
    client: AsyncClient, app: FastAPI, make_tone: MakeTone
) -> None:
    media_id = await _ingest(client, make_tone("t.mp3", -18))
    await _drain(app)
    files = {
        f["file_type"]: f
        for f in (await client.get(f"/api/v1/media/{media_id}/files")).json()
    }
    profile = (await client.post("/api/v1/profiles", json={"name": "Kid"})).json()
    r = await client.post(
        f"/api/v1/media/{media_id}/assign", json={"profile_ids": [profile["id"]]}
    )
    assert r.status_code in (200, 201), r.text
    device = (
        await client.post(
            "/api/v1/devices", json={"name": "Pi", "profile_id": profile["id"]}
        )
    ).json()

    r = await client.get(
        f"/api/v1/devices/{device['id']}/manifest",
        headers={"Authorization": f"Bearer {device['api_key']}"},
    )
    assert r.status_code == 200, r.text
    manifest = r.json()
    hashes = {f["content_hash"] for f in manifest["files"]}
    assert files["audio"]["content_hash"] in hashes
    assert files["audio_source"]["content_hash"] not in hashes
    assert manifest["media"][0]["audio_path"] == files["audio"]["relative_path"]
