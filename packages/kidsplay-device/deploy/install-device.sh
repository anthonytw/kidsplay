#!/usr/bin/env bash
#
# KidsPlay handheld: one command from a fresh checkout to a booting kiosk.
#
# Run on the handheld, from the repo checkout (~/kidsplay), as your normal user.
# It will not finish unless you chose one of two modes:
#
#   --server URL     connect to a KidsPlay server you already run.
#                    Recommended: the device boots straight to a pairing code
#                    and you approve it from a phone. Nobody types an address
#                    on a D-pad keyboard.
#   --standalone     all-in-one: the server and the player on this one device.
#
# Then it installs the console-boot kiosk (install-kiosk.sh). See usage() below,
# docs/PAIRING.md and docs/ALL_IN_ONE.md.
#
# For tests every command it calls can be replaced:
#   KIDSPLAY_UV             uv                              (default: uv)
#   KIDSPLAY_KIOSK_INSTALLER install-kiosk.sh                (default: next to this script)
#   KIDSPLAY_ALLINONE       kidsplay-allinone               (default: <repo>/.venv/bin/kidsplay-allinone)
#   KIDSPLAY_PYTHON         the player's Python             (default: <repo>/.venv/bin/python)
#   KIDSPLAY_APP_CMD        the player                      (default: <repo>/.venv/bin/kidsplay-player)
#   KIDSPLAY_CONFIG         the device config               (default: ~/.kidsplay/config.json)
#   KIDSPLAY_FFMPEG         the ffmpeg to look for          (default: ffmpeg)
#   KIDSPLAY_REPO           the checkout                    (default: derived from this script)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="${KIDSPLAY_REPO:-$(cd "$SCRIPT_DIR/../../.." && pwd)}"
# uv's own installer puts it in ~/.local/bin (older ones: ~/.cargo/bin), which a
# non-login shell (ssh host cmd, cron, a fresh script) does not have on PATH.
find_uv() {
  if [ -n "${KIDSPLAY_UV:-}" ]; then echo "$KIDSPLAY_UV"; return; fi
  if command -v uv >/dev/null 2>&1; then command -v uv; return; fi
  for candidate in "$HOME/.local/bin/uv" "$HOME/.cargo/bin/uv"; do
    if [ -x "$candidate" ]; then echo "$candidate"; return; fi
  done
  echo uv
}
UV="$(find_uv)"
KIOSK_INSTALLER="${KIDSPLAY_KIOSK_INSTALLER:-$SCRIPT_DIR/install-kiosk.sh}"
ALLINONE="${KIDSPLAY_ALLINONE:-$REPO/.venv/bin/kidsplay-allinone}"
PY="${KIDSPLAY_PYTHON:-$REPO/.venv/bin/python}"
APP_CMD="${KIDSPLAY_APP_CMD:-$REPO/.venv/bin/kidsplay-player}"
CONFIG_PATH="${KIDSPLAY_CONFIG:-$HOME/.kidsplay/config.json}"
CONFIG_DIR="$(dirname "$CONFIG_PATH")"

SERVER=""
CONFIG_FILE=""
STANDALONE=0
PROFILE_NAME=""
DEVICE_NAME=""
PORT=""
LAN=0
TIMEZONE="${KIDSPLAY_TIMEZONE:-}"
FORCE=0

usage() {
  cat <<'USAGE'
usage: install-device.sh (--server URL [--config FILE] | --standalone --profile-name NAME)
                         [--timezone ZONE] [--force]

Set up this handheld from the repo checkout. Choose ONE mode:

  --server URL      Connect to the KidsPlay server at URL (recommended).
                    The device boots straight to a pairing code; approve it
                    from a phone. No address to type on the device.
      --config FILE   Instead of pairing, install FILE (made by
                      `kidsplay device setup --output FILE`) as the device
                      config. Its server_url must match URL.

  --standalone      All-in-one: run the server and the player on this device.
      --profile-name NAME   Child's profile to create (required).
      --device-name NAME    This device's name.
      --lan                 Make the web UI reachable from the network.
      --port PORT           Web UI port (default 8000).
                            The admin password comes from KIDSPLAY_ADMIN_PASSWORD
                            or a prompt, never a command-line flag.

Both modes:
  --timezone ZONE   Set the device timezone (bedtime uses it), e.g. America/New_York.
  --force           Replace an existing config that points at another server
                    (the old one is kept as config.json.old).
USAGE
}

die() { echo "ERROR: $*" >&2; exit 1; }
need_arg() { [ "$2" -ge 2 ] || die "$1 needs a value"; }

