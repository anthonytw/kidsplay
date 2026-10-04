"""KidsPlay importer plugin for YouTube, backed by yt-dlp.

Installing this package registers ``YtDlpImporter`` under the
``kidsplay.importers`` entry-point group; the server discovers it at startup.
"""

from .importer import YtDlpImporter

__all__ = ["YtDlpImporter"]
