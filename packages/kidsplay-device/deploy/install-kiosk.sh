#!/usr/bin/env bash
#
# KidsPlay handheld -> console-boot kiosk installer.
#
# Converts a Raspberry Pi CM4 running Raspberry Pi OS (Debian 13 "trixie",
# labwc/Wayland desktop) so it boots straight into the KidsPlay pygame app,
# fullscreen, with no desktop shell and no display-manager greeter.
#
# WHY NOT kmsdrm / a pure text console:
#   This hardware uses the *fake-KMS* overlay `vc4-fkms-v3d` on a raw DPI panel.
#   SDL's kmsdrm backend acquires DRM master but every drmModePageFlip returns
#   EINVAL (a fake-KMS limitation); the only fix is full-KMS, which is unsafe on
#   a hand-tuned DPI panel. pygame-ce's bundled SDL also has *no* native Wayland
#   backend. The app has therefore always rendered as an X11/SDL app via
#   XWayland. This kiosk keeps that proven path: a bare `labwc` compositor whose
#   only job is to host the app under XWayland.
#
# WHAT IT DOES (all reversible, see --rollback):
#   - getty@tty1 autologin as the invoking user
#   - ~/.bash_profile: on tty1 only -> `exec labwc -S <app-launcher>` (never SSH)
#   - empty ~/.config/labwc/autostart -> suppresses wf-panel-pi / pcmanfm /
#     lxsession-xdg-autostart (bare labwc runs the system autostart otherwise)
#   - transparent Xcursor theme -> hides labwc's compositor cursor (no mouse)
#   - set-default multi-user.target; disable lightdm and wayvnc autostart
#     (both stay installed and startable on demand)
#   - kidsplay-timeset.service/.timer: step the clock from the server's HTTP
#     Date header (no RTC, and an isolated kids' network may have no NTP route).
#     Skipped for an all-in-one config (see below): the server is this machine.
#
# The launcher always runs the player with --fullscreen (also for the pairing
# screen), whichever tool wrote config.json: a kiosk has no window to manage.
#
# Recovery: a getty stays available on tty2 (Ctrl+Alt+F2). SSH is unaffected.
#
# Usage:
#   ./install-kiosk.sh              # install (idempotent)
#   ./install-kiosk.sh --timezone America/New_York   # also set the timezone
#   ./install-kiosk.sh --rollback   # revert to desktop boot
#   ./install-kiosk.sh --no-timeset # never install the clock bootstrap
#
# ALL-IN-ONE: when the config has "sync_transport": "local" (written by
# `kidsplay-allinone`, docs/ALL_IN_ONE.md) the server runs on this same device,
# so its Date header is this device's own clock and cannot correct it. The
# clock bootstrap is then not installed (and removed if an earlier install
# left it). Use NTP or set the clock by hand instead.
#
# TIMEZONE: bedtime is enforced in the device's *system* timezone, and a fresh
# Pi OS image is on UTC, which would start bedtime hours early or late. Pass
# --timezone ZONE (or KIDSPLAY_TIMEZONE=ZONE) to set it; the installer warns
# when the zone is still UTC and none was given. Rollback leaves it as set.
#
# Runs as your normal user (NOT root); it uses sudo only for the system bits.
# Preset the server to pair with, so nobody types its address on the device:
#   ./install-kiosk.sh --server https://kidsplay.example.net
# Override the app command with:  KIDSPLAY_APP_CMD=/path/to/kidsplay-player
# Override the device config with: KIDSPLAY_CONFIG=/path/to/config.json

set -euo pipefail

TTY="tty1"
USER_NAME="$(id -un)"
APP_CMD="${KIDSPLAY_APP_CMD:-$HOME/kidsplay/.venv/bin/kidsplay-player}"
CONFIG_PATH="${KIDSPLAY_CONFIG:-$HOME/.kidsplay/config.json}"

TIMEZONE="${KIDSPLAY_TIMEZONE:-}"
NO_TIMESET=0
PAIR_SERVER=""

