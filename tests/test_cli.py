import base64
import json
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from rebase import cli
from rebase.auth import AuthSession
from rebase.cli import _format_duration, deploy_file, main
from rebase.client import (
    ASGIApp,
    Bucket,
    Client,
    Function,
    Model,
    Project,
    RebaseWorkflowError,
    Run,
    Workflow,
)


def test_main_without_args_prints_help(capsys) -> None:
    assert main([]) == 0

    output = capsys.readouterr().out
    normalized_output = " ".join(output.split())
    assert "Usage:" in output
    assert (
        "Rebase Toolkit lets you develop Python workflows and models that can then be deployed to the Rebase Platform."
    ) in normalized_output
    assert "setup" in output
    assert "workspace" in output
    assert "profile" in output
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
        "profile",
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
    assert "--mode" in output
    assert "--isolation" in output
    assert "--run-type" in output
    assert "--module, -m" in output
    assert "--wait / --no-wait" in output
    assert "Inspection Commands" in output
    command_order = ["cancel", "get", "list", "logs", "replay"]
    positions = [output.index(command) for command in command_order]
    assert positions == sorted(positions)
    assert "list" in output
    assert "get" in output
    assert "logs" in output
    assert "cancel" in output
    assert "replay" in output


def test_short_help_flag_works_at_every_depth(capsys) -> None:
    for args in (["-h"], ["workflow", "-h"], ["workflow", "schedule", "-h"], ["workflow", "schedule", "set", "-h"]):
        assert main(args) == 0, args
        assert "Usage:" in capsys.readouterr().out, args


def test_unknown_option_reports_usage_error(capsys) -> None:
    # typer vendors its own click, so its NoSuchOption used to escape main() and reach typer's
    # Rich excepthook: a traceback and exit code 1 instead of a usage error and exit code 2.
    assert main(["deploy", "--bogus"]) == 2

    captured = capsys.readouterr()
    assert "No such option" in captured.err
    assert "--bogus" in captured.err
    assert "Traceback" not in captured.err
    assert "Try 'rebase deploy --help' for help." in captured.err


def test_unknown_command_reports_usage_error(capsys) -> None:
    assert main(["definitely-not-a-command"]) == 2
    assert "No such command" in capsys.readouterr().err


def test_abort_from_vendored_click_is_reported(monkeypatch, capsys) -> None:
    import typer

    from rebase import cli

    def raise_abort(**_kwargs: Any) -> None:
        raise typer.Abort()

    monkeypatch.setattr(cli, "app", raise_abort)
    assert main(["deploy", "app.py"]) == 1
    assert "Aborted." in capsys.readouterr().err


def _iter_leaf_commands(command: Any, path: tuple[str, ...] = ()) -> Any:
    commands = getattr(command, "commands", None)
    if commands:
        for name, subcommand in commands.items():
            yield from _iter_leaf_commands(subcommand, (*path, name))
    else:
        yield " ".join(path), command


def test_options_expose_first_letter_short_flags() -> None:
    """Every option gets ``-x`` for its first letter unless something already holds that letter.

    ``-h`` belongs to ``--help``, hand-written short flags win, and within a command the
    first-declared option wins a contested letter. Anything else is a new option that forgot
    its short flag.
    """
    import typer.main

    from rebase.cli import app

    for name, command in _iter_leaf_commands(typer.main.get_command(app)):
        options = []
        for param in command.params:
            if getattr(param, "param_type_name", None) != "option" or param.hidden:
                continue
            longs = [opt for opt in param.opts if opt.startswith("--")]
            shorts = [opt for opt in param.opts if len(opt) == 2 and opt.startswith("-")]
            assert longs, f"rebase {name}: option {param.opts} has no long flag"
            assert "-h" not in shorts, f"rebase {name}: {longs[0]} claims -h, which belongs to --help"
            options.append((longs[0], shorts))

        taken = {"-h"}
        for _long_flag, shorts in options:
            for short in shorts:
                assert short not in taken, f"rebase {name}: {short} is claimed twice"
                taken.add(short)

        for long_flag, shorts in options:
            if not shorts:
                letter = f"-{long_flag[2]}"
                assert letter in taken, f"rebase {name}: {long_flag} should take {letter}"


def test_tui_command_invokes_textual_app(monkeypatch) -> None:
    observed: dict[str, Any] = {}

    def fake_run_tui(
        *,
        project: str | None = None,
        limit: int = 25,
        client: Client | None = None,
        # Accepted and ignored: the command forwards the auto-refresh interval too, and
        # this test is about the project and limit it passes on, not the timer.
        refresh_interval: float = 0.0,
    ) -> None:
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

    def fake_project_deploy(self: Project, *, replace: bool = False, environment: str = "dev") -> Project:
        assert environment == "dev"
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

    def fake_project_deploy(self: Project, *, replace: bool = False, environment: str = "dev") -> Project:
        assert environment == "dev"
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
        environment: str = "dev",
    ) -> Project:
        observed["name"] = self.name
        observed["deploy_source"] = deploy_source
        observed["environment"] = environment
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

    result = deploy_file(workflow_file, deploy_source="github", environment="staging")

    assert observed == {"name": "energy-forecasting", "deploy_source": "github", "environment": "staging"}
    assert result == [("project", "energy-forecasting", "project-id")]


def test_deploy_file_deploys_standalone_workflow(monkeypatch, tmp_path: Path) -> None:
    deployed: list[str] = []

    def fake_workflow_deploy(self: Workflow, *, replace: bool = False, environment: str = "dev") -> Workflow:
        assert environment == "dev"
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

    def fake_asgi_app_deploy(self: ASGIApp, *, replace: bool = False, environment: str = "dev") -> ASGIApp:
        assert environment == "dev"
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
    assert result == [("asgi_app", "grid-api", "grid-api-id", "https://workflows.example.com/e/default/grid/api")]


def test_deploy_file_deploys_standalone_predictor(monkeypatch, tmp_path: Path) -> None:
    deployed: list[str] = []

    def fake_model_deploy(self: Model, *, replace: bool = False, environment: str = "dev") -> Model:
        assert environment == "dev"
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


class _DirectPolicyClient:
    def list_environment_policies(self) -> list[dict[str, Any]]:
        return [{"environment": "dev", "deploy_mode": "direct", "protected": False, "require_pr": False}]


def test_deploy_file_rejects_plain_model(monkeypatch, tmp_path: Path, capsys) -> None:
    monkeypatch.setattr("rebase.cli.Client", _DirectPolicyClient)
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

    def fake_project_deploy(self: Project, *, replace: bool = False, environment: str = "dev") -> Project:
        assert environment == "dev"
        self.id = "project-id"
        return self

    monkeypatch.setattr(Project, "deploy", fake_project_deploy)
    monkeypatch.setattr("rebase.cli.Client", _DirectPolicyClient)

    assert main(["deploy", str(workflow_file)]) == 0

    output = capsys.readouterr().out
    assert "Deployed Targets" in output
    assert "project" in output
    assert "energy-forecasting" in output
    assert "project-id" in output


def test_deploy_command_creates_gitops_intent_for_protected_environment(monkeypatch, tmp_path: Path, capsys) -> None:
    workflow_file = tmp_path / "workflow.py"
    workflow_file.write_text("import rebase as rb\nproject = rb.Project('energy-forecasting')\n", encoding="utf-8")
    observed: dict[str, Any] = {}

    class FakeClient:
        def list_environment_policies(self) -> list[dict[str, Any]]:
            return [{"environment": "prod", "deploy_mode": "gitops", "protected": True, "require_pr": True}]

        def create_gitops_deployment_intent(self, **kwargs: Any) -> dict[str, Any]:
            observed.update(kwargs)
            return {
                "id": "intent-id",
                "environment": kwargs["environment"],
                "status": "pending_pr",
                "source_repo": kwargs["source_repo"],
                "source_path": kwargs["source_path"],
                "git_commit_sha": kwargs["git_commit_sha"],
                "pr_url": "https://github.com/rebase/platform/pulls",
            }

    monkeypatch.setattr("rebase.cli.Client", FakeClient)
    monkeypatch.setattr(
        "rebase.cli._gitops_source_metadata",
        lambda path: {
            "source_repo": "rebase/platform",
            "repo_owner": "rebase",
            "repo_name": "platform",
            "source_path": "workflow.py",
            "git_commit_sha": "abc123",
            "git_branch": "feature/gitops",
        },
    )
    monkeypatch.setattr(
        "rebase.cli.deploy_file",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("direct deploy should not run")),
    )

    assert main(["deploy", str(workflow_file), "--env", "prod"]) == 0

    assert observed["environment"] == "prod"
    assert observed["source_repo"] == "rebase/platform"
    assert observed["source_path"] == "workflow.py"
    assert observed["git_commit_sha"] == "abc123"
    assert observed["plan"]["kind"] == "rebase_deploy"
    output = capsys.readouterr().out
    assert "GitOps Deployment Request" in output
    assert "https://github.com/rebase/platform/pulls" in output


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
        "mode": "interactive",
        "isolation": "shared",
        "parameters": {"a": 1, "b": 2},
        "default_parameters": {},
        "image_spec": {"kind": "python", "python_version": "3.13", "uv_pip_packages": [], "uv_version": None},
        "source_code": observed["source_code"],
        "cloud_run_min_instances": None,
        "cloud_run_concurrency": None,
        # Forwarded as kwargs even when empty; run_ephemeral drops empty dicts
        # and lists from the request body so an older API still accepts it.
        "env": {},
        "secrets": {},
        "buckets": [],
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

@rb.function(project="math", name="add", run_type="quick")
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
    assert observed["mode"] == "interactive"
    assert observed["isolation"] == "shared"
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


def test_run_logs_command_renders_events_steps_and_log_lines_without_following(monkeypatch, capsys) -> None:
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
    monkeypatch.setattr(
        Client,
        "get_run_logs",
        lambda self, run_id, since=None, limit=None: {
            "run_id": run_id,
            "source": "cloud_logging",
            "entries": [
                {"timestamp": "2026-07-11T10:00:01Z", "severity": "INFO", "message": "hello from user code"},
                {"timestamp": "2026-07-11T10:00:02Z", "severity": "ERROR", "message": "something went wrong"},
            ],
            "next_since": "2026-07-11T10:00:02Z",
            "message": None,
        },
    )

    assert main(["run", "logs", "run-id", "--no-follow"]) == 0

    output = capsys.readouterr().out
    assert "Accepted run request." in output
    assert "Step load-name completed." in output
    assert "Run completed." in output
    assert "hello from user code" in output
    assert "something went wrong" in output


def test_run_logs_command_prints_unsupported_backend_message_once(monkeypatch, capsys) -> None:
    monkeypatch.setattr(
        Client,
        "get_run",
        lambda self, run_id: {"id": run_id, "target_type": "function", "status": "succeeded"},
    )
    monkeypatch.setattr(Client, "list_run_events", lambda self, run_id: [])
    monkeypatch.setattr(
        Client,
        "get_run_logs",
        lambda self, run_id, since=None, limit=None: {
            "run_id": run_id,
            "source": "none",
            "entries": [],
            "next_since": None,
            "message": "Log retrieval is not supported for this backend yet.",
        },
    )

    assert main(["run", "logs", "run-id", "--no-follow"]) == 0

    output = capsys.readouterr().out
    assert "Log retrieval is not supported for this backend yet." in output


def test_run_logs_command_can_print_json(monkeypatch, capsys) -> None:
    monkeypatch.setattr(
        Client,
        "get_run",
        lambda self, run_id: {"id": run_id, "target_type": "function", "status": "running"},
    )
    monkeypatch.setattr(Client, "list_run_events", lambda self, run_id: [{"id": "event-id"}])
    monkeypatch.setattr(Client, "list_run_steps", lambda self, run_id: (_ for _ in ()).throw(AssertionError()))
    monkeypatch.setattr(
        Client,
        "get_run_logs",
        lambda self, run_id, since=None, limit=None: {"run_id": run_id, "source": "none", "entries": []},
    )

    assert main(["run", "logs", "run-id", "--json"]) == 0

    output = capsys.readouterr().out
    assert '"run"' in output
    assert '"events"' in output
    assert '"steps": []' in output
    assert '"logs"' in output


def test_run_cancel_command_prints_cancelled_run(monkeypatch, capsys) -> None:
    monkeypatch.setattr(
        Client,
        "cancel_run",
        lambda self, run_id: {
            "id": run_id,
            "status": "cancelled",
            "target_type": "workflow",
            "execution_backend": "prefect_cloud_run_service",
            "error": "cancelled by user",
        },
    )

    assert main(["run", "cancel", "run-id"]) == 0

    output = capsys.readouterr().out
    assert "Cancelled Run" in output
    assert "cancelled" in output


def test_run_cancel_command_surfaces_server_conflict(monkeypatch, capsys) -> None:
    from rebase.client import RebaseWorkflowError

    def fake_cancel(self, run_id):
        raise RebaseWorkflowError("run already finished with status succeeded")

    monkeypatch.setattr(Client, "cancel_run", fake_cancel)

    assert main(["run", "cancel", "run-id"]) == 1

    output = capsys.readouterr().err
    assert "run already finished with status succeeded" in output


def test_run_replay_help_routes_through_run_app(capsys) -> None:
    # Proves "replay" is wired into RUN_INSPECTION_COMMANDS: without it the run
    # target executor would swallow the subcommand.
    assert main(["run", "replay", "--help"]) == 0

    output = capsys.readouterr().out
    assert "--workflow" in output
    assert "--code" in output
    assert "--compare" in output


def test_run_replay_requires_exactly_one_mode(capsys) -> None:
    assert main(["run", "replay"]) == 1
    assert "RUN_ID" in capsys.readouterr().err

    assert main(["run", "replay", "run-id", "--workflow", "energy/forecast"]) == 1
    assert "not both" in capsys.readouterr().err


def test_run_replay_single_submits_and_prints_detail(monkeypatch, capsys) -> None:
    observed: dict[str, Any] = {}

    def fake_replay_run(self, run_id, *, version=None, parameters=None):
        observed.update({"run_id": run_id, "version": version, "parameters": parameters})
        data = {"id": "replay-id", "status": "queued", "replay_of": run_id, "target_version_id": "version-1"}
        return Run("replay-id", client=self, data=data)

    monkeypatch.setattr(Client, "replay_run", fake_replay_run)

    assert main(["run", "replay", "run-id", "--no-wait"]) == 0

    output = capsys.readouterr().out
    assert "Replay Run" in output
    assert "replay-id" in output
    assert "run-id" in output
    assert observed == {"run_id": "run-id", "version": None, "parameters": None}


def test_run_replay_code_latest_and_params_are_forwarded(monkeypatch, capsys) -> None:
    observed: dict[str, Any] = {}

    def fake_replay_run(self, run_id, *, version=None, parameters=None):
        observed.update({"version": version, "parameters": parameters})
        return Run("replay-id", client=self, data={"id": "replay-id", "status": "queued", "replay_of": run_id})

    monkeypatch.setattr(Client, "replay_run", fake_replay_run)

    assert main(["run", "replay", "run-id", "--code", "latest", "-p", "horizon=48", "--no-wait"]) == 0

    assert observed == {"version": "latest", "parameters": {"horizon": 48}}


