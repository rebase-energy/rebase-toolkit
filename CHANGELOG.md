# Changelog

## Unreleased

### Added

- **`-h` is an alias for `--help`** on every command and subcommand.
- **Short flags for CLI options**: each option now also answers to `-x`, where `x` is the first
  letter of its long name — `rebase deploy -n api -e prod`, `rebase workflow list -j`. 235 of 269
  options got one. The remaining 34 lost the letter to an option declared earlier in the same
  command (`rebase deploy --sync` has no `-s`, because `--source` took it), and `-h` is reserved
  for `--help` throughout. Hand-written short flags are unchanged.

### Fixed

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
