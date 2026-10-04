"""Importer discovery and lookup.

Third-party importers register an object under the ``kidsplay.importers``
entry-point group::

    [project.entry-points."kidsplay.importers"]
    myimporter = "my_package.importer:MyImporter"

The object may be an importer instance or a class that takes no arguments.
A plugin that fails to load is logged and skipped, so a broken or missing
plugin never stops the server from starting.
"""

import functools
import logging
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from importlib.metadata import entry_points
from urllib.parse import urlparse

from .base import Importer
from .builtin import builtin_importers

logger = logging.getLogger(__name__)

ENTRY_POINT_GROUP = "kidsplay.importers"


@dataclass(frozen=True)
class KnownSource:
    """A source that only an optional plugin can import.

    Without the plugin, the generic ``http`` importer would claim the URL and
    download a web page instead of media. Naming the source here lets the
    server refuse it with an actionable message.

    Attributes:
        label: Human-readable source name, e.g. ``YouTube``.
        plugin_label: What users call the plugin, e.g. ``yt-dlp``.
        importer: ``Importer.name`` the plugin registers.
        plugin: Name of the package that provides the importer.
        hosts: Hostnames whose URLs belong to this source.
    """

    label: str
    plugin_label: str
    importer: str
    plugin: str
    hosts: frozenset[str]


YOUTUBE_HOSTS: frozenset[str] = frozenset(
    {
        "youtube.com",
        "www.youtube.com",
        "m.youtube.com",
        "music.youtube.com",
        "youtu.be",
        "youtube-nocookie.com",
        "www.youtube-nocookie.com",
    }
)
"""Hostnames of YouTube URLs. The one definition: the core refuses them
without the yt-dlp plugin, and the plugin (which imports it) claims them."""

KNOWN_SOURCES: tuple[KnownSource, ...] = (
    KnownSource(
        label="YouTube",
        plugin_label="yt-dlp",
        importer="ytdlp",
        plugin="kidsplay-importer-ytdlp",
        hosts=YOUTUBE_HOSTS,
    ),
)


class SourceNeedsPluginError(RuntimeError):
    """A source is known, but the plugin that imports it is not installed."""

    def __init__(self, source: KnownSource) -> None:
        super().__init__(
            f"{source.label} import needs the {source.plugin_label} plugin "
            f"({source.plugin}), "
            "which is not installed. Install it and restart the server; "
            "see docs/IMPORTERS.md."
        )
        self.source = source


class ImporterRegistry:
    """An ordered set of importers with lookup by name and by source.

    Order matters: ``resolve`` returns the first importer whose
    ``can_handle`` matches, so specific importers must precede generic ones.

    Args:
        importers: Importers in resolution order. A later importer whose
            ``name`` repeats an earlier one is dropped with a warning.
    """

    def __init__(self, importers: Iterable[Importer]) -> None:
        self._importers: list[Importer] = []
        for importer in importers:
            if self.get(importer.name) is not None:
                logger.warning(
                    "Ignoring importer %r: an importer with that name is "
                    "already registered",
                    importer.name,
                )
                continue
            self._importers.append(importer)

    def __iter__(self) -> Iterator[Importer]:
        return iter(self._importers)

    def __len__(self) -> int:
        return len(self._importers)

    def get(self, name: str) -> Importer | None:
        """Return the importer called *name*.

        Args:
            name: ``Importer.name`` to look up.

        Returns:
            The importer, or ``None`` if none is installed under that name.
        """
        return next((i for i in self._importers if i.name == name), None)

    def require_plugin(self, source: str) -> None:
        """Refuse a URL that needs a plugin which is not installed.

        Without this, such a URL resolves to the generic ``http`` importer,
        which downloads the site's web page and fails (a queued job then
        retries that pointlessly).

        Args:
            source: A URL or server-side path.

        Raises:
            SourceNeedsPluginError: If *source* belongs to a known source
                (``KNOWN_SOURCES``) whose importer is not installed.
        """
        hostname = urlparse(source).hostname or ""
        for known in KNOWN_SOURCES:
            if hostname in known.hosts and self.get(known.importer) is None:
                raise SourceNeedsPluginError(known)

    def resolve(self, source: str) -> Importer | None:
        """Return the first importer that can handle *source*.

        An importer whose ``can_handle`` raises is logged and skipped.

        Args:
            source: A URL or server-side path.

        Returns:
            The matching importer, or ``None``.
        """
        for importer in self._importers:
            try:
                if importer.can_handle(source):
                    return importer
            except Exception:
                logger.exception("Importer %r failed in can_handle", importer.name)
        return None


def _load_plugins() -> list[Importer]:
    plugins: list[Importer] = []
    for ep in sorted(entry_points(group=ENTRY_POINT_GROUP), key=lambda e: e.name):
        try:
            obj = ep.load()
            importer = obj() if isinstance(obj, type) else obj
        # SystemExit too: a plugin calling sys.exit() at import must not take
        # the server down with it.
        except (Exception, SystemExit):
            logger.exception(
                "Failed to load importer plugin %r (%s)", ep.name, ep.value
            )
            continue
        if not isinstance(importer, Importer):
            logger.error(
                "Entry point %r (%s) is not an importer; skipping", ep.name, ep.value
            )
            continue
        plugins.append(importer)
    return plugins


def discover_importers() -> ImporterRegistry:
    """Load plugin importers from entry points, followed by the built-ins.

    Plugins come first so that a specific importer (YouTube) wins over the
    generic HTTP one for URLs both can handle. Plugins are ordered by
    entry-point name.

    Returns:
        A new registry.
    """
    registry = ImporterRegistry([*_load_plugins(), *builtin_importers()])
    logger.info("Importers available: %s", ", ".join(i.name for i in registry))
    return registry


@functools.cache
def get_default_registry() -> ImporterRegistry:
    """Return the process-wide registry, discovering importers on first use.

    Returns:
        The shared registry.
    """
    return discover_importers()
