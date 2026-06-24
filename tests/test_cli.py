import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from rebase.auth import AuthSession
from rebase.cli import _format_duration, deploy_file, main
from rebase.client import ASGIApp, Client, Function, Model, Project, Run, Workflow


def test_main_without_args_prints_help(capsys) -> None:
    assert main([]) == 0

    output = capsys.readouterr().out
    normalized_output = " ".join(output.split())
    assert "Usage:" in output
    assert (
        "Rebase Toolkit lets you develop Python workflows and models "
        "that can then be deployed to the Rebase Platform."
    ) in normalized_output
    assert "setup" in output
    assert "workspace" in output
    assert "project" in output
    assert "endpoint" in output
    assert "function" in output
    assert "workflow" in output
    assert "deploy" in output
    assert "inspect submitted runs" in output
    command_order = [
        "api-key",
        "connect",
        "deploy",
        "endpoint",
        "function",
        "model",
        "project",
        "run",
        "setup",
        "tui",
        "workflow",
        "workspace",
    ]
    positions = [output.index(f"│ {command}") for command in command_order]
    assert positions == sorted(positions)


def test_command_group_help_sorts_commands(capsys) -> None:
    assert main(["endpoint", "--help"]) == 0

    output = capsys.readouterr().out
    command_order = ["disable", "get", "invoke", "list", "versions"]
    positions = [output.index(f"│ {command}") for command in command_order]
    assert positions == sorted(positions)


def test_run_help_shows_execution_and_inspection_commands(capsys) -> None:
    assert main(["run", "--help"]) == 0

    output = capsys.readouterr().out
    assert "Usage: rebase run [OPTIONS] TARGET_REF" in output
    assert "--param, -p" in output
    assert "--parameters-json" in output
    assert "--backend" in output
    assert "--module, -m" in output
    assert "--wait / --no-wait" in output
    assert "Inspection Commands" in output
    command_order = ["cancel", "get", "list", "logs"]
    positions = [output.index(command) for command in command_order]
    assert positions == sorted(positions)
    assert "list" in output
    assert "get" in output
    assert "logs" in output
    assert "cancel" in output


def test_tui_command_invokes_textual_app(monkeypatch) -> None:
    observed: dict[str, Any] = {}

    def fake_run_tui(*, project: str | None = None, limit: int = 25, client: Client | None = None) -> None:
        observed["project"] = project
        observed["limit"] = limit
        observed["client"] = client

    monkeypatch.setattr("rebase.tui.run_tui", fake_run_tui)

    assert main(["tui", "--project", "energy", "--limit", "10"]) == 0
    assert observed == {"project": "energy", "limit": 10, "client": None}


def test_format_duration_uses_two_decimal_places() -> None:
    assert _format_duration(0) == "0.00"
    assert _format_duration(1.234) == "1.23"
    assert _format_duration(12.999) == "13.00"


def test_deploy_file_deploys_top_level_project(monkeypatch, tmp_path: Path) -> None:
    deployed: list[str] = []

    def fake_project_deploy(self: Project, *, replace: bool = False) -> Project:
        deployed.append(self.name)
        self.id = f"{self.name}-id"
        return self

    monkeypatch.setattr(Project, "deploy", fake_project_deploy)
    workflow_file = tmp_path / "workflow.py"
    workflow_file.write_text(
        """
import rebase as rb

project = rb.Project("energy-forecasting")

@project.step()
def load_weather(site_id: str) -> dict:
    return {"site_id": site_id}

@project.workflow()
def forecast(site_id: str) -> dict:
    return {"weather": load_weather(site_id)}
""",
        encoding="utf-8",
    )

    result = deploy_file(workflow_file)

    assert deployed == ["energy-forecasting"]
    assert result == [("project", "energy-forecasting", "energy-forecasting-id")]


def test_deploy_file_can_filter_by_project_name(monkeypatch, tmp_path: Path) -> None:
    deployed: list[str] = []

    def fake_project_deploy(self: Project, *, replace: bool = False) -> Project:
        deployed.append(self.name)
        self.id = f"{self.name}-id"
        return self

    monkeypatch.setattr(Project, "deploy", fake_project_deploy)
    workflow_file = tmp_path / "workflow.py"
    workflow_file.write_text(
        """
import rebase as rb

first = rb.Project("first")
second = rb.Project("second")
""",
        encoding="utf-8",
    )

    result = deploy_file(workflow_file, object_names=["second"])

    assert deployed == ["second"]
    assert result == [("project", "second", "second-id")]


def test_deploy_file_passes_source_override(monkeypatch, tmp_path: Path) -> None:
    observed: dict[str, Any] = {}

    def fake_project_deploy(
        self: Project,
        *,
        replace: bool = False,
        deploy_source: str | None = None,
    ) -> Project:
        observed["name"] = self.name
        observed["deploy_source"] = deploy_source
        self.id = "project-id"
        return self

    monkeypatch.setattr(Project, "deploy", fake_project_deploy)
    workflow_file = tmp_path / "workflow.py"
    workflow_file.write_text(
        """
import rebase as rb

project = rb.Project("energy-forecasting")
""",
        encoding="utf-8",
    )

    result = deploy_file(workflow_file, deploy_source="github")

    assert observed == {"name": "energy-forecasting", "deploy_source": "github"}
    assert result == [("project", "energy-forecasting", "project-id")]


def test_deploy_file_deploys_standalone_workflow(monkeypatch, tmp_path: Path) -> None:
    deployed: list[str] = []

    def fake_workflow_deploy(self: Workflow, *, replace: bool = False) -> Workflow:
        deployed.append(str(self.name))
        self.id = f"{self.name}-id"
        return self

    monkeypatch.setattr(Workflow, "deploy", fake_workflow_deploy)
    workflow_file = tmp_path / "workflow.py"
    workflow_file.write_text(
        """
import rebase as rb

def forecast(site_id: str) -> dict:
    return {"site_id": site_id}

workflow = rb.Workflow(forecast, name="site-forecast")
""",
        encoding="utf-8",
    )

    result = deploy_file(workflow_file)

    assert deployed == ["site-forecast"]
    assert result == [("workflow", "site-forecast", "site-forecast-id")]


def test_deploy_file_deploys_standalone_asgi_app(monkeypatch, tmp_path: Path) -> None:
    deployed: list[str] = []

    def fake_asgi_app_deploy(self: ASGIApp, *, replace: bool = False) -> ASGIApp:
        deployed.append(str(self.name))
        self.id = f"{self.name}-id"
        self.data = {"url": "https://workflows.example.com/e/default/grid/api"}
        return self

    monkeypatch.setattr(ASGIApp, "deploy", fake_asgi_app_deploy)
    api_file = tmp_path / "api.py"
    api_file.write_text(
        """
import rebase as rb

@rb.asgi_app(project="grid", name="grid-api", base_path="/api")
def grid_api() -> object:
    return object()
""",
        encoding="utf-8",
    )

    result = deploy_file(api_file)

    assert deployed == ["grid-api"]
    assert result == [
        ("asgi_app", "grid-api", "grid-api-id", "https://workflows.example.com/e/default/grid/api")
    ]


def test_deploy_file_deploys_standalone_predictor(monkeypatch, tmp_path: Path) -> None:
    deployed: list[str] = []

    def fake_model_deploy(self: Model, *, replace: bool = False) -> Model:
        deployed.append(str(self.name))
        self.id = f"{self.name}-id"
        return self

    monkeypatch.setattr(Model, "deploy", fake_model_deploy)
    model_file = tmp_path / "model.py"
    model_file.write_text(
        """
import rebase as rb

class PriceForecastPredictor(rb.Predictor):
    name = "price-forecast"

    def predict(self, zone: str = "SE3") -> dict:
        return {"zone": zone}

model = PriceForecastPredictor()
""",
        encoding="utf-8",
    )

    result = deploy_file(model_file)

    assert deployed == ["price-forecast"]
    assert result == [("predictor", "price-forecast", "price-forecast-id")]


def test_deploy_file_rejects_plain_model(tmp_path: Path, capsys) -> None:
    model_file = tmp_path / "model.py"
    model_file.write_text(
        """
import rebase as rb

class BaseEnergyModel(rb.Model):
    name = "base-energy-model"

model = BaseEnergyModel()
""",
        encoding="utf-8",
    )

    assert main(["deploy", str(model_file)]) == 1
    assert "rebase.Model is not directly deployable" in capsys.readouterr().err


def test_main_prints_deployed_targets(monkeypatch, tmp_path: Path, capsys) -> None:
    workflow_file = tmp_path / "workflow.py"
    workflow_file.write_text("import rebase as rb\nproject = rb.Project('energy-forecasting')\n", encoding="utf-8")

    def fake_project_deploy(self: Project, *, replace: bool = False) -> Project:
        self.id = "project-id"
        return self

    monkeypatch.setattr(Project, "deploy", fake_project_deploy)

    assert main(["deploy", str(workflow_file)]) == 0

    output = capsys.readouterr().out
    assert "Deployed Targets" in output
    assert "project" in output
    assert "energy-forecasting" in output
    assert "project-id" in output


def test_run_command_runs_function_on_cloud_run_by_default(monkeypatch, tmp_path: Path, capsys) -> None:
    function_file = tmp_path / "functions.py"
    function_file.write_text(
        """
import rebase as rb

@rb.function(project="math", name="add")
def add(a: int, b: int) -> dict:
    return {"sum": a + b}
""",
        encoding="utf-8",
    )
    observed: dict[str, Any] = {}

    def fake_deploy(self: Function, *, replace: bool = False) -> Function:
        raise AssertionError("rebase run should not deploy")

    def fake_run_ephemeral(self: Client, **kwargs: Any) -> Run:
        observed.update(kwargs)
        return Run("run-id", client=self)

    monkeypatch.setattr(Function, "deploy", fake_deploy)
    monkeypatch.setattr(Client, "run_ephemeral", fake_run_ephemeral)
    monkeypatch.setattr(Client, "list_run_events", lambda self, run_id: [])
    monkeypatch.setattr(
        Client,
        "get_run",
        lambda self, run_id: {"id": run_id, "status": "succeeded", "result": {"sum": 3}},
    )

    assert (
        main(
            [
                "run",
                f"{function_file}::add",
                "--param",
                "a=1",
                "--param",
                "b=2",
            ]
        )
        == 0
    )

    assert observed == {
        "target_type": "function",
        "project": "math",
        "name": "add",
        "entrypoint": "add",
        "execution_backend": "cloud_run",
        "parameters": {"a": 1, "b": 2},
        "default_parameters": {},
        "image_spec": {"kind": "python", "python_version": "3.13", "uv_pip_packages": [], "uv_version": None},
        "source_code": observed["source_code"],
        "cloud_run_min_instances": None,
        "cloud_run_concurrency": None,
    }
    assert "def add(a: int, b: int) -> dict:" in observed["source_code"]
    assert '"sum": 3' in capsys.readouterr().out


