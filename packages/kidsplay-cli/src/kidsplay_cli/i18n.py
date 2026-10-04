"""Localization of the CLI: language choice, translation and Click's own text.

The language is, in order: ``--lang``, then the POSIX locale variables
(``LANGUAGE``, ``LC_ALL``, ``LC_MESSAGES``, ``LANG``), then English. Message ids
are English; ``locale/<lang>/LC_MESSAGES/kidsplay_cli.mo`` holds the
translations.

Two kinds of strings are translated differently:

- *Runtime messages* (``click.echo``, ``console.print``, errors) call ``_()``
  when they run, so they follow the language active at that moment.
- *Help text* lives in decorators, which run at import time, before the
  language is known. Those strings are marked with ``N_()`` (or are command
  docstrings, which are extracted too) and :func:`translate_help` swaps in the
  translation on the command tree once the language is known.

Click's own messages ("Usage:", "Error: ...", "Show this message and exit.")
come from Click's gettext calls, which use the process-wide ``messages`` domain;
:func:`activate` points that domain at our ``messages`` catalog.
"""

import gettext
import inspect
import os
import weakref
from pathlib import Path

import click

from kidsplay_models import (
    DEFAULT_LANGUAGE,
    SUPPORTED_LANGUAGES,
    language_from_environment,
    load_translations,
    normalize_language,
)

DOMAIN = "kidsplay_cli"
CLICK_DOMAIN = "messages"
LOCALE_DIR = Path(__file__).parent / "locale"

_UNSET = object()
# The caller's LANGUAGE value (or None if unset), saved by activate().
_saved_language_env: object = _UNSET
_translations: gettext.NullTranslations = gettext.NullTranslations()
_language: str = DEFAULT_LANGUAGE

# The original (English) text of every command and option attribute seen, so
# the tree can be translated again into another language.
_TRANSLATED_ATTRIBUTES = ("help", "short_help", "prompt")
_originals: "weakref.WeakKeyDictionary[object, dict[str, str]]" = (
    weakref.WeakKeyDictionary()
)


def resolve_language(explicit: str | None = None) -> str:
    """Decide the CLI language.

    Args:
        explicit: The ``--lang`` value, if given.

    Returns:
        A supported language code.

    Raises:
        click.BadParameter: If ``explicit`` is not a supported language.
    """
    if explicit:
        language = normalize_language(explicit)
        if language is None:
            raise click.BadParameter(
                _("unsupported language {language!r}; use one of: {supported}").format(
                    language=explicit, supported=", ".join(SUPPORTED_LANGUAGES)
                ),
                param_hint="'--lang'",
            )
        return language
    return language_from_environment() or DEFAULT_LANGUAGE


def activate(language: str) -> None:
    """Make ``language`` the language of every later message.

    Args:
        language: A supported language code.
    """
    global _translations, _language, _saved_language_env
    _language = language
    _translations = load_translations(LOCALE_DIR, DOMAIN, language)
    # Click's built-in messages: gettext.gettext reads the domain bound here
    # and the language from LANGUAGE. Remember the caller's value so
    # restore_environment() can put it back.
    gettext.bindtextdomain(CLICK_DOMAIN, str(LOCALE_DIR))
    if _saved_language_env is _UNSET:
        _saved_language_env = os.environ.get("LANGUAGE")
    os.environ["LANGUAGE"] = language


def restore_environment() -> None:
    """Undo :func:`activate`'s change to the ``LANGUAGE`` variable."""
    global _saved_language_env
    if _saved_language_env is _UNSET:
        return
    if _saved_language_env is None:
        os.environ.pop("LANGUAGE", None)
    else:
        os.environ["LANGUAGE"] = str(_saved_language_env)
    _saved_language_env = _UNSET


def current_language() -> str:
    """Return the active language code."""
    return _language


def _(message: str) -> str:
    """Translate a message now.

    Args:
        message: The English message id.

    Returns:
        The translation, or ``message`` if there is none.
    """
    return _translations.gettext(message)


def N_(message: str) -> str:
    """Mark a string for extraction; translate it later.

    Args:
        message: The English message id.

    Returns:
        ``message`` unchanged.
    """
    return message


def translate_help(command: click.Command) -> None:
    """Translate the help of a command tree into the active language.

    Command docstrings, ``help=`` and ``prompt=`` strings are English message
    ids; this replaces them with their translations (and restores English when
    the language is English), including subcommands and options.

    Args:
        command: The root of the tree; subcommands are visited recursively.
    """
    for obj in (command, *command.params):
        originals = _originals.setdefault(obj, {})
        for attribute in _TRANSLATED_ATTRIBUTES:
            current = getattr(obj, attribute, None)
            if attribute not in originals:
                if not isinstance(current, str) or not current:
                    continue
                originals[attribute] = current
            # Click cleans a docstring only when it formats the help, so the
            # message id is the cleaned text (what the extractor recorded).
            message = inspect.cleandoc(originals[attribute])
            setattr(obj, attribute, _(message))
    # Click builds the --help option once and caches it (with its help text),
    # so drop the cached copy or it keeps the language of the first run.
    if "_help_option" in vars(command):
        vars(command)["_help_option"] = None
    if isinstance(command, click.Group):
        for sub in command.commands.values():
            translate_help(sub)


class LocalizedGroup(click.Group):
    """A group that picks the language before parsing and translates its help.

    ``--lang`` has to be honoured before Click builds ``--help`` output, so the
    argument list is scanned for it first (an option callback would run too late
    for ``kidsplay --help --lang es``).
    """

    def parse_args(self, ctx: click.Context, args: list[str]) -> list[str]:
        """Set the language, translate the help, then parse as usual.

        Args:
            ctx: The group's context.
            args: The command-line arguments after the program name.

        Returns:
            The remaining arguments.
        """
        activate(resolve_language(_scan_lang_option(args)))
        # Leave the process environment as we found it (matters to callers that
        # run several commands in one process, such as tests).
        ctx.call_on_close(restore_environment)
        translate_help(self)
        return super().parse_args(ctx, args)


def _scan_lang_option(args: list[str]) -> str | None:
    """Find ``--lang X`` / ``--lang=X`` in the arguments before any subcommand."""
    for index, arg in enumerate(args):
        if arg == "--lang":
            return args[index + 1] if index + 1 < len(args) else None
        if arg.startswith("--lang="):
            return arg.split("=", 1)[1]
    return None
