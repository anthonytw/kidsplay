# Loudness normalization

Every song and audiobook is normalized to the same perceived loudness (EBU
R128) after ingest, so a quiet track and a loud one play at about the same level
at the same volume setting.

It happens on the server because the device can only attenuate
(`pygame.mixer.music.set_volume` is at most 1.0) and cannot lift a quiet track
without clipping, and because all processing is server-side.

## Settings

The **loudness targets** are server settings: change them on the web UI's
**Settings** page (or with `PUT /api/v1/server-settings`; see
[SETTINGS.md](SETTINGS.md), the one place that describes how server settings
work). They take effect for the next file, without a restart. A target whose
environment variable is set is shown locked on the page: the environment wins.

| Setting (API key) | Env variable | Default | Description |
|---|---|---|---|
| Loudness target (`loudness_target_lufs`) | `KIDSPLAY_LOUDNESS_TARGET_LUFS` | `-16` | Integrated-loudness target, -70 to -5 LUFS. |
| Loudness target for music (`loudness_target_lufs_music`) | `KIDSPLAY_LOUDNESS_TARGET_LUFS_MUSIC` | unset | Target for music; empty means the overall target. |
| Loudness target for audiobooks (`loudness_target_lufs_audiobook`) | `KIDSPLAY_LOUDNESS_TARGET_LUFS_AUDIOBOOK` | unset | Target for audiobooks (e.g. `-14` for louder speech); empty means the overall target. |