def test_run_command_runs_model_as_ephemeral_model(monkeypatch, tmp_path: Path, capsys) -> None:
    model_file = tmp_path / "model.py"
    model_file.write_text(
        """
import rebase as rb

class PriceForecastPredictor(rb.Predictor):
    name = "price-forecast"

    def predict(self, zone: str = "SE3") -> dict:
        return {"zone": zone}

model = PriceForecastPredictor()
""",
        encoding="utf-8",
    )
    observed: dict[str, Any] = {}

    def fake_run_ephemeral(self: Client, **kwargs: Any) -> Run:
        observed.update(kwargs)
        return Run("run-id", client=self)

    monkeypatch.setattr(Function, "deploy", lambda self, **kwargs: (_ for _ in ()).throw(AssertionError()))
    monkeypatch.setattr(Client, "run_ephemeral", fake_run_ephemeral)
    monkeypatch.setattr(Client, "list_run_events", lambda self, run_id: [])
    monkeypatch.setattr(
        Client,
        "get_run",
        lambda self, run_id: {"id": run_id, "status": "succeeded", "result": {"zone": "SE4"}},
    )

    assert main(["run", str(model_file), "--param", "zone=SE4"]) == 0

    assert observed["target_type"] == "model"
    assert observed["project"] == "default"
    assert observed["name"] == "price-forecast"
    assert observed["entrypoint"] == "predict"
    assert observed["parameters"] == {"zone": "SE4"}
    assert observed["default_parameters"] == {"zone": "SE3"}
    assert any("emflow" in package for package in observed["image_spec"]["uv_pip_packages"])
    assert "class PriceForecastPredictor(rb.Predictor):" in observed["source_code"]
    assert '"zone": "SE4"' in capsys.readouterr().out


def test_run_command_can_submit_without_waiting(monkeypatch, tmp_path: Path, capsys) -> None:
    function_file = tmp_path / "functions.py"
    function_file.write_text(
        """
import rebase as rb

@rb.function(project="math", name="add", backend="cloud_run")
def add(a: int, b: int) -> dict:
    return {"sum": a + b}
""",
        encoding="utf-8",
    )

    def fake_deploy(self: Function, *, replace: bool = False) -> Function:
        raise AssertionError("rebase run should not deploy")

    def fake_run_ephemeral(self: Client, **kwargs: Any) -> Run:
        return Run("run-id", client=self, data={"id": "run-id", "status": "submitted"})

    monkeypatch.setattr(Function, "deploy", fake_deploy)
    monkeypatch.setattr(Client, "run_ephemeral", fake_run_ephemeral)

    assert main(["run", str(function_file), "--parameters-json", '{"a": 1, "b": 2}', "--no-wait"]) == 0

    output = capsys.readouterr().out
    assert "Rebase Run" in output
    assert "run-id" in output
    assert "submitted" in output


def test_run_command_runs_workflow_ephemerally(monkeypatch, tmp_path: Path, capsys) -> None:
    workflow_file = tmp_path / "workflow.py"
    workflow_file.write_text(
        """
import rebase as rb

project = rb.project("hello")
step = project.step
workflow = project.workflow

@step()
def load_name(name: str = "World") -> dict:
    return {"name": name}

@step()
def package(payload: dict) -> dict:
    return {"message": f"Hello, {payload['name']}!"}

@workflow(name="hello-workflow")
def hello_workflow(name: str = "World") -> dict:
    return package(load_name(name))
""",
        encoding="utf-8",
    )
    observed: dict[str, Any] = {}

    def fake_run_ephemeral(self: Client, **kwargs: Any) -> Run:
        observed.update(kwargs)
        return Run("run-id", client=self)

    monkeypatch.setattr(Client, "run_ephemeral", fake_run_ephemeral)
    monkeypatch.setattr(
        Client,
        "list_run_events",
        lambda self, run_id: [
            {"id": "event-1", "status": "completed", "message": "Accepted run request."},
            {"id": "event-2", "status": "running", "message": "Submitting workflow run to Prefect Cloud Run Service."},
            {"id": "event-3", "status": "completed", "message": "Prefect Cloud Run Service accepted the workflow run."},
        ],
    )
    monkeypatch.setattr(
        Client,
        "list_run_steps",
        lambda self, run_id: [
            {"id": "step-1", "name": "load-name", "status": "succeeded"},
            {"id": "step-2", "name": "package", "status": "succeeded"},
        ],
    )
    monkeypatch.setattr(
        Client,
        "get_run",
        lambda self, run_id: {"id": run_id, "status": "succeeded", "result": {"message": "Hello, Rebase!"}},
    )

    assert main(["run", str(workflow_file), "--param", 'name="Rebase"']) == 0

    assert observed["target_type"] == "workflow"
    assert observed["project"] == "hello"
    assert observed["name"] == "hello-workflow"
    assert observed["parameters"] == {"name": "Rebase"}
    assert observed["execution_backend"] == "prefect_cloud_run_service"
    assert [node["name"] for node in observed["step_graph"]["nodes"]] == ["load-name", "package"]
    assert all(node["source_code"] for node in observed["step_graph"]["nodes"])
    output = capsys.readouterr().out
    assert "Accepted run request." in output
    assert "Prefect Cloud Run Service accepted the workflow run." in output
    assert "Step load-name completed." in output
    assert "Step package completed." in output
    assert "Run completed in " in output
    assert '"Hello, Rebase!"' in output


def test_run_list_command_renders_runs(monkeypatch, capsys) -> None:
    monkeypatch.setattr(Client, "list_projects", lambda self: [{"id": "project-id", "name": "default"}])
    monkeypatch.setattr(
        Client,
        "list_runs",
        lambda self, **kwargs: [
            {
                "id": "run-id",
                "project_id": "project-id",
                "target_type": "workflow",
                "status": "succeeded",
                "execution_backend": "prefect_cloud_run_service",
                "created_at": "2026-06-16T10:00:00Z",
                "finished_at": "2026-06-16T10:00:05Z",
            }
        ],
    )

    assert main(["run", "list"]) == 0

    output = capsys.readouterr().out
    assert "Runs" in output
    assert "run-id" in output
    assert "workflow" in output
    assert "default" in output


def test_run_list_command_can_print_json(monkeypatch, capsys) -> None:
    monkeypatch.setattr(Client, "list_projects", lambda self: (_ for _ in ()).throw(AssertionError()))
    monkeypatch.setattr(
        Client,
        "list_runs",
        lambda self, **kwargs: [{"id": "run-id", "status": "queued"}],
    )

    assert main(["run", "list", "--json"]) == 0

    output = capsys.readouterr().out
    assert '"id": "run-id"' in output
    assert '"status": "queued"' in output


def test_run_get_command_renders_detail(monkeypatch, capsys) -> None:
    monkeypatch.setattr(
        Client,
        "get_run",
        lambda self, run_id: {
            "id": run_id,
            "target_type": "function",
            "status": "succeeded",
            "execution_backend": "cloud_run",
            "result": {"sum": 3},
            "timings": {"backend_execution_seconds": 1.2},
        },
    )

    assert main(["run", "get", "run-id"]) == 0

    output = capsys.readouterr().out
    assert "Run" in output
    assert "run-id" in output
    assert "cloud_run" in output
    assert "backend_execution_seconds" in output


def test_run_logs_command_renders_events_and_steps_without_following(monkeypatch, capsys) -> None:
    monkeypatch.setattr(
        Client,
        "get_run",
        lambda self, run_id: {"id": run_id, "target_type": "workflow", "status": "succeeded", "result": {"ok": True}},
    )
    monkeypatch.setattr(
        Client,
        "list_run_events",
        lambda self, run_id: [{"id": "event-id", "status": "completed", "message": "Accepted run request."}],
    )
    monkeypatch.setattr(
        Client,
        "list_run_steps",
        lambda self, run_id: [{"id": "step-id", "name": "load-name", "status": "succeeded"}],
    )

    assert main(["run", "logs", "run-id", "--no-follow"]) == 0

    output = capsys.readouterr().out
    assert "Accepted run request." in output
    assert "Step load-name completed." in output
    assert "Run completed." in output


def test_run_logs_command_can_print_json(monkeypatch, capsys) -> None:
    monkeypatch.setattr(
        Client,
        "get_run",
        lambda self, run_id: {"id": run_id, "target_type": "function", "status": "running"},
    )
    monkeypatch.setattr(Client, "list_run_events", lambda self, run_id: [{"id": "event-id"}])
    monkeypatch.setattr(Client, "list_run_steps", lambda self, run_id: (_ for _ in ()).throw(AssertionError()))

    assert main(["run", "logs", "run-id", "--json"]) == 0

    output = capsys.readouterr().out
    assert '"run"' in output
    assert '"events"' in output
    assert '"steps": []' in output


def test_run_cancel_command_is_explicitly_unsupported(capsys) -> None:
    assert main(["run", "cancel", "run-id"]) == 1

    output = capsys.readouterr().err
    assert "run cancellation is not supported yet: run-id" in output


def test_setup_stores_api_key(monkeypatch, tmp_path: Path, capsys) -> None:
    config_path = tmp_path / "config.json"
    monkeypatch.setenv("REBASE_CONFIG_PATH", str(config_path))
    monkeypatch.setattr(Client, "get_workspace", lambda self: {"id": "workspace-id", "name": "ACME"})

    assert main(["setup", "--profile", "test", "--api-key", "rbw_secret"]) == 0

    data = json.loads(config_path.read_text(encoding="utf-8"))
    assert data == {
        "default_profile": "test",
        "profiles": {
            "test": {
                "api_key": "rbw_secret",
                "workspace_id": "workspace-id",
                "workspace_name": "ACME",
            }
        },
    }
    output = capsys.readouterr().out
    assert "Saved Rebase credentials for profile 'test'" in output
    assert str(config_path) in output


