# Hardware profiles: screen size and input

The player was built for the Retroflag GPi Case 2 (640×480, a d-pad, four face
buttons and two shoulder buttons). Two settings in the device's
`~/.kidsplay/config.json` adapt it to other hardware. Both default to the GPi
Case 2, so an install that sets neither behaves exactly as before.

```json
{
  "width": 800,
  "height": 480,
  "input_profile": "generic-gamepad",
  "input_overrides": {"joy_buttons": {"7": "playpause"}}
}
```

`width` and `height` can also be given for one run:
`kidsplay-player --width 1280 --height 720 --input-profile keyboard`.
`kidsplay device setup --width 800 --height 480` writes them into the config it
generates.

## Screen size

Every size in the views is designed for 640×480 and scaled by
`min(width / 640, height / 480)` (`layout.py`): text, icons, padding, the
playback bar and the list rows. The three-row lists keep showing exactly three
rows. On a screen wider than 4:3 (800×480) the scale follows the height and the
extra width goes to the lists and grids. At 640×480 the scale is exactly 1, so
nothing moves.

Sizes below 240×180 are refused: the text would be unreadable. Tested at
320×240, 640×480, 800×480 and 1280×720 (see below).

The server's device record also has a `display_width`/`display_height`; the
player does not read it. `config.json` is what counts.

## Input profiles

An input profile is a named table in `input_profiles.py`: which keyboard key,
joystick button, hat or stick axis presses which logical button. The logical
buttons are `up`, `down`, `left`, `right`, `select` (confirm), `cancel` (back),
`playpause`, `repeat`, `volume_up`, `volume_down` and `terminate` (quit).

| Profile | For | Bindings |
|---|---|---|
| `gpi2` (default) | Retroflag GPi Case 2 | The bindings every install had before profiles: the d-pad hat; joystick buttons 0 select, 1 cancel, 2 play/pause, 3 repeat, 4 volume down, 5 volume up; and a keyboard (arrows, Return/`a` select, `b`/Backspace cancel, `x`/Space play/pause, `y` repeat, `+`/`-` volume, Escape quits). |
| `keyboard` | A computer keyboard, for development or a keyboard-only device | The same keys, no joystick. The in-app volume keys are **on** by default, since there is no volume dial. |
| `generic-gamepad` | Any SDL gamepad | The same face and shoulder buttons and keys as `gpi2`, plus the left stick as a d-pad (for pads whose d-pad is an axis). |

What each logical button *does* on each screen is not part of a profile, and
neither are these rules: **Escape quits** (not during bedtime), `cancel`/B goes back, the bedtime
sleep screen ignores every button, and the in-app volume buttons only act when
volume buttons are on (a press at the cap or at zero still shows the volume
overlay, so the child sees the limit). The volume cap always applies.

An unknown `input_profile` is logged and treated as `gpi2`: a typo must not
leave a child's device unusable.

### Volume buttons and the input profile

`ProfileSettings.volume_buttons` (per child, set on the server) is on, off or
undecided (the default). A profile sets what "undecided" means: `keyboard` turns
the buttons on (no volume dial), `gpi2` and `generic-gamepad` leave them off.
A parent's explicit on or off wins over the profile, so they can switch the
buttons off for a `keyboard` device; the volume cap applies either way. To
change the default on one device, use
`"input_overrides": {"volume_buttons": false}` in its `config.json`.

### Overriding a profile

`input_overrides` in `config.json` is applied on top of the profile:

```json
"input_overrides": {
  "keys": {"K_F5": "repeat", "return": "playpause", "K_a": null},
  "joy_buttons": {"7": "playpause"},
  "volume_buttons": true
}
```

- `keys`: a pygame key constant name (`"K_F5"`) or its bare name (`"f5"`,
  `"return"`, `"a"`) mapped to a logical button name. `null` removes a binding.
- `joy_buttons`: joystick button number (a string, as JSON keys are) to a
  logical button name, or `null`.
- `volume_buttons`: `true` or `false`.

A bad entry (an unknown key, an unknown button, a non-number) is logged and
skipped; the rest of the table still applies.

To find a button's number on a pad, run
`uv run python -m pygame.examples.joystick` (pygame-ce ships it) or watch
`JOYBUTTONDOWN` events.

## Adding a hardware profile

1. Add an `InputProfile` to `PROFILES` in
   `packages/kidsplay-device/src/kidsplay_device/input_profiles.py`:

   ```python
   InputProfile(
       "my-handheld",
       keys=_KEYS,                      # keyboard keys, or {} for none
       joy_buttons={0: "select", 1: "cancel", 2: "playpause", 3: "repeat"},
       hat=True,                        # d-pad as a joystick hat
       axes={0: "horizontal", 1: "vertical"},   # optional: a stick as d-pad
       volume_buttons=True,             # optional: no volume dial on this device
   ),
   ```

   Names are strings (`"K_RETURN"`, `"select"`) so the table is plain data.
2. Add a test in `tests/test_input_profiles.py` that maps each of its events.
3. Set `"input_profile": "my-handheld"` on the device, and its `width` and
   `height` if they differ from 640×480. No other code changes.
4. Check every screen at the device's size: the test in
   `tests/test_screens.py` covers the four sizes above; add your size to its
   `SIZES` and run it with `KIDSPLAY_SHOTS_DIR=/tmp/shots` to look at the
   frames it saves.
5. On the real device, walk the "Setting up and checking a handheld" steps in
   [RELEASE_CHECKLIST.md](RELEASE_CHECKLIST.md).

## Looking at the screens without hardware

`tests/test_screens.py` runs the real player headless (SDL dummy drivers),
visits every screen in English and Spanish at each size, and saves one PNG per
screen: `KIDSPLAY_SHOTS_DIR=/tmp/shots uv run pytest
packages/kidsplay-device/tests/test_screens.py`. It also fails if anything is
drawn past the edge of the surface it is drawn on (clipped or overflowing
text). Without the variable the frames go to pytest's temp directory, never
into git.