def _patch_replay_wait(monkeypatch, *, original_result, replay_result, replay_status="succeeded") -> None:
    def fake_replay_run(self, run_id, *, version=None, parameters=None):
        return Run("replay-id", client=self, data={"id": "replay-id", "status": "queued", "replay_of": run_id})

    def fake_get_run(self, run_id):
        if run_id == "replay-id":
            return {
                "id": "replay-id",
                "status": replay_status,
                "result": replay_result,
                "error": "boom" if replay_status == "failed" else None,
                "started_at": "2026-07-11T09:00:00+00:00",
                "finished_at": "2026-07-11T09:00:05+00:00",
            }
        return {
            "id": run_id,
            "status": "succeeded",
            "result": original_result,
            "started_at": "2026-07-10T09:00:00+00:00",
            "finished_at": "2026-07-10T09:00:04+00:00",
        }

    monkeypatch.setattr(Client, "replay_run", fake_replay_run)
    monkeypatch.setattr(Client, "get_run", fake_get_run)


def test_run_replay_wait_reports_identical_results(monkeypatch, capsys) -> None:
    _patch_replay_wait(monkeypatch, original_result={"forecast": 42.0}, replay_result={"forecast": 42.0})

    assert main(["run", "replay", "run-id"]) == 0

    output = capsys.readouterr().out
    assert "Replay Comparison" in output
    assert "identical" in output
    assert "succeeded -> succeeded" in output


def test_run_replay_wait_reports_differing_keys(monkeypatch, capsys) -> None:
    _patch_replay_wait(
        monkeypatch,
        original_result={"forecast": 42.0, "site": "a"},
        replay_result={"forecast": 43.5, "site": "a"},
    )

    assert main(["run", "replay", "run-id"]) == 0

    output = capsys.readouterr().out
    assert "differs" in output
    assert "forecast" in output


def test_run_replay_wait_exits_nonzero_when_replay_fails(monkeypatch, capsys) -> None:
    _patch_replay_wait(monkeypatch, original_result={"forecast": 42.0}, replay_result=None, replay_status="failed")

    assert main(["run", "replay", "run-id"]) == 1

    output = capsys.readouterr().out
    assert "succeeded -> failed" in output


def _patch_batch_workflow(monkeypatch) -> None:
    monkeypatch.setattr(
        "rebase.cli._resolve_workflow_selector",
        lambda client, name, workflow_id=None, project_name=None: {"id": "workflow-id", "name": name},
    )


def test_run_replay_batch_excludes_replays_and_compares(monkeypatch, capsys) -> None:
    replayed: list[str] = []
    _patch_batch_workflow(monkeypatch)

    def fake_list_runs(self, **kwargs):
        assert kwargs["workflow_id"] == "workflow-id"
        assert kwargs["target_type"] == "workflow"
        assert kwargs["limit"] == 500
        assert kwargs["since"] is not None
        return [
            {"id": "run-1", "status": "succeeded", "trigger_source": "schedule", "created_at": "2026-07-09T09:00:00Z"},
            {"id": "run-2", "status": "succeeded", "trigger_source": "api", "created_at": "2026-07-10T09:00:00Z"},
            {"id": "run-3", "status": "succeeded", "trigger_source": "replay", "created_at": "2026-07-10T10:00:00Z"},
        ]

    def fake_replay_run(self, run_id, *, version=None, parameters=None):
        replayed.append(run_id)
        replay_id = f"replay-{run_id}"
        return Run(replay_id, client=self, data={"id": replay_id, "status": "queued", "replay_of": run_id})

    results = {
        "run-1": {"forecast": 1.0},
        "replay-run-1": {"forecast": 1.0},
        "run-2": {"forecast": 2.0},
        "replay-run-2": {"forecast": 99.0},
    }

    monkeypatch.setattr(Client, "list_runs", fake_list_runs)
    monkeypatch.setattr(Client, "replay_run", fake_replay_run)
    monkeypatch.setattr(
        Client, "get_run", lambda self, run_id: {"id": run_id, "status": "succeeded", "result": results[run_id]}
    )

    assert main(["run", "replay", "--workflow", "energy/forecast", "--since", "7d", "--yes"]) == 0

    output = capsys.readouterr().out
    assert "Replay Candidates" in output
    assert "Replays" in output
    assert replayed == ["run-1", "run-2"]  # the run-3 replay is never replayed again
    assert "identical" in output
    assert "differs" in output
    assert "forecast" in output


def test_run_replay_batch_json_rows_and_failure_exit_code(monkeypatch, capsys) -> None:
    _patch_batch_workflow(monkeypatch)
    monkeypatch.setattr(
        Client,
        "list_runs",
        lambda self, **kwargs: [{"id": "run-1", "status": "succeeded", "trigger_source": "api"}],
    )
    monkeypatch.setattr(
        Client,
        "replay_run",
        lambda self, run_id, **kwargs: Run(
            "replay-run-1", client=self, data={"id": "replay-run-1", "status": "queued", "replay_of": run_id}
        ),
    )

    def fake_get_run(self, run_id):
        if run_id == "replay-run-1":
            return {"id": run_id, "status": "failed", "error": "boom"}
        return {"id": run_id, "status": "succeeded", "result": {"forecast": 1.0}}

    monkeypatch.setattr(Client, "get_run", fake_get_run)

    assert main(["run", "replay", "--workflow", "energy/forecast", "--since", "24h", "--yes", "--json"]) == 1

    rows = json.loads(capsys.readouterr().out)
    assert rows == [
        {
            "original": "run-1",
            "replay": "replay-run-1",
            "original_status": "succeeded",
            "replay_status": "failed",
            "result_identical": None,
            "differing_keys": None,
        }
    ]


def test_run_replay_batch_no_compare_submits_and_exits_zero(monkeypatch, capsys) -> None:
    _patch_batch_workflow(monkeypatch)
    monkeypatch.setattr(
        Client,
        "list_runs",
        lambda self, **kwargs: [{"id": "run-1", "status": "failed", "trigger_source": "api"}],
    )
    monkeypatch.setattr(
        Client,
        "replay_run",
        lambda self, run_id, **kwargs: Run(
            "replay-run-1", client=self, data={"id": "replay-run-1", "status": "queued", "replay_of": run_id}
        ),
    )
    monkeypatch.setattr(Client, "get_run", lambda self, run_id: (_ for _ in ()).throw(AssertionError("no polling")))

    assert main(["run", "replay", "--workflow", "energy/forecast", "--since", "24h", "--yes", "--no-compare"]) == 0

    output = capsys.readouterr().out
    assert "replay-run-1" in output


def test_run_replay_batch_decline_aborts_without_replaying(monkeypatch, capsys) -> None:
    import click

    _patch_batch_workflow(monkeypatch)
    monkeypatch.setattr(
        Client,
        "list_runs",
        lambda self, **kwargs: [{"id": "run-1", "status": "succeeded", "trigger_source": "api"}],
    )
    monkeypatch.setattr(
        Client,
        "replay_run",
        lambda self, run_id, **kwargs: (_ for _ in ()).throw(AssertionError("declined confirm must not replay")),
    )
    monkeypatch.setattr("typer.confirm", lambda *args, **kwargs: (_ for _ in ()).throw(click.Abort()))

    assert main(["run", "replay", "--workflow", "energy/forecast", "--since", "24h"]) == 1


def test_run_replay_batch_requires_since(capsys) -> None:
    assert main(["run", "replay", "--workflow", "energy/forecast"]) == 1
    assert "--since" in capsys.readouterr().err


def test_run_replay_batch_no_candidates_is_friendly(monkeypatch, capsys) -> None:
    _patch_batch_workflow(monkeypatch)
    monkeypatch.setattr(Client, "list_runs", lambda self, **kwargs: [])

    assert main(["run", "replay", "--workflow", "energy/forecast", "--since", "7d"]) == 0

    assert "No matching runs" in capsys.readouterr().out


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
    workdir = tmp_path / "workdir"
    workdir.mkdir()
    monkeypatch.chdir(workdir)

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
            "values": [
                "Join workspace (Team)",
                setup_module.CREATE_WORKSPACE,
                setup_module.SWITCH_ACCOUNT,
            ],
            "default": "Join workspace (Team)",
            "title": "You were invited to a workspace. What do you want to do?",
        }
    ]


def _github_session(email: str = "1234+bob@users.noreply.github.com") -> AuthSession:
    return AuthSession(
        access_token="stored-token",
        refresh_token="refresh-token",
        expires_at=2_000_000_000,
        token_type="bearer",
        email=email,
        provider="github",
    )


def _auth_args(**overrides: Any) -> SimpleNamespace:
    args = SimpleNamespace(
        profile="default",
        api_url=None,
        provider="github",
        force_auth=False,
        callback_port=17658,
        auth_timeout=300,
        no_browser=False,
    )
    for key, value in overrides.items():
        setattr(args, key, value)
    return args


def test_setup_offers_to_switch_account_when_a_session_is_stored(monkeypatch) -> None:
    from rebase import setup as setup_module

    calls: list[str] = []
    observed: dict[str, Any] = {}

    monkeypatch.setattr(setup_module, "_can_prompt", lambda: True)
    monkeypatch.setattr(setup_module, "load_access_token", lambda: "stored-token")
    monkeypatch.setattr(setup_module, "load_session", _github_session)

    def fake_choose(label: str, values: list[str], *, default: str | None = None, title: str | None = None) -> str:
        observed.update({"label": label, "values": values, "default": default, "title": title})
        return setup_module.SWITCH_ACCOUNT

    monkeypatch.setattr(setup_module, "_choose", fake_choose)
    monkeypatch.setattr(setup_module, "clear_session", lambda: calls.append("clear_session") or True)
    monkeypatch.setattr(
        setup_module,
        "_oauth_session",
        lambda args, config: calls.append(f"oauth:provider={args.provider}:force={args.force_auth}") or "new-token",
    )

    assert setup_module._access_token(_auth_args(), {}) == "new-token"
    assert calls == ["clear_session", "oauth:provider=None:force=True"]
    assert observed["values"] == [
        "Continue as 1234+bob@users.noreply.github.com (via github)",
        setup_module.SWITCH_ACCOUNT,
    ]
    assert observed["default"] == "Continue as 1234+bob@users.noreply.github.com (via github)"


def test_setup_signs_in_fresh_when_the_stored_session_is_for_another_supabase_project(monkeypatch) -> None:
    """A session is minted by one Supabase project and means nothing to another."""
    from rebase import setup as setup_module

    hints: list[str] = []
    monkeypatch.setattr(setup_module, "_can_prompt", lambda: True)
    monkeypatch.setattr(setup_module, "load_access_token", lambda: pytest.fail("must not refresh the wrong session"))
    monkeypatch.setattr(
        setup_module,
        "load_session",
        lambda: AuthSession(
            access_token="stored-token",
            refresh_token="refresh-token",
            expires_at=2_000_000_000,
            token_type="bearer",
            supabase_url="https://oyldgfpjnmfzsradovyi.supabase.co",
        ),
    )
    monkeypatch.setattr(setup_module, "_choose", lambda *args, **kwargs: pytest.fail("must not offer Continue as"))
    monkeypatch.setattr(setup_module, "_hint", hints.append)
    monkeypatch.setattr(setup_module, "_oauth_session", lambda args, config: "new-token")
    config = {"supabase_url": "https://rgglvcamwgatiwfztqyd.supabase.co/", "supabase_anon_key": "anon"}

    assert setup_module._access_token(_auth_args(), config) == "new-token"
    assert hints and "oyldgfpjnmfzsradovyi" in hints[0] and "rgglvcamwgatiwfztqyd" in hints[0]


def test_setup_still_offers_the_stored_session_when_the_supabase_project_matches(monkeypatch) -> None:
    from rebase import setup as setup_module

    offered: list[list[str]] = []
    monkeypatch.setattr(setup_module, "_can_prompt", lambda: True)
    monkeypatch.setattr(setup_module, "load_access_token", lambda: "stored-token")
    monkeypatch.setattr(
        setup_module,
        "load_session",
        lambda: AuthSession(
            access_token="stored-token",
            refresh_token="refresh-token",
            expires_at=2_000_000_000,
            token_type="bearer",
            email="bob@example.com",
            supabase_url="https://rgglvcamwgatiwfztqyd.supabase.co",
        ),
    )

    def choose(label: str, values: list[str], **kwargs: Any) -> str:
        offered.append(values)
        return values[0]

    monkeypatch.setattr(setup_module, "_choose", choose)
    config = {"supabase_url": "https://rgglvcamwgatiwfztqyd.supabase.co/", "supabase_anon_key": "anon"}

    assert setup_module._access_token(_auth_args(), config) == "stored-token"
    assert offered and offered[0][0].startswith("Continue as bob@example.com")


def test_setup_keeps_the_stored_session_when_you_continue(monkeypatch) -> None:
    from rebase import setup as setup_module

    monkeypatch.setattr(setup_module, "_can_prompt", lambda: True)
    monkeypatch.setattr(setup_module, "load_access_token", lambda: "stored-token")
    monkeypatch.setattr(setup_module, "load_session", _github_session)
    monkeypatch.setattr(setup_module, "_choose", lambda label, values, **kwargs: values[0])
    monkeypatch.setattr(setup_module, "_oauth_session", lambda args, config: pytest.fail("should not re-authenticate"))

    assert setup_module._access_token(_auth_args(), {}) == "stored-token"


def test_setup_does_not_ask_about_the_account_without_a_terminal(monkeypatch) -> None:
    from rebase import setup as setup_module

    monkeypatch.setattr(setup_module, "_can_prompt", lambda: False)
    monkeypatch.setattr(setup_module, "load_access_token", lambda: "stored-token")
    monkeypatch.setattr(setup_module, "load_session", _github_session)
    monkeypatch.setattr(setup_module, "_choose", lambda *args, **kwargs: pytest.fail("should not prompt"))

    assert setup_module._access_token(_auth_args(), {}) == "stored-token"


def test_setup_workspace_join_without_access_offers_to_switch_account(monkeypatch, capsys) -> None:
    from rebase import setup as setup_module

    class FakeClient:
        def list_my_workspaces(self) -> list[dict[str, Any]]:
            return []

    monkeypatch.setattr(setup_module, "_can_prompt", lambda: True)
    monkeypatch.setattr(setup_module, "_prompt", lambda *args, **kwargs: "acme")

    def fake_choose(label: str, values: list[str], *, default: str | None = None, title: str | None = None) -> str:
        return setup_module.JOIN_WORKSPACE if label == "workspace setup" else setup_module.SWITCH_ACCOUNT

    monkeypatch.setattr(setup_module, "_choose", fake_choose)

    with pytest.raises(setup_module._SwitchAccountRequested):
        setup_module._select_workspace(
            SimpleNamespace(workspace=None, workspace_name=None, handle=None),
            FakeClient(),
            session=_github_session(),
        )

    output = capsys.readouterr().out
    assert "You do not have access to workspace 'acme'." in output
    assert "signed in as 1234+bob@users.noreply.github.com (via github)" in output


def test_setup_workspace_join_without_access_names_the_account_when_scripted(monkeypatch) -> None:
    from rebase import setup as setup_module

    class FakeClient:
        def list_my_workspaces(self) -> list[dict[str, Any]]:
            return []

    monkeypatch.setattr(setup_module, "_can_prompt", lambda: False)
    monkeypatch.setattr(setup_module, "_prompt", lambda *args, **kwargs: "acme")
    monkeypatch.setattr(setup_module, "_choose", lambda *args, **kwargs: setup_module.JOIN_WORKSPACE)

    with pytest.raises(setup_module.RebaseWorkflowError) as excinfo:
        setup_module._select_workspace(
            SimpleNamespace(workspace=None, workspace_name=None, handle=None),
            FakeClient(),
            session=_github_session(),
        )

    message = str(excinfo.value)
    assert "1234+bob@users.noreply.github.com (via github)" in message
    assert "--force-auth" in message


