"""Tests for kidsplay_server.importers: contract helpers, built-ins, registry.

Entry points are faked by patching ``importlib.metadata.entry_points`` as seen
by the registry module, so these tests do not depend on which plugins happen
to be installed.
"""

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kidsplay_server.importers import (
    BaseImporter,
    FetchContext,
    FetchedItem,
    HttpImporter,
    Importer,
    ImporterInfo,
    ImporterRegistry,
    ImportPreview,
    LocalImporter,
    PreviewDebug,
    PreviewError,
    SupportsPreview,
    builtin_importers,
    discover_importers,
    get_default_registry,
)

# ---------------------------------------------------------------------------
# Test importers
# ---------------------------------------------------------------------------


class _FakeImporter(BaseImporter):
    def __init__(self, name: str = "fake", prefix: str = "fake://") -> None:
        self.name = name
        self.label = name.title()
        self.prefix = prefix

    def can_handle(self, source: str) -> bool:
        return source.startswith(self.prefix)

    async def fetch(
        self, source: str, workdir: Path, ctx: FetchContext
    ) -> list[FetchedItem]:
        return []


class _PreviewImporter(_FakeImporter):
    async def preview(self, source: str, max_items: int) -> ImportPreview:
        return ImportPreview(
            is_playlist=False,
            tracks=[],
            debug=PreviewDebug(command=[], returncode=0, stderr=""),
        )


class _BrokenImporter(_FakeImporter):
    def can_handle(self, source: str) -> bool:
        raise ValueError("boom")


# ---------------------------------------------------------------------------
# Contract helpers
# ---------------------------------------------------------------------------


def test_fetch_context_collects_log() -> None:
    ctx = FetchContext()
    ctx.log("one")
    ctx.log("two")
    assert ctx.text == "one\ntwo"
    assert ctx.attempt == 1
    assert not ctx.queued


def test_base_importer_normalize_is_identity() -> None:
    assert _FakeImporter().normalize("fake://x?y=1") == "fake://x?y=1"


def test_base_importer_subclass_satisfies_protocol() -> None:
    assert isinstance(_FakeImporter(), Importer)
    assert not isinstance(_FakeImporter(), SupportsPreview)
    assert isinstance(_PreviewImporter(), SupportsPreview)


def test_importer_info_reports_preview_support() -> None:
    info = ImporterInfo.of(_PreviewImporter("pv"))
    assert info.model_dump() == {
        "name": "pv",
        "label": "Pv",
        "requires_queue": False,
        "supports_preview": True,
    }
    assert ImporterInfo.of(_FakeImporter()).supports_preview is False


def test_preview_error_keeps_detail_and_debug() -> None:
    debug = PreviewDebug(command=["x"], returncode=1, stderr="err")
    exc = PreviewError("nope", debug)
    assert str(exc) == "nope"
    assert exc.detail == "nope"
    assert exc.debug is debug


# ---------------------------------------------------------------------------
# LocalImporter
# ---------------------------------------------------------------------------


def test_local_can_handle(tmp_path: Path) -> None:
    importer = LocalImporter()
    (tmp_path / "a.mp3").write_bytes(b"x")
    assert importer.can_handle(str(tmp_path))
    assert importer.can_handle(str(tmp_path / "a.mp3"))
    assert not importer.can_handle(str(tmp_path / "missing.mp3"))
    assert not importer.can_handle("https://example.com/a.mp3")


async def test_local_fetch_file(tmp_path: Path) -> None:
    song = tmp_path / "a.mp3"
    song.write_bytes(b"x")
    items = await LocalImporter().fetch(str(song), tmp_path, FetchContext())
    assert items == [FetchedItem(path=song)]


async def test_local_fetch_directory_keeps_media_files_only(tmp_path: Path) -> None:
    (tmp_path / "sub").mkdir()
    (tmp_path / "b.MP3").write_bytes(b"x")
    (tmp_path / "sub" / "a.jpg").write_bytes(b"x")
    (tmp_path / "notes.txt").write_text("skip me")
    items = await LocalImporter().fetch(str(tmp_path), tmp_path, FetchContext())
    assert [i.path.name for i in items] == ["b.MP3", "a.jpg"]