# The config's sync transport: "local" (all-in-one) or "http" (anything else,
# including a missing or unreadable config).
config_transport() {
  python3 -c '
import json, sys
try:
    print(json.load(open(sys.argv[1])).get("sync_transport", "http"))
except (OSError, ValueError, AttributeError):
    print("http")
' "$CONFIG_PATH" 2>/dev/null || echo http
}

remove_timeset() {
  sudo systemctl disable --now kidsplay-timeset.timer kidsplay-timeset.service 2>/dev/null || true
  sudo rm -f /etc/systemd/system/kidsplay-timeset.service \
             /etc/systemd/system/kidsplay-timeset.timer \
             /usr/local/sbin/kidsplay-timeset.sh
}

die() { echo "ERROR: $*" >&2; exit 1; }

rollback() {
  echo "== rollback: restoring desktop boot =="
  sudo systemctl set-default graphical.target
  sudo systemctl enable lightdm 2>/dev/null || true
  echo "   (wayvnc left disabled; 'sudo systemctl enable wayvnc' to restore VNC autostart)"
  remove_timeset
  sudo rm -f "/etc/systemd/system/getty@${TTY}.service.d/autologin.conf"
  sudo systemctl revert "getty@${TTY}" 2>/dev/null || true
  sudo systemctl daemon-reload
  rm -f "$HOME/.config/labwc/autostart" "$HOME/.local/bin/kidsplay-kiosk.sh"
  [ -f "$HOME/.bash_profile" ] && sed -i '/# KidsPlay kiosk/,/^fi$/d' "$HOME/.bash_profile"
  if [ -f "$HOME/.config/labwc/environment" ]; then
    sed -i '/^XCURSOR_THEME=blank$/d; /^XCURSOR_SIZE=24$/d' "$HOME/.config/labwc/environment"
  fi
  rm -rf "$HOME/.icons/blank"
  echo "Done. Reboot to return to the desktop."
}

ROLLBACK=0
while [ $# -gt 0 ]; do
  case "$1" in
    --rollback) ROLLBACK=1 ;;
    --no-timeset) NO_TIMESET=1 ;;
    --timezone)
      [ $# -ge 2 ] || die "--timezone needs a zone name, e.g. America/New_York"
      TIMEZONE="$2"; shift ;;
    --timezone=*) TIMEZONE="${1#--timezone=}" ;;
    --server)
      [ $# -ge 2 ] || die "--server needs the server's address, e.g. https://kidsplay.example.net"
      PAIR_SERVER="$2"; shift ;;
    --server=*) PAIR_SERVER="${1#--server=}" ;;
    *) die "unknown option: $1  (use --timezone ZONE, --server URL, --no-timeset or --rollback)" ;;
  esac
  shift
done

if [ "$ROLLBACK" -eq 1 ]; then rollback; exit 0; fi
if [ "$(id -u)" -eq 0 ]; then die "run as your normal user, not root"; fi
[ -x "$APP_CMD" ] || die "app not found/executable: $APP_CMD  (set KIDSPLAY_APP_CMD)"
command -v labwc >/dev/null || die "labwc not installed (expected the Pi labwc desktop)"

# Fail on a mistyped zone before changing anything else.
if [ -n "$TIMEZONE" ]; then
  timedatectl list-timezones | grep -qx -- "$TIMEZONE" \
    || die "unknown timezone '$TIMEZONE' (see: timedatectl list-timezones)"
fi

# Check a preset pairing server the same way the device will read it, so a typo
# fails here rather than on the handheld.
PAIR_URL=""
if [ -n "$PAIR_SERVER" ]; then
  # Importing the player pulls in pygame, which prints a banner on stdout:
  # hide it, and keep only the last line (the checked address).
  PAIR_URL="$(PYGAME_HIDE_SUPPORT_PROMPT=1 "$(dirname "$APP_CMD")/python" -c '
import sys
from kidsplay_device.pairing import normalize_server_url
print(normalize_server_url(sys.argv[1]) or "")
' "$PAIR_SERVER" | tail -n 1)" || die "could not check --server with the player's Python"
  [ -n "$PAIR_URL" ] || die "--server: not a server address: $PAIR_SERVER"
fi

echo "== KidsPlay kiosk install: user='$USER_NAME' app='$APP_CMD' =="

