"""Extract, update and compile the gettext catalogs of every package.

Run through ``just i18n-extract`` / ``just i18n-compile`` (see
``docs/TRANSLATING.md``), or directly::

    uv run python scripts/i18n.py extract     # sources -> .pot, merge into .po
    uv run python scripts/i18n.py compile     # .po -> .mo
    uv run python scripts/i18n.py check       # fail if .pot / .mo are stale
    uv run python scripts/i18n.py add-language fr

The tests import this module too (``check`` is what they assert), so the
extraction rules exist in exactly one place.
"""

import argparse
import ast
import io
import re
import sys
from collections import Counter
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from babel.messages import Catalog, mofile, pofile
from babel.messages.extract import extract_from_dir

from kidsplay_models import SUPPORTED_LANGUAGES

ROOT = Path(__file__).resolve().parent.parent
SOURCE_LANGUAGE = "en"

KEYWORDS: dict[str, object] = {
    "_": None,
    "gettext": None,
    "ngettext": (1, 2),
    "pgettext": ((1, "c"), 2),
    "N_": None,
    # kidsplay_server.i18n: translate(request, message) and gettext_now(message)
    "translate": (2,),
    "gettext_now": None,
}
"""Function names that take a message id (``N_`` only marks; see also i18n.py)."""

_PYTHON = ("**.py", "python")

_FIXED_DATE = datetime(2026, 1, 1, tzinfo=UTC)
_HEADER_COMMENT = """\
# Translations for {name}.
# Copyright (C) KidsPlay contributors
# This file is distributed under the same license as the KidsPlay project.
"""


@dataclass(frozen=True)
class Domain:
    """One package's catalog.

    Attributes:
        name: gettext domain, also the ``.po``/``.mo`` file name.
        package_dir: The package's importable directory.
        method_map: Babel ``(glob, extractor)`` pairs.
        options: Babel extractor options, keyed by glob.
    """

    name: str
    package_dir: Path
    method_map: tuple[tuple[str, str], ...]
    options: dict[str, dict[str, str]]
    click_help: bool = False
    """Also extract the docstrings of Click commands (the CLI's help text)."""
    extractable: bool = True
    """False for a hand-maintained catalog with no ``.pot`` (Click's own text)."""

    @property
    def locale_dir(self) -> Path:
        """Directory holding ``<lang>/LC_MESSAGES/<name>.po``."""
        return self.package_dir / "locale"

    @property
    def pot_path(self) -> Path:
        """The template every ``.po`` is merged from."""
        return self.locale_dir / f"{self.name}.pot"

    def po_path(self, language: str) -> Path:
        """Path of a language's editable catalog."""
        return self.locale_dir / language / "LC_MESSAGES" / f"{self.name}.po"

    def mo_path(self, language: str) -> Path:
        """Path of a language's compiled catalog."""
        return self.po_path(language).with_suffix(".mo")

    def languages(self) -> list[str]:
        """Languages that have a ``.po`` file, sorted."""
        pattern = f"*/LC_MESSAGES/{self.name}.po"
        return sorted(p.parent.parent.name for p in self.locale_dir.glob(pattern))


_CLICK_DECORATORS = {"command", "group"}


