"""Web UI for pairing: the approval partial on the Devices page and the
``pairing_enabled`` checkbox on the settings page."""

import secrets
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from kidsplay_server.api.app import create_app


@pytest.fixture
def app(tmp_path: Path) -> FastAPI:
    return create_app(tmp_path / "test.db", tmp_path / "media")


@pytest.fixture
async def admin(
    app: FastAPI, admin_headers: dict[str, str]
) -> AsyncIterator[AsyncClient]:
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers=admin_headers,
    ) as c:
        yield c


async def request_pairing(
    client: AsyncClient, name: str = "Blue one", code: str = "ABCD2345"
) -> None:
    r = await client.post(
        "/api/v1/pairing",
        json={
            "code": code,
            "binding_secret": secrets.token_urlsafe(32),
            "device_name": name,
        },
    )
    assert r.status_code == 201


class TestDevicesPage:
    async def test_offers_no_one_click_action_per_request(
        self, admin: AsyncClient, anon_client: AsyncClient
    ) -> None:
        """Any host on the LAN can register a request under any name, so the
        page must not list requests with a button that fills in the approval:
        the parent has to type the code shown on the handheld."""
        await request_pairing(anon_client, "Mallory Tablet", code="WXYZ6789")
        html = (await admin.get("/devices")).text
        assert "Pair a device" in html
        assert "/api/v1/pairing/approve" in html
        # Neither the code nor the attacker-chosen name is on the page.
        assert "WXYZ-6789" not in html
        assert "WXYZ6789" not in html
        assert "Mallory Tablet" not in html
        assert "pickPairing" not in html
        assert "data-code" not in html
        assert "Waiting for approval" not in html
        # An informational count is fine.
        assert "1 device is waiting" in html
        assert "handheld's screen" in html

    async def test_qr_link_shows_what_is_being_approved(
        self, admin: AsyncClient, anon_client: AsyncClient
    ) -> None:
        """The QR link pre-fills the code, and shows the device's reported name
        and screen size so the parent decides after seeing them; approving is
        still a separate press."""
        await request_pairing(anon_client, "Blue one")
        html = (await admin.get("/devices", params={"pair": "abcd-2345"})).text
        assert 'id="pair-code" value="ABCD-2345"' in html
        assert "Blue one" in html
        assert "640×480" in html
        assert "Check that the code below matches" in html
        assert html.count("/api/v1/pairing/approve") == 1

    async def test_qr_link_for_an_unknown_code_says_so(
        self, admin: AsyncClient
    ) -> None:
        html = (await admin.get("/devices", params={"pair": "ABCD-2345"})).text
        assert "No device is waiting with that code" in html

    async def test_untrusted_device_name_is_escaped(
        self, admin: AsyncClient, anon_client: AsyncClient
    ) -> None:
        """A device names itself, unauthenticated: it must not inject markup."""
        await request_pairing(anon_client, '"><script>alert(1)</script>')
        html = (await admin.get("/devices", params={"pair": "ABCD2345"})).text
        assert "<script>alert(1)</script>" not in html
        assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html

    async def test_qr_link_prefills_the_code(self, admin: AsyncClient) -> None:
        html = (await admin.get("/devices", params={"pair": "abcd-2345"})).text
        assert 'id="pair-code" value="ABCD-2345"' in html

    async def test_bad_prefill_is_ignored(self, admin: AsyncClient) -> None:
        html = (await admin.get("/devices", params={"pair": '"><b>'})).text
        assert 'id="pair-code" value=""' in html

    async def test_qr_link_requires_login(self, anon_client: AsyncClient) -> None:
        r = await anon_client.get("/devices", params={"pair": "ABCD-2345"})
        assert r.status_code == 303
        assert r.headers["location"].startswith("/login?next=")
        assert "pair%3DABCD-2345" in r.headers["location"]

    async def test_disabled_shows_a_hint(self, admin: AsyncClient) -> None:
        await admin.put("/api/v1/server-settings", json={"pairing_enabled": False})
        html = (await admin.get("/devices")).text
        assert "Pairing is turned off" in html
        assert 'id="pair-code"' not in html

    async def test_spanish(self, admin: AsyncClient) -> None:
        html = (await admin.get("/devices", params={"lang": "es"})).text
        assert "Vincular un dispositivo" in html
        assert "Código que aparece en la pantalla del dispositivo" in html


class TestSettingsPage:
    async def test_checkbox_reflects_and_saves(self, admin: AsyncClient) -> None:
        html = (await admin.get("/settings")).text
        assert 'type="checkbox" id="setting-pairing_enabled"' in html
        assert "checked" in html.split('id="setting-pairing_enabled"')[1][:120]
        await admin.put("/api/v1/server-settings", json={"pairing_enabled": False})
        html = (await admin.get("/settings")).text
        assert "checked" not in html.split('id="setting-pairing_enabled"')[1][:120]
        assert "Reset to default (on)" in html
