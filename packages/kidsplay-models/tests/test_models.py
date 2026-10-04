"""Tests for kidsplay shared models.

Validates construction, serialization, and field constraints
for all model types.
"""

import uuid
from datetime import datetime

import pytest
from pydantic import ValidationError

from kidsplay_models import (
    Device,
    DeviceCreate,
    IngestBatchResult,
    IngestRequest,
    IngestResult,
    MediaItem,
    MediaType,
    NormalizeRequest,
    NormalizeStatus,
    ProcessedFile,
    ProcessingStatus,
    Profile,
    ProfileCreate,
    ProfileMediaAssignment,
    SyncFileEntry,
    SyncManifest,
    SyncMediaEntry,
    ThumbnailSize,
)


class TestMediaTypes:
    """Tests for MediaType enum."""

    def test_media_type_values(self) -> None:
        assert MediaType.MUSIC == "music"
        assert MediaType.AUDIOBOOK == "audiobook"
        assert MediaType.PHOTO == "photo"

    def test_media_type_from_string(self) -> None:
        assert MediaType("music") == MediaType.MUSIC
        assert MediaType("audiobook") == MediaType.AUDIOBOOK
        assert MediaType("photo") == MediaType.PHOTO

    def test_invalid_media_type(self) -> None:
        with pytest.raises(ValueError):
            MediaType("video")


class TestMediaItem:
    """Tests for the unified MediaItem model."""

    def test_create_music(self) -> None:
        item = MediaItem(
            media_type=MediaType.MUSIC,
            content_hash="abc123",
            playlist_title="Pica-Pica Halloween",
            title="La Bamba",
            artist="Ritchie Valens",
            duration_seconds=124,
        )
        assert item.media_type == MediaType.MUSIC
        assert item.playlist_title == "Pica-Pica Halloween"
        assert item.title == "La Bamba"
        assert item.artist == "Ritchie Valens"
        assert item.duration_seconds == 124
        assert isinstance(item.id, uuid.UUID)

    def test_create_audiobook(self) -> None:
        item = MediaItem(
            media_type=MediaType.AUDIOBOOK,
            content_hash="book123",
            playlist_title="The Gruffalo",
            title="Chapter 3",
            artist="Julia Donaldson",
            duration_seconds=180,
        )
        assert item.media_type == MediaType.AUDIOBOOK
        assert item.playlist_title == "The Gruffalo"
        assert item.artist == "Julia Donaldson"

    def test_create_photo(self) -> None:
        item = MediaItem(
            media_type=MediaType.PHOTO,
            content_hash="photo123",
            playlist_title="Beach Trip 2025",
            title="sunset_01",
        )
        assert item.media_type == MediaType.PHOTO
        assert item.duration_seconds is None
        assert item.artist is None

    def test_defaults(self) -> None:
        item = MediaItem(
            media_type=MediaType.MUSIC,
            content_hash="x",
            playlist_title="P",
            title="T",
        )
        assert item.processing_status == "pending"
        assert item.artist is None
        assert item.duration_seconds is None
        assert isinstance(item.created_at, datetime)

    def test_requires_playlist_title(self) -> None:
        with pytest.raises(ValidationError):
            # Deliberately omits the required argument to exercise validation.
            MediaItem(  # ty: ignore[missing-argument]
                media_type=MediaType.MUSIC,
                content_hash="x",
                title="T",
            )

    def test_serialization_roundtrip(self) -> None:
        item = MediaItem(
            media_type=MediaType.MUSIC,
            content_hash="abc",
            playlist_title="Album",
            title="Song",
            artist="Band",
        )
        data = item.model_dump()
        restored = MediaItem.model_validate(data)
        assert restored.title == item.title
        assert restored.id == item.id
        assert restored.media_type == MediaType.MUSIC

    def test_json_roundtrip(self) -> None:
        item = MediaItem(
            media_type=MediaType.PHOTO,
            content_hash="p1",
            playlist_title="Vacation",
            title="beach",
        )
        json_str = item.model_dump_json()
        restored = MediaItem.model_validate_json(json_str)
        assert restored == item


class TestProfile:
    """Tests for Profile model."""

    def test_create_profile(self) -> None:
        profile = Profile(name="Leo")
        assert profile.name == "Leo"
        assert isinstance(profile.id, uuid.UUID)
        assert isinstance(profile.created_at, datetime)

    def test_profile_create_request(self) -> None:
        req = ProfileCreate(name="Sofia")
        assert req.name == "Sofia"


class TestDevice:
    """Tests for Device model."""

    def test_create_device(self) -> None:
        profile_id = uuid.uuid4()
        device = Device(
            name="Leo's GameBoy",
            profile_id=profile_id,
        )
        assert device.name == "Leo's GameBoy"
        assert device.profile_id == profile_id
        assert device.display_width == 640
        assert device.display_height == 480
        assert device.last_sync_at is None
        assert len(device.api_key) == 32

    def test_device_create_request(self) -> None:
        pid = uuid.uuid4()
        req = DeviceCreate(name="Test Device", profile_id=pid)
        assert req.display_width == 640
        assert req.display_height == 480

    def test_device_custom_resolution(self) -> None:
        req = DeviceCreate(
            name="Big Screen",
            profile_id=uuid.uuid4(),
            display_width=1024,
            display_height=768,
        )
        assert req.display_width == 1024


