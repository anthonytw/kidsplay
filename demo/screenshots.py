"""Regenerate the README screenshots and GIF in ``docs/images/``.

Seeds a throwaway server with the bundled demo media (the same state as
``just demo``), then captures:

- the **web UI** with headless Chromium via Playwright (fixed 1280x800
  viewport, UTC, en-US, light scheme, animations off);
- the **device UI** by driving the real player under SDL's dummy video
  driver (``demo.device_shots``), one frame per scripted key press;
- ``hero.png``, the two composed for the top of the README.

Everything that could vary between runs is pinned (seeded sample media,
name-ordered views, explicit playback positions, fixed window sizes), and a
file whose decoded pixels are unchanged is not rewritten (``demo.imagefiles``),
so a rerun leaves only real changes in ``docs/images/``.

``--mobile`` instead captures *every* web UI page at a phone (375x812) and a
desktop (1280x800) viewport, in English and Spanish, into a scratch directory
that is not committed (``screenshots-mobile/``), and exits non-zero if any
page scrolls sideways at phone width.

Usage::

    uv run --all-packages python -m demo.screenshots [--out docs/images]
        [--skip-web] [--skip-device]
    uv run --all-packages python -m demo.screenshots --mobile [--out DIR]

The web capture needs Playwright's Chromium once:
``uv run playwright install chromium``.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, TypedDict

import httpx
from PIL import Image, ImageDraw

from demo.imagefiles import save_png_bytes_if_changed, save_png_if_changed
from demo.seed import (
    DEMO_ADMIN_PASSWORD,
    device_config,
    login,
    running_server,
    seed,
    seed_queue,
    stalled_source,
    sync_device,
    write_device_home,
)

if TYPE_CHECKING:
    from playwright.sync_api import Page, ViewportSize

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUT = REPO_ROOT / "docs" / "images"

WEB_VIEWPORT: ViewportSize = {"width": 1280, "height": 800}
MOBILE_VIEWPORT: ViewportSize = {"width": 375, "height": 812}
MOBILE_OUT = REPO_ROOT / "screenshots-mobile"
LANGUAGES: tuple[str, ...] = ("en", "es")


@dataclass(frozen=True)
class WebShot:
    """One web UI page to capture.

    Attributes:
        path: URL path on the server.
        name: Output file name without extension.
        script: JavaScript run before the capture, e.g. to expand sections.
        full_page: Capture the whole scrollable page, not just the viewport.
    """

    path: str
    name: str
    script: str | None = None
    full_page: bool = False


# Open every playlist group so the library shows its rows.
_EXPAND_GROUPS = "document.querySelectorAll('details').forEach(d => d.open = true);"

# web-media is a viewport-height shot, not the full page: it sits next to the
# GIF in a two-column README table, where a 1280x1349 page renders very tall.
WEB_SHOTS: tuple[WebShot, ...] = (
    WebShot("/", "web-dashboard"),
    WebShot("/media", "web-media", script=_EXPAND_GROUPS),
)


class Overflow(TypedDict):
    """How far a page spills past the viewport, and what spills.

    Attributes:
        scroll_width: ``document.documentElement.scrollWidth``.
        inner_width: ``window.innerWidth``.
        offenders: A few elements whose right edge is past the viewport.
    """

    scroll_width: int
    inner_width: int
    offenders: list[str]


# Measure horizontal scroll the way a person meets it: the document is wider
# than the window. The offenders (elements whose box ends past the window,
# outside a scroll container that clips them) make a failure easy to fix.
MEASURE_OVERFLOW_JS = """
() => {
  const w = window.innerWidth;
  const offenders = [];
  for (const el of document.body.querySelectorAll('*')) {
    const r = el.getBoundingClientRect();
    if (r.width === 0 || r.right <= w + 0.5) continue;
    let clipped = false;
    for (let p = el.parentElement; p && p !== document.body; p = p.parentElement) {
      const o = getComputedStyle(p).overflowX;
      if (o === 'auto' || o === 'scroll' || o === 'hidden') { clipped = true; break; }
    }
    if (getComputedStyle(el).position === 'fixed' || clipped) continue;
    const id = el.id ? '#' + el.id : '';
    const cls = el.classList.length ? '.' + [...el.classList].join('.') : '';
    const at = ' right=' + Math.round(r.right);
    offenders.push(el.tagName.toLowerCase() + id + cls + at);
    if (offenders.length >= 5) break;
  }
  return {
    scroll_width: document.documentElement.scrollWidth,
    inner_width: w,
    offenders,
  };
}
"""

# The import page's tabs show one pane at a time; open each for its capture.
_IMPORT_TAB = "showTab('import-tabs', '{tab}');"
_OPEN_MENU = "document.getElementById('nav-toggle').click();"


# A canned answer for the import preview, so the preview and the playlist track
# list can be captured (and tested) without yt-dlp reaching the internet. It
# replaces fetch() for that one endpoint on the loaded page, types a URL and
# presses Preview. Long titles are there to try the layout.
PREVIEW_PLAYLIST_JS = """
(() => {
  const answer = {
    is_playlist: true,
    playlist_title: 'Songs for a Long Car Ride Across the Whole Country',
    total_available: 24,
    truncated: true,
    tracks: [
      {url: 'https://example.com/v/1', artist: 'Sing Along Friends',
       title: 'The Wheels on the Bus (Extended Sing-Along Version)',
       duration_seconds: 213},
      {url: 'https://example.com/v/2', title: 'Old MacDonald', artist: 'Farm Tunes',
       duration_seconds: 158},
      {url: 'https://example.com/v/3', title: 'Twinkle, Twinkle, Little Star',
       artist: 'Nursery Tunes', duration_seconds: 96},
    ],
    debug: {command: ['yt-dlp', '-J'], returncode: 0, stderr: ''},
  };
  const original = window.fetch;
  window.fetch = (input, init) => String(input instanceof Request ? input.url : input)
      .includes('/api/v1/media/preview')
    ? Promise.resolve(new Response(JSON.stringify(answer),
        {status: 200, headers: {'Content-Type': 'application/json'}}))
    : original(input, init);
  showTab('import-tabs', 'tab-youtube');
  document.getElementById('yt-url').value = 'https://www.youtube.com/playlist?list=PLdemo';
  ytPreview();
})();
"""

# Select the first two library rows, which raises the bulk-action bar.
_SELECT_TWO_ROWS = """
document.querySelectorAll('.row-checkbox').forEach((box, i) => {
  if (i < 2) { box.checked = true; toggleRowSelect(box.value, true); }
});
"""

# Open the edit modal of the first library row.
_OPEN_EDIT = "openEdit(document.querySelector('tr.media-row').id.replace('row-', ''));"


def all_web_pages(profile_id: str) -> tuple[WebShot, ...]:
    """Every page of the admin web UI (plus the states worth a look).

    Args:
        profile_id: A profile UUID, for its settings page.

    Returns:
        The pages to capture, all behind the admin login.
    """
    return (
        WebShot("/", "dashboard"),
        WebShot("/", "menu-open", script=_OPEN_MENU),
        WebShot("/media", "media", script=_EXPAND_GROUPS, full_page=True),
        WebShot("/media?group_by=none", "media-flat", full_page=True),
        WebShot("/media?group_by=artist", "media-by-artist", script=_EXPAND_GROUPS),
        WebShot("/media/import", "import-quick"),
        WebShot(
            "/media/import",
            "import-photo",
            script=_IMPORT_TAB.format(tab="tab-photo")
            + "showTab('photo-src-tabs', 'photo-from-file');",
        ),
        WebShot(
            "/media/import",
            "import-bulk",
            script=_IMPORT_TAB.format(tab="tab-bulk"),
        ),
        WebShot(
            "/media/import",
            "import-archive",
            script=_IMPORT_TAB.format(tab="tab-archive"),
            full_page=True,
        ),
        WebShot(
            "/media/import",
            "import-preview",
            script=PREVIEW_PLAYLIST_JS,
            full_page=True,
        ),
        WebShot("/media?group_by=none", "media-edit", script=_OPEN_EDIT),
        WebShot("/media?group_by=none", "media-bulk-bar", script=_SELECT_TWO_ROWS),
        WebShot("/queue", "queue"),
        WebShot("/profiles", "profiles"),
        WebShot(f"/profiles/{profile_id}/settings", "profile-settings", full_page=True),
        WebShot("/devices", "devices"),
        WebShot("/logs", "logs"),
        WebShot("/tokens", "tokens"),
        WebShot("/account", "account"),
        WebShot("/settings", "settings", full_page=True),
    )


def measure_overflow(page: Page) -> Overflow:
    """Measure how far the current page scrolls sideways.

    Args:
        page: A loaded page.

    Returns:
        The document and window widths and the elements that spill.
    """
    result: Overflow = page.evaluate(MEASURE_OVERFLOW_JS)
    return result


# Resolves once web fonts have loaded and the browser has laid out and painted
# the page twice over, which is all a fixed pause after ``load`` was guessing at.
PAINTED_JS = """
async () => {
  await document.fonts.ready;
  await new Promise(done => requestAnimationFrame(() => requestAnimationFrame(done)));
}
"""


def open_page(page: Page, path: str) -> None:
    """Go to a page and wait until it has drawn.

    Not ``networkidle``: the log page holds a server-sent-events stream open.

    Args:
        page: The browser page.
        path: URL path (with query) on the server.
    """
    page.goto(path, wait_until="load")
    page.evaluate(PAINTED_JS)


def _capture(
    page: Page,
    shot: WebShot,
    out_dir: Path,
    width: int,
    lang: str,
    phone: bool,
    problems: list[str],
) -> None:
    """Run a shot's script, save the full page and record any sideways scroll."""
    if shot.script:
        page.evaluate(shot.script)
        page.wait_for_timeout(150)
    name = f"{shot.name}-{width}-{lang}"
    page.screenshot(
        full_page=True,
        path=out_dir / f"{name}.png",
        animations="disabled",
        caret="hide",
    )
    spill = measure_overflow(page)
    if phone and spill["scroll_width"] > spill["inner_width"]:
        problems.append(f"{name}: {spill}")
    print(f"  {name}.png")


