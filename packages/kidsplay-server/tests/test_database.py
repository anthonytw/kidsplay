"""Tests for kidsplay_server.database — async SQLite CRUD layer.

All tests use a real SQLite file in tmp_path via the db fixture.
No mocking. Each test calls conn.commit() after writes that need to
persist for subsequent reads (delete_media_item commits internally).
"""

import sqlite3
import stat
import uuid
from datetime import datetime
from pathlib import Path

import aiosqlite
import pytest

from kidsplay_models import (
    Device,
    MediaItem,
    ProcessedFile,
    Profile,
    ProfileMediaAssignment,
    QueueItem,
)
from kidsplay_models.media import MediaType
from kidsplay_models.queue import QueueStatus
from kidsplay_server.api.app import create_app
from kidsplay_server.database import (
    assign_media_to_profile,
    claim_next_queue_item,
    configure_conn,
    create_device,
    create_media_item,
    create_processed_file,
    create_profile,
    create_queue_item,
    delete_device,
    delete_media_item,
    delete_profile,
    delete_queue_item,
    ensure_private_db_file,
    get_device,
    get_device_by_api_key,
    get_media_item,
    get_media_item_by_hash,
    get_processed_file_by_hash,
    get_profile,
    get_queue_item,
    init_db,
    list_devices,
    list_media_for_profile,
    list_media_items,
    list_processed_files,
    list_profiles,
    list_profiles_for_media,
    list_queue_items,
    unassign_media_from_profile,
    update_device,
    update_device_sync,
    update_media_item_loudness,
    update_media_item_status,
    update_processed_file,
    update_queue_item,
)

# ---------------------------------------------------------------------------
# Factories
# ---------------------------------------------------------------------------


def make_profile(name: str = "Leo") -> Profile:
    return Profile(name=name)


def make_device(profile_id: uuid.UUID, name: str = "Leo's GameBoy") -> Device:
    return Device(name=name, profile_id=profile_id)


def make_media(
    playlist_title: str = "Pica-Pica",
    title: str = "La Bamba",
    media_type: MediaType = MediaType.MUSIC,
    content_hash: str | None = None,
) -> MediaItem:
    return MediaItem(
        media_type=media_type,
        content_hash=content_hash or uuid.uuid4().hex,
        playlist_title=playlist_title,
        title=title,
        artist="Test Artist",
        duration_seconds=180,
    )


def make_processed_file(media_id: uuid.UUID, file_type: str = "audio") -> ProcessedFile:
    h = uuid.uuid4().hex
    return ProcessedFile(
        media_id=media_id,
        content_hash=h,
        file_type=file_type,
        relative_path=f"audio/{h[:2]}/{h}.mp3",
        size_bytes=1024,
        mime_type="audio/mpeg",
    )


# ---------------------------------------------------------------------------
# Profiles
# ---------------------------------------------------------------------------


class TestProfiles:
    async def test_create_and_get(self, db: aiosqlite.Connection) -> None:
        p = make_profile("Leo")
        await create_profile(db, p)
        await db.commit()

        fetched = await get_profile(db, p.id)
        assert fetched is not None
        assert fetched.id == p.id
        assert fetched.name == "Leo"

    async def test_get_nonexistent_returns_none(self, db: aiosqlite.Connection) -> None:
        result = await get_profile(db, uuid.uuid4())
        assert result is None

    async def test_list(self, db: aiosqlite.Connection) -> None:
        names = ["Zara", "Anna", "Leo"]
        for n in names:
            await create_profile(db, make_profile(n))
        await db.commit()

        profiles = await list_profiles(db)
        assert len(profiles) == 3
        assert [p.name for p in profiles] == sorted(names)  # ordered by name

    async def test_delete(self, db: aiosqlite.Connection) -> None:
        p = make_profile()
        await create_profile(db, p)
        await db.commit()

        await delete_profile(db, p.id)
        await db.commit()

        assert await get_profile(db, p.id) is None

    async def test_delete_with_linked_device_raises(
        self, db: aiosqlite.Connection
    ) -> None:
        p = make_profile()
        await create_profile(db, p)
        await create_device(db, make_device(p.id))
        await db.commit()

        with pytest.raises(sqlite3.IntegrityError):
            await delete_profile(db, p.id)
            await db.commit()


