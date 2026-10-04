"""Pluggable media importers.

See ``base`` for the contract, ``builtin`` for the core importers and
``registry`` for entry-point discovery.
"""

from .base import (
    BaseImporter,
    FetchContext,
    FetchedItem,
    Importer,
    ImporterInfo,
    ImportPreview,
    PreviewDebug,
    PreviewError,
    SupportsPreview,
    TrackPreview,
)
from .builtin import HttpImporter, LocalImporter, builtin_importers
from .registry import (
    ENTRY_POINT_GROUP,
    KNOWN_SOURCES,
    YOUTUBE_HOSTS,
    ImporterRegistry,
    KnownSource,
    SourceNeedsPluginError,
    discover_importers,
    get_default_registry,
)

__all__ = [
    "BaseImporter",
    "ENTRY_POINT_GROUP",
    "FetchContext",
    "FetchedItem",
    "HttpImporter",
    "KNOWN_SOURCES",
    "ImportPreview",
    "Importer",
    "ImporterInfo",
    "ImporterRegistry",
    "KnownSource",
    "LocalImporter",
    "PreviewDebug",
    "PreviewError",
    "SourceNeedsPluginError",
    "YOUTUBE_HOSTS",
    "SupportsPreview",
    "TrackPreview",
    "builtin_importers",
    "discover_importers",
    "get_default_registry",
]
