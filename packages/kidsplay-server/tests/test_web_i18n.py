"""Web UI localization: language choice, the picker, and Spanish pages."""

import itertools
import json
import re
import shutil
import subprocess
from collections.abc import AsyncIterator, Callable
from html.parser import HTMLParser
from pathlib import Path

import aiosqlite
import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from kidsplay_models import MediaItem, MediaType, ProcessingStatus, ProfileSettings
from kidsplay_server import i18n
from kidsplay_server.api.app import create_app
from kidsplay_server.database import configure_conn, create_media_item
from kidsplay_server.web.routes import loudness_label


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


@pytest.fixture
async def browser(
    app: FastAPI, admin_headers: dict[str, str]
) -> AsyncIterator[AsyncClient]:
    """A cookie-keeping client with no credentials, like a fresh browser.

    The admin password is set (``admin_headers`` seeds it), so ``/login`` renders.
    """
    del admin_headers
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as c:
        yield c


def html_lang(html: str) -> str:
    match = re.search(r'<html lang="([^"]*)"', html)
    assert match
    return match.group(1)


class _VisibleText(HTMLParser):
    """The text a person would read: no scripts, styles or tags."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        del attrs
        if tag in {"script", "style"}:
            self._skip += 1

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style"}:
            self._skip -= 1

    def handle_data(self, data: str) -> None:
        if not self._skip and data.strip():
            self.parts.append(" ".join(data.split()))


def visible_text(html: str) -> str:
    parser = _VisibleText()
    parser.feed(html)
    return "\n".join(parser.parts)


class TestLanguageChoice:
    async def test_defaults_to_english(self, browser: AsyncClient) -> None:
        r = await browser.get("/login")
        assert html_lang(r.text) == "en"
        assert "Admin password" in r.text

    async def test_accept_language_header(self, browser: AsyncClient) -> None:
        r = await browser.get("/login", headers={"Accept-Language": "es-MX,es;q=0.9"})
        assert html_lang(r.text) == "es"
        assert "Contraseña de administrador" in r.text

    async def test_unsupported_accept_language_falls_back_to_english(
        self, browser: AsyncClient
    ) -> None:
        r = await browser.get("/login", headers={"Accept-Language": "fr,de;q=0.5"})
        assert html_lang(r.text) == "en"

    async def test_query_parameter_wins_and_is_remembered(
        self, browser: AsyncClient
    ) -> None:
        first = await browser.get("/login", params={"lang": "es"})
        assert html_lang(first.text) == "es"
        assert "kidsplay_lang=es" in first.headers["set-cookie"]
        # No parameter now: the cookie keeps Spanish, even against the header.
        again = await browser.get("/login", headers={"Accept-Language": "en"})
        assert html_lang(again.text) == "es"
        # And it can be switched back.
        back = await browser.get("/login", params={"lang": "en"})
        assert html_lang(back.text) == "en"
        assert "kidsplay_lang=en" in back.headers["set-cookie"]

    async def test_cookie_beats_the_header(self, browser: AsyncClient) -> None:
        browser.cookies.set("kidsplay_lang", "en")
        r = await browser.get("/login", headers={"Accept-Language": "es"})
        assert html_lang(r.text) == "en"

    async def test_unsupported_query_parameter_is_ignored(
        self, browser: AsyncClient
    ) -> None:
        r = await browser.get("/login", params={"lang": "klingon"})
        assert html_lang(r.text) == "en"
        assert "kidsplay_lang" not in r.headers.get("set-cookie", "")

    async def test_picker_offers_every_language_and_keeps_the_page(
        self, client: AsyncClient
    ) -> None:
        html = (await client.get("/media", params={"q": "abc"})).text
        assert 'lang="es" hreflang="es"' in html
        assert "Español" in html
        assert "English" in html
        links = re.findall(r'<a href="([^"]*lang=[^"]*)"', html)
        assert links
        assert all("q=abc" in link for link in links)
        assert 'aria-current="true"' in html

    async def test_picker_is_on_public_pages_too(self, browser: AsyncClient) -> None:
        assert "hreflang" in (await browser.get("/login")).text


_INLINE_SCRIPT = re.compile(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", re.S)

PAGES = [
    "/",
    "/media",
    "/media/import",
    "/queue",
    "/profiles",
    "/devices",
    "/logs",
    "/tokens",
    "/settings",
]


def english_ids() -> list[str]:
    """Multi-word English message ids of the web catalog."""
    from babel.messages import pofile

    path = i18n.LOCALE_DIR / f"{i18n.DOMAIN}.pot"
    with path.open("rb") as handle:
        catalog = pofile.read_po(handle)
    return [
        m.id
        for m in catalog
        if isinstance(m.id, str)
        and len(m.id.split()) >= 2
        and "%" not in m.id
        and "{" not in m.id
        and "<" not in m.id
    ]


class TestSpanishPages:
    @pytest.mark.parametrize("path", PAGES)
    async def test_page_renders_in_spanish_without_english_text(
        self, client: AsyncClient, path: str
    ) -> None:
        spanish = await client.get(path, params={"lang": "es"})
        assert spanish.status_code == 200
        assert html_lang(spanish.text) == "es"
        text = visible_text(spanish.text)
        leaked = [m for m in english_ids() if m in text]
        assert not leaked, f"English text on the Spanish {path}: {leaked}"
        english = await client.get(path, params={"lang": "en"})
        assert visible_text(english.text) != text

    async def test_profile_settings_page(self, client: AsyncClient) -> None:
        pid = (await client.post("/api/v1/profiles", json={"name": "Leo"})).json()["id"]
        r = await client.get(f"/profiles/{pid}/settings", params={"lang": "es"})
        assert r.status_code == 200
        text = visible_text(r.text)
        assert "Volumen máximo" in text
        assert not [m for m in english_ids() if m in text]

    async def test_setup_errors_are_translated(self, tmp_path: Path) -> None:
        fresh = create_app(tmp_path / "fresh.db", tmp_path / "fresh-media")
        async with (
            fresh.router.lifespan_context(fresh),
            AsyncClient(
                transport=ASGITransport(app=fresh), base_url="http://test"
            ) as c,
        ):
            page = await c.get("/setup", params={"lang": "es"})
            assert "Te damos la bienvenida a KidsPlay" in page.text
            token = re.search(r'name="csrf-token" content="([^"]+)"', page.text)
            assert token
            r = await c.post(
                "/setup",
                data={
                    "password": "long-enough-password",
                    "password_confirm": "something-else-entirely",
                    "csrf_token": token.group(1),
                    "setup_code": fresh.state.setup_code.code,
                },
            )
        assert r.status_code == 400
        assert "Las contraseñas no coinciden." in r.text
        assert "do not match" not in r.text

    async def test_login_error_is_translated(self, browser: AsyncClient) -> None:
        page = await browser.get("/login", params={"lang": "es"})
        token = re.search(r'name="csrf-token" content="([^"]+)"', page.text)
        assert token
        r = await browser.post(
            "/login",
            data={"password": "wrong", "csrf_token": token.group(1), "next": "/"},
        )
        assert r.status_code == 401
        assert "Contraseña incorrecta." in r.text

    async def test_label_helpers_follow_the_request_language(
        self, client: AsyncClient
    ) -> None:
        assert loudness_label("music", None, None, None) == "Loudness: not normalized"
        i18n.use_language("es")
        try:
            assert (
                loudness_label("music", None, None, None) == "Sonoridad: sin normalizar"
            )
        finally:
            i18n.use_language("en")


class TestProfileLanguageSetting:
    async def test_new_profile_is_english(self, client: AsyncClient) -> None:
        pid = (await client.post("/api/v1/profiles", json={"name": "Leo"})).json()["id"]
        r = await client.get(f"/api/v1/profiles/{pid}/settings")
        assert r.json()["language"] == "en"

    async def test_profile_without_stored_settings_has_no_language(
        self, client: AsyncClient, app: FastAPI
    ) -> None:
        """A profile from before language existed: nothing stored."""
        import aiosqlite

        from kidsplay_models import Profile
        from kidsplay_server.database import create_profile

        profile = Profile(name="Old")
        async with aiosqlite.connect(app.state.db_path) as conn:
            await create_profile(conn, profile)
            await conn.commit()
        r = await client.get(f"/api/v1/profiles/{profile.id}/settings")
        assert r.json()["language"] is None
        page = await client.get(f"/profiles/{profile.id}/settings")
        assert '<option value="es" selected>' in page.text

    async def test_language_round_trips(self, client: AsyncClient) -> None:
        pid = (await client.post("/api/v1/profiles", json={"name": "Leo"})).json()["id"]
        url = f"/api/v1/profiles/{pid}/settings"
        r = await client.put(url, json={"language": "es"})
        assert r.status_code == 200
        assert (await client.get(url)).json()["language"] == "es"
        page = await client.get(f"/profiles/{pid}/settings")
        assert '<option value="es" selected>' in page.text
        assert "language: document.getElementById('device-language').value" in page.text

    async def test_unsupported_language_is_rejected(self, client: AsyncClient) -> None:
        pid = (await client.post("/api/v1/profiles", json={"name": "Leo"})).json()["id"]
        url = f"/api/v1/profiles/{pid}/settings"
        r = await client.put(url, json={"language": "klingon"})
        assert r.status_code == 422
        assert r.json()["error_code"] == "UNSUPPORTED_LANGUAGE"
        assert (await client.get(url)).json()["language"] == "en"

    async def test_language_is_delivered_in_the_device_manifest(
        self, client: AsyncClient
    ) -> None:
        pid = (await client.post("/api/v1/profiles", json={"name": "Leo"})).json()["id"]
        device = (
            await client.post("/api/v1/devices", json={"name": "GB", "profile_id": pid})
        ).json()
        await client.put(
            f"/api/v1/profiles/{pid}/settings",
            json=ProfileSettings(language="es").model_dump(mode="json"),
        )
        r = await client.get(
            f"/api/v1/devices/{device['id']}/manifest",
            headers={"Authorization": f"Bearer {device['api_key']}"},
        )
        assert r.status_code == 200
        assert r.json()["profile_settings"]["language"] == "es"


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
class TestRenderedScripts:
    """Wrapping strings must not break the pages' JavaScript."""

    @pytest.mark.parametrize("language", ["en", "es"])
    async def test_every_script_parses(
        self, client: AsyncClient, tmp_path: Path, language: str
    ) -> None:
        pid = (await client.post("/api/v1/profiles", json={"name": "Leo"})).json()["id"]
        failures: list[str] = []
        for path in [*PAGES, f"/profiles/{pid}/settings"]:
            html = (await client.get(path, params={"lang": language})).text
            scripts = re.findall(
                r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", html, re.S
            )
            for index, source in enumerate(scripts):
                script = tmp_path / f"{language}-{index}.js"
                script.write_text(source)
                checked = subprocess.run(
                    ["node", "--check", str(script)], capture_output=True, text=True
                )
                if checked.returncode:
                    failures.append(f"{path} script {index}: {checked.stderr[:300]}")
        assert not failures, "\n".join(failures)