# ---------------------------------------------------------------------------
# Devices
# ---------------------------------------------------------------------------


class TestDevices:
    async def test_create_and_get(self, db: aiosqlite.Connection) -> None:
        p = make_profile()
        await create_profile(db, p)
        d = make_device(p.id)
        await create_device(db, d)
        await db.commit()

        fetched = await get_device(db, d.id)
        assert fetched is not None
        assert fetched.id == d.id
        assert fetched.name == d.name
        assert fetched.profile_id == p.id
        assert fetched.display_width == 640
        assert fetched.last_sync_at is None

    async def test_get_nonexistent_returns_none(self, db: aiosqlite.Connection) -> None:
        assert await get_device(db, uuid.uuid4()) is None

    async def test_get_by_api_key(self, db: aiosqlite.Connection) -> None:
        p = make_profile()
        await create_profile(db, p)
        d = make_device(p.id)
        await create_device(db, d)
        await db.commit()

        fetched = await get_device_by_api_key(db, d.api_key)
        assert fetched is not None
        assert fetched.id == d.id

    async def test_get_by_api_key_unknown_returns_none(
        self, db: aiosqlite.Connection
    ) -> None:
        assert await get_device_by_api_key(db, "no-such-key") is None

    async def test_list(self, db: aiosqlite.Connection) -> None:
        p = make_profile()
        await create_profile(db, p)
        for name in ["Zara's device", "Anna's device"]:
            await create_device(db, make_device(p.id, name))
        await db.commit()

        devices = await list_devices(db)
        assert len(devices) == 2
        assert [d.name for d in devices] == ["Anna's device", "Zara's device"]

    async def test_update_device(self, db: aiosqlite.Connection) -> None:
        p1 = make_profile("Leo")
        p2 = make_profile("Sofia")
        await create_profile(db, p1)
        await create_profile(db, p2)
        d = make_device(p1.id)
        await create_device(db, d)
        await db.commit()

        await update_device(
            db,
            d.id,
            name="Updated Name",
            profile_id=p2.id,
            display_width=800,
            display_height=600,
        )
        await db.commit()

        fetched = await get_device(db, d.id)
        assert fetched is not None
        assert fetched.name == "Updated Name"
        assert fetched.profile_id == p2.id
        assert fetched.display_width == 800
        assert fetched.display_height == 600

    async def test_update_device_sync(self, db: aiosqlite.Connection) -> None:
        p = make_profile()
        await create_profile(db, p)
        d = make_device(p.id)
        await create_device(db, d)
        await db.commit()

        sync_at = datetime(2025, 6, 1, 12, 0, 0)
        await update_device_sync(db, d.id, sync_at, "manifest-hash-abc")
        await db.commit()

        fetched = await get_device(db, d.id)
        assert fetched is not None
        assert fetched.last_sync_at == sync_at
        assert fetched.last_sync_manifest_hash == "manifest-hash-abc"

    async def test_delete(self, db: aiosqlite.Connection) -> None:
        p = make_profile()
        await create_profile(db, p)
        d = make_device(p.id)
        await create_device(db, d)
        await db.commit()

        await delete_device(db, d.id)
        await db.commit()

        assert await get_device(db, d.id) is None

    async def test_create_with_nonexistent_profile_raises(
        self, db: aiosqlite.Connection
    ) -> None:
        d = make_device(uuid.uuid4())  # profile doesn't exist
        with pytest.raises(sqlite3.IntegrityError):
            await create_device(db, d)
            await db.commit()


# ---------------------------------------------------------------------------
# MediaItems
# ---------------------------------------------------------------------------


