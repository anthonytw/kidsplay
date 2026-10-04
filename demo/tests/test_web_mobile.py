"""Phone-width checks of the web UI in headless Chromium (real server, real login).

Every page must fit a 375 px screen in both languages (no sideways scroll), the
import flow must work end to end at that size (through the real login form,
with touch input), and its controls must be finger-sized. Needs Playwright's
Chromium (``uv run playwright install chromium``); CI installs it, and
elsewhere the tests skip with that hint when it is missing.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING

import httpx
import pytest
from PIL import Image
from playwright.sync_api import (
    Browser,
    BrowserContext,
    Dialog,
    Page,
    sync_playwright,
)

from demo import screenshots, seed
from demo.seed import (
    DEMO_ADMIN_PASSWORD,
    running_server,
    seed_queue,
    stalled_source,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

    from playwright.sync_api import FloatRect, ViewportSize

PHONE: ViewportSize = screenshots.MOBILE_VIEWPORT
MIN_TAP = 44

# Elements that scroll their own content sideways. A stacked-card table must
# fit its container: measure_overflow skips anything inside a clipping
# container, so a table that quietly scrolls in its wrapper would pass it.
INNER_SCROLL_JS = """
() => [...document.body.querySelectorAll('*')]
  .filter(el => {
    const o = getComputedStyle(el).overflowX;
    return (o === 'auto' || o === 'scroll' || o === 'hidden')
      && el.clientWidth > 0 && el.scrollWidth > el.clientWidth + 1;
  })
  .slice(0, 5)
  .map(el => el.tagName.toLowerCase() + (el.id ? '#' + el.id : '')
             + (el.className ? '.' + String(el.className).split(' ').join('.') : '')
             + ' scrollWidth=' + el.scrollWidth + ' clientWidth=' + el.clientWidth)