async def test_media_page_labels_follow_the_request_language(
    client: AsyncClient, make_tone: Callable[..., Path]
) -> None:
    """Labels built in Python (not in the template) use the request's language."""
    r = await client.post(
        "/api/v1/media/ingest",
        json={
            "source_path": str(make_tone("t.mp3", -18)),
            "media_type": "music",
            "playlist_title": "Tones",
        },
    )
    assert r.status_code == 200, r.text
    spanish = (await client.get("/media", params={"lang": "es"})).text
    assert 'data-loudness="Sonoridad: ' in spanish
    assert "Loudness:" not in spanish
    english = (await client.get("/media", params={"lang": "en"})).text
    assert 'data-loudness="Loudness: ' in english


_RAW_ENUM_WORDS = {
    *(m.value for m in MediaType),
    *(p.value for p in ProcessingStatus),
}

_SPANISH_TYPES = {
    MediaType.MUSIC: "Música",
    MediaType.AUDIOBOOK: "Audiolibro",
    MediaType.PHOTO: "Foto",
}

_SPANISH_STATUSES = {
    ProcessingStatus.PENDING: "Pendiente",
    ProcessingStatus.PROCESSING: "Procesando",
    ProcessingStatus.READY: "Listo",
    ProcessingStatus.FAILED: "Con error",
}


