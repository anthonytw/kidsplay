"""The "Button sounds" control on the profile settings page (headless Chromium).

Drives the real page at phone and desktop width in both languages: the control
is there with its label and help, fits the screen, and a change saved from the
page reaches the API (and so the device's manifest) and shows again on reload.
Set ``KIDSPLAY_TEST_SHOTS`` to a directory to keep the screenshots.
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
LABEL = {"en": "Button sounds", "es": "Sonidos de botones"}
OFF_TEXT = {"en": "Off", "es": "Desactivado"}
MIN_TAP = 44


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
    with running_server(tmp_path_factory.mktemp("profile-settings")) as url:
        with httpx.Client(base_url=url, timeout=300.0) as client:
            seed.login(client)
            seed.seed(client)
        yield url


def shot(page: Page, name: str) -> None:
    out = os.environ.get("KIDSPLAY_TEST_SHOTS")
    if out:
        Path(out).mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(Path(out) / f"{name}.png"), full_page=True)


def open_profile_settings(
    browser: Browser, base_url: str, lang: str, viewport: str
) -> tuple[BrowserContext, Page, str]:
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
    profiles = page.request.get("/api/v1/profiles").json()
    profile_id: str = profiles[0]["id"]
    screenshots.open_page(page, f"/profiles/{profile_id}/settings")
    return context, page, profile_id


@pytest.mark.parametrize("viewport", VIEWPORTS)
@pytest.mark.parametrize("lang", screenshots.LANGUAGES)
def test_button_sounds_control_saves(
    browser: Browser, base_url: str, lang: str, viewport: str
) -> None:
    context, page, profile_id = open_profile_settings(browser, base_url, lang, viewport)
    url = f"/api/v1/profiles/{profile_id}/settings"
    name = f"profile-settings-button-sounds-{lang}-{viewport}"
    try:
        label = page.locator("label[for=ui-sounds]")
        assert label.inner_text() == LABEL[lang]
        assert page.locator("#ui-sounds").is_visible()
        assert page.input_value("#ui-sounds") == "on"  # the default
        spill = screenshots.measure_overflow(page)
        assert spill["scroll_width"] <= spill["inner_width"], spill
        if viewport == "phone":
            box = page.locator("#ui-sounds").bounding_box()
            assert box is not None
            assert box["height"] >= MIN_TAP, box
        page.locator("#ui-sounds").scroll_into_view_if_needed()
        shot(page, f"{name}-1-on")

        page.select_option("#ui-sounds", "off")
        page.click("button.btn-primary")
        page.wait_for_selector("#settings-result.alert-success")
        assert page.request.get(url).json()["ui_sounds"] is False
        shot(page, f"{name}-2-saved-off")

        # It is still off on reload, and shown as such.
        screenshots.open_page(page, f"/profiles/{profile_id}/settings")
        assert page.input_value("#ui-sounds") == "off"
        assert page.locator("#ui-sounds option:checked").inner_text() == OFF_TEXT[lang]

        # Turning it back on saves too.
        page.select_option("#ui-sounds", "on")
        page.click("button.btn-primary")
        page.wait_for_selector("#settings-result.alert-success")
        assert page.request.get(url).json()["ui_sounds"] is True
    finally:
        page.request.put(url, data={"ui_sounds": True})
        context.close()
