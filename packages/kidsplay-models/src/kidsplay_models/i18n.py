"""Language codes and gettext helpers shared by every KidsPlay package.

The web UI, the CLI and the device player each ship their own message
catalogs (``locale/<lang>/LC_MESSAGES/<domain>.mo``); this module holds what
they have in common so all three agree on which languages exist and how a
language tag such as ``es_MX.UTF-8`` or an ``Accept-Language`` header maps
onto one.

Message ids are the English source strings, so ``en`` needs no catalog: a
missing translation falls back to the message id.
"""

import gettext
import re
from pathlib import Path

SUPPORTED_LANGUAGES: tuple[str, ...] = ("en", "es")
"""Language codes that have (or, for ``en``, are) a catalog."""

DEFAULT_LANGUAGE = "en"
"""Fallback for the web UI and CLI, and for new profiles."""

LEGACY_DEVICE_LANGUAGE = "es"
"""What a device shows for a profile that has no stored language.

The device UI was Spanish-only before language became a setting, so profiles
created back then (which have no ``language`` in their stored settings) keep
showing Spanish until someone chooses otherwise.
"""

LANGUAGE_NAMES: dict[str, str] = {"en": "English", "es": "Español"}
"""Each language written in itself, for language pickers."""

_SEPARATORS = re.compile(r"[-_.@]")


def normalize_language(tag: str | None) -> str | None:
    """Map a language tag onto a supported language code.

    Accepts BCP 47 tags (``es-MX``), POSIX locales (``es_MX.UTF-8``) and bare
    codes, in any case.

    Args:
        tag: The tag to map, or None.

    Returns:
        The supported code (``"es"`` for ``"es_419"``), or None if the tag is
        empty, ``C``/``POSIX``, or names an unsupported language.
    """
    if not tag:
        return None
    primary = _SEPARATORS.split(tag.strip().lower(), maxsplit=1)[0]
    return primary if primary in SUPPORTED_LANGUAGES else None


def negotiate_accept_language(header: str | None) -> str | None:
    """Pick the best supported language from an ``Accept-Language`` header.

    Args:
        header: The raw header value, e.g. ``"fr;q=0.9, es-MX;q=0.8, en;q=0.5"``.

    Returns:
        The supported language with the highest quality value (earlier wins a
        tie), or None if the header is missing or lists none.
    """
    if not header:
        return None
    candidates: list[tuple[float, int, str]] = []
    for position, part in enumerate(header.split(",")):
        tag, _, params = part.partition(";")
        language = normalize_language(tag)
        if language is None:
            continue
        quality = 1.0
        match = re.search(r"q\s*=\s*([^;,\s]+)", params)
        if match:
            try:
                quality = float(match.group(1))
            except ValueError:
                continue
        if quality > 0:
            candidates.append((-quality, position, language))
    return min(candidates)[2] if candidates else None


def language_from_environment(environ: dict[str, str] | None = None) -> str | None:
    """Pick the language from the POSIX locale environment variables.

    Follows the gettext order: ``LANGUAGE`` (a colon-separated list), then
    ``LC_ALL``, ``LC_MESSAGES`` and ``LANG``. ``C`` and ``POSIX`` mean "no
    preference" and are skipped.

    Args:
        environ: The environment to read; defaults to ``os.environ``.

    Returns:
        A supported language code, or None if none of the variables names one.
    """
    if environ is None:
        import os

        environ = dict(os.environ)
    for name in ("LANGUAGE", "LC_ALL", "LC_MESSAGES", "LANG"):
        for tag in environ.get(name, "").split(":"):
            language = normalize_language(tag)
            if language is not None:
                return language
    return None


def load_translations(
    locale_dir: Path, domain: str, language: str | None
) -> gettext.NullTranslations:
    """Load a compiled catalog, falling back to the message ids.

    Args:
        locale_dir: The package's ``locale`` directory.
        domain: Catalog name (the ``.mo`` file name without extension).
        language: A supported language code; None or ``en`` needs no catalog.

    Returns:
        The translations for ``language``, or a pass-through when there are
        none, so a missing catalog never breaks the UI.
    """
    if language is None:
        return gettext.NullTranslations()
    return gettext.translation(
        domain, localedir=str(locale_dir), languages=[language], fallback=True
    )
