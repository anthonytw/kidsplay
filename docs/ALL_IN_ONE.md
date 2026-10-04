# All-in-one mode: server and player on one device

For families without a home server. The KidsPlay server and the player run on
the same Raspberry Pi (or any Linux machine). A parent imports media in the web
UI from a phone or laptop; the player on the device plays it.

The separate-server setup stays the default and is better if you have a NAS or
always-on machine: the handheld's SD card only holds what it plays, and imports
do not compete with playback for the CPU. Use all-in-one when you do not.

## What changes

| | Separate server (default) | All-in-one |
|---|---|---|
| Media on the SD card | Server store on the server; a copy on each device | **One copy**: the player's files are hard links to the server's store |
| How the player gets files | HTTP download | Hard link (copy if on another filesystem) |
| Manifest, diffing, settings, bedtime | as usual | identical: still fetched over HTTP from `127.0.0.1` |
| Clock bootstrap (`kidsplay-timeset`) | Sets the clock from the server's `Date` header | **Not installed**: the server's clock is the device's own |
| ffmpeg, yt-dlp | Full speed | `nice`, one job at a time (a yt-dlp download, its ffmpeg included, counts as one job) |

Everything else (profiles, per-profile settings, loudness normalization,
importers, backups) works as documented elsewhere.

## Set up, from scratch

You need a Raspberry Pi OS (or other Debian-based) machine with network access
for the install, `git`, [uv](https://docs.astral.sh/uv/) and ffmpeg.

```bash
sudo apt-get update && sudo apt-get install -y git ffmpeg
git clone https://github.com/anthonytw/kidsplay ~/kidsplay
cd ~/kidsplay

# One command: packages, server data, admin, profile, this device, config,
# systemd unit, and the console-boot kiosk.
packages/kidsplay-device/deploy/install-device.sh \
  --standalone --profile-name "Alice" --lan --timezone America/New_York
sudo reboot
```

`install-device.sh --standalone` runs `uv sync --all-packages --locked`, checks
that ffmpeg is installed, then runs `kidsplay-allinone` and finally the kiosk
installer. The player starts fullscreen. Pass your time zone: bedtime is
enforced in the system's local time. (`kidsplay-allinone` and `install-kiosk.sh`
still work on their own if you want the steps apart.)

`kidsplay-allinone` asks for the admin password (at least 8 characters), then:

1. creates the server database and media store in `~/.local/share/kidsplay/`;
2. creates the profile and registers this device on it;
3. writes `~/.kidsplay/config.json` with the local transport and
   `"fullscreen": true` (mode `0600`, it holds the device's API key; a re-run
   keeps a `fullscreen` you changed);
4. installs `kidsplay-server.service` (using `sudo`), enables it and starts it.

It is safe to run again: the profile and device are reused, the admin password
is never changed, and it refuses to replace a config that points at another
server unless you pass `--force`.

`--lan` makes the web UI reachable from other devices on your network
(`http://<device-address>:8000`). Without it the server only listens on
`localhost`. The web UI is plain HTTP, so only use `--lan` on a network you
trust, and choose a real admin password. The installer also takes `--port` and
`--device-name`; `kidsplay-allinone --help` lists the rest (`--data-dir`,
`--media-root`, `--no-systemd`).

Notes: re-running without `--lan` reverts the server to localhost-only. Do not
point an all-in-one box's `sync_transport` at `http://127.0.0.1`: a localhost
HTTP sync anchors the bedtime clock to the device's own clock. Give the admin
password through `KIDSPLAY_ADMIN_PASSWORD` or the prompt, never a flag: the
installer rejects `--admin-password`, which is visible in `ps`.

