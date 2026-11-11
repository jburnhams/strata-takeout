# Changelog

Newest first, grouped by day. One entry per change, in the same commit or PR as the change.

## 2026-11-10

**Move to uv, ruff, pyright and the shared strata-python gate (branch `claude/project-thread-iw7199`)**
Jonathan asked for every Python project to use the same tooling as [python-app-template](https://github.com/jburnhams/python-app-template): uv, ruff with `ruff format`, pyright and ty, one `scripts/check` gate, and dependencies kept current. Behaviour of the program is unchanged.

- `pyproject.toml`, `uv.lock`: hatchling instead of setuptools (entry points and the `filecat.profiles` group are unchanged); a `dev` group (pytest tooling, ruff, pyright, ty, strata-python for its gate); ruff `E,F,I,B,UP,SIM,RUF,C4,PT,S,TID` with `pytest.importorskip` banned, line length 120 (was 400), pyright strict ratchet starting at `__init__.py`, ty, coverage floor 70 (73% measured with bsdtar installed). `requirements-test.txt`: **removed**. `scripts/check`: **new**.
- `build/`: the setuptools build output (`build/lib/...`) was tracked by mistake; **removed** from git and ignored.
- `src/`, `tests/`: `ruff format` and import sorting (mechanical). `inbox.py`: `noqa: S108` on the `/tmp` fallback for the lock file (only used when `XDG_RUNTIME_DIR` is unset). `tests/unit/test_manifest.py`: the stub HTTP handlers' `log_message` takes the base class's `(format, *args)`.
- `.github/workflows/unit.yml`: uv, current action versions, bsdtar installed, the gate once on 3.13, unit and integration tests with coverage on 3.11-3.13. `upgrade-strata-python.yml`: **new**, weekly PR that moves the library pin after the gate (it installs bsdtar first, as `unit.yml` does). `.github/dependabot.yml`: ecosystem `uv` instead of `pip`.
- `README.md`, `AGENTS.md`: commands for uv. `scripts/install.sh` already used uv and is unchanged.
- Still skipped on a machine without `bsdtar` (22 tests, with a reason; CI installs it): making a missing tool fail instead of skip is a follow-up.
