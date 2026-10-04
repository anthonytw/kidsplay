# Hardware release checklist

The manual part of KidsPlay's verification policy (issue #13). Feature logic is
verified in software: unit tests, headless runs and phone-width browser tests
in CI on every PR (tier 1), and the device package on arm64 Debian bookworm
nightly and on device-related PRs (tier 2, `.github/workflows/arm64-device.yml`).
What software cannot see is **tier 3: a real handheld**, and this is its
checklist. Only the Raspberry Pi reference hardware (CM4 in a GPi Case 2) is
supported; other hardware is up to forks.

**When to run it:**

- before every release (tag), on the reference handheld;
- when a change touches **boot** (`install-kiosk.sh`, systemd units, the kiosk
  session), the **display** (resolution, layout, fonts, themes), **input**
  (input profiles, buttons) or the **clock** (`kidsplay-timeset`, bedtime,
  `TimeSource`);
- when a change adds a step to this list: do that step once before merging.

It is *not* part of every PR. Record the date, the commit
(`just device-verify <host>`) and any failure in the release notes. Anyone
adding behaviour that only hardware can confirm adds a step here, not a
separate list.

## Release checklist

Run these on a handheld installed from the release commit.

- [ ] Cold boot with **no network**: the kiosk reaches the home screen and
      plays local media. Radios must boot and play offline.
- [ ] Boot on Wi-Fi: the clock is corrected (`kidsplay-timeset`) before TLS
      sync, with no "certificate is not yet valid" errors.
- [ ] A sync from the server picks up new media, a removed assignment, and a
      changed profile setting.
- [ ] Every physical button does what the on-screen legend says. B/Escape
      backs out of every screen.
