# Changelog

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
