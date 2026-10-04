# Translating KidsPlay

KidsPlay speaks English (`en`) and Spanish (`es`). Each surface picks its
language independently, because the people differ: a parent runs the web UI and
the CLI, a child uses the handheld.

| Surface | Language comes from | Catalog |
|---|---|---|
| Web UI | `?lang=xx` (the picker in the top bar, remembered in a cookie), then the cookie, then the browser's `Accept-Language`, then English | `packages/kidsplay-server/src/kidsplay_server/locale` |
| CLI | `--lang xx`, then `LANGUAGE`, `LC_ALL`, `LC_MESSAGES`, `LANG`, then English | `packages/kidsplay-cli/src/kidsplay_cli/locale` |
| Device | `language` in the device's `config.json`, then the **profile's** `language` setting from the last sync (see [SETTINGS.md](SETTINGS.md#language)), then Spanish if the profile never had one, then English | `packages/kidsplay-device/src/kidsplay_device/locale` |

API error messages (`detail` in JSON responses) stay English: they are for
developers. The web UI shows its own translated text instead where it can:
`apiDetail()` in `base.html` maps the response's `error_code` to a message from
`_ERROR_CODE_MESSAGES` in `kidsplay_server/web/routes.py` and falls back to the
English `detail` only for a code that has no entry (see Known gaps). A new API
error code with a fixed message needs an entry there; a test fails until it has
one or is listed as carrying specifics.

## How it works

Message ids are the English source strings, so English needs no catalog. Each
package has `locale/<lang>/LC_MESSAGES/<domain>.po` (edited by people) and the
compiled `<domain>.mo` (read at runtime with the standard library's `gettext`;
both are committed). `locale/<domain>.pot` is the template extracted from the
sources.

- **Web templates** (`web/templates/*.html`): `{% trans %}Text{% endtrans %}`
  for text, `{{ _('Text') }}` in attributes, and `{{ _('Text')|tojson }}` for
  strings used by the page's JavaScript. A literal `%` in a message is written
  `%%`. Python code in the server uses `gettext_now("…")` or
  `translate(request, "…")`; module-level constants use `N_("…")` and are
  translated where they are shown.
- **CLI**: `_("…")` for anything printed while a command runs, `N_("…")` in
  decorators (`help=N_("…")`), because decorators run before the language is
  known. Command docstrings are extracted automatically and translated when
  `--help` is shown.
- **Device**: `_("…")` from `kidsplay_device.i18n` when text is drawn,
  `N_("…")` for constants.
- Use named placeholders (`{name}` or `%(name)s`) and whole sentences, never
  concatenated fragments, so a translation can reorder words.

Click's own text ("Usage:", "Error: …", "Show this message and exit.") is
translated by a hand-maintained catalog, `locale/<lang>/LC_MESSAGES/messages.po`
in the CLI package. Click reads it through the process-wide gettext `messages`
domain.

## Commands

```bash
just i18n-extract      # rescan sources -> .pot, merge new strings into each .po
# ...edit the .po files...
just i18n-compile      # .po -> .mo
just i18n-check        # what the tests assert: .pot and .mo are current, no
                       #   untranslated strings, placeholders match
```

The tests fail when a string was added without re-extracting, a `.po` was
edited without compiling, or a supported language has an empty, fuzzy or
still-English entry, or a translation that drops or invents a placeholder or
HTML tag. A separate test scans the web templates and fails on any English
text left outside `{% trans %}` / `_()`.

## Adding a language

1. `just i18n-add-language fr` creates an empty catalog in every package.
2. Translate the `.po` files. Write natural, neutral text rather than word for
   word, and keep strings on the kid-facing device short. Keep placeholders
   (`{name}`, `%(name)s`, `%%`) and HTML tags exactly as they are; reorder them
   as the language needs.
3. Add the code to `SUPPORTED_LANGUAGES` and `LANGUAGE_NAMES` in
   `packages/kidsplay-models/src/kidsplay_models/i18n.py`. Only then does the
   picker offer it, and only then must the catalogs be complete (a language
   that is not listed may be a work in progress).
4. Copy Click's messages into `messages.po` for the CLI
   (`just i18n-add-language` creates the empty file).
5. `just i18n-compile`, then run the tests. The device font test renders every
   character of the new catalog with the device font and fails if one draws a
   missing-glyph box; a language outside Latin-1/Latin Extended-A needs a
   bundled font (an OFL one such as Noto Sans, recorded in
   `THIRD_PARTY_NOTICES.md`).

## Decisions

- **One Spanish term for bedtime: "Hora de dormir"**, on the device, the web
  and the CLI. The sleep-screen mode is "Pantalla de hora de dormir" so the
  word is the same everywhere.
- **Wake times stay 24-hour** ("Until 07:30"), as in the parents' bedtime
  settings, but the wording follows the hour: Spanish says "Hasta la 1:00" and
  "Hasta las 2:00", so the device's message is an `ngettext` on the hour.
- **The parent-facing CLI is localized**: help, messages, tables (enum values
  such as media type, status, bedtime mode and weekday are shown through
  translated labels, not raw), and Click's own text. The raw values remain what
  you type (`--type photo`, `--bedtime-mode sleep_screen`, `mon..sun`).
- **`kidsplay-server` (backup, restore, gc) and `kidsplay-allinone` are not
  localized.** They are run by whoever administers the machine, mostly from
  cron, systemd and support threads where the output is pasted or grepped, so
  stable English lines matter more than translation, and they hold a handful of
  short messages. Revisit if a non-English-speaking parent has to run them.

## Known gaps

- Click's type names in validation errors (`integer range`, `TEXT` in option
  usage lines, `[OPTIONS] COMMAND [ARGS]...`) are hard-coded in English by
  Click, and choice lists such as `--type [music|audiobook|photo]` show the
  literal values you type, which must not be translated.
- Text that comes from the server and is shown as-is is not translated: log
  lines, media titles, and the English `detail` of an API error whose
  `error_code` carries specifics (`PLUGIN_REQUIRED`, `PREVIEW_FAILED`,
  `INVALID_SETTING`, ...: a plugin name, a reason, a setting).
- Enum values (media type, processing status) are shown through translated
  label mappings, not raw: `_MEDIA_TYPE_LABELS` and `_PROCESSING_STATUS_LABELS`
  in `kidsplay_server/web/routes.py` (templates use the `media_type_label` and
  `processing_status_label` filters); the queue page has its own `T.mediaType` and
  `T.status` tables. A new enum member needs an entry there, or it is shown
  untranslated.
