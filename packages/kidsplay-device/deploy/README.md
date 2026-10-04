# Device deploy: console-boot kiosk

`install-kiosk.sh` turns a KidsPlay handheld (Raspberry Pi CM4 in a Retroflag
GPi Case 2, running Raspberry Pi OS "trixie" with the labwc/Wayland desktop)
into a single-purpose appliance that **boots straight into the app** — no
desktop shell, no login greeter.

## Why it works the way it does

The obvious approach (text console + SDL `kmsdrm`) does **not** work on this
hardware, and neither does native Wayland:

- The panel is a raw DPI display driven by the **fake-KMS** overlay
  `vc4-fkms-v3d`. SDL's `kmsdrm` backend acquires DRM master fine, but every
  `drmModePageFlip` fails with `EINVAL`. The only fix is switching to full-KMS
  (`vc4-kms-v3d`), which is unsafe on a hand-tuned DPI panel (risk of a
  black-screened display with no local recovery), so we don't touch the overlay.
- **pygame-ce's bundled SDL has no native Wayland backend** (`SDL_VIDEODRIVER=
  wayland` → "wayland not available"). The app has always rendered as an
  **X11/SDL app via XWayland** under the Pi's labwc compositor.

So the kiosk keeps that proven render path: a **bare `labwc`** compositor whose
sole job is to host the app under XWayland, launched via `labwc -S`.

## What the installer changes

| Change | Purpose |
|---|---|
| `getty@tty1` autologin drop-in | log the user in on the console at boot |
| `~/.bash_profile` hook | on **tty1 only** → `exec labwc -S ~/.local/bin/kidsplay-kiosk.sh` (guarded so it never fires over SSH) |
| `~/.local/bin/kidsplay-kiosk.sh` | runs the app with `--fullscreen` (the pairing screen too, whichever tool wrote the config) under `SDL_VIDEODRIVER=x11`, restart-on-exit, logs to `~/kidsplay-kiosk.log` |
| empty `~/.config/labwc/autostart` | suppresses `wf-panel-pi` / `pcmanfm` / `lxsession-xdg-autostart` (bare labwc runs the *system* autostart otherwise) |
| transparent Xcursor theme + `~/.config/labwc/environment` | hides labwc's own (wlroots) compositor cursor — there's no mouse, and X-side tools like `unclutter` can't touch the compositor cursor |
| `set-default multi-user.target` | boot to console, not the desktop |
| `disable lightdm`, `disable wayvnc` | drop desktop + VNC **autostart** (both remain installed and startable) |
| `kidsplay-timeset.service` + hourly `.timer` | step the clock from the server's HTTP `Date` header (no RTC, and an isolated kids' network may have no NTP route); runs **in the background** and retries until the server answers. **Not installed** when the config has `"sync_transport": "local"` (all-in-one, see [docs/ALL_IN_ONE.md](../../../docs/ALL_IN_ONE.md)) or with `--no-timeset`: the server is this device, so its clock cannot correct ours |

## The device must boot and play with no network

A handheld goes to grandma's, or comes up after a power cut before the router
and NAS do. Neither may stop it from booting into the app:

- **Nothing in the boot waits for WiFi or the server.** `kidsplay-timeset` is
  `Type=exec` with no ordering against `getty@tty1`, so the kiosk starts
  whether or not the clock has been corrected. (It was once ordered before
  getty, and the handhelds sat at a black console for minutes whenever the
  server was unreachable.) `tests/test_install_kiosk.py` fails if that ordering
  comes back.
- **The app plays whatever it last synced.** Sync runs in a daemon thread that
  swallows every error and retries each `sync_interval_seconds`; a first sync
  that fails on a stale clock costs one interval.
- **Bedtime never trusts an obviously wrong clock.** Until a sync has seen
  the server's time, a clock earlier than the last sync disables bedtime
  (logged as `Clock untrusted`) instead of locking the kid out. See
  `docs/SETTINGS.md` for what cannot be detected offline.

`brcm-pcie fd500000.pcie: link down` on the console at boot is **harmless**: the
CM4 has nothing on its PCIe lane. It is just the last kernel line printed
before the kiosk takes the screen. If the screen stays there, something
is holding `getty@tty1`. `systemd-analyze critical-chain getty@tty1.service`
names it.

## Usage

### One command: `install-device.sh`

Run it from the repo checkout on the handheld, as the normal user. It installs
the right packages, sets the device up and then runs `install-kiosk.sh`, and it
refuses to finish unless you choose a mode:

```bash
# Connect to your server (recommended): boots to a pairing code, approve on a phone
./install-device.sh --server https://kidsplay.example.net --timezone America/New_York
# ...or install a config made by `kidsplay device setup --output config.json`
./install-device.sh --server https://kidsplay.example.net --config config.json
# All-in-one: the server and the player on this device
./install-device.sh --standalone --profile-name Alice --lan --timezone America/New_York
```

`--server` uses `uv sync --package kidsplay-device --locked` (never a bare
`uv sync`, which uninstalls the player) and passes the address to
`install-kiosk.sh --server`, which writes `~/.kidsplay/pair-server.txt` (see
[docs/PAIRING.md](../../../docs/PAIRING.md)). `--standalone` uses
`uv sync --all-packages --locked`, needs ffmpeg and runs `kidsplay-allinone`
([docs/ALL_IN_ONE.md](../../../docs/ALL_IN_ONE.md)); the admin password comes
from `KIDSPLAY_ADMIN_PASSWORD` or a prompt. Bad input (address, timezone,
config) is refused before anything on the device changes, and an existing
config for another server is only replaced with `--force` (kept as
`config.json.old`). Its test overrides are listed at the top of the script.

### `install-kiosk.sh` on its own

Run it **as the normal user** (not root):

```bash
scp install-kiosk.sh <device>:
ssh <device> './install-kiosk.sh'      # idempotent
ssh <device> 'sudo reboot'
```

The app command defaults to `~/kidsplay/.venv/bin/kidsplay-player`; override with
`KIDSPLAY_APP_CMD=/path/to/kidsplay-player ./install-kiosk.sh`.

`--server URL` presets the pairing server (written to
`~/.kidsplay/pair-server.txt` after the address is checked), so a device with no
config boots straight to its pairing code.

**Set the timezone.** Bedtime is enforced in the device's system timezone, and a
fresh Pi OS image is on UTC. Pass `--timezone ZONE` (or `KIDSPLAY_TIMEZONE=ZONE`),
e.g. `./install-kiosk.sh --timezone America/New_York`; it runs
`timedatectl set-timezone` (a no-op when already set) and validates the name
first. Without it, the installer prints a warning while the zone is UTC.
Rollback does not revert the timezone.

## Recovery & rollback

- **Physical recovery:** Ctrl+Alt+F2 → a `getty` login on tty2 (only tty1 is
  taken by the app). SSH is independent of the display and always available.
- **Start the desktop once, without rolling back:**
  `sudo systemctl isolate graphical.target` (kiosk returns on next reboot).
- **VNC in once:** `sudo systemctl start wayvnc`.
- **Full rollback to desktop boot:** `./install-kiosk.sh --rollback && sudo reboot`.

## Per-unit notes

Every handheld gets the identical setup. On a clean boot the
app is resident in ~5–10 s using a few hundred MB of RAM; a 2 GB CM4 is
comfortable. If a unit is on a temporary DHCP address (router reservation not
yet updated), reach it by IP; the kiosk itself is independent of the address.