class TestMediaItems:
    async def test_create_and_get(self, db: aiosqlite.Connection) -> None:
        m = make_media()
        await create_media_item(db, m)
        await db.commit()

        fetched = await get_media_item(db, m.id)
        assert fetched is not None
        assert fetched.id == m.id
        assert fetched.playlist_title == m.playlist_title
        assert fetched.title == m.title
        assert fetched.artist == m.artist
        assert fetched.duration_seconds == m.duration_seconds
        assert fetched.media_type == MediaType.MUSIC

    async def test_get_nonexistent_returns_none(self, db: aiosqlite.Connection) -> None:
        assert await get_media_item(db, uuid.uuid4()) is None

    async def test_get_by_hash(self, db: aiosqlite.Connection) -> None:
        m = make_media(content_hash="deadbeef" * 8)
        await create_media_item(db, m)
        await db.commit()

        fetched = await get_media_item_by_hash(db, m.content_hash)
        assert fetched is not None
        assert fetched.id == m.id

    async def test_get_by_hash_unknown_returns_none(
        self, db: aiosqlite.Connection
    ) -> None:
        assert await get_media_item_by_hash(db, "no-such-hash") is None

    async def test_duplicate_content_hash_raises(
        self, db: aiosqlite.Connection
    ) -> None:
        h = "aabbcc" * 10
        await create_media_item(db, make_media(content_hash=h))
        await db.commit()

        with pytest.raises(sqlite3.IntegrityError):
            await create_media_item(db, make_media(content_hash=h))
            await db.commit()

    async def test_list_all(self, db: aiosqlite.Connection) -> None:
        for i in range(3):
            await create_media_item(db, make_media(title=f"Song {i}"))
        await db.commit()

        items = await list_media_items(db)
        assert len(items) == 3

    async def test_list_by_media_type(self, db: aiosqlite.Connection) -> None:
        await create_media_item(db, make_media(media_type=MediaType.MUSIC))
        await create_media_item(
            db, make_media(media_type=MediaType.AUDIOBOOK, title="Chapter 1")
        )
        await db.commit()

        music = await list_media_items(db, media_type=MediaType.MUSIC)
        books = await list_media_items(db, media_type=MediaType.AUDIOBOOK)
        assert len(music) == 1
        assert len(books) == 1

    async def test_list_by_playlist_title(self, db: aiosqlite.Connection) -> None:
        await create_media_item(db, make_media(playlist_title="Album A"))
        await create_media_item(db, make_media(playlist_title="Album B", title="B1"))
        await db.commit()

        results = await list_media_items(db, playlist_title="Album A")
        assert len(results) == 1
        assert results[0].playlist_title == "Album A"

    async def test_list_by_status(self, db: aiosqlite.Connection) -> None:
        m_pending = make_media(title="Pending Song")
        m_ready = make_media(title="Ready Song")
        m_ready.processing_status = "ready"
        await create_media_item(db, m_pending)
        await create_media_item(db, m_ready)
        await db.commit()

        pending = await list_media_items(db, status="pending")
        ready = await list_media_items(db, status="ready")
        assert len(pending) == 1
        assert len(ready) == 1

    async def test_list_search_q_title(self, db: aiosqlite.Connection) -> None:
        await create_media_item(db, make_media(title="La Bamba"))
        await create_media_item(db, make_media(title="Twist and Shout"))
        await db.commit()

        results = await list_media_items(db, q="Bamba")
        assert len(results) == 1
        assert results[0].title == "La Bamba"

    async def test_list_search_q_playlist(self, db: aiosqlite.Connection) -> None:
        await create_media_item(db, make_media(playlist_title="Pica-Pica"))
        await create_media_item(db, make_media(playlist_title="Gruffalo"))
        await db.commit()

        results = await list_media_items(db, q="Pica")
        assert len(results) == 1

    async def test_list_pagination(self, db: aiosqlite.Connection) -> None:
        for i in range(5):
            await create_media_item(db, make_media(title=f"Song {i:02d}"))
        await db.commit()

        page1 = await list_media_items(db, limit=3, offset=0)
        page2 = await list_media_items(db, limit=3, offset=3)
        assert len(page1) == 3
        assert len(page2) == 2
        titles = [m.title for m in page1 + page2]
        assert len(set(titles)) == 5

    async def test_list_ordered_by_playlist_then_title(
        self, db: aiosqlite.Connection
    ) -> None:
        await create_media_item(db, make_media(playlist_title="Z", title="a"))
        await create_media_item(db, make_media(playlist_title="A", title="b"))
        await create_media_item(db, make_media(playlist_title="A", title="a"))
        await db.commit()

        items = await list_media_items(db)
        assert items[0].playlist_title == "A"
        assert items[0].title == "a"
        assert items[1].playlist_title == "A"
        assert items[1].title == "b"
        assert items[2].playlist_title == "Z"

    async def test_update_status(self, db: aiosqlite.Connection) -> None:
        m = make_media()
        await create_media_item(db, m)
        await db.commit()

        updated_at = datetime(2025, 6, 1)
        await update_media_item_status(db, m.id, "ready", updated_at)
        await db.commit()

        fetched = await get_media_item(db, m.id)
        assert fetched is not None
        assert fetched.processing_status == "ready"
        assert fetched.updated_at == updated_at

    async def test_delete_removes_item(self, db: aiosqlite.Connection) -> None:
        m = make_media()
        await create_media_item(db, m)
        await db.commit()

        await delete_media_item(db, m.id)

        assert await get_media_item(db, m.id) is None

    async def test_delete_cascades_to_processed_files(
        self, db: aiosqlite.Connection
    ) -> None:
        m = make_media()
        await create_media_item(db, m)
        pf = make_processed_file(m.id)
        await create_processed_file(db, pf)
        await db.commit()

        await delete_media_item(db, m.id)

        files = await list_processed_files(db, m.id)
        assert files == []

    async def test_delete_cascades_to_profile_media(
        self, db: aiosqlite.Connection
    ) -> None:
        p = make_profile()
        await create_profile(db, p)
        m = make_media()
        await create_media_item(db, m)
        await assign_media_to_profile(db, p.id, m.id, datetime.now())
        await db.commit()

        await delete_media_item(db, m.id)

        assigned = await list_media_for_profile(db, p.id)
        assert assigned == []