# 0) preset pairing server: used only while there is no config.json ----------
if [ -n "$PAIR_URL" ]; then
  install -d -m 700 "$(dirname "$CONFIG_PATH")"
  printf '%s\n' "$PAIR_URL" > "$(dirname "$CONFIG_PATH")/pair-server.txt"
  echo "   pairing server preset: $PAIR_URL (the device pairs with it straight away)"
fi

# 1) kiosk launcher (app under XWayland, SDL x11, restart-on-exit) -----------
install -d "$HOME/.local/bin"
cat > "$HOME/.local/bin/kidsplay-kiosk.sh" <<KIOSK
#!/bin/sh
LOG="\$HOME/kidsplay-kiosk.log"
export SDL_VIDEODRIVER=x11
while :; do
  echo "=== \$(date -Is) start (DISPLAY=\$DISPLAY) ===" >>"\$LOG"
  $APP_CMD --fullscreen >>"\$LOG" 2>&1
  echo "=== \$(date -Is) exit rc=\$? ===" >>"\$LOG"
  sleep 2
done
KIOSK
chmod +x "$HOME/.local/bin/kidsplay-kiosk.sh"

# 2) empty labwc autostart -> no panel/file-manager/xdg-autostart ------------
install -d -m700 "$HOME/.config/labwc"
cat > "$HOME/.config/labwc/autostart" <<'AS'
#!/bin/sh
# KidsPlay kiosk: intentionally empty. Suppresses the Pi desktop autostart
# (wf-panel-pi, pcmanfm-pi, kanshi, lxsession-xdg-autostart). The app is
# launched by `labwc -S` from ~/.bash_profile.
exit 0
AS
chmod +x "$HOME/.config/labwc/autostart"

# 3) transparent cursor theme -> hide the wlroots compositor cursor ----------
python3 - <<'PY'
import struct, pathlib
home = pathlib.Path.home()
cur = home / ".icons/blank/cursors"
cur.mkdir(parents=True, exist_ok=True)
# Minimal Xcursor: 1x1 fully-transparent image.
img_hdr = [36, 0xfffd0002, 24, 1, 1, 1, 0, 0, 0]          # hdrsz,type,size,ver,w,h,xh,yh,delay
file_hdr = b"Xcur" + struct.pack("<III", 16, 0x00010000, 1)  # magic,hdrsz,ver,ntoc
toc = struct.pack("<III", 0xfffd0002, 24, 16 + 12)           # type,size,position(28)
body = b"".join(struct.pack("<I", x) for x in img_hdr) + struct.pack("<I", 0)
(cur / "default").write_bytes(file_hdr + toc + body)
for name in ("left_ptr", "arrow", "top_left_arrow", "xterm", "hand1", "hand2",
             "watch", "cross", "pointer", "text", "wait"):
    link = cur / name
    if link.exists() or link.is_symlink():
        link.unlink()
    link.symlink_to("default")
(home / ".icons/blank/index.theme").write_text("[Icon Theme]\nName=blank\nInherits=core\n")
PY
touch "$HOME/.config/labwc/environment"
if grep -q '^XCURSOR_THEME=' "$HOME/.config/labwc/environment"; then
  sed -i 's/^XCURSOR_THEME=.*/XCURSOR_THEME=blank/' "$HOME/.config/labwc/environment"
else
  echo 'XCURSOR_THEME=blank' >> "$HOME/.config/labwc/environment"
fi
grep -q '^XCURSOR_SIZE=' "$HOME/.config/labwc/environment" || \
  echo 'XCURSOR_SIZE=24' >> "$HOME/.config/labwc/environment"

# 4) login-shell hook: tty1 console only -> labwc kiosk (never over SSH) -----
touch "$HOME/.bash_profile"
if ! grep -q '# KidsPlay kiosk' "$HOME/.bash_profile"; then
cat >> "$HOME/.bash_profile" <<'PROFILE'

# KidsPlay kiosk: on tty1 console only, launch the labwc Wayland session.
if [ "$(tty)" = "/dev/tty1" ] && [ -z "$DISPLAY" ] && [ -z "$WAYLAND_DISPLAY" ] && [ -z "$SSH_TTY" ]; then
  exec labwc -S "$HOME/.local/bin/kidsplay-kiosk.sh"
