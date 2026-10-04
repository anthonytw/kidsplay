"""End-to-end tests for ``kidsplay auth`` and token handling in the CLI.

Uses the session's real uvicorn server (see conftest.py), which has admin
auth enabled.
"""

import json
import stat

import pytest
from click.testing import CliRunner, Result

from kidsplay_cli.credentials import credentials_path, load_token
from kidsplay_cli.main import cli


def run(server_url: str, *args: str, input: str | None = None) -> Result:
    return CliRunner().invoke(
        cli,
        ["--server", server_url, *args],
        input=input,
        catch_exceptions=False,
        env={"COLUMNS": "200"},
    )


@pytest.fixture
def no_env_token(monkeypatch: pytest.MonkeyPatch) -> None:
    """Rely on the saved credentials file rather than KIDSPLAY_TOKEN."""
    monkeypatch.delenv("KIDSPLAY_TOKEN")


@pytest.mark.usefixtures("no_env_token")
class TestAuthCommands:
    def test_unauthenticated_command_fails_with_hint(self, server_url: str) -> None:
        result = run(server_url, "profile", "list")
        assert result.exit_code == 1
        assert "UNAUTHORIZED" in result.output
        assert "kidsplay auth login" in result.output

    def test_status_not_logged_in(self, server_url: str) -> None:
        result = run(server_url, "auth", "status")
        assert result.exit_code == 1
        assert "Not logged in" in result.output

    def test_login_saves_token_and_commands_work(
        self, server_url: str, admin_password: str
    ) -> None:
        result = run(
            server_url, "auth", "login", "--name", "t1", input=admin_password + "\n"
        )
        assert result.exit_code == 0, result.output
        assert "Logged in" in result.output
        assert admin_password not in result.output

        path = credentials_path()
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        saved = json.loads(path.read_text())["servers"][server_url]
        assert saved["token"].startswith("kpa_")

        assert run(server_url, "auth", "status").exit_code == 0
        assert run(server_url, "profile", "list").exit_code == 0

    def test_login_wrong_password(self, server_url: str) -> None:
        result = run(server_url, "auth", "login", input="not-the-password\n")
        assert result.exit_code == 1
        assert "Incorrect password" in result.output
        assert load_token(server_url) is None

    def test_logout_revokes_token(self, server_url: str, admin_password: str) -> None:
        run(server_url, "auth", "login", input=admin_password + "\n")
        stored = load_token(server_url)
        assert stored is not None

        result = run(server_url, "auth", "logout")
        assert result.exit_code == 0
        assert load_token(server_url) is None

        # The token no longer works on the server either.
        result = run(server_url, "--token", stored.token, "profile", "list")
        assert result.exit_code == 1
        assert "UNAUTHORIZED" in result.output

    def test_logout_when_not_logged_in(self, server_url: str) -> None:
        result = run(server_url, "auth", "logout")
        assert result.exit_code == 0
        assert "Not logged in" in result.output

    def test_token_option_used_without_saved_token(
        self, server_url: str, admin_token: str
    ) -> None:
        result = run(server_url, "--token", admin_token, "auth", "status")
        assert result.exit_code == 0
        assert "Authenticated" in result.output

    def test_corrupt_credentials_file(self, server_url: str) -> None:
        path = credentials_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{oops")
        result = CliRunner().invoke(cli, ["--server", server_url, "profile", "list"])
        assert result.exit_code == 1
        assert "Cannot read credentials" in result.output


class TestEnvToken:
    def test_kidsplay_token_env_used(self, server_url: str) -> None:
        result = run(server_url, "auth", "status")
        assert result.exit_code == 0
        assert "Authenticated" in result.output
