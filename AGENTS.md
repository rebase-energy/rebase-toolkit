# Repository Guidelines

## Project Structure & Module Organization

This Python 3.12+ toolkit provides an SDK, Typer CLI, and Textual TUI for the Rebase Platform; the backend lives separately.

- `rebase/` contains the package: `client.py` defines resource handles, `public.py` exposes decorators, and `cli.py` implements commands. Warehouse connectors live in `rebase/sources/`.
- `tests/` holds module-oriented tests and shared fixtures.
- `scripts/` contains local setup and installation smoke tests; `pypi/rebase/` contains the compatibility metapackage.
- Consult `README.md` for usage and `ONBOARDING.md` for architecture and development details.

## Build, Test, and Development Commands

Run from the repository root:

- `uv sync --dev` installs dependencies and development tools.
- `uv run rebase --help` explores the local CLI.
- `uv run --locked pytest -q` runs the test suite.
- `uv run --locked ruff check .` checks lint and import ordering.
- `uv run --locked ruff format --check .` checks formatting; use `uv run --locked ruff format <changed-files>` to format touched files.
- `uv run --locked ty check` checks types; avoid new diagnostics despite the documented existing backlog.
- `uv build` builds distributions; `uv build --project pypi/rebase` builds the alias.
- `bash scripts/smoke_uv_install.sh local` verifies a fresh installation for packaging changes; requires Bash.

## Coding Style & Naming Conventions

Use four-space indentation, type annotations, `snake_case` functions/modules, `PascalCase` classes, and `UPPER_SNAKE_CASE` constants. Ruff targets Python 3.12 with a 120-character line limit. Keep optional dependencies lazily imported so core installations remain usable.

## Testing Guidelines

Use pytest with `tests/test_<module>.py` files and descriptive `test_<behavior>` functions. Add regression tests for behavior changes; run focused tests with `uv run --locked pytest tests/test_client.py -q` before the full suite. No numerical coverage threshold is configured.

Mock HTTP through `tests/http_stub.py`. Preserve shared isolation fixtures; setup tests must use `tmp_path` instead of writing workspace markers into this checkout. Tests should require no platform credentials.

## Commit & Pull Request Guidelines

Branch from `master`. Recent commits use descriptive, sentence-style subjects, sometimes prefixed by a component; Conventional Commits are not required. Describe the behavior changed and checks performed in PRs; link relevant issues and include screenshots for visible TUI changes. Update `README.md` and `CHANGELOG.md` for public API changes. Keep release versions synchronized in `pyproject.toml`, `rebase/version.py`, and `pypi/rebase/pyproject.toml`.