class TestProfileMediaAssignment:
    """Tests for ProfileMediaAssignment model."""

    def test_create_assignment(self) -> None:
        assignment = ProfileMediaAssignment(
            profile_id=uuid.uuid4(),
            media_id=uuid.uuid4(),
        )
        assert isinstance(assignment.assigned_at, datetime)


class TestProcessingStatus:
    """Tests for ProcessingStatus enum."""

    def test_status_values(self) -> None:
        assert ProcessingStatus.PENDING == "pending"
        assert ProcessingStatus.PROCESSING == "processing"
        assert ProcessingStatus.READY == "ready"
        assert ProcessingStatus.FAILED == "failed"


class TestThumbnailSize:
    """Tests for ThumbnailSize enum."""

    def test_size_values(self) -> None:
        assert ThumbnailSize.SMALL == "60x60"
        assert ThumbnailSize.MEDIUM == "200x200"
        assert ThumbnailSize.LARGE == "480x480"

    def test_dimensions_property(self) -> None:
        assert ThumbnailSize.SMALL.dimensions == (60, 60)
        assert ThumbnailSize.MEDIUM.dimensions == (200, 200)
        assert ThumbnailSize.LARGE.dimensions == (480, 480)

    def test_width_height_properties(self) -> None:
        assert ThumbnailSize.LARGE.width == 480
        assert ThumbnailSize.LARGE.height == 480


class TestProcessedFile:
    """Tests for ProcessedFile model."""

    def test_create_processed_file(self) -> None:
        media_id = uuid.uuid4()
        pf = ProcessedFile(
            media_id=media_id,
            content_hash="sha256abc",
            file_type="thumbnail_medium",
            relative_path="thumbnails/sh/sha256abc_200x200.webp",
            size_bytes=15360,
            mime_type="image/webp",
        )
        assert pf.media_id == media_id
        assert pf.file_type == "thumbnail_medium"
        assert pf.size_bytes == 15360


class TestIngestModels:
    """Tests for ingest request/result models."""

    def test_ingest_request(self) -> None:
        req = IngestRequest(
            source_path="/music/Artist/Album",
            media_type=MediaType.MUSIC,
            playlist_title="Artist - Album",
        )
        assert req.profile_ids == []
        assert req.playlist_title == "Artist - Album"

    def test_ingest_request_with_profiles(self) -> None:
        pid = uuid.uuid4()
        req = IngestRequest(
            source_path="/music/songs",
            media_type=MediaType.MUSIC,
            playlist_title="Party Songs",
            profile_ids=[pid],
        )
        assert len(req.profile_ids) == 1

    def test_ingest_result_success(self) -> None:
        result = IngestResult(
            media_id=uuid.uuid4(),
            source_path="/music/track.mp3",
            media_type=MediaType.MUSIC,
            title="Good Song",
            processing_status=ProcessingStatus.READY,
        )
        assert result.errors == []
        assert not result.skipped

    def test_ingest_result_failure(self) -> None:
        result = IngestResult(
            source_path="/music/bad.mp3",
            media_type=MediaType.MUSIC,
            processing_status=ProcessingStatus.FAILED,
            errors=["Corrupt file header"],
        )
        assert result.media_id is None
        assert len(result.errors) == 1

    def test_ingest_batch_result(self) -> None:
        batch = IngestBatchResult(
            total_files=10,
            successful=8,
            failed=1,
            skipped=1,
        )
        assert batch.total_files == 10
        assert batch.results == []