def capture_mobile(base_url: str, out_dir: Path, profile_id: str) -> list[str]:
    """Screenshot every page at phone and desktop width, in both languages.

    Files are named ``<page>-<width>-<lang>.png``. Every page is captured
    full-height, so a screenshot shows everything the page has.

    Args:
        base_url: Server root URL.
        out_dir: Destination directory (created).
        profile_id: A profile UUID, for its settings page.

    Returns:
        One line per page that scrolls sideways at phone width (empty when
        the pass is clean).
    """
    from playwright.sync_api import sync_playwright

    out_dir.mkdir(parents=True, exist_ok=True)
    problems: list[str] = []
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        try:
            for lang in LANGUAGES:
                for viewport in (MOBILE_VIEWPORT, WEB_VIEWPORT):
                    width = viewport["width"]
                    phone = width == MOBILE_VIEWPORT["width"]
                    context = browser.new_context(
                        viewport=viewport,
                        device_scale_factor=1,
                        locale=lang,
                        timezone_id="UTC",
                        color_scheme="light",
                        reduced_motion="reduce",
                        base_url=base_url,
                        is_mobile=phone,
                        has_touch=phone,
                    )
                    page = context.new_page()
                    # The login page is public, so capture it before signing in.
                    open_page(page, "/login")
                    login_shot = WebShot("/login", "login")
                    _capture(page, login_shot, out_dir, width, lang, phone, problems)
                    page.fill("#password", DEMO_ADMIN_PASSWORD)
                    with page.expect_navigation():
                        page.click("button[type=submit]")
                    for shot in all_web_pages(profile_id):
                        if shot.name == "menu-open" and not phone:
                            continue  # the menu button only exists on phones
                        open_page(page, shot.path)
                        _capture(page, shot, out_dir, width, lang, phone, problems)
                    context.close()
        finally:
            browser.close()
    return problems


