"""Tests for ``kidsplay profile settings`` against a real server."""

import re
import uuid

import click
import pytest
from click.testing import CliRunner, Result

from kidsplay_cli.devices import _parse_bedtime, _parse_days
from kidsplay_cli.main import cli
from kidsplay_models import Weekday


def run(server_url: str, *args: str) -> Result:
    return CliRunner().invoke(
        cli,
        ["--server", server_url, *args],
        catch_exceptions=False,
        env={"COLUMNS": "200"},
    )


def new_profile(server_url: str) -> str:
    result = run(server_url, "profile", "create", f"Kid-{uuid.uuid4().hex[:6]}")
    m = re.search(r"\(([0-9a-f-]{36})\)", result.output)
    assert m, result.output
    return m.group(1)


class TestProfileSettingsCommand:
    def test_show_defaults(self, server_url: str) -> None:
        pid = new_profile(server_url)
        result = run(server_url, "profile", "settings", pid)
        assert result.exit_code == 0, result.output
        assert "100%" in result.output
        assert "Updated" not in result.output

    def test_set_values(self, server_url: str) -> None:
        pid = new_profile(server_url)
        result = run(
            server_url,
            "profile",
            "settings",
            pid,
            "--max-volume",
            "55",
            "--volume-buttons",
            "--bedtime-mode",
            "sleep_screen",
            "--bedtime",
            "weekdays=20:00-07:00",
            "--bedtime",
            "sat=21:30-08:30",
        )
        assert result.exit_code == 0, result.output
        assert "Updated profile settings" in result.output
        assert "55%" in result.output
        assert "sleep screen" in result.output
        assert "sleep_screen" not in result.output
        assert "21:30 - 08:30" in result.output

        shown = run(server_url, "profile", "settings", pid).output
        assert "55%" in shown
        assert "20:00 - 07:00" in shown

    def test_volume_buttons_default_on_and_off_are_told_apart(
        self, server_url: str
    ) -> None:
        pid = new_profile(server_url)
        assert "device default" in run(server_url, "profile", "settings", pid).output
        on = run(server_url, "profile", "settings", pid, "--volume-buttons").output
        assert "Volume buttons" in on and "device default" not in on
        off = run(server_url, "profile", "settings", pid, "--no-volume-buttons")
        assert "device default" not in off.output
        assert "off" in off.output
        # An unrelated change keeps the explicit choice.
        kept = run(server_url, "profile", "settings", pid, "--max-volume", "40")
        assert "device default" not in kept.output

    def test_ui_sounds_option_sets_and_shows_the_row(self, server_url: str) -> None:
        pid = new_profile(server_url)
        shown = run(server_url, "profile", "settings", pid).output
        assert "Button sounds" in shown and "on" in shown
        off = run(server_url, "profile", "settings", pid, "--no-ui-sounds")
        assert off.exit_code == 0, off.output
        (row,) = [ln for ln in off.output.splitlines() if "Button sounds" in ln]
        assert "off" in row
        # An unrelated change keeps it off, and --ui-sounds turns it back on.
        kept = run(server_url, "profile", "settings", pid, "--max-volume", "40")
        (row,) = [ln for ln in kept.output.splitlines() if "Button sounds" in ln]
        assert "off" in row
        on = run(server_url, "profile", "settings", pid, "--ui-sounds")
        (row,) = [ln for ln in on.output.splitlines() if "Button sounds" in ln]
        assert "on" in row

    def test_ui_sounds_row_is_translated(self, server_url: str) -> None:
        pid = new_profile(server_url)
        result = CliRunner().invoke(
            cli,
            ["--server", server_url, "--lang", "es", "profile", "settings", pid]
            + ["--no-ui-sounds"],
            catch_exceptions=False,
            env={"COLUMNS": "200"},
        )
        assert result.exit_code == 0, result.output
        (row,) = [ln for ln in result.output.splitlines() if "Sonidos" in ln]
        assert "desactivado" in row
        assert "Button sounds" not in result.output

    def test_partial_update_keeps_other_values(self, server_url: str) -> None:
        pid = new_profile(server_url)
        run(server_url, "profile", "settings", pid, "--max-volume", "40")
        run(server_url, "profile", "settings", pid, "--bedtime", "all=19:00-06:30")
        result = run(server_url, "profile", "settings", pid, "--no-bedtime", "weekend")
        assert result.exit_code == 0, result.output
        assert "40%" in result.output
        # Five weekdays keep their bedtime; the weekend has none.
        assert result.output.count("19:00 - 06:30") == 5

    def test_language_is_shown_and_set(self, server_url: str) -> None:
        pid = new_profile(server_url)
        assert "device default" in run(server_url, "profile", "settings", pid).output
        result = run(server_url, "profile", "settings", pid, "--language", "es")
        assert result.exit_code == 0, result.output
        assert "Language" in result.output
        assert "Español" in result.output
        # An unrelated change keeps it (a PUT must not clear the language).
        kept = run(server_url, "profile", "settings", pid, "--max-volume", "30")
        assert "Español" in kept.output
        english = run(server_url, "profile", "settings", pid, "--language", "en")
        assert "English" in english.output

    def test_unsupported_language_is_refused(self, server_url: str) -> None:
        pid = new_profile(server_url)
        result = run(server_url, "profile", "settings", pid, "--language", "fr")
        assert result.exit_code == 2

    def test_table_shows_no_raw_enum_values(self, server_url: str) -> None:
        pid = new_profile(server_url)
        run(server_url, "profile", "settings", pid, "--bedtime-mode", "audiobooks_only")
        output = run(server_url, "profile", "settings", pid).output
        for raw in ("audiobooks_only", "Bedtime mon", "Bedtime sun"):
            assert raw not in output
        assert "Bedtime Monday" in output

    def test_spanish_table_is_all_spanish(self, server_url: str) -> None:
        pid = new_profile(server_url)
        result = CliRunner().invoke(
            cli,
            [
                "--server",
                server_url,
                "--lang",
                "es",
                "profile",
                "settings",
                pid,
                "--bedtime-mode",
                "sleep_screen",
                "--no-volume-buttons",
                "--language",
                "es",
            ],
            catch_exceptions=False,
            env={"COLUMNS": "200"},
        )
        assert result.exit_code == 0, result.output
        for spanish in ("Idioma", "Español", "Hora de dormir lunes", "desactivado"):
            assert spanish in result.output
        assert "pantalla de hora de dormir" in result.output.lower()
        for english in ("sleep_screen", " off", " mon", "Monday", "Language"):
            assert english not in result.output

    def test_max_volume_out_of_range(self, server_url: str) -> None:
        pid = new_profile(server_url)
        result = run(server_url, "profile", "settings", pid, "--max-volume", "150")
        assert result.exit_code == 2

    def test_bad_bedtime_spec(self, server_url: str) -> None:
        pid = new_profile(server_url)
        result = run(server_url, "profile", "settings", pid, "--bedtime", "mon=late")
        assert result.exit_code == 2

    def test_unknown_profile(self, server_url: str) -> None:
        result = run(server_url, "profile", "settings", str(uuid.uuid4()))
        assert result.exit_code == 1
        assert "Error" in result.output


class TestParsing:
    def test_days_groups(self) -> None:
        assert _parse_days("weekend") == [Weekday.SATURDAY, Weekday.SUNDAY]
        assert len(_parse_days("all")) == 7
        assert _parse_days("Mon, fri") == [Weekday.MONDAY, Weekday.FRIDAY]

    def test_unknown_day(self) -> None:
        with pytest.raises(click.BadParameter):
            _parse_days("someday")

    def test_bedtime(self) -> None:
        days, window = _parse_bedtime("tue=20:15-07:00")
        assert days == [Weekday.TUESDAY]
        assert f"{window.bedtime:%H:%M}" == "20:15"

    @pytest.mark.parametrize(
        "spec",
        [
            "tue",
            "tue=20:00",
            "tue=20:00-20:00",
            "tue=25:00-07:00",
            "tue=20:00-07:00+05:00",
        ],
    )
    def test_bad_bedtime(self, spec: str) -> None:
        with pytest.raises(click.BadParameter):
            _parse_bedtime(spec)

    def test_timezone_suffix_explained(self) -> None:
        with pytest.raises(click.BadParameter, match="no timezone offset"):
            _parse_bedtime("tue=20:00-07:00Z")
