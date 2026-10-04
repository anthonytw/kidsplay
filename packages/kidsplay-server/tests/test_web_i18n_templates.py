"""Every user-visible string in the web templates goes through translation.

This is a static scan of ``web/templates/*.html``. It removes what Jinja
translates (``{% trans %}`` blocks, ``{{ ... }}`` expressions, which include
``{{ _('...') }}``) and what is not text (``<style>``, comments), then fails on
any letters left in text nodes, in the attributes browsers show
(``title``, ``placeholder``, ``alt``, ``aria-label``, a button's ``value``) and
in the natural-language string literals of the ``<script>`` blocks. Adding a
plain English sentence to a template therefore fails this test until it is
wrapped in ``{% trans %}`` / ``_()``.
"""

import re
from html.parser import HTMLParser
from pathlib import Path

import pytest

TEMPLATES = sorted(
    (Path(__file__).parents[1] / "src" / "kidsplay_server" / "web" / "templates").glob(
        "*.html"
    )
)

#: Words that are the same in every language: names, units and acronyms.
ALLOWED_WORDS = {
    "kidsplay",
    "api",
    "url",
    "uuid",
    "id",
    "lufs",
    "db",
    "webp",
    "csv",
    "json",
    "mp3",
    "http",
    "https",
    "utc",
    "ok",
    "yt",
    "dlp",
    "youtube",
    "sha",
    "x",
    "px",
    "kb",
    "mb",
    "gb",
    "s",
    "ms",
    "hz",
    "khz",
}

#: Exact-case tokens that are data, not prose: log level names (the server's
#: logging vocabulary) and the ``KeyboardEvent.key`` values scripts compare to.
ALLOWED_EXACT = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL", "Escape", "Enter"}

_ATTRIBUTES = {"title", "placeholder", "alt", "aria-label", "aria-description"}
_BUTTON_TYPES = {"submit", "button", "reset"}
_MARK = "\x00"

_COMMENT = re.compile(r"\{#.*?#\}", re.S)
_TRANS = re.compile(r"\{%-?\s*trans\b.*?\{%-?\s*endtrans\s*-?%\}", re.S)
_EXPRESSION = re.compile(r"\{\{.*?\}\}", re.S)
_STATEMENT = re.compile(r"\{%.*?%\}", re.S)
_STYLE = re.compile(r"<style\b.*?</style>", re.S | re.I)
_SCRIPT = re.compile(r"<script\b[^>]*>(.*?)</script>", re.S | re.I)
_JS_STRING = re.compile(
    r"""'((?:[^'\\\n]|\\.)*)'|"((?:[^"\\\n]|\\.)*)"|`((?:[^`\\]|\\.)*)`""", re.S
)
_JS_COMMENT = re.compile(r"//[^\n]*|/\*.*?\*/", re.S)
_WORD = re.compile(r"[^\W\d_]{2,}")


def _strip_jinja(source: str) -> str:
    """Drop what Jinja translates or evaluates, leaving a marker in its place."""
    for pattern in (_COMMENT, _TRANS, _EXPRESSION, _STATEMENT):
        source = pattern.sub(_MARK, source)
    return source


def _untranslated_words(text: str) -> list[str]:
    """Words in ``text`` that would need translating."""
    text = re.sub(r"&[#\w]+;", " ", text.replace(_MARK, " "))
    return [
        w
        for w in _WORD.findall(text)
        if w.lower() not in ALLOWED_WORDS and w not in ALLOWED_EXACT
    ]


