Place fa-solid-900.ttf (Font Awesome 6 Free Solid) here to enable icons
on the home screen.  pygame requires a TTF or OTF file — WOFF/WOFF2 are
not supported.

Option 1 — download the full release zip (includes TTF):
  https://github.com/FortAwesome/Font-Awesome/releases
  → fontawesome-free-*-desktop.zip → otfs/Font Awesome 6 Free-Solid-900.otf
  → rename to fa-solid-900.ttf and place here

Option 2 — Raspberry Pi OS (apt):
  sudo apt-get install fonts-font-awesome
  Then symlink or copy:
  /usr/share/fonts/truetype/font-awesome/fontawesome-webfont.ttf
  → rename/copy to this directory as fa-solid-900.ttf

Option 3 — place the file at ~/.kidsplay/fa-solid-900.ttf instead.
  The player checks that location too.