async def test_local_fetch_missing_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        await LocalImporter().fetch(str(tmp_path / "gone"), tmp_path, FetchContext())


# ---------------------------------------------------------------------------
# HttpImporter
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "source,expected",
    [
        ("https://example.com/a.mp3", True),
        ("http://localhost:8000/file", True),
        ("ftp://example.com/a.mp3", False),
        ("https://", False),
        ("/srv/music/a.mp3", False),
    ],
)
def test_http_can_handle(source: str, expected: bool) -> None:
    assert HttpImporter().can_handle(source) is expected


async def test_http_fetch_downloads_into_workdir(tmp_path: Path) -> None:
    dest = tmp_path / "a.mp3"
    download = AsyncMock(return_value=dest)
    with patch("kidsplay_server.importers.builtin.download_from_url", new=download):
        items = await HttpImporter().fetch(
            "https://example.com/a.mp3", tmp_path, FetchContext()
        )
    download.assert_awaited_once_with("https://example.com/a.mp3", tmp_path)
    assert items == [FetchedItem(path=dest)]


def test_builtin_importers_order() -> None:
    assert [i.name for i in builtin_importers()] == ["local", "http"]


# ---------------------------------------------------------------------------
# ImporterRegistry
# ---------------------------------------------------------------------------


def test_registry_get_and_iterate() -> None:
    a, b = _FakeImporter("a", "a://"), _FakeImporter("b", "b://")
    registry = ImporterRegistry([a, b])
    assert list(registry) == [a, b]
    assert len(registry) == 2
    assert registry.get("b") is b
    assert registry.get("zzz") is None


def test_registry_drops_duplicate_names() -> None:
    first, second = _FakeImporter("dup"), _FakeImporter("dup")
    registry = ImporterRegistry([first, second])
    assert list(registry) == [first]


def test_registry_resolve_returns_first_match() -> None:
    specific = _FakeImporter("yt", "https://youtu.be/")
    registry = ImporterRegistry([specific, *builtin_importers()])
    assert registry.resolve("https://youtu.be/abc") is specific
    resolved = registry.resolve("https://example.com/a.mp3")
    assert resolved is not None and resolved.name == "http"
    assert registry.resolve("gopher://nothing") is None


def test_registry_resolve_skips_importer_that_raises() -> None:
    fallback = _FakeImporter("ok", "fake://")
    registry = ImporterRegistry([_BrokenImporter("broken"), fallback])
    assert registry.resolve("fake://x") is fallback


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------


def _entry_point(name: str, obj: object | BaseException) -> MagicMock:
    ep = MagicMock()
    ep.name = name
    ep.value = f"pkg:{name}"
    if isinstance(obj, BaseException):
        ep.load.side_effect = obj
    else:
        ep.load.return_value = obj
    return ep


def test_discover_loads_plugins_before_builtins() -> None:
    instance = _PreviewImporter("zeta", "z://")
    eps = [
        _entry_point("zeta", instance),
        _entry_point("alpha", _FakeImporter),  # a class: instantiated
        _entry_point("broken", ImportError("no module named yt_dlp")),
        _entry_point("junk", object()),
    ]
    with patch("kidsplay_server.importers.registry.entry_points", return_value=eps):
        registry = discover_importers()
    # Sorted by entry-point name; failures skipped; built-ins last.
    assert [i.name for i in registry] == ["fake", "zeta", "local", "http"]
    assert registry.get("zeta") is instance


def test_discover_survives_plugin_calling_exit() -> None:
    eps = [_entry_point("exits", SystemExit(1))]
    with patch("kidsplay_server.importers.registry.entry_points", return_value=eps):
        registry = discover_importers()
    assert [i.name for i in registry] == ["local", "http"]


def test_discover_without_plugins_has_only_builtins() -> None:
    with patch("kidsplay_server.importers.registry.entry_points", return_value=[]):
        registry = discover_importers()
    assert [i.name for i in registry] == ["local", "http"]


def test_default_registry_is_cached() -> None:
    assert get_default_registry() is get_default_registry()
