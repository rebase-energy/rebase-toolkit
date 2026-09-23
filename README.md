# Rebase Toolkit

Python client and toolkit for the Rebase Platform.

The toolkit is intentionally small by default: it contains the API-key client, SDK handles for functions and workflows, and optional entry points for Rebase's energy data/modeling packages. It does not run a database or platform backend locally.

## Install

During early development, install directly from GitHub into a clean `uv` environment:

```bash
uv venv .venv
source .venv/bin/activate
uv pip install "rebase-toolkit @ git+ssh://git@github.com/rebase-energy/rebase-toolkit.git@master"
```

Install from a branch by replacing `master` with the branch name:

```bash
uv pip install --upgrade "rebase-toolkit @ git+ssh://git@github.com/rebase-energy/rebase-toolkit.git@my-branch"
```

Once the package is published to PyPI:

```bash
pip install rebase-toolkit
```

The `rebase` PyPI project is a compatibility metapackage that installs the
same toolkit:

```bash
pip install rebase
```

When releasing to PyPI, publish `rebase-toolkit` first, then publish the alias
package from `pypi/rebase` with the same version:

```bash
uv build
uv build --project pypi/rebase
```

For local development:

```bash
uv sync --dev
```

To smoke-test a fresh install from this checkout before pushing:

```bash
scripts/smoke_uv_install.sh local
```

To smoke-test a GitHub ref:

```bash
REBASE_TOOLKIT_GIT_REF=master scripts/smoke_uv_install.sh github
```

Optional data/modeling packages:

```bash
uv pip install "rebase-toolkit[data]"
uv pip install "rebase-toolkit[modeling]"
uv pip install "rebase-toolkit[hillclimb]"
uv pip install "rebase-toolkit[all]"
```

`[all]` covers every extra except `[snowflake]`: hillclimb requires pandas 3
and no stable `snowflake-connector-python` allows it yet, so the two cannot
share an environment. Install `rebase-toolkit[snowflake]` on its own.

## Configure

```bash
rebase setup
```

The setup command asks for a Rebase API key and stores it in `~/.rebase/config.json`. The hosted Rebase API URL is built into the SDK, so normal user code does not need an API URL or API key argument.

Setup remembers who you signed in as, so a later run opens with a choice —
continue as that account, or sign in with a different one. Take the second when a
workspace says you were not invited: an invite is granted to one address, and a
GitHub login often reports a private `users.noreply.github.com` address rather than
the one the invite was sent to. Signing in again is also offered at every point
where setup finds no workspace for you, so a mismatch does not mean starting over.
`rebase setup --force-auth` skips straight to a fresh login.

You can select a named local profile when needed:

```bash
rebase setup --profile prod
```

For fast local development against a locally running workflow API, start the API
from the platform checkout and run the editable toolkit setup helper:

```bash
cd /Users/sebaheg/Documents/Github/platform/workflows
./scripts/dev_api.sh

cd /Users/sebaheg/Documents/Github/platform/rebase-toolkit
./scripts/dev_setup_local.sh --print-command
./scripts/dev_setup_local.sh
```

The helper waits for `http://127.0.0.1:18082/health` and then runs:

```bash
uv run rebase setup --force-auth --api-url http://127.0.0.1:18082
```

Common overrides:

```bash
REBASE_DEV_PROFILE=local ./scripts/dev_setup_local.sh
REBASE_DEV_PROVIDER=github ./scripts/dev_setup_local.sh
REBASE_DEV_REPO=sebaheg/toolkit-test REBASE_DEV_GITHUB=1 ./scripts/dev_setup_local.sh
```

For local development against the internal deployed workflow API, port-forward
the API and store that URL in the profile:

```bash
kubectl -n rebase-workflows port-forward svc/workflow-mvp-api 8080:8080
rebase setup --api-url http://127.0.0.1:8080
```

```bash
rebase workspace list
rebase workspace switch prod
```

## Environments and GitOps

Environments are workspace namespaces, not values baked into an app. Projects and
their compute, runs, endpoints, schedules, models, secrets, volumes, and buckets all
live in one environment. The same project name can therefore exist independently in
`dev`, `staging`, a pull-request environment, or `prod`.

Select a default for the current workspace:

```bash
rebase environment create preview
rebase environment use preview
```