- [ ] Volume: with the cap at 50%, the hardware dial at max is audibly capped.
      UI sounds are capped too: the music dips briefly under each beep, so
      music plus beep stays within the cap (see
      [SETTINGS.md](SETTINGS.md#volume-cap)). The dip should not be jarring.
- [ ] Button sounds: with "Button sounds" off on the settings page and after a
      sync, pressing buttons is silent and the music does not dip. Back on,
      the beep and the dip return.
- [ ] The device timezone is the family's local zone (`timedatectl`), not UTC.
      Bedtime is enforced in device-local time.
- [ ] Bedtime: it enters on schedule and fades out rather than cutting off.
      The sleep screen shows, and wake time restores normal use. No button
      does anything on the sleep screen, Escape on a keyboard included. With
      the Spanish profile, a 01:00 wake time reads "Hasta la 1:00" and 07:30
      reads "Hasta las 07:30" (headless frames check the wording, not how
      the dim text looks on the panel in a dark room).
- [ ] Loudness: consecutive tracks from different sources play at about the
      same level ([LOUDNESS.md](LOUDNESS.md)).
- [ ] Reboot while offline: the last-synced settings (cap, bedtime) still
      apply.
- [ ] `just device-verify <host>` shows the deployed commit matches the
      release.
- [ ] **Installer, server mode:** on a fresh SD card, clone the repo and run
      `install-device.sh --server URL --timezone <zone>`, then reboot. The
      device boots straight to a pairing code (no server picker, nothing typed),
      the code is approved from a phone, and the first sync completes.
- [ ] **Installer, standalone:** on a fresh SD card, run
      `install-device.sh --standalone --profile-name <name> --timezone <zone>`
      and reboot. The player starts fullscreen (not windowed) and the web UI
      answers.
- [ ] **Boot-partition preset:** put the server address in
      `kidsplay-server.txt` on the SD card's boot partition (from a computer,
      before first boot) and boot a device with no config and no
      `pair-server.txt`: it shows a pairing code for that server.
- [ ] **Pairing, back to the picker:** on a preset device press **B** on the
      code screen, then **A** on the "Preset: <address>" row: it shows a fresh
      code with nothing typed.
- [ ] **Fullscreen:** on a device paired on-screen, the pairing screen and the
      player fill the screen, with no title bar and nothing clipped.

## Setting up and checking a handheld

Before handing a handheld over (and after reinstalling one), and for the
changes named in each step:

1. `./install-kiosk.sh` has been run (`--timezone ZONE` for the family's
   zone); the device boots straight into the player.
2. **Device timezone is set correctly:** `ssh <device> timedatectl` shows the
   local zone (not `UTC`) and the local time matches your watch.
3. `just device-verify <host>` shows the expected code and a completed sync.
4. The profile has the intended volume cap and bedtime; after the next sync
   the volume never exceeds the cap, whatever the volume dial is set to.
5. **Language:** the screens are in the language the child should see. A
   handheld set up before language existed still shows Spanish; a new
   profile's shows English until you pick one on its settings page. Change the
   profile's language, sync, and confirm every screen switches without a
   restart (home, a list, the play view, settings, the sleep screen).
6. **Accents on the real panel:** with the Spanish profile, look at a track or
   album named with á é í ó ú ñ ü ¿ ¡ (for example "Canción de cuna ¿Dónde
   está?") on the home, list and play views and the playback bar: every
   character has its accent and none is a box. Headless rendering is tested,
   but only the real LCD shows whether the smallest text stays legible.
7. Offline test: with the server off, power the device off shortly before
   bedtime and on again a few minutes later. Expect the player to run with
   no sleep screen (restored clock, fail open) and no crash loop in
   `~/kidsplay-kiosk.log`.
8. **GPi Case 2 is unchanged** (no `input_profile`, `width` or `height` in its
   `config.json`): every button does what it did before the update (d-pad, A
   select, B back, X play/pause, Y repeat), Escape on an attached keyboard
   still quits, the shoulder buttons do nothing until the profile turns the
   volume buttons on, and the home, list, play and settings screens look as
   they did (the settings screen now shows eight color swatches in two rows).
9. **Themes on the real panel.** Set the profile's theme to `high-contrast`,
   then `night`; after the next sync confirm the device switches, the settings
   screen says "Theme chosen by your parents" and left/right no longer change
   it. In a dim room, check `night` is comfortable and `high-contrast` text is
   crisp on the LCD. Set it back to "Chosen on the device" and confirm the
   child's own color returns.
10. **Custom theme assets** ([THEMES.md](THEMES.md)): with a custom theme that
    has a background, a font and a UI sound, confirm on the device that the
    background is legible behind lists and the play screen, the font shows the
    accents (á é ñ ¿), and the sound plays through the speaker at a level under
    the volume cap. Then switch the device offline and reboot: the theme, with
    its assets, still applies.
11. **Other hardware** ([HARDWARE.md](HARDWARE.md)): on each new kind of
    handheld or screen, set `width`, `height` and `input_profile`, then press
    every button on every screen (home, each list, a photo, play, settings):
    each does what the profile says, B goes back, and nothing is cut off at
    the screen edges. Check the smallest text (list subtitles, the playback bar
    title) is legible at the panel's real pixel density: headless rendering
    cannot judge that.
12. **Web UI on a real phone** (headless Chromium checks layout and touch
   events, not the phone's own file picker or keyboard): open the web UI on
   an iPhone (Safari) and an Android phone (Chrome), in both languages.
   - The menu button opens and closes the nav; Media, Devices and Profiles
     show one card per row with every action reachable one-handed.
   - Import, Photo, Upload file: the picker offers the camera roll, lets you
     select several photos, and HEIC photos arrive as JPEG (iOS converts them
     because the input only asks for `image/*`).
   - The crop frame follows one finger and zooms with a two-finger pinch,
     while dragging from the margins beside the photo scrolls the page (the
     frame is inset on a phone and says so; a touch on the frame itself
     never scrolls, because `touch-action: none` is what lets it pinch).
     Import all picked photos in turn ("Photo 2 of 3").
   - Tab through a form with a Bluetooth keyboard: the focused field shows
     a purple ring, not only a border colour change.
   - Tapping a URL field brings up the URL keyboard, tapping number
     fields the numeric keypad, and the page does not zoom in on focus.
13. **Text size and fit on the real panel** (headless tests check that no
    label is cut short, nothing overlaps and no text is under 10 px tall, at
    240×180 to 1280×720; only the panel shows whether that is comfortable):
    with the Spanish profile, open Settings ("Contraste" is the longest theme
    name) and a play view. On a 320×240 or smaller screen, read the smallest
    text (theme names, list subtitles, the playback bar) at normal holding
    distance; if it strains, raise `MIN_FONT_PX` in `fonts.py` and re-run
    `tests/test_screens.py`.
14. **Photo grid on a wide screen:** on an 800×480 or wider panel, open a
    photo album of at least 8 photos: the grid has four columns, thumbnails
    are square and centred in their cells, and the d-pad moves across all
    four columns and down.
15. **Volume buttons and the input profile:** on a `keyboard`-profile device
    leave the profile's volume buttons at "Device default": `+`/`-` change the
    volume, never above the cap. Set them to "Off" on the settings page, sync,
    and `+`/`-` do nothing; back to "Device default" and they work again.
    On a GPi Case 2 the shoulder buttons do nothing until "On" is chosen.
    With the buttons on and the cap at 60%, hold `+` past the cap: the volume
    overlay stays up with its bar full at the orange cap mark, and the sound
    does not get louder. (Checked headless; this confirms it reads at arm's
    length on the real screen.)
16. **Bad screen size in `config.json`:** put `"width": "wide"` (and, next, a
    size under 240×180) in a scratch `config.json`: the player starts at
    640×480 and `~/kidsplay-kiosk.log` has a "Ignoring the screen size" warning.

## Pairing

For [on-device pairing](PAIRING.md); none of it can be run without the
hardware and a real network:

1. On a handheld with no `~/.kidsplay/config.json`, start the player. The
   pairing screen appears at the panel's resolution: nothing is clipped, the
   code and the QR code are readable at arm's length, and the countdown runs.
2. **Buttons:** the gamepad D-pad moves through the server list and the
   on-screen keyboard; A types, B deletes (and goes back on an empty field),
   Y switches keys, X confirms. Check that each physical button does what the
   legend at the bottom says, and that no key on the keyboard is missed or
   sticks when held.
3. **Discovery:** with the server on the same WiFi (not in Docker), a row like
   "KidsPlay · <server>:8000 · 3F2A-9C1E" appears within a few seconds, and the
   note under the question ("Pick only a server you recognize…") is readable
   and not clipped on the panel. Turn **Allow pairing new devices** off in the
   server's Settings: the row disappears within about a minute (mDNS caches
   expire); on again, it comes back. Then stop the server's advertising
   (`KIDSPLAY_MDNS=0`) and confirm typing `<server-ip>` works.
4. **QR code:** scan it with a phone camera from a normal reading distance
   (the panel's brightness and glare are the variables here); it opens the
   Devices page with the code filled in after login.
5. Approve it. The handheld says "All set!", syncs and plays; `stat -c %a
   ~/.kidsplay/config.json` shows `600`.
6. Pull the power right after approving, on a few tries: the device either
   pairs again or starts normally; it never boots into a half-written config.
   (If the handheld was switched off mid-pairing, delete the device and pair
   again; a dropped WiFi or a failed write on a running handheld recovers by
   itself.)
7. Let a code sit for 10 minutes: the screen says it expired and A gives a
   fresh code.
8. In Spanish (set `"language": "es"` in a scratch config, or view a paired
   handheld's own screens) check the long strings fit; the pairing screen of a
   device with no config shows English, its default.
9. **Server ID:** while the code shows, the panel shows "Server ID: 3F2A-9C1E…"
   under the countdown, in the panel's real font, wrapped and not clipped, and
   the same ID is on the server's Devices page.
10. **Unknown-server warning:** on a paired handheld, point `server_url` in
    `~/.kidsplay/config.json` at a second KidsPlay server (or change `server_id`
    there) and restart the player. The home screen shows the orange strip
    "Unknown server. Ask a grown-up." in both languages, fully readable on the
    LCD and clear of the tiles, nothing new syncs, and music keeps playing.
    Restore the config: the strip goes away after the next sync.

## All-in-one handhelds

For a handheld set up with `kidsplay-allinone` ([ALL_IN_ONE.md](ALL_IN_ONE.md)).
None of this can be run without the hardware, systemd and the kiosk session:

1. On a fresh Pi OS image, follow ALL_IN_ONE.md from the top, with `--lan`.
   `systemctl status kidsplay-server` is active and enabled;
   `ss -ltn | grep 8000` shows `0.0.0.0:8000` (or `127.0.0.1:8000` without
   `--lan`).
2. From a phone on the same WiFi, open `http://<device>:8000`, log in and
   import an album. Reboot; the album is on the device after the first sync
   and plays.
3. `stat -c '%h %i' ~/.kidsplay/media/audio/*/* | head` shows `2` links, and the
   same inode as the file under `~/.local/share/kidsplay/media/`; `df` shows
   the SD card did not grow by the size of the album twice.
4. `systemctl list-timers | grep kidsplay-timeset` shows nothing, and the boot
   does not wait on the network or the server.
5. Cold boot with the server slow to start: the player shows its empty
   library, then the media appears within about a minute of the server coming
   up, not 15 minutes later.
6. Import a long audiobook while music plays. Playback does not stutter,
   `ps -o ni,comm -C ffmpeg` shows niceness 10, and only one ffmpeg runs at a
   time. **Note how long the import took and put the numbers in
   [ALL_IN_ONE.md](ALL_IN_ONE.md#expected-ingest-times)**: the figures there
   are from an x86 machine, not a CM4.
7. Restored-clock check: power off shortly before bedtime and on again a few
   minutes later with no NTP. Expect no sleep screen (clock untrusted), as in
   the separate-server case, and that a sync does not change that.
   Then repeat with the network up: power off shortly before bedtime and on
   again a few minutes later with the WiFi available. Once
   `timedatectl show -p NTPSynchronized` says `yes` (usually within a minute of
   the WiFi joining), `~/kidsplay-kiosk.log` shows `System clock is
   NTP-synchronized` and the sleep screen appears at the next once-a-second
   check, with no player restart. Boot with the router off: the kiosk still
   reaches the home screen within its usual boot time (it never waits for NTP).
8. Remove an album from the profile, wait for the sync: its files vanish from
   `~/.kidsplay/media/`; the copies in `~/.local/share/kidsplay/media/` remain.
9. **Import limits, end to end** ([LOUDNESS.md](LOUDNESS.md#normalization-runs-in-the-background)).
   Upload a long audiobook while music plays: the upload returns within seconds,
   the item shows "Normalizing…" in the web UI, and playback does not stutter.
   While it normalizes, `ps -o ni,args -C ffmpeg,yt-dlp` shows one heavy job at
   a time (import a YouTube link meanwhile: its yt-dlp/ffmpeg waits for the
   normalization, or the reverse), all at niceness 10. Restart the server
   (`sudo systemctl restart kidsplay-server`) mid-normalization: the ffmpeg
   stops within a couple of seconds and the item resumes normalizing afterwards.
