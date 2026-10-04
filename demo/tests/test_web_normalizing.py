"""The web UI while an upload is being loudness-normalized (headless Chromium).

Uploads return once the file is stored; the queue worker normalizes it after.
This drives the real thing: a real server process, a real upload of a long
tone through the API, and a browser on the media page. It checks that the
upload returns long before ffmpeg is done, that the row says "Normalizing…"
meanwhile (at desktop and phone width), and that the page updates itself when
the worker finishes, without a manual reload.

Set ``KIDSPLAY_TEST_SHOTS`` to a directory to keep the screenshots.
"""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path
from typing import TYPE_CHECKING

import httpx
import pytest
from playwright.sync_api import Browser, Page, sync_playwright

from demo import screenshots, seed
from demo.seed import DEMO_ADMIN_PASSWORD, running_server

if TYPE_CHECKING:
    from collections.abc import Iterator

# Long enough that normalizing it (15 to 40 x realtime, depending on the core)
# is still running when the browsers have loaded the page, short enough to finish.
TONE_SECONDS = 1500


@pytest.fixture(scope="module")
def browser() -> Iterator[Browser]:
    with sync_playwright() as pw:
        try:
            chromium = pw.chromium.launch()
        except Exception as exc:  # any launch failure means no browser
            if os.environ.get("CI"):
                raise  # CI must have the browser: a skip would hide a broken job
            pytest.skip(
                f"Chromium missing ({exc}); run: uv run playwright install chromium"
            )
        yield chromium
        chromium.close()


def make_tone(path: Path, seconds: int, frequency: int) -> Path:
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-nostdin", "-loglevel", "error", "-y"]
        + ["-f", "lavfi", "-i", f"sine=frequency={frequency}:duration={seconds}"]
        + ["-af", "volume=-15dB", str(path)],
        check=True,
    )
    return path


def shot(page: Page, name: str) -> None:
    out = os.environ.get("KIDSPLAY_TEST_SHOTS")
    if out:
        Path(out).mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(Path(out) / f"{name}.png"), full_page=True)


@pytest.fixture(scope="module")
def base_url(tmp_path_factory: pytest.TempPathFactory) -> Iterator[str]:
    """A server with auth on and an empty library."""
    with running_server(tmp_path_factory.mktemp("normalizing")) as url:
        yield url


def upload(base_url: str, tone: Path) -> float:
    """Upload ``tone`` as an audiobook; return how long the request took."""
    with httpx.Client(base_url=base_url, timeout=300.0) as client:
        seed.login(client)
        started = time.monotonic()
        with tone.open("rb") as handle:
            response = client.post(
                "/api/v1/media/upload",
                files={"file": (tone.name, handle, "audio/mpeg")},
                data={"media_type": "audiobook", "playlist_title": "Stories"},
            )
        took = time.monotonic() - started
    assert response.status_code == 200, response.text
    assert response.json()["successful"] == 1
    return took


def log_in(page: Page) -> None:
    page.goto("/login")
    page.fill("#password", DEMO_ADMIN_PASSWORD)
    with page.expect_navigation():
        page.click("button[type=submit]")


VIEWPORTS = {"desktop": 1100, "phone": 375}


def test_upload_returns_at_once_and_the_rows_update_themselves(
    browser: Browser, base_url: str, tmp_path: Path
) -> None:
    tone = make_tone(tmp_path / "Long story.mp3", TONE_SECONDS, 330)
    took = upload(base_url, tone)
    # Storing the file takes a second or so; normalizing a 25-minute one takes
    # far longer (measure + encode + check), and used to be inside this call.
    assert took < 8, f"the upload took {took:.1f} s"
    with httpx.Client(base_url=base_url) as client:
        seed.login(client)
        assert client.get("/api/v1/media/normalize").json()["running"] is True

    contexts = {
        name: browser.new_context(
            viewport={"width": width, "height": 800},
            locale="en",
            base_url=base_url,
            reduced_motion="reduce",
        )
        for name, width in VIEWPORTS.items()
    }
    pages = {name: context.new_page() for name, context in contexts.items()}
    try:
        for name, page in pages.items():
            log_in(page)
            screenshots.open_page(page, "/media")
            # Groups start collapsed; open them like a parent would.
            page.evaluate(
                "document.querySelectorAll('details.media-group')"
                ".forEach(d => d.open = true)"
            )

            # While the worker is at it.
            badge = page.locator(".media-row .normalizing-tag:visible")
            badge.first.wait_for(state="visible", timeout=10_000)
            assert badge.count() == 1
            assert badge.first.inner_text().strip() == "Normalizing…"
            row = page.locator(".media-row[data-normalizing='1']").first
            assert row.get_attribute("data-loudness") == "Loudness: normalizing…"
            spill = screenshots.measure_overflow(page)
            assert spill["scroll_width"] <= spill["inner_width"], (name, spill)
            # The top bar shows the run's progress.
            assert "Normalizing…" in page.locator("#normalize-status").inner_text()
            shot(page, f"{name}-normalizing")

        # When the worker is done each page reloads itself (its poll sees the
        # queue drain): the badge goes and the details have the numbers.
        for name, page in pages.items():
            page.wait_for_selector(
                ".media-row[data-normalizing='']", state="attached", timeout=240_000
            )
            assert page.locator(".normalizing-tag").count() == 0
            # The group the parent had open is still open after the reload.
            assert page.locator("details.media-group[open]").count() == 1
            loudness = page.locator(".media-row").last.get_attribute("data-loudness")
            assert loudness is not None
            # An audiobook's target is the overall -16 LUFS unless configured.
            assert loudness.startswith("Loudness: -")
            assert "→ -16 LUFS" in loudness
            shot(page, f"{name}-normalized")
    finally:
        for context in contexts.values():
            context.close()
