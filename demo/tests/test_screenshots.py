"""Tests for the screenshot orchestrator (``demo/screenshots.py``).

Playwright, the server and the device capture subprocess are mocked.
"""

import contextlib
import io
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import MagicMock

import playwright.sync_api
import pytest
from PIL import Image

from demo import screenshots
from demo.screenshots import WebShot
from demo.seed import DemoDevice


def _png(color: tuple[int, int, int]) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (4, 4), color).save(buf, format="PNG")
    return buf.getvalue()


def test_web_shots_cover_dashboard_and_library() -> None:
    assert [s.path for s in screenshots.WEB_SHOTS] == ["/", "/media"]


def test_readme_media_shot_is_viewport_height_and_does_not_rewrite_the_page() -> None:
    """Profile tags are ordered server-side now, so no script relabels them."""
    media = next(s for s in screenshots.WEB_SHOTS if s.name == "web-media")
    assert media.full_page is False
    assert media.script is not None
    assert "textContent" not in media.script


def test_capture_web_uses_fixed_viewport_and_runs_scripts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pw = MagicMock()
    manager = MagicMock()
    manager.__enter__.return_value = pw
    monkeypatch.setattr(playwright.sync_api, "sync_playwright", lambda: manager)
    browser = pw.chromium.launch.return_value
    context = browser.new_context.return_value
    page = context.new_page.return_value
    page.screenshot.side_effect = [
        _png((255, 0, 0)),
        _png((0, 0, 255)),
    ]

    shots = (WebShot("/", "a"), WebShot("/b", "b", script="x()", full_page=True))
    screenshots.capture_web("http://h:1", tmp_path, shots)

    kwargs = browser.new_context.call_args.kwargs
    assert kwargs["viewport"] == screenshots.WEB_VIEWPORT
    assert kwargs["base_url"] == "http://h:1"
    assert kwargs["timezone_id"] == "UTC"
    # Logs in once, then captures each page with that session.
    assert [c.args[0] for c in page.goto.call_args_list] == ["/login", "/", "/b"]
    page.fill.assert_called_once_with("#password", screenshots.DEMO_ADMIN_PASSWORD)
    page.evaluate.assert_called_once_with("x()")
    assert Image.open(tmp_path / "a.png").getpixel((0, 0)) == (255, 0, 0)
    assert Image.open(tmp_path / "b.png").getpixel((0, 0)) == (0, 0, 255)
    assert page.screenshot.call_args.kwargs["full_page"] is True
    browser.close.assert_called_once()


def test_capture_device_runs_child_with_demo_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run = MagicMock()
    monkeypatch.setattr(screenshots.subprocess, "run", run)
    screenshots.capture_device(tmp_path / "home", tmp_path / "out")

    cmd = run.call_args.args[0]
    env = run.call_args.kwargs["env"]
    assert cmd[1:] == ["-m", "demo.device_shots", str(tmp_path / "out")]
    assert env["HOME"] == str(tmp_path / "home")
    assert env["SDL_VIDEODRIVER"] == "dummy"
    assert run.call_args.kwargs["check"] is True


def test_compose_hero_places_two_screens(tmp_path: Path) -> None:
    for name, colour in (("device-tracks", (255, 0, 0)), ("device-play", (0, 0, 255))):
        Image.new("RGB", (64, 48), colour).save(tmp_path / f"{name}.png")
    hero = Image.open(screenshots.compose_hero(tmp_path)).convert("RGB")

    assert hero.size == (32 * 2 + 2 * (64 + 56) + 48, 32 * 2 + 48 + 56)
    assert hero.getpixel((32 + 28, 32 + 28)) == (255, 0, 0)
    assert hero.getpixel((32 + 120 + 48 + 28, 32 + 28)) == (0, 0, 255)


def test_parse_args_defaults_to_docs_images() -> None:
    args = screenshots.parse_args([])
    assert args.out == screenshots.REPO_ROOT / "docs" / "images"
    assert not args.skip_web
    assert not args.skip_device


