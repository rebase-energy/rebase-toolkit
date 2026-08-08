# Changelog

## Unreleased

### Added

- **`o` opens a project's source file from the TUI**, from the project list or from inside a
  project. A project is a platform record with no path on this machine, so the file is found by
  searching this workspace's search paths for the `rb.project(...)` call that names it — which
  means a file that has moved, or was never deployed from this computer, still resolves. The
  name is read out of the AST rather than matched as text, because the real idiom passes a
  constant (`rb.project(PROJECT_NAME)`) and a text search for `rb.project("epex")` finds
  nothing. Candidate files are never imported. When more than one file declares the project a
  picker asks which; the answer is deliberately not remembered, since a stored path is exactly
  what goes stale. Editor resolution is `REBASE_EDITOR` → the `editor` config key → `$VISUAL` →
  `$EDITOR` → the first of `code`/`cursor`/`zed`/`subl`/`idea` on `PATH` → a macOS application
  bundle. Terminal editors get the terminal, via suspend, and get it back on exit.
- **`rebase project open`** does the same from the command line, with `--path` to print the
  resolved `file:line` instead of launching anything and `--json` for the full search result.
- **`rebase project search-path {list,add,remove}`** manages where that search looks. The TUI
  records the git repository it was started in automatically, so the common case needs no
  setup; `add` refuses your home directory without `--force`, since registering it would turn
  every lookup into a scan of everything you own.
- **An Endpoints column in the TUI's project table**, alongside Workflows and Functions, so the
  workspace view shows at a glance how much of a project is exposed over HTTP. Endpoints belong
  to a function or workflow rather than sitting beside them, so the columns overlap by design —
  a project with 1 function and 1 endpoint has one deployed thing, reachable two ways. The count
  costs no extra startup time: it comes from one workspace-wide `/endpoints` call issued
  alongside the others. Project detail and the summary line carry the same total.
- **`d` deletes from the TUI**, with `shift+up` / `shift+down` to mark a range of rows first.
  It acts on the projects table in the workspace view and on the workflows/functions table of
  the active tab inside a project; marked rows turn amber and are counted in the title. Every
  delete goes through a type-to-confirm dialog — one row asks for its own name typed back, a
  batch asks for the word `delete` — and then deletes with `force`, so contents and run history
  go with it. ASGI apps and runs have no delete endpoint, so `d` declines there.
- **`s` hands the mouse back to the terminal** so text can be selected and copied the way it is
  in any program that never took the mouse — drag, then the terminal's own copy shortcut
  (`cmd+c` on macOS). Pressing `s` again takes the mouse back for hover, clicking and wheel
  scrolling. The two cannot be had at once: while mouse reporting is on the terminal forwards
  drags to the app rather than selecting, which is why `cmd+c` had nothing to copy. Keys work in
  both modes, and the title says which one is active.
- **`shift+left` / `shift+right` adjust the TUI's text selection** after a mouse drag, growing
  or shrinking it at its trailing end so a copy can be trimmed without re-dragging. It stops at
  the ends of the line rather than wrapping, and leaves selections that span several widgets
  alone. Copying itself is Textual's own `ctrl+c` / `cmd+c` binding, unchanged.
- **`Client.delete_projects(ids, force=...)`** deletes many projects in one request, against the
  platform's new `POST /projects/batch-delete`. It returns `(project_id, error)` per failure
  instead of raising, because the route is deliberately not atomic — a project delete tears down
  Cloud Run services and Prefect deployments, which cannot be rolled back, so it reports on each
  project rather than pretending the set succeeds or fails together. Against an API too old to
  have the route it falls back to one request per project, so a toolkit ahead of its platform
  still deletes. The TUI's `d` uses it for a marked run of projects; functions and workflows have
  no batch route and stay a concurrent fan-out.
- **`RebaseWorkflowError.status_code`** carries the HTTP status behind a failure, so callers can
  tell a route this API version does not have from a genuine error.
- **`-h` is an alias for `--help`** on every command and subcommand.
- **Short flags for CLI options**: each option now also answers to `-x`, where `x` is the first
  letter of its long name — `rebase deploy -n api -e prod`, `rebase workflow list -j`. 235 of 269
  options got one. The remaining 34 lost the letter to an option declared earlier in the same
  command (`rebase deploy --sync` has no `-s`, because `--source` took it), and `-h` is reserved
  for `--help` throughout. Hand-written short flags are unchanged.

### Removed