async def _seed_every_type_and_status(app: FastAPI) -> None:
    async with aiosqlite.connect(app.state.db_path) as conn:
        await configure_conn(conn)
        for n, (media_type, status) in enumerate(
            itertools.product(MediaType, ProcessingStatus)
        ):
            await create_media_item(
                conn,
                MediaItem(
                    media_type=media_type,
                    content_hash=f"{n:064x}",
                    playlist_title="Grupo",
                    title=f"Pieza {n}",
                    artist="Artista",
                    processing_status=status.value,
                ),
            )
        await conn.commit()


class TestEnumLabels:
    """Enum values shown in tables are translated; raw values stay in data-*."""

    @pytest.mark.parametrize("group", ["playlist", "artist", "none"])
    async def test_media_table_shows_spanish_labels(
        self, app: FastAPI, client: AsyncClient, group: str
    ) -> None:
        await _seed_every_type_and_status(app)
        r = await client.get("/media", params={"lang": "es", "group_by": group})
        assert r.status_code == 200
        lines = set(visible_text(r.text).splitlines())
        for label in [*_SPANISH_TYPES.values(), *_SPANISH_STATUSES.values()]:
            assert label in lines, f"{label!r} missing from the Spanish media table"
        assert not lines & _RAW_ENUM_WORDS, lines & _RAW_ENUM_WORDS
        # Code that reads the rows still sees the raw values.
        for media_type in MediaType:
            assert f'data-type="{media_type.value}"' in r.text
        for status in ProcessingStatus:
            assert f'data-status="{status.value}"' in r.text

    async def test_media_table_labels_are_english_in_english(
        self, app: FastAPI, client: AsyncClient
    ) -> None:
        await _seed_every_type_and_status(app)
        r = await client.get("/media", params={"lang": "en"})
        lines = set(visible_text(r.text).splitlines())
        assert {"Music", "Audiobook", "Photo", "Ready", "Failed"} <= lines
        assert {"Processing", "Pending"} <= lines

    async def test_queue_script_has_translated_type_and_status(
        self, client: AsyncClient
    ) -> None:
        html = (await client.get("/queue", params={"lang": "es"})).text

        def table(name: str) -> dict[str, str]:
            block = re.search(rf"{name}: \{{(.*?)\}}", html, re.S)
            assert block, f"no {name} table in the queue script"
            return {
                key: json.loads(value)
                for key, value in re.findall(r'(\w+): ("[^"]*")', block.group(1))
            }

        types = table("mediaType")
        assert types == {m.value: _SPANISH_TYPES[m] for m in MediaType}
        assert table("status")["failed"] == "Con error"
        assert "escH(T.mediaType[item.media_type]" in html


