# Themes

A theme is the player's look: colors, and optionally a font, UI sounds and
background images. Each child's profile can choose one; the choice reaches the
device in the sync manifest, and everything a theme needs is synced to the
device, so it works offline.

## Choosing a theme

On the web UI open **Profiles → Settings → Theme**, pick one and save. The
device shows it after its next sync (within the sync interval), with no
restart. Through the API it is the `theme` field of the profile's settings
(`PUT /api/v1/profiles/{id}/settings`, see [API.md](API.md)).

Built-in themes (always available, in `kidsplay_models.themes`):

| Id | Look |
|---|---|
| `default` | Blue: the original look, exactly as before themes existed |
| `purple`, `green`, `red`, `orange`, `cyan` | The other original colors |
| `high-contrast` | Black, white and saturated yellow/cyan; every text pair is at least 7:1 (WCAG AAA) |
| `night` | Dim and warm, no bright blue, for bedtime |

**Two ways a theme is chosen.** A profile with **no theme** (the default; shown
as "Chosen on the device") keeps the old behaviour: the child picks one of the
built-in colors on the device (Settings screen, left/right), remembered on the
device, blue at first. A profile with a **theme set** overrides that: the
device shows the profile's theme and its picker is disabled, with a note
("Theme chosen by your parents"). Setting the profile back to "Chosen on the
device" gives the child their own pick back.

Existing profiles have no theme, so they look exactly as before the upgrade.

## Custom themes

A custom theme lives on the server: a name, a palette, and up to seven assets.
There is no web editor yet; use the API (admin token, see
[API.md](API.md#themes)):

```bash
T="Authorization: Bearer $KIDSPLAY_TOKEN"
S=http://server:8000/api/v1

# 1. Create (or update) the theme: id in the URL, colors are #rrggbb
curl -X PUT $S/themes/ocean -H "$T" -H 'Content-Type: application/json' -d '{
  "name": "Ocean",
  "colors": {"bg": "#06121c", "surface": "#0d2233", "surface_sel": "#164060",
             "primary": "#39b6d8", "text": "#d5e8f0", "text_dim": "#7d98a8",
             "text_bright": "#ffffff", "accent": "#ffd166", "progress_bg": "#1b3446"}}'

# 2. Add assets (each replaces the one of the same role)
curl -X PUT $S/themes/ocean/assets/background      -H "$T" -F file=@waves.jpg
curl -X PUT $S/themes/ocean/assets/home_background -H "$T" -F file=@harbour.png
curl -X PUT $S/themes/ocean/assets/font            -H "$T" -F file=@Nunito-Bold.ttf
curl -X PUT $S/themes/ocean/assets/sound_move      -H "$T" -F file=@tick.ogg

# 3. Give it to a profile (or use the selector on the profile's settings page)
curl -X PUT $S/profiles/$PROFILE/settings -H "$T" -H 'Content-Type: application/json' \
     -d '{"theme": "ocean"}'
```

Asset roles and their rules (checked when uploaded; a bad file gets a 422 with
the reason):

| Role | Used for | Accepted |
|---|---|---|
| `background` | Behind every screen except home, the photo viewer and the sleep screen | Any image Pillow reads, up to 10 MB. Re-encoded as WebP, at most 1280×720, never upscaled |
| `home_background` | Behind the home screen (falls back to `background`) | Same |
| `font` | All on-screen text (not the icons) | TrueType/OpenType (`.ttf`, `.otf`), up to 8 MB, must open in FreeType. Use a font you may redistribute, and one that has the accented letters your languages need |
| `sound_move`, `sound_select`, `sound_back`, `sound_open` | UI sounds for cursor move, choose, back and open | Ogg Vorbis or WAV, up to 2 MB and 10 seconds |

The device fits a background to its own screen once when the theme is applied
(scaled to cover, cropped in the centre) and veils it with the theme's `bg`
color so text stays readable on any picture. The volume cap applies to theme
sounds like the built-in ones.

## How it is synced

- Custom-theme files are stored in the media store under `themes/` and
  addressed by their SHA-256 like media. They are never modified or deleted in
  place, so [backups](BACKUP.md) keep working (replacing an asset, or deleting a
  theme, leaves the old file in the store).
- The manifest carries the chosen theme (`theme`) and lists its files in
  `files` (`file_type: "theme"`), so the device downloads, verifies and keeps
  them with its normal sync. It is part of the manifest hash: any change to the
  choice, the palette or an asset reaches the device.
- Tables: `themes` and `theme_assets` (new tables, no migration step).

## When something is wrong

The player never stops for a theme problem:

- A theme id the device cannot resolve (a deleted custom theme, an id from a
  newer server): the default theme, and the picker stays disabled.
- A manifest `theme` the device cannot parse: logged, ignored, and media
  syncs regardless.
- An asset that is missing, corrupt or unsupported: that one asset falls back
  (default font, built-in sound, plain background); the colors still apply. A
  file that could not be downloaded is retried on the next sync.

## Adding a built-in theme

Add a `ThemeDefinition` to `BUILTIN_THEMES` in
`packages/kidsplay-models/src/kidsplay_models/themes.py`, add its English name
to `BUILTIN_THEME_LABELS` in the server's `themes.py` and to `THEME_LABELS` in
the device's `views.py` (a short label: it sits in a swatch), then
`just i18n-extract`, translate, `just i18n-compile`. `tests/test_themes.py`
checks the contrast of every built-in.
