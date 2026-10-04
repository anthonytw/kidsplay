# Third-party notices

KidsPlay itself is MIT-licensed (see [LICENSE](LICENSE)). The repository also
vendors the following third-party files, each under its own license.

| File(s) | Project | License |
|---|---|---|
| `packages/kidsplay-server/src/kidsplay_server/web/static/htmx.min.js` | [htmx](https://htmx.org) 1.9.12 | BSD 2-Clause |
| `packages/kidsplay-server/src/kidsplay_server/web/static/cropper.min.{js,css}` | [Cropper.js](https://github.com/fengyuanchen/cropperjs) 1.6.2, © Chen Fengyuan | MIT |
| `packages/kidsplay-device/src/kidsplay_device/assets/fa-solid-900.ttf` | [Font Awesome Free](https://fontawesome.com) (Solid) | SIL OFL 1.1 (font) |
| `packages/kidsplay-device/src/kidsplay_device/assets/ui-sounds/*.ogg` | [Kenney](https://kenney.nl) UI audio | CC0 1.0 |

## Demo sample media (`demo/media/`)

Every file under `demo/media/` was generated for this repository by
[`demo/make_sample_media.py`](demo/make_sample_media.py): no recordings,
photos or artwork were downloaded. The files are dedicated to the public
domain under [CC0 1.0 Universal](https://creativecommons.org/publicdomain/zero/1.0/).
The underlying works are public domain too:

| File | What it is | Underlying work | License |
|---|---|---|---|
| `demo/media/music/Nursery Tunes/01-twinkle.mp3` | Synthesized melody, Pillow-drawn cover | "Twinkle, Twinkle, Little Star" (traditional French melody, 1761) | CC0 1.0 |
| `demo/media/music/Nursery Tunes/02-frere-jacques.mp3` | Synthesized melody, Pillow-drawn cover | "Frère Jacques" (traditional, 18th century) | CC0 1.0 |
| `demo/media/music/Nursery Tunes/03-mary-lamb.mp3` | Synthesized melody, Pillow-drawn cover | "Mary Had a Little Lamb" (traditional American melody, 1830s) | CC0 1.0 |
| `demo/media/music/Bedtime Classics/01-lullaby.mp3` | Synthesized melody, Pillow-drawn cover | Brahms, "Wiegenlied", Op. 49 No. 4 (1868) | CC0 1.0 |
| `demo/media/music/Bedtime Classics/02-ode-to-joy.mp3` | Synthesized melody, Pillow-drawn cover | Beethoven, Symphony No. 9, "Ode to Joy" theme (1824) | CC0 1.0 |
| `demo/media/audiobooks/Aesop's Fables/01-hare-and-tortoise.mp3` | Speech synthesized with ffmpeg's `flite` filter, Pillow-drawn cover | Aesop, "The Hare and the Tortoise", tr. George Fyler Townsend (1867) | CC0 1.0 |
| `demo/media/audiobooks/Aesop's Fables/02-lion-and-mouse.mp3` | Speech synthesized with ffmpeg's `flite` filter, Pillow-drawn cover | Aesop, "The Lion and the Mouse", tr. George Fyler Townsend (1867) | CC0 1.0 |
| `demo/media/photos/Day Trips/beach-sailboat.jpg` | Procedurally drawn with Pillow | (original) | CC0 1.0 |
| `demo/media/photos/Day Trips/mountain-hike.jpg` | Procedurally drawn with Pillow | (original) | CC0 1.0 |
| `demo/media/photos/Day Trips/sunset-drive.jpg` | Procedurally drawn with Pillow | (original) | CC0 1.0 |
| `demo/media/photos/Backyard/flower-meadow.jpg` | Procedurally drawn with Pillow | (original) | CC0 1.0 |
| `demo/media/photos/Backyard/rainbow.jpg` | Procedurally drawn with Pillow | (original) | CC0 1.0 |
| `demo/media/photos/Backyard/starry-night.jpg` | Procedurally drawn with Pillow | (original) | CC0 1.0 |

## Pairing screen dependencies

Installed by `uv`, not vendored:

| Package | Used for | License |
|---|---|---|
| [segno](https://github.com/heuer/segno) 1.6.x, © Lars Heuer | Draws the pairing QR code on the device. Pure Python, no dependencies (and no Pillow: the device never imports it). | BSD 3-Clause |
| [python-zeroconf](https://github.com/python-zeroconf/python-zeroconf) 0.132+ | mDNS: the server advertises `_kidsplay._tcp`, the device finds it. Pure-Python fallback; optional compiled speedups. | LGPL 2.1 or later (used as an unmodified library, not vendored or linked into a distributed binary) |
| [ifaddr](https://github.com/ifaddr/ifaddr) (via zeroconf) | Lists network addresses to advertise. | MIT |

Runtime dependencies (installed by `uv`, not vendored) carry their own licenses;
see each package's `pyproject.toml`. Notably, `ffmpeg` is used as an external
binary by the server and is not distributed with this repository's source.