"""


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
def server(tmp_path_factory: pytest.TempPathFactory) -> Iterator[tuple[str, str]]:
    """A seeded server with auth on: (base URL, a profile id).

    Its import queue holds a failed, a running and a pending item."""
    with (
        # Queued links do not wake the queue worker: poll often, not every 10 s.
        running_server(tmp_path_factory.mktemp("mobile"), queue_poll=0.1) as base_url,
        stalled_source() as stalled_url,
    ):
        with httpx.Client(base_url=base_url, timeout=300.0) as client:
            seed.login(client)
            device = seed.seed(client)
            seed_queue(client, stalled_url)  # a failed, a running, a pending item
        yield base_url, next(iter(device.profile_ids.values()))


def phone_context(
    browser: Browser, base_url: str, lang: str, width: int | None = None
) -> BrowserContext:
    viewport: ViewportSize = PHONE if width is None else {**PHONE, "width": width}
    return browser.new_context(
        viewport=viewport,
        locale=lang,
        base_url=base_url,
        is_mobile=True,
        has_touch=True,
        reduced_motion="reduce",
    )


def log_in(page: Page) -> None:
    """Sign in through the real login form."""
    page.goto("/login")
    page.fill("#password", DEMO_ADMIN_PASSWORD)
    with page.expect_navigation():
        page.click("button[type=submit]")


@pytest.fixture(scope="module", params=screenshots.LANGUAGES)
def phone_page(
    request: pytest.FixtureRequest, browser: Browser, server: tuple[str, str]
) -> Iterator[Page]:
    context = phone_context(browser, server[0], request.param)
    page = context.new_page()
    log_in(page)
    yield page
    context.close()


def test_every_page_fits_a_phone_without_sideways_scroll(
    phone_page: Page, server: tuple[str, str]
) -> None:
    lang = phone_page.evaluate("document.documentElement.lang")
    pages = screenshots.all_web_pages(server[1])
    assert len(pages) >= 18
    for shot in pages:
        screenshots.open_page(phone_page, shot.path)
        if shot.script:
            phone_page.evaluate(shot.script)
        spill = screenshots.measure_overflow(phone_page)
        assert spill["scroll_width"] <= spill["inner_width"], (
            f"{shot.path} ({shot.name}, {lang}) scrolls sideways: {spill}"
        )
        assert phone_page.evaluate(
            "document.documentElement.scrollWidth <= window.innerWidth"
        )
        # The stacked-card layout is in effect: nothing scrolls inside its box.
        inner = phone_page.evaluate(INNER_SCROLL_JS)
        assert not inner, f"{shot.path} ({shot.name}, {lang}) scrolls inside: {inner}"


@pytest.mark.parametrize("lang", screenshots.LANGUAGES)
@pytest.mark.parametrize("width", [601, 768, 900, 901, 1024, 1025, 1100, 1101])
def test_every_page_fits_tablet_widths_without_sideways_scroll(
    browser: Browser, server: tuple[str, str], lang: str, width: int
) -> None:
    """The nav collapses before its link row outgrows the screen."""
    # A desktop-style context: a mobile one stretches the viewport to fit.
    context = browser.new_context(
        viewport={"width": width, "height": 900}, locale=lang, base_url=server[0]
    )
    page = context.new_page()
    log_in(page)
    for shot in screenshots.all_web_pages(server[1]):
        screenshots.open_page(page, shot.path)
        if shot.script:
            page.evaluate(shot.script)
        spill = screenshots.measure_overflow(page)
        assert spill["scroll_width"] <= width, (
            f"{shot.path} ({shot.name}, {lang}, {width}px) scrolls sideways: {spill}"
        )
    context.close()


@pytest.mark.parametrize("lang", screenshots.LANGUAGES)
@pytest.mark.parametrize("width", [1101, 1200, 1280])
def test_full_nav_wraps_instead_of_overflowing_with_wide_fonts(
    browser: Browser, server: tuple[str, str], lang: str, width: int
) -> None:
    """How wide the full nav row is depends on the fonts a machine has.

    CI's Linux fonts are wider than macOS's, so a fixed breakpoint that
    passes on one overflows on the other. Widen the nav text on purpose and
    check the row wraps rather than scrolling the page sideways.
    """
    context = browser.new_context(
        viewport={"width": width, "height": 900}, locale=lang, base_url=server[0]
    )
    page = context.new_page()
    log_in(page)
    screenshots.open_page(page, "/")
    page.add_style_tag(content="nav a, nav button.link { letter-spacing: 0.25em; }")
    spill = screenshots.measure_overflow(page)
    assert spill["scroll_width"] <= width, (
        f"nav with wide fonts ({lang}, {width}px) scrolls sideways: {spill}"
    )
    context.close()


@pytest.mark.parametrize("width", [1024, 1280])
def test_bedtime_table_fits_its_card_on_desktop(
    browser: Browser, server: tuple[str, str], width: int
) -> None:
    context = browser.new_context(
        viewport={"width": width, "height": 800}, base_url=server[0]
    )
    page = context.new_page()
    log_in(page)
    screenshots.open_page(page, f"/profiles/{server[1]}/settings")
    card = page.locator(".card", has=page.locator("#bedtime-mode"))
    card_box = card.bounding_box()
    table_box = card.locator("table").bounding_box()
    assert card_box is not None
    assert table_box is not None
    assert table_box["x"] >= card_box["x"] - 0.5
    assert table_box["x"] + table_box["width"] <= card_box["x"] + card_box["width"]
    # Compact fields, not full width, on a desktop screen.
    time_input = page.locator("input[type=time][id^=bedtime-] >> nth=0").bounding_box()
    assert time_input is not None
    assert time_input["width"] < 200
    context.close()


def test_number_inputs_stay_compact_on_desktop(
    browser: Browser, server: tuple[str, str]
) -> None:
    context = browser.new_context(
        viewport={"width": 1280, "height": 800}, base_url=server[0]
    )
    page = context.new_page()
    log_in(page)
    screenshots.open_page(page, "/settings")
    boxes = page.locator("input[type=number].server-setting").evaluate_all(
        "els => els.map(el => el.getBoundingClientRect().width)"
    )
    assert boxes
    assert all(width < 300 for width in boxes), boxes
    context.close()


@pytest.mark.parametrize("lang", screenshots.LANGUAGES)
def test_escape_closes_the_nav_menu(
    browser: Browser, server: tuple[str, str], lang: str
) -> None:
    context = phone_context(browser, server[0], lang)
    page = context.new_page()
    log_in(page)
    toggle = page.locator("#nav-toggle")
    toggle.tap()
    assert toggle.get_attribute("aria-expanded") == "true"
    page.locator("#nav-links a[href='/queue']").focus()
    page.keyboard.press("Escape")
    assert toggle.get_attribute("aria-expanded") == "false"
    assert not page.locator("#nav-links a[href='/queue']").is_visible()
    assert page.evaluate("document.activeElement.id") == "nav-toggle"
    context.close()


def test_login_language_picker_sits_beside_the_brand_on_desktop(
    browser: Browser, server: tuple[str, str]
) -> None:
    context = browser.new_context(
        viewport={"width": 1280, "height": 800}, base_url=server[0]
    )
    page = context.new_page()
    screenshots.open_page(page, "/login")
    picker = page.locator(".lang-picker").bounding_box()
    assert picker is not None
    assert picker["x"] < 400
    context.close()


@pytest.mark.parametrize("lang", screenshots.LANGUAGES)
def test_public_pages_fit_a_phone(
    browser: Browser, server: tuple[str, str], lang: str
) -> None:
    context = phone_context(browser, server[0], lang)
    page = context.new_page()
    screenshots.open_page(page, "/login")
    spill = screenshots.measure_overflow(page)
    assert spill["scroll_width"] <= spill["inner_width"], spill
    context.close()


def test_first_run_setup_page_fits_a_phone(
    browser: Browser, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = seed.server_env

    def no_password_env(data_dir: Path) -> dict[str, str]:
        env = original(data_dir)
        env.pop("KIDSPLAY_ADMIN_PASSWORD")
        return env

    monkeypatch.setattr(seed, "server_env", no_password_env)
    with running_server(tmp_path) as base_url:
        for lang in screenshots.LANGUAGES:
            context = phone_context(browser, base_url, lang)
            page = context.new_page()
            screenshots.open_page(page, "/setup")
            assert page.locator("form").count() == 1
            spill = screenshots.measure_overflow(page)
            assert spill["scroll_width"] <= spill["inner_width"], (lang, spill)
            context.close()


@pytest.mark.parametrize("lang", screenshots.LANGUAGES)
def test_nav_collapses_into_a_menu_button(
    browser: Browser, server: tuple[str, str], lang: str
) -> None:
    context = phone_context(browser, server[0], lang)
    page = context.new_page()
    log_in(page)
    links = page.locator("#nav-links a[href='/queue']")
    assert not links.is_visible()
    toggle = page.locator("#nav-toggle")
    assert toggle.get_attribute("aria-expanded") == "false"
    toggle.tap()
    assert toggle.get_attribute("aria-expanded") == "true"
    assert links.is_visible()
    box = links.bounding_box()
    assert box is not None
    assert box["height"] >= MIN_TAP
    context.close()


def test_tables_become_labelled_cards_on_a_phone(
    phone_page: Page, server: tuple[str, str]
) -> None:
    screenshots.open_page(phone_page, "/media?group_by=none")
    row = phone_page.locator("tr.media-row").first
    assert row.evaluate("el => getComputedStyle(el).display") == "grid"
    # The header row is hidden; each value carries its column heading instead.
    headings = phone_page.locator("table.stack thead th:not(.col-select)")
    assert headings.count() >= 5
    assert not any(headings.nth(i).is_visible() for i in range(headings.count()))
    labels = row.locator("td[data-label]").evaluate_all(
        "tds => tds.map(td => getComputedStyle(td, '::before').content)"
    )
    assert len(labels) >= 4
    assert all(label not in ("none", "normal", '""') for label in labels)


def make_photo(path: Path, colour: tuple[int, int, int]) -> Path:
    Image.new("RGB", (1200, 900), colour).save(path)
    return path


def small_targets(page: Page, selector: str) -> list[str]:
    """Visible controls matching ``selector`` shorter than a fingertip."""
    return page.evaluate(
        """([sel, min]) => [...document.querySelectorAll(sel)]
          .filter(el => el.offsetParent !== null)
          .map(el => [el, el.getBoundingClientRect()])
          .filter(([el, r]) => r.height > 0 && r.height < min - 0.5)
          .map(([el, r]) => el.tagName + '#' + el.id + '.' + el.className
                            + ' h=' + Math.round(r.height))""",
        [selector, MIN_TAP],
    )


@pytest.mark.parametrize("lang", screenshots.LANGUAGES)
def test_import_flow_on_a_phone(
    browser: Browser, server: tuple[str, str], tmp_path: Path, lang: str
) -> None:
    """Log in, open Import from the menu, upload two photos, crop and import."""
    base_url, profile_id = server
    context = phone_context(browser, base_url, lang)
    page = context.new_page()
    log_in(page)

    # Reach the import page the way a phone user does: menu, Media, Import.
    page.locator("#nav-toggle").tap()
    page.locator("#nav-links a[href='/media']").tap()
    page.wait_for_url("**/media")
    page.locator("a[href='/media/import']").first.tap()
    page.wait_for_url("**/media/import")
    assert not small_targets(page, ".btn, .tab-btn, input[type=url], select")

    page.locator("[data-tab='tab-photo']").tap()
    page.locator("[data-tab='photo-from-file']").tap()
    file_input = page.locator("#photo-file-input")
    # Camera-roll friendly: images only, several at once.
    assert file_input.get_attribute("accept") == "image/*"
    assert file_input.get_attribute("multiple") is not None
    tag = f"phone-{lang}"
    # Identical pixels would be a duplicate: make each language's photos unique.
    shade = 40 * screenshots.LANGUAGES.index(lang)
    file_input.set_input_files(
        [
            make_photo(tmp_path / f"{tag}-a.png", (200, 30, 30 + shade)),
            make_photo(tmp_path / f"{tag}-b.png", (30, 30 + shade, 200)),
        ]
    )
    page.wait_for_selector("#cropper-card:not(.hidden) .cropper-container")
    page.wait_for_function("cropper && cropper.ready")
    assert "1" in page.locator("#photo-queue-hint").inner_text()
    assert screenshots.measure_overflow(page)["scroll_width"] <= PHONE["width"]

    # Touch: drag the zoomed-in image; the cropper follows a finger.
    page.evaluate("cropper.zoom(0.6)")
    before = page.evaluate("cropper.getCanvasData().left")
    # The page brings the crop frame into view when it opens (a longer hint
    # can push it below the fold); touch points are viewport coordinates, so
    # the drag must start on screen, whatever the font metrics.
    box = page.locator(".cropper-container").bounding_box()
    assert box is not None
    assert box["y"] >= 0 and box["y"] + box["height"] <= PHONE["height"], box
    cx, cy = box["x"] + box["width"] / 2, box["y"] + box["height"] / 2
    cdp = context.new_cdp_session(page)
    cdp.send(
        "Input.dispatchTouchEvent",
        {"type": "touchStart", "touchPoints": [{"x": cx, "y": cy}]},
    )
    for step in range(1, 6):
        cdp.send(
            "Input.dispatchTouchEvent",
            {"type": "touchMove", "touchPoints": [{"x": cx - 12 * step, "y": cy}]},
        )
    cdp.send("Input.dispatchTouchEvent", {"type": "touchEnd", "touchPoints": []})
    after = page.evaluate("cropper.getCanvasData().left")
    assert after != before
    # Let Chromium finish the synthetic touch sequence before the next tap.
    page.wait_for_timeout(500)

    page.fill("#photo-title", f"{tag} one")
    page.fill("#photo-playlist-title", f"Phone {lang}")
    page.locator(f"input[name=photo_profile_ids][value='{profile_id}']").check()
    assert not small_targets(page, "#cropper-card .btn, #cropper-card input[type=text]")
    with page.expect_response("**/api/v1/media/upload") as first:
        page.locator("#cropper-card .btn-primary").tap()
    assert first.value.ok
    page.wait_for_selector("#photo-result.alert-success")
    # The second picked photo is up next.
    page.wait_for_function(
        "document.getElementById('photo-queue-hint').textContent.includes('2')"
    )
    page.wait_for_function("cropper && cropper.ready")
    page.fill("#photo-title", f"{tag} two")
    with page.expect_response("**/api/v1/media/upload") as second:
        page.locator("#cropper-card .btn-primary").tap()
    assert second.value.ok

    # Both landed in the library, under the playlist typed on the phone.
    screenshots.open_page(page, f"/media?q={tag}&group_by=none")
    page.wait_for_selector("tr.media-row")
    assert page.locator("tr.media-row").count() == 2
    assert screenshots.measure_overflow(page)["scroll_width"] <= PHONE["width"]
    context.close()


def test_devices_page_with_a_pairing_request_fits_a_phone(
    phone_page: Page, server: tuple[str, str]
) -> None:
    """The pairing card, with a request waiting and via the QR link, at 375 px.

    A device names itself, so use a long name to try to break the layout. No
    sideways scroll, nothing scrolling inside its box, finger-sized controls,
    and the waiting request is not offered as a one-click action.
    """
    lang = phone_page.evaluate("document.documentElement.lang")
    code = "PHNE" + ("2345" if lang == "en" else "6789")
    registered = httpx.post(
        f"{server[0]}/api/v1/pairing",
        json={
            "code": code,
            "binding_secret": "s" * 43,
            "device_name": "Leo's Player with a very long name that must wrap",
            "display_width": 640,
            "display_height": 480,
        },
    )
    assert registered.status_code == 201, registered.text
    formatted = f"{code[:4]}-{code[4:]}"

    for path in ("/devices", f"/devices?pair={formatted}"):
        screenshots.open_page(phone_page, path)
        card = phone_page.locator("#pairing-card")
        assert card.is_visible()
        spill = screenshots.measure_overflow(phone_page)
        assert spill["scroll_width"] <= spill["inner_width"], (path, lang, spill)
        assert not phone_page.evaluate(INNER_SCROLL_JS), (path, lang)
        assert not small_targets(
            phone_page,
            "#pairing-card .btn, #pairing-card input, #pairing-card select",
        ), (path, lang)
        # Approve and Decline are full, finger-sized buttons.
        for button in phone_page.locator("#pair-actions .btn").all():
            box = button.bounding_box()
            assert box is not None
            assert box["height"] >= MIN_TAP
            assert box["x"] >= 0 and box["x"] + box["width"] <= PHONE["width"]
        # Waiting requests are only counted; nothing to click to approve one.
        assert phone_page.locator("#pairing-card [data-code]").count() == 0
        details = phone_page.locator("#pair-details")
        if "?pair" in path:
            # The QR link shows what is being approved, and prefills the code.
            assert "very long name" in details.inner_text()
            assert phone_page.input_value("#pair-code") == formatted
        else:
            assert "very long name" not in card.inner_text()
            assert phone_page.input_value("#pair-code") == ""
        shot_dir = os.environ.get("KIDSPLAY_SHOTS_DIR")
        if shot_dir:
            name = "pair-qr" if "?pair" in path else "pair-list"
            card.screenshot(path=str(Path(shot_dir) / f"{name}-{lang}.png"))


# ---------------------------------------------------------------------------
# The states behind a tap: queue items, the import preview, the edit modal,
# the delete confirmation and the bulk-action bar.
# ---------------------------------------------------------------------------


def dismiss_and_record(messages: list[str]) -> Callable[[Dialog], None]:
    """A dialog handler that notes the message and answers Cancel."""

    def handle(dialog: Dialog) -> None:
        messages.append(dialog.message)
        dialog.dismiss()

    return handle


def within_viewport(page: Page, selector: str) -> FloatRect:
    """The box of ``selector``; asserts it lies inside the phone's viewport."""
    box = page.locator(selector).bounding_box()
    assert box is not None, selector
    assert box["x"] >= -0.5, (selector, box)
    assert box["x"] + box["width"] <= PHONE["width"] + 0.5, (selector, box)
    return box