Or keep the choice in Python. An explicit environment wins over the ambient context,
which wins over `REBASE_ENVIRONMENT` and the locally selected default:

```python
import rebase as rb

preview = rb.Environment.from_name("preview", create_if_missing=True)

with preview:
    project = rb.project("forecasting")

    @project.function(buckets=["forecasts"])
    def build_forecast() -> dict:
        return {"ok": True}

    project.deploy()
```

Persistent resources use the same context and are isolated by environment:

```python
with rb.Environment.from_name("prod"):
    forecasts = rb.Bucket.from_name("forecasts", create_if_missing=True)
    cache = rb.Volume.from_name("model-cache", create_if_missing=True)
    credentials = rb.Secret.from_name("weather-api")
```

Environment access is explicit when a workspace needs narrower boundaries:

```python
prod = rb.Environment.from_name("prod")
prod.grant(profile_id="PROFILE_UUID", access="read")
prod.grant(api_key_id="API_KEY_UUID", access="write")
```

Protect an environment and bind each of its projects to a Git ref and Python
declaration file:

```bash
rebase environment protect prod --allowed-branch main
rebase environment track-project prod forecasting \
  --connection CONNECTION_ID \
  --ref refs/heads/main \
  --entrypoint deploy.py
```

A signed GitHub push then reconciles the exact merged commit in an isolated job. The
Python file is the desired state—there is no deployment YAML. Objects removed from the
file are pruned from compute after a successful apply; secrets, buckets, volumes, and
their data are retained. Endpoint URLs include the environment, for example
`/e/acme/prod/forecasting/predict`.

## Minimal Function

```python
import rebase as rb

project = rb.project("first-user")


@project.function()
def add(a: int = 0, b: int = 0) -> dict:
    return {"sum": a + b}


project.deploy()

run = add.spawn(a=2, b=3)
print(run.result(timeout=120))
```

Functions default to `mode="interactive", isolation="shared"` for the lowest-latency cloud
loop. Use `@project.function(isolation="dedicated")` for a private warm Cloud Run service or
`@project.function(mode="job")` for a fresh, cancellable Cloud Run Job execution.

## Minimal Workflow

```python
import rebase as rb

project = rb.project("forecasting")


@project.step()
def load_weather(site_id: str) -> dict:
    return {"site_id": site_id}


@project.step()
def build_forecast(weather: dict, horizon_hours: int = 24) -> dict:
    return {"weather": weather, "horizon_hours": horizon_hours}


@project.workflow()
def forecast(site_id: str = "site-001", horizon_hours: int = 24) -> dict:
    weather = load_weather(site_id)
    return build_forecast(weather, horizon_hours=horizon_hours)


project.deploy()
print(forecast.remote(site_id="site-001"))
```

## Tasks

Use `rb.task` to report named units of work inside a function or workflow. The
body still runs inline in the current process; a task adds status visibility, not
new compute or retries.

```python
@project.workflow()
def capture(datasets: list[str]) -> dict:
    failed = []
    for dataset in datasets:
        try:
            with rb.task(
                f"Capture {dataset}",
                key=dataset,
                parameters={"dataset": dataset},
            ) as task:
                rows = fetch_and_store(dataset)
                task.set_result({"outcome": "captured", "rows": rows})
        except Exception as exc:
            failed.append({"dataset": dataset, "error": str(exc)})
    return {"failed": failed}
```

Entering the context reports `running`; a normal exit reports `succeeded`, and
an exception reports `failed` before being re-raised. Catch outside the context
when later tasks should continue. `set_result()` is optional and records a
JSON-object domain result; lifecycle status stays `succeeded` for outcomes such
as skipped or pending. Outside a hosted Rebase run the same context manager is a
transparent in-memory no-op, which keeps local execution ordinary Python.

## Artifacts

Use `rb.artifact` after writing a durable output to register its URI on the
current run. Rebase records the pointer and metadata; it does not upload or copy
the object.

```python
@project.workflow()
def capture(dataset: str = "SE3") -> dict:
    uri = write_curve_to_gcs(dataset)
    artifact = rb.artifact(
        f"Day-ahead curve {dataset}",
        uri=uri,
        key=f"day-ahead/{dataset}",
        disposition="created",
        media_type="application/json",
        size_bytes=42_018,
        version="1741632447112345",
        digest="md5:8d777f385d3dfec8815d20f7496026dc",
        metadata={"dataset": dataset, "points": 96},
    )
    return {"artifact_id": artifact.id, "uri": artifact.uri}
```