def test_setup_stores_api_url(monkeypatch, tmp_path: Path) -> None:
    config_path = tmp_path / "config.json"
    monkeypatch.setenv("REBASE_CONFIG_PATH", str(config_path))
    monkeypatch.setattr(Client, "get_workspace", lambda self: {"id": "workspace-id", "name": "ACME"})

    assert (
        main(
            [
                "setup",
                "--profile",
                "local",
                "--api-key",
                "rbw_secret",
                "--api-url",
                "http://127.0.0.1:8080/",
            ]
        )
        == 0
    )

    data = json.loads(config_path.read_text(encoding="utf-8"))
    assert data["profiles"]["local"]["api_key"] == "rbw_secret"
    assert data["profiles"]["local"]["api_url"] == "http://127.0.0.1:8080"


def test_setup_can_skip_verification(monkeypatch, tmp_path: Path) -> None:
    config_path = tmp_path / "config.json"
    monkeypatch.setenv("REBASE_CONFIG_PATH", str(config_path))

    assert main(["setup", "--api-key", "rbw_secret", "--no-verify"]) == 0

    data = json.loads(config_path.read_text(encoding="utf-8"))
    assert data["profiles"]["default"]["api_key"] == "rbw_secret"


def test_setup_wizard_stores_supabase_profile(monkeypatch, tmp_path: Path, capsys) -> None:
    config_path = tmp_path / "config.json"
    monkeypatch.setenv("REBASE_CONFIG_PATH", str(config_path))

    class FakeClient:
        def __init__(
            self,
            *,
            api_key: str | None = None,
            api_url: str | None = None,
            profile: str | None = None,
            access_token: str | None = None,
        ) -> None:
            self.api_key = api_key
            self.api_url = (api_url or "https://workflows.example.com").rstrip("/")
            self.profile = profile
            self.access_token = access_token

        def setup_config(self) -> dict[str, Any]:
            return {
                "supabase_url": "https://project.supabase.co",
                "supabase_anon_key": "anon",
                "github_app_configured": False,
            }

        def list_my_workspaces(self) -> list[dict[str, Any]]:
            return [{"id": "default", "name": "Default", "default": True}]

    monkeypatch.setattr("rebase.setup.Client", FakeClient)
    monkeypatch.setattr("rebase.setup.load_access_token", lambda: "access-token")
    monkeypatch.setattr(
        "rebase.setup.load_session",
        lambda: AuthSession(
            access_token="access-token",
            refresh_token="refresh-token",
            expires_at=2_000_000_000,
            token_type="bearer",
            email="sebastian@rebase.energy",
        ),
    )

    assert (
        main(
            [
                "setup",
                "--profile",
                "local",
                "--api-url",
                "https://workflows.example.com",
                "--workspace",
                "default",
            ]
        )
        == 0
    )

    data = json.loads(config_path.read_text(encoding="utf-8"))
    assert data == {
        "default_profile": "local",
        "profiles": {
            "local": {
                "api_url": "https://workflows.example.com",
                "workspace_id": "default",
                "workspace_name": "Default",
            }
        },
    }
    output = capsys.readouterr().out
    assert "Step 1 of 2: Authenticate" in output
    assert "Step 2 of 2: Workspace" in output
    assert "Step 3" not in output
    assert "rebase connect github" in output
    assert "────────────────" in output


def test_setup_prompt_treats_literal_ctrl_c_as_abort(monkeypatch) -> None:
    from rebase import setup as setup_module

    monkeypatch.setattr("builtins.input", lambda _prompt: "\x03")

    try:
        setup_module._read_input("workspace [1-1]: ")
    except KeyboardInterrupt:
        return

    raise AssertionError("expected KeyboardInterrupt")


def test_setup_prompt_restores_terminal_before_input(monkeypatch) -> None:
    from rebase import setup as setup_module

    calls: list[str] = []
    monkeypatch.setattr(setup_module, "_restore_terminal_for_prompts", lambda: calls.append("restore"))
    monkeypatch.setattr("builtins.input", lambda _prompt: "1")

    assert setup_module._read_input("auth provider [1-2]: ") == "1"
    assert calls == ["restore"]


def test_setup_selector_uses_green_circle_marker() -> None:
    from rebase import setup as setup_module

    lines = setup_module._selector_lines("auth provider", ["google", "github"], 0)

    assert setup_module.SELECTED_MARKER in lines[1]
    assert "●" in lines[1]
    assert "google" in lines[1]
    assert setup_module.UNSELECTED_MARKER in lines[2]


def test_setup_selector_arrow_navigation_wraps() -> None:
    from rebase import setup as setup_module

    assert setup_module._selector_index_for_key(0, b"\x1b[B", 2) == 1
    assert setup_module._selector_index_for_key(1, b"\x1b[B", 2) == 0
    assert setup_module._selector_index_for_key(0, b"\x1b[A", 2) == 1
    assert setup_module._selector_index_for_key(1, b"x", 2) == 1


def test_setup_confirm_uses_selector(monkeypatch) -> None:
    from rebase import setup as setup_module

    observed: dict[str, Any] = {}

    def fake_choose(label: str, values: list[str], *, default: str | None = None, title: str | None = None) -> str:
        observed.update({"label": label, "values": values, "default": default, "title": title})
        return "Yes"

    monkeypatch.setattr(setup_module, "_choose", fake_choose)

    assert setup_module._confirm("Connect GitHub now?", default=False) is True
    assert observed == {
        "label": "answer",
        "values": ["Yes", "No"],
        "default": "No",
        "title": "Connect GitHub now?",
    }


def test_setup_workspace_join_prompts_for_workspace_handle(monkeypatch) -> None:
    from rebase import setup as setup_module

    class FakeClient:
        def list_my_workspaces(self) -> list[dict[str, Any]]:
            return [{"id": "default", "name": "Default", "default": True}]

    prompts: list[str] = []

    monkeypatch.setattr(setup_module, "_choose", lambda *args, **kwargs: setup_module.JOIN_WORKSPACE)

    def fake_prompt(value: str | None, message: str, *, default: str | None = None) -> str:
        prompts.append(message)
        return "Default"

    monkeypatch.setattr(setup_module, "_prompt", fake_prompt)

    workspace = setup_module._select_workspace(
        SimpleNamespace(workspace=None, workspace_name=None, handle=None),
        FakeClient(),
        session=None,
    )

    assert workspace["id"] == "default"
    assert prompts == ["Workspace handle to join"]


def test_setup_workspace_invite_offers_join_and_create(monkeypatch) -> None:
    from rebase import setup as setup_module

    class FakeClient:
        def list_my_workspaces(self) -> list[dict[str, Any]]:
            return [
                {
                    "id": "team",
                    "name": "Team",
                    "default": True,
                    "joined_via_invite": True,
                }
            ]

    choices: list[dict[str, Any]] = []

    def fake_choose(label: str, values: list[str], *, default: str | None = None, title: str | None = None) -> str:
        choices.append({"label": label, "values": values, "default": default, "title": title})
        return "Join workspace (Team)"

    monkeypatch.setattr(setup_module, "_choose", fake_choose)

    workspace = setup_module._select_workspace(
        SimpleNamespace(workspace=None, workspace_name=None, handle=None),
        FakeClient(),
        session=None,
    )

    assert workspace["id"] == "team"
    assert choices == [
        {
            "label": "workspace setup",
            "values": ["Join workspace (Team)", setup_module.CREATE_WORKSPACE],
            "default": "Join workspace (Team)",
            "title": "You were invited to a workspace. What do you want to do?",
        }
    ]


def test_setup_workspace_invite_create_without_beta_enrollment_errors(monkeypatch) -> None:
    from rebase import setup as setup_module

    class FakeClient:
        def list_my_workspaces(self) -> list[dict[str, Any]]:
            return [{"id": "team", "name": "Team", "joined_via_invite": True}]

        def request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
            assert (method, path) == ("GET", "/me/profile")
            return {"id": "profile-id", "handle": "invited-user"}

        def create_workspace(self, workspace_id: str, *, name: str | None = None) -> dict[str, Any]:
            raise setup_module.RebaseWorkflowError("creating a workspace requires a platform beta invite")

    monkeypatch.setattr(setup_module, "_choose", lambda *args, **kwargs: setup_module.CREATE_WORKSPACE)
    monkeypatch.setattr(setup_module, "_prompt", lambda *args, **kwargs: "invited-user")

    with pytest.raises(setup_module.RebaseWorkflowError, match="not enrolled in the beta program"):
        setup_module._select_workspace(
            SimpleNamespace(workspace=None, workspace_name=None, handle=None),
            FakeClient(),
            session=None,
        )


def test_setup_workspace_invite_create_with_beta_enrollment_continues(monkeypatch) -> None:
    from rebase import setup as setup_module

    calls: list[tuple[str, str | None]] = []

    class FakeClient:
        def list_my_workspaces(self) -> list[dict[str, Any]]:
            return [{"id": "team", "name": "Team", "joined_via_invite": True}]

        def request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
            if (method, path) == ("GET", "/me/profile"):
                calls.append(("get_profile", None))
                return {"id": "profile-id", "handle": "sebaheg"}
            raise AssertionError((method, path))

        def create_workspace(self, workspace_id: str, *, name: str | None = None) -> dict[str, Any]:
            calls.append(("create_workspace", workspace_id))
            return {"id": workspace_id, "name": name}

    monkeypatch.setattr(setup_module, "_choose", lambda *args, **kwargs: setup_module.CREATE_WORKSPACE)
    monkeypatch.setattr(setup_module, "_prompt", lambda *args, **kwargs: "sebaheg")

    workspace = setup_module._select_workspace(
        SimpleNamespace(workspace=None, workspace_name=None, handle=None),
        FakeClient(),
        session=None,
    )

    assert workspace["id"] == "sebaheg"
    assert calls == [("get_profile", None), ("create_workspace", "sebaheg")]