def test_queue_with_items_fits_a_phone(
    phone_page: Page, server: tuple[str, str]
) -> None:
    lang = phone_page.evaluate("document.documentElement.lang")
    screenshots.open_page(phone_page, "/queue")
    phone_page.wait_for_selector(".q-item")
    items = phone_page.locator(".q-item")
    assert items.count() == 3
    statuses = phone_page.locator(".q-status").evaluate_all(
        "els => els.map(el => el.className.split('q-status-')[1])"
    )
    assert sorted(statuses) == ["failed", "pending", "running"]
    # The badges are words in the page's language, not the raw values.
    badges = {b.lower() for b in phone_page.locator(".q-status").all_inner_texts()}
    expected = (
        {"con error", "pendiente", "en ejecución"}
        if lang == "es"
        else {"failed", "pending", "running"}
    )
    assert badges == expected, badges
    spill = screenshots.measure_overflow(phone_page)
    assert spill["scroll_width"] <= spill["inner_width"], (lang, spill)
    assert not phone_page.evaluate(INNER_SCROLL_JS), lang
    # The failed item carries its error and a log, inside the card.
    failed = phone_page.locator(".q-item", has=phone_page.locator(".q-status-failed"))
    assert failed.locator(".q-error").is_visible()
    failed.locator("details.q-log summary").tap()
    assert failed.locator("details.q-log pre").is_visible()
    assert screenshots.measure_overflow(phone_page)["scroll_width"] <= PHONE["width"]
    assert not small_targets(phone_page, ".q-actions .btn, #queue-filter"), lang
    # The running item cannot be deleted; the others can.
    running = phone_page.locator(".q-item", has=phone_page.locator(".q-status-running"))
    assert running.locator(".q-actions .btn").count() == 0
    assert failed.locator(".q-actions .btn").count() == 2


