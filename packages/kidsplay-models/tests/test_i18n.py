"""Tests for kidsplay_models.i18n and ProfileSettings.language."""

import gettext
from pathlib import Path

import pytest

from kidsplay_models import (
    DEFAULT_LANGUAGE,
    LANGUAGE_NAMES,
    LEGACY_DEVICE_LANGUAGE,
    SUPPORTED_LANGUAGES,
    ProfileSettings,
    language_from_environment,
    load_translations,
    negotiate_accept_language,
    normalize_language,
)


@pytest.mark.parametrize(
    ("tag", "expected"),
    [
        ("es", "es"),
        ("ES", "es"),
        ("es-MX", "es"),
        ("es_419", "es"),
        ("es_MX.UTF-8", "es"),
        ("en_US.UTF-8@euro", "en"),
        ("fr", None),
        ("C", None),
        ("POSIX", None),
        ("", None),
        (None, None),
    ],
)
def test_normalize_language(tag: str | None, expected: str | None) -> None:
    assert normalize_language(tag) == expected


@pytest.mark.parametrize(
    ("header", "expected"),
    [
        ("es-MX,es;q=0.9,en;q=0.5", "es"),
        ("fr;q=0.9, es;q=0.8, en;q=0.5", "es"),
        ("en;q=0.4, es;q=0.6", "es"),
        ("es;q=0, en", "en"),
        ("es, en", "es"),
        ("fr, de;q=0.5", None),
        ("es;q=oops, en;q=0.1", "en"),
        ("*", None),
        ("", None),
        (None, None),
    ],
)
def test_negotiate_accept_language(header: str | None, expected: str | None) -> None:
    assert negotiate_accept_language(header) == expected


def test_language_from_environment_gettext_order() -> None:
    assert language_from_environment({"LANG": "es_MX.UTF-8"}) == "es"
    assert language_from_environment({"LANG": "es_MX.UTF-8", "LC_ALL": "en"}) == "en"
    assert language_from_environment({"LANGUAGE": "fr:es", "LANG": "en"}) == "es"
    assert language_from_environment({"LANG": "C", "LC_MESSAGES": "es"}) == "es"
    assert language_from_environment({"LANG": "C"}) is None
    assert language_from_environment({}) is None


def test_language_from_environment_defaults_to_process_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in ("LANGUAGE", "LC_ALL", "LC_MESSAGES"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("LANG", "es_ES.UTF-8")
    assert language_from_environment() == "es"


def test_load_translations_reads_a_catalog_and_falls_back(tmp_path: Path) -> None:
    assert load_translations(tmp_path, "demo", None).gettext("Hi") == "Hi"
    # No catalog on disk: pass-through, never an error.
    assert load_translations(tmp_path, "demo", "es").gettext("Hi") == "Hi"
    assert isinstance(
        load_translations(tmp_path, "demo", "en"), gettext.NullTranslations
    )


def test_language_constants_agree() -> None:
    assert DEFAULT_LANGUAGE in SUPPORTED_LANGUAGES
    assert LEGACY_DEVICE_LANGUAGE in SUPPORTED_LANGUAGES
    assert set(LANGUAGE_NAMES) == set(SUPPORTED_LANGUAGES)


def test_profile_settings_language_defaults_to_unset() -> None:
    assert ProfileSettings().language is None


def test_profile_settings_language_round_trips() -> None:
    settings = ProfileSettings(language="es")
    assert ProfileSettings.model_validate_json(settings.model_dump_json()) == settings


def test_profile_settings_accepts_a_language_from_a_newer_server() -> None:
    """An unknown language must not invalidate the rest of the settings."""
    settings = ProfileSettings.model_validate({"language": "fr", "max_volume": 40})
    assert settings.language == "fr"
    assert settings.max_volume == 40


def test_profile_settings_without_language_still_validate() -> None:
    """A manifest from a server that predates the field."""
    assert ProfileSettings.model_validate({"max_volume": 50}).language is None