Use `disposition="reused"` when the workflow found an existing output instead
of creating it. A `key` identifies the logical output within the run, making a
retry with the same key and URI idempotent. Artifacts created inside `rb.task`
or a `Function.map` item are attributed to that task automatically and are also
listed on the parent workflow run. In a hosted run registration is strict: a
reporting failure raises `ArtifactReportingError`, so a successful upload is not
silently omitted from the run record. Local calls validate their arguments and
otherwise remain in-memory no-ops.

The TUI lists artifacts under `[ Artifacts ]`; select one and press `a` to
resolve and open its current location.

Deploy a file from the command line:

```bash
rebase deploy workflow.py
```

The CLI prints a final deployment report, including when a deployment stops
partway. Each selected function, workflow, app, or model is marked `succeeded`,
`failed`, `uncertain`, or `unattempted`; shared steps have their own rows.
Counts describe the last outcome for each target in this invocation. Success
means the control plane acknowledged the definition, not that a runtime is
ready. Deployment still stops at the first error, and an incomplete CLI deploy
exits nonzero (1 for deployment errors, 130 for interruption).

Within one deployment attempt, the SDK caches project/function/workflow lookups
and secret references. Shared steps with the same resolved definition are written
once; changed definitions are written again. Caches are discarded when the attempt
ends, and uncertain-write reconciliation always reads fresh API state.

Workflow creation and updates, and function writes during deployment, use a
300-second HTTP timeout. During deployment, reads retry transient connection/
timeout failures and HTTP 429/500/502/503/504 responses up to three attempts with
bounded backoff. Writes retry only connection timeouts
that occur before a connection is established. After an ambiguous workflow or function
write, the SDK probes the deployed definition up to three times, checking the
resource and its pinned version together. It accepts success only if
all submitted definition fields match after accounting for the API's Python
image and job-isolation defaults, and the environment selector agrees. Otherwise
the outcome stays `uncertain`. An absent workflow is not proof that a timed-out create failed.
Build submissions cannot be confirmed from the definition alone.

SDK deploy methods keep their existing return values and expose a
`deployment_report` after an attempt. Deployment errors subclass
`RebaseWorkflowError` and carry the same report:

```python
try:
    project.deploy()
except rb.DeploymentError as exc:
    print(exc.report.counts)
    for result in exc.report.results:
        print(result.project, result.name, result.status, result.error)
    raise
else:
    print(project.deployment_report.counts)
```

Deployments are not atomic: successful earlier writes remain in place after
a failure. Reruns still redeploy unchanged targets. Inspect uncertain outcomes
before retrying; there is no blanket retry of workflow creation or run submission.

Run a function from local source and force the interactive backend:

```bash
rebase run functions.py::add --backend interactive --param a=2 --param b=3
```

If the file contains exactly one Rebase function, the function name can be omitted:

```bash
rebase run functions.py --parameters-json '{"a": 2, "b": 3}'
```

## Models

`rebase.Model` is the shared base for model metadata and deployment config. Deployable models use typed emflow-style
subclasses such as `rebase.Predictor`, `rebase.Optimizer`, and `rebase.Agent`.

```python
import rebase


class PriceForecastPredictor(rebase.Predictor):
    name = "price-forecast"

    def predict(self, zone: str = "SE3", horizon_hours: int = 24) -> dict:
        return {"zone": zone, "horizon_hours": horizon_hours}


model = PriceForecastPredictor()
rebase.deploy(model)
```

Call a deployed model through its generated `predict` endpoint:

```python
model = rebase.get_predictor("default/price-forecast")
result = model.predict.remote(zone="SE4")
```

## Hillclimb Searches