def test_deleting_a_queue_item_asks_first(phone_page: Page) -> None:
    lang = phone_page.evaluate("document.documentElement.lang")
    screenshots.open_page(phone_page, "/queue")
    phone_page.wait_for_selector(".q-item")
    pending = phone_page.locator(".q-item", has=phone_page.locator(".q-status-pending"))
    messages: list[str] = []
    phone_page.once("dialog", dismiss_and_record(messages))
    pending.locator(".btn-danger").tap()
    phone_page.wait_for_timeout(300)
    assert (
        messages == ["¿Eliminar este elemento de la cola?"]
        if lang == "es"
        else ["Delete this queue item?"]
    )
    assert pending.count() == 1  # dismissed: still there


def test_import_preview_and_track_list_fit_a_phone(phone_page: Page) -> None:
    lang = phone_page.evaluate("document.documentElement.lang")
    screenshots.open_page(phone_page, "/media/import")
    phone_page.evaluate(screenshots.PREVIEW_PLAYLIST_JS)
    phone_page.wait_for_selector("#yt-playlist:not(.hidden) #yt-tracks tr")
    assert phone_page.locator("#yt-tracks tr").count() == 3
    spill = screenshots.measure_overflow(phone_page)
    assert spill["scroll_width"] <= spill["inner_width"], (lang, spill)
    assert not phone_page.evaluate(INNER_SCROLL_JS), lang
    # One compact card per track: the column labels are one line tall, not the
    # 7.5rem row-direction basis of the stacked-table layout, and the checkbox
    # column has no label to overlap the title's.
    for row in phone_page.locator("#yt-tracks tr").all():
        box = row.bounding_box()
        assert box is not None
        assert box["height"] < 230, (lang, box)
    assert (
        phone_page.locator("#yt-tracks td").first.evaluate(
            "el => getComputedStyle(el, '::before').content"
        )
        == "none"
    )
    # The long playlist and track titles wrap inside the card, not past it.
    for selector in ("#yt-playlist-name", "#yt-tracks tr >> nth=0"):
        within_viewport(phone_page, selector)
    assert not small_targets(
        phone_page,
        "#yt-playlist .btn, #yt-playlist input[type=text], #yt-playlist select",
    ), lang