def capture_web(base_url: str, out_dir: Path, shots: tuple[WebShot, ...]) -> None:
    """Screenshot web UI pages with headless Chromium.

    Args:
        base_url: Server root URL.
        out_dir: Destination directory.
        shots: Pages to capture.
    """
    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        try:
            context = browser.new_context(
                viewport=WEB_VIEWPORT,
                device_scale_factor=1,
                locale="en-US",
                timezone_id="UTC",
                color_scheme="light",
                reduced_motion="reduce",
                base_url=base_url,
            )
            # Log in once; every page below reuses the context's session cookie.
            page = context.new_page()
            page.goto("/login", wait_until="networkidle")
            page.fill("#password", DEMO_ADMIN_PASSWORD)
            with page.expect_navigation():
                page.click("button[type=submit]")
            for shot in shots:
                page.goto(shot.path, wait_until="networkidle")
                if shot.script:
                    page.evaluate(shot.script)
                    page.wait_for_load_state("networkidle")
                png = page.screenshot(
                    full_page=shot.full_page,
                    animations="disabled",
                    caret="hide",
                )
                changed = save_png_bytes_if_changed(png, out_dir / f"{shot.name}.png")
                print(f"  {shot.name}.png{'' if changed else ' (unchanged)'}")
        finally:
            browser.close()


