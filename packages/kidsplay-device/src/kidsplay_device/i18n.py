"""Localization of the on-screen strings.

Message ids are the English source strings; ``locale/es`` holds the Spanish
the device has always shown. The active language is process-wide because the
player has a single screen: :func:`activate` is called at startup and again
whenever a sync changes the profile's language.

Which language wins (:func:`resolve_language`):

1. ``language`` in the device's ``config.json`` (a local override);
2. the profile's ``language`` setting from the last sync;
3. Spanish if the profile never had one, because every device showed Spanish
   before language was a setting and existing installs must not change;
4. English if the setting names a language this build does not have.
"""

import gettext
from pathlib import Path

from kidsplay_models import (
    DEFAULT_LANGUAGE,
    LEGACY_DEVICE_LANGUAGE,
    load_translations,
    normalize_language,
)

DOMAIN = "kidsplay_device"
LOCALE_DIR = Path(__file__).parent / "locale"

_translations: gettext.NullTranslations = gettext.NullTranslations()
_language: str = DEFAULT_LANGUAGE


def resolve_language(override: str | None, synced: str | None) -> str:
    """Decide which language the screens use.

    Args:
        override: ``language`` from ``config.json``, or None.
        synced: ``ProfileSettings.language`` from the last sync, or None if
            the profile never had one.

    Returns:
        A supported language code.
    """
    if override:
        chosen = normalize_language(override)
        if chosen is not None:
            return chosen
    if synced is None:
        return LEGACY_DEVICE_LANGUAGE
    return normalize_language(synced) or DEFAULT_LANGUAGE


def activate(language: str) -> None:
    """Make ``language`` the language of every later :func:`_` call.

    Args:
        language: A supported language code.
    """
    global _translations, _language
    _language = language
    _translations = load_translations(LOCALE_DIR, DOMAIN, language)


def current_language() -> str:
    """Return the active language code."""
    return _language


def _(message: str) -> str:
    """Translate a message into the active language.

    Args:
        message: The English message id.

    Returns:
        The translation, or ``message`` if the language has none.
    """
    return _translations.gettext(message)


def ngettext(singular: str, plural: str, n: int) -> str:
    """Translate a message whose wording depends on a number.

    Args:
        singular: The English message id used when ``n`` is 1.
        plural: The English plural form.
        n: The count that picks the form (in Spanish only 1 is singular).

    Returns:
        The translation, or the English form if the language has none.
    """
    return _translations.ngettext(singular, plural, n)


def N_(message: str) -> str:
    """Mark a string for extraction without translating it yet.

    Args:
        message: The English message id.

    Returns:
        ``message`` unchanged; translate it with :func:`_` when it is shown.
    """
    return message
