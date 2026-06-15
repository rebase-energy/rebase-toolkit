import json
from pathlib import Path

from rebase.cli import deploy_file, main
from rebase.client import Client, Project, Workflow


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
