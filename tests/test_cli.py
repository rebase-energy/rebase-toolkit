import json
from pathlib import Path
from typing import Any

from rebase.cli import deploy_file, main
from rebase.client import Client, Function, Project, Run, Workflow


def test_main_without_args_prints_help(capsys) -> None:
    assert main([]) == 0

    output = capsys.readouterr().out
    assert "Usage:" in output
    assert "Rebase Platform toolkit." in output
    assert "setup" in output
    assert "workspace" in output
    assert "deploy" in output


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
    assert '"Hello, Rebase!"' in capsys.readouterr().out


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
