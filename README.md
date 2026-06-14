# Rebase Toolkit

Python client and toolkit for the Rebase Platform.

The toolkit is intentionally small by default: it contains the API-key client, SDK handles for functions and workflows, and optional entry points for Rebase's energy data/modeling packages. It does not run a database or platform backend locally.

## Install

```bash
pip install git+ssh://git@github.com/rebase-energy/rebase-toolkit.git
```

For local development:

```bash
pip install -e ".[dev]"
```

Optional data/modeling packages:

```bash
pip install "rebase-toolkit[data]"
pip install "rebase-toolkit[modeling]"
pip install "rebase-toolkit[all]"
```

## Configure

```bash
export REBASE_API_KEY="rbw_..."
export REBASE_API_URL="https://<your-rebase-platform-api>"
```

You can also configure the client in Python:

```python
import rebase as rb

rb.configure(
    api_key="rbw_...",
    api_url="https://<your-rebase-platform-api>",
)
```

## Minimal Function

```python
import rebase as rb

project = rb.Project("first-user")


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

project = rb.Project("forecasting")


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