while [ $# -gt 0 ]; do
  case "$1" in
    --server) need_arg "$1" $#; SERVER="$2"; shift ;;
    --server=*) SERVER="${1#--server=}" ;;
    --config) need_arg "$1" $#; CONFIG_FILE="$2"; shift ;;
    --config=*) CONFIG_FILE="${1#--config=}" ;;
    --standalone) STANDALONE=1 ;;
    --profile-name) need_arg "$1" $#; PROFILE_NAME="$2"; shift ;;
    --profile-name=*) PROFILE_NAME="${1#--profile-name=}" ;;
    --device-name) need_arg "$1" $#; DEVICE_NAME="$2"; shift ;;
    --device-name=*) DEVICE_NAME="${1#--device-name=}" ;;
    --port) need_arg "$1" $#; PORT="$2"; shift ;;
    --port=*) PORT="${1#--port=}" ;;
    --lan) LAN=1 ;;
    --timezone) need_arg "$1" $#; TIMEZONE="$2"; shift ;;
    --timezone=*) TIMEZONE="${1#--timezone=}" ;;
    --force) FORCE=1 ;;
    -h|--help) usage; exit 0 ;;
    --admin-password*) die "the admin password is never a flag (it shows in ps): use KIDSPLAY_ADMIN_PASSWORD or the prompt" ;;
    *) usage >&2; die "unknown option: $1" ;;
  esac
  shift
done

# --- choose exactly one mode ------------------------------------------------
if [ -z "$SERVER" ] && [ "$STANDALONE" -eq 0 ]; then
  usage >&2
  die "choose a mode: --server URL (connect to your server) or --standalone (all-in-one)"
fi
if [ -n "$SERVER" ] && [ "$STANDALONE" -eq 1 ]; then
  usage >&2
  die "--server and --standalone are different modes: choose one"
fi
if [ "$STANDALONE" -eq 1 ]; then
  [ -z "$CONFIG_FILE" ] || die "--config goes with --server (standalone writes its own config)"
  [ -n "$PROFILE_NAME" ] || die "--standalone needs --profile-name NAME (the child's profile)"
else
  [ -z "$PROFILE_NAME$DEVICE_NAME$PORT" ] && [ "$LAN" -eq 0 ] \
    || die "--profile-name, --device-name, --port and --lan belong to --standalone"
fi
if [ -n "$PORT" ]; then
  case "$PORT" in ''|*[!0-9]*) die "--port must be a number: $PORT" ;; esac
fi
[ "$(id -u)" -ne 0 ] || die "run as your normal user, not root"
command -v "$UV" >/dev/null 2>&1 || die "uv not found (install it: https://docs.astral.sh/uv/)"
[ -x "$KIOSK_INSTALLER" ] || [ -f "$KIOSK_INSTALLER" ] || die "kiosk installer not found: $KIOSK_INSTALLER"

# --- checks that need no installed player -----------------------------------
if [ -n "$TIMEZONE" ]; then
  python3 -c '
import sys
from zoneinfo import ZoneInfo
try:
    ZoneInfo(sys.argv[1])
except Exception:
    sys.exit(1)
' "$TIMEZONE" 2>/dev/null || die "unknown timezone '$TIMEZONE' (e.g. America/New_York)"
fi

if [ "$STANDALONE" -eq 1 ]; then
  command -v "${KIDSPLAY_FFMPEG:-ffmpeg}" >/dev/null 2>&1 \
    || die "ffmpeg is not installed (the server needs it): sudo apt-get install -y ffmpeg"
fi

# json_get FILE KEY: a top-level string value, or nothing.
json_get() {
  python3 -c '
import json, sys
try:
    value = json.load(open(sys.argv[1])).get(sys.argv[2], "")
except (OSError, ValueError, AttributeError):
    value = ""
print(value if isinstance(value, str) else "")
' "$1" "$2" 2>/dev/null || true
}

if [ -n "$CONFIG_FILE" ]; then
  [ -f "$CONFIG_FILE" ] || die "--config: no such file: $CONFIG_FILE"
  python3 -c '
import json, sys
required = ("server_url", "device_id", "api_key", "media_root", "db_path")
try:
    data = json.load(open(sys.argv[1]))
except (OSError, ValueError) as exc:
    sys.exit(f"not valid JSON: {exc}")
if not isinstance(data, dict):
    sys.exit("not a JSON object")
missing = [k for k in required if not isinstance(data.get(k), str) or not data[k]]
if missing:
    sys.exit("missing " + ", ".join(missing))
' "$CONFIG_FILE" || die "--config $CONFIG_FILE is not a device config (make one with: kidsplay device setup --output FILE)"
fi

# --- checks that use the player's own address parser ------------------------
# The same parser the device and install-kiosk.sh use. It needs the player
# installed, so on a first run this happens right after `uv sync`; nothing on
# the device's config has been touched by then either way. Importing the
# player prints pygame's banner on stdout: hide it, keep only the last line.
normalize() {
  PYGAME_HIDE_SUPPORT_PROMPT=1 "$PY" -c '
import sys
from kidsplay_device.pairing import normalize_server_url
print(normalize_server_url(sys.argv[1]) or "")
' "$1" 2>/dev/null | tail -n 1
}