fi
PROFILE
fi

# 5) autologin on tty1 -------------------------------------------------------
# Deliberately NOT ordered after time-sync.target (nor Wants=time-sync.target
# with systemd-time-wait-sync enabled): with no route to an NTP peer that target
# is never reached, and the kiosk would sit on a black console like the
# timeset unit once did (see step 7). The player instead notices when NTP has
# synchronized the clock (`timedatectl show -p NTPSynchronized`) and trusts it
# for bedtime from then on; with no network it fails open.
sudo install -d "/etc/systemd/system/getty@${TTY}.service.d"
printf '[Service]\nExecStart=\nExecStart=-/sbin/agetty --autologin %s --noclear %%I $TERM\n' \
  "$USER_NAME" | sudo tee "/etc/systemd/system/getty@${TTY}.service.d/autologin.conf" >/dev/null

# 6) boot to console; drop desktop + VNC autostart (kept installed) ----------
sudo systemctl set-default multi-user.target
sudo systemctl disable lightdm 2>/dev/null || true
sudo systemctl disable wayvnc  2>/dev/null || true
sudo systemctl daemon-reload

# 7) clock bootstrap: set the time from the server, in the background ---------
#
# WHY: the CM4 has no RTC, and a handheld on an isolated network that may reach
# ONLY the KidsPlay server has no route to any NTP peer. systemd restores the clock
# to roughly the last shutdown, so a device that has been off for a while boots
# into the past. Once that boot-time lands before the `notBefore` of the server's
# TLS cert (when it is served over HTTPS), every sync dies with
# "certificate is not yet valid" while WiFi looks perfectly healthy -- which is
# exactly how this presents: connected, but never pulling new content.
#
# The only host the device may talk to is the server, so that is where the time
# comes from: the `Date` header of a HEAD request. It is read with `curl -k`
# because validating the cert is precisely what we cannot do yet -- this is the
# bootstrap that *makes* validation possible. The real sync still verifies the
# cert normally, so an on-path attacker who fed a bogus Date could stall syncing
# but could not impersonate the server or push content.
if [ "$NO_TIMESET" -eq 0 ] && [ "$(config_transport)" = "local" ]; then
  echo "   all-in-one config (sync_transport=local): the server is this device, so"
  echo "   its clock cannot correct ours; not installing kidsplay-timeset."
  echo "   Bedtime needs a real time source: NTP, or set the clock by hand."
  NO_TIMESET=1
fi

if [ "$NO_TIMESET" -eq 1 ]; then
  remove_timeset
  sudo systemctl daemon-reload
else
sudo install -m755 /dev/stdin /usr/local/sbin/kidsplay-timeset.sh <<'TIMESET'
#!/bin/sh
# Step the clock from the KidsPlay server's HTTP Date header.
# Installed by install-kiosk.sh; see the rationale there.
set -eu

CONFIG="${1:?usage: kidsplay-timeset.sh <path/to/config.json>}"

# All-in-one: the "server" is this machine, so its Date header is our own clock.
TRANSPORT=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("sync_transport", "http"))' \
            "$CONFIG" 2>/dev/null || echo http)
if [ "$TRANSPORT" = "local" ]; then
  echo "kidsplay-timeset: the server is this device (sync_transport=local); nothing to set"
  exit 0
fi
MIN_EPOCH=1750000000   # 2025-06-15; anything earlier is a bad read, not a clock
SKEW_TOLERANCE=30      # seconds; below this we leave the clock alone

URL=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["server_url"])' \
      "$CONFIG") || { echo "kidsplay-timeset: cannot read server_url from $CONFIG" >&2; exit 1; }