def test_setup_beta_enrollment_error_names_the_account() -> None:
    from rebase import setup as setup_module

    error = setup_module._workspace_creation_error(
        setup_module.RebaseWorkflowError("creating a workspace requires a platform beta invite"),
        session=_github_session(),
    )

    assert "not enrolled in the beta program" in str(error)
    assert "1234+bob@users.noreply.github.com (via github)" in str(error)


def test_setup_retries_the_workspace_step_after_switching_account(monkeypatch, tmp_path) -> None:
    from rebase import setup as setup_module

    calls: list[str] = []
    monkeypatch.setenv("REBASE_CONFIG_PATH", str(tmp_path / "config.json"))
    monkeypatch.chdir(tmp_path)

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
            return {"supabase_url": "https://project.supabase.co", "supabase_anon_key": "anon"}

    def fake_select_workspace(args: Any, client: Any, *, session: Any) -> dict[str, Any]:
        calls.append(f"select_workspace:{client.access_token}")
        if client.access_token == "stored-token":
            raise setup_module._SwitchAccountRequested
        return {"id": "acme", "name": "Acme"}

    monkeypatch.setattr(setup_module, "Client", FakeClient)
    monkeypatch.setattr(setup_module, "_access_token", lambda args, config: "stored-token")
    monkeypatch.setattr(setup_module, "load_session", lambda: None)
    monkeypatch.setattr(setup_module, "_select_workspace", fake_select_workspace)
    monkeypatch.setattr(
        setup_module,
        "_switch_account",
        lambda args, config: calls.append("switch_account") or "google-token",
    )

    assert setup_module.run_setup(_auth_args(handle=None, workspace=None, workspace_name=None)) == 0
    assert calls == [
        "select_workspace:stored-token",
        "switch_account",
        "select_workspace:google-token",
    ]

    data = json.loads((tmp_path / "config.json").read_text(encoding="utf-8"))
    assert data["profiles"]["default"]["workspace_id"] == "acme"


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


def _workspace_create_fake_client(calls: list[tuple[str, str | None]]) -> Any:
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

    return FakeClient()


def test_setup_workspace_create_claims_profile_handle_first(monkeypatch, tmp_path: Path) -> None:
    from rebase import setup as setup_module

    calls: list[tuple[str, str | None]] = []
    repo = tmp_path / "agent-work"
    repo.mkdir()
    monkeypatch.chdir(repo)

    monkeypatch.setattr(setup_module, "_choose", lambda *args, **kwargs: setup_module.CREATE_WORKSPACE)

    def fake_prompt(value: str | None, message: str, *, default: str | None = None) -> str:
        if message == "Choose your Rebase handle":
            assert default == "sebastian"
            return "SebaHeg"
        assert message == "Workspace handle to create"
        # The directory, not the user handle: a workspace is 1:1 with a repo.
        assert default == "agent-work"
        return default or "fallback"

    monkeypatch.setattr(setup_module, "_prompt", fake_prompt)

    workspace = setup_module._select_workspace(
        SimpleNamespace(workspace=None, workspace_name=None, handle=None),
        _workspace_create_fake_client(calls),
        session=SimpleNamespace(email="sebastian@rebase.energy"),
    )

    assert workspace["id"] == "agent-work"
    assert calls == [
        ("get_profile", None),
        ("update_profile", "sebaheg"),
        ("create_workspace", "agent-work"),
    ]


def test_setup_workspace_create_falls_back_to_handle_for_unusable_dir(monkeypatch, tmp_path: Path) -> None:
    """A directory name too short to be a handle leaves the old default in place."""
    from rebase import setup as setup_module

    calls: list[tuple[str, str | None]] = []
    repo = tmp_path / "ab"
    repo.mkdir()
    monkeypatch.chdir(repo)

    monkeypatch.setattr(setup_module, "_choose", lambda *args, **kwargs: setup_module.CREATE_WORKSPACE)

    def fake_prompt(value: str | None, message: str, *, default: str | None = None) -> str:
        if message == "Choose your Rebase handle":
            return "SebaHeg"
        assert default == "sebaheg"
        return default or "fallback"

    monkeypatch.setattr(setup_module, "_prompt", fake_prompt)

    workspace = setup_module._select_workspace(
        SimpleNamespace(workspace=None, workspace_name=None, handle=None),
        _workspace_create_fake_client(calls),
        session=SimpleNamespace(email="sebastian@rebase.energy"),
    )

    assert workspace["id"] == "sebaheg"


def test_workspace_create_helper_creates_workspace_without_github_prompt(monkeypatch, tmp_path: Path) -> None:
    from rebase import setup as setup_module

    calls: list[str] = []
    config_path = tmp_path / "config.json"
    monkeypatch.setenv("REBASE_CONFIG_PATH", str(config_path))
    workdir = tmp_path / "energy-team"
    workdir.mkdir()
    monkeypatch.chdir(workdir)

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
    marker = workdir / ".rebase" / "config.json"
    assert json.loads(marker.read_text(encoding="utf-8")) == {
        "workspace": "energy-team",
        "workspace_name": "Energy Team",
    }
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
    monkeypatch.setattr(setup_module, "_huggingface_login_function", lambda: lambda **kwargs: None)
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
    monkeypatch.setattr(setup_module, "_huggingface_login_function", lambda: lambda **kwargs: None)

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
        lambda selected, **kwargs: calls.append(f"local:{selected['repo_owner']}/{selected['repo_name']}"),
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


def test_connect_github_helper_resolves_active_profile_when_omitted(monkeypatch) -> None:
    from rebase import setup as setup_module

    observed: dict[str, Any] = {}
    connection = {
        "scope": "workspace",
        "project_id": None,
        "repo_owner": "rebase",
        "repo_name": "workspace-repo",
    }

    class FakeClient:
        def __init__(self, *, api_url: str | None = None, profile: str | None = None) -> None:
            observed["profile"] = profile
            self.workspace_id = "rebase-workspace"

        def setup_config(self) -> dict[str, Any]:
            return {"github_app_configured": True}

        def list_github_repo_connections(self) -> list[dict[str, Any]]:
            return [connection]

    monkeypatch.setattr(setup_module, "Client", FakeClient)
    monkeypatch.setattr(setup_module, "selected_profile_name", lambda: "workspace-profile")
    monkeypatch.setattr(setup_module, "_ensure_local_workspace_repo", lambda selected, **kwargs: None)
    monkeypatch.setattr(setup_module, "_verify_github_app_access", lambda *args, **kwargs: None)

    assert (
        setup_module.run_connect_github(
            SimpleNamespace(
                profile=None,
                api_url=None,
                no_browser=True,
                github_installation_id=None,
                github_timeout=1,
                poll_interval=0,
                repo=None,
                repo_path=None,
                create_repo=False,
            )
        )
        == 0
    )

    assert observed["profile"] == "workspace-profile"


def test_connect_github_helper_reconnects_stale_workspace_connection(monkeypatch, capsys) -> None:
    from rebase import setup as setup_module

    calls: list[str] = []
    stale_connection = {
        "scope": "workspace",
        "project_id": None,
        "repo_owner": "rebase-energy",
        "repo_name": "rebase-grid",
    }
    active_connection = {
        "scope": "workspace",
        "project_id": None,
        "repo_owner": "rebase-energy",
        "repo_name": "rebase-workspace",
    }

    class FakeClient:
        def __init__(self, *, api_url: str | None = None, profile: str | None = None) -> None:
            self.workspace_id = "rebase-workspace"

        def setup_config(self) -> dict[str, Any]:
            return {"github_app_configured": True}

        def get_workspace(self) -> dict[str, Any]:
            return {
                "id": "rebase-workspace",
                "repo_owner": "rebase-energy",
                "repo_name": "rebase-workspace",
            }

        def list_github_repo_connections(self) -> list[dict[str, Any]]:
            calls.append("list_connections")
            return [stale_connection]

    def fake_connect(args: Any, client: Any, *, workspace_id: str) -> dict[str, Any]:
        calls.append(f"connect:{workspace_id}:{args.repo}")
        return active_connection

    monkeypatch.setattr(setup_module, "Client", FakeClient)
    monkeypatch.setattr(setup_module, "selected_profile_name", lambda: "workspace-profile")
    monkeypatch.setattr(setup_module, "_connect_github", fake_connect)
    monkeypatch.setattr(
        setup_module,
        "_ensure_local_workspace_repo",
        lambda selected, **kwargs: calls.append(f"local:{selected['repo_owner']}/{selected['repo_name']}"),
    )
    monkeypatch.setattr(
        setup_module,
        "_verify_github_app_access",
        lambda args, client, selected, *, workspace_id: calls.append(
            f"app:{workspace_id}:{selected['repo_owner']}/{selected['repo_name']}"
        ),
    )

    assert (
        setup_module.run_connect_github(
            SimpleNamespace(
                profile=None,
                api_url=None,
                no_browser=True,
                github_installation_id=None,
                github_timeout=1,
                poll_interval=0,
                repo=None,
                repo_path=None,
                create_repo=False,
            )
        )
        == 0
    )

    output = capsys.readouterr().out
    assert "No workspace-level GitHub repository connection found for rebase-energy/rebase-workspace" in output
    assert "Workspace is connected to rebase-energy/rebase-grid" not in output
    assert calls == [
        "list_connections",
        "connect:rebase-workspace:rebase-energy/rebase-workspace",
        "list_connections",
        "local:rebase-energy/rebase-workspace",
        "app:rebase-workspace:rebase-energy/rebase-workspace",
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
    monkeypatch.setattr(setup_module, "_ensure_local_workspace_repo", lambda selected, **kwargs: calls.append("local"))
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
    monkeypatch.setattr(setup_module, "_remote_branch_exists", lambda cwd, branch: branch == "main")
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
        ),
    ]


def test_github_connect_seeds_empty_workspace_repo_before_checkout(monkeypatch, tmp_path: Path) -> None:
    from rebase import setup as setup_module

    calls: list[tuple[str, object]] = []
    state: dict[str, object] = {"root": None, "seeded": False}
    (tmp_path / ".venv").mkdir()

    class FakeClient:
        def create_github_starter_workflow(self, connection_id: str) -> dict[str, str]:
            calls.append(("starter", connection_id))
            state["seeded"] = True
            return {"path": ".rebase/starter_workflow.py", "commit_sha": "abc123"}

    def fake_run_git(args: list[str], *, cwd: Path, action: str, timeout: float = 60) -> None:
        calls.append(("git", (args, cwd, action, timeout)))
        if args == ["init"]:
            state["root"] = tmp_path

    def fake_remote_branches(cwd: Path) -> list[str]:
        _ = cwd
        return ["origin/main"] if state["seeded"] else []

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(setup_module, "_confirm", lambda message, *, default: True)
    monkeypatch.setattr(
        setup_module,
        "_choose",
        lambda label, values, *, default=None, title=None: "SSH (git@github.com:rebase/platform.git)",
    )
    monkeypatch.setattr(setup_module, "_current_git_root", lambda: state["root"])
    monkeypatch.setattr(setup_module, "_local_github_remote", lambda cwd=None: "rebase/platform")
    monkeypatch.setattr(setup_module, "_remote_branches", fake_remote_branches)
    monkeypatch.setattr(setup_module, "_remote_branch_exists", lambda cwd, branch: state["seeded"] and branch == "main")
    monkeypatch.setattr(setup_module, "_run_git", fake_run_git)

    setup_module._ensure_local_workspace_repo(
        {
            "id": "connection-id",
            "repo_owner": "rebase",
            "repo_name": "platform",
            "default_branch": "main",
        },
        client=FakeClient(),
    )

    assert calls == [
        ("git", (["init"], tmp_path, "initialize a git repository", 60)),
        ("git", (["remote", "add", "origin", "git@github.com:rebase/platform.git"], tmp_path, "add GitHub origin", 60)),
        ("git", (["fetch", "origin"], tmp_path, "fetch rebase/platform", 300)),
        ("starter", "connection-id"),
        ("git", (["fetch", "origin"], tmp_path, "fetch starter workflow", 300)),
        ("git", (["checkout", "-B", "main", "origin/main"], tmp_path, "check out origin/main", 60)),
    ]