def test_edit_modal_fits_a_phone(phone_page: Page) -> None:
    lang = phone_page.evaluate("document.documentElement.lang")
    screenshots.open_page(phone_page, "/media?group_by=none")
    phone_page.evaluate(screenshots._OPEN_EDIT)
    modal = phone_page.locator("#edit-modal")
    assert modal.is_visible()
    box = within_viewport(phone_page, "#edit-modal")
    assert box["y"] >= 0, box
    assert box["y"] + box["height"] <= PHONE["height"], box
    assert not small_targets(
        phone_page, "#edit-modal .btn, #edit-modal input[type=text], #edit-modal select"
    ), lang
    assert phone_page.locator("#modal-profiles input").count() == 2
    assert screenshots.measure_overflow(phone_page)["scroll_width"] <= PHONE["width"]
    phone_page.locator("#edit-modal .btn-secondary").tap()  # Cancel
    assert not modal.is_visible()


def test_deleting_media_asks_first(phone_page: Page) -> None:
    lang = phone_page.evaluate("document.documentElement.lang")
    screenshots.open_page(phone_page, "/media?group_by=none")
    rows = phone_page.locator("tr.media-row")
    before = rows.count()
    title = rows.first.get_attribute("data-title")
    messages: list[str] = []
    phone_page.once("dialog", dismiss_and_record(messages))
    rows.first.locator(".btn-danger").tap()
    phone_page.wait_for_timeout(300)
    assert len(messages) == 1
    assert title is not None
    assert title in messages[0]
    assert messages[0].startswith("¿Eliminar" if lang == "es" else "Delete")
    assert rows.count() == before  # dismissed: nothing was deleted