class _TextScanner(HTMLParser):
    """Collect text nodes and displayed attributes that hold real words."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.found: list[str] = []

    def handle_data(self, data: str) -> None:
        if _untranslated_words(data):
            self.found.append(data.strip())

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        shown = set(_ATTRIBUTES)
        if tag == "input" and (values.get("type") or "") in _BUTTON_TYPES:
            shown.add("value")
        for name, value in attrs:
            if name in shown and value and _untranslated_words(value):
                self.found.append(f'{tag} {name}="{value}"')


def _is_prose(literal: str) -> bool:
    """Whether a JavaScript string literal looks like text shown to a person."""
    text = literal.strip()
    if not _untranslated_words(text):
        return False
    if "<" in text and ">" in text:  # an HTML fragment: scanned separately
        return False
    if re.match(r"^[.#\[/:@$&?]|^[\w-]+:\/\/", text) or re.search(r"[=>{}]", text):
        return False  # selector, path, URL or code
    letters = re.sub(r"^[^A-Za-z\u00c0-\u024f]+", "", text)
    if not letters or letters == letters.upper():
        return False  # an HTTP verb or other constant
    if not re.search(r"\s", letters.strip()) and not letters[0].isupper():
        return False  # one lowercase token: an id, key, class or field name
    if "-" in letters and " " not in letters.strip():
        return False  # a header or CSS name
    if letters[0].isupper():
        return True  # "Error", "Saved. Reload the page."
    # Lowercase start: prose only if it is a real phrase (class lists such as
    # "alert alert-error" or selector fragments have hyphens or dots).
    words = _WORD.findall(letters)
    return len(words) >= 3 and not re.search(r"[-.]\w", letters)


def scan_source(source: str) -> list[str]:
    """Return the untranslated text found in a template's source.

    Args:
        source: The template text.

    Returns:
        One entry per offending text node, attribute or script string.
    """
    found: list[str] = []
    source = _STYLE.sub("", _strip_jinja(source))
    scripts = _SCRIPT.findall(source)
    html = _SCRIPT.sub("", source)

    scanner = _TextScanner()
    scanner.feed(html)
    found.extend(scanner.found)

    for script in scripts:
        for match in _JS_STRING.finditer(_JS_COMMENT.sub("", script)):
            literal = next(g for g in match.groups() if g is not None)
            if "<" in literal and ">" in literal:
                fragment = _TextScanner()
                fragment.feed(literal)
                found.extend(f"js html: {item}" for item in fragment.found)
            elif _is_prose(literal):
                found.append(f"js string: {literal}")
    return found


def test_there_are_templates_to_scan() -> None:
    assert len(TEMPLATES) >= 10


@pytest.mark.parametrize("template", TEMPLATES, ids=lambda p: p.name)
def test_template_has_no_untranslated_text(template: Path) -> None:
    found = scan_source(template.read_text())
    assert not found, (
        f"{template.name} has text outside {{% trans %}} / _(): "
        + "; ".join(repr(f) for f in found[:10])
    )


class TestScannerCatchesWhatItShould:
    """Prove the scan is not vacuous (a scan that finds nothing would pass)."""

    @pytest.mark.parametrize(
        "source",
        [
            "<p>Hello there</p>",
            "<button>Save</button>",
            '<input type="text" placeholder="Search media">',
            '<input type="submit" value="Go now">',
            '<a href="/x" title="Open the page">x</a>',
            "<p>{{ count }} files</p>",
            "<label>Name</label>",
            "<script>alert('Something went wrong');</script>",
            "<script>el.textContent = 'Saved';</script>",
            "<script>el.innerHTML = '<b>Bold words</b>';</script>",
        ],
    )
    def test_flags(self, source: str) -> None:
        assert scan_source(source)

    @pytest.mark.parametrize(
        "source",
        [
            "<p>{% trans %}Hello there{% endtrans %}</p>",
            "<button>{{ _('Save') }}</button>",
            '<input type="text" placeholder="{{ _(\'Search media\') }}">',
            "<title>KidsPlay{% block title %}{% endblock %}</title>",
            "<p>{{ n }} / {{ total }}</p>",
            "<style>.a { color: red }</style>",
            "{# a comment with words #}",
            "<script>fetch('/api/v1/media'); el.className = 'hidden';</script>",
            "<script>alert({{ _('Something went wrong')|tojson }});</script>",
            "<script>const q = document.querySelector('#box .item');</script>",
        ],
    )
    def test_accepts(self, source: str) -> None:
        assert scan_source(source) == []