# ---------------------------------------------------------------------------
# ProcessedFiles
# ---------------------------------------------------------------------------


class TestProcessedFiles:
    async def test_create_and_list(self, db: aiosqlite.Connection) -> None:
        m = make_media()
        await create_media_item(db, m)

        pf1 = make_processed_file(m.id, "audio")
        pf2 = make_processed_file(m.id, "thumbnail_medium")
        await create_processed_file(db, pf1)
        await create_processed_file(db, pf2)
        await db.commit()

        files = await list_processed_files(db, m.id)
        assert len(files) == 2
        types = {f.file_type for f in files}
        assert types == {"audio", "thumbnail_medium"}

    async def test_list_empty_for_unknown_media(self, db: aiosqlite.Connection) -> None:
        files = await list_processed_files(db, uuid.uuid4())
        assert files == []

    async def test_get_by_hash(self, db: aiosqlite.Connection) -> None:
        m = make_media()
        await create_media_item(db, m)
        pf = make_processed_file(m.id)
        await create_processed_file(db, pf)
        await db.commit()

        fetched = await get_processed_file_by_hash(db, pf.content_hash)
        assert fetched is not None
        assert fetched.id == pf.id
        assert fetched.media_id == m.id

    async def test_get_by_hash_unknown_returns_none(
        self, db: aiosqlite.Connection
    ) -> None:
        assert await get_processed_file_by_hash(db, "no-such-hash") is None

    async def test_fields_roundtrip(self, db: aiosqlite.Connection) -> None:
        m = make_media()
        await create_media_item(db, m)
        h = "deadbeef" * 8
        pf = ProcessedFile(
            media_id=m.id,
            content_hash=h,
            file_type="thumbnail_large",
            relative_path=f"thumbnails/{h[:2]}/{h}_480x480.webp",
            size_bytes=8192,
            mime_type="image/webp",
        )
        await create_processed_file(db, pf)
        await db.commit()

        fetched = await get_processed_file_by_hash(db, h)
        assert fetched is not None
        assert fetched.file_type == "thumbnail_large"
        assert fetched.size_bytes == 8192
        assert fetched.mime_type == "image/webp"