def test_bulk_action_bar_fits_a_phone(phone_page: Page) -> None:
    lang = phone_page.evaluate("document.documentElement.lang")
    screenshots.open_page(phone_page, "/media?group_by=none")
    assert not phone_page.locator("#bulk-bar").is_visible()
    phone_page.evaluate(screenshots._SELECT_TWO_ROWS)
    assert phone_page.locator("#bulk-bar").is_visible()
    assert "2" in phone_page.locator("#bulk-count").inner_text()
    within_viewport(phone_page, "#bulk-bar")
    assert not small_targets(
        phone_page, "#bulk-bar .btn, #bulk-bar select, #bulk-bar input"
    ), lang
    box = phone_page.locator("#bulk-bar").bounding_box()
    assert box is not None
    # The bar leaves room for the page: at most 60% of the screen.
    assert box["height"] <= PHONE["height"] * 0.6, box
    assert box["y"] + box["height"] <= PHONE["height"] + 0.5
    spill = screenshots.measure_overflow(phone_page)
    assert spill["scroll_width"] <= spill["inner_width"], (lang, spill)
    phone_page.locator("#bulk-bar .btn-secondary").first.tap()  # Clear
    assert not phone_page.locator("#bulk-bar").is_visible()


def test_quick_import_placeholder_is_not_cut_off(phone_page: Page) -> None:
    """The placeholder is the only hint what to paste; it must be readable whole."""
    lang = phone_page.evaluate("document.documentElement.lang")
    screenshots.open_page(phone_page, "/media/import")
    cut = phone_page.evaluate(
        """() => [...document.querySelectorAll('input[placeholder]')]
          .filter(el => el.offsetParent !== null)
          .map(el => {
            const cs = getComputedStyle(el);
            const ctx = document.createElement('canvas').getContext('2d');
            ctx.font = cs.fontStyle + ' ' + cs.fontWeight + ' ' + cs.fontSize + ' '
                       + cs.fontFamily;
            const room = el.clientWidth - parseFloat(cs.paddingLeft)
                         - parseFloat(cs.paddingRight);
            return [el.id, el.placeholder, ctx.measureText(el.placeholder).width, room];
          })
          .filter(([, , need, room]) => need > room)"""
    )
    assert not cut, (lang, cut)


