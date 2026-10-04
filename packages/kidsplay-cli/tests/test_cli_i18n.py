"""CLI localization: language choice, translated help and messages."""

import inspect
import os
import re
import uuid

import click
import pytest
from click.testing import CliRunner, Result

from kidsplay_cli import i18n
from kidsplay_cli.main import cli


def invoke(
    *args: str, env: dict[str, str] | None = None, server_url: str | None = None
) -> Result:
    base = ["--server", server_url] if server_url else []
    return CliRunner().invoke(
        cli,
        [*base, *args],
        env={
            "COLUMNS": "200",
            "LANGUAGE": "",
            "LC_ALL": "",
            "LANG": "C",
            **(env or {}),
        },
    )


def all_commands(
    command: click.Command, path: tuple[str, ...] = ()
) -> list[tuple[str, ...]]:
    paths = [path]
    if isinstance(command, click.Group):
        for name, sub in sorted(command.commands.items()):
            paths.extend(all_commands(sub, (*path, name)))
    return paths


class TestLanguageChoice:
    def test_default_is_english(self) -> None:
        result = invoke("--help")
        assert "Usage:" in result.output
        assert "Show this message and exit." in result.output

    def test_lang_option(self) -> None:
        result = invoke("--lang", "es", "--help")
        assert result.exit_code == 0
        assert "Uso:" in result.output
        assert "Opciones:" in result.output
        assert "Muestra este mensaje y sale." in result.output
        assert "Usage:" not in result.output

    def test_lang_equals_form_and_position_after_help(self) -> None:
        assert "Uso:" in invoke("--lang=es", "--help").output
        assert "Uso:" in invoke("--help", "--lang", "es").output

    @pytest.mark.parametrize("value", ["es_MX.UTF-8", "es", "es_419"])
    def test_locale_environment(self, value: str) -> None:
        assert "Uso:" in invoke("--help", env={"LANG": value}).output

    def test_lc_messages_and_language_variables(self) -> None:
        assert "Uso:" in invoke("--help", env={"LC_MESSAGES": "es_ES"}).output
        assert "Uso:" in invoke("--help", env={"LANGUAGE": "fr:es"}).output

    def test_lang_option_beats_the_environment(self) -> None:
        result = invoke("--lang", "en", "--help", env={"LANG": "es_MX.UTF-8"})
        assert "Usage:" in result.output

    def test_unsupported_language_is_an_error(self) -> None:
        result = invoke("--lang", "klingon", "--help")
        assert result.exit_code == 2
        assert "unsupported language 'klingon'" in result.output

    def test_unsupported_environment_language_falls_back_to_english(self) -> None:
        assert "Usage:" in invoke("--help", env={"LANG": "fr_FR.UTF-8"}).output

    def test_process_environment_is_restored(self) -> None:
        before = os.environ.get("LANGUAGE")
        invoke("--lang", "es", "--help")
        assert os.environ.get("LANGUAGE") == before

    def test_language_does_not_leak_between_invocations(self) -> None:
        invoke("--lang", "es", "--help")
        assert "Usage:" in invoke("--help").output


class TestTranslatedHelp:
    def test_every_command_and_option_help_is_translated(self) -> None:
        """No English left in `--help`: each help text differs from its msgid."""
        i18n.activate("es")
        try:
            i18n.translate_help(cli)
            untranslated: list[str] = []

            def visit(command: click.Command, path: str) -> None:
                for obj in (command, *command.params):
                    for attribute, original in i18n._originals.get(obj, {}).items():
                        shown = getattr(obj, attribute)
                        if shown == inspect.cleandoc(original):
                            untranslated.append(f"{path} {attribute}: {shown[:40]!r}")
                if isinstance(command, click.Group):
                    for name, sub in command.commands.items():
                        visit(sub, f"{path} {name}")

            visit(cli, "kidsplay")
            assert not untranslated, "\n".join(untranslated)
        finally:
            i18n.activate("en")
            i18n.restore_environment()
            i18n.translate_help(cli)

    @pytest.mark.parametrize(
        "path", [p for p in all_commands(cli) if p], ids=lambda p: " ".join(p)
    )
    def test_help_of_every_command_renders_in_spanish(
        self, path: tuple[str, ...]
    ) -> None:
        result = invoke("--lang", "es", *path, "--help")
        assert result.exit_code == 0, result.output
        assert "Uso:" in result.output
        assert "Show this message and exit." not in result.output
        assert "Usage:" not in result.output

    def test_english_help_is_restored(self) -> None:
        invoke("--lang", "es", "--help")
        result = invoke("--help")
        assert "media management for kids' devices" in result.output


class TestTranslatedMessages:
    def test_success_and_empty_messages(self, server_url: str) -> None:
        name = f"Kid-{uuid.uuid4().hex[:6]}"
        result = invoke(
            "--lang", "es", "profile", "create", name, server_url=server_url
        )
        assert result.exit_code == 0, result.output
        assert "Perfil creado" in result.output
        assert "Created profile" not in result.output
        listed = invoke("--lang", "es", "profile", "list", server_url=server_url)
        assert name in listed.output
        assert "Perfiles" in listed.output

    def test_media_types_are_translated(self) -> None:
        from kidsplay_cli.media import _MEDIA_TYPE_LABELS, _type_label

        assert set(_MEDIA_TYPE_LABELS) == {"music", "audiobook", "photo"}
        i18n.activate("es")
        try:
            assert [_type_label(t) for t in ("music", "audiobook", "photo")] == [
                "música",
                "audiolibro",
                "foto",
            ]
            assert _type_label("podcast") == "podcast"
        finally:
            i18n.activate("en")

    def test_error_messages(self, server_url: str) -> None:
        result = invoke(
            "--lang", "es", "media", "show", str(uuid.uuid4()), server_url=server_url
        )
        assert result.exit_code == 1
        assert "Error:" in result.output

    def test_usage_error_from_the_cli(self, server_url: str) -> None:
        result = invoke("--lang", "es", "media", "normalize", server_url=server_url)
        assert result.exit_code != 0
        assert "Indica --all" in result.output

    def test_click_validation_errors_are_translated(self) -> None:
        result = invoke("--lang", "es", "media", "nope")
        assert result.exit_code == 2
        assert "No existe el comando 'nope'." in result.output
        result = invoke("--lang", "es", "--bogus")
        assert "No existe la opción: --bogus" in result.output

    def test_confirmation_prompt_is_translated(self, server_url: str) -> None:
        result = CliRunner().invoke(
            cli,
            [
                "--server",
                server_url,
                "--lang",
                "es",
                "media",
                "delete",
                str(uuid.uuid4()),
            ],
            input="n\n",
            env={"COLUMNS": "200"},
        )
        assert (
            "¿Eliminar este archivo multimedia y todos sus archivos?" in result.output
        )
        assert "Delete this media item" not in result.output

    def test_english_messages_are_unchanged(self, server_url: str) -> None:
        name = f"Kid-{uuid.uuid4().hex[:6]}"
        result = invoke("profile", "create", name, server_url=server_url)
        assert re.search(r"Created profile\s+" + name, result.output)