def test_setup_workspace_create_claims_profile_handle_first(monkeypatch) -> None:
    from rebase import setup as setup_module

    calls: list[tuple[str, str | None]] = []

    class FakeClient:
        def list_my_workspaces(self) -> list[dict[str, Any]]:
            return []

        def request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
            if (method, path) == ("GET", "/me/profile"):
                calls.append(("get_profile", None))
                return {"id": "profile-id", "handle": None}
            assert (method, path) == ("PATCH", "/me/profile")
            handle = kwargs["json"]["handle"]
            calls.append(("update_profile", handle))
            return {"id": "profile-id", "handle": handle}

        def create_workspace(self, workspace_id: str, *, name: str | None = None) -> dict[str, Any]:
            calls.append(("create_workspace", workspace_id))
            return {"id": workspace_id, "name": name}

    monkeypatch.setattr(setup_module, "_choose", lambda *args, **kwargs: setup_module.CREATE_WORKSPACE)

    def fake_prompt(value: str | None, message: str, *, default: str | None = None) -> str:
        if message == "Choose your Rebase handle":
            assert default == "sebastian"
            return "SebaHeg"
        assert message == "Workspace handle to create"
        assert default == "sebaheg"
        return default or "fallback"

    monkeypatch.setattr(setup_module, "_prompt", fake_prompt)

    workspace = setup_module._select_workspace(
        SimpleNamespace(workspace=None, workspace_name=None, handle=None),
        FakeClient(),
        session=SimpleNamespace(email="sebastian@rebase.energy"),
    )

    assert workspace["id"] == "sebaheg"
    assert calls == [
        ("get_profile", None),
        ("update_profile", "sebaheg"),
        ("create_workspace", "sebaheg"),
    ]


def test_workspace_create_helper_creates_workspace_without_github_prompt(monkeypatch, tmp_path: Path) -> None:
    from rebase import setup as setup_module

    calls: list[str] = []
    config_path = tmp_path / "config.json"
    monkeypatch.setenv("REBASE_CONFIG_PATH", str(config_path))

    class FakeClient:
        def __init__(
            self,
            *,
            api_url: str | None = None,
            access_token: str | None = None,
            profile: str | None = None,
        ) -> None:
            self.api_url = api_url or "https://api.example.test"
            self.access_token = access_token

        def setup_config(self) -> dict[str, Any]:
            calls.append("setup_config")
            return {"github_app_configured": True}

        def request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
            if (method, path) == ("GET", "/me/profile"):
                calls.append("get_profile")
                return {"id": "profile-id", "handle": "sebastian"}
            raise AssertionError((method, path))

        def create_workspace(self, workspace_id: str, *, name: str | None = None) -> dict[str, Any]:
            calls.append(f"create_workspace:{workspace_id}:{name}")
            return {"id": workspace_id, "name": name or workspace_id}

    monkeypatch.setattr(setup_module, "Client", FakeClient)
    monkeypatch.setattr(setup_module, "_access_token", lambda args, config: "access-token")
    monkeypatch.setattr(setup_module, "load_session", lambda: SimpleNamespace(email="sebastian@rebase.energy"))
    monkeypatch.setattr(setup_module, "_confirm", lambda *args, **kwargs: calls.append("confirm") or True)
    monkeypatch.setattr(setup_module, "_connect_github", lambda *args, **kwargs: calls.append("connect_github"))

    assert (
        setup_module.run_workspace_create(
            SimpleNamespace(
                profile="new",
                api_url=None,
                workspace="energy-team",
                workspace_name="Energy Team",
                handle=None,
            )
        )
        == 0
    )

    assert calls == [
        "setup_config",
        "get_profile",
        "create_workspace:energy-team:Energy Team",
    ]
    data = json.loads(config_path.read_text(encoding="utf-8"))
    assert data["default_profile"] == "new"
    assert data["profiles"]["new"]["workspace_id"] == "energy-team"


def test_workspace_create_helper_stops_before_github_when_quota_is_exhausted(monkeypatch) -> None:
    from rebase import setup as setup_module

    calls: list[str] = []

    class FakeClient:
        def __init__(
            self,
            *,
            api_url: str | None = None,
            access_token: str | None = None,
            profile: str | None = None,
        ) -> None:
            self.api_url = api_url or "https://api.example.test"

        def setup_config(self) -> dict[str, Any]:
            return {"github_app_configured": True}

        def request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
            if (method, path) == ("GET", "/me/profile"):
                return {"id": "profile-id", "handle": "sebastian"}
            raise AssertionError((method, path))

        def create_workspace(self, workspace_id: str, *, name: str | None = None) -> dict[str, Any]:
            calls.append(f"create_workspace:{workspace_id}")
            raise setup_module.RebaseWorkflowError("workspace creation limit reached (1/1)")

    monkeypatch.setattr(setup_module, "Client", FakeClient)
    monkeypatch.setattr(setup_module, "_access_token", lambda args, config: "access-token")
    monkeypatch.setattr(setup_module, "load_session", lambda: None)
    monkeypatch.setattr(setup_module, "_confirm", lambda *args, **kwargs: calls.append("confirm") or True)
    monkeypatch.setattr(setup_module, "_connect_github", lambda *args, **kwargs: calls.append("connect_github"))

    with pytest.raises(setup_module.RebaseWorkflowError, match="You've reached your quota"):
        setup_module.run_workspace_create(
            SimpleNamespace(
                profile="default",
                api_url=None,
                workspace="energy-team",
                workspace_name=None,
                handle=None,
            )
        )

    assert calls == ["create_workspace:energy-team"]


def test_connect_huggingface_helper_runs_device_flow(monkeypatch) -> None:
    from rebase import setup as setup_module

    calls: list[str] = []

    class FakeResponse:
        def __init__(self, status_code: int, payload: dict[str, Any]) -> None:
            self.status_code = status_code
            self._payload = payload
            self.text = json.dumps(payload)

        def json(self) -> dict[str, Any]:
            return self._payload

    def fake_post(url: str, *, data: dict[str, Any], timeout: int) -> FakeResponse:
        calls.append(f"post:{url}:{data['client_id']}")
        if url == setup_module.HUGGINGFACE_DEVICE_URL:
            assert data["scope"] == "openid profile email write-repos"
            return FakeResponse(
                200,
                {
                    "device_code": "device-code",
                    "user_code": "ABCD-EFGH",
                    "verification_uri": "https://huggingface.co/activate",
                    "interval": 0,
                    "expires_in": 300,
                },
            )
        assert url == setup_module.HUGGINGFACE_TOKEN_URL
        return FakeResponse(200, {"access_token": "hf_oauth_token", "token_type": "bearer"})

    monkeypatch.setenv(setup_module.HUGGINGFACE_CLIENT_ID_ENV, "hf-client")
    monkeypatch.setattr(setup_module, "_huggingface_login_function", lambda: (lambda **kwargs: None))
    monkeypatch.setattr(setup_module.requests, "post", fake_post)
    monkeypatch.setattr(setup_module.webbrowser, "open", lambda url: calls.append(f"open:{url}"))
    monkeypatch.setattr(
        setup_module,
        "_save_huggingface_token",
        lambda token, *, add_to_git_credential: calls.append(f"save:{token}:{add_to_git_credential}"),
    )

    assert (
        setup_module.run_connect_huggingface(
            SimpleNamespace(
                profile="default",
                api_url=None,
                client_id=None,
                scope=None,
                no_browser=False,
                timeout=10,
                poll_interval=0,
                add_to_git_credential=True,
            )
        )
        == 0
    )

    assert calls == [
        f"post:{setup_module.HUGGINGFACE_DEVICE_URL}:hf-client",
        "open:https://huggingface.co/activate",
        f"post:{setup_module.HUGGINGFACE_TOKEN_URL}:hf-client",
        "save:hf_oauth_token:True",
    ]


def test_connect_huggingface_helper_requires_client_id(monkeypatch) -> None:
    from rebase import setup as setup_module

    class FakeClient:
        def __init__(self, *, api_url: str | None = None, profile: str | None = None) -> None:
            pass

        def setup_config(self) -> dict[str, Any]:
            return {}

    monkeypatch.delenv(setup_module.HUGGINGFACE_CLIENT_ID_ENV, raising=False)
    monkeypatch.setattr(setup_module, "Client", FakeClient)
    monkeypatch.setattr(setup_module, "_huggingface_login_function", lambda: (lambda **kwargs: None))

    with pytest.raises(setup_module.RebaseWorkflowError, match="OAuth client id"):
        setup_module.run_connect_huggingface(
            SimpleNamespace(
                profile="default",
                api_url=None,
                client_id=None,
                scope=None,
                no_browser=True,
                timeout=10,
                poll_interval=0,
                add_to_git_credential=False,
            )
        )


def test_setup_repo_creation_uses_action_selector(monkeypatch) -> None:
    from rebase import setup as setup_module

    observed: dict[str, Any] = {}

    class Args:
        create_repo = False
        repo = None

    def fake_choose(label: str, values: list[str], *, default: str | None = None, title: str | None = None) -> str:
        observed.update({"label": label, "values": values, "default": default, "title": title})
        return "Create a new one"

    monkeypatch.setattr(setup_module, "_choose", fake_choose)

    assert setup_module._should_create_repo(Args()) is True
    assert observed == {
        "label": "repository setup",
        "values": ["Select an existing repository", "Create a new one"],
        "default": "Create a new one",
        "title": "Do you want to use an existing GitHub repository as your sync repo or create a new one?",
    }


def test_setup_existing_repo_prompts_before_verifying_installation(monkeypatch, capsys) -> None:
    from rebase import setup as setup_module

    calls: list[str] = []

    class FakeClient:
        def list_github_repositories(self, installation_id: int) -> list[dict[str, Any]]:
            calls.append(f"list_repos:{installation_id}")
            return [
                {
                    "id": 456,
                    "owner": "rebase",
                    "name": "platform",
                    "full_name": "rebase/platform",
                    "default_branch": "main",
                }
            ]

        def connect_github_repo(self, **kwargs: Any) -> dict[str, Any]:
            calls.append(f"connect:{kwargs['repo_owner']}/{kwargs['repo_name']}")
            return {"repo_owner": kwargs["repo_owner"], "repo_name": kwargs["repo_name"]}

    def fake_choose(label: str, values: list[str], *, default: str | None = None, title: str | None = None) -> str:
        assert label == "repository setup"
        return "Select an existing repository"

    def fake_prompt(value: str | None, message: str, *, default: str | None = None) -> str:
        calls.append(f"prompt:{message}")
        return "rebase/platform"

    monkeypatch.setattr(setup_module, "_choose", fake_choose)
    monkeypatch.setattr(setup_module, "_prompt", fake_prompt)

    setup_module._connect_github(
        SimpleNamespace(
            repo_scope="workspace",
            project=None,
            repo=None,
            create_repo=False,
            github_installation_id=123,
            repo_path=None,
        ),
        FakeClient(),
        workspace_id="default",
    )

    assert calls == [
        "prompt:GitHub repository full name",
        "list_repos:123",
        "connect:rebase/platform",
    ]
    assert "Verified GitHub App access to rebase/platform" in capsys.readouterr().out


