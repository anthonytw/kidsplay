"""Tests for the device's localization: language choice and the catalogs."""

import gettext
import json
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path

import pytest

from kidsplay_device import i18n
from kidsplay_device.config import DeviceConfig
from kidsplay_device.i18n import _, activate, current_language, resolve_language
from kidsplay_device.views import until_label
from kidsplay_models import SUPPORTED_LANGUAGES


@pytest.fixture(autouse=True)
def _restore_language() -> Iterator[None]:
    before = current_language()
    yield
    activate(before)


class TestResolveLanguage:
    def test_profile_language_is_used(self) -> None:
        assert resolve_language(None, "en") == "en"
        assert resolve_language(None, "es") == "es"

    def test_profile_without_a_language_stays_spanish(self) -> None:
        """Existing installs: the device UI was Spanish-only before this."""
        assert resolve_language(None, None) == "es"

    def test_local_override_beats_the_profile(self) -> None:
        assert resolve_language("en", "es") == "en"
        assert resolve_language("es", "en") == "es"
        assert resolve_language("en", None) == "en"

    def test_override_accepts_locale_style_tags(self) -> None:
        assert resolve_language("es_MX.UTF-8", "en") == "es"

    def test_unsupported_override_is_ignored(self) -> None:
        assert resolve_language("fr", "en") == "en"
        assert resolve_language("fr", None) == "es"

    def test_unsupported_profile_language_falls_back_to_english(self) -> None:
        assert resolve_language(None, "fr") == "en"


class TestActivate:
    def test_translates_into_the_active_language(self) -> None:
        activate("es")
        assert _("Music") == "Música"
        activate("en")
        assert _("Music") == "Music"
        assert current_language() == "en"

    def test_unknown_message_passes_through(self) -> None:
        activate("es")
        assert _("no such message") == "no such message"

    def test_legacy_spanish_strings_are_unchanged(self) -> None:
        """The Spanish the device showed before localization is the es catalog."""
        activate("es")
        assert [_(m) for m in ("Music", "Audiobooks", "Photos", "Settings")] == [
            "Música",
            "Audiolibros",
            "Fotos",
            "Ajustes",
        ]
        assert _("Color Theme") == "Tema de Color"
        assert _("Change theme") == "Cambiar tema"
        assert _("Theme chosen by your parents") == "Tema elegido por tus padres"
        assert (_("Contrast"), _("Night")) == ("Contraste", "Noche")
        assert _("Back") == "Volver"
        assert _("Bedtime") == "Hora de dormir"
        assert until_label(datetime(2026, 9, 29, 7, 0)) == "Hasta las 07:00"
        assert _("Volume {percent}%").format(percent=40) == "Volumen 40%"


class TestCatalogs:
    def test_every_supported_language_but_english_has_a_catalog(self) -> None:
        for language in SUPPORTED_LANGUAGES:
            if language == "en":
                continue
            mo = i18n.LOCALE_DIR / language / "LC_MESSAGES" / f"{i18n.DOMAIN}.mo"
            assert mo.is_file(), f"missing compiled catalog for {language}"

    def test_spanish_translates_every_message(self) -> None:
        """No English left on a Spanish screen: each id has a different string."""
        translations = gettext.translation(
            i18n.DOMAIN, str(i18n.LOCALE_DIR), languages=["es"]
        )
        po = (i18n.LOCALE_DIR / "es" / "LC_MESSAGES" / f"{i18n.DOMAIN}.po").read_text()
        ids = [
            line.removeprefix("msgid ").strip('"')
            for line in po.splitlines()
            if line.startswith('msgid "') and line != 'msgid ""'
        ]
        assert ids
        for message in ids:
            assert translations.gettext(message) != message, message


class TestConfigLanguage:
    def _write(self, tmp_path: Path, **extra: object) -> Path:
        path = tmp_path / "config.json"
        path.write_text(
            json.dumps(
                {
                    "server_url": "http://x",
                    "device_id": "d",
                    "api_key": "k",
                    "media_root": str(tmp_path / "media"),
                    "db_path": str(tmp_path / "db.sqlite"),
                    **extra,
                }
            )
        )
        return path

    def test_language_defaults_to_none(self, tmp_path: Path) -> None:
        assert DeviceConfig.load(self._write(tmp_path)).language is None

    def test_language_override_loads(self, tmp_path: Path) -> None:
        assert DeviceConfig.load(self._write(tmp_path, language="en")).language == "en"

    @pytest.mark.parametrize("bad", ["", "  ", 5, None, ["es"]])
    def test_unusable_language_is_ignored(self, tmp_path: Path, bad: object) -> None:
        assert DeviceConfig.load(self._write(tmp_path, language=bad)).language is None

    def test_language_round_trips_and_is_omitted_when_unset(
        self, tmp_path: Path
    ) -> None:
        config = DeviceConfig.load(self._write(tmp_path))
        out = tmp_path / "saved.json"
        config.save(out)
        assert "language" not in json.loads(out.read_text())
        config.language = "es"
        config.save(out)
        assert DeviceConfig.load(out).language == "es"


class TestUntilLabel:
    """ "Hasta la 1:00" but "Hasta las 2:00": the hour picks the Spanish form."""

    @pytest.mark.parametrize(
        ("hour", "minute", "spanish"),
        [
            (0, 0, "Hasta las 00:00"),
            (1, 0, "Hasta la 1:00"),
            (1, 30, "Hasta la 1:30"),
            (2, 0, "Hasta las 02:00"),
            (7, 30, "Hasta las 07:30"),
            (13, 0, "Hasta las 13:00"),
        ],
    )
    def test_spanish(self, hour: int, minute: int, spanish: str) -> None:
        activate("es")
        assert until_label(datetime(2026, 9, 29, hour, minute)) == spanish

    def test_english_keeps_the_24_hour_clock(self) -> None:
        activate("en")
        assert until_label(datetime(2026, 9, 29, 1, 0)) == "Until 01:00"
        assert until_label(datetime(2026, 9, 29, 1, 30)) == "Until 01:30"
        assert until_label(datetime(2026, 9, 29, 7, 30)) == "Until 07:30"