# ---------------------------------------------------------------------------
# Profile-media assignments
# ---------------------------------------------------------------------------


class TestProfileMedia:
    async def test_assign_and_list(self, db: aiosqlite.Connection) -> None:
        p = make_profile()
        await create_profile(db, p)
        m = make_media()
        await create_media_item(db, m)
        await assign_media_to_profile(db, p.id, m.id, datetime.now())
        await db.commit()

        assigned = await list_media_for_profile(db, p.id)
        assert len(assigned) == 1
        assert assigned[0].id == m.id

    async def test_assign_returns_assignment(self, db: aiosqlite.Connection) -> None:
        p = make_profile()
        await create_profile(db, p)
        m = make_media()
        await create_media_item(db, m)
        at = datetime(2025, 1, 1)
        assignment = await assign_media_to_profile(db, p.id, m.id, at)

        assert isinstance(assignment, ProfileMediaAssignment)
        assert assignment.profile_id == p.id
        assert assignment.media_id == m.id

    async def test_assign_idempotent(self, db: aiosqlite.Connection) -> None:
        p = make_profile()
        await create_profile(db, p)
        m = make_media()
        await create_media_item(db, m)
        at = datetime.now()

        await assign_media_to_profile(db, p.id, m.id, at)
        await assign_media_to_profile(db, p.id, m.id, at)  # second call OK
        await db.commit()

        assigned = await list_media_for_profile(db, p.id)
        assert len(assigned) == 1  # not doubled

    async def test_unassign(self, db: aiosqlite.Connection) -> None:
        p = make_profile()
        await create_profile(db, p)
        m = make_media()
        await create_media_item(db, m)
        await assign_media_to_profile(db, p.id, m.id, datetime.now())
        await db.commit()

        await unassign_media_from_profile(db, p.id, m.id)
        await db.commit()

        assigned = await list_media_for_profile(db, p.id)
        assert assigned == []

    async def test_unassign_nonexistent_is_noop(self, db: aiosqlite.Connection) -> None:
        await unassign_media_from_profile(db, uuid.uuid4(), uuid.uuid4())
        await db.commit()  # must not raise

    async def test_list_profiles_for_media(self, db: aiosqlite.Connection) -> None:
        p1 = make_profile("Leo")
        p2 = make_profile("Sofia")
        await create_profile(db, p1)
        await create_profile(db, p2)
        m = make_media()
        await create_media_item(db, m)
        at = datetime.now()
        await assign_media_to_profile(db, p1.id, m.id, at)
        await assign_media_to_profile(db, p2.id, m.id, at)
        await db.commit()

        profiles = await list_profiles_for_media(db, m.id)
        assert len(profiles) == 2
        names = {p.name for p in profiles}
        assert names == {"Leo", "Sofia"}

    async def test_list_profiles_for_media_breaks_name_ties_by_id(
        self, db: aiosqlite.Connection
    ) -> None:
        """Two profiles with one name come back in id order, whatever the
        order they were created or assigned in.

        With today's schema SQLite happens to visit ties in id order already
        (it scans the ``profile_media`` primary key); an index on ``media_id``
        makes it visit them in assignment order, which is what a future schema
        or planner change could do, so the test adds one.
        """
        await db.execute("CREATE INDEX idx_pm_media ON profile_media(media_id)")
        low = Profile(id=uuid.UUID(int=1), name="Leo")
        high = Profile(id=uuid.UUID(int=2), name="Leo")
        other = Profile(id=uuid.UUID(int=3), name="Abe")
        for profile in (high, other, low):
            await create_profile(db, profile)
        m = make_media()
        await create_media_item(db, m)
        at = datetime.now()
        for profile in (high, low, other):
            await assign_media_to_profile(db, profile.id, m.id, at)
        await db.commit()

        profiles = await list_profiles_for_media(db, m.id)
        assert [p.id for p in profiles] == [other.id, low.id, high.id]

    async def test_list_media_for_profile_filtered_by_profile_id(
        self, db: aiosqlite.Connection
    ) -> None:
        """Media assigned to one profile doesn't appear in another's list."""
        p1 = make_profile("Leo")
        p2 = make_profile("Sofia")
        await create_profile(db, p1)
        await create_profile(db, p2)

        m1 = make_media(title="Leo's Song")
        m2 = make_media(title="Sofia's Song")
        await create_media_item(db, m1)
        await create_media_item(db, m2)
        at = datetime.now()
        await assign_media_to_profile(db, p1.id, m1.id, at)
        await assign_media_to_profile(db, p2.id, m2.id, at)
        await db.commit()

        leo_media = await list_media_for_profile(db, p1.id)
        sofia_media = await list_media_for_profile(db, p2.id)

        assert [m.title for m in leo_media] == ["Leo's Song"]
        assert [m.title for m in sofia_media] == ["Sofia's Song"]

    async def test_list_media_items_by_profile_id(
        self, db: aiosqlite.Connection
    ) -> None:
        """list_media_items with profile_id filter returns assigned items only."""
        p = make_profile()
        await create_profile(db, p)

        m_assigned = make_media(title="Assigned")
        m_other = make_media(title="Unassigned")
        await create_media_item(db, m_assigned)
        await create_media_item(db, m_other)
        await assign_media_to_profile(db, p.id, m_assigned.id, datetime.now())
        await db.commit()

        results = await list_media_items(db, profile_id=p.id)
        assert len(results) == 1
        assert results[0].title == "Assigned"