def test_setup_opens_github_installation_after_repo_questions(monkeypatch) -> None:
    from rebase import setup as setup_module

    calls: list[str] = []

    class FakeClient:
        def create_github_setup_session(self, *, workspace_id: str | None = None) -> dict[str, Any]:
            calls.append(f"create_setup:{workspace_id}")
            return {"id": "setup-id", "install_url": "https://github.com/apps/rebase-toolkit/installations/new"}

        def get_github_setup_session(self, setup_session_id: str) -> dict[str, Any]:
            calls.append(f"poll_setup:{setup_session_id}")
            return {"status": "installed", "installation_id": 123}

        def find_github_repository_installation(self, repo_full_name: str) -> dict[str, Any]:
            calls.append(f"find_repo:{repo_full_name}")
            return {
                "installation_id": 123,
                "repository": {
                    "id": 456,
                    "owner": "rebase",
                    "name": "platform",
                    "full_name": "rebase/platform",
                    "default_branch": "main",
                },
            }

        def list_github_repositories(self, installation_id: int) -> list[dict[str, Any]]:
            calls.append(f"list_repos:{installation_id}")
            return [
                {
                    "id": 456,
                    "owner": "rebase",
                    "name": "platform",
                    "full_name": "rebase/platform",
                    "default_branch": "main",
                }
            ]

        def connect_github_repo(self, **kwargs: Any) -> dict[str, Any]:
            calls.append(f"connect:{kwargs['repo_owner']}/{kwargs['repo_name']}")
            return {"repo_owner": kwargs["repo_owner"], "repo_name": kwargs["repo_name"]}

    def fake_choose(label: str, values: list[str], *, default: str | None = None, title: str | None = None) -> str:
        calls.append(f"choose:{label}")
        if label == "GitHub connection scope":
            return "workspace"
        assert label == "repository setup"
        return "Select an existing repository"

    def fake_prompt(value: str | None, message: str, *, default: str | None = None) -> str:
        calls.append(f"prompt:{message}")
        return "rebase/platform"

    monkeypatch.setattr(setup_module, "_choose", fake_choose)
    monkeypatch.setattr(setup_module, "_prompt", fake_prompt)
    monkeypatch.setattr(setup_module.webbrowser, "open", lambda url: calls.append(f"open:{url}"))

    setup_module._connect_github(
        SimpleNamespace(
            repo_scope=None,
            project=None,
            repo=None,
            create_repo=False,
            github_installation_id=None,
            github_timeout=1,
            poll_interval=0,
            repo_path=None,
            no_browser=False,
        ),
        FakeClient(),
        workspace_id="default",
    )

    assert calls == [
        "choose:GitHub connection scope",
        "choose:repository setup",
        "prompt:GitHub repository full name",
        "create_setup:default",
        "open:https://github.com/apps/rebase-toolkit/installations/new",
        "find_repo:rebase/platform",
        "connect:rebase/platform",
    ]


def test_setup_retries_direct_repo_verification_after_user_finishes_install(monkeypatch, capsys) -> None:
    from rebase import setup as setup_module

    calls: list[str] = []

    class FakeClient:
        attempts = 0

        def create_github_setup_session(self, *, workspace_id: str | None = None) -> dict[str, Any]:
            calls.append(f"create_setup:{workspace_id}")
            return {"id": "setup-id", "install_url": "https://github.com/apps/rebase-toolkit/installations/new"}

        def find_github_repository_installation(self, repo_full_name: str) -> dict[str, Any]:
            calls.append(f"find_repo:{repo_full_name}")
            self.attempts += 1
            if self.attempts == 1:
                raise setup_module.RebaseWorkflowError("repository is not accessible")
            return {
                "installation_id": 123,
                "repository": {
                    "id": 456,
                    "owner": "rebase",
                    "name": "platform",
                    "full_name": "rebase/platform",
                    "default_branch": "main",
                },
            }

        def connect_github_repo(self, **kwargs: Any) -> dict[str, Any]:
            calls.append(f"connect:{kwargs['repo_owner']}/{kwargs['repo_name']}")
            return {"repo_owner": kwargs["repo_owner"], "repo_name": kwargs["repo_name"]}

    def fake_choose(label: str, values: list[str], *, default: str | None = None, title: str | None = None) -> str:
        if label == "GitHub connection scope":
            return "workspace"
        return "Select an existing repository"

    monkeypatch.setattr(setup_module, "_choose", fake_choose)
    monkeypatch.setattr(setup_module, "_prompt", lambda *args, **kwargs: "rebase/platform")
    monkeypatch.setattr(setup_module, "_read_input", lambda message: calls.append(message) or "")
    monkeypatch.setattr(setup_module.webbrowser, "open", lambda url: calls.append(f"open:{url}"))

    setup_module._connect_github(
        SimpleNamespace(
            repo_scope=None,
            project=None,
            repo=None,
            create_repo=False,
            github_installation_id=None,
            repo_path=None,
            no_browser=False,
        ),
        FakeClient(),
        workspace_id="default",
    )

    assert calls == [
        "create_setup:default",
        "open:https://github.com/apps/rebase-toolkit/installations/new",
        "find_repo:rebase/platform",
        "Press Enter to verify GitHub access again: ",
        "find_repo:rebase/platform",
        "connect:rebase/platform",
    ]
    assert "GitHub App access is not visible yet." in capsys.readouterr().out


def test_setup_existing_repo_reports_verification_failure(monkeypatch, capsys) -> None:
    from rebase import setup as setup_module

    class FakeClient:
        def list_github_repositories(self, installation_id: int) -> list[dict[str, Any]]:
            return [
                {
                    "id": 456,
                    "owner": "rebase",
                    "name": "other",
                    "full_name": "rebase/other",
                    "default_branch": "main",
                }
            ]

    monkeypatch.setattr(setup_module, "_choose", lambda *args, **kwargs: "Select an existing repository")
    monkeypatch.setattr(setup_module, "_prompt", lambda *args, **kwargs: "rebase/platform")

    try:
        setup_module._connect_github(
            SimpleNamespace(
                repo_scope="workspace",
                project=None,
                repo=None,
                create_repo=False,
                github_installation_id=123,
                repo_path=None,
            ),
            FakeClient(),
            workspace_id="default",
        )
    except setup_module.RebaseWorkflowError:
        pass
    else:
        raise AssertionError("expected repo verification to fail")

    assert "Could not verify GitHub App access to rebase/platform" in capsys.readouterr().out


def test_setup_existing_repo_requires_owner_name() -> None:
    from rebase import setup as setup_module

    assert setup_module._validate_github_repo_full_name("Rebase/Platform.git") == "Rebase/Platform"

    try:
        setup_module._validate_github_repo_full_name("platform")
    except Exception as exc:
        assert "owner/name" in str(exc)
    else:
        raise AssertionError("expected invalid repo format")


def test_connect_github_helper_uses_existing_workspace_connection(monkeypatch) -> None:
    from rebase import setup as setup_module

    calls: list[str] = []
    connection = {
        "scope": "workspace",
        "project_id": None,
        "repo_owner": "rebase",
        "repo_name": "platform",
    }

    class FakeClient:
        def __init__(self, *, api_url: str | None = None, profile: str | None = None) -> None:
            self.api_url = api_url or "https://api.example.test"
            self.profile = profile
            self.workspace_id = "energy-team"
            calls.append(f"client:{self.api_url}:{profile}")

        def setup_config(self) -> dict[str, Any]:
            calls.append("setup_config")
            return {"github_app_configured": True}

        def list_github_repo_connections(self) -> list[dict[str, Any]]:
            calls.append("list_connections")
            return [connection]

    monkeypatch.setattr(setup_module, "Client", FakeClient)
    monkeypatch.setattr(
        setup_module,
        "_ensure_local_workspace_repo",
        lambda selected: calls.append(f"local:{selected['repo_owner']}/{selected['repo_name']}"),
    )
    monkeypatch.setattr(
        setup_module,
        "_verify_github_app_access",
        lambda args, client, selected, *, workspace_id: calls.append(f"app:{workspace_id}:{args.repo}"),
    )

    assert (
        setup_module.run_connect_github(
            SimpleNamespace(
                profile="energy",
                api_url="https://api.example.test",
                no_browser=True,
                github_installation_id=None,
                github_timeout=1,
                poll_interval=0,
                repo="rebase/platform",
                repo_path=None,
                create_repo=False,
            )
        )
        == 0
    )

    assert calls == [
        "client:https://api.example.test:energy",
        "setup_config",
        "list_connections",
        "local:rebase/platform",
        "app:energy-team:rebase/platform",
    ]


def test_connect_github_helper_creates_missing_workspace_connection(monkeypatch) -> None:
    from rebase import setup as setup_module

    calls: list[str] = []
    connection = {
        "scope": "workspace",
        "project_id": None,
        "repo_owner": "rebase",
        "repo_name": "platform",
    }

    class FakeClient:
        def __init__(self, *, api_url: str | None = None, profile: str | None = None) -> None:
            self.api_url = api_url or "https://api.example.test"
            self.workspace_id = "energy-team"

        def setup_config(self) -> dict[str, Any]:
            return {"github_app_configured": True}

        def list_github_repo_connections(self) -> list[dict[str, Any]]:
            calls.append("list_connections")
            return []

    def fake_connect(args: Any, client: Any, *, workspace_id: str) -> dict[str, Any]:
        calls.append(f"connect:{workspace_id}:{args.repo_scope}:{args.project}")
        return connection

    monkeypatch.setattr(setup_module, "Client", FakeClient)
    monkeypatch.setattr(setup_module, "_connect_github", fake_connect)
    monkeypatch.setattr(setup_module, "_ensure_local_workspace_repo", lambda selected: calls.append("local"))
    monkeypatch.setattr(setup_module, "_verify_github_app_access", lambda *args, **kwargs: calls.append("app"))

    assert (
        setup_module.run_connect_github(
            SimpleNamespace(
                profile="energy",
                api_url=None,
                no_browser=True,
                github_installation_id=None,
                github_timeout=1,
                poll_interval=0,
                repo="rebase/platform",
                repo_path=None,
                create_repo=False,
            )
        )
        == 0
    )

    assert calls == ["list_connections", "connect:energy-team:workspace:None", "list_connections", "local", "app"]


