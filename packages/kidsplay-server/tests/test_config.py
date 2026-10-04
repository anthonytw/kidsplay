"""Tests for the environment settings in kidsplay_server.config."""

from pathlib import Path

import pytest

from kidsplay_server.auth import AuthConfig
from kidsplay_server.config import Settings

_AUTH_VARS = (
    "KIDSPLAY_AUTH",
    "KIDSPLAY_ADMIN_PASSWORD",
    "KIDSPLAY_SECRET_KEY_FILE",
    "KIDSPLAY_COOKIE_SECURE",
    "KIDSPLAY_TRUSTED_PROXIES",
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in _AUTH_VARS:
        monkeypatch.delenv(var, raising=False)


class TestAuthSettings:
    def test_defaults(self) -> None:
        assert Settings().auth == AuthConfig()

    def test_all_set(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("KIDSPLAY_AUTH", "disabled")
        monkeypatch.setenv("KIDSPLAY_ADMIN_PASSWORD", "hunter2hunter2")
        monkeypatch.setenv("KIDSPLAY_SECRET_KEY_FILE", "/srv/kidsplay/secret.key")
        monkeypatch.setenv("KIDSPLAY_COOKIE_SECURE", "1")
        assert Settings().auth == AuthConfig(
            disabled=True,
            admin_password="hunter2hunter2",
            secret_key_path=Path("/srv/kidsplay/secret.key"),
            cookie_secure=True,
        )

    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            ("", None),
            ("1", True),
            ("true", True),
            (" On ", True),
            ("0", False),
            ("false", False),
        ],
    )
    def test_cookie_secure_override(
        self, monkeypatch: pytest.MonkeyPatch, value: str, expected: bool | None
    ) -> None:
        """Unset means automatic; 1 forces Secure and 0 forbids it."""
        monkeypatch.setenv("KIDSPLAY_COOKIE_SECURE", value)
        assert Settings().auth.cookie_secure is expected

    def test_cookie_secure_typo_is_an_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("KIDSPLAY_COOKIE_SECURE", "sure")
        with pytest.raises(ValueError, match="KIDSPLAY_COOKIE_SECURE"):
            Settings()

    def test_trusted_proxies(self, monkeypatch: pytest.MonkeyPatch) -> None:
        assert Settings().auth.trusted_proxies == ()
        monkeypatch.setenv("KIDSPLAY_TRUSTED_PROXIES", "172.18.0.1, 10.0.0.0/8")
        nets = Settings().auth.trusted_proxies
        assert [str(n) for n in nets] == ["172.18.0.1/32", "10.0.0.0/8"]

    @pytest.mark.parametrize("value", ["*", "0.0.0.0/0", "caddy"])
    def test_trusted_proxies_refuses_wildcards_and_names(
        self, monkeypatch: pytest.MonkeyPatch, value: str
    ) -> None:
        monkeypatch.setenv("KIDSPLAY_TRUSTED_PROXIES", value)
        with pytest.raises(ValueError, match="KIDSPLAY_TRUSTED_PROXIES"):
            Settings()

    @pytest.mark.parametrize("value", ["enabled", "ENABLED", " enabled ", ""])
    def test_enabled_spellings(
        self, monkeypatch: pytest.MonkeyPatch, value: str
    ) -> None:
        monkeypatch.setenv("KIDSPLAY_AUTH", value)
        assert not Settings().auth.disabled

    @pytest.mark.parametrize("value", ["disable", "off", "false", "0"])
    def test_unknown_mode_is_an_error(
        self, monkeypatch: pytest.MonkeyPatch, value: str
    ) -> None:
        """A typo must fail loudly rather than silently pick a mode."""
        monkeypatch.setenv("KIDSPLAY_AUTH", value)
        with pytest.raises(ValueError, match="KIDSPLAY_AUTH"):
            Settings()

    def test_empty_admin_password_ignored(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("KIDSPLAY_ADMIN_PASSWORD", "")
        assert Settings().auth.admin_password is None