def api_errors_in(html: str) -> dict[str, str]:
    """The ``API_ERRORS`` table a page embeds for its scripts."""
    match = re.search(r"const API_ERRORS = (\{.*?\});", html, re.S)
    assert match
    return json.loads(match.group(1))


class TestApiErrorMessages:
    """Toasts show a translated message for the API's known ``error_code``s."""

    def test_every_message_is_translated_into_spanish(self) -> None:
        from kidsplay_server.web.routes import _ERROR_CODE_MESSAGES, error_messages

        i18n.use_language("es")
        try:
            spanish = error_messages()
        finally:
            i18n.use_language("en")
        assert set(spanish) == set(_ERROR_CODE_MESSAGES)
        untranslated = [c for c, m in spanish.items() if m == _ERROR_CODE_MESSAGES[c]]
        assert not untranslated

    async def test_pages_embed_the_messages_in_the_request_language(
        self, client: AsyncClient
    ) -> None:
        spanish = api_errors_in(
            (await client.get("/media", params={"lang": "es"})).text
        )
        english = api_errors_in(
            (await client.get("/media", params={"lang": "en"})).text
        )
        assert spanish["NOT_FOUND"].startswith("No se encontró ese elemento")
        assert english["NOT_FOUND"].startswith("That item was not found")
        assert set(spanish) == set(english)

    def test_every_fixed_message_error_code_of_the_api_has_a_toast(self) -> None:
        """A new error code needs a translated toast, or a reason to skip it."""
        from kidsplay_server.web.routes import _ERROR_CODE_MESSAGES

        detail_has_specifics = {
            "PLUGIN_REQUIRED",  # names the plugin to install
            "UNKNOWN_IMPORTER",  # names the importer
            "PREVIEW_FAILED",  # why the preview failed
            "ARCHIVE_EXTRACT_FAILED",  # why extraction failed
            "ENV_LOCKED",  # names the setting and its variable
            "INVALID_SETTING",  # which setting is out of range
            "INVALID_THEME_ASSET",  # what is wrong with the file
            "UNSUPPORTED_LANGUAGE",  # lists the supported ones
            "UNKNOWN_THEME",  # names the theme
            "PASSWORD_TOO_SHORT",  # the account page has its own text
            "PATH_NOT_FOUND",  # names the path
            "CONFLICT",  # says what conflicts
        }
        source = "".join(
            path.read_text()
            for path in (Path(i18n.__file__).parent / "api").glob("*.py")
        )
        sent = set(re.findall(r'"error_code": "([A-Z_]+)"', source))
        sent |= set(re.findall(r'_error\([^)]*?"([A-Z_]+)"\s*\)', source, re.S))
        sent -= {"MACHINE_CODE", "ERROR"}
        assert sent, "found no error codes: the pattern needs updating"
        assert not sent - set(_ERROR_CODE_MESSAGES) - detail_has_specifics


async def test_import_page_radio_note_is_one_sentence(client: AsyncClient) -> None:
    """The radio/mix note is one translatable sentence with its markup in place."""
    for language, opening in (
        ("en", "Radio/mix links"),
        ("es", "Los enlaces de radio"),
    ):
        page = (await client.get("/media/import", params={"lang": language})).text
        note = re.search(r"<p[^>]*>\s*(" + opening + r".*?)</p>", page, re.S)
        assert note, language
        text = note.group(1)
        assert "<code>" in text and 'id="yt-max-items"' in text
        assert text.index("<code>") < text.index('id="yt-max-items"')
