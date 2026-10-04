"""Static checks on the kiosk installer's boot wiring.

The handheld must boot into the app with no network: away from home, or after a
power cut that took the router and NAS down. A unit ordered before the tty1
getty (or a oneshot the boot waits on) turns an unreachable server into a toy
stuck on a black console, which is exactly what happened with
``kidsplay-timeset``. These tests read the units out of ``install-kiosk.sh``,
the only place they are defined.
"""

import re
import subprocess
from pathlib import Path

INSTALLER = Path(__file__).resolve().parents[1] / "deploy" / "install-kiosk.sh"


def _heredoc(name: str) -> str:
    """Return the body of the ``<<NAME`` / ``<<'NAME'`` heredoc in the installer.

    Args:
        name: The heredoc delimiter, e.g. ``UNIT``.

    Returns:
        The heredoc body, without the delimiter lines.
    """
    text = INSTALLER.read_text()
    match = re.search(rf"<<'?{name}'?\n(.*?)\n{name}\n", text, re.DOTALL)
    assert match, f"heredoc {name} not found in {INSTALLER}"
    return match.group(1)


def test_timeset_unit_does_not_hold_the_kiosk() -> None:
    """The clock bootstrap must not be ordered before the tty1 autologin."""
    unit = _heredoc("UNIT")
    assert "kidsplay-timeset" in unit
    assert not re.search(r"^Before=.*getty", unit, re.MULTILINE)


def test_timeset_unit_is_not_waited_on() -> None:
    """A oneshot holds up everything ordered after it until the server answers."""
    unit = _heredoc("UNIT")
    assert re.search(r"^Type=exec$", unit, re.MULTILINE)


def test_timeset_unit_does_not_wait_for_network_online() -> None:
    """``network-online.target`` blocks until a connection exists, i.e. forever
    away from home."""
    assert "network-online" not in _heredoc("UNIT")


def test_timeset_script_never_gives_up() -> None:
    """The script retries until the server answers, so a device brought home
    corrects its clock within a minute rather than at the next timer run."""
    script = _heredoc("TIMESET")
    assert "while :; do" in script
    assert "exit 1\n" not in script.split("while :; do", 1)[1]


def test_kiosk_is_not_ordered_after_time_sync() -> None:
    """Waiting for ``time-sync.target`` never ends without an NTP peer.

    The player confirms an NTP-synchronized clock itself (``controls.py``);
    the boot must not wait for it.
    """
    code = [
        line
        for line in INSTALLER.read_text().splitlines()
        if not line.lstrip().startswith("#")
    ]
    assert not any("time-sync" in line or "time-wait-sync" in line for line in code)


def test_installer_parses() -> None:
    """The installer is valid bash (a syntax slip would brick a device setup)."""
    subprocess.run(["bash", "-n", str(INSTALLER)], check=True)


def test_timezone_option_sets_the_zone() -> None:
    """Bedtime is enforced in the system timezone, so the installer can set it."""
    text = INSTALLER.read_text()
    assert "--timezone" in text
    assert "KIDSPLAY_TIMEZONE" in text
    assert re.search(r"sudo timedatectl set-timezone \"\$TIMEZONE\"", text)
    # Idempotent: no change when the zone already matches.
    assert '[ "$CURRENT_TZ" = "$TIMEZONE" ]' in text


def test_timezone_validated_before_any_change() -> None:
    text = INSTALLER.read_text()
    validate = text.index("timedatectl list-timezones")
    first_change = text.index('install -d "$HOME/.local/bin"')
    assert validate < first_change


def test_utc_without_timezone_warns() -> None:
    text = INSTALLER.read_text()
    warn = text.split("TZWARN", 2)[1]
    assert "WARNING" in warn
    assert "LOCAL time" in warn
    assert "--timezone" in warn
    assert "Etc/UTC" in text.split('case "$CURRENT_TZ" in', 1)[1]


def test_server_option_presets_pairing_and_is_checked_first() -> None:
    """``--server`` writes pair-server.txt next to the config, after checking the
    address with the player's own parser and before changing anything."""
    script = INSTALLER.read_text()
    assert "--server)" in script and "--server=*)" in script
    assert "from kidsplay_device.pairing import normalize_server_url" in script
    check = script.index("normalize_server_url(sys.argv[1])")
    write = script.index('/pair-server.txt"')
    first_change = script.index('install -d "$HOME/.local/bin"')
    assert check < write < first_change


def test_server_check_output_is_only_the_address() -> None:
    """The check imports the player, and pygame prints a banner on stdout; on
    the reference handheld that banner became the "address". Only the last
    line is used, with the banner turned off."""
    script = INSTALLER.read_text()
    start = script.index('PAIR_URL="$(')
    check = script[start : script.index("|| die", start)]
    assert "PYGAME_HIDE_SUPPORT_PROMPT=1" in check
    assert "| tail -n 1" in check


def test_launcher_always_runs_the_player_fullscreen() -> None:
    """A kiosk has no window: the generated launcher passes ``--fullscreen``,
    which also covers the pairing screen and configs that do not say so."""
    script = INSTALLER.read_text()
    start = script.index("<<KIOSK")
    launcher = script[start : script.index("\nKIOSK\n", start)]
    assert "$APP_CMD --fullscreen" in launcher