def test_github_connect_local_repo_must_match_workspace(monkeypatch, tmp_path: Path) -> None:
    from rebase import setup as setup_module

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(setup_module, "_current_git_root", lambda: tmp_path)
    monkeypatch.setattr(setup_module, "_local_github_remote", lambda cwd=None: "rebase/other")

    with pytest.raises(setup_module.RebaseWorkflowError, match="same GitHub repo"):
        setup_module._ensure_local_workspace_repo({"repo_owner": "rebase", "repo_name": "platform"})


def test_github_connect_clones_workspace_repo_when_current_folder_has_only_venv(
    monkeypatch,
    tmp_path: Path,
) -> None:
    from rebase import setup as setup_module

    calls: list[tuple[list[str], Path, str, float]] = []
    state: dict[str, Path | None] = {"root": None}
    (tmp_path / ".venv").mkdir()

    def fake_run_git(args: list[str], *, cwd: Path, action: str, timeout: float = 60) -> None:
        calls.append((args, cwd, action, timeout))
        if args == ["init"]:
            state["root"] = tmp_path

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(setup_module, "_confirm", lambda message, *, default: True)
    monkeypatch.setattr(
        setup_module,
        "_choose",
        lambda label, values, *, default=None, title=None: "SSH (git@github.com:rebase/platform.git)",
    )
    monkeypatch.setattr(setup_module, "_current_git_root", lambda: state["root"])
    monkeypatch.setattr(setup_module, "_local_github_remote", lambda cwd=None: "rebase/platform")
    monkeypatch.setattr(setup_module, "_run_git", fake_run_git)

    setup_module._ensure_local_workspace_repo(
        {"repo_owner": "rebase", "repo_name": "platform", "default_branch": "main"}
    )

    assert calls == [
        (
            ["init"],
            tmp_path,
            "initialize a git repository",
            60,
        ),
        (
            ["remote", "add", "origin", "git@github.com:rebase/platform.git"],
            tmp_path,
            "add GitHub origin",
            60,
        ),
        (
            ["fetch", "origin"],
            tmp_path,
            "fetch rebase/platform",
            300,
        ),
        (
            ["checkout", "-B", "main", "origin/main"],
            tmp_path,
            "check out origin/main",
            60,
        )
    ]


def test_github_connect_can_clone_workspace_repo_over_https(monkeypatch, tmp_path: Path) -> None:
    from rebase import setup as setup_module

    calls: list[list[str]] = []
    state: dict[str, Path | None] = {"root": None}

    def fake_run_git(args: list[str], *, cwd: Path, action: str, timeout: float = 60) -> None:
        _ = cwd, action, timeout
        calls.append(args)
        if args == ["init"]:
            state["root"] = tmp_path

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(setup_module, "_confirm", lambda message, *, default: True)
    monkeypatch.setattr(
        setup_module,
        "_choose",
        lambda label, values, *, default=None, title=None: "HTTPS (https://github.com/rebase/platform.git)",
    )
    monkeypatch.setattr(setup_module, "_current_git_root", lambda: state["root"])
    monkeypatch.setattr(setup_module, "_local_github_remote", lambda cwd=None: "rebase/platform")
    monkeypatch.setattr(setup_module, "_run_git", fake_run_git)

    setup_module._ensure_local_workspace_repo(
        {"repo_owner": "rebase", "repo_name": "platform", "default_branch": "main"}
    )

    assert ["remote", "add", "origin", "https://github.com/rebase/platform.git"] in calls


def test_github_connect_rewrites_matching_https_origin_to_ssh(monkeypatch, tmp_path: Path) -> None:
    from rebase import setup as setup_module

    calls: list[tuple[list[str], Path, str]] = []

    def fake_run_git(args: list[str], *, cwd: Path, action: str, timeout: float = 60) -> None:
        _ = timeout
        calls.append((args, cwd, action))

    monkeypatch.setattr(setup_module, "_current_git_root", lambda: tmp_path)
    monkeypatch.setattr(setup_module, "_local_github_remote", lambda cwd=None: "rebase/platform")
    monkeypatch.setattr(setup_module, "_origin_url", lambda cwd: "https://github.com/rebase/platform.git")
    monkeypatch.setattr(
        setup_module,
        "_choose",
        lambda label, values, *, default=None, title=None: "SSH (git@github.com:rebase/platform.git)",
    )
    monkeypatch.setattr(setup_module, "_run_git", fake_run_git)

    setup_module._ensure_local_workspace_repo({"repo_owner": "rebase", "repo_name": "platform"})

    assert calls == [
        (
            ["remote", "set-url", "origin", "git@github.com:rebase/platform.git"],
            tmp_path,
            "update GitHub origin",
        )
    ]


def test_github_connect_stops_when_user_declines_clone(monkeypatch, tmp_path: Path) -> None:
    from rebase import setup as setup_module

    (tmp_path / "workflow.py").write_text("print('hello')\n")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(setup_module, "_confirm", lambda message, *, default: False)
    monkeypatch.setattr(setup_module, "_current_git_root", lambda: None)

    with pytest.raises(setup_module.RebaseWorkflowError, match="choose Yes"):
        setup_module._ensure_local_workspace_repo({"repo_owner": "rebase", "repo_name": "platform"})


def test_github_connect_verifies_app_after_install_retry(monkeypatch) -> None:
    from rebase import setup as setup_module

    calls: list[str] = []

    class FakeClient:
        attempts = 0

        def find_github_repository_installation(self, repo_full_name: str) -> dict[str, Any]:
            calls.append(f"find:{repo_full_name}")
            self.attempts += 1
            if self.attempts == 1:
                raise setup_module.RebaseWorkflowError("not installed")
            return {
                "installation_id": 123,
                "repository": {"id": 456, "owner": "rebase", "name": "platform"},
            }

    monkeypatch.setattr(
        setup_module,
        "_start_github_installation",
        lambda args, client, *, workspace_id: calls.append(f"install:{workspace_id}") or {},
    )
    monkeypatch.setattr(setup_module, "_read_input", lambda message: calls.append(message) or "")

    setup_module._verify_github_app_access(
        SimpleNamespace(no_browser=True),
        FakeClient(),
        {"repo_owner": "rebase", "repo_name": "platform"},
        workspace_id="energy-team",
    )

    assert calls == [
        "find:rebase/platform",
        "install:energy-team",
        "Press Enter to verify GitHub App access again: ",
        "find:rebase/platform",
    ]


def test_main_keyboard_interrupt_aborts_cleanly(monkeypatch, capsys) -> None:
    def interrupting_app(*args: Any, **kwargs: Any) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr("rebase.cli.app", interrupting_app)

    assert main(["setup"]) == 130
    assert "Aborted." in capsys.readouterr().err


def test_workspace_lists_profiles(monkeypatch, tmp_path: Path, capsys) -> None:
    config_path = tmp_path / "config.json"
    monkeypatch.setenv("REBASE_CONFIG_PATH", str(config_path))
    config_path.write_text(
        json.dumps(
            {
                "default_profile": "prod",
                "profiles": {
                    "dev": {"api_key": "rbw_dev", "workspace_name": "Development"},
                    "prod": {"api_key": "rbw_prod", "workspace_name": "Production"},
                },
            }
        ),
        encoding="utf-8",
    )

    assert main(["workspace", "list"]) == 0

    output = capsys.readouterr().out
    assert "Workspace Profiles" in output
    assert "dev" in output
    assert "Development" in output
    assert "prod" in output
    assert "Production" in output
    assert "*" in output


def test_workspace_use_changes_default_profile(monkeypatch, tmp_path: Path, capsys) -> None:
    config_path = tmp_path / "config.json"
    monkeypatch.setenv("REBASE_CONFIG_PATH", str(config_path))
    config_path.write_text(
        json.dumps(
            {
                "default_profile": "dev",
                "profiles": {
                    "dev": {"api_key": "rbw_dev"},
                    "prod": {"api_key": "rbw_prod"},
                },
            }
        ),
        encoding="utf-8",
    )

    assert main(["workspace", "use", "prod"]) == 0

    data = json.loads(config_path.read_text(encoding="utf-8"))
    assert data["default_profile"] == "prod"
    assert capsys.readouterr().out == "Switched workspace profile to 'prod'\n"


def test_workspace_switch_changes_default_profile(monkeypatch, tmp_path: Path, capsys) -> None:
    config_path = tmp_path / "config.json"
    monkeypatch.setenv("REBASE_CONFIG_PATH", str(config_path))
    config_path.write_text(
        json.dumps(
            {
                "default_profile": "dev",
                "profiles": {
                    "dev": {"api_key": "rbw_dev"},
                    "prod": {"api_key": "rbw_prod"},
                },
            }
        ),
        encoding="utf-8",
    )

    assert main(["workspace", "switch", "prod"]) == 0

    data = json.loads(config_path.read_text(encoding="utf-8"))
    assert data["default_profile"] == "prod"
    assert capsys.readouterr().out == "Switched workspace profile to 'prod'\n"


def test_workspace_create_command_dispatches_to_setup_helper(monkeypatch) -> None:
    observed: dict[str, Any] = {}

    def fake_run_workspace_create(args: Any) -> int:
        observed.update(vars(args))
        return 0

    monkeypatch.setattr("rebase.setup.run_workspace_create", fake_run_workspace_create)

    assert (
        main(
            [
                "workspace",
                "create",
                "energy-team",
                "--workspace-name",
                "Energy Team",
                "--profile",
                "energy",
            ]
        )
        == 0
    )

    assert observed["workspace"] == "energy-team"
    assert observed["workspace_name"] == "Energy Team"
    assert observed["profile"] == "energy"


