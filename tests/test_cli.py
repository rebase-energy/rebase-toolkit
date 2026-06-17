import json
from pathlib import Path
from typing import Any

from rebase.cli import _format_duration, deploy_file, main
from rebase.client import Client, Function, Model, Project, Run, Workflow


def test_main_without_args_prints_help(capsys) -> None:
    assert main([]) == 0

    output = capsys.readouterr().out
    assert "Usage:" in output
    assert "Rebase Platform toolkit." in output
    assert "setup" in output
    assert "workspace" in output
    assert "project" in output
    assert "function" in output
    assert "workflow" in output
    assert "deploy" in output


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


def test_run_command_runs_model_as_ephemeral_function(monkeypatch, tmp_path: Path, capsys) -> None:
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

    assert observed["target_type"] == "function"
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
    monkeypatch.setattr("getpass.getpass", lambda prompt: "rbw_secret")
    monkeypatch.setattr(Client, "get_workspace", lambda self: {"id": "workspace-id", "name": "ACME"})

    assert main(["setup", "--profile", "test"]) == 0

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
