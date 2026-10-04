# Contributing to KidsPlay

Read [CLAUDE.md](CLAUDE.md) first: it holds the architecture principles, the
settled tech stack and the code-style rules. [docs/DEVELOPMENT.md](docs/DEVELOPMENT.md)
covers running the server, CLI and device player locally.

## Development setup

You need Python 3.12+, [uv](https://docs.astral.sh/uv/) and ffmpeg (the
loudness tests run it).

```bash
git clone https://github.com/anthonytw/kidsplay.git
cd kidsplay
uv sync --all-packages
```

## Checks

CI (`.github/workflows/ci.yml`) runs these on every push and pull request. Run
them locally before pushing; all four must exit 0.

```bash
uv run ruff check .           # lint (add --fix to apply safe fixes)
uv run ruff format --check .  # formatting (drop --check to apply)
uv run ty check               # type check src/ and tests/ of every package
uv run pytest                 # all tests
uv run pytest -n auto --dist loadscope   # the same, in parallel (what CI runs)
```

Type checking uses [ty](https://github.com/astral-sh/ty), pinned to an exact
version in `pyproject.toml` because it is pre-1.0. Upgrade it in a dedicated PR.
Fix type errors rather than suppressing them. When a suppression is genuinely
needed, use `# ty: ignore[<rule>]` with a comment saying why; blanket
`# type: ignore` comments are not honoured, and unused suppressions fail the
check.

## Workflow

- **One PR per issue.** Keep each PR scoped to a single issue and reference
  it in the description (`Closes #N`). Following CLAUDE.md, an endpoint and
  its CLI command are separate issues that share a contract in `docs/API.md`.
- **Behaviour changes need tests.** Every public function has at least one test.
  The test count should never drop.
- Branch from `main`; keep your branch rebased rather than merging `main` in.

## Verification tiers

- **Tier 1, every PR:** the four checks above, plus the headless player and
  phone-width browser tests they include.
- **Tier 2, arm64 Debian:** `.github/workflows/arm64-device.yml` installs and
  tests the device package on the handhelds' platform. It runs nightly and on
  PRs that touch `packages/kidsplay-device/`, `packages/kidsplay-models/`,
  `uv.lock` or the `justfile`; it is not a required check yet.
- **Tier 3, real hardware:** [docs/RELEASE_CHECKLIST.md](docs/RELEASE_CHECKLIST.md),
  run on a handheld before each release and when a change touches boot (the
  kiosk installer, systemd units), the display (layout, fonts, themes), input
  (buttons, input profiles) or the clock (`kidsplay-timeset`, bedtime). If your
  change can only be confirmed on hardware, add the step to that file.

## Commit messages

Use [Conventional Commits](https://www.conventionalcommits.org/):

```
<type>(<optional scope>): <summary in the imperative>
```

Common types: `feat`, `fix`, `refactor`, `test`, `docs`, `style`, `build`,
`ci`, `chore`. Scopes are package names, e.g. `fix(device): ...` or
`feat(server): ...`. Make each commit one logical step that passes the checks.

## Reporting bugs and requesting features

Use the issue templates under **New issue**. For bugs, include the package,
steps to reproduce, and expected vs actual behaviour.

## License of contributions

KidsPlay is licensed under the GNU AGPL-3.0-or-later. By contributing, you
agree that your contribution is licensed under the same terms.