def test_connect_github_command_dispatches_to_setup_helper(monkeypatch) -> None:
    observed: dict[str, Any] = {}

    def fake_run_connect_github(args: Any) -> int:
        observed.update(vars(args))
        return 0

    monkeypatch.setattr("rebase.setup.run_connect_github", fake_run_connect_github)

    assert (
        main(
            [
                "connect",
                "github",
                "--profile",
                "energy",
                "--repo",
                "rebase/platform",
                "--create-repo",
            ]
        )
        == 0
    )

    assert observed["profile"] == "energy"
    assert observed["repo"] == "rebase/platform"
    assert observed["create_repo"] is True


def test_connect_huggingface_command_dispatches_to_setup_helper(monkeypatch) -> None:
    observed: dict[str, Any] = {}

    def fake_run_connect_huggingface(args: Any) -> int:
        observed.update(vars(args))
        return 0

    monkeypatch.setattr("rebase.setup.run_connect_huggingface", fake_run_connect_huggingface)

    assert (
        main(
            [
                "connect",
                "huggingface",
                "--client-id",
                "hf-client",
                "--scope",
                "openid",
                "--scope",
                "write-repos",
                "--add-to-git-credential",
            ]
        )
        == 0
    )

    assert observed["client_id"] == "hf-client"
    assert observed["scope"] == ["openid", "write-repos"]
    assert observed["add_to_git_credential"] is True


def test_workspace_invite_email_target(monkeypatch, capsys) -> None:
    observed: dict[str, Any] = {}

    def fake_create_workspace_invite(self: Client, **kwargs: Any) -> dict[str, Any]:
        observed.update(kwargs)
        return {
            "email": kwargs["email"],
            "github_username": None,
            "role": kwargs["role"],
            "status": "pending",
        }

    monkeypatch.setattr(Client, "create_workspace_invite", fake_create_workspace_invite)

    assert main(["workspace", "invite", "davide@rebase.energy", "--role", "Developer"]) == 0

    assert observed == {
        "email": "davide@rebase.energy",
        "github_username": None,
        "role": "Developer",
    }
    output = capsys.readouterr().out
    assert "davide@rebase.energy" in output
    assert "Developer" in output


def test_workspace_invite_github_target(monkeypatch, capsys) -> None:
    observed: dict[str, Any] = {}

    def fake_create_workspace_invite(self: Client, **kwargs: Any) -> dict[str, Any]:
        observed.update(kwargs)
        return {
            "email": None,
            "github_username": kwargs["github_username"],
            "role": kwargs["role"],
            "status": "pending",
        }

    monkeypatch.setattr(Client, "create_workspace_invite", fake_create_workspace_invite)

    assert main(["workspace", "invite", "davide-github"]) == 0

    assert observed == {
        "email": None,
        "github_username": "davide-github",
        "role": "Viewer",
    }
    assert "@davide-github" in capsys.readouterr().out


def test_workspace_invite_requires_one_target(capsys) -> None:
    assert main(["workspace", "invite"]) == 1

    assert "provide exactly one invite target" in capsys.readouterr().err


def test_workspace_members_lists_members_and_pending_invites(monkeypatch, capsys) -> None:
    def fake_list_workspace_members(self: Client) -> list[dict[str, Any]]:
        return [
            {
                "email": "sebastian@rebase.energy",
                "github_username": "sebaheg",
                "role": "Owner",
                "enabled": True,
                "created_at": "2026-06-22T22:00:00Z",
            }
        ]

    def fake_list_workspace_invites(self: Client) -> list[dict[str, Any]]:
        return [
            {
                "email": "davide@rebase.energy",
                "github_username": None,
                "role": "Viewer",
                "status": "pending",
                "created_at": "2026-06-22T22:52:29Z",
            },
            {
                "email": "accepted@example.com",
                "github_username": None,
                "role": "Developer",
                "status": "accepted",
                "created_at": "2026-06-22T22:30:00Z",
            },
        ]

    monkeypatch.setattr(Client, "list_workspace_members", fake_list_workspace_members)
    monkeypatch.setattr(Client, "list_workspace_invites", fake_list_workspace_invites)

    assert main(["workspace", "members"]) == 0

    output = capsys.readouterr().out
    assert "sebastian@rebase.energy" in output
    assert "Owner" in output
    assert "davide@rebase.energy" in output
    assert "Viewer" in output
    assert "pending" in output
    assert "accepted@example.com" not in output


def test_workspace_usage_renders_credit_balance(monkeypatch, capsys) -> None:
    def fake_get_workspace_usage(self: Client) -> dict[str, Any]:
        return {
            "workspace_id": "beta-team",
            "currency": "EUR",
            "period_start": "2026-06-01T00:00:00Z",
            "period_end": "2026-07-01T00:00:00Z",
            "monthly_credit_cents": 2000,
            "finalized_spend_cents": 325,
            "active_reservation_cents": 100,
            "remaining_cents": 1575,
            "compute_blocked": False,
        }

    monkeypatch.setattr(Client, "get_workspace_usage", fake_get_workspace_usage)

    assert main(["workspace", "usage"]) == 0

    output = capsys.readouterr().out
    assert "Workspace Usage" in output
    assert "beta-team" in output
    assert "20.00 EUR" in output
    assert "3.25 EUR" in output
    assert "1.00 EUR" in output
    assert "15.75 EUR" in output


def test_api_key_list_command_renders_keys(monkeypatch, capsys) -> None:
    monkeypatch.setattr(
        Client,
        "list_api_keys",
        lambda self: [
            {
                "id": "key-id",
                "name": "agent",
                "key_prefix": "rb_abcd1234",
                "project_id": None,
                "enabled": True,
                "last_used_at": None,
                "expires_at": None,
                "revoked_at": None,
                "permissions": ["workspace:read", "runs:read"],
            }
        ],
    )

    assert main(["api-key", "list"]) == 0

    output = capsys.readouterr().out
    assert "API Keys" in output
    assert "agent" in output
    assert "rb_abcd1234" in output
    assert "2p" in output


def test_api_key_create_uses_agent_permissions_and_prints_secret(monkeypatch, capsys) -> None:
    observed: dict[str, Any] = {}

    def fake_create_api_key(self: Client, name: str, **kwargs: Any) -> dict[str, Any]:
        observed["name"] = name
        observed.update(kwargs)
        return {
            "id": "key-id",
            "name": name,
            "key_prefix": "rb_abcd1234",
            "project_id": None,
            "permissions": kwargs["permissions"],
            "enabled": True,
            "expires_at": None,
            "created_at": "2026-06-23T10:00:00Z",
            "api_key": "rb_abcd1234_secret",
        }

    monkeypatch.setattr(Client, "create_api_key", fake_create_api_key)

    assert main(["api-key", "create", "agent"]) == 0

    assert observed == {
        "name": "agent",
        "project_id": None,
        "permissions": [
            "workspace:read",
            "projects:read",
            "endpoints:read",
            "endpoints:execute",
            "functions:read",
            "workflows:read",
            "models:read",
            "runs:read",
        ],
        "expires_at": None,
    }
    output = capsys.readouterr().out
    assert "Created API Key" in output
    assert "API key secret (shown once):" in output
    assert "rb_abcd1234_secret" in output


def test_api_key_create_overrides_permissions_and_resolves_project(monkeypatch, capsys) -> None:
    observed: dict[str, Any] = {}
    monkeypatch.setattr(Client, "find_project", lambda self, name: {"id": "project-id", "name": name})

    def fake_create_api_key(self: Client, name: str, **kwargs: Any) -> dict[str, Any]:
        observed["name"] = name
        observed.update(kwargs)
        return {"id": "key-id", "name": name, "api_key": "rb_secret"}

    monkeypatch.setattr(Client, "create_api_key", fake_create_api_key)

    assert (
        main(
            [
                "api-key",
                "create",
                "runner",
                "--project",
                "energy",
                "--permission",
                "runs:read",
                "--permission",
                "runs:write",
                "--expires-at",
                "2026-07-01T00:00:00Z",
            ]
        )
        == 0
    )

    assert observed == {
        "name": "runner",
        "project_id": "project-id",
        "permissions": ["runs:read", "runs:write"],
        "expires_at": "2026-07-01T00:00:00Z",
    }
    assert "rb_secret" in capsys.readouterr().out


def test_api_key_create_rejects_project_and_project_id(capsys) -> None:
    assert main(["api-key", "create", "agent", "--project", "energy", "--project-id", "project-id"]) == 1

    assert "provide either --project or --project-id" in capsys.readouterr().err


def test_api_key_revoke_resolves_id_prefix_and_unique_name(monkeypatch, capsys) -> None:
    revoked_ids: list[str] = []

    def fake_list_api_keys(self: Client) -> list[dict[str, Any]]:
        return [
            {"id": "key-id", "name": "agent", "key_prefix": "rb_agent"},
            {"id": "other-id", "name": "other", "key_prefix": "rb_other"},
        ]

    def fake_revoke_api_key(self: Client, api_key_id: str) -> dict[str, Any]:
        revoked_ids.append(api_key_id)
        return {"id": api_key_id, "name": "revoked", "key_prefix": "rb_revoked", "enabled": False}

    monkeypatch.setattr(Client, "list_api_keys", fake_list_api_keys)
    monkeypatch.setattr(Client, "revoke_api_key", fake_revoke_api_key)

    assert main(["api-key", "revoke", "key-id"]) == 0
    assert main(["api-key", "revoke", "rb_other"]) == 0
    assert main(["api-key", "revoke", "agent"]) == 0

    assert revoked_ids == ["key-id", "other-id", "key-id"]
    output = capsys.readouterr().out
    assert "Revoked API Key" in output


def test_api_key_revoke_errors_on_ambiguous_selector(monkeypatch, capsys) -> None:
    monkeypatch.setattr(
        Client,
        "list_api_keys",
        lambda self: [
            {"id": "key-id", "name": "agent", "key_prefix": "rb_first"},
            {"id": "other-id", "name": "agent", "key_prefix": "rb_second"},
        ],
    )

    assert main(["api-key", "revoke", "agent"]) == 1

    assert "api key selector is ambiguous" in capsys.readouterr().err


