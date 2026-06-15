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
uv pip install "rebase-toolkit[all]"
```

## Configure

```bash
rebase setup
```

The setup command asks for a Rebase API key and stores it in `~/.rebase/config.json`. The hosted Rebase API URL is built into the SDK, so normal user code does not need an API URL or API key argument.

You can select a named local profile when needed:

```bash
rebase setup --profile prod
```

```bash
rebase workspace list
rebase workspace switch prod
```

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

Deploy a file from the command line:

```bash
rebase deploy workflow.py
```

## Dependencies

Function dependencies are declared with a Modal-like image builder:

```python
image = rb.Image.python("3.13").uv_pip_install("boltons==24.0.0")


@project.function(image=image)
def add_with_boltons(a: int = 0, b: int = 0) -> dict:
    from boltons.iterutils import flatten

    return {"sum": sum(flatten([[a], [b]]))}
```

## Data and Modeling Packages

The toolkit keeps Rebase's data/modeling libraries optional:

```python
from rebase import data
from rebase import modeling
```

Install `rebase-toolkit[data]` for `energydatamodel` and `rebase-toolkit[modeling]` for `emflow`.
