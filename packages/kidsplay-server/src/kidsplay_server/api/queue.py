"""Import queue endpoints.

Slow or rate-limited importers (``requires_queue``, e.g. YouTube) run in the
background worker instead of inside the HTTP request. Any importer can be
queued, though.

Routes
------
POST   /queue                 -- submit a source to the import queue
GET    /queue                 -- list queue items (with optional status filter)
GET    /queue/{item_id}       -- get a single queue item with full log
DELETE /queue/{item_id}       -- cancel/remove a queue item
POST   /queue/{item_id}/retry -- reset a failed item back to pending
"""

import logging
import uuid
from datetime import datetime

from fastapi import APIRouter, HTTPException
from fastapi.responses import Response

from kidsplay_models import QueueItem, QueueRequest
from kidsplay_models.queue import LOUDNESS_JOB, QueueStatus
from kidsplay_server import database as queue_db
from kidsplay_server.importers import SourceNeedsPluginError

from .deps import DBConn, Importers

router = APIRouter(tags=["queue"])
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Submit
# ---------------------------------------------------------------------------


@router.post("/queue", response_model=QueueItem, status_code=201)
async def submit_to_queue(
    req: QueueRequest,
    db: DBConn,
    importers: Importers,
) -> QueueItem:
    """Submit a source to the import queue.

    The importer is the one named in the request, or else the first
    installed importer that can handle the URL. The URL is normalized by
    that importer before it is stored (for YouTube this strips radio/mix and
    tracking params, so a single-song submission stays one song).

    The item starts in ``pending`` status and will be picked up by the
    background worker. The importer sees the attempt number, so it can back
    off further on each retry.

    Args:
        req: Queue submission request with URL and ingest parameters.
        db: Database connection (injected).
        importers: Installed importers (injected).

    Returns:
        The created ``QueueItem``.

    Raises:
        HTTPException: 422 ``UNKNOWN_IMPORTER`` if the named importer is not
            installed, ``NO_IMPORTER`` if no importer can handle the URL, or
            ``PLUGIN_REQUIRED`` if the URL needs a plugin that is not
            installed (a YouTube URL without the yt-dlp plugin).
    """
    try:
        importers.require_plugin(req.url)
    except SourceNeedsPluginError as exc:
        raise HTTPException(
            status_code=422,
            detail={"detail": str(exc), "error_code": "PLUGIN_REQUIRED"},
        ) from exc
    if req.importer is not None:
        importer = importers.get(req.importer)
        if importer is None:
            raise HTTPException(
                status_code=422,
                detail={
                    "detail": f"Importer {req.importer!r} is not installed",
                    "error_code": "UNKNOWN_IMPORTER",
                },
            )
    else:
        importer = importers.resolve(req.url)
        if importer is None:
            raise HTTPException(
                status_code=422,
                detail={
                    "detail": f"No installed importer can handle {req.url}",
                    "error_code": "NO_IMPORTER",
                },
            )

    now = datetime.now()
    url = importer.normalize(req.url)
    item = QueueItem(
        url=url,
        importer=importer.name,
        media_type=req.media_type,
        playlist_title=req.playlist_title,
        profile_ids=req.profile_ids,
        title_override=req.title_override,
        artist_override=req.artist_override,
        max_retries=req.max_retries,
        status=QueueStatus.PENDING,
        created_at=now,
        updated_at=now,
    )
    await queue_db.create_queue_item(db, item)
    await db.commit()
    logger.info(
        "Queued %s import %s: %s type=%s playlist=%r",
        importer.name,
        item.id,
        url,
        req.media_type,
        req.playlist_title,
    )
    return item


async def _get_import_or_404(db: DBConn, item_id: uuid.UUID) -> QueueItem:
    """Return an import queue item, or raise 404.

    Loudness-normalization jobs share the table but are not imports: the list
    hides them, and they are progress of the library-wide backfill, which
    groups them by creation time and counts them. Deleting or retrying one
    here would corrupt that state, so they answer 404 like any unknown ID.
    """
    item = await queue_db.get_queue_item(db, item_id)
    if item is None or item.importer == LOUDNESS_JOB:
        raise HTTPException(
            status_code=404,
            detail={
                "detail": "Queue item not found",
                "error_code": "NOT_FOUND",
            },
        )
    return item


# ---------------------------------------------------------------------------
# List / Get
# ---------------------------------------------------------------------------


@router.get("/queue", response_model=list[QueueItem])
async def list_queue(
    db: DBConn,
    status: QueueStatus | None = None,
    limit: int = 100,
    offset: int = 0,
) -> list[QueueItem]:
    """List import queue items.

    Args:
        db: Database connection (injected).
        status: Optional filter by queue status.
        limit: Maximum results (default 100).
        offset: Pagination offset.

    Returns:
        List of ``QueueItem`` instances, newest first.
    """
    return await queue_db.list_queue_items(
        db, status=status, limit=limit, offset=offset
    )


@router.get("/queue/{item_id}", response_model=QueueItem)
async def get_queue_item(
    item_id: uuid.UUID,
    db: DBConn,
) -> QueueItem:
    """Fetch a single queue item by ID, including its full log.

    Args:
        item_id: UUID of the queue item.
        db: Database connection (injected).

    Returns:
        The ``QueueItem`` with full command/output log.

    Raises:
        HTTPException: 404 if not found.
    """
    item = await _get_import_or_404(db, item_id)
    return item


# ---------------------------------------------------------------------------
# Delete / Retry
# ---------------------------------------------------------------------------


@router.delete("/queue/{item_id}", status_code=204)
async def delete_queue_item(
    item_id: uuid.UUID,
    db: DBConn,
) -> Response:
    """Delete a queue item.

    Running items are cancelled. Completed/failed items are removed
    from history.

    Args:
        item_id: UUID of the queue item.
        db: Database connection (injected).

    Returns:
        204 No Content.

    Raises:
        HTTPException: 404 if not found.
    """
    item = await _get_import_or_404(db, item_id)
    await queue_db.delete_queue_item(db, item_id)
    await db.commit()
    logger.info("Deleted queue item %s (%s): %s", item_id, item.status, item.url)
    return Response(status_code=204)


@router.post("/queue/{item_id}/retry", response_model=QueueItem)
async def retry_queue_item(
    item_id: uuid.UUID,
    db: DBConn,
) -> QueueItem:
    """Reset a failed queue item back to pending for another round of retries.

    Resets the attempt counter to 0 and clears the log so the item
    gets a fresh set of retries.

    Args:
        item_id: UUID of the queue item.
        db: Database connection (injected).

    Returns:
        The updated ``QueueItem``.

    Raises:
        HTTPException: 404 if not found, 409 if not in a retryable state.
    """
    item = await _get_import_or_404(db, item_id)

    if item.status not in (QueueStatus.FAILED, QueueStatus.CANCELLED):
        raise HTTPException(
            status_code=409,
            detail={
                "detail": (
                    f"Cannot retry item in '{item.status}' state. "
                    "Only failed or cancelled items can be retried."
                ),
                "error_code": "INVALID_STATE",
            },
        )

    await queue_db.update_queue_item(
        db,
        item_id,
        status=QueueStatus.PENDING,
        attempt=0,
        last_error="",
        log="",
    )
    await db.commit()
    logger.info("Retrying queue item %s: %s", item_id, item.url)

    updated = await queue_db.get_queue_item(db, item_id)
    assert updated is not None
    return updated