class TestSyncModels:
    """Tests for sync protocol models."""

    def test_sync_file_entry(self) -> None:
        entry = SyncFileEntry(
            content_hash="abc123",
            relative_path="audio/ab/abc123.mp3",
            size_bytes=5_242_880,
            file_type="audio",
        )
        assert entry.file_type == "audio"

    def test_sync_media_entry_music(self) -> None:
        entry = SyncMediaEntry(
            media_id=uuid.uuid4(),
            media_type=MediaType.MUSIC,
            playlist_title="Best of Band",
            title="Test Song",
            artist="Test Artist",
            duration_seconds=180,
            audio_path="audio/ab/abc123.mp3",
            thumbnail_paths={
                "60x60": "thumbnails/ab/abc123_60x60.webp",
                "200x200": "thumbnails/ab/abc123_200x200.webp",
            },
        )
        assert entry.playlist_title == "Best of Band"
        assert len(entry.thumbnail_paths) == 2

    def test_sync_media_entry_audiobook(self) -> None:
        entry = SyncMediaEntry(
            media_id=uuid.uuid4(),
            media_type=MediaType.AUDIOBOOK,
            playlist_title="The Gruffalo",
            title="Chapter 1",
            artist="Julia Donaldson",
            duration_seconds=300,
            audio_path="audio/de/def456.mp3",
        )
        assert entry.playlist_title == "The Gruffalo"

    def test_sync_manifest(self) -> None:
        device_id = uuid.uuid4()
        profile_id = uuid.uuid4()
        manifest = SyncManifest(
            device_id=device_id,
            profile_id=profile_id,
            manifest_hash="manifest_sha256",
            files=[
                SyncFileEntry(
                    content_hash="f1",
                    relative_path="audio/f1/f1.mp3",
                    size_bytes=1000,
                    file_type="audio",
                ),
                SyncFileEntry(
                    content_hash="f2",
                    relative_path="thumbnails/f2/f2_200x200.webp",
                    size_bytes=500,
                    file_type="thumbnail",
                ),
            ],
            media=[
                SyncMediaEntry(
                    media_id=uuid.uuid4(),
                    media_type=MediaType.MUSIC,
                    playlist_title="Hits",
                    title="Song",
                    audio_path="audio/f1/f1.mp3",
                ),
            ],
            total_size_bytes=1500,
        )
        assert manifest.device_id == device_id
        assert len(manifest.files) == 2
        assert len(manifest.media) == 1
        assert manifest.total_size_bytes == 1500

    def test_manifest_json_roundtrip(self) -> None:
        manifest = SyncManifest(
            device_id=uuid.uuid4(),
            profile_id=uuid.uuid4(),
            manifest_hash="test_hash",
            files=[],
            media=[],
        )
        json_str = manifest.model_dump_json()
        restored = SyncManifest.model_validate_json(json_str)
        assert restored.manifest_hash == manifest.manifest_hash
        assert restored.device_id == manifest.device_id

    def test_empty_manifest(self) -> None:
        """A device with no assigned media gets an empty manifest."""
        manifest = SyncManifest(
            device_id=uuid.uuid4(),
            profile_id=uuid.uuid4(),
            manifest_hash="empty",
        )
        assert manifest.files == []
        assert manifest.media == []
        assert manifest.total_size_bytes == 0


class TestLoudnessModels:
    """Loudness fields on MediaItem and the backfill request/status models."""

    def test_media_item_loudness_defaults(self) -> None:
        item = MediaItem(
            media_type=MediaType.MUSIC,
            content_hash="abc",
            playlist_title="P",
            title="T",
        )
        assert item.loudness_source_lufs is None
        assert item.loudness_source_true_peak_dbtp is None
        assert item.loudness_gain_db is None
        assert item.loudness_target_lufs is None
        assert item.loudness_target_true_peak_dbtp is None
        assert item.loudness_mode is None

    def test_media_item_loudness_roundtrip(self) -> None:
        item = MediaItem(
            media_type=MediaType.AUDIOBOOK,
            content_hash="abc",
            playlist_title="P",
            title="T",
            loudness_source_lufs=-24.1,
            loudness_source_true_peak_dbtp=-6.0,
            loudness_gain_db=8.1,
            loudness_mode="capped",
            loudness_target_lufs=-16.0,
            loudness_target_true_peak_dbtp=-1.5,
        )
        assert MediaItem.model_validate_json(item.model_dump_json()) == item

    def test_normalize_request_all(self) -> None:
        assert NormalizeRequest(all=True).media_ids == []

    def test_normalize_request_ids(self) -> None:
        ids = [uuid.uuid4()]
        assert NormalizeRequest(media_ids=ids).media_ids == ids

    @pytest.mark.parametrize(
        "data",
        [{}, {"all": False}, {"all": True, "media_ids": [str(uuid.uuid4())]}],
    )
    def test_normalize_request_needs_exactly_one(self, data: dict) -> None:
        with pytest.raises(ValidationError):
            NormalizeRequest.model_validate(data)

    def test_normalize_status_defaults(self) -> None:
        status = NormalizeStatus()
        assert not status.running
        assert (status.total, status.normalized, status.skipped) == (0, 0, 0)
        assert (status.unchanged, status.failed, status.errors) == (0, 0, [])
        assert status.started_at is None and status.finished_at is None
        assert status.errors_omitted == 0

    def test_normalize_status_keeps_a_bounded_list_of_errors(self) -> None:
        status = NormalizeStatus()
        for i in range(status.MAX_ERRORS + 7):
            status.add_error(f"item {i}: boom")
        assert len(status.errors) == status.MAX_ERRORS
        assert status.errors[0] == "item 0: boom"  # the first ones are kept
        assert status.errors_omitted == 7

    def test_normalize_status_error_cap_survives_json(self) -> None:
        status = NormalizeStatus()
        for i in range(25):
            status.add_error(str(i))
        again = NormalizeStatus.model_validate_json(status.model_dump_json())
        assert (len(again.errors), again.errors_omitted) == (20, 5)
