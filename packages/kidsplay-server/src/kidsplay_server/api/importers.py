"""Importer discovery endpoints.

Routes
------
GET /importers        -- list installed importers
GET /importers/match  -- find the importer that would handle a source
"""

from fastapi import APIRouter, HTTPException

from kidsplay_server.importers import ImporterInfo, SourceNeedsPluginError

from .deps import Importers

router = APIRouter(tags=["importers"])


@router.get("/importers", response_model=list[ImporterInfo])
async def list_importers(importers: Importers) -> list[ImporterInfo]:
    """List the installed importers in resolution order.

    Plugins come first, then the built-in ``local`` and ``http`` importers.

    Args:
        importers: Installed importers (injected).

    Returns:
        One ``ImporterInfo`` per importer.
    """
    return [ImporterInfo.of(i) for i in importers]


@router.get("/importers/match", response_model=ImporterInfo)
async def match_importer(source: str, importers: Importers) -> ImporterInfo:
    """Return the importer that would handle *source*.

    Args:
        source: A URL or server-side path.
        importers: Installed importers (injected).

    Returns:
        The first importer whose ``can_handle`` matches.

    Raises:
        HTTPException: 404 ``NO_IMPORTER`` if none matches, or 422
            ``PLUGIN_REQUIRED`` if the source needs a plugin that is not
            installed (a YouTube URL without the yt-dlp plugin).
    """
    try:
        importers.require_plugin(source)
    except SourceNeedsPluginError as exc:
        raise HTTPException(
            status_code=422,
            detail={"detail": str(exc), "error_code": "PLUGIN_REQUIRED"},
        ) from exc
    importer = importers.resolve(source)
    if importer is None:
        raise HTTPException(
            status_code=404,
            detail={
                "detail": f"No installed importer can handle {source}",
                "error_code": "NO_IMPORTER",
            },
        )
    return ImporterInfo.of(importer)
