from pathlib import Path

from rebase.cli import deploy_file, main
from rebase.client import Project, Workflow


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

    assert capsys.readouterr().out == "Deployed project energy-forecasting (project-id)\n"
