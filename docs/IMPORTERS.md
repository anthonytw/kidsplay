# Importers

An **importer** fetches media from one kind of source into a scratch directory.
Everything after that (hashing, transcoding, thumbnails, storage, profile
assignment) is the core pipeline's job, so an importer only ever *fetches*.

The server ships two importers:

| Name    | Label                | Handles                                    |
|---------|----------------------|--------------------------------------------|
| `local` | Local file or folder | An existing path on the server             |
| `http`  | Web URL              | Any `http://` or `https://` file URL       |

Everything else is a plugin, discovered through the Python entry-point group
`kidsplay.importers`. YouTube support is one such plugin,
[`kidsplay-importer-ytdlp`](../packages/kidsplay-importer-ytdlp), so deployers
who don't want yt-dlp can leave it out.

## Installing or leaving out the YouTube plugin

```bash
uv sync --package kidsplay-server           # core only: no yt-dlp installed
uv sync --package kidsplay-importer-ytdlp   # core + YouTube
uv sync --all-packages                      # development: everything
```

The Docker image installs the plugin by default. Build with
`--build-arg KIDSPLAY_WITH_YTDLP=0` (or set `KIDSPLAY_WITH_YTDLP=0` in
`docker/.env` for Compose) for a core-only image. The value may be
`1`/`true`/`yes`/`on` or `0`/`false`/`no`/`off` (any case); anything else fails
the build rather than silently building core only.

Without the plugin, a YouTube URL is refused: `POST /queue` and
`GET /importers/match` answer `422 PLUGIN_REQUIRED`, `POST /media/ingest`
reports it as the failure, and a job already queued for it (from before the
plugin was removed) fails at once instead of retrying against YouTube's web
page. The message names the plugin to install.

The plugin reads `KIDSPLAY_YT_COOKIES`: an optional path to a Netscape-format
`cookies.txt` passed to yt-dlp with `--cookies` when YouTube asks for a sign-in
(private videos). Export it from a logged-in browser session.

To see what a running server has installed:

```bash
uv run kidsplay importer list          # CLI
curl http://localhost:8000/api/v1/importers
```

When no installed importer supports previews, the web UI's YouTube tab is not
shown, and `POST /api/v1/media/preview` answers `422 PREVIEW_UNSUPPORTED`.

## Writing an importer

An importer is any object with these members (the `Importer` protocol in
`kidsplay_server.importers`). Subclassing `BaseImporter` supplies the optional
ones.

| Member | Required | Meaning |
|--------|----------|---------|
| `name: str` | yes | Stable machine name, unique across installed importers. Stored with queued jobs, so never rename it after release. |
| `label: str` | yes | Human-readable name for the web UI and CLI. |
| `can_handle(source) -> bool` | yes | Whether this importer should fetch `source` (a URL or server path). Keep it cheap and side-effect free. |
| `async fetch(source, workdir, ctx) -> list[FetchedItem]` | yes | Download into `workdir` and return the files. Raise any exception to fail; its message is shown to the user. |
| `requires_queue: bool` | no (`False`) | `True` for slow or rate-limited sources. The web UI sends them through the background queue, which retries failures. |
| `normalize(source) -> str` | no (identity) | Canonical form of the source, applied before a job is queued (e.g. strip tracking parameters). |
| `async preview(source, max_items) -> ImportPreview` | no | List a video/playlist's tracks without downloading. Implementing it adds the preview-and-queue tab to the web UI. Raise `PreviewError` on failure. |

`FetchedItem` is a file path plus optional `title`, `artist` and `thumbnail`
(an image path used as artwork when the file has none). The user's own title
and artist overrides win over yours.

`ctx` is a `FetchContext`:

- `ctx.attempt` is the 1-based attempt number. It only goes above 1 when the
  queue retries a failed job, so use it to back off.
- `ctx.queued` is `True` inside the background worker.
- `ctx.log(text)` adds debug output (commands run, responses) to the job's log
  on the web UI's Queue page.

`workdir` is an empty temporary directory, deleted once the pipeline has
ingested your files. Returning files outside it is fine (the `local` importer
does), as long as they still exist when `fetch` returns.

### Which importer handles a source

Plugins are asked first, in entry-point name order, then `local` and `http`.
The first importer whose `can_handle` returns `True` wins, so a specific
importer (YouTube) takes precedence over the generic `http` one. A plugin whose
`name` repeats an earlier importer's is ignored, and a plugin that fails to
import is logged and skipped: the server still starts.

### Minimal example

A plugin that imports Dropbox shared links. Dropbox serves an HTML page for
`?dl=0` links and the file itself for `?dl=1`, so the importer rewrites the
link and downloads it.

`pyproject.toml`:

```toml
[project]
name = "kidsplay-importer-dropbox"
version = "0.1.0"
requires-python = ">=3.12"
dependencies = ["kidsplay-server"]

[project.entry-points."kidsplay.importers"]
dropbox = "kidsplay_importer_dropbox:DropboxImporter"

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"
```

`src/kidsplay_importer_dropbox/__init__.py`:

```python
"""KidsPlay importer for Dropbox shared links."""

from pathlib import Path
from urllib.parse import urlparse

from kidsplay_server.importers import BaseImporter, FetchContext, FetchedItem
from kidsplay_server.processing.download import download_from_url


class DropboxImporter(BaseImporter):
    """Download the file behind a Dropbox shared link."""

    name = "dropbox"
    label = "Dropbox"

    def can_handle(self, source: str) -> bool:
        """Return True for www.dropbox.com links."""
        return urlparse(source).hostname == "www.dropbox.com"

    def normalize(self, source: str) -> str:
        """Ask Dropbox for the file rather than its preview page."""
        return source.replace("dl=0", "dl=1")

    async def fetch(
        self, source: str, workdir: Path, ctx: FetchContext
    ) -> list[FetchedItem]:
        """Download the shared file into workdir."""
        url = self.normalize(source)
        ctx.log(f"GET {url}")
        return [FetchedItem(path=await download_from_url(url, workdir))]
```

Install it into the server's environment (for this workspace, add it as a
member in the root `pyproject.toml` and run `uv sync --all-packages`), restart
the server, and it appears in `GET /api/v1/importers`. Test it by building a
registry by hand and passing it to the app:

```python
from kidsplay_server.api.app import create_app
from kidsplay_server.importers import ImporterRegistry, builtin_importers

registry = ImporterRegistry([DropboxImporter(), *builtin_importers()])
app = create_app(db_path, media_root, importers=registry)
```

For a fuller example with a queue, retries, cookies and previews, read
`packages/kidsplay-importer-ytdlp/src/kidsplay_importer_ytdlp/importer.py`.