# ---------------------------------------------------------------------------
# Import queue
# ---------------------------------------------------------------------------


def make_queue_item(url: str = "https://example.com/a.mp3") -> QueueItem:
    return QueueItem(
        url=url,
        importer="http",
        media_type=MediaType.MUSIC,
        playlist_title="Mix",
    )


class TestImportQueue:
    async def test_create_and_get(self, db: aiosqlite.Connection) -> None:
        item = make_queue_item()
        await create_queue_item(db, item)
        await db.commit()
        fetched = await get_queue_item(db, item.id)
        assert fetched is not None
        assert fetched.url == item.url
        assert fetched.importer == "http"
        assert fetched.status == QueueStatus.PENDING

    async def test_get_missing_returns_none(self, db: aiosqlite.Connection) -> None:
        assert await get_queue_item(db, uuid.uuid4()) is None

    async def test_list_filters_by_status(self, db: aiosqlite.Connection) -> None:
        a, b = make_queue_item("https://e.com/a"), make_queue_item("https://e.com/b")
        await create_queue_item(db, a)
        await create_queue_item(db, b)
        await update_queue_item(db, b.id, status=QueueStatus.FAILED)
        await db.commit()
        failed = await list_queue_items(db, status=QueueStatus.FAILED)
        assert [i.id for i in failed] == [b.id]
        assert len(await list_queue_items(db)) == 2

    async def test_claim_next_marks_running(self, db: aiosqlite.Connection) -> None:
        item = make_queue_item()
        await create_queue_item(db, item)
        await db.commit()
        claimed = await claim_next_queue_item(db)
        assert claimed is not None
        assert claimed.id == item.id
        assert claimed.status == QueueStatus.RUNNING
        assert claimed.attempt == 1
        assert await claim_next_queue_item(db) is None

    async def test_delete(self, db: aiosqlite.Connection) -> None:
        item = make_queue_item()
        await create_queue_item(db, item)
        await delete_queue_item(db, item.id)
        await db.commit()
        assert await get_queue_item(db, item.id) is None


# ---------------------------------------------------------------------------
# Loudness columns and repointing processed files
# ---------------------------------------------------------------------------