The kiosk installer sees `"sync_transport": "local"` in the config and does not
install the clock bootstrap (see [Clock](#clock-and-bedtime)).

### Import media and play it

1. Open the web UI, log in, and choose **Media** to import music, audiobooks
   or photos. Assign them to the profile.
2. The player syncs at startup and then every 15 minutes (the server's sync
   interval; see [SETTINGS.md](SETTINGS.md)). To pick up something you just
   imported, restart the player: `pkill -f kidsplay-player` (the kiosk
   restarts it within seconds).

If the player starts before the server is ready (normal at boot), it retries
every 30 seconds until the first sync works, rather than waiting 15 minutes.

## How the local transport works

The device config has two extra keys (`"sync_transport": "http"` is the
default and needs neither):

```json
{
  "sync_transport": "local",
  "server_media_store": "/home/pi/.local/share/kidsplay/media"
}
```

The player fetches the manifest over HTTP as always and diffs it against its
database. For each new file it calls `os.link` from the server's store into
`media_root`, which takes no space. If linking fails (another filesystem, or one
without hard links such as FAT/exFAT) it copies the file, checks its SHA-256 and
moves it into place, and the file then takes space twice. Keep `--data-dir` and
`--media-root` on the same filesystem (both under the home directory by
default); `kidsplay-allinone` warns if they are not.

A file must have the size the manifest says, so a file the server is still
writing is never linked. `media_root` and the server's store must be separate
directories, neither inside the other; the config is rejected otherwise,
because the player deletes files in `media_root` that are not in its manifest.

### Server files are never modified

A hard link **is** the server's file under a second name, and the server's
backups rely on media-store files never changing. So the player:

- never opens a linked file or a store file for writing;
- removes files only with `unlink`, which drops the player's name and leaves
  the server's file, inode and content as they were (tests check the inode
  and the hash);
- writes its own downloads and copies to a new file and renames it into place.

What this means when you delete things:

- **Remove media from a profile, or delete it, then sync:** the player unlinks
  its names. The server's files are untouched.
- **Deleting media on the server** removes it from the database only; the
  server keeps the stored files, so no disk space is freed until you clean
  the store yourself.
- **Restoring a backup** (`kidsplay-server restore`) keeps existing store
  files and adds missing ones with a rename, so it does not change what the
  player has linked.

## Resource budget

A Raspberry Pi CM4 is slow, and the player must keep playing audio while a
parent imports an album. The unit therefore runs the server at `Nice=5` with
best-effort IO priority 7, and sets these (also usable on any server):

| Variable | Default | All-in-one unit | Effect |
|---|---|---|---|
| `KIDSPLAY_PROCESSING_NICE` | `0` (unchanged) | `5` | ffmpeg runs at this extra niceness (0-19), so 10 in total with the server's `Nice=5` |
| `KIDSPLAY_PROCESSING_JOBS` | `0` (no limit) | `1` | At most this many ffmpeg processes at once across ingest requests, the import queue (imports, yt-dlp, loudness normalization) |

Loudness normalization does not run inside the ingest request: an upload stores
the file and returns, and the import queue's worker normalizes it afterwards,
one file at a time (see [LOUDNESS.md](LOUDNESS.md#normalization-runs-in-the-background)).
The web UI shows "Normalizing…" until it is done, and the work resumes after a
restart or power cut. Set `KIDSPLAY_LOUDNORM=disabled` in the unit to skip
normalization altogether.

### Expected ingest times

Normalizing runs ffmpeg three times per audio file (measure, normalize,
verify; a fourth time when the peaks need limiting, see
[LOUDNESS.md](LOUDNESS.md#ingest-time-cost)). Measured on a 4-core x86 machine,
with `nice` 10 and one job at a time, importing the demo library (7 MP3s, 242 s
of audio in total): **11.3 s, about 21 times realtime** (this was measured when
the import waited for it; now the import returns at once and the same work runs
in the background). The demo's 6 photos (resizing and thumbnails, no ffmpeg) took
under a second in total.

**These have not been measured on a CM4.** As a planning estimate only, expect
a CM4 to be several times slower than the machine above, so budget on the order
of one minute of background normalization per few minutes of audio, and much
longer for a multi-hour audiobook (which no longer keeps the upload waiting).
Timing an import on a handheld is on the
[handheld checklist](RELEASE_CHECKLIST.md#all-in-one-handhelds); please replace this
estimate with that measurement.

## Clock and bedtime

`kidsplay-timeset` sets the clock from the server's HTTP `Date` header. With the
server on the same machine that header is the device's own clock, so it cannot
correct anything and is not installed (it is also removed if an earlier install
left it, and does nothing if it runs against a local config).

For the same reason a sync does not count as confirmation of the time for
bedtime. Bedtime still trusts a clock that has run since the player started, and
still detects a clock **restored from the last shutdown** (see
[SETTINGS.md](SETTINGS.md)). A Pi without a battery-backed RTC restores its
clock like that on a cold boot until NTP corrects it, which usually happens
after the player has started. In separate-server mode the first sync then
confirms the time; in all-in-one mode nothing does, so the clock stays
untrusted for that whole boot and **bedtime is not enforced** (the player fails
open rather than lock a child out at the wrong time).

The player also treats a clock as confirmed once systemd reports it
NTP-synchronized (`timedatectl show -p NTPSynchronized` says `yes`). It checks
every 30 seconds in the background, so with a network (WiFi joining, then
`systemd-timesyncd` syncing, usually within a minute of boot) bedtime starts
being enforced on its own, without restarting the player. It deliberately does
**not** hold the boot for NTP (no `After=time-sync.target` on the kiosk):
with no route to an NTP peer that wait never ends and the toy would sit on a
black screen. So with no network the clock stays untrusted and bedtime fails
open, as above.

**NTP anchoring trusts the NTP server.** `NTPSynchronized` only says the
device agrees with the server it syncs to, not that the server is right. A
router that serves its own clock over NTP (OpenWrt's busybox `ntpd -l`, which
answers at stratum 10 without an upstream) hands out its *restored*, post-outage
clock after a power cut, and the device would then call a wrong time
synchronized and enforce bedtime against it. Point `systemd-timesyncd` at
upstream or public servers (`NTP=` in `/etc/systemd/timesyncd.conf`, for example
`pool.ntp.org`) and not at the router. Then a router that is itself unsynced
cannot mislead the player; with no route to those servers the clock simply stays
untrusted and bedtime fails open, as above.

For bedtime to be enforced from the first second, give the device a clock that
is right when the player starts: an RTC module.

## Backups

Back up the server data as usual (`kidsplay-server backup`, see
[BACKUP.md](BACKUP.md)). The player's `media_root` is derived from the server's
store and re-created by the next sync, so it needs no backup.

## Going back to a separate server

Point the player at the other server with a config from that server (`kidsplay
device setup`, see the README) and remove `sync_transport` and
`server_media_store`. Then `sudo systemctl disable --now kidsplay-server` if you
no longer want the local one, and run the kiosk installer again to install the
clock bootstrap.

## Troubleshooting

- **`journalctl -u kidsplay-server`** and `~/.local/share/kidsplay/server.log`
  for the server; `~/kidsplay-kiosk.log` for the player.
- **Player shows nothing after import:** wait for the next sync or restart the
  player (above). `Sync cycle failed` in the log means the server is not up
  yet or the config is wrong.
- **Storage doubles anyway:** `stat -c '%h %i %n'` on a file in each directory:
  hard links share the inode number and show `2` links. Different inodes mean
  the files were copied, usually because the two directories are on different
  filesystems (`df ~/.local/share/kidsplay ~/.kidsplay`).
- **`media_root and server_media_store must be separate directories`:** move
  `media_root` out of the server data directory.
