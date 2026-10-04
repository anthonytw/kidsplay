"""The loudness targets on the settings page (headless Chromium, real server).

Drives the real page at phone and desktop width in both languages: the three
targets are there and fit the screen, saving a changed target offers to
normalize the library, "Not now" keeps the value without queuing anything,
"Normalize library now" queues the backfill, and a target pinned by its
environment variable shows locked. Set ``KIDSPLAY_TEST_SHOTS`` to a directory
to keep the screenshots.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING

import httpx
import pytest
from playwright.sync_api import Browser, BrowserContext, Page, sync_playwright

from demo import screenshots, seed
from demo.seed import DEMO_ADMIN_PASSWORD, running_server

if TYPE_CHECKING:
    from collections.abc import Iterator

VIEWPORTS = {
    "phone": screenshots.MOBILE_VIEWPORT,
    "desktop": {"width": 1280, "height": 800},
}
OFFER_TEXT = {"en": "The loudness target changed", "es": "El objetivo de sonoridad"}
LOCKED_TEXT = {"en": "set by KIDSPLAY_", "es": "definido por KIDSPLAY_"}
MIN_TAP = 44
TOUCHED_SETTINGS = {
    "loudness_target_lufs",
    "loudness_target_lufs_music",
    "webp_quality",
}


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


@pytest.fixture(scope="module")
def base_url(tmp_path_factory: pytest.TempPathFactory) -> Iterator[str]:
    """A seeded server (it has audio to normalize) with auth on."""
    with running_server(tmp_path_factory.mktemp("loudness-settings")) as url:
        with httpx.Client(base_url=url, timeout=300.0) as client:
            seed.login(client)
            seed.seed(client)
        yield url


def shot(page: Page, name: str) -> None:
    out = os.environ.get("KIDSPLAY_TEST_SHOTS")
    if out:
        Path(out).mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(Path(out) / f"{name}.png"), full_page=True)


def open_settings(
    browser: Browser, base_url: str, lang: str, viewport: str
) -> tuple[BrowserContext, Page]:
    phone = viewport == "phone"
    context = browser.new_context(
        viewport=VIEWPORTS[viewport],  # ty: ignore[invalid-argument-type]  # plain dict literal of the right shape
        locale=lang,
        base_url=base_url,
        is_mobile=phone,
        has_touch=phone,
        reduced_motion="reduce",
    )
    page = context.new_page()
    page.goto("/login")
    page.fill("#password", DEMO_ADMIN_PASSWORD)
    with page.expect_navigation():
        page.click("button[type=submit]")
    screenshots.open_page(page, "/settings")
    return context, page


def wait_until_idle(page: Page) -> dict[str, object]:
    """Wait for the import queue to finish the seed's normalization jobs."""
    for _ in range(600):
        status: dict[str, object] = page.request.get("/api/v1/media/normalize").json()
        if not status["running"]:
            return status
        page.wait_for_timeout(200)
    raise AssertionError("the queue never went idle")


def assert_fits(page: Page, what: str) -> None:
    spill = screenshots.measure_overflow(page)
    assert spill["scroll_width"] <= spill["inner_width"], f"{what} scrolls: {spill}"


@pytest.mark.parametrize("viewport", VIEWPORTS)
@pytest.mark.parametrize("lang", screenshots.LANGUAGES)
def test_change_a_target_and_normalize(
    browser: Browser, base_url: str, lang: str, viewport: str
) -> None:
    context, page = open_settings(browser, base_url, lang, viewport)
    try:
        _change_a_target_and_normalize(page, lang, viewport)
    finally:
        # Whatever happened, leave the server as the next case expects it.
        page.request.put(
            "/api/v1/server-settings",
            data={"reset": sorted(TOUCHED_SETTINGS)},
        )
        context.close()


