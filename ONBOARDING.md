# Onboarding: rebase-toolkit

Welcome! This is the Python client and SDK for the Rebase Platform — the thing our
users `pip install` to deploy functions, workflows, and models and to read/write
warehouse data. It is deliberately small: it talks to the platform over HTTPS and
does **not** run a backend or database locally.

The server side lives in a sibling checkout, `platform/workflows`. You can be
productive here without it (the test suite is fully mocked), but you'll want it
running to exercise anything end-to-end.

---

## 1. Get set up

Requires Python ≥ 3.12 and [`uv`](https://docs.astral.sh/uv/).

```bash
git clone git@github.com:rebase-energy/rebase-toolkit.git
cd rebase-toolkit
uv venv .venv --python 3.13
source .venv/bin/activate
uv pip install -e . --group dev
```

> Two setup gotchas, both covered in [Known rough edges](#8-known-rough-edges):
> `dev` is a dependency *group*, not an extra — `uv pip install -e ".[dev]"`
> silently installs no dev tools. And **`uv sync` / bare `uv run` currently fail**;
> use `uv run --no-sync …` or the venv's interpreter directly.

Point the CLI at the hosted API and authenticate:

```bash
rebase setup                 # opens a browser, mints an API key
rebase workspace list
```

Credentials land in `~/.rebase/config.json` as named *profiles*
(`api_key`, `api_url`, `workspace_id`). Nothing else needs an API URL — the
hosted URL is baked into the SDK.

### Working against a local API

```bash
cd ../workflows && ./scripts/dev_api.sh          # starts API on :18082

cd ../rebase-toolkit
./scripts/dev_setup_local.sh --print-command      # see what it will do
./scripts/dev_setup_local.sh                      # waits for /health, then setup
```

Useful overrides: `REBASE_DEV_PROFILE`, `REBASE_DEV_PROVIDER`, `REBASE_DEV_REPO`,
`REBASE_DEV_API_URL`. Run with `--help` for the full list.

---

## 2. Verify your setup

```bash
uv run --no-sync pytest -q
```

**Expected today: `1 failed, 512 passed`.** The one failure,
`tests/test_client.py::test_project_deploy_registers_step_workflow_graph`, is
pre-existing (it also fails at the `v0.6.0` tag) — step registration order comes
back reversed. You did not break it. Everything else should be green.

Lint and types:

```bash
uv run --no-sync ruff check .            # clean today
uv run --no-sync ruff format --check .   # 2 files are already unformatted on master
uv run --no-sync ty check                # ~81 diagnostics today — not a clean gate
```

Keep `ruff check` clean in your changes. `ty` has a large pre-existing backlog, so
judge it by "did I add new diagnostics", not by the total.

Smoke-test a real install before pushing anything packaging-related:

```bash
scripts/smoke_uv_install.sh local
REBASE_TOOLKIT_GIT_REF=my-branch scripts/smoke_uv_install.sh github
```

---

## 3. The codebase in one page

Everything is in `rebase/`. Two files hold most of the weight — `client.py`
(~5.3k lines) and `cli.py` (~5.5k lines).

| File | What it is |
| --- | --- |
| `__init__.py` | The public surface. Eagerly re-exports ~50 names into `__all__`. |
| `public.py` | Thin decorator façade — `project()`, `@function`, `@step`, `@workflow`, `deploy()`. No logic of its own. |
| `client.py` | **The object model.** `Client` (a `requests` wrapper with ~130 REST methods) plus every resource class. |
| `cli.py` | The Typer CLI. Entry point is `rebase.cli:main`. |
| `config.py` / `auth.py` | Two credential stores: profiles in `~/.rebase/config.json`, and an OAuth/PKCE session for browser login. |
| `setup.py` | The `rebase setup` wizard (**not** a packaging file — don't be fooled). |
| `sources/` | Warehouse connectors: `base.py` + `bigquery`, `snowflake`, `databricks`, `fabric`, and the canonical `energy.py` layout. |
| `contract.py` | Dataset schema/quality contracts and the validation engine. |
| `stitch.py` | Priority-ordered composition of time series. |
| `timing.py` | `Duration` and `ForecastWindow`. |
| `tui.py` | Textual app behind `rebase tui`. |
| `_optional.py`, `data.py`, `modeling.py` | Lazy proxies to optional deps (`energydatamodel`, `emflow`). |

`hillclimb.py` (agentic model search) is a self-contained optional feature behind
the `hillclimb` extra — skip it for now; you won't need it to work on anything else.

Test files map 1:1 onto modules (`test_client.py` → `client.py`, etc.).
`tests/conftest.py` has one autouse fixture that clears the process-wide dataset
registry between tests — important, since `Dataset` registration is global state.

---

## 4. The core mental model

The same shape applies to functions, workflows, and models:

**decorate → deploy → invoke remotely.**

```python
import rebase as rb

project = rb.project("forecasting")

@project.function()
def add(a: int = 0, b: int = 0) -> dict:
    return {"sum": a + b}

project.deploy()
run = add.spawn(a=2, b=3)     # returns a Run handle
print(run.result(timeout=120))
```

Two things are worth internalizing early because they surprise people:

**1. Deploy ships your source text, not a pickle.** `deploy()` extracts the
decorated callable's source via `ast`, attaches git metadata, and POSTs it with an
image spec. That's why `rb.Image.python("3.13").uv_pip_install(...)` exists.

**2. Workflow bodies are *traced*, never executed locally.** On deploy, the body
runs against a `_WorkflowTrace` where each step call returns a `StepPromise`
sentinel; those promises are then compiled into a step DAG server-side. So a
workflow body must be plain orchestration — no branching on step *results*, since
at trace time they aren't values yet.

Invocation is uniform across resources: `.spawn()` → `Run`, `.remote()` →
`.spawn().result()`, `__call__` == `.remote`, and `Function.map()` fans out.
`Run` gives you `.status`, `.result()`, `.logs()`, `.cancel()`, `.replay()`.

---

## 5. Datasets, contracts, and replay

This is the newest and least-documented area, so it's where you're most likely to
be asked to work.

A `Dataset` is a named signal channel. A `Contract` declares what valid data looks
like — per-column dtype/not-null/range/`isin` plus table-level policy (primary
key, min rows, freshness, watermark). `DataSource.write()` runs a fixed pipeline:

```
resolve contract → validate frame → write → signal listeners
```

`on_violation` controls whether validation failures `"fail"` or `"warn"`.
Contracts are declared in code and stored server-side, and `preflight_datasets()`
blocks `deploy` when the two have drifted — `rebase dataset check` / `sync` are
the CLI side of that.

**Replay safety is pervasive and easy to break.** `REBASE_REPLAY_KNOWLEDGE_TIME`
makes `read_bitemporal` filter rows to what was knowable at the original run time,
and it suppresses dataset signals so a replay never re-triggers downstream
workflows. If you touch read or write paths in `sources/`, preserve that.

Triggers are `Cron`, `OnWorkflow` (another workflow finished), and `OnUpdate` (a
dataset was updated). A workflow can declare a reserved `ctx` parameter to receive
a `TriggerContext` explaining *why* it fired (`reason`, `fired_at`, `is_replay`).
Resolve `ForecastWindow` offsets against `ctx.fired_at`, never `datetime.now()` —
that's what makes replays reproduce the original window.

---

## 6. Day-to-day commands

```bash
rebase deploy workflow.py
rebase run functions.py::add --param a=2 --param b=3
rebase run list / get <id> / logs <id> / cancel <id> / replay <id>
rebase dataset list / validate / check / sync
rebase workflow schedule show|set|clear|pause|resume
rebase volume ls|put|download|rm
rebase bucket ls|put|download|rm|uri
rebase secret create NAME KEY=value
rebase tui                        # interactive dashboard
```

CLI groups mirror the client object model closely, so `cli.py` is often the
fastest way to discover which `Client` method does what.

---

## 7. Making a change

1. Branch off `master`.
2. Write the code **and** a test — the suite is mocked and fast (~13 s), so
   there's no excuse to skip it.
3. `uv run --no-sync ruff check .`, then `ruff format` **only the files you
   touched** — a bare `ruff format .` also reformats two unrelated files that are
   already unformatted on `master`, which muddies your diff.
4. `uv run --no-sync pytest -q` — expect the one known failure, nothing more.
5. If you changed the public surface, update `README.md` and add a `CHANGELOG.md`
   entry (we keep it hand-written, grouped under a version heading).
6. Version bumps touch **three** places: `pyproject.toml`, `rebase/version.py`,
   and `pypi/rebase/pyproject.toml` (the alias package must match).

---

## 8. Known rough edges

Real potholes, discovered while writing this guide. Don't lose an afternoon to
them:

- **`uv sync` and bare `uv run` are broken.** `uv.lock` is stale — it pins
  `rebase-toolkit` at `0.2.0` while `pyproject.toml` says `0.6.0` — so uv
  re-resolves from scratch. That resolution is *universal*: it covers every extra
  on every platform, including one optional dependency that isn't published to
  PyPI, so it dead-ends at "No solution found". This bites even though you need
  none of those extras. Workaround: `uv run --no-sync …`, or just call
  `.venv/bin/python -m pytest`.
- **`uv pip install -e ".[dev]"` is a silent no-op.** `dev` is a
  `[dependency-groups]` entry, not an optional extra, so uv accepts the command,
  exits 0, and installs no dev tooling. Use `--group dev`.
- **`rebase/version.py` says `0.2.0`.** `rb.__version__` reports `0.2.0` while we
  ship `0.6.0`. `tests/test_packaging.py` only checks `pyproject.toml` against the
  `pypi/rebase` alias, so the drift is untested.
- **One test fails on a clean checkout** (see §2). Pre-existing, not yours.
- **`ty check` is not a clean gate** (~81 diagnostics) and `ruff format --check`
  flags 2 files. Don't treat either total as a regression signal.
- **`rebase/setup.py` is the auth wizard**, not packaging config.
- **`Dataset` registration is process-global.** Tests rely on the `conftest.py`
  fixture to reset it; remember that if you add tests outside `tests/`.
- **`runs/` is git-ignored** local hillclimb output. Safe to delete.

---

## 9. Where to go next

- `README.md` — the user-facing story; the best tour of the public API.
- `CHANGELOG.md` — the fastest way to see what's been built recently and why.
- `../workflows/` — the platform backend, if you need end-to-end behaviour.
- `tests/test_public_api.py` — a compact, executable inventory of the public
  surface.

Good first task: fix the `rebase/version.py` drift (§8) and add a packaging test
that asserts `rebase.__version__` matches `pyproject.toml`. It's small,
self-contained, and stops the next person from shipping a wrong version string.