`rebase hillclimb` runs agentic model searches with
[hillclimb](https://github.com/rebase-energy/hillclimb): coding
agents draft, debug, improve, and ensemble
[emflow](https://github.com/rebase-energy/emflow) `Predictor` classes; every
candidate is backtested leakage-safe on the problem's validation split and the
winner is selected on a hidden holdout. Requires the `hillclimb` extra.

Prepare a repository for local search state and discover installed problem
targets without switching to the standalone Hillclimb CLI:

```bash
rebase hillclimb init
rebase hillclimb problems gefcom2014
```

Start a search — hosted on the platform by default (a long-running Cloud Run
job), or on your own machine with `--local`:

```bash
rebase hillclimb start emflow://gefcom2014:solar --budget 2h
rebase hillclimb start emflow://gefcom2014:solar --budget 2h --parallel-searches 3 --parallel-operators 2
rebase hillclimb start emflow://gefcom2014:solar --budget 2h --local
```

A hosted run can carry several independent searches (`--parallel-searches`),
each its own engine, sharing what they learn as they go; `--parallel-operators`
is how many agents each search keeps busy. The platform sizes the job from
that shape. `--policy` picks the search engine (`greedy`, `openevolve`, `gepa`).

Hidden holdout selection is enabled by default. For public-data plumbing checks
where private holdout credentials are intentionally unavailable, pass
`--no-holdout`; do not use that mode to select a model for promotion.

Any problem in emflow's registry is a valid target (`emflow://<name>`), as are
plain hillclimb problem folders. `--backend dummy` runs the search loop
without agent calls (smoke tests).

Watch a hosted search in hillclimb's own TUIs — the same screens as
`hillclimb watch`, fed by a live mirror the platform streams to your laptop —
or read it as text:

```bash
rebase hillclimb watch <run-id>    # runs → searches → candidates; s = stop, x = prune
rebase hillclimb chart <run-id>    # the hillclimb: best score over time
rebase hillclimb tree <run-id>     # one search's exploration tree
rebase hillclimb graph <run-id>    # the knowledge graph
rebase hillclimb status <run-id>   # candidates, best score, budget left
rebase hillclimb logs <run-id>     # engine logs
rebase hillclimb stop <run-id>     # graceful: parks after the current operator
rebase hillclimb list
```

Without a run id, `watch`, `chart`, `tree` and `graph` open the local
hillclimb dir, exactly like the standalone commands.

When the search finishes, promote the selected model into your workspace repo
as versioned source, then deploy it like any other model:

```bash
rebase hillclimb promote <run-id>            # writes models/<problem_id>.py
# review, commit, open a PR (protected environments deploy through gitops)
rebase model deploy models/gefcom2014_solar.py
```

The promoted file exposes `get_model() -> emflow.Predictor` — the same class
that won the backtest is what serves in production.

Hosted agents bill the workspace secret named `hillclimb` when it exists —
a `CLAUDE_CODE_OAUTH_TOKEN` for subscription billing (mint one with
`claude setup-token`) or an `ANTHROPIC_API_KEY` — else the platform's own
credentials; `--claude-secret` names a different bundle:

```bash
claude setup-token | rebase secret create hillclimb CLAUDE_CODE_OAUTH_TOKEN=-
```

`--local` searches use your local Claude login. Hosted search state is served
by the platform (`/runs/{id}/hillclimb/...`); nothing on your machine needs
Google credentials or a bucket name. Server-side requirements are documented
in `platform/toolkit/HILLCLIMB.md`.

## Stitching and Forecast Windows

`rebase.ForecastWindow` is the standard vocabulary for a forecast run's target
range: offsets relative to an issue time, in ISO-8601 durations (the compact
`"45m"`/`"2h"` style also works). It survives the JSON round-trip through
workflow parameters, and scheduled runs resolve it against `ctx.fired_at` so
replays reproduce the original window:

```python
import rebase as rb
from datetime import UTC, datetime

project = rb.project("forecasting")


@project.workflow(schedule=rb.Cron("0 * * * *"))
def forecast(ctx=None, window=rb.ForecastWindow(start="PT1H", end="P10D")) -> dict:
    window = rb.ForecastWindow.coerce(window)
    start, end = window.resolve(ctx.fired_at if ctx and ctx.fired_at else datetime.now(UTC))
    ...
```

`rebase.stitch` composes prioritised time series layers (pandas required):
the first layer whose window covers a timestamp and whose value is non-null
wins, lower layers only fill the gaps. Windows are `[start, end)` offsets
relative to the issue time, or absolute timezone-aware datetimes.
`rebase.Exclude` blocks fallback inside a window — deliberate nulls that lower
layers must not fill (e.g. masking a storm week out of training data):

```python
combined = rb.stitch(
    [
        rb.Layer(forecast_df, start="PT0H", name="forecast"),
        rb.Layer(history_df, end="PT0H", name="history"),
        rb.Exclude(start=uri_start, end=uri_end),
        rb.Layer(climatology_df, name="climatology"),
    ],
    issue_time=ctx.fired_at,
)

combined, sources = rb.stitch([...], issue_time=..., return_sources=True)
```

## HTTP Endpoints

`rb.endpoint` gives a deployed function, model, or workflow a stable HTTP route.
Auth defaults to `api_key`, so endpoints are not public unless you say so
(`auth` also accepts `"workspace"` and `"public"`):

```python
@rb.endpoint(method="POST", path="/forecast")
@rb.function(project="forecasting")
def forecast(zone: str = "SE3") -> dict:
    return {"zone": zone}
```

```bash
rebase api-key create forecast-agent
curl -X POST "$ENDPOINT_URL" -H "Authorization: Bearer rb_..." \
  -H "Content-Type: application/json" -d '{"zone": "SE3"}'
```

## ASGI Apps

`rb.asgi_app` deploys a whole FastAPI/Starlette app instead of a single route.
The decorated function builds and returns the app:

```python
image = rb.Image.python("3.12").uv_pip_install("fastapi==0.141.1")


@rb.asgi_app(project="grid", name="grid-api", image=image)
def grid_api():
    from fastapi import FastAPI

    web_app = FastAPI()

    @web_app.get("/zones/{zone}")
    def read_zone(zone: str):
        return {"zone": zone}

    return web_app


rb.deploy(grid_api)
```

Two things to know when writing the app function, because its source is shipped
and re-executed remotely:

- Import inside the function — module-scope imports must also resolve on the
  machine running `deploy`.
- FastAPI resolves annotations against module globals, so a name imported inside
  the function is invisible to it. `Header`/`Query`/`Body` parameters with plain
  types work; a `request: Request` parameter is silently read as a query
  parameter instead.

If the app does its own auth (`auth="public"`), carry the credential in a header
other than `Authorization`: Rebase invokes the app with its own Google service
account, whose token occupies `Authorization`. FastAPI's `HTTPBearer` and
`OAuth2PasswordBearer` read that header and would validate the platform's token
rather than your caller's. See
[ASGI Apps](https://rebase-platform-docs.fly.dev/docs/documentation/asgi-apps).

## Dependencies

Function dependencies are declared with a Modal-like image builder:

```python
image = rb.Image.python("3.13").uv_pip_install("boltons==24.0.0")


@project.function(image=image)
def add_with_boltons(a: int = 0, b: int = 0) -> dict:
    from boltons.iterutils import flatten

    return {"sum": sum(flatten([[a], [b]]))}
```

For a larger codebase, opt into a built image. Rebase automatically bundles the
module or package that defines the decorated target; additional local packages
and files are explicit, as in Modal:

```python
image = (
    rb.Image.python("3.13")
    .uv_sync(".", frozen=True)
    .add_local_python_source("agent_work")
    .add_local_dir("config", "/workspace/config")
)


@project.workflow(image=image, mode="job")
def sync_accounting():
    from agent_work.accounting.pipeline import run

    return run()
```

`uv_sync()` requires both `pyproject.toml` and `uv.lock`. `add_local_file()`,
`add_local_dir()`, and `add_local_python_source()` use a read-only,
content-addressed source mount by default. Set `copy=True` to bake that input
into the immutable OCI image instead. `.gitignore`, `.rebaseignore`, and an
explicit `ignore=` list are applied when directories are bundled; symlinks are
rejected.

Built images support Python 3.12 and 3.13. They run in dedicated functions,
function jobs, ASGI apps, and job-mode workflows; the shared function runner
and interactive workflows cannot switch images per invocation. `deploy()`
uploads the deterministic bundle, waits for the cached Cloud Build result, and
pins the deployed version to both the OCI image digest and source-bundle
digest. Legacy images that only use `uv_pip_install()` retain the existing
runtime-install behavior.

## Data and Modeling Packages

The toolkit can expose optional `emflow` and EnergyDataModel modules through:

```python
from rebase import data
from rebase import modeling
```

Install `rebase-toolkit[modeling]` to use `emflow` through `rebase.modeling`.
Install `rebase-toolkit[data]` to import `energydatamodel` through `rebase.data`.
