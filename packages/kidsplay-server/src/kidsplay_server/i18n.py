"""Localization of the web UI: language choice, translation and the picker.

The language of a request is, in order:

1. ``?lang=xx`` in the URL (what the language picker links to; also stored in
   a cookie so it sticks);
2. the ``kidsplay_lang`` cookie set by an earlier pick;
3. the browser's ``Accept-Language`` header;
4. English.

Message ids are English; ``locale/<lang>/LC_MESSAGES/kidsplay_server.mo``
holds the translations. API error messages are for developers and stay
English; only the web UI's own strings (templates, form errors) are translated.

Templates translate through Jinja's ``i18n`` extension: ``{% trans %}``,
``{{ _('...') }}``. The callables it uses read the language of the request being
rendered from a context variable that :func:`i18n_template_context` sets, so one
shared template environment serves every language.
"""

import contextvars
import gettext
from dataclasses import dataclass
from pathlib import Path

from fastapi import Request, Response
from jinja2 import Environment

from kidsplay_models import (
    DEFAULT_LANGUAGE,
    LANGUAGE_NAMES,
    SUPPORTED_LANGUAGES,
    load_translations,
    negotiate_accept_language,
    normalize_language,
)

DOMAIN = "kidsplay_server"
LOCALE_DIR = Path(__file__).parent / "locale"
LANGUAGE_COOKIE = "kidsplay_lang"
LANGUAGE_PARAM = "lang"
_COOKIE_MAX_AGE = 365 * 24 * 3600

_catalogs: dict[str, gettext.NullTranslations] = {}
_current_language: contextvars.ContextVar[str] = contextvars.ContextVar(
    "kidsplay_language", default=DEFAULT_LANGUAGE
)


@dataclass(frozen=True)
class LanguageChoice:
    """One entry of the language picker.

    Attributes:
        code: Language code (``"es"``).
        name: The language's name in itself (``"Español"``).
        url: Link that switches to it and keeps the rest of the URL.
        active: Whether it is the language of this page.
    """

    code: str
    name: str
    url: str
    active: bool


def translations_for(language: str) -> gettext.NullTranslations:
    """Return the (cached) catalog for a language.

    Args:
        language: A supported language code.

    Returns:
        Its translations; a pass-through for English or a missing catalog.
    """
    if language not in _catalogs:
        _catalogs[language] = load_translations(LOCALE_DIR, DOMAIN, language)
    return _catalogs[language]


def resolve_language(request: Request) -> str:
    """Decide the language of a request (see the module docstring).

    Args:
        request: The incoming request.

    Returns:
        A supported language code.
    """
    return (
        normalize_language(request.query_params.get(LANGUAGE_PARAM))
        or normalize_language(request.cookies.get(LANGUAGE_COOKIE))
        or negotiate_accept_language(request.headers.get("accept-language"))
        or DEFAULT_LANGUAGE
    )


def remember_language(request: Request, response: Response) -> None:
    """Store a ``?lang=`` pick in a cookie so it applies to later requests.

    Args:
        request: The request that may carry ``?lang=xx``.
        response: The response to set the cookie on.
    """
    picked = normalize_language(request.query_params.get(LANGUAGE_PARAM))
    if picked is not None:
        response.set_cookie(
            LANGUAGE_COOKIE,
            picked,
            max_age=_COOKIE_MAX_AGE,
            samesite="lax",
            path="/",
        )


def translate(request: Request, message: str, **values: object) -> str:
    """Translate a message for a request (form errors and the like).

    Args:
        request: The request whose language to use.
        message: The English message id; ``{name}`` placeholders are filled
            from ``values``.
        **values: Placeholder values.

    Returns:
        The translated, formatted message.
    """
    text = translations_for(resolve_language(request)).gettext(message)
    return text.format(**values) if values else text


def use_language(language: str) -> None:
    """Make ``language`` the one :func:`gettext` and templates use.

    Args:
        language: A supported language code.
    """
    _current_language.set(language)


def gettext_now(message: str, **values: object) -> str:
    """Translate a message into the language of the request being served.

    For code that has no ``request`` at hand (label helpers). The language is
    set per request by :func:`use_language`; outside a request it is English.

    Args:
        message: The English message id; ``{name}`` placeholders are filled
            from ``values``.
        **values: Placeholder values.

    Returns:
        The translated, formatted message.
    """
    text = _current().gettext(message)
    return text.format(**values) if values else text


def N_(message: str) -> str:
    """Mark a string for extraction; translate it when it is shown.

    Args:
        message: The English message id.

    Returns:
        ``message`` unchanged.
    """
    return message


def language_choices(request: Request, active: str) -> list[LanguageChoice]:
    """Build the language picker entries for a page.

    Args:
        request: The current request (its URL is what the links keep).
        active: The language of the page.

    Returns:
        One entry per supported language.
    """
    return [
        LanguageChoice(
            code=code,
            name=LANGUAGE_NAMES[code],
            url=str(request.url.include_query_params(**{LANGUAGE_PARAM: code})),
            active=code == active,
        )
        for code in SUPPORTED_LANGUAGES
    ]


def i18n_template_context(request: Request) -> dict[str, object]:
    """Template context processor: pick the language and expose the picker.

    Args:
        request: The request being rendered.

    Returns:
        ``language`` and ``language_choices`` for ``base.html``. As a side
        effect it selects the catalog the template's ``_``/``{% trans %}`` use.
    """
    language = resolve_language(request)
    use_language(language)
    return {
        "language": language,
        "language_choices": language_choices(request, language),
    }


def install_jinja_i18n(env: Environment) -> None:
    """Enable ``{% trans %}`` and ``_()`` on a Jinja environment.

    Args:
        env: The template environment to extend.
    """
    env.add_extension("jinja2.ext.i18n")
    env.policies["ext.i18n.trimmed"] = True
    env.install_gettext_callables(  # ty: ignore[unresolved-attribute] # added to Environment by the i18n extension
        lambda message: _current().gettext(message),
        lambda singular, plural, n: _current().ngettext(singular, plural, n),
        newstyle=True,
    )


def _current() -> gettext.NullTranslations:
    return translations_for(_current_language.get())