def test_logger_names_are_not_split_across_lines(phone_page: Page) -> None:
    lang = phone_page.evaluate("document.documentElement.lang")
    screenshots.open_page(phone_page, "/logs")
    phone_page.wait_for_selector(".log-row .log-name")
    bad = phone_page.evaluate(
        """() => [...document.querySelectorAll('.log-name')]
          .filter(el => el.getClientRects().length !== 1)
          .slice(0, 3).map(el => el.textContent)"""
    )
    assert not bad, (lang, bad)
    container = phone_page.locator("#log-container").bounding_box()
    assert container is not None
    for name in phone_page.locator(".log-name").all()[:20]:
        box = name.bounding_box()
        assert box is not None
        assert box["x"] + box["width"] <= container["x"] + container["width"] + 0.5


def test_inputs_show_a_focus_ring(phone_page: Page) -> None:
    """Keyboard users see where they are: a real outline, not just a border."""
    screenshots.open_page(phone_page, "/media/import")
    field = phone_page.locator("#smart-input")
    field.focus()
    ring = field.evaluate(
        """el => { const s = getComputedStyle(el);
          return {style: s.outlineStyle, width: parseFloat(s.outlineWidth)}; }"""
    )
    assert ring["style"] != "none"
    assert ring["width"] >= 2


def test_every_media_row_keeps_its_artist_under_the_title(phone_page: Page) -> None:
    lang = phone_page.evaluate("document.documentElement.lang")
    screenshots.open_page(phone_page, "/media?group_by=none")
    rows = phone_page.evaluate(
        """() => [...document.querySelectorAll('tr.media-row')].map(row => {
          const cell = row.querySelector('td:nth-child(3)');
          const title = cell.querySelector('strong').getBoundingClientRect();
          const artist = cell.querySelector('span');
          return [cell.querySelector('strong').textContent,
                  artist ? artist.getBoundingClientRect().top - title.bottom : null];
        })"""
    )
    assert any(gap is not None for _, gap in rows)
    beside = [title for title, gap in rows if gap is not None and gap < -1]
    assert not beside, (lang, beside)