def test_endpoint_invoke_resolves_selector_and_posts_json(monkeypatch, capsys) -> None:
    observed: dict[str, Any] = {}
    endpoint = {
        "id": "endpoint-id",
        "project_name": "energy",
        "name": "forecast",
        "method": "POST",
        "path": "/forecast",
        "url_path": "/e/default/energy/forecast",
    }

    monkeypatch.setattr(Client, "list_endpoints", lambda self, **kwargs: [endpoint])

    def fake_invoke_endpoint(self: Client, selected: dict[str, Any], parameters: dict[str, Any]) -> dict[str, Any]:
        observed["endpoint"] = selected
        observed["parameters"] = parameters
        return {"run_id": "run-id", "status": "succeeded", "result": {"ok": True}}

    monkeypatch.setattr(Client, "invoke_endpoint", fake_invoke_endpoint)

    assert main(["endpoint", "invoke", "energy/forecast", "--json", '{"zone":"SE3"}']) == 0

    assert observed == {"endpoint": endpoint, "parameters": {"zone": "SE3"}}
    output = capsys.readouterr().out
    assert '"run_id": "run-id"' in output
    assert '"status": "succeeded"' in output


def test_project_list_command_prints_projects(monkeypatch, capsys) -> None:
    monkeypatch.setattr(
        Client,
        "list_projects",
        lambda self: [
            {
                "id": "project-id",
                "name": "energy",
                "source_mode": "rebase_hosted",
                "updated_at": "2026-06-16T12:00:00Z",
            }
        ],
    )

    assert main(["project", "list"]) == 0

    output = capsys.readouterr().out
    assert "Projects" in output
    assert "energy" in output
    assert "project-id" in output


def test_project_get_command_supports_name_and_id(monkeypatch, capsys) -> None:
    monkeypatch.setattr(
        Client,
        "list_projects",
        lambda self: [
            {
                "id": "project-id",
                "workspace_id": "default",
                "name": "energy",
                "description": "Forecasting",
                "source_mode": "rebase_hosted",
                "repo_owner": None,
                "repo_name": None,
                "repo_path": None,
                "created_at": "2026-06-15T12:00:00Z",
                "updated_at": "2026-06-16T12:00:00Z",
            }
        ],
    )
    monkeypatch.setattr(
        Client,
        "get_project",
        lambda self, project_id: {
            "id": project_id,
            "workspace_id": "default",
            "name": "energy",
            "description": "Forecasting",
            "source_mode": "rebase_hosted",
            "repo_owner": None,
            "repo_name": None,
            "repo_path": None,
            "created_at": "2026-06-15T12:00:00Z",
            "updated_at": "2026-06-16T12:00:00Z",
        },
    )

    assert main(["project", "get", "energy"]) == 0
    output = capsys.readouterr().out
    assert "Project" in output
    assert "Forecasting" in output

    assert main(["project", "get", "--id", "project-id", "--json"]) == 0
    output = capsys.readouterr().out
    assert '"name": "energy"' in output
    assert '"id": "project-id"' in output


def test_function_list_command_supports_project_filter(monkeypatch, capsys) -> None:
    monkeypatch.setattr(
        Client,
        "list_projects",
        lambda self: [
            {"id": "project-id", "name": "energy"},
            {"id": "other-project-id", "name": "trading"},
        ],
    )
    monkeypatch.setattr(
        Client,
        "list_functions",
        lambda self, *, project=None, project_id=None: [
            {
                "id": "function-id",
                "project_id": project_id or "project-id",
                "name": "add",
                "execution_backend": "cloud_run",
                "enabled": True,
                "updated_at": "2026-06-16T12:00:00Z",
            }
        ],
    )

    assert main(["function", "list", "--project", "energy"]) == 0
    output = capsys.readouterr().out
    assert "Functions" in output
    assert "add" in output
    assert "energy" in output

    assert main(["function", "list", "--json"]) == 0
    output = capsys.readouterr().out
    assert '"name": "add"' in output


def test_function_get_command_requires_project_for_name_lookup(capsys) -> None:
    assert main(["function", "get", "add"]) == 1

    error_output = capsys.readouterr().err
    assert "--project is required when selecting a function by name" in error_output


def test_function_get_and_versions_commands(monkeypatch, capsys) -> None:
    monkeypatch.setattr(Client, "list_projects", lambda self: [{"id": "project-id", "name": "energy"}])
    monkeypatch.setattr(
        Client,
        "list_functions",
        lambda self, *, project=None, project_id=None: [
            {
                "id": "function-id",
                "workspace_id": "default",
                "project_id": project_id or "project-id",
                "name": "add",
                "description": "sum numbers",
                "source_code": "def add(a: int, b: int) -> dict:\n    return {'sum': a + b}\n",
                "entrypoint": "add",
                "default_parameters": {"a": 1},
                "execution_backend": "cloud_run",
                "image_spec": {"kind": "python"},
                "image_fingerprint": "image-fingerprint",
                "cloud_run_min_instances": None,
                "cloud_run_concurrency": None,
                "cloud_run_service_name": "rebase-fn-add",
                "cloud_run_url": "https://service.run.app",
                "deployment_timings": {"backend_provision_seconds": 1.0},
                "enabled": True,
                "current_version_id": "version-id",
                "created_at": "2026-06-15T12:00:00Z",
                "updated_at": "2026-06-16T12:00:00Z",
            }
        ],
    )
    monkeypatch.setattr(
        Client,
        "get_function",
        lambda self, function_id: {
            "id": function_id,
            "workspace_id": "default",
            "project_id": "project-id",
            "name": "add",
            "description": "sum numbers",
            "source_code": "def add(a: int, b: int) -> dict:\n    return {'sum': a + b}\n",
            "entrypoint": "add",
            "default_parameters": {"a": 1},
            "execution_backend": "cloud_run",
            "image_spec": {"kind": "python"},
            "image_fingerprint": "image-fingerprint",
            "cloud_run_min_instances": None,
            "cloud_run_concurrency": None,
            "cloud_run_service_name": "rebase-fn-add",
            "cloud_run_url": "https://service.run.app",
            "deployment_timings": {"backend_provision_seconds": 1.0},
            "enabled": True,
            "current_version_id": "version-id",
            "created_at": "2026-06-15T12:00:00Z",
            "updated_at": "2026-06-16T12:00:00Z",
        },
    )
    monkeypatch.setattr(
        Client,
        "list_function_versions",
        lambda self, function_id: [
            {
                "id": "version-id",
                "version_number": 3,
                "fingerprint": "fp-123",
                "execution_backend": "cloud_run",
                "created_at": "2026-06-16T12:00:00Z",
            }
        ],
    )

    assert main(["function", "get", "add", "--project", "energy"]) == 0
    output = capsys.readouterr().out
    assert "Function" in output
    assert "rebase-fn-add" in output

    assert main(["function", "get", "--id", "function-id", "--json"]) == 0
    output = capsys.readouterr().out
    assert '"name": "add"' in output
    assert '"id": "function-id"' in output

    assert main(["function", "versions", "add", "--project", "energy"]) == 0
    output = capsys.readouterr().out
    assert "Function Versions" in output
    assert "fp-123" in output


def test_workflow_list_and_get_commands(monkeypatch, capsys) -> None:
    monkeypatch.setattr(Client, "list_projects", lambda self: [{"id": "project-id", "name": "energy"}])
    monkeypatch.setattr(
        Client,
        "list_workflows",
        lambda self, *, project=None, project_id=None: [
            {
                "id": "workflow-id",
                "workspace_id": "default",
                "project_id": project_id or "project-id",
                "name": "forecast",
                "description": "forecast workflow",
                "flow_ref": None,
                "source_code": "def forecast(site_id: str) -> dict:\n    return {'site_id': site_id}\n",
                "entrypoint": "forecast",
                "default_parameters": {"site_id": "site-001"},
                "execution_backend": "prefect_cloud_run_service",
                "enabled": True,
                "current_version_id": "workflow-version-id",
                "created_at": "2026-06-15T12:00:00Z",
                "updated_at": "2026-06-16T12:00:00Z",
            }
        ],
    )
    monkeypatch.setattr(
        Client,
        "get_workflow",
        lambda self, workflow_id: {
            "id": workflow_id,
            "workspace_id": "default",
            "project_id": "project-id",
            "name": "forecast",
            "description": "forecast workflow",
            "flow_ref": None,
            "source_code": "def forecast(site_id: str) -> dict:\n    return {'site_id': site_id}\n",
            "entrypoint": "forecast",
            "default_parameters": {"site_id": "site-001"},
            "execution_backend": "prefect_cloud_run_service",
            "enabled": True,
            "current_version_id": "workflow-version-id",
            "created_at": "2026-06-15T12:00:00Z",
            "updated_at": "2026-06-16T12:00:00Z",
        },
    )

    assert main(["workflow", "list"]) == 0
    output = capsys.readouterr().out
    assert "Workflows" in output
    assert "forecast" in output

    assert main(["workflow", "get", "forecast", "--project", "energy"]) == 0
    output = capsys.readouterr().out
    assert "Workflow" in output
    assert "prefect_cloud_run_service" in output

    assert main(["workflow", "get", "--id", "workflow-id", "--json"]) == 0
    output = capsys.readouterr().out
    assert '"name": "forecast"' in output


def test_workflow_versions_command_and_name_lookup_error(capsys, monkeypatch) -> None:
    assert main(["workflow", "versions", "forecast"]) == 1
    error_output = capsys.readouterr().err
    assert "--project is required when selecting a workflow by name" in error_output

    monkeypatch.setattr(Client, "list_projects", lambda self: [{"id": "project-id", "name": "energy"}])
    monkeypatch.setattr(
        Client,
        "list_workflows",
        lambda self, *, project=None, project_id=None: [
            {
                "id": "workflow-id",
                "project_id": project_id or "project-id",
                "name": "forecast",
                "execution_backend": "prefect_cloud_run_service",
                "enabled": True,
                "updated_at": "2026-06-16T12:00:00Z",
            }
        ],
    )
    monkeypatch.setattr(
        Client,
        "list_workflow_versions",
        lambda self, workflow_id: [
            {
                "id": "workflow-version-id",
                "version_number": 2,
                "fingerprint": "workflow-fingerprint",
                "execution_backend": "prefect_cloud_run_service",
                "created_at": "2026-06-16T12:00:00Z",
            }
        ],
    )

    assert main(["workflow", "versions", "forecast", "--project", "energy", "--json"]) == 0
    output = capsys.readouterr().out
    assert '"version_number": 2' in output