Saving a changed target on the page offers to **normalize the library now**:
that is the backfill below, queued from the page. Items already at their
type's target are skipped; the others are re-normalized from their kept
originals. Choosing "Not now" keeps the new target for new uploads only; run
`kidsplay media normalize --all` (or use the Media page's button) later.

Three more variables are deployment switches. They are read from the
environment only, each time a file is normalized, and are not on the page:

| Variable | Default | Description |
|---|---|---|
| `KIDSPLAY_LOUDNORM` | `enabled` | `disabled` queues no normalization for uploads (items stay "not normalized"). |
| `KIDSPLAY_LOUDNESS_TRUE_PEAK_DBTP` | `-1.5` | True-peak ceiling, -9 to 0 dBTP. |
| `KIDSPLAY_LOUDNORM_LIMITING` | `allowed` | `never` keeps the gain below the target instead of limiting loud peaks (see [Limited and capped items](#limited-and-capped-items)). |

A value that is not a number, or is out of range, stops the server at startup.
After changing a target, or an item's media type to one with a different
target, run the backfill to bring the existing library to it.

## Normalization runs in the background

Ingest does not wait for ffmpeg. An upload (or an importer's download) stores
the original file, writes the item's rows, and puts a **loudness job** for the
item on the import queue, all in one transaction; then it returns. The item
plays right away, un-normalized ("Normalizing…" in the web UI). The queue worker
then normalizes it. A 10-hour audiobook therefore costs the same request time as
a 3-minute song, and proxies and browsers have nothing to time out.

The jobs live on the same queue as imports (`import_queue`, importer name
`loudness`), so:

- **One at a time.** The worker processes one job after another, so at most one
  normalization runs however many files were uploaded, and never beside an
  import's own ffmpeg or yt-dlp (`KIDSPLAY_PROCESSING_JOBS`, the job limit,
  covers yt-dlp too). Imports go before normalization, so a link added during a
  library backfill starts at once.
- **Resumes after a restart.** Jobs are database rows. A job that was running
  when the server stopped (or crashed) goes back to pending at the next start.
- **Stops quickly at shutdown.** The running ffmpeg is killed and its job put
  back to pending without using up an attempt, so a restart does not sit
  through a long encode.
- **Retries.** A failed job (ffmpeg missing or failing) is tried twice, then
  marked failed; the item stays playable and "not normalized", and appears in
  the errors of `kidsplay media normalize --status`. The next
  `kidsplay media normalize` queues it again.

The jobs are not listed in `GET /queue` or on the queue page (they are not
imports); their progress is `GET /media/normalize`. Finished jobs are deleted
after a week.

When a job finishes, the item's `audio` row is repointed at the new file in one
transaction (see [Files](#files)), so the next device sync picks it up: the
device downloads the normalized file and deletes the un-normalized one it got
before. A device that syncs between the upload and the job plays the original
until then.

## How it works

`kidsplay_server.processing.audio.normalize_loudness` runs ffmpeg:

1. **Measure** the source with `loudnorm` (pass 1, `print_format=json`):
   integrated loudness, true peak, loudness range and threshold. Audio that is
   silent or below the -70 LUFS gate cannot be normalized; it is stored
   unchanged and marked as processed for the target.
2. **Encode** with `loudnorm` pass 2, fed the measured values, with
   `linear=true`. When the gain fits under the ceiling (measured true peak +
   gain ≤ ceiling), loudnorm applies one constant gain, so dynamics are
   untouched. Only when it does not fit does loudnorm fall back to dynamic
   mode (gain plus a true-peak limiter). The loudness-range target is the
   source's own range (at least 7 LU), so the fallback limits peaks without
   also compressing the music. The output is MP3 (libmp3lame VBR `-q:a 2`) at
   the source's sample rate where MP3 supports it (Opus, for which the tags
   carry no rate, is probed with `ffprobe`: 48 kHz stays 48 kHz).
3. **Check the ceiling** on the encoded file: its true peak (sample peak after
   oversampling to 192 kHz in floating point, which agrees with loudnorm's
   own measurement to 0.01 dB). loudnorm's true-peak limiter is approximate and
   MP3 encoding adds inter-sample overshoot of its own, so a loud master can
   come out a few tenths of a dB over. If it did, the encode is repeated from
   the same source with a plain gain reduction after loudnorm (`volume`) of the
   overshoot plus a margin (0.1 dB, doubling with each retry). Encoder overshoot
   follows the level, so this converges, which lowering loudnorm's own ceiling
   did not. Up to four attempts are made; if the peak is still over, normalization
   fails and the original is stored unchanged. The stored file therefore never
   exceeds the ceiling.

   Such an item ends a fraction of a dB (up to about a dB for a loud, clipped master) **under** its
   target. The stored values are what was actually produced: `loudness_gain_db`
   is the real output minus source loudness (lower by the reduction), and
   `loudness_mode` stays `dynamic`/`linear`, as loudnorm used it. The
   `capped` mode is not used for this: it means "limiting forbidden", and the
   backfill redoes a `capped` item once limiting is allowed. When an item needs
   more than one attempt, the per-attempt true peaks and the reduction are
   logged at INFO; a failure after the last attempt logs them too.

What the web UI (media details) and the API (`MediaItem`) show:

| Field | Meaning |
|---|---|
| `loudness_source_lufs`, `loudness_source_true_peak_dbtp` | Measured on the original. |
| `loudness_gain_db` | Output minus source loudness. `null` if the audio was kept unchanged. |
| `loudness_mode` | `linear` (one constant gain), `dynamic` (gain plus a true-peak limiter) or `capped` (a constant gain held below the target). `null` if unchanged, or normalized before the mode was recorded. |
| `loudness_target_lufs`, `loudness_target_true_peak_dbtp` | Target the item was processed for. `null` means not normalized yet. |

### Limited and capped items

A plain gain to the target is not always possible: a quiet lullaby with a few
loud peaks would have those peaks pushed over the ceiling. loudnorm then has
two ways out, and the item records which one was used (`loudness_mode`, shown
in the media details and `kidsplay media show`):

- **`dynamic` (the default):** loudnorm applies the gain and limits the peaks
  that would clip. The item reaches the target loudness, but its loud moments
  are squashed by however many dB the limiter takes off, which can be several dB
  for quiet, dynamic material (lullabies, classical). The UI says "loud peaks
  limited".
- **`capped` (`KIDSPLAY_LOUDNORM_LIMITING=never`):** the gain is held at what
  fits under the ceiling, so nothing is limited or reshaped, and the item ends
  up quieter than the target. The UI shows the level it reached: "-30.0 LUFS →
  -21.0 LUFS (+9.0 dB, kept below the -16 LUFS target to avoid limiting)". The
  device's volume control can make it up, and a capped item counts as
  up to date while limiting stays forbidden (its stored target is still the
  configured one), so the backfill does not redo it.

The setting applies to items normalized from then on. Switching from allowed
to `never` does not turn existing `dynamic` items into `capped` ones: the
backfill skips items already at their target, so to do that, change the target
or the ceiling (which makes the backfill redo everything), or re-import the
item. The other direction is automatic: once limiting is allowed again, a
backfill normalizes the `capped` items again (from their original audio), and
they are then up to date like any other.

### Files

An ingested track has two files in the content-addressed store:

- `audio_source`: the original, byte for byte. It stays on the server and is
  never in a device's sync manifest.
- `audio`: the normalized MP3, which devices download.

The media item's `content_hash` (the deduplication key) is still the hash of
the original, so re-importing the same file is still skipped. The processed
file's hash changes, and sync handles that like any replaced file: the device
downloads the new file and deletes the old one.

**Why keep originals:** re-normalizing (after a target change, or a better
encoder) always starts from the original, so there is no generational loss
from re-encoding an MP3 made from an MP3. The cost is disk space: each track
is stored about twice (the original plus a normalized MP3). For a family
library that is small next to the media store, and it keeps "the original is
re-fetchable" true for local files, which cannot be re-fetched.

### Without ffmpeg

Ingest still works without ffmpeg (plain MP3 imports rely on this): the original
is stored as the `audio` file, a warning is logged, no job is queued, and the
item stays "not normalized". Install ffmpeg and run the backfill to normalize
those items later. A normalization that fails (e.g. a file ffmpeg cannot decode)
is retried, then marked failed, and the item stays "not normalized" the same
way. With `KIDSPLAY_LOUDNORM=disabled`, no job is queued for an upload.

## Backfill

```bash
kidsplay media normalize --all            # whole library, in the background
kidsplay media normalize <id> [<id>...]   # selected items
kidsplay media normalize --status         # progress of the current/last run
kidsplay media normalize --all --wait     # start and wait until it finishes
```

The web UI's media library has a **Normalize loudness** button that starts the
same run. The API is `POST /api/v1/media/normalize` and
`GET /api/v1/media/normalize` (see [API.md](API.md)).

The backfill runs in a background task on the server, one item at a time.
Items already processed for their type's current target and ceiling are
skipped without being decoded, so running it twice re-encodes nothing.
Otherwise it normalizes from the original (the `audio_source` file, or the
`audio` file of an item that was never normalized) and then, in one database
transaction, repoints the item's `audio` row at the new file (a
never-normalized item's `audio` row becomes its `audio_source`) and updates
its loudness fields.

The backfill never rewrites or deletes a store file, because backups rely on
stored files never changing. It writes each normalized output as a new file.
A superseded normalized file (after a target change) stays in the store until
you clean up (see [Cleaning up superseded files](#cleaning-up-superseded-files)).

The backfill only queues the jobs (one per item, all with the same creation
time, which is how their progress is grouped) and returns; the queue worker runs
them as described above. A request made while uploads are still normalizing is
fine: items with an unfinished job are not queued twice, and the progress
counts them all. Progress is read from the job rows, so it survives a restart,
and each finished item is committed on its own. `errors` in the status lists at
most 20 failures (`errors_omitted` counts the rest); the CLI prints ten and
summarizes the remainder.

## Cleaning up superseded files

Changing a target, or a re-normalization, leaves the previous normalized MP3 in
the store with nothing pointing at it; deleting an item does too. `kidsplay-server
gc` removes such files:

```bash
kidsplay-server gc --dry-run      # what would go
kidsplay-server gc                # run it (e.g. weekly, from cron or a timer)
kidsplay-server gc --grace-days 14
```

It deletes a file only after it has been unreferenced for the **grace period**
(seven days by default), as seen by its own runs: the first run finding a file
unreferenced only records it, and starts its clock; a later run at least one
grace period on deletes it, after checking again under the database write lock
that no row points to it. A file that is referenced again is forgotten. It only
touches `audio/`, `thumbnails/` and `photos/`.

**Backups.** [`kidsplay-server backup`](BACKUP.md) copies the database first and
then the files it references, and relies on those files not vanishing. A file
that is in a backup's database copy became unreferenced no earlier than the
backup started, so the grace period guarantees it is still there for any backup
that finishes within it. Keep the grace period longer than your longest backup
(the default is a week). One more thing to avoid: run `gc` while no import is
in progress. An ingest that stores a file byte-identical to one that is
pending deletion, in the moments around that file's deletion, could end up
pointing at a file that was just removed. It cannot happen while nothing is
being imported.

## Ingest-time cost

Normalization decodes each file three times (measure, encode, peak check) and
encodes it once as MP3. **Items that need limiting (`dynamic` mode) usually cost
one more decode and encode:** when the MP3 encoder overshoots the ceiling after
loudnorm's limiter, the encode is repeated with a gain reduction of the
overshoot (up to four attempts in all; loud, clipped masters can need a
third), so such an item is decoded five times and encoded twice (both dynamic
items in the review needed the second encode).
`capped` items are steadier, but can be re-encoded by the same rule. Since this
happens in the background now, the cost is time on the worker, not an open
request. Measured with `normalize_loudness` on a 4-vCPU
Intel Xeon at 2.1 GHz (ffmpeg 6.1, one core per file):

| Input | Time | Speed |
|---|---|---|
| Demo samples: 7 MP3s, 22.05 kHz mono, 4 min in total | 8.7 s | ~28x realtime |
| 3-minute song: 44.1 kHz stereo MP3, 192 kbps | 12.3 s | ~15x realtime |

Ingesting an MP3 itself only copies it (well under a second); the demo's seed
waits for the queue to finish the normalization, so `just demo` still takes about
9 s to seed. Most of the time is loudnorm's
measurement pass (6.6 s of the 12.3 s), which resamples to 192 kHz to measure
the true peak.

**Raspberry Pi (all-in-one mode).** Not measured on hardware. A CM4
(Cortex-A72, 1.5 GHz) is typically 3 to 5 times slower than the machine above
for this kind of single-threaded ffmpeg work, so expect about 3 to 5x realtime:
roughly 40 to 60 seconds per 3-minute song, and 15 to 20 minutes for a
one-hour audiobook. The upload does not wait for it, but the Pi's CPU is
also busy playing, and the job limit and `nice` of the all-in-one unit keep the
encode from starving playback. To keep the CPU free altogether, use
`KIDSPLAY_LOUDNORM=disabled` and run `kidsplay media normalize --all` when the
device is idle (e.g. overnight). To measure your own hardware:

```bash
time ffmpeg -nostdin -i song.mp3 -af loudnorm=print_format=json -f null -
```

and multiply by about two for the whole normalization.
