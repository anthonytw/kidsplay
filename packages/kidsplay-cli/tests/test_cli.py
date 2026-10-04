"""End-to-end CLI tests.

Every test uses Click's CliRunner against real commands hitting a real
FastAPI server (started once per session in conftest.py).  The full
workflow is exercised: create profile → create device → ingest photos →
list → assign → verify → delete.
"""

import io
from pathlib import Path

import pytest
from click.testing import CliRunner, Result
from PIL import Image

from kidsplay_cli.main import cli

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_png(path: Path, seed: int = 0) -> Path:
    """Write a distinct solid-colour PNG to *path*."""
    color = (100 + seed * 30 % 155, 150, max(10, 200 - seed * 20 % 190))
    buf = io.BytesIO()
    Image.new("RGB", (100, 100), color=color).save(buf, format="PNG")
    path.write_bytes(buf.getvalue())
    return path


def run(server_url: str, *args: str) -> Result:
    """Invoke the CLI with --server injected.

    Sets COLUMNS=200 so Rich renders full-width tables without truncation.
    """
    runner = CliRunner()
    return runner.invoke(
        cli,
        ["--server", server_url, *args],
        catch_exceptions=False,
        env={"COLUMNS": "200"},
    )


# ---------------------------------------------------------------------------
# Profile commands
# ---------------------------------------------------------------------------


class TestProfileCommands:
    def test_profile_list_empty(self, server_url: str) -> None:
        result = run(server_url, "profile", "list")
        assert result.exit_code == 0
        # Either "No profiles found" or a table — depends on order of tests.
        # Just assert no crash.

    def test_profile_create(self, server_url: str) -> None:
        result = run(server_url, "profile", "create", "TestKid")
        assert result.exit_code == 0
        assert "TestKid" in result.output

    def test_profile_list_after_create(self, server_url: str) -> None:
        run(server_url, "profile", "create", "ListMe")
        result = run(server_url, "profile", "list")
        assert result.exit_code == 0
        assert "ListMe" in result.output

    def test_profile_delete(self, server_url: str) -> None:
        # Create then delete.
        create = run(server_url, "profile", "create", "ToDelete")
        assert create.exit_code == 0
        # Extract UUID from output line: "Created profile ToDelete (uuid)"
        import re

        m = re.search(r"\(([0-9a-f-]{36})\)", create.output)
        assert m, f"No UUID in: {create.output}"
        profile_id = m.group(1)

        result = run(server_url, "profile", "delete", "--yes", profile_id)
        assert result.exit_code == 0
        assert "Deleted" in result.output

    def test_profile_delete_not_found(self, server_url: str) -> None:
        import uuid

        result = run(server_url, "profile", "delete", "--yes", str(uuid.uuid4()))
        assert result.exit_code == 1
        assert "Error" in result.output


# ---------------------------------------------------------------------------
# Device commands
# ---------------------------------------------------------------------------


class TestDeviceCommands:
    @pytest.fixture(autouse=True)
    def _profile(self, server_url: str) -> None:
        """Create a shared profile for device tests."""
        import re

        result = run(server_url, "profile", "create", "DeviceTestProfile")
        m = re.search(r"\(([0-9a-f-]{36})\)", result.output)
        assert m
        self._profile_id = m.group(1)

    def test_device_list_after_create(self, server_url: str) -> None:
        run(server_url, "device", "create", "MyDevice", "--profile", self._profile_id)
        result = run(server_url, "device", "list")
        assert result.exit_code == 0
        assert "MyDevice" in result.output

    def test_device_create_returns_api_key(self, server_url: str) -> None:
        result = run(
            server_url, "device", "create", "KeyDevice", "--profile", self._profile_id
        )
        assert result.exit_code == 0
        assert "api_key" in result.output

    def test_device_delete(self, server_url: str) -> None:
        import re

        create = run(
            server_url, "device", "create", "DeleteMe", "--profile", self._profile_id
        )
        m = re.search(r"\(([0-9a-f-]{36})\)", create.output)
        assert m
        device_id = m.group(1)

        result = run(server_url, "device", "delete", "--yes", device_id)
        assert result.exit_code == 0
        assert "Deleted" in result.output

    def test_device_create_unknown_profile(self, server_url: str) -> None:
        import uuid

        result = run(
            server_url,
            "device",
            "create",
            "Ghost",
            "--profile",
            str(uuid.uuid4()),
        )
        assert result.exit_code == 1
        assert "Error" in result.output