def test_cropper_leaves_margins_to_scroll_the_page(
    browser: Browser, server: tuple[str, str], tmp_path: Path
) -> None:
    """The crop frame takes every touch on it (drag, pinch), so on a phone it is
    inset and the page scrolls from the strips beside it. Pinching itself needs
    a real phone (docs/RELEASE_CHECKLIST.md, section 12)."""
    context = phone_context(browser, server[0], "en")
    page = context.new_page()
    log_in(page)
    screenshots.open_page(page, "/media/import")
    page.locator("[data-tab='tab-photo']").tap()
    page.locator("[data-tab='photo-from-file']").tap()
    page.locator("#photo-file-input").set_input_files(
        make_photo(tmp_path / "margins.png", (10, 200, 90))
    )
    page.wait_for_selector("#cropper-card:not(.hidden) .cropper-container")
    wrap = page.locator("#cropper-wrap").bounding_box()
    assert wrap is not None
    assert wrap["x"] >= 16
    assert wrap["x"] + wrap["width"] <= PHONE["width"] - 16
    assert page.locator(".scroll-hint").is_visible()
    assert (
        page.locator("#cropper-wrap").evaluate("el => getComputedStyle(el).touchAction")
        == "none"
    )
    # A drag that starts in the margin scrolls the page.
    page.evaluate("window.scrollTo(0, 0)")
    cdp = context.new_cdp_session(page)
    assert page.evaluate("document.documentElement.scrollHeight > innerHeight")
    y = min(wrap["y"] + wrap["height"] / 2, PHONE["height"] / 2)
    x = 8.0
    cdp.send(
        "Input.dispatchTouchEvent",
        {"type": "touchStart", "touchPoints": [{"x": x, "y": y + 100}]},
    )
    for step in range(1, 11):
        cdp.send(
            "Input.dispatchTouchEvent",
            {"type": "touchMove", "touchPoints": [{"x": x, "y": y + 100 - 15 * step}]},
        )
    cdp.send("Input.dispatchTouchEvent", {"type": "touchEnd", "touchPoints": []})
    page.wait_for_timeout(300)
    assert page.evaluate("window.scrollY") > 0
    context.close()


# ---------------------------------------------------------------------------
# Failed requests show a message in the page's language.
# ---------------------------------------------------------------------------


def test_api_detail_maps_known_codes_and_keeps_unknown_details(
    phone_page: Page,
) -> None:
    lang = phone_page.evaluate("document.documentElement.lang")
    screenshots.open_page(phone_page, "/media")
    known = phone_page.evaluate(
        "apiDetail({error_code: 'NOT_FOUND', detail: 'Media item not found'}, 'x')"
    )
    assert "not found" not in known.lower() or lang == "en"
    assert known.startswith("No se encontró" if lang == "es" else "That item was not")
    # A code the page has no message for keeps the server's detail, and a
    # response with neither falls back to the status text.
    assert (
        phone_page.evaluate(
            "apiDetail({error_code: 'SOMETHING_NEW', detail: 'Only the server knows'})"
        )
        == "Only the server knows"
    )
    assert phone_page.evaluate("apiDetail({}, 'Bad Gateway')") == "Bad Gateway"
    assert (
        phone_page.evaluate("apiDetail({detail: [{msg: 'a'}, {msg: 'b'}]})") == "a; b"
    )


def test_a_failed_save_shows_the_translated_message_not_the_english_detail(
    phone_page: Page, server: tuple[str, str], tmp_path: Path
) -> None:
    """Edit a photo that was deleted meanwhile: the API answers 404 NOT_FOUND."""
    lang = phone_page.evaluate("document.documentElement.lang")
    shade = 30 if lang == "en" else 90
    photo = make_photo(tmp_path / f"gone-{lang}.png", (shade, 120, 200))
    with httpx.Client(base_url=server[0], timeout=60.0) as api:
        seed.login(api)
        made = api.post(
            "/api/v1/media/ingest",
            json={
                "source_path": str(photo),
                "media_type": "photo",
                "playlist_title": f"Gone {lang}",
            },
        )
        assert made.status_code == 200, made.text
        media_id = made.json()["results"][0]["media_id"]
        screenshots.open_page(phone_page, f"/media?q=gone-{lang}&group_by=none")
        phone_page.evaluate(f"openEdit('{media_id}')")
        assert api.delete(f"/api/v1/media/{media_id}").status_code == 204
    phone_page.locator("#edit-modal .btn-primary").tap()
    error = phone_page.locator("#modal-error")
    error.wait_for(state="visible")
    text = error.inner_text()
    assert "not found" not in text.lower() or lang == "en"
    assert "Media item not found" not in text
    assert (
        "No se encontró ese elemento" if lang == "es" else "That item was not"
    ) in text