- **The TUI's summary bar** — the `Profile | API | Projects | Functions | Workflows | Endpoints |
  Selected project | Latest runs per target` line above the project detail. It restated the
  workspace counts on every screen that already showed them, and cost three rows at the top of
  the project view; those rows go to the target tables instead. The project detail panel below it
  keeps the per-project counts, and a failed request now writes its error there and raises a
  notification, since the bar used to be where errors landed.

### Fixed

- **Deleted rows leave the TUI immediately instead of after every request has returned.** A marked
  run of projects was deleted one round trip at a time and then followed by a full workspace
  reload, so rows the user had already confirmed gone sat on screen for seconds. The rows now come
  off as soon as the dialog is confirmed, the deletes are issued concurrently, and the reload only
  happens if one of them fails — in which case the surviving rows come back. The API has no bulk
  delete route, so this is still one request per object, just no longer one wait per object.

- **`rebase tui` starts in about a second instead of stalling on the project list.** The workspace
  overview issued two requests per project — one for functions, one for workflows — end to end, so
  startup was `2 × projects` round trips deep: ~13.5s on a 21-project workspace. Workflow counts now
  come from a single workspace-wide `/workflows` call (every workflow carries its `project_id`), and
  the function counts, which have no workspace-wide route, are fetched concurrently. Same numbers,
  ~1.8s.

- **Unknown options and commands print a usage error again** instead of a Rich traceback, and exit
  2 rather than 1. typer >= 0.26 vendors its own copy of click, and the vendored exception classes
  do not subclass the ones in the `click` package, so the CLI's `except click.ClickException`
  handler never fired for anything typer's parser raised. `Abort` (Ctrl-C at a prompt) was affected
  the same way.

## 0.6.1 — 2026-08-07

### Fixed

- `rebase-toolkit[hillclimb]` now installs the published `hillclimb` 0.2 release
  with its compatible `emflow` 0.3.1 integration and packaged benchmark data.
- The `rebase` compatibility package now forwards its `hillclimb` extra.

### Changed

- `rebase-toolkit[all]` excludes Snowflake because its current pandas constraint
  conflicts with hillclimb's pandas 3 requirement; install the `snowflake` extra
  separately.

## 0.6.0 — 2026-07-07

### Added

- **GitLab integration**: `rebase connect gitlab [group/project]` connects a GitLab
  repository (gitlab.com or self-managed via `--host`) with an access token — resolved
  from `--token`, `$GITLAB_ACCESS_TOKEN`, or a hidden prompt, and validated against the
  GitLab API. The token is stored in the platform's Secret Manager (never in the Rebase
  database); reconnecting rotates it. Provides the same surface as the GitHub
  integration: workspace/project source backing, repo file reads, starter workflows,
  and promotion merge requests. Client wrappers: `connect_gitlab_repo`,
  `list_gitlab_repo_connections`, `get_gitlab_repo_file`,
  `create_gitlab_starter_workflow`, `create_gitlab_promotion_mr`.

## 0.5.0 — 2026-07-07

### Added

- **Canonical energy layout for BigQuery** (`rebase.sources.energy`), mirroring rebase's
  open data model (energydatamodel/timedatamodel) and its ClickHouse persistence: an
  append-only `series_values` table with three time axes (`valid_time`,
  `knowledge_time`, `change_time`) plus a `series` catalog keyed by
  `(path, data_type, name)` with a deterministic `series_id`.
  `BigQuerySource.ensure_energy_schema` / `register_series` / `write_series` /
  `read_series` cover schema creation, idempotent registration, SIMPLE/VERSIONED writes
  (corrections are new rows), and point-in-time reads with timedb semantics: latest,
  `as_of` knowledge cutoff, `overlapping` (every forecast issue), and the full audit
  trail. `rebase.sources.SeriesKey` is exported. The `QUALIFY`-based read builder is
  dialect-portable to Snowflake and Databricks.
- BigQuery query parameters now support `datetime` values (`TIMESTAMP`).

## 0.4.0 — 2026-07-07

### Added

- **Per-function `cpu=` / `memory=`**, Modal-style, on functions and models (including
  `Predictor` and the other model classes): `@rb.function(cpu=2, memory=1024)` (cores /
  MiB as numbers) or Cloud Run strings (`cpu="500m"`, `memory="1Gi"`), and as class
  attributes on models. Applies to both the isolated Cloud Run service and Cloud Run
  Jobs backends; requests are clamped by the workspace compute policy. Unset means the
  platform defaults.

## 0.3.0 — 2026-07-07

### Added

- **Secrets, Modal-style.** `rebase.Secret` — a named bundle of environment variables
  attached to deploys with `secrets=[rebase.Secret.from_name("acme-snowflake")]`; every key
  becomes an env var at run time. Build bundles with `Secret.from_name`, `Secret.from_dict`,
  or `Secret.from_dotenv`. Values live in the platform's Secret Manager and are injected by
  Cloud Run — they never pass through the deploy payload or source snapshot.
- **`rebase secret` CLI**: `create NAME KEY=value ... [--from-dotenv FILE] [--force]`
  (use `KEY=-` to read one value from stdin), `list`, and `delete`.
- **`env=` / `secrets=` on functions and models** (previously ASGI apps only), including
  `Predictor` and the other model classes — credentialed code now runs on every backend.
- **`rebase.sources` warehouse connectors** for Snowflake, Databricks, BigQuery, and
  Microsoft Fabric behind per-warehouse extras (`rebase-toolkit[snowflake]`,
  `[databricks]`, `[bigquery]`, `[fabric]`, or `[sources]` for all four): a uniform
  `read` / `read_bitemporal` / `write` surface with a leakage-safe bitemporal mapping
  (`BitemporalSpec`) for honest emflow backtests.
- Client methods `set_secret`, `get_secret`, `delete_secret`, `list_secrets`.

## 0.2.0

Initial public toolkit: projects, functions, workflows, models, endpoints, runs,
images, hillclimb, GitHub and Hugging Face integrations.