def _change_a_target_and_normalize(page: Page, lang: str, viewport: str) -> None:
    name = f"loudness-settings-{lang}-{viewport}"
    for key in ("", "_music", "_audiobook"):
        assert page.locator(f"#setting-loudness_target_lufs{key}").is_visible()
    assert page.input_value("#setting-loudness_target_lufs") == "-16"
    assert page.input_value("#setting-loudness_target_lufs_music") == ""
    assert not page.locator("#normalize-offer").is_visible()
    assert_fits(page, "settings page")
    shot(page, f"{name}-1-page")

    # A changed target is saved, and the page offers to normalize.
    page.fill("#setting-loudness_target_lufs_music", "-14")
    page.click("#save-settings")
    page.wait_for_selector("#normalize-offer", state="visible")
    assert OFFER_TEXT[lang] in page.inner_text("#normalize-offer")
    # What was saved is the new baseline, so saving it again offers nothing new.
    assert (
        page.get_attribute("#setting-loudness_target_lufs_music", "data-initial")
        == "-14"
    )
    assert_fits(page, "normalize offer")
    if viewport == "phone":
        for button in ("#normalize-offer-yes", "#normalize-offer-no"):
            box = page.locator(button).bounding_box()
            assert box is not None
            assert box["height"] >= MIN_TAP, (button, box)
    shot(page, f"{name}-2-offer")

    # "Not now" keeps the target and queues nothing.
    before = wait_until_idle(page)
    with page.expect_navigation():
        page.click("#normalize-offer-no")
    assert page.input_value("#setting-loudness_target_lufs_music") == "-14"
    assert page.request.get("/api/v1/media/normalize").json() == before
    assert not page.locator("#normalize-offer").is_visible()

    # An unrelated setting does not offer a backfill.
    page.fill("#setting-webp_quality", "80")
    with page.expect_navigation():
        page.click("#save-settings")
    assert page.input_value("#setting-webp_quality") == "80"
    assert not page.locator("#normalize-offer").is_visible()

    # "Normalize now" queues the backfill and lands on the media page.
    page.fill("#setting-loudness_target_lufs", "-18")
    page.click("#save-settings")
    page.wait_for_selector("#normalize-offer", state="visible")
    with page.expect_navigation(url="**/media"):
        page.click("#normalize-offer-yes")
    status = page.request.get("/api/v1/media/normalize").json()
    assert status["total"] > 0
    assert status["started_at"] != before["started_at"]  # a new run was queued
    shot(page, f"{name}-3-media")

    # The reset buttons put each setting back (a loudness reset offers a backfill).
    screenshots.open_page(page, "/settings")
    for key in ("loudness_target_lufs", "loudness_target_lufs_music", "webp_quality"):
        if key.startswith("loudness_"):
            page.click(f"[onclick=\"resetSetting('{key}')\"]")
            page.wait_for_selector("#normalize-offer", state="visible")
            with page.expect_navigation():
                page.click("#normalize-offer-no")
        else:
            with page.expect_navigation():
                page.click(f"[onclick=\"resetSetting('{key}')\"]")
    assert page.input_value("#setting-loudness_target_lufs") == "-16"


@pytest.mark.parametrize("viewport", VIEWPORTS)
@pytest.mark.parametrize("lang", screenshots.LANGUAGES)
def test_environment_pinned_target_shows_locked(
    browser: Browser,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    lang: str,
    viewport: str,
) -> None:
    original = seed.server_env
    monkeypatch.setattr(
        seed,
        "server_env",
        lambda data_dir: {
            **original(data_dir),
            "KIDSPLAY_LOUDNESS_TARGET_LUFS_AUDIOBOOK": "-13.5",
        },
    )
    with running_server(tmp_path) as url:
        context, page = open_settings(browser, url, lang, viewport)
        field = page.locator("#setting-loudness_target_lufs_audiobook")
        assert field.input_value() == "-13.5"
        assert field.is_disabled()
        assert not page.locator("#setting-loudness_target_lufs").is_disabled()
        assert LOCKED_TEXT[lang] in page.inner_text(".card")
        assert_fits(page, "locked settings page")
        shot(page, f"loudness-settings-locked-{lang}-{viewport}")
        context.close()
