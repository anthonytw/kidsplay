# KidsPlay device operations.
#
# HOST is an ssh target (a Host alias from ~/.ssh/config works well). Examples:
#   just device-status  handheld1
#   just device-update  handheld2
#
# These recipes run from an operator machine that can reach both the git remote
# and the devices over ssh.

# Where device-identity backups go: an ssh host and a directory on it. Media itself
# re-syncs from the server, so it is deliberately NOT backed up.
backup_host := env_var_or_default("KIDSPLAY_BACKUP_HOST", "localhost")
backup_dir := env_var_or_default("KIDSPLAY_BACKUP_DIR", "kidsplay-device-backups")

_default:
    @just --list

# Ship origin/main to a device, resync the venv, restart the kiosk, verify.
#
# WHY THIS PUSHES INSTEAD OF PULLING: a kids' device is best kept on an isolated
# network that can reach the KidsPlay server and nothing else -- including the git
# remote. A device-side `git pull` would then fail (and it fails silently, so devices
# drift). The direction is inverted instead: the operator machine can reach both the
# remote and the devices, and pushes into the device checkout. The device never
# initiates, so a handheld in a kid's hands needs no access to the git server.
#
# `receive.denyCurrentBranch=updateInstead` is what lets a push update a checked-out
# worktree -- and it REFUSES on a dirty tree or a non-fast-forward, so local drift
# fails the deploy loudly instead of being silently overwritten.
#
# Push origin/main to a device, resync the venv, restart the kiosk, verify.
device-update HOST:
    #!/usr/bin/env bash
    set -euo pipefail
    git fetch --quiet origin
    target=$(git rev-parse origin/main)
    ssh {{HOST}} 'git -C ~/kidsplay config receive.denyCurrentBranch updateInstead'
    git push --quiet "ssh://{{HOST}}/~/kidsplay" "$target:refs/heads/main"
    # `--package kidsplay-device` is REQUIRED. A bare `uv sync` resolves the workspace
    # ROOT, which does not include kidsplay-device -- it uninstalls the player and
    # leaves the kiosk looping on "kidsplay-player: not found" (rc=127). No network
    # here, so this works only from uv's local cache; a commit that adds a dependency
    # will fail right here, which is the intended behaviour rather than a half-update.
    ssh {{HOST}} 'cd ~/kidsplay && ~/.local/bin/uv sync --package kidsplay-device'
    ssh {{HOST}} 'sudo systemctl restart getty@tty1.service'
    # Give the kiosk loop a moment to respawn the player before asserting on it.
    sleep 15
    just device-verify {{HOST}}

# Exits non-zero on any mismatch, so it is usable as a gate in other recipes.
#
# Assert a device runs exactly origin/main: right commit, right branch, clean, player up.
device-verify HOST:
    #!/usr/bin/env bash
    set -euo pipefail
    git fetch --quiet origin
    expected=$(git rev-parse origin/main)
    # `[k]idsplay-player` is the bracket trick, and it is load-bearing: a plain
    # `pgrep -f kidsplay-player` over ssh also matches the ssh command line itself,
    # so it reports the player alive even when it is dead.
    read -r actual branch dirty players < <(ssh {{HOST}} 'cd ~/kidsplay && echo \
        "$(git rev-parse HEAD) \
         $(git branch --show-current || echo DETACHED) \
         $(git status --porcelain --untracked-files=no | wc -l) \
         $(pgrep -cf "[k]idsplay-player" || true)"')
    # Media/db must live outside ~/kidsplay: that directory IS the deploy checkout,
    # so anything synced into it is untracked bulk inside the worktree that a
    # `git clean -fdx` would delete.
    paths=$(ssh {{HOST}} 'python3 -' <<'PYCHK'
    import json, pathlib
    cfg = json.loads((pathlib.Path.home() / ".kidsplay/config.json").read_text())
    checkout = pathlib.Path.home() / "kidsplay"
    bad = [k for k in ("media_root", "db_path")
           if checkout in pathlib.Path(cfg[k]).expanduser().parents]
    print(",".join(bad) or "ok")
    PYCHK
    )
    ok=0
    [ "$actual" = "$expected" ] || { echo "FAIL commit:  $actual != origin/main $expected"; ok=1; }
    [ "$branch" = "main" ]      || { echo "FAIL branch:  on '$branch', expected main"; ok=1; }
    [ "$dirty" = "0" ]          || { echo "FAIL tree:    $dirty uncommitted tracked change(s)"; ok=1; }
    [ "$players" != "0" ]       || { echo "FAIL player:  kidsplay-player is not running"; ok=1; }
    [ "$paths" = "ok" ]         || { echo "FAIL config:  $paths inside the git checkout (~/kidsplay)"; ok=1; }
    if [ "$ok" = "0" ]; then
        echo "{{HOST}}: OK -- main @ ${actual:0:7}, clean, player up, media outside the checkout."
    else
        echo "{{HOST}}: NOT running origin/main. Fix with 'just device-update {{HOST}}'."
    fi
    exit "$ok"

