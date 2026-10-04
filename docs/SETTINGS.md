# Settings and parental controls

KidsPlay has two kinds of runtime settings:

- **Profile settings**, per child: the volume cap, in-app volume buttons, button sounds and
  bedtime. They travel to every device linked to the profile in the sync
  manifest and are kept on the device, so they apply offline and from the
  first frame after boot.
- **Server settings**: the device sync interval, the WebP quality of new
  thumbnails and photos, whether devices may pair, and the loudness targets.
  Edited on the web UI's **Settings** page; an
  environment variable, when set, wins and locks the value.

## Profile settings

Edit them on the web UI (**Profiles → Settings**), with the CLI, or through
`PUT /api/v1/profiles/{id}/settings` (see `docs/API.md`):

```bash
uv run kidsplay profile settings <profile-id>                # show
uv run kidsplay profile settings <profile-id> --max-volume 60
uv run kidsplay profile settings <profile-id> \
    --bedtime-mode sleep_screen \
    --bedtime weekdays=20:00-07:00 --bedtime weekend=21:00-08:30
uv run kidsplay profile settings <profile-id> --no-bedtime sat
uv run kidsplay profile settings <profile-id> --language es   # the device's screens
```

| Field | Default | Meaning |
|---|---|---|
| `max_volume` | `100` | Loudest the player may play, 0-100 %. |
| `volume_buttons` | device default | In-app volume up/down, for hardware without a volume dial. On, off, or "device default": follow the input profile. |
| `ui_sounds` | `true` | Button sounds: the short beep when a button is pressed. Off is silent and the music does not dip. |
| `bedtime_mode` | `off` | `off`, `audiobooks_only` or `sleep_screen`. |
| `bedtime_schedule` | `{}` | Per weekday (`mon` ... `sun`): `bedtime` and `wake`, local time. |
| `language` | unset (new profiles: `en`) | `en` or `es`: the language of the child's device screens. Unset shows Spanish, see [Language](#language). |
| `theme` | unset | Id of a theme (`default`, `night`, a custom one ...). Unset: the child picks a color on the device. See [THEMES.md](THEMES.md). |

The model is `kidsplay_models.ProfileSettings`. Every field has a default and
unknown fields are ignored, so an older device keeps working when a newer
server adds a field. Later per-profile settings belong in the same model rather
than a mechanism of their own. (Input profiles are not per child: they describe
the *hardware*, so they live in each device's `config.json`, see
[HARDWARE.md](HARDWARE.md).)

A change reaches a device at its next sync (the settings are part of the
manifest hash). The device stores them in its local database
(`sync_state` key `profile_settings`) and applies them on the next frame.

### Language

The child's device screens are shown in the profile's `language` (choose it on
the profile's settings page or with `kidsplay profile settings --language`; the
API accepts `en` or `es`). It is the *child's*
language: the web UI and the CLI use their own, chosen by the person using
them (see [TRANSLATING.md](TRANSLATING.md)).

- **Existing installs keep Spanish.** The device UI was Spanish-only before
  this setting existed, so a profile that has no stored `language` is shown in
  Spanish on the device. Nothing changes until someone picks a language.
  New profiles are created with `en`.
- A device that has never synced has no profile language yet, so it shows
  Spanish until its first sync delivers the profile's language.
- `PUT /profiles/{id}/settings` replaces the settings, but a body without a
  `language` key keeps the stored language; only an explicit
  `"language": null` clears it (see [API.md](API.md)).
- A device can override the profile with `"language": "es"` (or `"en"`) in its
  local `~/.kidsplay/config.json`; the override wins over the profile.