SERVER_URL=""
REPLACE_EXISTING=0   # an existing config for another server will be replaced
preflight() {
  if [ -n "$SERVER" ]; then
    SERVER_URL="$(normalize "$SERVER")" || true
    [ -n "$SERVER_URL" ] || die "--server: not a server address: $SERVER"
    if [ -n "$CONFIG_FILE" ]; then
      local theirs
      theirs="$(normalize "$(json_get "$CONFIG_FILE" server_url)")" || true
      if [ "$theirs" != "$SERVER_URL" ] && [ "$FORCE" -eq 0 ]; then
        die "--config is for ${theirs:-an unknown server}, not $SERVER_URL (--force to use it anyway)"
      fi
    fi
  fi
  if [ -f "$CONFIG_PATH" ]; then
    if [ "$STANDALONE" -eq 1 ]; then
      [ "$(json_get "$CONFIG_PATH" sync_transport)" = "local" ] && return 0
      REPLACE_EXISTING=1
    else
      local existing
      existing="$(normalize "$(json_get "$CONFIG_PATH" server_url)")" || true
      if [ "$existing" = "$SERVER_URL" ]; then ALREADY_PAIRED=1; return 0; fi
      REPLACE_EXISTING=1
    fi
    [ "$FORCE" -eq 1 ] || die "$CONFIG_PATH already configures this device for $(json_get "$CONFIG_PATH" server_url || true) (--force to replace it; the old one is kept as config.json.old)"
  fi
}

PREFLIGHT_DONE=0
ALREADY_PAIRED=0
if [ -x "$PY" ]; then preflight; PREFLIGHT_DONE=1; fi

# --- install the packages ---------------------------------------------------
# Never a bare `uv sync`: it removes the player (see the deploy README).
echo "== installing packages =="
if [ "$STANDALONE" -eq 1 ]; then
  (cd "$REPO" && "$UV" sync --all-packages --locked)
else
  (cd "$REPO" && "$UV" sync --package kidsplay-device --locked)
fi
[ -x "$PY" ] || die "the player's Python is missing after uv sync: $PY (set KIDSPLAY_PYTHON)"
[ "$PREFLIGHT_DONE" -eq 1 ] || preflight

# --- configure ---------------------------------------------------------------
backup_existing() {
  if [ "$REPLACE_EXISTING" -eq 1 ] && [ -f "$CONFIG_PATH" ]; then
    cp -p "$CONFIG_PATH" "$CONFIG_PATH.old"
    chmod 600 "$CONFIG_PATH.old"
    echo "   previous config kept as $CONFIG_PATH.old"
  fi
}

KIOSK_ARGS=()
[ -z "$TIMEZONE" ] || KIOSK_ARGS+=(--timezone "$TIMEZONE")

if [ "$STANDALONE" -eq 1 ]; then
  echo "== all-in-one: server + player on this device =="
  args=(--profile-name "$PROFILE_NAME" --config-path "$CONFIG_PATH")
  [ -z "$DEVICE_NAME" ] || args+=(--device-name "$DEVICE_NAME")
  [ -z "$PORT" ] || args+=(--port "$PORT")
  [ "$LAN" -eq 0 ] || args+=(--lan)
  [ "$FORCE" -eq 0 ] || args+=(--force)
  # The admin password is read from KIDSPLAY_ADMIN_PASSWORD or prompted for.
  "$ALLINONE" "${args[@]}"
elif [ -n "$CONFIG_FILE" ]; then
  echo "== installing the device config =="
  backup_existing
  install -d -m 700 "$CONFIG_DIR"
  chmod 700 "$CONFIG_DIR"
  tmp="$CONFIG_PATH.tmp.$$"
  ( umask 077; cat "$CONFIG_FILE" > "$tmp" )
  chmod 600 "$tmp"
  mv -f "$tmp" "$CONFIG_PATH"
  echo "   $CONFIG_PATH installed (mode 600)"
else
  # Preset the pairing server: install-kiosk.sh owns writing pair-server.txt.
  # A config left over from another server would stop the device pairing.
  if [ "$REPLACE_EXISTING" -eq 1 ]; then
    backup_existing
    /bin/rm -f "$CONFIG_PATH"
  fi
  KIOSK_ARGS+=(--server "$SERVER_URL")
fi

# --- boot into the player ----------------------------------------------------
echo "== installing the kiosk =="
export KIDSPLAY_APP_CMD="$APP_CMD" KIDSPLAY_CONFIG="$CONFIG_PATH"
"$KIOSK_INSTALLER" ${KIOSK_ARGS[@]+"${KIOSK_ARGS[@]}"}

echo
if [ "$STANDALONE" -eq 1 ]; then
  echo "== done. Reboot: the player starts fullscreen. Import media: docs/ALL_IN_ONE.md =="
elif [ -n "$CONFIG_FILE" ] || [ "$ALREADY_PAIRED" -eq 1 ]; then
  echo "== done. Reboot: the device syncs with $SERVER_URL =="
else
  echo "== done. Reboot: the device shows a pairing code for $SERVER_URL;"
  echo "   open Devices on the web UI (or scan the QR code) and approve it =="
fi
