# Changelog

Newest first, grouped by day. One entry per change, in the same commit or PR as the change.

## 2026-11-15

**Google Photos sidecars give filecat the people in each photo (branch `sidecar-labels`).** Pairs with strata-filecat's file labels (`filecat labels`): the `takeout` profile now says which files can carry labels and reads them.

- `src/strata_takeout/sidecar.py`: **new**. `candidates(name)` lists the sidecar names Google may have used (cut at 46 characters before `.json`, `name(1).jpg` -> `name.jpg.supplemental-metadata(1).json`, `-edited` copies use the original's sidecar, older `<file>.json`); `find`, `people`.
- `profile.py`: `Takeout.label_exts` (image extensions) and `Takeout.labels(rel)` -> `[("person", name)]` or None when there is no sidecar.
- Tests: `tests/unit/test_sidecar.py` (synthetic files only), including a long-named duplicate whose truncated literal name would otherwise pick up the original's sidecar.

## 2026-11-10

**Move to uv, ruff, pyright and the shared strata-python gate (branch `claude/project-thread-iw7199`)**
Jonathan asked for every Python project to use the same tooling as [python-app-template](https://github.com/jburnhams/python-app-template): uv, ruff with `ruff format`, pyright and ty, one `scripts/check` gate, and dependencies kept current. Behaviour of the program is unchanged.

- `pyproject.toml`, `uv.lock`: hatchling instead of setuptools (entry points and the `filecat.profiles` group are unchanged); a `dev` group (pytest tooling, ruff, pyright, ty, strata-python for its gate); ruff `E,F,I,B,UP,SIM,RUF,C4,PT,S,TID` with `pytest.importorskip` banned, line length 120 (was 400), pyright strict ratchet starting at `__init__.py`, ty, coverage floor 70 (73% measured with bsdtar installed). `requirements-test.txt`: **removed**. `scripts/check`: **new**.
- `build/`: the setuptools build output (`build/lib/...`) was tracked by mistake; **removed** from git and ignored.
- `src/`, `tests/`: `ruff format` and import sorting (mechanical). `inbox.py`: `noqa: S108` on the `/tmp` fallback for the lock file (only used when `XDG_RUNTIME_DIR` is unset). `tests/unit/test_manifest.py`: the stub HTTP handlers' `log_message` takes the base class's `(format, *args)`.
- `.github/workflows/unit.yml`: uv, current action versions, bsdtar installed, the gate once on 3.13, unit and integration tests with coverage on 3.11-3.13. `upgrade-strata-python.yml`: **new**, weekly PR that moves the library pin after the gate (it installs bsdtar first, as `unit.yml` does). `.github/dependabot.yml`: ecosystem `uv` instead of `pip`.
- `README.md`, `AGENTS.md`: commands for uv. `scripts/install.sh` already used uv and is unchanged.
- Still skipped on a machine without `bsdtar` (22 tests, with a reason; CI installs it): making a missing tool fail instead of skip is a follow-up.