- The order is: `config.json` override, then the profile's setting, then
  Spanish if the profile never had one, then English if the profile names a
  language this build does not know (say, a newer server's).
- A change reaches the device at its next sync and switches the screens on the
  next frame, no restart.

### Volume cap

The player sets `pygame.mixer.music.set_volume(max_volume / 100)` on every
track load and whenever the settings change; UI sounds play at 0.50 of that.
This is digital attenuation *before* the OS mixer and any hardware volume
dial (such as the GPi Case 2's), which only multiply it further down. So the
dial still works, but only between silence and the cap: the loudest possible
output is capped whatever the kid does with it. No in-app control is needed
for the cap to be effective.

The music stream is capped at exactly `max_volume`. A UI sound is a second
stream, and SDL sums the two, so the player ducks the music while a UI sound
plays: the music drops to `max_volume x 0.50` for the length of the clip (plus
about 0.1 s for the output latency) and returns to `max_volume` afterwards,
so music plus beep stays within `max_volume` and never clips, even at 100%.
The beep keeps its level (`max_volume x 0.50`); it is the music that dips.
Overlapping beeps extend one duck rather than stacking, and the duck composes
with the volume buttons, synced cap changes and the bedtime fade (the fade is
never raised by a duck). Nothing is ducked when no music is playing.
The tests pin the bound on the real output (a peak above the cap, or clipping
at a 100% cap, fails them) and check that the beep is audible in it.

The cap is tested on the real output, not just on the `set_volume` calls:
`packages/kidsplay-device/tests/test_audio_output.py` runs the player on SDL's
`disk` audio driver (`SDL_AUDIODRIVER=disk`), plays a full-scale tone and
measures the PCM that comes out, so no audio device is needed.

### In-app volume buttons

`volume_buttons` is on, off or undecided ("Device default" on the settings
page, the default). Undecided follows the device's input profile: on for one
with no volume dial (`keyboard`), off for the rest (`gpi2`). A parent's explicit
on or off always wins, so switching them off for a keyboard device from the
server works; the volume cap applies either way. When the buttons are on, keyboard `+`/`-` and the gamepad
shoulder buttons (joystick buttons 4 and 5) step the volume by 10 points,
clamped to `0..max_volume`, and the level is remembered across reboots in
`~/.kidsplay/settings.json`. With it off, no input changes the volume. Which
key or button does it is set by the device's input profile
([HARDWARE.md](HARDWARE.md)); a profile for hardware with no volume dial (the
`keyboard` profile) turns the buttons on by default. The GPi Case 2's `gpi2`
profile does not, so its behaviour is unchanged.

A profile saved before this option had three states stored `volume_buttons:
false` for "never chosen"; that now reads as an explicit "off". It changes
nothing on a `gpi2` device. To hand a `keyboard` device back to its default,
choose "Device default" on the settings page.

### Button sounds

`ui_sounds` (shown as "Button sounds" on the settings page, `--ui-sounds` /
`--no-ui-sounds` on the CLI) is on by default. Off, the device plays no clip
for a button press, a theme's custom UI sounds included, and does not duck the
music, since there is nothing to duck under. The device applies a change on its
UI thread right after the sync, so it takes effect without a restart. A missing
or invalid value (an older server, a damaged manifest) means on. The player has
no other UI sounds: nothing plays at startup, and music, audiobooks and
bedtime fades are not "UI sounds" and are unaffected.

### Bedtime

A weekday's window runs from its `bedtime` to the next `wake`: the next
morning when `wake` is not after `bedtime` (the usual case), the same day
when it is (a nap). A weekday without an entry has no bedtime that evening.

- `sleep_screen`: a dim sleep screen ("Bedtime" / "Hora de dormir", with the
  wake time).
  Nothing plays and every button is ignored until wake time.
- `audiobooks_only`: only the home screen, audiobooks and audiobook playback
  are available; the other home tiles show a moon. An audiobook that is
  playing when bedtime starts keeps playing.

When bedtime starts while something that is not allowed is playing, the
sound fades out over about 10 seconds while the screen dims, then stops and
the sleep screen (or the audiobook list) appears. Nothing is cut abruptly.
At wake time the device returns to the home screen.

### The clock and the timezone

Bedtime times are **local wall-clock times in the device's system
timezone**, so the device's timezone must be right: a Pi left on UTC starts
and ends bedtime hours off. `install-kiosk.sh --timezone ZONE` sets it (and
warns while the zone is UTC); see `packages/kidsplay-device/deploy/README.md`.
Times with a UTC offset (`20:00+05:00`) are rejected when settings are saved.

The handheld has no battery-backed clock (RTC). Offline it may boot with its
clock restored from the last shutdown, or with a clock that is simply wrong.
`kidsplay-timeset` corrects it from the server's `Date` header once the
server is reachable. Bedtime is enforced against the best time available:

1. After a sync since boot, the **server's time** (from the manifest
   response's `Date` header) plus the time elapsed since.
2. Otherwise the **system clock**, unless it is untrusted, in which case
   bedtime is **not enforced** (fail open) and the player logs
   `Clock untrusted` once. A wrong clock must never lock a kid out at noon.
   The system clock is untrusted when:
   - it is earlier than the last server time seen by a sync (a clock that
     went backwards past a real sync is certainly wrong), or
   - it **looks restored from the last shutdown** (below).

   Either way, once systemd reports the clock **NTP-synchronized**
   (`timedatectl show -p NTPSynchronized` says `yes`), the system clock is
   trusted: a real time source has just corrected it. The player checks in the
   background every 30 seconds until it does, so a device that gets its
   network late catches up without a restart. This is what confirms the
   time in [all-in-one mode](ALL_IN_ONE.md#clock-and-bedtime), where a sync
   with the local server cannot. With no network NTP never reports
   synchronized and bedtime fails open as described. The player and the kiosk
   deliberately do not *wait* for NTP at boot: with no route to an NTP peer
   that wait would never end and the toy would stay on a black screen.
   The anchor is only as good as the NTP server: a router serving its own
   restored clock over NTP (busybox `ntpd -l`, stratum 10) would make a wrong
   clock look synchronized, so point `timesyncd` at upstream or public NTP (see
   [ALL_IN_ONE.md](ALL_IN_ONE.md#clock-and-bedtime)).

**Restored-clock detection.** Pi OS restores the clock at boot from the last
shutdown, so a handheld switched off at 20:30 and switched on offline at noon
the next day reads 20:31: later than the last sync, yet 15 hours wrong. To
catch this the player writes a *heartbeat* (the wall clock, the kernel boot
id, and whether it trusted the clock) into its local database at startup,
every 5 minutes, and on clean shutdown. At startup, a clock that reads less
than 15 minutes after the heartbeat of a *different boot* (or before it) has
the signature of a restored clock. It is then untrusted **for the whole boot**
(a restored clock keeps lagging real time as it runs) until a sync reaches
the server and confirms the time. A player restart within the same boot keeps
whatever trust the boot had. A clock far past the heartbeat (real time
elapsed, for example set by NTP) follows the normal rules. Heartbeat writes
are atomic (SQLite) and happen minutes apart, so they do not wear the SD card.

Remaining limits, all of which fail open (bedtime not enforced) or, in the
last case, late:

- A device off for more than 15 minutes whose clock still restores close to
  the shutdown time is treated as trusted; and with no heartbeat yet (first
  run after an update) only the "earlier than the last sync" rule applies.
- After a restored-clock boot, bedtime is off until the device next reaches
  the server or NTP synchronizes the clock, however long that takes; a trip
  with neither has no bedtime.
- A clock that is wrong in some other way (drifted, or set by hand to a time
  later than the heartbeat window) cannot be detected offline and is trusted
  until the next sync corrects it.

A bedtime evaluation that fails for any reason (for example a corrupt
persisted setting) is logged once and treated as "no bedtime"; it never stops
the player.

### Unreadable settings from a newer server

The device parses profile settings separately from the media manifest. If
they fail validation (a `bedtime_mode`, weekday or value this device does not
know) or carry a `version` newer than the device supports, the device keeps
its last good settings, logs a warning and still syncs media as usual. It
does not record the manifest as up to date in that case, so it retries the
settings every cycle. The server stays strict: it rejects invalid input with
422. Unknown *fields* are ignored as before.

## Manual handheld checklist

Moved to [RELEASE_CHECKLIST.md](RELEASE_CHECKLIST.md), the single list of
everything that can only be checked on a real handheld (release steps, setup,
language, themes, hardware, pairing, all-in-one). Add hardware-only steps there.

## Server settings

| Setting | Env variable | Default | Effect |
|---|---|---|---|
| Device sync interval | `KIDSPLAY_SYNC_INTERVAL_SECONDS` | `900` (60-86400) | Sent to devices in the manifest **only once it has been saved or pinned**; each device uses it from its next sync on. Until then (and after a reset) each device uses `sync_interval_seconds` from its own `config.json` (default 900), so the server's default never overrides a device you configured by hand. When both are set, the server's value wins. |
| WebP quality | `KIDSPLAY_WEBP_QUALITY` | `85` (1-100) | Quality of thumbnails and photos processed from now on. Existing files are never re-encoded. |
| Allow pairing new devices | `KIDSPLAY_PAIRING_ENABLED` | `true` | Whether handhelds may pair themselves with a code ([PAIRING.md](PAIRING.md)). Off refuses new and unfinished pairings. |
| Loudness target | `KIDSPLAY_LOUDNESS_TARGET_LUFS` | `-16` (-70 to -5) | Target for audio processed from now on. Changing it on the page offers to normalize the library ([LOUDNESS.md](LOUDNESS.md)). |
| Loudness target for music / audiobooks | `KIDSPLAY_LOUDNESS_TARGET_LUFS_MUSIC` / `_AUDIOBOOK` | unset (-70 to -5) | Overrides the overall target for that type; empty means the overall target. |

Values saved on the Settings page are stored in the `server_settings` table
and take effect without a restart. A set environment variable wins: the page
shows the setting locked and the API refuses to change it (409). An invalid
environment value stops the server at startup. `PUT /api/v1/server-settings`
rejects a key it does not know (422).

This is the one mechanism for settings that change at runtime. Deployment
choices that are read once at startup or per job stay environment-only:
authentication (`KIDSPLAY_AUTH`, ...), paths, `KIDSPLAY_PROCESSING_*`, and the
loudness switches `KIDSPLAY_LOUDNORM`, `KIDSPLAY_LOUDNORM_LIMITING` and
`KIDSPLAY_LOUDNESS_TRUE_PEAK_DBTP`.