def _is_click_command(node: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    for decorator in node.decorator_list:
        target = decorator.func if isinstance(decorator, ast.Call) else decorator
        name = (
            target.attr
            if isinstance(target, ast.Attribute)
            else getattr(target, "id", "")
        )
        if name in _CLICK_DECORATORS:
            return True
    return False


def click_docstrings(package_dir: Path) -> Iterator[tuple[str, str]]:
    """Yield the help text (docstring) of every Click command in a package.

    A Click command's docstring is its help text, so it is a message id like
    any other; ``kidsplay_cli.i18n.translate_help`` looks it up at runtime.

    Args:
        package_dir: The package to scan.

    Yields:
        ``(file relative to the package, docstring)`` pairs.
    """
    for path in sorted(package_dir.rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and (
                _is_click_command(node)
            ):
                docstring = ast.get_docstring(node, clean=True)
                if docstring:
                    yield path.relative_to(package_dir).as_posix(), docstring


_JINJA = {
    "extensions": "jinja2.ext.i18n",
    "trimmed": "true",
    "newstyle_gettext": "true",
}

DOMAINS: dict[str, Domain] = {
    "server": Domain(
        "kidsplay_server",
        ROOT / "packages/kidsplay-server/src/kidsplay_server",
        (_PYTHON, ("**/templates/**.html", "jinja2")),
        {"**/templates/**.html": _JINJA},
    ),
    "cli": Domain(
        "kidsplay_cli",
        ROOT / "packages/kidsplay-cli/src/kidsplay_cli",
        (_PYTHON,),
        {},
        click_help=True,
    ),
    # Click's own messages ("Usage:", "Error: ...", "Show this message and exit.")
    # are looked up by Click through the process-wide gettext "messages" domain.
    # There is nothing to extract: the list follows Click, and is maintained
    # by hand in locale/<lang>/LC_MESSAGES/messages.po.
    "click": Domain(
        "messages",
        ROOT / "packages/kidsplay-cli/src/kidsplay_cli",
        (),
        {},
        extractable=False,
    ),
    "device": Domain(
        "kidsplay_device",
        ROOT / "packages/kidsplay-device/src/kidsplay_device",
        (_PYTHON,),
        {},
    ),
}


def extract_template(domain: Domain) -> Catalog:
    """Extract every translatable message of a package into a template.

    Args:
        domain: The package to scan.

    Returns:
        A catalog with the message ids and where they were found (file only,
        no line numbers, so the ``.pot`` does not churn on unrelated edits).
    """
    catalog = Catalog(
        project=domain.name,
        version="0.1.0",
        charset="utf-8",
        fuzzy=False,
        # Fixed, so re-extracting an unchanged source rewrites identical files.
        creation_date=_FIXED_DATE,
        revision_date=_FIXED_DATE,
        header_comment=_HEADER_COMMENT.format(name=domain.name),
        copyright_holder="KidsPlay contributors",
        msgid_bugs_address="https://github.com/anthonytw/kidsplay/issues",
        last_translator="KidsPlay contributors",
        language_team="",
    )
    for filename, _lineno, message, comments, context in extract_from_dir(
        str(domain.package_dir),
        method_map=list(domain.method_map),
        options_map=domain.options,
        keywords=KEYWORDS,  # ty: ignore[invalid-argument-type] # Babel's stub wants a narrower dict than the documented (int | tuple) values
        comment_tags=("i18n:",),
    ):
        catalog.add(
            message,
            None,
            [(filename.replace("\\", "/"), 0)],
            auto_comments=comments,
            context=context,
        )
    if domain.click_help:
        for filename, docstring in click_docstrings(domain.package_dir):
            catalog.add(docstring, None, [(filename, 0)])
    return catalog


def _write_po(catalog: Catalog, path: Path, *, template: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as handle:
        pofile.write_po(
            handle,
            catalog,
            width=88,
            omit_header=False,
            include_lineno=False,
            sort_output=True,
            ignore_obsolete=template,
        )


def _fresh_template_text(domain: Domain) -> str:
    buffer = io.BytesIO()
    pofile.write_po(
        buffer,
        extract_template(domain),
        width=88,
        include_lineno=False,
        sort_output=True,
        ignore_obsolete=True,
    )
    return buffer.getvalue().decode()


def read_po(path: Path) -> Catalog:
    """Read a ``.po`` file.

    Args:
        path: The file to read.

    Returns:
        Its catalog.
    """
    with path.open("rb") as handle:
        return pofile.read_po(handle)


def _as_tuple(value: object) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return (value,)
    return tuple(str(part) for part in value)  # ty: ignore[not-iterable] # Babel ids/strings are str, tuple or list


def _translations(
    catalog: Catalog, *, compiled_only: bool
) -> dict[tuple[str, ...], tuple[str, ...]]:
    """Map each message id to its translation.

    Args:
        catalog: A catalog read from ``.po`` or ``.mo``.
        compiled_only: Keep only what ``compile`` writes: translated, not fuzzy.

    Returns:
        ``{message id parts: translation parts}``.
    """
    result: dict[tuple[str, ...], tuple[str, ...]] = {}
    for message in catalog:
        if not message.id:
            continue
        strings = _as_tuple(message.string)
        if compiled_only and (message.fuzzy or not any(strings)):
            continue
        result[_as_tuple(message.id)] = strings
    return result


def extract(domain: Domain) -> None:
    """Refresh a package's ``.pot`` and merge it into every ``.po``.

    Args:
        domain: The package to process.
    """
    if not domain.extractable:
        return
    template = extract_template(domain)
    _write_po(template, domain.pot_path, template=True)
    for language in domain.languages():
        catalog = read_po(domain.po_path(language))
        catalog.update(template, no_fuzzy_matching=False)
        _write_po(catalog, domain.po_path(language))


def compile_catalogs(domain: Domain) -> None:
    """Compile every ``.po`` of a package into a ``.mo``.

    Args:
        domain: The package to process.
    """
    for language in domain.languages():
        catalog = read_po(domain.po_path(language))
        with domain.mo_path(language).open("wb") as handle:
            mofile.write_mo(handle, catalog)


def add_language(domain: Domain, language: str) -> None:
    """Create an empty catalog for a new language.

    Args:
        domain: The package to add it to.
        language: The language code, e.g. ``"fr"``.
    """
    if domain.po_path(language).exists():
        return
    if not domain.extractable:
        blank = Catalog(
            locale=language, fuzzy=False, language_team="KidsPlay contributors"
        )
        _write_po(blank, domain.po_path(language))
        return
    if not domain.pot_path.exists():
        extract(domain)
    with domain.pot_path.open("rb") as handle:
        template = pofile.read_po(handle)
    template.locale = language
    template.language_team = "KidsPlay contributors"
    _write_po(template, domain.po_path(language))


SAME_AS_SOURCE = {
    # Spanish spells it the same, or it is a technical example a translator
    # would leave alone.
    "Error:",
    "Error: {detail}",
    "/mnt/source/music",
    "https://example.com/image.jpg",
    "https://example.com/photo.jpg",
    "stderr",
    # Spanish spells it the same; or nothing but placeholders and symbols.
    "no",
    "ID",
    "  ID:         [dim]{value}[/dim]",
    "[bold red]Error:[/bold red] {msg}",
    "  [red]✗[/red] {path}: {errors}",
    "{source:.1f} LUFS → {target:g} LUFS ({gain:+.1f} dB)",
    "Error: {e.message}",
    "Error: {message}",
}
"""Message ids a finished catalog may leave identical to the English."""

_PLACEHOLDER = re.compile(
    r"%\([^)]+\)[sdif]|%[sdif]|%%|\{[^{}\s]*\}|</?[A-Za-z][^>]*>|&[#\w]+;"
)


def placeholder_problems(message_id: str, translation: str) -> list[str]:
    """Compare the placeholders and markup of a message and its translation.

    Translators may reorder them but must not drop, add or alter one: that
    would crash a ``%``/``format`` call or break the page's markup.

    Args:
        message_id: The English message (or plural) id.
        translation: Its translation.

    Returns:
        A description of each difference; empty when they match.
    """
    wanted = Counter(_PLACEHOLDER.findall(message_id))
    got = Counter(_PLACEHOLDER.findall(translation))
    if wanted == got:
        return []
    missing = sorted((wanted - got).elements())
    extra = sorted((got - wanted).elements())
    return [f"missing {missing} extra {extra} in {translation!r}"]


def completeness_problems(domain: Domain) -> list[str]:
    """Find untranslated messages in every *supported* language.

    A language counts as supported once it is listed in
    ``kidsplay_models.SUPPORTED_LANGUAGES``; until then its catalog may be a
    work in progress. English is the source and has no catalog.

    Args:
        domain: The package to check.

    Returns:
        One line per empty, fuzzy or unchanged-English message.
    """
    problems: list[str] = []
    for language in domain.languages():
        if language not in SUPPORTED_LANGUAGES:
            continue
        for message in read_po(domain.po_path(language)):
            if not message.id:
                continue
            ids = _as_tuple(message.id)
            strings = _as_tuple(message.string)
            if message.fuzzy or not all(strings) or not strings:
                problems.append(f"{language}: untranslated: {ids[0]!r}")
            elif strings[0] == ids[0] and ids[0] not in SAME_AS_SOURCE:
                problems.append(f"{language}: still English: {ids[0]!r}")
    return problems


def translation_problems(domain: Domain) -> list[str]:
    """Find placeholder mismatches in every language of a package.

    Args:
        domain: The package to check.

    Returns:
        One line per problem.
    """
    problems: list[str] = []
    for language in domain.languages():
        for message in read_po(domain.po_path(language)):
            if not message.id or message.fuzzy:
                continue
            ids = _as_tuple(message.id)
            for index, translation in enumerate(_as_tuple(message.string)):
                if not translation:
                    continue
                source = ids[min(index, len(ids) - 1)]
                if len(ids) > 1 and index > 0:
                    source = ids[1]
                for problem in placeholder_problems(source, translation):
                    problems.append(f"{language}: {ids[0]!r}: {problem}")
    return problems


def check(domain: Domain) -> list[str]:
    """Report what is out of date in a package's catalogs.

    Args:
        domain: The package to check.

    Returns:
        One human-readable problem per line; empty when everything is current.
    """
    problems: list[str] = completeness_problems(domain)
    problems += translation_problems(domain)
    if domain.extractable:
        if not domain.pot_path.exists():
            return [f"{domain.pot_path}: missing (run `just i18n-extract`)"]
        if domain.pot_path.read_text() != _fresh_template_text(domain):
            problems.append(f"{domain.pot_path}: stale (run `just i18n-extract`)")
    for language in domain.languages():
        po = read_po(domain.po_path(language))
        mo_path = domain.mo_path(language)
        if not mo_path.exists():
            problems.append(f"{mo_path}: missing (run `just i18n-compile`)")
            continue
        with mo_path.open("rb") as handle:
            mo = mofile.read_mo(handle)
        wanted = _translations(po, compiled_only=True)
        got = _translations(mo, compiled_only=False)
        if wanted != got:
            problems.append(f"{mo_path}: stale (run `just i18n-compile`)")
    return problems


def main(argv: list[str] | None = None) -> int:
    """Command-line entry point.

    Args:
        argv: Arguments; defaults to ``sys.argv[1:]``.

    Returns:
        The process exit status.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("extract")
    sub.add_parser("compile")
    sub.add_parser("check")
    add = sub.add_parser("add-language")
    add.add_argument("language")
    args = parser.parse_args(argv)

    status = 0
    for domain in DOMAINS.values():
        if args.command == "extract":
            extract(domain)
        elif args.command == "compile":
            compile_catalogs(domain)
        elif args.command == "add-language":
            add_language(domain, args.language)
        else:
            for problem in check(domain):
                print(problem, file=sys.stderr)
                status = 1
    return status


if __name__ == "__main__":
    sys.exit(main())