def test_github_connect_repairs_partial_empty_repo_checkout(monkeypatch, tmp_path: Path) -> None:
    from rebase import setup as setup_module

    calls: list[tuple[str, object]] = []
    state: dict[str, bool] = {"seeded": False, "head": False}

    class FakeClient:
        def create_github_starter_workflow(self, connection_id: str) -> dict[str, str]:
            calls.append(("starter", connection_id))
            state["seeded"] = True
            return {"path": ".rebase/starter_workflow.py", "commit_sha": "abc123"}

    def fake_run_git(args: list[str], *, cwd: Path, action: str, timeout: float = 60) -> None:
        calls.append(("git", (args, cwd, action, timeout)))
        if args == ["checkout", "-B", "main", "origin/main"]:
            state["head"] = True

    monkeypatch.setattr(setup_module, "_current_git_root", lambda: tmp_path)
    monkeypatch.setattr(setup_module, "_local_github_remote", lambda cwd=None: "rebase/platform")
    monkeypatch.setattr(setup_module, "_ensure_workspace_origin_transport", lambda cwd, repo_full_name: None)
    monkeypatch.setattr(setup_module, "_has_local_head", lambda cwd: state["head"])
    monkeypatch.setattr(
        setup_module,
        "_remote_branches",
        lambda cwd: ["origin/main"] if state["seeded"] else [],
    )
    monkeypatch.setattr(setup_module, "_remote_branch_exists", lambda cwd, branch: state["seeded"] and branch == "main")
    monkeypatch.setattr(setup_module, "_run_git", fake_run_git)

    setup_module._ensure_local_workspace_repo(
        {
            "id": "connection-id",
            "repo_owner": "rebase",
            "repo_name": "platform",
            "default_branch": "main",
        },
        client=FakeClient(),
    )

    assert calls == [
        ("git", (["fetch", "origin"], tmp_path, "fetch rebase/platform", 300)),
        ("starter", "connection-id"),
        ("git", (["fetch", "origin"], tmp_path, "fetch starter workflow", 300)),
        ("git", (["checkout", "-B", "main", "origin/main"], tmp_path, "check out origin/main", 60)),
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


def test_github_connect_preserves_matching_https_origin(monkeypatch, tmp_path: Path) -> None:
    from rebase import setup as setup_module

    calls: list[tuple[list[str], Path, str]] = []
    choices: list[tuple[str, list[str]]] = []

    def fake_run_git(args: list[str], *, cwd: Path, action: str, timeout: float = 60) -> None:
        _ = timeout
        calls.append((args, cwd, action))

    monkeypatch.setattr(setup_module, "_current_git_root", lambda: tmp_path)
    monkeypatch.setattr(setup_module, "_local_github_remote", lambda cwd=None: "rebase/platform")
    monkeypatch.setattr(setup_module, "_origin_url", lambda cwd: "https://github.com/rebase/platform.git")
    monkeypatch.setattr(setup_module, "_has_local_head", lambda cwd: True)
    monkeypatch.setattr(
        setup_module,
        "_choose",
        lambda label, values, *, default=None, title=None: choices.append((label, values)),
    )
    monkeypatch.setattr(setup_module, "_run_git", fake_run_git)

    setup_module._ensure_local_workspace_repo({"repo_owner": "rebase", "repo_name": "platform"})

    assert calls == []
    assert choices == []


def test_github_connect_can_switch_matching_https_origin_when_prompted(monkeypatch, tmp_path: Path) -> None:
    from rebase import setup as setup_module

    calls: list[tuple[list[str], Path, str]] = []

    def fake_run_git(args: list[str], *, cwd: Path, action: str, timeout: float = 60) -> None:
        _ = timeout
        calls.append((args, cwd, action))

    monkeypatch.setattr(setup_module, "_origin_url", lambda cwd: "https://github.com/rebase/platform.git")
    monkeypatch.setattr(
        setup_module,
        "_choose",
        lambda label, values, *, default=None, title=None: "SSH (git@github.com:rebase/platform.git)",
    )
    monkeypatch.setattr(setup_module, "_run_git", fake_run_git)

    setup_module._ensure_workspace_origin_transport(tmp_path, "rebase/platform", prompt=True)

    assert calls == [
        (
            ["remote", "set-url", "origin", "git@github.com:rebase/platform.git"],
            tmp_path,
            "update GitHub origin",
        )
    ]


def test_git_password_auth_failure_adds_github_hint(monkeypatch, tmp_path: Path) -> None:
    from rebase import setup as setup_module

    def fake_run(*args: Any, **kwargs: Any) -> object:
        _ = args, kwargs
        raise subprocess.CalledProcessError(
            128,
            ["git", "fetch", "origin"],
            stderr=(
                "remote: Invalid username or token. Password authentication is not supported for Git operations.\n"
                "fatal: Authentication failed"
            ),
        )

    monkeypatch.setattr(setup_module.subprocess, "run", fake_run)

    with pytest.raises(setup_module.RebaseWorkflowError, match="GitHub no longer accepts account passwords"):
        setup_module._run_git(["fetch", "origin"], cwd=tmp_path, action="fetch rebase/platform")


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


def test_workspace_lists_memberships_with_connected_repos(monkeypatch, tmp_path: Path, capsys) -> None:
    config_path = tmp_path / "config.json"
    monkeypatch.setenv("REBASE_CONFIG_PATH", str(config_path))
    config_path.write_text(
        json.dumps(
            {
                "default_profile": "prod",
                "profiles": {
                    "dev": {"api_key": "rbw_dev", "workspace_id": "dev", "workspace_name": "Development"},
                    "prod": {"api_key": "rbw_prod", "workspace_id": "prod", "workspace_name": "Production"},
                },
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        Client,
        "list_my_workspaces",
        lambda self: [
            {"id": "dev", "name": "Development", "role": "Developer"},
            {"id": "prod", "name": "Production", "role": "Owner"},
        ],
    )

    def fake_request(self: Client, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        assert (method, path) == ("GET", "/workspace")
        workspace_id = kwargs["headers"]["X-Rebase-Workspace"]
        if workspace_id == "dev":
            return {
                "id": "dev",
                "name": "Development",
                "source_mode": "workspace_repo",
                "repo_owner": "rebase",
                "repo_name": "platform-dev",
                "repo_path": "projects/dev",
            }
        return {
            "id": "prod",
            "name": "Production",
            "source_mode": "rebase_hosted",
            "repo_owner": None,
            "repo_name": None,
            "repo_path": None,
        }

    monkeypatch.setattr(Client, "request", fake_request)

    assert main(["workspace", "list"]) == 0

    output = capsys.readouterr().out
    assert "Workspaces" in output
    assert "dev" in output
    assert "Development" in output
    assert "Developer" in output
    assert "rebase/platform-dev" in output
    assert "prod" in output
    assert "Production" in output
    assert "Owner" in output
    assert "*" in output


def test_workspace_command_lists_memberships(monkeypatch, tmp_path: Path, capsys) -> None:
    monkeypatch.setenv("REBASE_CONFIG_PATH", str(tmp_path / "config.json"))
    monkeypatch.setattr(
        Client,
        "list_my_workspaces",
        lambda self: [{"id": "workspace-id", "name": "ACME", "role": "Owner"}],
    )
    monkeypatch.setattr(
        Client,
        "request",
        lambda self, method, path, **kwargs: {
            "id": "workspace-id",
            "name": "ACME",
            "source_mode": "rebase_hosted",
            "repo_owner": None,
            "repo_name": None,
            "repo_path": None,
        },
    )

    assert main(["workspace"]) == 0

    output = capsys.readouterr().out
    assert "Workspaces" in output
    assert "workspace-id" in output
    assert "ACME" in output


def test_profile_command_shows_active_profile_and_auth(monkeypatch, tmp_path: Path, capsys) -> None:
    config_path = tmp_path / "config.json"
    auth_path = tmp_path / "auth.json"
    monkeypatch.setenv("REBASE_CONFIG_PATH", str(config_path))
    monkeypatch.setenv("REBASE_AUTH_FILE", str(auth_path))
    config_path.write_text(
        json.dumps(
            {
                "default_profile": "prod",
                "profiles": {
                    "prod": {
                        "api_url": "https://api.example.test",
                        "workspace_id": "workspace-id",
                        "workspace_name": "ACME",
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    auth_path.write_text(
        json.dumps(
            AuthSession(
                access_token="access-token",
                refresh_token="refresh-token",
                expires_at=1893456000,
                token_type="bearer",
                email="joule@rebase.energy",
                user_id="user-id",
            ).to_json()
        ),
        encoding="utf-8",
    )

    assert main(["profile"]) == 0

    output = capsys.readouterr().out
    assert "Profile" in output
    assert "prod" in output
    assert "ACME" in output
    assert "joule@rebase.energy" in output
    assert "Config file" in output
    assert "Auth file" in output
    assert "access-token" not in output
    assert "refresh-token" not in output


def test_profile_list_renders_local_profiles(monkeypatch, tmp_path: Path, capsys) -> None:
    config_path = tmp_path / "config.json"
    auth_path = tmp_path / "auth.json"
    monkeypatch.setenv("REBASE_CONFIG_PATH", str(config_path))
    monkeypatch.setenv("REBASE_AUTH_FILE", str(auth_path))
    config_path.write_text(
        json.dumps(
            {
                "default_profile": "dev",
                "profiles": {
                    "dev": {"api_url": "https://dev.test", "workspace_id": "dev-workspace"},
                    "prod": {"api_key": "rbw_prod", "api_url": "https://prod.test", "workspace_id": "prod-workspace"},
                },
            }
        ),
        encoding="utf-8",
    )

    assert main(["profile", "list"]) == 0

    output = capsys.readouterr().out
    assert "Profiles" in output
    assert "dev-workspace" in output
    assert "prod" in output
    assert "api key" in output
    assert "rbw_prod" not in output


def test_profile_list_json_does_not_leak_api_keys(monkeypatch, tmp_path: Path, capsys) -> None:
    config_path = tmp_path / "config.json"
    monkeypatch.setenv("REBASE_CONFIG_PATH", str(config_path))
    config_path.write_text(
        json.dumps(
            {
                "default_profile": "prod",
                "profiles": {
                    "prod": {
                        "api_key": "rbw_secret",
                        "api_url": "https://api.example.test",
                        "workspace_id": "workspace-id",
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    assert main(["profile", "list", "--json"]) == 0

    output = capsys.readouterr().out
    data = json.loads(output)
    assert data["active_profile"] == "prod"
    assert data["profiles"] == [
        {
            "active": True,
            "profile": "prod",
            "workspace": "workspace-id",
            "workspace_id": "workspace-id",
            "api_url": "https://api.example.test",
            "has_api_key": True,
            "credential": "api key",
        }
    ]
    assert "rbw_secret" not in output


def test_profile_command_names_the_platform_only_when_not_production(monkeypatch, tmp_path: Path, capsys) -> None:
    """An ordinary install has one platform and never hears the word."""
    monkeypatch.setenv("REBASE_CONFIG_PATH", str(tmp_path / "config.json"))
    monkeypatch.setenv("REBASE_AUTH_FILE", str(tmp_path / "auth.json"))

    assert main(["profile"]) == 0
    assert "Platform" not in capsys.readouterr().out

    monkeypatch.setenv("REBASE_PLATFORM", "staging")
    assert main(["profile"]) == 0
    output = capsys.readouterr().out
    assert "Platform" in output and "staging" in output

    assert main(["profile", "show", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["platform"] == "staging"
    monkeypatch.setenv("REBASE_PLATFORM", "production")
    assert main(["profile", "show", "--json"]) == 0
    assert "platform" not in json.loads(capsys.readouterr().out)


def test_main_pins_a_non_default_platform_into_the_environment(monkeypatch, tmp_path: Path, capsys) -> None:
    """A running process keeps its platform even if the pointer file changes under it."""
    home = tmp_path / "home"
    (home / ".rebase").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("REBASE_PLATFORM", raising=False)
    monkeypatch.setenv("REBASE_CONFIG_PATH", str(tmp_path / "config.json"))
    monkeypatch.setenv("REBASE_AUTH_FILE", str(tmp_path / "auth.json"))

    assert main(["profile"]) == 0
    assert "REBASE_PLATFORM" not in os.environ

    (home / ".rebase" / "platform").write_text("staging\n", encoding="utf-8")
    assert main(["profile"]) == 0
    assert os.environ["REBASE_PLATFORM"] == "staging"
    assert "staging" in capsys.readouterr().out


def test_profile_show_json_returns_safe_profile_metadata(monkeypatch, tmp_path: Path, capsys) -> None:
    config_path = tmp_path / "config.json"
    auth_path = tmp_path / "auth.json"
    monkeypatch.setenv("REBASE_CONFIG_PATH", str(config_path))
    monkeypatch.setenv("REBASE_AUTH_FILE", str(auth_path))
    config_path.write_text(
        json.dumps(
            {
                "default_profile": "dev",
                "profiles": {
                    "dev": {"workspace_id": "dev-workspace"},
                    "prod": {"api_key": "rbw_prod", "workspace_id": "prod-workspace"},
                },
            }
        ),
        encoding="utf-8",
    )
    auth_path.write_text(
        json.dumps(
            AuthSession(
                access_token="access-token",
                refresh_token="refresh-token",
                expires_at=1893456000,
                token_type="bearer",
                email="sebastian@rebase.energy",
                user_id="user-id",
            ).to_json()
        ),
        encoding="utf-8",
    )

    assert main(["profile", "show", "prod", "--json"]) == 0

    output = capsys.readouterr().out
    data = json.loads(output)
    assert data["profile"] == "prod"
    assert data["active_profile"] == "dev"
    assert data["active"] is False
    assert data["workspace_id"] == "prod-workspace"
    assert data["has_api_key"] is True
    assert data["credential"] == "api key"
    assert data["auth"]["email"] == "sebastian@rebase.energy"
    assert data["config_file"] == str(config_path)
    assert data["auth"]["auth_file"] == str(auth_path)
    assert "rbw_prod" not in output
    assert "access-token" not in output
    assert "refresh-token" not in output


def test_profile_switch_changes_default_profile(monkeypatch, tmp_path: Path, capsys) -> None:
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

    assert main(["profile", "switch", "prod"]) == 0

    data = json.loads(config_path.read_text(encoding="utf-8"))
    assert data["default_profile"] == "prod"
    assert capsys.readouterr().out == "Switched profile to 'prod'\n"


def test_profile_logout_clears_auth_session_only(monkeypatch, tmp_path: Path, capsys) -> None:
    config_path = tmp_path / "config.json"
    auth_path = tmp_path / "auth.json"
    monkeypatch.setenv("REBASE_CONFIG_PATH", str(config_path))
    monkeypatch.setenv("REBASE_AUTH_FILE", str(auth_path))
    config_path.write_text(
        json.dumps({"default_profile": "dev", "profiles": {"dev": {"workspace_id": "workspace-id"}}}),
        encoding="utf-8",
    )
    auth_path.write_text(
        json.dumps(
            AuthSession(
                access_token="access-token",
                refresh_token="refresh-token",
                expires_at=1893456000,
                token_type="bearer",
            ).to_json()
        ),
        encoding="utf-8",
    )

    assert main(["profile", "logout"]) == 0

    assert config_path.exists()
    assert not auth_path.exists()
    assert f"Cleared Rebase auth session at {auth_path}" in capsys.readouterr().out


def test_auth_session_records_the_login_provider(monkeypatch, tmp_path: Path) -> None:
    from rebase.auth import load_session, parse_callback_url, save_session

    monkeypatch.setenv("REBASE_AUTH_FILE", str(tmp_path / "auth.json"))
    claims = {
        "email": "1234+bob@users.noreply.github.com",
        "sub": "user-id",
        "app_metadata": {"provider": "github"},
    }
    encoded = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    token = f"header.{encoded}.signature"

    session = parse_callback_url(f"http://127.0.0.1/auth/callback#access_token={token}&token_type=bearer")

    assert session.provider == "github"
    save_session(session)
    stored = load_session()
    assert stored is not None
    assert stored.provider == "github"


def test_profile_show_unknown_profile_errors(monkeypatch, tmp_path: Path) -> None:
    config_path = tmp_path / "config.json"
    monkeypatch.setenv("REBASE_CONFIG_PATH", str(config_path))
    config_path.write_text(
        json.dumps({"default_profile": "dev", "profiles": {"dev": {"workspace_id": "workspace-id"}}}),
        encoding="utf-8",
    )

    assert main(["profile", "show", "prod"]) == 1


def _config_with_profiles(tmp_path: Path, monkeypatch) -> Path:
    config_path = tmp_path / "config.json"
    monkeypatch.setenv("REBASE_CONFIG_PATH", str(config_path))
    config_path.write_text(
        json.dumps(
            {
                "default_profile": "work",
                "profiles": {
                    "work": {"api_key": "rbw_work", "workspace_id": "alpha", "workspace_name": "Alpha"},
                    "other": {"api_key": "rbw_other"},
                },
            }
        ),
        encoding="utf-8",
    )
    return config_path


class _WorkspacesClient:
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        pass

    def list_my_workspaces(self) -> list[dict[str, Any]]:
        return [
            {"id": "alpha", "name": "Alpha"},
            {"id": "beta", "name": "Beta"},
        ]


def test_workspace_switch_moves_the_selection_not_the_profile(monkeypatch, tmp_path: Path, capsys) -> None:
    """The profile is the identity; only the workspace selection moves.

    Switching workspace used to switch profile, which is why belonging to four
    workspaces meant keeping four profiles.
    """
    config_path = _config_with_profiles(tmp_path, monkeypatch)
    monkeypatch.setattr(cli, "Client", _WorkspacesClient)

    assert main(["workspace", "switch", "beta"]) == 0

    data = json.loads(config_path.read_text(encoding="utf-8"))
    assert data["default_profile"] == "work", "the active profile must not change"
    assert data["profiles"]["work"]["workspace_id"] == "beta"
    assert data["profiles"]["work"]["workspace_name"] == "Beta"
    assert data["profiles"]["work"]["api_key"] == "rbw_work", "credentials must survive"
    assert "Beta" in capsys.readouterr().out


def test_workspace_switch_accepts_an_id_as_well_as_a_name(monkeypatch, tmp_path: Path) -> None:
    config_path = _config_with_profiles(tmp_path, monkeypatch)
    monkeypatch.setattr(cli, "Client", _WorkspacesClient)

    assert main(["workspace", "switch", "beta"]) == 0
    assert json.loads(config_path.read_text(encoding="utf-8"))["profiles"]["work"]["workspace_id"] == "beta"


def test_workspace_switch_refuses_a_workspace_you_are_not_in(monkeypatch, tmp_path: Path) -> None:
    """Better to name what this profile can reach than to write a selection the
    server will reject on every later call."""
    _config_with_profiles(tmp_path, monkeypatch)
    monkeypatch.setattr(cli, "Client", _WorkspacesClient)

    with pytest.raises(RebaseWorkflowError, match="not a member of workspace 'gamma'"):
        cli._switch_workspace("gamma")


def test_workspace_use_is_an_alias_for_switch(monkeypatch, tmp_path: Path) -> None:
    config_path = _config_with_profiles(tmp_path, monkeypatch)
    monkeypatch.setattr(cli, "Client", _WorkspacesClient)

    assert main(["workspace", "use", "beta"]) == 0

    data = json.loads(config_path.read_text(encoding="utf-8"))
    assert data["default_profile"] == "work"
    assert data["profiles"]["work"]["workspace_id"] == "beta"


def test_profile_switch_still_changes_the_active_profile(monkeypatch, tmp_path: Path) -> None:
    config_path = _config_with_profiles(tmp_path, monkeypatch)

    assert main(["profile", "switch", "other"]) == 0
    assert json.loads(config_path.read_text(encoding="utf-8"))["default_profile"] == "other"


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


def test_connect_github_command_uses_active_workspace_profile_by_default(monkeypatch) -> None:
    observed: dict[str, Any] = {}

    def fake_run_connect_github(args: Any) -> int:
        observed.update(vars(args))
        return 0

    monkeypatch.setattr("rebase.setup.run_connect_github", fake_run_connect_github)

    assert main(["connect", "github"]) == 0

    assert observed["profile"] is None


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


def test_workspace_invite_email_and_github_together(monkeypatch, capsys) -> None:
    observed: dict[str, Any] = {}

    def fake_create_workspace_invite(self: Client, **kwargs: Any) -> dict[str, Any]:
        observed.update(kwargs)
        return {
            "email": kwargs["email"],
            "github_username": kwargs["github_username"],
            "role": kwargs["role"],
            "status": "pending",
        }

    monkeypatch.setattr(Client, "create_workspace_invite", fake_create_workspace_invite)

    assert main(["workspace", "invite", "--email", "davide@rebase.energy", "--github", "davide-github"]) == 0

    assert observed == {
        "email": "davide@rebase.energy",
        "github_username": "davide-github",
        "role": "Viewer",
    }
    output = capsys.readouterr().out
    assert "davide@rebase.energy" in output
    assert "@davide-github" in output


def test_workspace_invite_target_combines_with_other_identity(monkeypatch, capsys) -> None:
    observed: dict[str, Any] = {}

    def fake_create_workspace_invite(self: Client, **kwargs: Any) -> dict[str, Any]:
        observed.update(kwargs)
        return {
            "email": kwargs["email"],
            "github_username": kwargs["github_username"],
            "role": kwargs["role"],
            "status": "pending",
        }

    monkeypatch.setattr(Client, "create_workspace_invite", fake_create_workspace_invite)

    assert main(["workspace", "invite", "davide-github", "--email", "davide@rebase.energy"]) == 0

    assert observed["email"] == "davide@rebase.energy"
    assert observed["github_username"] == "davide-github"
    capsys.readouterr()


def test_workspace_invite_requires_a_target(capsys) -> None:
    assert main(["workspace", "invite"]) == 1

    assert "provide an invite target" in capsys.readouterr().err


def test_workspace_invite_rejects_duplicate_identity(capsys) -> None:
    assert main(["workspace", "invite", "davide@rebase.energy", "--email", "other@rebase.energy"]) == 1

    assert "email was given twice" in capsys.readouterr().err


def _fake_invites(*invites: dict[str, Any]):
    def fake_list_workspace_invites(self: Client) -> list[dict[str, Any]]:
        return list(invites)

    return fake_list_workspace_invites


def test_workspace_uninvite_revokes_pending_invite_by_email(monkeypatch, capsys) -> None:
    revoked: list[str] = []

    def fake_revoke_workspace_invite(self: Client, invite_id: str) -> dict[str, Any]:
        revoked.append(invite_id)
        return {"id": invite_id, "email": "davide@rebase.energy", "status": "revoked"}

    monkeypatch.setattr(
        Client,
        "list_workspace_invites",
        _fake_invites(
            {"id": "invite-1", "email": "davide@rebase.energy", "github_username": None, "status": "pending"},
            {"id": "invite-2", "email": "other@rebase.energy", "github_username": None, "status": "accepted"},
        ),
    )
    monkeypatch.setattr(Client, "revoke_workspace_invite", fake_revoke_workspace_invite)

    assert main(["workspace", "uninvite", "davide@rebase.energy"]) == 0

    assert revoked == ["invite-1"]
    assert "davide@rebase.energy" in capsys.readouterr().out


def test_workspace_uninvite_matches_github_username(monkeypatch, capsys) -> None:
    revoked: list[str] = []

    def fake_revoke_workspace_invite(self: Client, invite_id: str) -> dict[str, Any]:
        revoked.append(invite_id)
        return {"id": invite_id, "email": None, "github_username": "davide-github", "status": "revoked"}

    monkeypatch.setattr(
        Client,
        "list_workspace_invites",
        _fake_invites({"id": "invite-1", "email": None, "github_username": "davide-github", "status": "pending"}),
    )
    monkeypatch.setattr(Client, "revoke_workspace_invite", fake_revoke_workspace_invite)

    assert main(["workspace", "uninvite", "@davide-github"]) == 0

    assert revoked == ["invite-1"]
    assert "@davide-github" in capsys.readouterr().out


def test_workspace_uninvite_ignores_non_pending_invites(monkeypatch, capsys) -> None:
    monkeypatch.setattr(
        Client,
        "list_workspace_invites",
        _fake_invites(
            {"id": "invite-1", "email": "davide@rebase.energy", "github_username": None, "status": "accepted"}
        ),
    )

    assert main(["workspace", "uninvite", "davide@rebase.energy"]) == 1

    assert "no pending invite matches" in capsys.readouterr().err


def _member(email: str, *, role: str, github_username: str | None = None) -> dict[str, Any]:
    return {
        "profile_id": f"profile-{email.split('@')[0]}",
        "email": email,
        "github_username": github_username,
        "role": role,
        "enabled": True,
        "created_at": "2026-06-22T22:00:00Z",
    }


def _fake_members(*members: dict[str, Any]):
    def fake_list_workspace_members(self: Client) -> list[dict[str, Any]]:
        return list(members)

    return fake_list_workspace_members


def test_workspace_set_role_promotes_member_by_email(monkeypatch, capsys) -> None:
    observed: dict[str, Any] = {}

    def fake_update_workspace_member(self: Client, profile_id: str, **kwargs: Any) -> dict[str, Any]:
        observed.update({"profile_id": profile_id, **kwargs})
        return {"profile_id": profile_id, "email": "sebastian@rebase.energy", "role": kwargs["role"]}

    monkeypatch.setattr(
        Client,
        "list_workspace_members",
        _fake_members(_member("sebastian@rebase.energy", role="Viewer")),
    )
    monkeypatch.setattr(Client, "update_workspace_member", fake_update_workspace_member)

    assert main(["workspace", "set-role", "sebastian@rebase.energy", "--role", "Owner"]) == 0

    assert observed == {"profile_id": "profile-sebastian", "role": "Owner"}
    output = capsys.readouterr().out
    assert "sebastian@rebase.energy" in output
    assert "Viewer" in output
    assert "Owner" in output


def test_workspace_set_role_resolves_github_username(monkeypatch) -> None:
    observed: dict[str, Any] = {}

    def fake_update_workspace_member(self: Client, profile_id: str, **kwargs: Any) -> dict[str, Any]:
        observed.update({"profile_id": profile_id, **kwargs})
        return {"profile_id": profile_id, "github_username": "sebaheg", "role": kwargs["role"]}

    monkeypatch.setattr(
        Client,
        "list_workspace_members",
        _fake_members(_member("sebastian@rebase.energy", role="Viewer", github_username="sebaheg")),
    )
    monkeypatch.setattr(Client, "update_workspace_member", fake_update_workspace_member)

    assert main(["workspace", "set-role", "@sebaheg", "--role", "Admin"]) == 0

    assert observed == {"profile_id": "profile-sebastian", "role": "Admin"}


def test_workspace_set_role_rejects_unknown_role(capsys) -> None:
    assert main(["workspace", "set-role", "sebastian@rebase.energy", "--role", "owner"]) == 1

    assert "role must be one of: Viewer, Developer, Admin, Owner" in capsys.readouterr().err


def test_workspace_set_role_reports_unknown_member(monkeypatch, capsys) -> None:
    monkeypatch.setattr(
        Client,
        "list_workspace_members",
        _fake_members(_member("davide@rebase.energy", role="Developer")),
    )

    assert main(["workspace", "set-role", "mihai@rebase.energy", "--role", "Owner"]) == 1

    error = capsys.readouterr().err
    assert "no workspace member matches 'mihai@rebase.energy'" in error
    assert "davide@rebase.energy" in error


def test_workspace_set_role_surfaces_server_permission_error(monkeypatch, capsys) -> None:
    def fake_update_workspace_member(self: Client, profile_id: str, **kwargs: Any) -> dict[str, Any]:
        raise RebaseWorkflowError("only Owner can assign Owner role")

    monkeypatch.setattr(
        Client,
        "list_workspace_members",
        _fake_members(_member("sebastian@rebase.energy", role="Viewer")),
    )
    monkeypatch.setattr(Client, "update_workspace_member", fake_update_workspace_member)

    assert main(["workspace", "set-role", "sebastian@rebase.energy", "--role", "Owner"]) == 1

    assert "only Owner can assign Owner role" in capsys.readouterr().err


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


def _project_open_setup(monkeypatch, tmp_path: Path, *, declaring_files: int = 1) -> list[list[str]]:
    """Seed a workspace whose search path holds `declaring_files` copies of a project.

    Returns the list that launched editor argv lands in.
    """
    config_path = tmp_path / "config.json"
    monkeypatch.setenv("REBASE_CONFIG_PATH", str(config_path))
    monkeypatch.setenv("REBASE_EDITOR", "fake-editor -g {path}:{line}")
    config_path.write_text(
        json.dumps(
            {
                "default_profile": "default",
                "profiles": {"default": {"api_key": "rbw_test", "workspace_id": "ws"}},
                "workspaces": {"ws": {"search_paths": [str(tmp_path / "code")]}},
            }
        ),
        encoding="utf-8",
    )
    code = tmp_path / "code"
    code.mkdir()
    for index in range(declaring_files):
        (code / f"deploy_{index}.py").write_text(
            'import rebase as rb\n\nPROJECT_NAME = "energy"\n\nproject = rb.project(PROJECT_NAME)\n',
            encoding="utf-8",
        )
    monkeypatch.setattr(Client, "list_projects", lambda self: [{"id": "project-id", "name": "energy"}])

    launched: list[list[str]] = []
    monkeypatch.setattr("rebase.cli.spawn_detached", lambda argv: launched.append(list(argv)))
    return launched


def test_cli_project_open_prints_the_resolved_path_without_launching_an_editor(
    monkeypatch, tmp_path: Path, capsys
) -> None:
    launched = _project_open_setup(monkeypatch, tmp_path)

    assert main(["project", "open", "energy", "--path"]) == 0

    assert "deploy_0.py:5" in capsys.readouterr().out
    assert launched == []


def test_cli_project_open_launches_the_resolved_editor(monkeypatch, tmp_path: Path, capsys) -> None:
    launched = _project_open_setup(monkeypatch, tmp_path)

    assert main(["project", "open", "energy"]) == 0

    assert "Opened" in capsys.readouterr().out
    assert launched == [["fake-editor", "-g", f"{tmp_path / 'code' / 'deploy_0.py'}:5"]]


def test_cli_project_open_reports_json_status_and_matches_without_launching(
    monkeypatch, tmp_path: Path, capsys
) -> None:
    launched = _project_open_setup(monkeypatch, tmp_path)

    assert main(["project", "open", "energy", "--json"]) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "found"
    assert payload["matches"][0]["line"] == 5
    assert payload["files_parsed"] == 1
    assert launched == []


def test_cli_project_open_fails_when_two_files_declare_the_project(monkeypatch, tmp_path: Path, capsys) -> None:
    launched = _project_open_setup(monkeypatch, tmp_path, declaring_files=2)

    assert main(["project", "open", "energy"]) == 1

    error = capsys.readouterr().err
    assert "deploy_0.py:5" in error
    assert "deploy_1.py:5" in error
    assert launched == []


def test_cli_project_open_path_prints_every_match_when_ambiguous(monkeypatch, tmp_path: Path, capsys) -> None:
    """--path is the scriptable escape hatch, so ambiguity is information, not an error."""
    _project_open_setup(monkeypatch, tmp_path, declaring_files=2)

    assert main(["project", "open", "energy", "--path"]) == 0

    output = capsys.readouterr().out
    assert "deploy_0.py:5" in output
    assert "deploy_1.py:5" in output


def test_cli_project_open_fails_when_no_search_paths_are_configured(monkeypatch, tmp_path: Path, capsys) -> None:
    _project_open_setup(monkeypatch, tmp_path)
    config_path = tmp_path / "config.json"
    data = json.loads(config_path.read_text(encoding="utf-8"))
    data.pop("workspaces")
    config_path.write_text(json.dumps(data), encoding="utf-8")

    assert main(["project", "open", "energy"]) == 1

    assert "rebase project search-path add" in capsys.readouterr().err


def test_cli_project_open_fails_when_no_file_declares_the_project(monkeypatch, tmp_path: Path, capsys) -> None:
    _project_open_setup(monkeypatch, tmp_path, declaring_files=0)

    assert main(["project", "open", "energy"]) == 1

    error = capsys.readouterr().err
    assert str(tmp_path / "code") in error
    assert "web app" in error


def test_cli_project_open_mentions_computed_project_names_when_nothing_matched(
    monkeypatch, tmp_path: Path, capsys
) -> None:
    _project_open_setup(monkeypatch, tmp_path, declaring_files=0)
    (tmp_path / "code" / "computed.py").write_text(
        'import os\n\nimport rebase as rb\n\nrb.project(os.environ["NAME"])\n', encoding="utf-8"
    )

    assert main(["project", "open", "energy"]) == 1

    assert "built at runtime" in capsys.readouterr().err


def test_cli_project_search_path_add_list_and_remove_round_trip(monkeypatch, tmp_path: Path, capsys) -> None:
    config_path = tmp_path / "config.json"
    monkeypatch.setenv("REBASE_CONFIG_PATH", str(config_path))
    config_path.write_text(
        json.dumps({"default_profile": "default", "profiles": {"default": {"workspace_id": "ws"}}}),
        encoding="utf-8",
    )
    code = tmp_path / "code"
    code.mkdir()

    assert main(["project", "search-path", "add", str(code)]) == 0
    assert "Added search path" in capsys.readouterr().out

    assert main(["project", "search-path", "add", str(code)]) == 0
    assert "already a search path" in capsys.readouterr().out

    assert main(["project", "search-path", "list", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == [{"path": str(code), "exists": True}]

    assert main(["project", "search-path", "remove", str(code)]) == 0
    assert "Removed search path" in capsys.readouterr().out

    assert main(["project", "search-path", "remove", str(code)]) == 1
    assert "not a search path" in capsys.readouterr().err


def test_cli_project_search_path_add_refuses_the_home_directory_without_force(
    monkeypatch, tmp_path: Path, capsys
) -> None:
    monkeypatch.setenv("REBASE_CONFIG_PATH", str(tmp_path / "config.json"))

    assert main(["project", "search-path", "add", str(Path.home())]) == 1

    assert "--force" in capsys.readouterr().err


def test_cli_project_search_path_add_refuses_a_directory_that_does_not_exist(
    monkeypatch, tmp_path: Path, capsys
) -> None:
    monkeypatch.setenv("REBASE_CONFIG_PATH", str(tmp_path / "config.json"))

    assert main(["project", "search-path", "add", str(tmp_path / "gone")]) == 1

    assert "not a directory" in capsys.readouterr().err


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


def test_hillclimb_promote_local(tmp_path):
    from rebase.hillclimb import promote_local

    run = tmp_path / "runs" / "20260706-010203-grid-wind-es"
    best = run / "searches" / "grid-wind-es" / "best"
    best.mkdir(parents=True)
    (best / "solution.py").write_text("def get_model():\n    return None\n")

    written = promote_local("latest", tmp_path / "models", runs_dir=tmp_path / "runs")
    assert [p.name for p in written] == ["grid_wind_es.py"]
    assert (tmp_path / "models" / "grid_wind_es.py").read_text().startswith("def get_model")

    written = promote_local("wind-es", tmp_path / "models", runs_dir=tmp_path / "runs")
    assert len(written) == 1

    with pytest.raises(RuntimeError, match="no local run matching"):
        promote_local("nope", tmp_path / "models", runs_dir=tmp_path / "runs")
    (best / "solution.py").unlink()
    with pytest.raises(RuntimeError, match="no searches with a best"):
        promote_local("latest", tmp_path / "models", runs_dir=tmp_path / "runs")


def _stub_workflow_lookup(monkeypatch) -> None:
    monkeypatch.setattr(
        Client,
        "get_workflow",
        lambda self, workflow_id: {
            "id": workflow_id,
            "name": "forecast",
            "project_id": "project-id",
            "enabled": True,
            "current_version_id": "version-id",
        },
    )


def test_workflow_schedule_show_command(monkeypatch, capsys) -> None:
    _stub_workflow_lookup(monkeypatch)
    monkeypatch.setattr(
        Client,
        "get_workflow_schedule",
        lambda self, workflow_id: {
            "workflow_id": workflow_id,
            "version_id": "version-id",
            "schedule": {"type": "cron", "cron": "0 * * * *", "timezone": "Europe/Stockholm"},
            "active": True,
            "next_run_at": "2026-07-11T11:00:00Z",
        },
    )

    assert main(["workflow", "schedule", "show", "--id", "workflow-id"]) == 0

    output = capsys.readouterr().out
    assert "Workflow Schedule" in output
    assert "0 * * * *" in output
    assert "Europe/Stockholm" in output
    assert "2026-07-11T11:00:00Z" in output


def test_workflow_schedule_show_without_schedule(monkeypatch, capsys) -> None:
    _stub_workflow_lookup(monkeypatch)
    monkeypatch.setattr(
        Client,
        "get_workflow_schedule",
        lambda self, workflow_id: {
            "workflow_id": workflow_id,
            "version_id": "version-id",
            "schedule": None,
            "active": None,
            "next_run_at": None,
        },
    )

    assert main(["workflow", "schedule", "show", "--id", "workflow-id"]) == 0
    assert "has no schedule" in capsys.readouterr().out


def test_workflow_schedule_set_command(monkeypatch, capsys) -> None:
    _stub_workflow_lookup(monkeypatch)
    observed: dict[str, Any] = {}

    def fake_update_workflow(self, workflow_id, **kwargs):
        observed["workflow_id"] = workflow_id
        observed["schedule"] = kwargs.get("schedule")
        return {"id": workflow_id}

    monkeypatch.setattr(Client, "update_workflow", fake_update_workflow)
    monkeypatch.setattr(
        Client,
        "get_workflow_schedule",
        lambda self, workflow_id: {
            "workflow_id": workflow_id,
            "version_id": "version-id",
            "schedule": {"type": "cron", "cron": "0 15 * * *", "timezone": "Europe/London"},
            "active": True,
            "next_run_at": "2026-07-11T14:00:00Z",
        },
    )

    assert (
        main(
            [
                "workflow",
                "schedule",
                "set",
                "--id",
                "workflow-id",
                "--cron",
                "0 15 * * *",
                "--timezone",
                "Europe/London",
            ]
        )
        == 0
    )

    assert observed["schedule"] == {
        "type": "cron",
        "cron": "0 15 * * *",
        "timezone": "Europe/London",
        "day_or": True,
        "active": True,
    }
    assert "Schedule Set" in capsys.readouterr().out


def test_workflow_schedule_set_requires_cron(monkeypatch, capsys) -> None:
    assert main(["workflow", "schedule", "set", "--id", "workflow-id"]) == 1
    assert "--cron is required" in capsys.readouterr().err


def test_workflow_schedule_set_rejects_invalid_cron(monkeypatch, capsys) -> None:
    _stub_workflow_lookup(monkeypatch)
    assert main(["workflow", "schedule", "set", "--id", "workflow-id", "--cron", "not-a-cron"]) == 1
    assert "cron" in capsys.readouterr().err


def test_workflow_schedule_pause_and_resume_use_the_pause_endpoints(monkeypatch, capsys) -> None:
    _stub_workflow_lookup(monkeypatch)
    calls: list[tuple[str, Any]] = []

    def fake_pause_workflow(self, workflow_id, *, until=None):
        calls.append(("pause", until))
        return {"id": workflow_id}

    def fake_resume_workflow(self, workflow_id):
        calls.append(("resume", None))
        return {"id": workflow_id}

    monkeypatch.setattr(Client, "pause_workflow", fake_pause_workflow)
    monkeypatch.setattr(Client, "resume_workflow", fake_resume_workflow)
    monkeypatch.setattr(
        Client,
        "get_workflow_schedule",
        lambda self, workflow_id: {
            "workflow_id": workflow_id,
            "version_id": "version-id",
            "schedule": {"type": "cron", "cron": "0 * * * *", "active": True},
            "active": True,
            "paused": False,
            "paused_until": None,
            "next_run_at": None,
        },
    )

    assert main(["workflow", "schedule", "pause", "--id", "workflow-id"]) == 0
    assert calls[-1] == ("pause", None)
    assert "Schedule Paused" in capsys.readouterr().out

    assert main(["workflow", "schedule", "pause", "--id", "workflow-id", "--until", "2099-09-15T10:00:00+00:00"]) == 0
    assert calls[-1] == ("pause", "2099-09-15T10:00:00+00:00")
    capsys.readouterr()

    assert main(["workflow", "schedule", "resume", "--id", "workflow-id"]) == 0
    assert calls[-1] == ("resume", None)
    assert "Schedule Resumed" in capsys.readouterr().out


def test_workflow_pause_rejects_until_and_for_together(monkeypatch, capsys) -> None:
    assert main(["workflow", "pause", "--id", "workflow-id", "--until", "2099-09-15", "--for", "2w"]) == 1
    assert "not both" in capsys.readouterr().err


def test_workflow_pause_rejects_an_expiry_in_the_past(monkeypatch, capsys) -> None:
    assert main(["workflow", "pause", "--id", "workflow-id", "--until", "2020-01-01"]) == 1
    assert "in the past" in capsys.readouterr().err


def test_workflow_resume_reactivates_a_legacy_inactive_schedule(monkeypatch, capsys) -> None:
    """Schedules paused by older CLIs carry active=false in the schedule JSON itself."""
    _stub_workflow_lookup(monkeypatch)
    observed: dict[str, Any] = {}

    def fake_update_workflow(self, workflow_id, **kwargs):
        observed["schedule"] = kwargs.get("schedule")
        return {"id": workflow_id}

    monkeypatch.setattr(Client, "resume_workflow", lambda self, workflow_id: {"id": workflow_id})
    monkeypatch.setattr(Client, "update_workflow", fake_update_workflow)
    monkeypatch.setattr(
        Client,
        "get_workflow_schedule",
        lambda self, workflow_id: {
            "workflow_id": workflow_id,
            "version_id": "version-id",
            "schedule": {"type": "cron", "cron": "0 * * * *", "active": False},
            "active": False,
            "next_run_at": None,
        },
    )

    assert main(["workflow", "resume", "--id", "workflow-id"]) == 0
    assert observed["schedule"]["active"] is True
    assert "Schedule Resumed" in capsys.readouterr().out


def test_workflow_schedule_pause_without_schedule_errors(monkeypatch, capsys) -> None:
    _stub_workflow_lookup(monkeypatch)
    monkeypatch.setattr(
        Client,
        "get_workflow_schedule",
        lambda self, workflow_id: {"workflow_id": workflow_id, "schedule": None},
    )

    assert main(["workflow", "schedule", "pause", "--id", "workflow-id"]) == 1
    assert "has no schedule" in capsys.readouterr().err


def test_workflow_schedule_clear_command(monkeypatch, capsys) -> None:
    _stub_workflow_lookup(monkeypatch)
    observed: dict[str, Any] = {"called": False}

    def fake_update_workflow(self, workflow_id, **kwargs):
        observed["called"] = True
        observed["schedule"] = kwargs.get("schedule", "MISSING")
        return {"id": workflow_id}

    monkeypatch.setattr(Client, "update_workflow", fake_update_workflow)
    monkeypatch.setattr(
        Client,
        "get_workflow_schedule",
        lambda self, workflow_id: {
            "workflow_id": workflow_id,
            "schedule": {"type": "cron", "cron": "0 * * * *"},
        },
    )

    assert main(["workflow", "schedule", "clear", "--id", "workflow-id"]) == 0
    assert observed["called"] is True
    assert observed["schedule"] is None
    assert "Schedule removed" in capsys.readouterr().out


def test_workflow_schedule_trigger_command(monkeypatch, capsys) -> None:
    _stub_workflow_lookup(monkeypatch)
    observed: dict[str, Any] = {}

    def fake_run_workflow(self, workflow_id, parameters=None):
        observed["workflow_id"] = workflow_id
        observed["parameters"] = parameters
        return Run(
            "run-id",
            client=self,
            data={"id": "run-id", "status": "queued", "target_type": "workflow"},
        )

    monkeypatch.setattr(Client, "run_workflow", fake_run_workflow)

    assert main(["workflow", "schedule", "trigger", "--id", "workflow-id", "-p", 'site_id="site-1"']) == 0

    assert observed["workflow_id"] == "workflow-id"
    assert observed["parameters"] == {"site_id": "site-1"}
    output = capsys.readouterr().out
    assert "Run Submitted" in output
    assert "run-id" in output


def test_workflow_schedule_list_command(monkeypatch, capsys) -> None:
    monkeypatch.setattr(
        Client,
        "list_workflows",
        lambda self, *, project=None, project_id=None: [
            {
                "id": "workflow-1",
                "name": "forecast",
                "schedule": {"type": "cron", "cron": "0 * * * *", "timezone": "UTC", "active": True},
                "next_run_at": "2026-07-11T11:00:00Z",
            },
            {"id": "workflow-2", "name": "unscheduled", "schedule": None},
        ],
    )

    assert main(["workflow", "schedule", "list"]) == 0

    output = capsys.readouterr().out
    assert "Workflow Schedules" in output
    assert "forecast" in output
    assert "unscheduled" not in output


def _stub_workflow_trigger(monkeypatch, trigger: dict[str, Any] | None) -> None:
    monkeypatch.setattr(
        Client,
        "get_workflow_trigger",
        lambda self, workflow_id: {
            "workflow_id": workflow_id,
            "version_id": "version-id",
            "trigger": trigger,
            "active": (trigger or {}).get("active"),
            "last_fired_at": "2026-07-11T08:00:00Z" if trigger else None,
            "next_deadline_at": "2026-07-11T09:00:00Z" if trigger else None,
            "state": [
                {
                    "source_type": "dataset",
                    "source": "nordpool/prices",
                    "on_status": None,
                    "pending": True,
                    "pending_since": "2026-07-11T07:30:00Z",
                    "last_event_at": "2026-07-11T07:30:00Z",
                    "last_consumed_watermark": "w-41",
                    "last_consumed_at": "2026-07-10T09:00:00Z",
                }
            ]
            if trigger
            else [],
        },
    )


def test_workflow_trigger_show_command(monkeypatch, capsys) -> None:
    _stub_workflow_lookup(monkeypatch)
    _stub_workflow_trigger(
        monkeypatch,
        {"type": "on_update", "datasets": ["nordpool/prices"], "require": "all", "active": True},
    )

    assert main(["workflow", "trigger", "show", "--id", "workflow-id"]) == 0

    output = capsys.readouterr().out
    assert "Workflow Trigger" in output
    assert "on_update" in output
    assert "nordpool/prices" in output
    assert "2026-07-11T09:00:00Z" in output
    assert "Trigger State" in output


def test_workflow_trigger_show_without_trigger(monkeypatch, capsys) -> None:
    _stub_workflow_lookup(monkeypatch)
    _stub_workflow_trigger(monkeypatch, None)

    assert main(["workflow", "trigger", "show", "--id", "workflow-id"]) == 0
    assert "has no trigger" in capsys.readouterr().out


def test_workflow_trigger_set_on_workflow(monkeypatch, capsys) -> None:
    _stub_workflow_lookup(monkeypatch)
    observed: dict[str, Any] = {}

    def fake_update_workflow(self, workflow_id, **kwargs):
        observed["workflow_id"] = workflow_id
        observed["trigger"] = kwargs.get("trigger")
        return {"id": workflow_id}

    monkeypatch.setattr(Client, "update_workflow", fake_update_workflow)
    _stub_workflow_trigger(
        monkeypatch,
        {"type": "on_workflow", "source": "energy/ingest-prices", "on": "failure", "active": True},
    )

    assert (
        main(
            [
                "workflow",
                "trigger",
                "set",
                "--id",
                "workflow-id",
                "--on-workflow",
                "energy/ingest-prices",
                "--on",
                "failure",
            ]
        )
        == 0
    )

    assert observed["trigger"] == {
        "type": "on_workflow",
        "source": "energy/ingest-prices",
        "on": "failure",
        "active": True,
    }
    assert "Trigger Set" in capsys.readouterr().out


def test_workflow_trigger_set_on_update(monkeypatch, capsys) -> None:
    _stub_workflow_lookup(monkeypatch)
    observed: dict[str, Any] = {}

    def fake_update_workflow(self, workflow_id, **kwargs):
        observed["trigger"] = kwargs.get("trigger")
        return {"id": workflow_id}

    monkeypatch.setattr(Client, "update_workflow", fake_update_workflow)
    _stub_workflow_trigger(
        monkeypatch,
        {"type": "on_update", "datasets": ["nordpool/prices", "weather/ecmwf"], "require": "any", "active": True},
    )

    assert (
        main(
            [
                "workflow",
                "trigger",
                "set",
                "--id",
                "workflow-id",
                "--on-update",
                "nordpool/prices,weather/ecmwf",
                "--require",
                "any",
                "--at-most-every",
                "15m",
                "--deadline-cron",
                "0 9 * * *",
                "--deadline-timezone",
                "Europe/Stockholm",
            ]
        )
        == 0
    )

    assert observed["trigger"] == {
        "type": "on_update",
        "datasets": ["nordpool/prices", "weather/ecmwf"],
        "require": "any",
        "at_most_every": "15m",
        "deadline": {
            "type": "cron",
            "cron": "0 9 * * *",
            "timezone": "Europe/Stockholm",
            "day_or": True,
            "active": True,
        },
        "active": True,
    }
    assert "Trigger Set" in capsys.readouterr().out


def test_workflow_trigger_set_requires_exactly_one_mode(monkeypatch, capsys) -> None:
    assert main(["workflow", "trigger", "set", "--id", "workflow-id"]) == 1
    assert "exactly one of" in capsys.readouterr().err

    assert (
        main(
            [
                "workflow",
                "trigger",
                "set",
                "--id",
                "workflow-id",
                "--on-workflow",
                "energy/ingest-prices",
                "--on-update",
                "nordpool/prices",
            ]
        )
        == 1
    )
    assert "exactly one of" in capsys.readouterr().err


def test_workflow_trigger_clear_command(monkeypatch, capsys) -> None:
    _stub_workflow_lookup(monkeypatch)
    observed: dict[str, Any] = {"called": False}

    def fake_update_workflow(self, workflow_id, **kwargs):
        observed["called"] = True
        observed["trigger"] = kwargs.get("trigger", "MISSING")
        return {"id": workflow_id}

    monkeypatch.setattr(Client, "update_workflow", fake_update_workflow)
    _stub_workflow_trigger(monkeypatch, {"type": "on_workflow", "source": "energy/ingest-prices", "active": True})

    assert main(["workflow", "trigger", "clear", "--id", "workflow-id"]) == 0
    assert observed["called"] is True
    assert observed["trigger"] is None
    assert "Trigger removed" in capsys.readouterr().out


def test_workflow_trigger_pause_and_resume_toggle_active(monkeypatch, capsys) -> None:
    _stub_workflow_lookup(monkeypatch)
    triggers: list[dict[str, Any]] = []

    def fake_update_workflow(self, workflow_id, **kwargs):
        triggers.append(dict(kwargs["trigger"]))
        return {"id": workflow_id}

    monkeypatch.setattr(Client, "update_workflow", fake_update_workflow)
    _stub_workflow_trigger(
        monkeypatch,
        {"type": "on_update", "datasets": ["nordpool/prices"], "require": "all", "active": True},
    )

    assert main(["workflow", "trigger", "pause", "--id", "workflow-id"]) == 0
    assert triggers[-1]["active"] is False
    assert "Trigger Paused" in capsys.readouterr().out

    assert main(["workflow", "trigger", "resume", "--id", "workflow-id"]) == 0
    assert triggers[-1]["active"] is True
    assert "Trigger Resumed" in capsys.readouterr().out


def test_workflow_trigger_pause_without_trigger_errors(monkeypatch, capsys) -> None:
    _stub_workflow_lookup(monkeypatch)
    _stub_workflow_trigger(monkeypatch, None)

    assert main(["workflow", "trigger", "pause", "--id", "workflow-id"]) == 1
    assert "has no trigger" in capsys.readouterr().err


def test_workflow_trigger_list_command(monkeypatch, capsys) -> None:
    monkeypatch.setattr(
        Client,
        "list_workflows",
        lambda self, *, project=None, project_id=None: [
            {
                "id": "workflow-1",
                "name": "forecast",
                "trigger": {"type": "on_update", "datasets": ["nordpool/prices"], "active": True},
            },
            {"id": "workflow-2", "name": "untriggered", "trigger": None},
        ],
    )

    assert main(["workflow", "trigger", "list"]) == 0

    output = capsys.readouterr().out
    assert "Workflow Triggers" in output
    assert "forecast" in output
    assert "untriggered" not in output


def test_dataset_create_and_get_commands(monkeypatch, capsys) -> None:
    monkeypatch.setattr(
        Client,
        "create_dataset",
        lambda self, name, description=None: {
            "id": "dataset-id",
            "name": name,
            "description": description,
            "watermark": None,
        },
    )

    assert main(["dataset", "create", "nordpool/prices", "--description", "Day-ahead prices"]) == 0
    output = capsys.readouterr().out
    assert "Dataset" in output
    assert "nordpool/prices" in output
    assert "Day-ahead prices" in output

    monkeypatch.setattr(
        Client,
        "get_dataset",
        lambda self, name: {"id": "dataset-id", "name": name, "watermark": "w-42"},
    )

    assert main(["dataset", "get", "nordpool/prices"]) == 0
    output = capsys.readouterr().out
    assert "nordpool/prices" in output
    assert "w-42" in output


def test_dataset_list_command(monkeypatch, capsys) -> None:
    monkeypatch.setattr(
        Client,
        "list_datasets",
        lambda self: [
            {
                "name": "nordpool/prices",
                "watermark": "w-42",
                "last_updated_at": "2026-07-11T09:00:00Z",
                "created_at": "2026-07-01T00:00:00Z",
            }
        ],
    )

    assert main(["dataset", "list"]) == 0

    output = capsys.readouterr().out
    assert "Datasets" in output
    assert "nordpool/prices" in output


def test_dataset_signal_command_parses_watermark(monkeypatch, capsys) -> None:
    observed: dict[str, Any] = {}

    def fake_signal_dataset(self, name, *, watermark=None, source="sdk", run_id=None):
        observed["name"] = name
        observed["watermark"] = watermark
        observed["source"] = source
        return {"dataset": name, "fired": ["run-1"]}

    monkeypatch.setattr(Client, "signal_dataset", fake_signal_dataset)

    assert main(["dataset", "signal", "nordpool/prices", "--watermark", '{"as_of": "2026-07-11"}']) == 0
    assert observed == {"name": "nordpool/prices", "watermark": {"as_of": "2026-07-11"}, "source": "cli"}
    output = capsys.readouterr().out
    assert "Signaled dataset nordpool/prices" in output
    assert "Fired 1 run(s)" in output

    assert main(["dataset", "signal", "nordpool/prices", "--watermark", "w-42"]) == 0
    assert observed["watermark"] == "w-42"


def test_dataset_delete_command(monkeypatch, capsys) -> None:
    monkeypatch.setattr(Client, "delete_dataset", lambda self, name: {"deleted": name})

    assert main(["dataset", "delete", "nordpool/prices", "--yes"]) == 0
    assert "Deleted dataset nordpool/prices" in capsys.readouterr().out


def test_dataset_listeners_command(monkeypatch, capsys) -> None:
    monkeypatch.setattr(Client, "list_dataset_listeners", lambda self, name: ["energy/forecast"])

    assert main(["dataset", "listeners", "nordpool/prices"]) == 0
    assert "energy/forecast" in capsys.readouterr().out

    monkeypatch.setattr(Client, "list_dataset_listeners", lambda self, name: [])

    assert main(["dataset", "listeners", "nordpool/prices"]) == 0
    assert "No workflows listen" in capsys.readouterr().out


def test_run_local_executes_function_in_process(monkeypatch, tmp_path: Path, capsys) -> None:
    function_file = tmp_path / "functions.py"
    function_file.write_text(
        """
import rebase as rb

@rb.function(project="math", name="add")
def add(a: int = 0, b: int = 0) -> dict:
    return {"sum": a + b}
""",
        encoding="utf-8",
    )

    def fail_run_ephemeral(self, **kwargs):
        raise AssertionError("--local must not submit a cloud run")

    monkeypatch.setattr(Client, "run_ephemeral", fail_run_ephemeral)

    assert main(["run", str(function_file), "--local", "-p", "a=2", "-p", "b=3"]) == 0

    output = capsys.readouterr().out
    assert '"sum": 5' in output


def test_run_local_executes_workflow_steps_in_process(monkeypatch, tmp_path: Path, capsys) -> None:
    workflow_file = tmp_path / "workflow.py"
    workflow_file.write_text(
        """
import rebase as rb

project = rb.project("hello")

@project.step()
def load_name(name: str = "World") -> dict:
    return {"name": name}

@project.workflow()
def hello_workflow(name: str = "World") -> dict:
    payload = load_name(name)
    return {"message": f"Hello, {payload['name']}!"}
""",
        encoding="utf-8",
    )

    assert main(["run", str(workflow_file), "--local", "-p", 'name="Rebase"']) == 0

    output = capsys.readouterr().out
    assert "Hello, Rebase!" in output


def test_run_local_scalar_results_are_wrapped(tmp_path: Path, capsys) -> None:
    function_file = tmp_path / "scalar.py"
    function_file.write_text(
        """
import rebase as rb

@rb.function(project="math", name="answer")
def answer() -> int:
    return 42
""",
        encoding="utf-8",
    )

    assert main(["run", str(function_file), "--local"]) == 0
    assert '"value": 42' in capsys.readouterr().out


def test_run_local_rejects_conflicting_flags(tmp_path: Path, capsys) -> None:
    target = tmp_path / "f.py"
    target.write_text("", encoding="utf-8")

    assert main(["run", str(target), "--local", "--run-type", "long"]) == 1
    assert "execution options select cloud execution" in capsys.readouterr().err

    assert main(["run", str(target), "--local", "--no-wait"]) == 1
    assert "--local always runs synchronously" in capsys.readouterr().err


def test_run_local_rejects_models(tmp_path: Path, capsys) -> None:
    model_file = tmp_path / "model.py"
    model_file.write_text(
        """
import rebase as rb

class PricePredictor(rb.Predictor):
    name = "price"
    project = "energy"

    def predict(self, zone: str = "SE3") -> dict:
        return {"zone": zone}

model = PricePredictor()
""",
        encoding="utf-8",
    )

    assert main(["run", str(model_file), "--local"]) == 1
    assert "Models cannot run with --local" in capsys.readouterr().err


def test_workspace_notifications_show_command(monkeypatch, capsys) -> None:
    monkeypatch.setattr(
        Client,
        "get_workspace_notifications",
        lambda self: {
            "workspace_id": "default",
            "notify_on_failure": True,
            "webhook_url": "https://hooks.example.com/rebase",
            "has_webhook_secret": True,
            "updated_at": "2026-07-11T10:00:00Z",
        },
    )

    assert main(["workspace", "notifications", "show"]) == 0

    output = capsys.readouterr().out
    assert "Notification Settings" in output
    assert "hooks.example.com" in output


def test_workspace_notifications_set_command(monkeypatch, capsys) -> None:
    observed: dict[str, Any] = {}

    def fake_update(self, **kwargs):
        observed.update(kwargs)
        return {
            "workspace_id": "default",
            "notify_on_failure": True,
            "webhook_url": kwargs.get("webhook_url"),
            "has_webhook_secret": True,
            "updated_at": "2026-07-11T10:00:00Z",
        }

    monkeypatch.setattr(Client, "update_workspace_notifications", fake_update)

    assert (
        main(
            [
                "workspace",
                "notifications",
                "set",
                "--webhook-url",
                "https://hooks.example.com/rebase",
                "--webhook-secret",
                "s3cret",
                "--on-failure",
            ]
        )
        == 0
    )

    assert observed == {
        "notify_on_failure": True,
        "webhook_url": "https://hooks.example.com/rebase",
        "webhook_secret": "s3cret",
    }


def test_workspace_notifications_set_requires_an_option(capsys) -> None:
    assert main(["workspace", "notifications", "set"]) == 1
    assert "nothing to update" in capsys.readouterr().err


def test_workspace_notifications_clear_webhook(monkeypatch, capsys) -> None:
    observed: dict[str, Any] = {}

    def fake_update(self, **kwargs):
        observed.update(kwargs)
        return {
            "workspace_id": "default",
            "notify_on_failure": False,
            "webhook_url": None,
            "has_webhook_secret": False,
            "updated_at": "2026-07-11T10:00:00Z",
        }

    monkeypatch.setattr(Client, "update_workspace_notifications", fake_update)

    assert main(["workspace", "notifications", "set", "--clear-webhook"]) == 0
    assert observed == {"webhook_url": None, "webhook_secret": None}


def test_volume_create_and_list_commands(monkeypatch, capsys) -> None:
    monkeypatch.setattr(
        Client,
        "create_volume",
        lambda self, name: {"name": name, "provider": "gcs", "bucket": "rebase-vol-p-w", "prefix": f"{name}/"},
    )
    monkeypatch.setattr(
        Client,
        "list_volumes",
        lambda self: [
            {"name": "model-cache", "provider": "gcs", "bucket": "rebase-vol-p-w", "created_at": "2026-07-11T00:00:00Z"}
        ],
    )

    assert main(["volume", "create", "model-cache"]) == 0
    output = capsys.readouterr().out
    assert "Volume" in output
    assert "model-cache" in output

    assert main(["volume", "list"]) == 0
    output = capsys.readouterr().out
    assert "Volumes" in output
    assert "model-cache" in output


def test_volume_ls_put_download_rm_commands(monkeypatch, tmp_path: Path, capsys) -> None:
    monkeypatch.setattr(
        Client,
        "list_volume_objects",
        lambda self, name, prefix="", limit=None: [
            {"path": "model.pkl", "size": 2048, "updated": "2026-07-11T00:00:00Z"}
        ],
    )
    monkeypatch.setattr(
        Client,
        "create_volume_upload_url",
        lambda self, name, path: {"url": "https://signed.example.com/up", "method": "PUT", "expires_seconds": 3600},
    )
    monkeypatch.setattr(
        Client,
        "create_volume_download_url",
        lambda self, name, path: {"url": "https://signed.example.com/down", "method": "GET", "expires_seconds": 3600},
    )
    deleted: list[str] = []
    monkeypatch.setattr(Client, "delete_volume_object", lambda self, name, path: deleted.append(path))

    class FakeTransferResponse:
        status_code = 200
        text = ""
        content = b"weights"

    monkeypatch.setattr("requests.put", lambda url, data=None, timeout=None: FakeTransferResponse())
    monkeypatch.setattr("requests.get", lambda url, timeout=None: FakeTransferResponse())

    assert main(["volume", "ls", "model-cache"]) == 0
    output = capsys.readouterr().out
    assert "model.pkl" in output
    assert "2.0 KiB" in output

    local = tmp_path / "model.pkl"
    local.write_bytes(b"weights")
    assert main(["volume", "put", "model-cache", str(local), "nested/model.pkl"]) == 0
    assert "Uploaded" in capsys.readouterr().out

    target = tmp_path / "downloaded.pkl"
    assert main(["volume", "download", "model-cache", "model.pkl", str(target)]) == 0
    assert target.read_bytes() == b"weights"
    capsys.readouterr()

    assert main(["volume", "rm", "model-cache", "/model.pkl"]) == 0
    assert deleted == ["model.pkl"]


def test_volume_delete_requires_confirmation(monkeypatch, capsys) -> None:
    calls: list[str] = []
    monkeypatch.setattr(Client, "delete_volume", lambda self, name: calls.append(name))

    assert main(["volume", "delete", "model-cache", "--force"]) == 0
    assert calls == ["model-cache"]
    assert "Deleted volume model-cache" in capsys.readouterr().out


def test_dataset_freshness_set_command(monkeypatch, capsys) -> None:
    observed: dict[str, Any] = {}

    def fake_update_dataset(self, name, **kwargs):
        observed["name"] = name
        observed.update(kwargs)
        return {"name": name, "freshness": kwargs.get("freshness"), "freshness_status": "fresh"}

    monkeypatch.setattr(Client, "update_dataset", fake_update_dataset)

    assert (
        main(
            [
                "dataset",
                "freshness",
                "set",
                "nordpool/prices",
                "--max-age",
                "45m",
                "--check-at",
                "15 9 * * *",
                "--timezone",
                "Europe/Stockholm",
            ]
        )
        == 0
    )

    assert observed["name"] == "nordpool/prices"
    assert observed["freshness"]["max_age"] == "45m"
    assert observed["freshness"]["check_at"]["cron"] == "15 9 * * *"
    assert observed["freshness"]["check_at"]["timezone"] == "Europe/Stockholm"
    assert "Dataset Freshness" in capsys.readouterr().out


def test_dataset_freshness_show_command(monkeypatch, capsys) -> None:
    monkeypatch.setattr(
        Client,
        "get_dataset",
        lambda self, name: {
            "name": name,
            "freshness": {"max_age": "45m"},
            "freshness_status": "stale",
            "stale_since": "2026-07-11T08:00:00Z",
        },
    )

    assert main(["dataset", "freshness", "show", "nordpool/prices"]) == 0
    output = capsys.readouterr().out
    assert "stale" in output
    assert "45m" in output

    monkeypatch.setattr(Client, "get_dataset", lambda self, name: {"name": name, "freshness": None})
    assert main(["dataset", "freshness", "show", "nordpool/prices"]) == 0
    assert "no freshness policy" in capsys.readouterr().out


def test_dataset_freshness_clear_command(monkeypatch, capsys) -> None:
    observed: dict[str, Any] = {}

    def fake_update_dataset(self, name, **kwargs):
        observed["name"] = name
        observed.update(kwargs)
        return {"name": name}

    monkeypatch.setattr(Client, "update_dataset", fake_update_dataset)

    assert main(["dataset", "freshness", "clear", "nordpool/prices"]) == 0
    assert observed == {"name": "nordpool/prices", "freshness": None}
    assert "Cleared freshness policy" in capsys.readouterr().out


_CLI_CONTRACT = {
    "$schema": "rebase/contract-v1",
    "properties": {
        "price": {"type": "number", "minimum": -500, "maximum": 4000, "x-not-null": True},
        "area": {"type": "string", "enum": ["SE1", "SE2"], "x-not-null": True},
        "delivery_start": {"type": "string", "format": "date-time", "x-not-null": True},
    },
    "required": ["price", "area", "delivery_start"],
    "x-rebase": {
        "primary_key": ["delivery_start", "area"],
        "extra": "ignore",
        "on_violation": "fail",
        "require_contract": False,
        "min_rows": 1,
        "watermark_column": "delivery_start",
    },
}


def test_dataset_contract_show_command(monkeypatch, capsys) -> None:
    monkeypatch.setattr(Client, "get_dataset", lambda self, name: {"name": name, "contract": _CLI_CONTRACT})

    assert main(["dataset", "contract", "show", "nordpool/prices"]) == 0
    output = capsys.readouterr().out
    assert "Contract Columns" in output
    assert "price" in output
    assert "timestamp" in output
    assert "Contract Policies" in output

    monkeypatch.setattr(Client, "get_dataset", lambda self, name: {"name": name, "contract": None})
    assert main(["dataset", "contract", "show", "nordpool/prices"]) == 0
    assert "has no contract" in capsys.readouterr().out


def test_dataset_contract_clear_command(monkeypatch, capsys) -> None:
    observed: dict[str, Any] = {}

    def fake_update_dataset(self, name, **kwargs):
        observed["name"] = name
        observed.update(kwargs)
        return {"name": name}

    monkeypatch.setattr(Client, "update_dataset", fake_update_dataset)

    assert main(["dataset", "contract", "clear", "nordpool/prices", "--yes"]) == 0
    assert observed == {"name": "nordpool/prices", "contract": None}
    assert "Cleared contract" in capsys.readouterr().out


def test_dataset_validate_command(monkeypatch, capsys, tmp_path) -> None:
    pytest.importorskip("pandas")
    monkeypatch.setattr(Client, "get_dataset", lambda self, name: {"name": name, "contract": _CLI_CONTRACT})

    good = tmp_path / "good.csv"
    good.write_text("price,area,delivery_start\n10.0,SE1,2026-07-11T09:00:00Z\n")
    bad = tmp_path / "bad.csv"
    bad.write_text("price,area,delivery_start\n9999.0,XX,2026-07-11T09:00:00Z\n")

    assert main(["dataset", "validate", "nordpool/prices", str(good)]) == 0
    assert "passed" in capsys.readouterr().out

    assert main(["dataset", "validate", "nordpool/prices", str(bad)]) == 1
    output = capsys.readouterr().out
    assert "FAILED" in output
    assert "range" in output
    assert "isin" in output


def test_dataset_validate_command_rejects_unknown_extension(monkeypatch, capsys, tmp_path) -> None:
    pytest.importorskip("pandas")
    path = tmp_path / "data.txt"
    path.write_text("hello")

    assert main(["dataset", "validate", "nordpool/prices", str(path)]) == 1
    assert ".parquet or .csv" in capsys.readouterr().err


def test_workspace_notifications_set_on_stale(monkeypatch, capsys) -> None:
    observed: dict[str, Any] = {}

    def fake_update(self, **kwargs):
        observed.update(kwargs)
        return {"workspace_id": "default", "notify_on_failure": True, "notify_on_stale": kwargs.get("notify_on_stale")}

    monkeypatch.setattr(Client, "update_workspace_notifications", fake_update)

    assert main(["workspace", "notifications", "set", "--on-stale"]) == 0
    assert observed == {"notify_on_stale": True}

    observed.clear()
    assert main(["workspace", "notifications", "set", "--no-on-stale"]) == 0
    assert observed == {"notify_on_stale": False}


def test_bucket_rm_recursive_without_key_empties_the_whole_bucket(monkeypatch, capsys) -> None:
    """`bucket delete` tells users to run exactly this when a bucket is not empty."""
    prefixes: list[str] = []

    def fake_list(self, prefix="", *, delimiter=None, limit=1000, page_token=None):
        prefixes.append(prefix)
        return {"objects": [], "prefixes": [], "next_page_token": None}

    monkeypatch.setattr(Client, "list_bucket_objects", lambda self, name, **kw: {"objects": []})
    monkeypatch.setattr(Bucket, "list", fake_list)

    assert main(["bucket", "rm", "forecasts", "--recursive", "--force"]) == 0
    assert prefixes == [""], "no key must mean the whole bucket, not a literal 'None' prefix"
    assert "Deleted 0 objects" in capsys.readouterr().out


def test_bucket_rm_without_key_or_recursive_is_an_error(monkeypatch, capsys) -> None:
    monkeypatch.setattr(Client, "delete_bucket_object", lambda self, name, path: None)

    assert main(["bucket", "rm", "forecasts"]) == 1
    assert "KEY is required" in capsys.readouterr().err


def test_bucket_create_and_ls_commands(monkeypatch, capsys) -> None:
    monkeypatch.setattr(
        Client,
        "create_bucket",
        lambda self, name: {"name": name, "provider": "gcs", "bucket": f"rb-{name}-abc", "uri": f"gs://rb-{name}-abc"},
    )
    assert main(["bucket", "create", "forecasts"]) == 0
    assert "gs://rb-forecasts-abc" in capsys.readouterr().out

    monkeypatch.setattr(
        Client,
        "list_bucket_objects",
        lambda self, name, **kw: {
            "objects": [{"path": "2026/a.parquet", "size": 2048, "updated": None}],
            "prefixes": ["2026/"],
            "next_page_token": None,
        },
    )
    assert main(["bucket", "ls", "forecasts", "--delimiter", "/"]) == 0
    output = capsys.readouterr().out
    assert "2026/" in output
    assert "2.0 KiB" in output


def test_stream_run_result_skips_fetches_when_submit_is_terminal(monkeypatch) -> None:
    """A terminal submit response must not trigger events/steps/refresh fetches —
    the whole point of the inline-result contract is a single POST."""
    import time as time_module

    from rebase.cli import _LineRunProgressReporter, _stream_run_result

    class FakeRun:
        id = "run-1"
        data = {"id": "run-1", "status": "succeeded", "result": {"value": 3}}

        def events(self) -> list[dict[str, Any]]:
            pytest.fail("terminal submit response must not fetch events")

        def steps(self) -> list[dict[str, Any]]:
            pytest.fail("terminal submit response must not fetch steps")

        def refresh(self) -> dict[str, Any]:
            pytest.fail("terminal submit response must not refresh")

    result = _stream_run_result(
        FakeRun(),
        reporter=_LineRunProgressReporter(),
        started_at=time_module.monotonic(),
        timeout=5,
        poll_interval=0,
    )

    assert result == {"value": 3}


def test_stream_run_result_still_polls_non_terminal_submit(monkeypatch) -> None:
    """Old-server contract: a `submitted` body keeps the poll loop (with events)."""
    import time as time_module

    from rebase.cli import _LineRunProgressReporter, _stream_run_result

    class FakeRun:
        id = "run-1"
        data = {"id": "run-1", "status": "submitted"}
        events_calls = 0

        def events(self) -> list[dict[str, Any]]:
            type(self).events_calls += 1
            return []

        def refresh(self) -> dict[str, Any]:
            return {"id": "run-1", "status": "succeeded", "result": {"value": 4}}

    result = _stream_run_result(
        FakeRun(),
        reporter=_LineRunProgressReporter(),
        started_at=time_module.monotonic(),
        timeout=5,
        poll_interval=0,
    )

    assert result == {"value": 4}
    assert FakeRun.events_calls >= 1


# --- rebase admin ------------------------------------------------------------------


def _admin_workspace(workspace_id: str, *, defaulted: bool = False, memory: int = 512) -> dict[str, Any]:
    return {
        "id": workspace_id,
        "name": workspace_id if not defaulted else None,
        "created_at": "2026-09-01T00:00:00Z",
        "members": [] if defaulted else [{"email": "sebastian@rebase.energy", "role": "Owner", "enabled": True}],
        "policy": {
            "currency": "EUR",
            "monthly_credit_cents": 2000,
            "max_concurrent_cloud_run_runs": 6,
            "max_run_timeout_seconds": 300,
            "max_cloud_run_cpu_milli": 1000,
            "max_cloud_run_memory_mib": memory,
        },
        "policy_defaulted": defaulted,
    }


def test_admin_workspaces_prints_every_workspace_and_flags_defaults(monkeypatch, capsys) -> None:
    # Nine columns do not fit an 80-column capture; rich reads COLUMNS on every render.
    monkeypatch.setenv("COLUMNS", "200")
    listing = [_admin_workspace("agent-work", memory=4096), _admin_workspace("fresh", defaulted=True)]
    monkeypatch.setattr(Client, "list_admin_workspaces", lambda self, *, limit=200: listing)

    assert main(["admin", "workspaces"]) == 0

    output = capsys.readouterr().out
    assert "Workspaces" in output
    assert "agent-work" in output and "4 GiB" in output
    assert "fresh" in output and "defaults" in output


def test_admin_workspaces_json_round_trips(monkeypatch, capsys) -> None:
    monkeypatch.setattr(Client, "list_admin_workspaces", lambda self, *, limit=200: [_admin_workspace("acme")])

    assert main(["admin", "workspaces", "--json"]) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload[0]["id"] == "acme"
    assert payload[0]["policy_defaulted"] is False


def test_admin_set_routes_ceilings_and_credit_to_their_own_writes(monkeypatch, capsys) -> None:
    """Capacity and billing are separate routes on the platform; the CLI must not blur them."""
    calls: list[tuple[str, str, dict[str, Any]]] = []

    def fake_policy(self: Client, workspace_id: str, **limits: int) -> dict[str, Any]:
        calls.append(("policy", workspace_id, limits))
        return {"workspace_id": workspace_id, **limits}

    def fake_credit(self: Client, workspace_id: str, *, monthly_credit_cents: int) -> dict[str, Any]:
        calls.append(("credit", workspace_id, {"monthly_credit_cents": monthly_credit_cents}))
        return {
            "workspace_id": workspace_id,
            "monthly_credit_cents": monthly_credit_cents,
            "remaining_cents": 0,
            "compute_blocked": True,
        }

    monkeypatch.setattr(Client, "update_admin_compute_policy", fake_policy)
    monkeypatch.setattr(Client, "update_admin_credit_grant", fake_credit)

    assert main(["admin", "set", "acme", "--max-memory-mib", "4096", "--monthly-credit-cents", "100"]) == 0

    assert calls == [
        ("policy", "acme", {"max_cloud_run_memory_mib": 4096}),
        ("credit", "acme", {"monthly_credit_cents": 100}),
    ]
    output = capsys.readouterr().out
    assert "Compute is now blocked in acme" in output


def test_admin_set_with_no_flags_is_an_error(monkeypatch, capsys) -> None:
    assert main(["admin", "set", "acme"]) == 1
    assert "nothing to update" in capsys.readouterr().err


def test_admin_set_surfaces_the_servers_409_verbatim(monkeypatch, capsys) -> None:
    def refuse(self: Client, workspace_id: str, **limits: int) -> dict[str, Any]:
        raise RebaseWorkflowError("max_cloud_run_memory_mib cannot exceed 32768 MiB, the platform maximum")

    monkeypatch.setattr(Client, "update_admin_compute_policy", refuse)

    assert main(["admin", "set", "acme", "--max-memory-mib", "99999"]) == 1
    assert "cannot exceed 32768 MiB, the platform maximum" in capsys.readouterr().err


def test_bare_admin_opens_the_tui_lazily(monkeypatch) -> None:
    """The TUI import happens inside the command, so `rebase admin` can be tested by
    patching the module it imports from, and no other command pays for textual."""
    import rebase.admin_tui as admin_tui

    opened: list[bool] = []
    monkeypatch.setattr(admin_tui, "run_admin_tui", lambda **kwargs: opened.append(True))

    assert main(["admin"]) == 0
    assert opened == [True]


def test_run_list_command_include_asks_for_the_bodies(monkeypatch, capsys) -> None:
    """The rows are summaries by default; `--include` is how a script gets the bodies."""
    observed: list[dict[str, Any]] = []

    def list_runs(self: Any, **kwargs: Any) -> list[dict[str, Any]]:
        observed.append(kwargs)
        return [{"id": "run-id", "status": "queued", "result_bytes": 12}]

    monkeypatch.setattr(Client, "list_runs", list_runs)

    assert main(["run", "list", "--json"]) == 0
    assert "include" not in observed[-1]

    assert main(["run", "list", "--json", "--include", "result,parameters"]) == 0
    assert observed[-1]["include"] == ["result", "parameters"]

    assert main(["run", "list", "--json", "--include", "results"]) != 0
    assert "--include does not know results" in capsys.readouterr().err