def test_main_seeds_then_captures_everything(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    @contextlib.contextmanager
    def fake_server(_: Path) -> Iterator[str]:
        yield "http://127.0.0.1:1"

    calls: list[str] = []
    monkeypatch.setattr(screenshots, "running_server", fake_server)
    monkeypatch.setattr(screenshots, "login", MagicMock())
    monkeypatch.setattr(
        screenshots, "seed", MagicMock(return_value=DemoDevice("d", "k", {}))
    )
    monkeypatch.setattr(screenshots, "sync_device", MagicMock(return_value=1))
    monkeypatch.setattr(screenshots, "capture_web", lambda *a: calls.append("web"))
    monkeypatch.setattr(
        screenshots, "capture_device", lambda *a: calls.append("device")
    )
    monkeypatch.setattr(
        screenshots, "compose_hero", lambda out: calls.append("hero") or out / "h"
    )

    screenshots.main(["--out", str(tmp_path / "imgs")])
    assert calls == ["web", "device", "hero"]
    assert (tmp_path / "imgs").is_dir()

    calls.clear()
    screenshots.main(["--out", str(tmp_path / "imgs"), "--skip-web"])
    assert calls == ["device", "hero"]


def test_all_web_pages_covers_every_admin_page() -> None:
    pages = screenshots.all_web_pages("pid")
    paths = {p.path.split("?")[0] for p in pages}
    assert paths == {
        "/",
        "/media",
        "/media/import",
        "/queue",
        "/profiles",
        "/profiles/pid/settings",
        "/devices",
        "/logs",
        "/tokens",
        "/account",
        "/settings",
    }
    names = [p.name for p in pages]
    assert len(names) == len(set(names))


def test_parse_args_mobile_defaults_to_the_scratch_directory() -> None:
    args = screenshots.parse_args(["--mobile"])
    assert args.mobile
    assert args.out == screenshots.MOBILE_OUT
    assert screenshots.parse_args(["--mobile", "--out", "x"]).out == Path("x")
    assert not screenshots.parse_args([]).mobile


def test_mobile_output_directory_is_git_ignored() -> None:
    ignored = (screenshots.REPO_ROOT / ".gitignore").read_text().splitlines()
    assert f"{screenshots.MOBILE_OUT.name}/" in ignored


def test_measure_overflow_returns_the_page_measurement() -> None:
    page = MagicMock()
    page.evaluate.return_value = {
        "scroll_width": 400,
        "inner_width": 375,
        "offenders": ["td right=400"],
    }
    assert screenshots.measure_overflow(page)["scroll_width"] == 400
    page.evaluate.assert_called_once_with(screenshots.MEASURE_OVERFLOW_JS)


def test_open_page_waits_for_load_not_network_idle() -> None:
    page = MagicMock()
    screenshots.open_page(page, "/logs")
    page.goto.assert_called_once_with("/logs", wait_until="load")


def test_capture_mobile_shoots_both_widths_and_languages(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pw = MagicMock()
    manager = MagicMock()
    manager.__enter__.return_value = pw
    monkeypatch.setattr(playwright.sync_api, "sync_playwright", lambda: manager)
    browser = pw.chromium.launch.return_value
    page = browser.new_context.return_value.new_page.return_value
    fits = {"scroll_width": 375, "inner_width": 375, "offenders": []}
    page.evaluate.return_value = fits

    assert screenshots.capture_mobile("http://h:1", tmp_path, "pid") == []

    widths = [c.kwargs["viewport"]["width"] for c in browser.new_context.call_args_list]
    assert widths == [375, 1280, 375, 1280]
    langs = [c.kwargs["locale"] for c in browser.new_context.call_args_list]
    assert langs == ["en", "en", "es", "es"]
    shots = [c.kwargs["path"].name for c in page.screenshot.call_args_list]
    assert "login-375-en.png" in shots
    assert "menu-open-375-es.png" in shots
    assert "menu-open-1280-en.png" not in shots  # only phones have the menu
    assert "media-1280-es.png" in shots

    page.evaluate.return_value = {**fits, "scroll_width": 500}
    problems = screenshots.capture_mobile("http://h:1", tmp_path, "pid")
    assert problems
    assert all("-375-" in line for line in problems)


@pytest.mark.parametrize(("problems", "code"), [([], 0), (["x: spills"], 1)])
def test_run_mobile_exit_code_reflects_sideways_scroll(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    problems: list[str],
    code: int,
) -> None:
    @contextlib.contextmanager
    def fake_server(_: Path) -> Iterator[str]:
        yield "http://127.0.0.1:1"

    monkeypatch.setattr(screenshots, "running_server", fake_server)

    @contextlib.contextmanager
    def fake_stalled() -> Iterator[str]:
        yield "http://127.0.0.1:2/slow.mp3"

    seed_queue = MagicMock()
    monkeypatch.setattr(screenshots, "stalled_source", fake_stalled)
    monkeypatch.setattr(screenshots, "seed_queue", seed_queue)
    monkeypatch.setattr(screenshots, "login", MagicMock())
    monkeypatch.setattr(
        screenshots, "seed", MagicMock(return_value=DemoDevice("d", "k", {"Ada": "p"}))
    )
    capture = MagicMock(return_value=problems)
    monkeypatch.setattr(screenshots, "capture_mobile", capture)

    assert screenshots.run_mobile(tmp_path) == code
    assert capture.call_args.args[2] == "p"
    assert seed_queue.call_args.args[1] == "http://127.0.0.1:2/slow.mp3"