class TestLoudness:
    async def test_new_item_has_no_loudness(self, db: aiosqlite.Connection) -> None:
        item = make_media()
        await create_media_item(db, item)
        fetched = await get_media_item(db, item.id)
        assert fetched is not None
        assert fetched.loudness_target_lufs is None
        assert fetched.loudness_gain_db is None

    async def test_create_roundtrip(self, db: aiosqlite.Connection) -> None:
        item = make_media().model_copy(
            update={
                "loudness_source_lufs": -23.4,
                "loudness_source_true_peak_dbtp": -4.2,
                "loudness_gain_db": 7.4,
                "loudness_target_lufs": -16.0,
                "loudness_target_true_peak_dbtp": -1.5,
            }
        )
        await create_media_item(db, item)
        assert await get_media_item(db, item.id) == item

    async def test_update_media_item_loudness(self, db: aiosqlite.Connection) -> None:
        item = make_media()
        await create_media_item(db, item)
        later = datetime(2030, 1, 2, 3, 4, 5)
        await update_media_item_loudness(
            db,
            item.id,
            source_lufs=-30.0,
            source_true_peak_dbtp=-12.0,
            gain_db=14.0,
            mode="dynamic",
            target_lufs=-16.0,
            target_true_peak_dbtp=-1.5,
            updated_at=later,
        )
        fetched = await get_media_item(db, item.id)
        assert fetched is not None
        assert fetched.loudness_source_lufs == -30.0
        assert fetched.loudness_source_true_peak_dbtp == -12.0
        assert fetched.loudness_gain_db == 14.0
        assert fetched.loudness_mode == "dynamic"
        assert fetched.loudness_target_lufs == -16.0
        assert fetched.loudness_target_true_peak_dbtp == -1.5
        assert fetched.updated_at == later
        assert fetched.title == item.title

    async def test_update_processed_file(self, db: aiosqlite.Connection) -> None:
        item = make_media()
        await create_media_item(db, item)
        old = make_processed_file(item.id)
        other = make_processed_file(item.id, "thumbnail_small")
        await create_processed_file(db, old)
        await create_processed_file(db, other)

        replacement = make_processed_file(item.id).model_copy(
            update={"id": old.id, "size_bytes": 2048}
        )
        await update_processed_file(db, replacement)

        files = {pf.id: pf for pf in await list_processed_files(db, item.id)}
        assert files[old.id] == replacement
        assert files[other.id] == other


class TestPrivateDbFile:
    """The database holds device API keys, so it is owner-only (#24)."""

    def test_creates_missing_file_owner_only(self, tmp_path: Path) -> None:
        path = tmp_path / "kidsplay.db"
        ensure_private_db_file(path)
        assert stat.S_IMODE(path.stat().st_mode) == 0o600

    def test_tightens_existing_file_and_sidecars(self, tmp_path: Path) -> None:
        path = tmp_path / "kidsplay.db"
        path.write_bytes(b"")
        wal = tmp_path / "kidsplay.db-wal"
        wal.write_bytes(b"")
        for p in (path, wal):
            p.chmod(0o644)
        ensure_private_db_file(path)
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert stat.S_IMODE(wal.stat().st_mode) == 0o600

    async def test_sqlite_keeps_the_mode(self, tmp_path: Path) -> None:
        path = tmp_path / "kidsplay.db"
        ensure_private_db_file(path)
        async with aiosqlite.connect(path) as conn:
            await configure_conn(conn)
            await init_db(conn)
        assert stat.S_IMODE(path.stat().st_mode) == 0o600

    async def test_app_startup_makes_database_owner_only(self, tmp_path: Path) -> None:
        db_path = tmp_path / "kidsplay.db"
        db_path.write_bytes(b"")
        db_path.chmod(0o644)
        app = create_app(db_path, tmp_path / "media")
        async with app.router.lifespan_context(app):
            pass
        assert stat.S_IMODE(db_path.stat().st_mode) == 0o600

    async def test_fresh_app_database_is_owner_only(self, tmp_path: Path) -> None:
        db_path = tmp_path / "new" / "kidsplay.db"
        db_path.parent.mkdir()
        app = create_app(db_path, tmp_path / "media")
        async with app.router.lifespan_context(app):
            pass
        assert stat.S_IMODE(db_path.stat().st_mode) == 0o600