# Restart the kiosk (labwc + app) without a full reboot.
device-restart HOST:
    ssh {{HOST}} 'sudo systemctl restart getty@tty1.service'
    @echo "{{HOST}}: kiosk restarted."

# Follow the app log.
device-logs HOST:
    ssh {{HOST}} 'tail -n 40 -f ~/kidsplay-kiosk.log'

# Quick health snapshot.
device-status HOST:
    @ssh {{HOST}} 'printf "default-target: %s\n" "$(systemctl get-default)"; \
        printf "app instances:  %s\n" "$(pgrep -cf bin/kidsplay-player)"; \
        pgrep -x labwc >/dev/null && echo "labwc:          up" || echo "labwc:          DOWN"; \
        printf "app commit:     %s\n" "$(git -C ~/kidsplay log --oneline -1)"; \
        echo "--- kiosk log tail ---"; tail -n 3 ~/kidsplay-kiosk.log'
    @echo "--- provenance ---"
    @just device-verify {{HOST}} || true

# First-time kiosk setup on a fresh device (copy + run the installer, then reboot).
device-deploy HOST:
    scp packages/kidsplay-device/deploy/install-kiosk.sh {{HOST}}:~/install-kiosk.sh
    ssh {{HOST}} 'chmod +x ~/install-kiosk.sh && ~/install-kiosk.sh'
    -ssh {{HOST}} 'sudo systemctl reboot'
    @echo "{{HOST}}: kiosk installed + rebooting."

# Revert a device from the kiosk back to desktop boot.
device-rollback HOST:
    ssh {{HOST}} '~/install-kiosk.sh --rollback'
    -ssh {{HOST}} 'sudo systemctl reboot'
    @echo "{{HOST}}: rolled back to desktop boot + rebooting."

# Backups are keyed by the device's own hostname (stable across IP changes).
# Media re-syncs from the server, so it is not backed up here.
#
# Back up a device's identity (config.json) to the backup host.
device-backup-config HOST:
    #!/usr/bin/env bash
    set -euo pipefail
    name=$(ssh {{HOST}} hostname)
    ssh {{backup_host}} "mkdir -p {{backup_dir}}/$name"
    ssh {{HOST}} 'cat ~/.kidsplay/config.json' | ssh {{backup_host}} "cat > {{backup_dir}}/$name/config.json"
    echo "{{HOST}} ($name): config.json -> {{backup_host}}:{{backup_dir}}/$name/config.json"

# Restore a device's identity from the backup host (after a re-flash); it then re-syncs media.
device-restore-config HOST:
    #!/usr/bin/env bash
    set -euo pipefail
    name=$(ssh {{HOST}} hostname)
    ssh {{HOST}} 'mkdir -p ~/.kidsplay'
    ssh {{backup_host}} "cat {{backup_dir}}/$name/config.json" | ssh {{HOST}} 'cat > ~/.kidsplay/config.json'
    echo "{{HOST}} ($name): config.json restored. Run 'just device-restart {{HOST}}' (or reboot) to apply."

# ---------------------------------------------------------------------------
# Demo and README screenshots. These run from a plain clone and need only uv
# (and ffmpeg for the server's other import paths).
# ---------------------------------------------------------------------------

# Everything lives in a temporary directory that is deleted when the player
# window closes. Options: --port N, --keep (keep the data), --no-player (web UI
# only, until Ctrl-C).
#
# Try KidsPlay: a throwaway server with sample media, and the player in a window.
demo *ARGS:
    uv run --all-packages --no-dev python -m demo {{ARGS}}

# Deterministic: rerunning on the same machine rewrites identical files, so any
# diff in docs/images/ is a real UI change. The first run needs the browser:
# `uv run playwright install chromium`. Options: --skip-web, --skip-device.
#
# Regenerate the README screenshots and GIF in docs/images/.
screenshots *ARGS:
    uv run --all-packages python -m demo.screenshots {{ARGS}}

# Regenerate the bundled sample media in demo/media/ (needs ffmpeg with flite;
# Homebrew's has none -- use Debian/Ubuntu). Existing files are only replaced once
# every new one has been generated.
sample-media:
    uv run --all-packages python -m demo.make_sample_media

# ---------------------------------------------------------------------------
# Translations (docs/TRANSLATING.md)
# ---------------------------------------------------------------------------

# Re-scan the sources for translatable strings, refresh every .pot and merge
# the new strings into each language's .po file.
i18n-extract:
    uv run python scripts/i18n.py extract

# Compile every .po into the .mo file the packages load at runtime. The .mo
# files are committed; the tests fail when they are out of date.
i18n-compile:
    uv run python scripts/i18n.py compile

# Fail if a .pot or .mo file is stale (what the tests assert too).
i18n-check:
    uv run python scripts/i18n.py check

# Start a catalog for a new language in every package, e.g. `just i18n-add-language fr`.
i18n-add-language LANG:
    uv run python scripts/i18n.py add-language {{LANG}}