# WiFi association and DHCP race the boot, so retry quickly for the first two
# minutes, then once a minute, forever. Nothing waits on this unit, so there is
# no reason to give up: a handheld that booted away from home (or while the
# router or server was down) corrects its clock within a minute of reaching
# the server again, instead of at the next hourly timer run.
i=0
while :; do
  header=$(curl -ksI --max-time 10 "$URL/" 2>/dev/null | tr -d '\r' \
           | awk -F': ' 'tolower($1) == "date" { print $2; exit }') || header=""
  if [ -n "$header" ] && epoch=$(date -u -d "$header" +%s 2>/dev/null) \
     && [ "$epoch" -gt "$MIN_EPOCH" ]; then
    skew=$(( epoch - $(date -u +%s) ))
    [ "$skew" -lt 0 ] && skew=$(( -skew ))
    if [ "$skew" -gt "$SKEW_TOLERANCE" ]; then
      date -u -s "@$epoch" >/dev/null
      echo "kidsplay-timeset: stepped clock ${skew}s -> $(date -u -Is)"
      # Carry the corrected time into the next boot's restore point.
      touch /var/lib/systemd/timesync/clock 2>/dev/null || true
    else
      echo "kidsplay-timeset: clock within ${skew}s of the server, unchanged"
    fi
    exit 0
  fi
  i=$(( i + 1 ))
  if [ "$i" -lt 24 ]; then
    sleep 5
  else
    [ "$i" -eq 24 ] && echo "kidsplay-timeset: no usable Date header from $URL yet; retrying every 60s" >&2
    sleep 60
  fi
done
TIMESET

# The boot must NEVER wait on this unit. It used to be `Type=oneshot` with
# `Before=getty@tty1.service`, which held the kiosk at a black console (the
# last kernel line, "brcm-pcie ... link down") for up to 7.5 minutes whenever
# the server was unreachable -- away from home, or after a power cut took the
# router or NAS down. The app plays offline and re-syncs every
# `sync_interval_seconds`, so a first sync that fails on a stale clock costs
# one interval, not a toy that will not turn on. Hence `Type=exec` (started =
# running, so nothing ordered after it waits) and no ordering against getty.
sudo install -m644 /dev/stdin /etc/systemd/system/kidsplay-timeset.service <<UNIT
[Unit]
Description=Set the clock from the KidsPlay server (no RTC, no reachable NTP peer)
Documentation=file:///usr/local/sbin/kidsplay-timeset.sh
Wants=network.target
After=network.target

[Service]
Type=exec
ExecStart=/usr/local/sbin/kidsplay-timeset.sh ${CONFIG_PATH}

[Install]
WantedBy=multi-user.target
UNIT

# Safety net: re-check hourly, so a device that booted before the server was
# reachable still corrects itself without waiting for the next reboot.
sudo install -m644 /dev/stdin /etc/systemd/system/kidsplay-timeset.timer <<'TIMER'
[Unit]
Description=Re-check the clock against the KidsPlay server

[Timer]
OnBootSec=10min
OnUnitActiveSec=1h

[Install]
WantedBy=timers.target
TIMER

sudo systemctl daemon-reload
sudo systemctl enable kidsplay-timeset.service >/dev/null
# --now on the timer only: the service runs at the next boot and from the
# timer, so an install over SSH does not also start stepping the clock.
sudo systemctl enable --now kidsplay-timeset.timer >/dev/null
fi

# 8) timezone: bedtime uses the device's local time --------------------------
CURRENT_TZ="$(timedatectl show -p Timezone --value 2>/dev/null || true)"
if [ -n "$TIMEZONE" ]; then
  if [ "$CURRENT_TZ" = "$TIMEZONE" ]; then
    echo "   timezone already $TIMEZONE"
  else
    sudo timedatectl set-timezone "$TIMEZONE"
    echo "   timezone set to $TIMEZONE (was ${CURRENT_TZ:-unknown})"
  fi
else
  case "$CURRENT_TZ" in
    ""|UTC|Etc/UTC|Etc/UCT|UCT|Etc/Universal|Universal|Etc/Zulu|Zulu)
      cat >&2 <<'TZWARN'
WARNING: the device timezone is UTC. KidsPlay enforces bedtime in the device's
         LOCAL time, so with UTC every bedtime starts and ends hours off.
         Set it with:  ./install-kiosk.sh --timezone America/New_York
         (or: sudo timedatectl set-timezone <zone>)
TZWARN
      ;;
    *) echo "   timezone is $CURRENT_TZ (bedtime follows it)" ;;
  esac
fi

echo "== done. Reboot to enter the kiosk.  Rollback: $0 --rollback =="