# ---------------------------------------------------------------------------
# Media commands
# ---------------------------------------------------------------------------


class TestMediaCommands:
    @pytest.fixture(autouse=True)
    def _profile(self, server_url: str) -> None:
        """Create a fresh profile for each media test."""
        import re

        result = run(server_url, "profile", "create", "MediaTestProfile")
        m = re.search(r"\(([0-9a-f-]{36})\)", result.output)
        assert m
        self._profile_id = m.group(1)

    def test_media_list_empty_initially(self, server_url: str) -> None:
        result = run(server_url, "media", "list", "--profile", self._profile_id)
        assert result.exit_code == 0
        assert "No media" in result.output

    def test_media_ingest_single_file(self, server_url: str, tmp_path: Path) -> None:
        src = make_png(tmp_path / "photo.png", seed=1)
        result = run(
            server_url,
            "media",
            "ingest",
            str(src),
            "--type",
            "photo",
            "--playlist",
            "SingleFile",
        )
        assert result.exit_code == 0
        assert "1 ingested" in result.output

    def test_media_ingest_directory(self, server_url: str, tmp_path: Path) -> None:
        d = tmp_path / "photos"
        d.mkdir()
        for i in range(3):
            make_png(d / f"p{i}.png", seed=10 + i)
        result = run(
            server_url,
            "media",
            "ingest",
            str(d),
            "--type",
            "photo",
            "--playlist",
            "DirBatch",
        )
        assert result.exit_code == 0
        assert "3 ingested" in result.output

    def test_media_ingest_invalid_path(self, server_url: str) -> None:
        result = run(
            server_url,
            "media",
            "ingest",
            "/nonexistent/path",
            "--type",
            "photo",
        )
        assert result.exit_code == 1
        assert "Error" in result.output

    def test_media_ingest_duplicate_skipped(
        self, server_url: str, tmp_path: Path
    ) -> None:
        src = make_png(tmp_path / "dup.png", seed=99)
        run(
            server_url,
            "media",
            "ingest",
            str(src),
            "--type",
            "photo",
            "--playlist",
            "Dup",
        )
        result = run(
            server_url,
            "media",
            "ingest",
            str(src),
            "--type",
            "photo",
            "--playlist",
            "Dup",
        )
        assert result.exit_code == 0
        assert "skipped" in result.output

    def test_media_list_shows_ingested(self, server_url: str, tmp_path: Path) -> None:
        d = tmp_path / "list_test"
        d.mkdir()
        make_png(d / "sunset.png", seed=20)
        run(
            server_url,
            "media",
            "ingest",
            str(d),
            "--type",
            "photo",
            "--playlist",
            "Sunsets",
            "--profile",
            self._profile_id,
        )
        result = run(server_url, "media", "list", "--profile", self._profile_id)
        assert result.exit_code == 0
        assert "sunset" in result.output.lower()

    def test_media_show(self, server_url: str, tmp_path: Path) -> None:
        import re

        src = make_png(tmp_path / "show_me.png", seed=30)
        run(
            server_url,
            "media",
            "ingest",
            str(src),
            "--type",
            "photo",
            "--playlist",
            "ShowTest",
        )
        # Get media_id from list
        media_list = run(server_url, "media", "list")
        # find a UUID on a line that contains "show_me"
        lines = media_list.output.lower()
        assert "show_me" in lines or "showtest" in lines

        # Extract ID from ingest result indirectly by listing
        all_media = run(server_url, "media", "list", "--search", "show_me")
        uuid_pat = r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})"
        m = re.search(uuid_pat, all_media.output)
        if m:
            media_id = m.group(1)
            show = run(server_url, "media", "show", media_id)
            assert show.exit_code == 0
            assert "show_me" in show.output.lower()

    def test_media_delete(self, server_url: str, tmp_path: Path) -> None:
        import re

        src = make_png(tmp_path / "del_photo.png", seed=40)
        run(
            server_url,
            "media",
            "ingest",
            str(src),
            "--type",
            "photo",
            "--playlist",
            "ToDelete",
        )
        uuid_pat = r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})"
        items = run(server_url, "media", "list", "--search", "del_photo")
        m = re.search(uuid_pat, items.output)
        assert m, f"No UUID in: {items.output}"
        media_id = m.group(1)

        result = run(server_url, "media", "delete", "--yes", media_id)
        assert result.exit_code == 0
        assert "Deleted" in result.output

        # Verify gone.
        show = run(server_url, "media", "show", media_id)
        assert show.exit_code == 1

    def test_media_assign_and_unassign(self, server_url: str, tmp_path: Path) -> None:
        import re

        src = make_png(tmp_path / "assign_me.png", seed=50)
        run(
            server_url,
            "media",
            "ingest",
            str(src),
            "--type",
            "photo",
            "--playlist",
            "AssignTest",
        )
        uuid_pat = r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})"
        items = run(server_url, "media", "list", "--search", "assign_me")
        m = re.search(uuid_pat, items.output)
        assert m
        media_id = m.group(1)

        # Assign.
        assign = run(server_url, "media", "assign", media_id, self._profile_id)
        assert assign.exit_code == 0
        assert "Assigned" in assign.output

        # Appears in profile list.
        profile_list = run(server_url, "media", "list", "--profile", self._profile_id)
        assert media_id in profile_list.output

        # Unassign.
        unassign = run(server_url, "media", "unassign", media_id, self._profile_id)
        assert unassign.exit_code == 0
        assert "Unassigned" in unassign.output

        # No longer in profile list.
        after = run(server_url, "media", "list", "--profile", self._profile_id)
        assert media_id not in after.output

    def test_media_ingest_with_profile_assignment(
        self, server_url: str, tmp_path: Path
    ) -> None:
        """--profile flag during ingest assigns media immediately."""
        src = make_png(tmp_path / "direct_assign.png", seed=60)
        run(
            server_url,
            "media",
            "ingest",
            str(src),
            "--type",
            "photo",
            "--playlist",
            "DirectAssign",
            "--profile",
            self._profile_id,
        )
        result = run(server_url, "media", "list", "--profile", self._profile_id)
        assert "direct_assign" in result.output.lower()

    def test_full_workflow(self, server_url: str, tmp_path: Path) -> None:
        """Create profile → device → ingest directory → list → assign → verify."""
        import re

        # Fresh profile for this test.
        p = run(server_url, "profile", "create", "WorkflowKid")
        pm = re.search(r"\(([0-9a-f-]{36})\)", p.output)
        assert pm
        pid = pm.group(1)

        # Create device.
        d = run(server_url, "device", "create", "WorkflowDevice", "--profile", pid)
        assert d.exit_code == 0

        # Ingest 3 photos.
        photos = tmp_path / "workflow_photos"
        photos.mkdir()
        for i in range(3):
            make_png(photos / f"wf_{i}.png", seed=70 + i)

        ingest = run(
            server_url,
            "media",
            "ingest",
            str(photos),
            "--type",
            "photo",
            "--playlist",
            "Workflow Album",
        )
        assert ingest.exit_code == 0
        assert "3 ingested" in ingest.output

        # List all media, grab IDs for the workflow photos.
        listing = run(server_url, "media", "list", "--search", "wf_")
        assert listing.exit_code == 0
        uuids = re.findall(
            r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})",
            listing.output,
        )
        assert len(uuids) >= 3

        # Assign all three to the profile.
        for mid in uuids[:3]:
            result = run(server_url, "media", "assign", mid, pid)
            assert result.exit_code == 0

        # Verify they appear in the profile's list.
        profile_media = run(server_url, "media", "list", "--profile", pid)
        assert profile_media.exit_code == 0
        for mid in uuids[:3]:
            assert mid in profile_media.output


# ---------------------------------------------------------------------------
# Importer commands
# ---------------------------------------------------------------------------


class TestImporterCommands:
    def test_importer_list_shows_builtins(self, server_url: str) -> None:
        result = run(server_url, "importer", "list")
        assert result.exit_code == 0
        assert "local" in result.output
        assert "Local file or folder" in result.output
        assert "http" in result.output