def capture_device(device_home: Path, out_dir: Path) -> None:
    """Capture the device screens and GIF in a child process.

    The child gets ``HOME=device_home`` so the player finds the demo config
    and keeps its settings file there, plus SDL's dummy drivers.

    Args:
        device_home: The demo device's stand-in home directory.
        out_dir: Destination directory.

    Raises:
        subprocess.CalledProcessError: If the capture fails.
    """
    env = dict(
        os.environ,
        HOME=str(device_home),
        SDL_VIDEODRIVER="dummy",
        SDL_AUDIODRIVER="dummy",
    )
    subprocess.run(
        [sys.executable, "-m", "demo.device_shots", str(out_dir)],
        env=env,
        cwd=REPO_ROOT,
        check=True,
    )


def compose_hero(out_dir: Path) -> Path:
    """Compose ``hero.png``: two device screens in a handheld-style bezel.

    Args:
        out_dir: Directory holding ``device-tracks.png`` and ``device-play.png``.

    Returns:
        Path of the written hero image.
    """
    screens = [
        Image.open(out_dir / f"{n}.png") for n in ("device-tracks", "device-play")
    ]
    sw, sh = screens[0].size
    bezel, gap, margin = 28, 48, 32
    w = margin * 2 + len(screens) * (sw + bezel * 2) + gap * (len(screens) - 1)
    h = margin * 2 + sh + bezel * 2
    hero = Image.new("RGB", (w, h), (244, 241, 236))
    draw = ImageDraw.Draw(hero)
    x = margin
    for screen in screens:
        draw.rounded_rectangle(
            [x, margin, x + sw + bezel * 2, margin + sh + bezel * 2],
            radius=26,
            fill=(38, 40, 48),
        )
        hero.paste(screen, (x + bezel, margin + bezel))
        x += sw + bezel * 2 + gap
    path = out_dir / "hero.png"
    changed = save_png_if_changed(hero, path)
    print(f"  hero.png{'' if changed else ' (unchanged)'}")
    return path


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse command-line options.

    Args:
        argv: Arguments without the program name; ``sys.argv[1:]`` if None.

    Returns:
        The parsed options.
    """
    parser = argparse.ArgumentParser(
        prog="just screenshots", description=__doc__.split("\n")[0]
    )
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--skip-web", action="store_true")
    parser.add_argument("--skip-device", action="store_true")
    parser.add_argument(
        "--mobile",
        action="store_true",
        help="capture every web page at 375x812 and 1280x800 (en and es) "
        f"into {MOBILE_OUT.name}/ instead of the README images",
    )
    args = parser.parse_args(argv)
    if args.mobile and args.out == DEFAULT_OUT:
        args.out = MOBILE_OUT
    return args


def run_mobile(out_dir: Path) -> int:
    """Seed a throwaway server and run the phone/desktop capture pass.

    Args:
        out_dir: Destination directory.

    Returns:
        Process exit code: 0 when no page scrolls sideways at phone width.
    """
    with (
        tempfile.TemporaryDirectory(prefix="kidsplay-shots-") as tmp,
        running_server(Path(tmp)) as base_url,
        stalled_source() as stalled_url,
    ):
        with httpx.Client(base_url=base_url, timeout=300.0) as client:
            login(client)
            device = seed(client)
            seed_queue(client, stalled_url)
        print("Web UI (phone and desktop):")
        problems = capture_mobile(
            base_url, out_dir, next(iter(device.profile_ids.values()))
        )
    for line in problems:
        print(f"SIDEWAYS SCROLL at 375px: {line}", file=sys.stderr)
    print(f"Wrote {out_dir}")
    return 1 if problems else 0


def main(argv: list[str] | None = None) -> None:
    """Seed a throwaway server and write every screenshot.

    Args:
        argv: Arguments without the program name; ``sys.argv[1:]`` if None.
    """
    args = parse_args(argv)
    out_dir: Path = args.out.resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    if args.mobile:
        sys.exit(run_mobile(out_dir))
    with tempfile.TemporaryDirectory(prefix="kidsplay-shots-") as tmp:
        data_dir = Path(tmp)
        device_home = data_dir / "device-home"
        with running_server(data_dir) as base_url:
            with httpx.Client(base_url=base_url, timeout=300.0) as client:
                login(client)
                device = seed(client)
            config = device_config(base_url, device, device_home)
            write_device_home(config, device_home)
            sync_device(config)
            if not args.skip_web:
                print("Web UI:")
                capture_web(base_url, out_dir, WEB_SHOTS)
            # The server stays up so the player's startup sync finds nothing
            # new (304) instead of logging a connection error.
            if not args.skip_device:
                print("Device UI:")
                capture_device(device_home, out_dir)
                compose_hero(out_dir)
    print(f"Wrote {out_dir}")


if __name__ == "__main__":
    main()
