from __future__ import annotations

import asyncio
import json
import math
import re
import sys
import threading
import types
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, cast
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo

import pytest
from textual._xterm_parser import XTermParser
from textual.coordinate import Coordinate
from textual.events import MouseMove
from textual.geometry import Offset
from textual.message import Message
from textual.selection import Selection
from textual.widgets import (
    DataTable,
    Footer,
    Header,
    HelpPanel,
    Input,
    OptionList,
    Static,
    Tab,
    TabbedContent,
    Tabs,
)
from textual.widgets._footer import FooterKey
from textual.widgets._toast import Toast

from rebase import config as config_module
from rebase import tui as tui_module
from rebase.brand import BRAND_AMBER, BRAND_BRIGHT_GREEN, BRAND_MAIN_GREEN, BRAND_MEDIUM_GRAY
from rebase.client import Client, RebaseWorkflowError
from rebase.editor import EditorCommand
from rebase.tui import (
    AUTO_REFRESH_FAILURE_LIMIT,
    COUNTDOWN_WIDTH,
    HISTORY_HOURS,
    MARK_STYLE,
    NEXT_RUN_COLUMN,
    RUN_SCAN_FLOOR,
    DeleteConfirmScreen,
    DetailDrawer,
    OpenSourceChoiceScreen,
    RebaseClock,
    RebaseTuiApp,
    RebaseTuiData,
    SelectableDataTable,
    TimezoneChoiceScreen,
    artifact_browser_url,
    bucket_console_url,
    collapse_message,
    compact_id,
    deployed_identities,
    detail_payload,
    endpoints_by_target,
    format_bytes,
    format_countdown,
    format_duration,
    format_endpoint,
    format_json_summary,
    format_step_keys,
    format_step_workflows,
    format_timestamp,
    format_workflow_commit,
    format_workflow_source,
    github_workflow_source_url,
    group_ephemeral_runs,
    history_column_label,
    history_from_buckets,
    history_from_runs,
    history_scale,
    history_text,
    is_github_backed,
    shaded,
    status_style,
    step_dependencies,
    target_ids_by_identity,
    workflow_definition_line,
    workflow_definition_line_at_commit,
)
from rebase.tui_graph import dim
from rebase.tui_graph_pane import GraphPane


@pytest.fixture(autouse=True)
def _isolate_rebase_config(monkeypatch, tmp_path):
    """Mounting the app records the cwd's git root as a search path, and this suite
    itself runs inside a git repository — without this, every test that starts the app
    would write to the developer's real ~/.rebase/config.json."""
    monkeypatch.setenv("REBASE_CONFIG_PATH", str(tmp_path / "isolated-config.json"))


class FakeClient:
    def __init__(self) -> None:
        self.api_url = "https://api.example.com"
        self.run_calls: list[dict[str, Any]] = []
        self.run_detail_calls = 0
        self.latest_run_calls = 0
        self.latest_runs_by_project: list[dict[str, Any]] = []
        self.deleted: list[tuple[str, str, bool]] = []
        self.delete_lock = threading.Lock()
        self.batch_calls: list[list[str]] = []
        self.function_calls: list[str | None] = []
        self.workflow_calls: list[str | None] = []
        self.endpoint_calls: list[str | None] = []
        self.projects = [
            {"id": "project-id", "name": "energy"},
            {"id": "other-project-id", "name": "trading"},
        ]
        self.version_calls: list[tuple[str, str]] = []
        self.log_calls: list[str] = []
        self.task_calls: list[str] = []
        self.artifact_calls: list[str] = []
        self.artifact_open_calls: list[tuple[str, str]] = []
        # Empty by default: a workflow whose steps fan out into nothing has no tasks,
        # which is most of them. `SteppedClient` is the other kind.
        self.tasks: list[dict[str, Any]] = []
        self.artifacts: list[dict[str, Any]] = []
        # One line between the run's only event and its only step, so the timeline has to
        # interleave the three routes rather than concatenate them.
        self.log_entries: list[dict[str, Any]] = [
            {"timestamp": "2026-06-16T14:00:02Z", "severity": "INFO", "message": "Fetching curves."}
        ]
        # None is the ordinary case: a workflow whose body does the work itself has no
        # step graph at all. `SteppedClient` is the other kind.
        self.step_graph: dict[str, Any] | None = None
        self.functions = [
            {
                "id": "function-id",
                "project_id": "project-id",
                "name": "normalize",
                "execution_backend": "cloud_run",
                "enabled": True,
                "current_version_id": "function-version-id-123456",
                "updated_at": "2026-06-16T12:00:00Z",
            }
        ]
        self.workflows = [
            {
                "id": "workflow-id",
                "project_id": "project-id",
                "name": "forecast",
                "execution_backend": "prefect_cloud_run_service",
                "enabled": True,
                "current_version_id": "workflow-version-id-123456",
                "schedule": {"type": "cron", "cron": "0 6 * * *", "active": True},
                # Set by the API only for a schedule that will actually fire, which is
                # what makes this workflow count as a cron job.
                "next_run_at": "2026-06-17T06:00:00Z",
                "updated_at": "2026-06-16T13:00:00Z",
            }
        ]
        self.endpoints = [
            {
                "id": "endpoint-id",
                "project_id": "project-id",
                "project_name": "energy",
                "name": "forecast",
                "method": "POST",
                "path": "/forecast",
                "auth": "workspace",
                "mode": "async",
                "target_type": "workflow",
                "target_id": "workflow-id",
                "enabled": True,
                "url_path": "/e/energy-workspace/energy/forecast",
                "url": "https://api.example.com/e/energy-workspace/energy/forecast",
                "updated_at": "2026-06-16T13:30:00Z",
            }
        ]
        self.runs = [
            {
                "id": "run-id",
                "target_type": "workflow",
                "status": "succeeded",
                "execution_backend": "prefect_cloud_run_service",
                "parameters": {"site_id": "site-001"},
                "result": {"ok": True},
                "trigger_source": "schedule",
                "created_at": "2026-06-16T14:00:00Z",
                "started_at": "2026-06-16T14:00:10Z",
                "finished_at": "2026-06-16T14:01:00Z",
            }
        ]

    def get_workflow_version(self, workflow_id: str, version_id: str) -> dict[str, Any]:
        self.version_calls.append((workflow_id, version_id))
        return {
            "id": version_id,
            "workflow_id": workflow_id,
            "step_graph": self.step_graph,
            "source_mode": "workspace_repo",
            "repo_owner": "rebase-energy",
            "repo_name": "grid-workflows",
            "source_path": "deploy/forecast.py",
            "git_commit_sha": "0123456789abcdef0123456789abcdef01234567",
            "entrypoint": "forecast",
            "source_code": "def forecast():\n    return {'ok': True}\n",
        }

    def list_run_tasks(self, run_id: str, *, step_run_id: str | None = None) -> list[dict[str, Any]]:
        self.task_calls.append(run_id)
        return self.tasks

    def list_run_artifacts(
        self, run_id: str, *, step_run_id: str | None = None, task_id: str | None = None
    ) -> list[dict[str, Any]]:
        self.artifact_calls.append(run_id)
        return self.artifacts

    def open_run_artifact(self, run_id: str, artifact_id: str) -> str:
        self.artifact_open_calls.append((run_id, artifact_id))
        return "https://storage.example/signed-object"

    def get_run_logs(self, run_id: str, *, since: str | None = None, limit: int | None = None) -> dict[str, Any]:
        self.log_calls.append(run_id)
        return {"run_id": run_id, "source": "prefect", "entries": self.log_entries}

    def list_projects(self) -> list[dict[str, Any]]:
        return self.projects

    def delete_project(self, project_id: str, *, force: bool = False) -> None:
        # Deletes are issued concurrently, so the bookkeeping has to survive it.
        with self.delete_lock:
            self.deleted.append(("project", project_id, force))
            self.projects = [item for item in self.projects if item["id"] != project_id]

    def delete_projects(self, project_ids: list[str], *, force: bool = False) -> list[tuple[str, str]]:
        """Mirrors the real client: one call, a per-project verdict, never raising per project."""
        self.batch_calls.append(list(project_ids))
        failures: list[tuple[str, str]] = []
        for project_id in project_ids:
            try:
                self.delete_project(project_id, force=force)
            except Exception as exc:
                failures.append((project_id, str(exc)))
        return failures

    def delete_function(self, function_id: str, *, force: bool = False) -> None:
        with self.delete_lock:
            self.deleted.append(("function", function_id, force))
            self.functions = [item for item in self.functions if item["id"] != function_id]

    def delete_workflow(self, workflow_id: str, *, force: bool = False) -> None:
        with self.delete_lock:
            self.deleted.append(("workflow", workflow_id, force))
            self.workflows = [item for item in self.workflows if item["id"] != workflow_id]

    def list_functions(self, *, project: str | None = None, project_id: str | None = None) -> list[dict[str, Any]]:
        assert project is None
        self.function_calls.append(project_id)
        return [item for item in self.functions if project_id is None or item["project_id"] == project_id]

    def list_workflows(self, *, project: str | None = None, project_id: str | None = None) -> list[dict[str, Any]]:
        assert project is None
        self.workflow_calls.append(project_id)
        return [item for item in self.workflows if project_id is None or item["project_id"] == project_id]

    def list_endpoints(self, *, project_id: str | None = None) -> list[dict[str, Any]]:
        self.endpoint_calls.append(project_id)
        return [item for item in self.endpoints if project_id is None or item["project_id"] == project_id]

    def list_project_endpoints(self, project_id: str) -> list[dict[str, Any]]:
        return [item for item in self.endpoints if item["project_id"] == project_id]

    def list_runs(
        self,
        *,
        project_id: str | None = None,
        workflow_id: str | None = None,
        function_id: str | None = None,
        model_id: str | None = None,
        target_type: str | None = None,
        limit: int = 100,
        include_result: bool | None = None,
    ) -> list[dict[str, Any]]:
        self.run_calls.append(
            {
                "project_id": project_id,
                "workflow_id": workflow_id,
                "function_id": function_id,
                "model_id": model_id,
                "target_type": target_type,
                "limit": limit,
            }
        )
        if workflow_id is not None:
            return self.runs[:limit]
        return []

    def list_latest_runs_by_project(self) -> list[dict[str, Any]]:
        self.latest_run_calls += 1
        return self.latest_runs_by_project

    def get_run(self, run_id: str) -> dict[str, Any]:
        assert run_id == "run-id"
        self.run_detail_calls += 1
        return self.runs[0]

    def list_run_events(self, run_id: str) -> list[dict[str, Any]]:
        assert run_id == "run-id"
        return [
            {
                "id": "event-id",
                "stage": "dispatch",
                "status": "completed",
                "message": "Accepted run request.",
                "created_at": "2026-06-16T14:00:01Z",
            }
        ]

    def list_run_steps(self, run_id: str) -> list[dict[str, Any]]:
        assert run_id == "run-id"
        return [
            {
                "id": "step-id",
                "name": "load_weather",
                "status": "succeeded",
                "attempt": 1,
                "started_at": "2026-06-16T14:00:05Z",
                "finished_at": "2026-06-16T14:00:20Z",
                "error": None,
            }
        ]


class EnvironmentClient(FakeClient):
    def __init__(self, environment_name: str = "dev") -> None:
        super().__init__()
        self.environment_name = environment_name
        self.workspace_id = "workspace-id"
        self.projects = [{"id": f"{environment_name}-project", "name": "energy"}]
        self.functions = []
        self.workflows = []
        self.endpoints = []
        self.runs = []

    def with_environment(self, environment_name: str) -> EnvironmentClient:
        return EnvironmentClient(environment_name)

    def list_environments(self) -> list[dict[str, Any]]:
        return [{"name": "dev"}, {"name": "staging"}, {"name": "prod"}]

    def list_buckets(self) -> list[dict[str, Any]]:
        return [
            {
                "name": f"{self.environment_name}-data",
                "uri": f"gs://rb-{self.environment_name}-data-abc123",
                "console_url": (
                    f"https://console.cloud.google.com/storage/browser/rb-{self.environment_name}-data-abc123"
                    "?project=rebase-prod"
                ),
            }
        ]

    def list_volumes(self) -> list[dict[str, Any]]:
        return [{"name": f"{self.environment_name}-cache", "provider": "gcs"}]

    def list_secrets(self) -> list[dict[str, Any]]:
        return [{"name": f"{self.environment_name}-api", "keys": ["TOKEN"]}]


class SteppedClient(FakeClient):
    """A project whose workflow is built out of steps, which are functions of its own."""

    def __init__(self) -> None:
        super().__init__()
        # Deliberately out of graph order, and with a function no workflow calls: the API
        # stores functions however it likes, and the table is what puts them back in shape.
        self.functions = [
            self.functions[0],
            {
                "id": "load-function-id",
                "project_id": "project-id",
                "name": "load_weather",
                "enabled": True,
                "current_version_id": "load-version-id-123456",
                "updated_at": "2026-06-16T11:00:00Z",
            },
            {
                "id": "standalone-function-id",
                "project_id": "project-id",
                "name": "healthcheck",
                "enabled": True,
                "current_version_id": "health-version-id-123456",
                "updated_at": "2026-06-16T10:00:00Z",
            },
        ]
        # One task per unit of work inside `load_weather`, which is what a step's
        # fan-out looks like on the run.
        self.tasks = [
            {
                "id": "task-0",
                "name": "Capture NO1",
                "step_run_id": "step-id",
                "batch_id": "batch-id",
                "item_index": 0,
                "parameters": {"area": "NO1"},
                "status": "succeeded",
                "result": {"objects": 1},
                "error": None,
                "created_at": "2026-06-16T14:00:05Z",
                "started_at": "2026-06-16T14:00:06Z",
                "finished_at": "2026-06-16T14:00:12Z",
            },
            {
                "id": "task-1",
                "name": "Capture SE3",
                "step_run_id": "step-id",
                "batch_id": "batch-id",
                "item_index": 1,
                "parameters": {"area": "SE3"},
                "status": "failed",
                "result": None,
                "error": "401 Unauthorized",
                "created_at": "2026-06-16T14:00:05Z",
                "started_at": "2026-06-16T14:00:07Z",
                "finished_at": "2026-06-16T14:00:09Z",
            },
        ]
        self.artifacts = [
            {
                "id": "artifact-id",
                "name": "NO1 day-ahead curves",
                "key": "nordpool/2026-06-16/NO1.json",
                "uri": "gs://nordpool-curves/2026-06-16/NO1.json",
                "disposition": "created",
                "media_type": "application/json",
                "size_bytes": 4096,
                "producer_run_id": "mapped-run-id",
                "workflow_run_id": "run-id",
                "step_run_id": "step-id",
                "task_id": "task-0",
                "created_at": "2026-06-16T14:00:08Z",
            }
        ]
        self.step_graph = {
            "schema_version": 1,
            "engine": "prefect",
            "nodes": [
                {
                    "node_key": "load_weather",
                    "name": "load_weather",
                    "function_id": "load-function-id",
                    "upstream_node_keys": [],
                },
                {
                    "node_key": "normalize",
                    "name": "normalize",
                    "function_id": "function-id",
                    "upstream_node_keys": ["load_weather"],
                },
            ],
        }


class EmptyWorkspaceClient(FakeClient):
    def __init__(self) -> None:
        super().__init__()
        self.projects = []
        self.functions = []
        self.workflows = []
        self.endpoints = []


def fake_tui_data(client: Any, **kwargs: Any) -> RebaseTuiData:
    """FakeClient duck-types Client; the cast keeps the type checker honest at that seam."""
    return RebaseTuiData(cast(Client, client), **kwargs)


def test_tui_environment_resources_and_switcher() -> None:
    async def scenario() -> None:
        app = RebaseTuiApp(data=fake_tui_data(EnvironmentClient()), refresh_interval=0)
        async with app.run_test(size=(140, 42)) as pilot:
            await pilot.pause(0.3)

            assert app.title.endswith("(dev)")
            assert str(app.query_one("#buckets-table", DataTable).get_cell_at(Coordinate(0, 0))) == "dev-data"
            assert str(app.query_one("#buckets-table", DataTable).get_cell_at(Coordinate(0, 1))) == (
                "gs://rb-dev-data-abc123"
            )
            assert str(app.query_one("#secrets-table", DataTable).get_cell_at(Coordinate(0, 0))) == "dev-api"

            await pilot.press("v")
            await pilot.pause(0.1)
            options = app.screen.query_one("#environment-options", OptionList)
            options.highlighted = 2
            await pilot.press("enter")
            await pilot.pause(0.4)

            assert app.environment_name == "prod"
            assert app.data.environment_name == "prod"
            assert app.title.endswith("(prod)")
            assert str(app.query_one("#buckets-table", DataTable).get_cell_at(Coordinate(0, 0))) == "prod-data"

    asyncio.run(scenario())


def test_tui_format_helpers() -> None:
    assert compact_id("123456789abcdef") == "12345678..."
    assert format_timestamp("2026-06-16T12:00:00Z") == "2026-06-16 12:00:00"
    assert format_json_summary({"b": 2, "a": 1}) == '{"a": 1, "b": 2}'
    assert status_style("failed") == "#E46962"

    github = {"source_mode": "workspace_repo", "git_commit_sha": "0123456789abcdef"}
    assert is_github_backed(github) is True
    assert format_workflow_source(github) == "GitHub"
    assert format_workflow_commit(github) == "01234567..."
    assert format_workflow_commit(github, compact=False) == "0123456789abcdef"

    hosted = {"source_mode": "rebase_hosted", "git_commit_sha": "must-not-be-shown"}
    assert is_github_backed(hosted) is False
    assert format_workflow_source(hosted) == "Rebase"
    assert format_workflow_commit(hosted) == "-"


def test_tui_github_source_url_is_pinned_and_can_name_the_definition_line() -> None:
    version = {
        "source_mode": "workspace_repo",
        "repo_owner": "rebase-energy",
        "repo_name": "grid workflows",
        "git_commit_sha": "0123456789abcdef",
        "source_path": "deploy/forecast curves.py",
    }
    assert github_workflow_source_url(version, line=17) == (
        "https://github.com/rebase-energy/grid%20workflows/blob/0123456789abcdef/deploy/forecast%20curves.py#L17"
    )
    assert github_workflow_source_url({**version, "source_mode": "rebase_hosted"}) is None

    committed = "def forecast():\n    return 1\n\n\ndef forecast():\n    return 2\n"
    deployed = "def forecast():\n    return 2\n"
    assert workflow_definition_line(committed, "forecast", deployed) == 5
    # Two definitions and no deployed body to disambiguate them: do not guess a line.
    assert workflow_definition_line(committed, "forecast") is None


def test_tui_definition_line_reads_the_exact_git_object(monkeypatch, tmp_path) -> None:
    committed = "VALUE = 1\n\ndef forecast():\n    return VALUE\n"
    seen: list[tuple[Path, str, str]] = []
    monkeypatch.setattr("rebase.tui.git_toplevel", lambda root: tmp_path)

    def git_source(repo: Path, commit: str, source_path: str) -> str:
        seen.append((repo, commit, source_path))
        return committed

    monkeypatch.setattr("rebase.tui._git_source_at_commit", git_source)
    version = {
        "git_commit_sha": "0123456789abcdef",
        "source_path": "deploy/forecast.py",
        "entrypoint": "forecast",
        "source_code": "def forecast():\n    return VALUE\n",
    }

    assert workflow_definition_line_at_commit(version, [tmp_path]) == 3
    assert seen == [(tmp_path, "0123456789abcdef", "deploy/forecast.py")]


def test_artifact_browser_url_targets_the_exact_gcs_object() -> None:
    assert artifact_browser_url("gs://power-system-data/raw/nordpool/curve 1.json") == (
        "https://console.cloud.google.com/storage/browser/_details/power-system-data/raw/nordpool/curve%201.json"
    )
    assert artifact_browser_url("https://example.com/result.json") == "https://example.com/result.json"
    assert artifact_browser_url("s3://bucket/result.json") is None


def test_tui_data_loads_project_filtered_overview_and_runs() -> None:
    client = FakeClient()
    data = fake_tui_data(client, project="energy", limit=10)

    overview = data.load_workspace_overview()
    assert [summary.project["name"] for summary in overview.project_summaries] == ["energy", "trading"]
    assert [(summary.function_count, summary.workflow_count) for summary in overview.project_summaries] == [
        (1, 1),
        (0, 0),
    ]
    assert overview.project_names == {"project-id": "energy", "other-project-id": "trading"}

    targets = data.load_project_targets(overview.project_summaries[0].project)
    assert [item["name"] for item in targets.functions] == ["normalize"]
    assert [item["name"] for item in targets.workflows] == ["forecast"]

    runs = data.load_target_runs("workflow", "workflow-id")
    assert [run["id"] for run in runs] == ["run-id"]
    assert client.run_calls[-1] == {
        "project_id": None,
        "workflow_id": "workflow-id",
        "function_id": None,
        "model_id": None,
        "target_type": "workflow",
        "limit": 10,
    }


def test_tui_data_loads_endpoints() -> None:
    data = fake_tui_data(FakeClient(), project="energy")
    overview = data.load_workspace_overview()

    targets = data.load_project_targets(overview.project_summaries[0].project)
    assert [item["name"] for item in targets.endpoints] == ["forecast"]

    grouped = endpoints_by_target(targets.endpoints)
    assert list(grouped) == [("workflow", "workflow-id")]
    assert str(format_endpoint(grouped[("workflow", "workflow-id")])) == "POST /forecast"
    assert str(format_endpoint([])) == "-"


def test_tui_endpoint_column_counts_extra_endpoints_and_dims_disabled() -> None:
    disabled = {"method": "POST", "path": "/a", "enabled": False}
    extra = {"method": "GET", "path": "/b", "enabled": True}

    assert str(format_endpoint([disabled])) == "POST /a"
    assert format_endpoint([disabled]).style == BRAND_MEDIUM_GRAY
    assert format_endpoint([extra]).style == ""
    assert str(format_endpoint([extra, disabled])) == "GET /b (+1)"


def test_tui_step_dependencies_resolve_node_keys_to_the_names_rows_show() -> None:
    """The graph addresses nodes by key; a step row shows the node's name.

    Left untranslated the column would say `count_to` beside a row reading `count-to`,
    which is the same step spelled two ways. An upstream with no node of its own keeps
    its raw key rather than being renamed into something that is not there.
    """
    graph = {
        "nodes": [
            {"node_key": "announce", "name": "announce", "upstream_node_keys": []},
            {"node_key": "count_to", "name": "count-to", "upstream_node_keys": ["announce"]},
            {"node_key": "sign_off", "name": "sign-off", "upstream_node_keys": ["count_to", "dropped"]},
        ]
    }

    assert step_dependencies(graph) == {
        "announce": [],
        "count_to": ["announce"],
        "sign_off": ["count-to", "dropped"],
    }


def test_tui_step_dependencies_tolerate_a_workflow_with_no_graph() -> None:
    """A workflow whose body does the work itself has no nodes, and that is not a fault."""
    assert step_dependencies(None) == {}
    assert step_dependencies({}) == {}
    assert step_dependencies({"nodes": "not-a-list"}) == {}


def test_tui_step_dependencies_survive_a_workflow_version_it_cannot_read() -> None:
    """A version the server will not serve costs the column, not the run view."""

    class NoVersionClient(SteppedClient):
        def __init__(self) -> None:
            super().__init__()
            self.runs[0]["target_id"] = "workflow-id"
            self.runs[0]["target_version_id"] = "workflow-version-id-123456"

        def get_workflow_version(self, workflow_id: str, version_id: str) -> dict[str, Any]:
            raise RebaseWorkflowError("no such version")

    detail = fake_tui_data(NoVersionClient()).load_run_detail("run-id", target_type="workflow")

    assert detail.step_graph is None
    assert step_dependencies(detail.step_graph) == {}
    assert [step["name"] for step in detail.steps] == ["load_weather"]


def test_tui_data_reads_the_step_graph_off_the_current_workflow_version() -> None:
    client = SteppedClient()
    data = fake_tui_data(client, project="energy")
    overview = data.load_workspace_overview()

    targets = data.load_project_targets(overview.project_summaries[0].project)

    assert client.version_calls == [("workflow-id", "workflow-version-id-123456")]
    assert [(step.workflow_name, step.node_key, step.upstream) for step in targets.steps] == [
        ("forecast", "load_weather", ()),
        ("forecast", "normalize", ("load_weather",)),
    ]
    assert set(targets.steps_by_function()) == {"load-function-id", "function-id"}
    assert targets.workflow_versions["workflow-id"]["git_commit_sha"] == ("0123456789abcdef0123456789abcdef01234567")


def test_tui_data_reports_no_steps_for_a_workflow_that_is_its_own_body() -> None:
    """The common case: `step_graph` is null, and that is not a failure."""
    client = FakeClient()
    data = fake_tui_data(client, project="energy")
    overview = data.load_workspace_overview()

    targets = data.load_project_targets(overview.project_summaries[0].project)

    assert client.version_calls == [("workflow-id", "workflow-version-id-123456")]
    assert targets.steps == ()
    assert targets.steps_by_function() == {}


def test_tui_data_tolerates_a_workflow_version_route_that_fails() -> None:
    """The Workflow column is supplementary; losing it must not empty the project view."""

    class Unsupported(SteppedClient):
        def get_workflow_version(self, workflow_id: str, version_id: str) -> dict[str, Any]:
            raise RebaseWorkflowError("404 Not Found")

    data = fake_tui_data(Unsupported(), project="energy")
    overview = data.load_workspace_overview()

    targets = data.load_project_targets(overview.project_summaries[0].project)

    assert targets.steps == ()
    assert [item["name"] for item in targets.functions] == ["normalize", "load_weather", "healthcheck"]
    assert [item["name"] for item in targets.workflows] == ["forecast"]


def test_tui_workflow_steps_skips_nodes_with_nothing_behind_them() -> None:
    assert tui_module.workflow_steps("workflow-id", "forecast", None) == ()
    assert tui_module.workflow_steps("workflow-id", "forecast", {"nodes": "not-a-list"}) == ()
    steps = tui_module.workflow_steps(
        "workflow-id",
        "forecast",
        {"nodes": [{"node_key": "a", "name": "a"}, {"node_key": "b_2", "name": "b", "function_id": "fn"}]},
    )
    assert [(step.node_key, step.name, step.function_id, step.order) for step in steps] == [("b_2", "b", "fn", 1)]


def test_tui_step_column_formatters() -> None:
    def step(workflow_name: str, node_key: str) -> tui_module.WorkflowStep:
        return tui_module.WorkflowStep(
            workflow_id=workflow_name,
            workflow_name=workflow_name,
            node_key=node_key,
            name=node_key,
            function_id="fn",
            upstream=(),
            order=0,
        )

    assert format_step_workflows([]) == "-"
    assert format_step_keys([]) == "-"
    assert format_step_workflows([step("forecast", "a")]) == "forecast"
    assert format_step_workflows([step("forecast", "a"), step("forecast", "a_2")]) == "forecast"
    assert format_step_workflows([step("forecast", "a"), step("backfill", "a")]) == "forecast (+1)"
    assert format_step_keys([step("forecast", "a"), step("forecast", "a_2")]) == "a, a_2"


def test_tui_format_duration() -> None:
    assert format_duration("2026-06-16T14:00:10Z", "2026-06-16T14:01:00Z") == "50.0s"
    assert format_duration("2026-06-16T14:00:10Z", "2026-06-16T14:05:40Z") == "5m30s"
    assert format_duration("2026-06-16T14:00:00Z", "2026-06-16T16:30:00Z") == "2h30m"
    # A run that has not started, or not finished, has no duration to report yet.
    assert format_duration(None, "2026-06-16T14:01:00Z") == "-"
    assert format_duration("2026-06-16T14:00:10Z", None) == "-"
    assert format_duration("nonsense", "2026-06-16T14:01:00Z") == "-"


def test_tui_format_artifact_size() -> None:
    assert format_bytes(0) == "0 B"
    assert format_bytes(4096) == "4.0 KiB"
    assert format_bytes(3 * 1024 * 1024) == "3.0 MiB"
    assert format_bytes(None) == "-"
    assert format_bytes(-1) == "-"


def test_tui_data_tolerates_a_missing_endpoint_route() -> None:
    class Unsupported(FakeClient):
        def list_project_endpoints(self, project_id: str) -> list[dict[str, Any]]:
            raise RebaseWorkflowError("404 Not Found")

    data = fake_tui_data(Unsupported(), project="energy")
    overview = data.load_workspace_overview()

    targets = data.load_project_targets(overview.project_summaries[0].project)
    assert targets.endpoints == []
    assert [item["name"] for item in targets.workflows] == ["forecast"]


def test_tui_overview_reads_every_count_in_one_call_each() -> None:
    """Per-project requests for these were the bulk of the TUI's startup wait.

    Functions were the last column still asked for project by project, which made
    opening the workspace view scale with the number of projects. All three are now a
    single workspace-wide read, so the overview costs the same for 3 projects as for 30.
    """
    client = FakeClient()
    client.projects.append({"id": "third-project-id", "name": "storage"})

    overview = fake_tui_data(client).load_workspace_overview()

    assert client.workflow_calls == [None]
    assert client.endpoint_calls == [None]
    assert client.function_calls == [None]
    assert [
        (summary.function_count, summary.workflow_count, summary.endpoint_count, summary.cron_count)
        for summary in overview.project_summaries
    ] == [(1, 1, 1, 1), (0, 0, 0, 0), (0, 0, 0, 0)]


def test_tui_project_row_puts_each_count_under_its_own_header() -> None:
    """Distinct counts, so swapping two columns cannot pass."""

    async def scenario() -> None:
        client = FakeClient()
        # energy: 3 functions, 2 workflows of which 1 is a cron, 1 endpoint.
        client.functions.extend(
            [
                {"id": "second-function-id", "project_id": "project-id", "name": "clean"},
                {"id": "third-function-id", "project_id": "project-id", "name": "publish"},
            ]
        )
        client.workflows.append(
            {"id": "ad-hoc-workflow-id", "project_id": "project-id", "name": "ad-hoc", "enabled": True}
        )
        app = RebaseTuiApp(data=fake_tui_data(client, limit=5))

        async with app.run_test(size=(140, 42)) as pilot:
            await pilot.pause(0.2)
            projects = app.query_one("#projects-table", SelectableDataTable)
            assert [str(column.label) for column in projects.columns.values()] == [
                "Project",
                "Functions",
                "Workflows",
                "Cron jobs",
                "Endpoints",
                "Status",
                "Last run",
                "Next run",
                "Created",
            ]
            # The count columns keep their order; the three time columns follow.
            assert [str(cell) for cell in projects.get_row_at(0)][:5] == ["energy", "3", "2", "1", "1"]
            assert [str(cell) for cell in projects.get_row_at(1)][:5] == ["trading", "0", "0", "0", "0"]

    asyncio.run(scenario())


def test_format_countdown_drops_to_the_two_units_that_matter() -> None:
    """A wall-clock next run is a subtraction the reader has to do; a countdown is not."""
    now = datetime(2026, 9, 2, 12, 0, 0, tzinfo=UTC)

    def until(**delta: float) -> str:
        return format_countdown(now + timedelta(**delta), now=now)

    assert until(seconds=12) == "in 12s"
    assert until(minutes=4, seconds=9) == "in 4m 09s"
    # Past the hour the seconds are noise, and two units is all the column has room for.
    assert until(hours=3, minutes=4, seconds=59) == "in 3h 04m"
    assert until(days=2, hours=3, minutes=59) == "in 2d 3h"
    # Nothing produced is wider than the width every value is padded out to.
    assert len(until(days=364, hours=23)) == COUNTDOWN_WIDTH
    # A time that has been and gone says the schedule owes a run, not "-12s".
    assert until(seconds=-30) == "due"
    assert format_countdown(None, now=now) == "-"
    # Naive timestamps are the API's UTC, the same assumption the timestamp column makes.
    assert format_countdown("2026-09-02T12:05:00", now=now) == "in 5m 00s"


def test_tui_next_run_counts_down_and_ticks_without_a_refresh() -> None:
    """The overview's Next run is a live duration: it moves on its own, in place."""
    client = FakeClient()
    client.workflows[0]["next_run_at"] = (datetime.now(UTC) + timedelta(minutes=5, seconds=30)).isoformat()

    async def scenario() -> None:
        app = RebaseTuiApp(data=fake_tui_data(client, limit=5))

        async with app.run_test(size=(140, 42)) as pilot:
            await pilot.pause(0.2)
            projects = app.query_one("#projects-table", SelectableDataTable)

            def next_run(row: int) -> str:
                return str(projects.get_row_at(row)[NEXT_RUN_COLUMN])

            assert next_run(0).strip().startswith("in 5m ")
            # A project with no cron has no countdown, and pads to the same width so the
            # column is sized once and never resized under a cell that redraws.
            assert next_run(1).strip() == "-"
            assert {len(next_run(0)), len(next_run(1))} == {COUNTDOWN_WIDTH}

            first = next_run(0)
            await pilot.pause(1.2)
            # No refresh has run: the cell redrew itself off the clock.
            assert next_run(0) != first
            assert next_run(0).strip().startswith("in 5m ")

    asyncio.run(scenario())


def test_tui_countdown_keeps_ticking_on_a_marked_row_without_unmarking_it() -> None:
    """The one cell that redraws on a timer must not rub the mark off the row it is in."""
    client = FakeClient()
    client.workflows[0]["next_run_at"] = (datetime.now(UTC) + timedelta(minutes=5, seconds=30)).isoformat()

    async def scenario() -> None:
        app = RebaseTuiApp(data=fake_tui_data(client, limit=5))

        async with app.run_test(size=(140, 42)) as pilot:
            await pilot.pause(0.2)
            projects = app.query_one("#projects-table", SelectableDataTable)
            projects.focus()
            await pilot.press("shift+down")
            await pilot.pause(0.1)
            assert projects.marked_keys == ["project-id", "other-project-id"]

            def next_run(row: int) -> Any:
                return projects.get_row_at(row)[NEXT_RUN_COLUMN]

            first = str(next_run(0))
            await pilot.pause(1.2)
            # Still ticking, still marked: the amber is reapplied to the value the tick
            # wrote, rather than lost with the renderable it replaced.
            assert str(next_run(0)) != first
            assert MARK_STYLE in [span.style for span in next_run(0).spans]

            # And unmarking restores what the countdown says *now*, not what it said when
            # the mark went on a second or more ago.
            ticked = str(next_run(0))
            await pilot.press("escape")
            await pilot.pause(0.1)
            assert projects.marked_keys == []
            assert str(next_run(0)) == ticked

    asyncio.run(scenario())


def test_tui_counts_configured_crons_and_rolls_their_states_into_status() -> None:
    """A stopped or paused cron is still a cron job; the Status column carries its state."""
    client = FakeClient()
    client.workflows.append(
        {
            "id": "stopped-workflow-id",
            "project_id": "project-id",
            "name": "stopped",
            "enabled": True,
            # Deactivated in the schedule itself: configured, deliberately not firing.
            "schedule": {"type": "cron", "cron": "0 6 * * *", "active": False},
            "next_run_at": None,
        }
    )
    client.workflows.append({"id": "ad-hoc-workflow-id", "project_id": "project-id", "name": "ad-hoc", "enabled": True})

    overview = fake_tui_data(client).load_workspace_overview()

    energy = overview.project_summaries[0]
    assert energy.workflow_count == 3
    assert energy.cron_count == 2
    # Any cron still due to fire makes the project active; run outcomes play no part.
    assert energy.cron_status == "active"


def test_tui_status_reads_paused_with_the_soonest_resume_when_nothing_fires() -> None:
    """With every firing cron paused, Status says so and names the earliest resume."""
    client = FakeClient()
    client.workflows[0]["paused"] = True
    client.workflows[0]["paused_until"] = "2099-09-15T10:00:00Z"

    overview = fake_tui_data(client).load_workspace_overview()

    energy = overview.project_summaries[0]
    assert energy.cron_count == 1
    assert energy.cron_status == "paused"
    assert energy.paused_until == "2099-09-15T10:00:00Z"


def test_tui_status_treats_an_expired_pause_as_lifted() -> None:
    """A pause whose expiry has passed no longer holds, whatever the row still says."""
    client = FakeClient()
    client.workflows[0]["paused"] = True
    client.workflows[0]["paused_until"] = "2020-01-01T00:00:00Z"

    overview = fake_tui_data(client).load_workspace_overview()

    assert overview.project_summaries[0].cron_status == "active"


def test_tui_overview_survives_an_api_without_the_endpoints_route() -> None:
    """The endpoint column is supplementary; losing it must not empty the project table."""

    class Unsupported(FakeClient):
        def list_endpoints(self, *, project_id: str | None = None) -> list[dict[str, Any]]:
            raise RebaseWorkflowError("404 Not Found")

    overview = fake_tui_data(Unsupported()).load_workspace_overview()

    assert [summary.endpoint_count for summary in overview.project_summaries] == [0, 0]
    assert [(summary.function_count, summary.workflow_count) for summary in overview.project_summaries] == [
        (1, 1),
        (0, 0),
    ]


def test_tui_data_reports_missing_project() -> None:
    with pytest.raises(RebaseWorkflowError, match="project not found: missing"):
        fake_tui_data(FakeClient(), project="missing").load_workspace_overview()


class ConcurrentOverviewClient(FakeClient):
    """Each read blocks until the other has started, so only true concurrency completes.

    The reads the workspace view needs used to go out in three waves, because
    `_overview_counts` took the project list as an argument it never read. Serialise them
    again and this deadlocks until the timeout, rather than passing a little slower.
    """

    def __init__(self) -> None:
        super().__init__()
        self.projects_started = threading.Event()
        self.counts_started = threading.Event()
        self.resources_started = threading.Event()

    def list_projects(self) -> list[dict[str, Any]]:
        self.projects_started.set()
        assert self.counts_started.wait(timeout=5), "the counts read waited on the project list"
        return super().list_projects()

    def list_workflows(self, *, project: str | None = None, project_id: str | None = None) -> list[dict[str, Any]]:
        self.counts_started.set()
        assert self.projects_started.wait(timeout=5), "the project list waited on the counts read"
        return super().list_workflows(project=project, project_id=project_id)

    def list_buckets(self) -> list[dict[str, Any]]:
        self.resources_started.set()
        assert self.projects_started.wait(timeout=5), "the project list waited on the resource reads"
        return []


def test_workspace_base_reads_projects_and_counts_together() -> None:
    base = fake_tui_data(ConcurrentOverviewClient()).load_workspace_base()

    assert [project["name"] for project in base.projects] == ["energy", "trading"]
    assert [summary.workflow_count for summary in base.project_summaries] == [1, 0]


def test_workspace_overview_reads_the_base_and_the_resources_together() -> None:
    client = ConcurrentOverviewClient()

    overview = fake_tui_data(client).load_workspace_overview()

    assert client.resources_started.is_set()
    assert [project["name"] for project in overview.projects] == ["energy", "trading"]


def test_workspace_base_holds_back_what_the_project_table_does_not_need() -> None:
    base = fake_tui_data(EnvironmentClient()).load_workspace_base()

    assert [project["name"] for project in base.projects] == ["energy"]
    # The four environment-sibling reads belong to `load_workspace_resources`, not here.
    assert base.environments == []
    assert base.buckets == []
    assert base.volumes == []
    assert base.secrets == []


def test_workspace_resources_fills_in_what_the_base_left_empty() -> None:
    resources = fake_tui_data(EnvironmentClient()).load_workspace_resources()

    assert [item["name"] for item in resources.environments] == ["dev", "staging", "prod"]
    assert [item["name"] for item in resources.buckets] == ["dev-data"]
    assert [item["name"] for item in resources.volumes] == ["dev-cache"]
    assert [item["name"] for item in resources.secrets] == ["dev-api"]


def test_workspace_resources_degrade_one_column_at_a_time() -> None:
    class Unsupported(EnvironmentClient):
        def list_buckets(self) -> list[dict[str, Any]]:
            raise RebaseWorkflowError("404 Not Found")

    resources = fake_tui_data(Unsupported()).load_workspace_resources()

    assert resources.buckets == []
    # The route that failed costs its own column and nothing else.
    assert [item["name"] for item in resources.volumes] == ["dev-cache"]
    assert [item["name"] for item in resources.secrets] == ["dev-api"]


def test_workspace_resources_name_the_current_environment_when_the_route_is_absent() -> None:
    # FakeClient defines none of the four resource routes, which is how a client too old
    # to have them behaves.
    resources = fake_tui_data(FakeClient()).load_workspace_resources()

    assert resources.environments == [{"name": "dev"}]
    assert resources.buckets == []


#: The one latest-run row both overview paths are given, so they can be compared.
LATEST_RUN = {"project_id": "project-id", "id": "run-id", "created_at": "2026-06-16T14:00:00Z"}


class CompositeClient(FakeClient):
    """A platform that has the composite route, and fails loudly if the fan-out is used."""

    def __init__(self) -> None:
        super().__init__()
        self.overview_calls = 0
        self.secret_calls = 0

    def get_workspace_overview(self) -> dict[str, Any]:
        self.overview_calls += 1
        return {
            "environment": "dev",
            "projects": self.projects,
            "workflows": self.workflows,
            "functions": self.functions,
            "endpoints": self.endpoints,
            "latest_runs_by_project": [LATEST_RUN],
            "environments": [{"name": "dev"}, {"name": "prod"}],
            "buckets": [{"name": "data"}],
            "volumes": [],
            # No secrets: the route deliberately does not answer for them.
        }

    def list_secrets(self) -> list[dict[str, Any]]:
        self.secret_calls += 1
        return [{"name": "api", "keys": ["TOKEN"]}]

    def _refuse(self, *_a: Any, **_k: Any) -> Any:
        raise AssertionError("the fan-out ran even though the composite route answered")

    list_projects = _refuse
    list_workflows = _refuse
    list_functions = _refuse
    list_endpoints = _refuse
    list_latest_runs_by_project = _refuse


def test_workspace_overview_prefers_the_composite_route() -> None:
    client = CompositeClient()

    overview = fake_tui_data(client).load_workspace_overview()

    assert client.overview_calls == 1
    assert [project["name"] for project in overview.projects] == ["energy", "trading"]
    # The counts come off the payload's raw lists, through the same arithmetic the
    # fan-out uses — including the cron rollup.
    assert [summary.workflow_count for summary in overview.project_summaries] == [1, 0]
    assert [summary.function_count for summary in overview.project_summaries] == [1, 0]
    assert overview.project_summaries[0].cron_status == "active"
    assert overview.project_summaries[0].last_run == LATEST_RUN
    assert [item["name"] for item in overview.buckets] == ["data"]
    # Secrets are the one thing the route does not carry, so they are not here yet.
    assert overview.secrets == []
    assert client.secret_calls == 0


def test_workspace_secrets_are_read_on_their_own_after_the_composite() -> None:
    """Secret Manager costs more than the rest of the view together, so the table it
    fills waits for nobody."""
    client = CompositeClient()
    data = fake_tui_data(client)

    data.load_workspace_overview()
    assert client.secret_calls == 0

    assert [item["name"] for item in data.load_workspace_secrets()] == ["api"]
    assert client.secret_calls == 1


def test_workspace_secrets_degrade_to_an_empty_table() -> None:
    class Unsupported(CompositeClient):
        def list_secrets(self) -> list[dict[str, Any]]:
            raise RebaseWorkflowError("502 Secret Manager error")

    assert fake_tui_data(Unsupported()).load_workspace_secrets() == []


def test_workspace_overview_falls_back_when_the_platform_lacks_the_route() -> None:
    """The deployed platform routinely lags the toolkit, so this is the common path."""

    class NoRoute(FakeClient):
        def get_workspace_overview(self) -> dict[str, Any] | None:
            return None

    overview = fake_tui_data(NoRoute()).load_workspace_overview()

    assert [project["name"] for project in overview.projects] == ["energy", "trading"]
    assert [summary.workflow_count for summary in overview.project_summaries] == [1, 0]


def test_workspace_overview_falls_back_when_the_client_has_no_such_method() -> None:
    # FakeClient predates the method entirely, which is how an older pairing behaves.
    overview = fake_tui_data(FakeClient()).load_workspace_overview()

    assert [project["name"] for project in overview.projects] == ["energy", "trading"]


def test_composite_overview_still_reports_a_missing_project() -> None:
    with pytest.raises(RebaseWorkflowError, match="project not found: missing"):
        fake_tui_data(CompositeClient(), project="missing").load_workspace_overview()


def test_composite_and_fanout_agree_on_the_project_table() -> None:
    """The two paths must produce the same rows, or the table changes as platforms roll."""
    fanout_client = FakeClient()
    fanout_client.latest_runs_by_project = [LATEST_RUN]

    composite = fake_tui_data(CompositeClient()).load_workspace_overview()
    fanout = fake_tui_data(fanout_client).load_workspace_overview()

    assert composite.project_summaries == fanout.project_summaries
    assert composite.project_names == fanout.project_names


def test_tui_app_mounts_and_renders_selected_workflow_run() -> None:
    async def scenario() -> None:
        client = FakeClient()
        app = RebaseTuiApp(data=fake_tui_data(client, project="energy", limit=5))

        async with app.run_test(size=(140, 42)) as pilot:
            await pilot.pause(0.2)

            assert app.query_one("#workspace-view").styles.display == "block"
            assert app.query_one("#project-view").styles.display == "none"
            assert app.query_one(Header).icon == "• Commands"
            projects = app.query_one("#projects-table", DataTable)
            functions = app.query_one("#functions-table", DataTable)
            workflows = app.query_one("#workflows-table", DataTable)
            assert projects.styles.scrollbar_size_vertical == 1
            # The projects table scrolls sideways too: eight columns past the name, and
            # "Created" was falling off the right of a narrow terminal with no way back.
            assert projects.styles.scrollbar_size_horizontal == 1
            assert projects.styles.scrollbar_background.hex == "#101412"
            assert projects.zebra_stripes is False
            assert projects.row_count == 2
            assert functions.row_count == 0
            assert workflows.row_count == 0

            projects.focus()
            projects.move_cursor(row=0)
            await pilot.press("enter")
            await pilot.pause(0.2)

            assert app.query_one("#workspace-view").styles.display == "none"
            assert app.query_one("#project-view").styles.display == "block"
            assert app.query_one("#project-view").show_vertical_scrollbar is False
            assert app.query_one("#target-tabs").show_vertical_scrollbar is False
            assert app.query_one("#workflows-tab").show_vertical_scrollbar is False
            assert functions.row_count == 1
            assert workflows.row_count == 1
            # Both target tables keep a horizontal scrollbar: nine columns each, and
            # clipping would hide "Last run" entirely on a narrow terminal.
            for table in (functions, workflows):
                assert table.styles.scrollbar_size_horizontal == 1
            assert app.query_one("#runs-table", DataTable).styles.scrollbar_size_horizontal == 0
            for table in (functions, workflows, app.query_one("#runs-table", DataTable)):
                assert table.styles.scrollbar_background.hex == "#101412"
            assert workflows.styles.scrollbar_color.hex == "#03C497"
            assert app.query_one("#projects-table", DataTable).styles.scrollbar_color.hex == "#03C497"
            assert functions.styles.scrollbar_color.hex != "#03C497"
            assert app.query_one("#runs-table", DataTable).styles.scrollbar_color.hex != "#03C497"

            workflows.focus()
            workflows.move_cursor(row=0)
            await pilot.press("enter")
            await pilot.pause(0.2)

            runs = app.query_one("#runs-table", DataTable)
            assert runs.row_count == 1
            # Selected by id — asserted over the calls rather than on the last one,
            # because opening a target also scans the project for runs that ran under
            # its name and that scan passes no workflow_id.
            assert any(call.get("workflow_id") == "workflow-id" for call in client.run_calls)

            runs.focus()
            runs.move_cursor(row=0)
            await pilot.press("enter")
            await pilot.pause(0.2)

            # Event, log and step land in one activity view, in the order they happened.
            # Columns: Time, Type, Scope, Item, Status, Summary.
            timeline = app.query_one("#timeline-table", DataTable)
            assert timeline.row_count == 3
            assert [str(timeline.get_cell_at(Coordinate(row, 1))) for row in range(3)] == [
                "event",
                "log",
                "step",
            ]
            assert [str(timeline.get_cell_at(Coordinate(row, 3))).strip() for row in range(3)] == [
                "dispatch",
                "",
                "load_weather",
            ]

            # Back closes the timeline, then the runs box, and only then leaves the project.
            await pilot.press("b")
            await pilot.pause(0.2)
            assert app.query_one("#timeline-pane").styles.display == "none"
            assert app.query_one("#runs-table").styles.display == "block"
            assert app.query_one("#project-view").styles.display == "block"

            await pilot.press("b")
            await pilot.pause(0.2)
            assert app.query_one("#runs-table").styles.display == "none"
            assert app.query_one("#project-view").styles.display == "block"

            await pilot.press("b")
            await pilot.pause(0.2)
            assert app.query_one("#workspace-view").styles.display == "block"
            assert app.query_one("#project-view").styles.display == "none"
            assert projects.row_count == 2

    asyncio.run(scenario())


def test_tui_app_shows_workflow_provenance_the_endpoint_column_and_the_target_tabs() -> None:
    async def scenario() -> None:
        app = RebaseTuiApp(data=fake_tui_data(FakeClient(), project="energy", limit=5))

        async with app.run_test(size=(140, 42)) as pilot:
            await pilot.pause(0.2)
            projects = app.query_one("#projects-table", DataTable)
            projects.focus()
            projects.move_cursor(row=0)
            await pilot.press("enter")
            await pilot.pause(0.2)

            workflows = app.query_one("#workflows-table", DataTable)
            functions = app.query_one("#functions-table", DataTable)
            # A GitHub-backed workflow names its source and deployed commit. The table
            # shortens the SHA; the drawer below keeps the copyable value intact.
            assert str(workflows.get_cell_at(Coordinate(0, 6))) == "GitHub"
            assert str(workflows.get_cell_at(Coordinate(0, 7))) == "01234567..."

            # Endpoint sits after the schedule columns in the workflow table and after
            # the step-graph columns in the function table.
            assert str(workflows.get_cell_at(Coordinate(0, 10))) == "POST /forecast"
            assert str(functions.get_cell_at(Coordinate(0, 6))) == "-"

            # The workflow's endpoint, with the full URL the column has no room for.
            workflows.focus()
            workflows.move_cursor(row=0)
            await pilot.press("p")
            await pilot.pause(0.2)
            drawer = app.screen
            assert isinstance(drawer, DetailDrawer)
            assert [(f.label, f.value) for f in drawer.fields] == [
                ("Workflow", "forecast"),
                ("State", "enabled"),
                ("Source", "GitHub"),
                ("Commit", "0123456789abcdef0123456789abcdef01234567"),
            ]
            assert drawer.payload["endpoints"][0]["url"] == "https://api.example.com/e/energy-workspace/energy/forecast"
            await pilot.press("escape")
            await pilot.pause(0.2)

            # Two chips, and left/right toggles between them either way round. The
            # brackets are in the label, so a chip reads as pressable without colour.
            tabs = app.query_one("#target-tabs", TabbedContent)
            assert [str(tab.label) for tab in app.query("#target-tabs Tab")] == [
                "[ Workflows ]",
                "[ Functions ]",
            ]
            assert tabs.active == "workflows-tab"
            await pilot.press("right")
            await pilot.pause(0.1)
            assert tabs.active == "functions-tab"
            await pilot.press("right")
            await pilot.pause(0.1)
            assert tabs.active == "workflows-tab"
            await pilot.press("left")
            await pilot.pause(0.1)
            assert tabs.active == "functions-tab"

            # The selected chip is a filled rectangle rather than an underlined label,
            # and the strip costs one row rather than two.
            assert app.query_one("#target-tabs Tabs").size.height == 1
            active = next(tab for tab in app.query("#target-tabs Tab") if tab.has_class("-active"))
            assert active.styles.background.hex == "#03C497"

    asyncio.run(scenario())


def test_tui_marks_rebase_hosted_workflow_without_a_git_commit() -> None:
    """A hosted source is explicit too, and never borrows incidental local git metadata."""

    class HostedClient(FakeClient):
        def get_workflow_version(self, workflow_id: str, version_id: str) -> dict[str, Any]:
            version = super().get_workflow_version(workflow_id, version_id)
            return {**version, "source_mode": "rebase_hosted", "git_commit_sha": "local-only-sha"}

    async def scenario() -> None:
        app = RebaseTuiApp(data=fake_tui_data(HostedClient(), project="energy", limit=5))

        async with app.run_test(size=(140, 42)) as pilot:
            await pilot.pause(0.2)
            projects = app.query_one("#projects-table", DataTable)
            projects.focus()
            projects.move_cursor(row=0)
            await pilot.press("enter")
            await pilot.pause(0.3)

            workflows = app.query_one("#workflows-table", DataTable)
            assert str(workflows.get_cell_at(Coordinate(0, 6))) == "Rebase"
            assert str(workflows.get_cell_at(Coordinate(0, 7))) == "-"

            workflows.focus()
            workflows.move_cursor(row=0)
            await pilot.press("p")
            await pilot.pause(0.2)
            drawer = app.screen
            assert isinstance(drawer, DetailDrawer)
            assert [(f.label, f.value) for f in drawer.fields] == [
                ("Workflow", "forecast"),
                ("State", "enabled"),
                ("Source", "Rebase"),
            ]

    asyncio.run(scenario())


def test_tui_g_opens_the_selected_workflow_at_its_deployed_commit_and_definition(monkeypatch) -> None:
    opened: list[str] = []
    monkeypatch.setattr("rebase.tui.workflow_definition_line_at_commit", lambda version, roots: 17)
    monkeypatch.setattr("rebase.tui.webbrowser.open", lambda url: opened.append(url) or True)

    async def scenario() -> None:
        app = RebaseTuiApp(data=fake_tui_data(FakeClient(), project="energy", limit=5))

        async with app.run_test(size=(140, 42)) as pilot:
            await pilot.pause(0.2)
            projects = app.query_one("#projects-table", DataTable)
            projects.focus()
            projects.move_cursor(row=0)
            await pilot.press("enter")
            await pilot.pause(0.3)

            workflows = app.query_one("#workflows-table", DataTable)
            workflows.focus()
            workflows.move_cursor(row=0)
            await pilot.press("g")
            await pilot.pause(0.3)

    asyncio.run(scenario())
    assert opened == [
        "https://github.com/rebase-energy/grid-workflows/blob/"
        "0123456789abcdef0123456789abcdef01234567/deploy/forecast.py#L17"
    ]


def test_tui_g_does_not_open_rebase_hosted_workflow(monkeypatch) -> None:
    opened: list[str] = []
    monkeypatch.setattr("rebase.tui.webbrowser.open", lambda url: opened.append(url) or True)

    class HostedClient(FakeClient):
        def get_workflow_version(self, workflow_id: str, version_id: str) -> dict[str, Any]:
            return {**super().get_workflow_version(workflow_id, version_id), "source_mode": "rebase_hosted"}

    async def scenario() -> None:
        app = RebaseTuiApp(data=fake_tui_data(HostedClient(), project="energy", limit=5))

        async with app.run_test(size=(140, 42)) as pilot:
            await pilot.pause(0.2)
            projects = app.query_one("#projects-table", DataTable)
            projects.focus()
            projects.move_cursor(row=0)
            await pilot.press("enter")
            await pilot.pause(0.3)

            workflows = app.query_one("#workflows-table", DataTable)
            workflows.focus()
            workflows.move_cursor(row=0)
            await pilot.press("g")
            await pilot.pause(0.2)

    asyncio.run(scenario())
    assert opened == []


@contextmanager
def local_rebase_api(
    log_entries: list[dict[str, Any]] | None = None,
    run_events: list[dict[str, Any]] | None = None,
) -> Iterator[tuple[str, list[tuple[str, dict[str, list[str]], str | None]]]]:
    seen_requests: list[tuple[str, dict[str, list[str]], str | None]] = []
    projects = [{"id": "project-id", "name": "energy"}]
    functions = [
        {
            "id": "function-id",
            "project_id": "project-id",
            "name": "normalize",
            "execution_backend": "cloud_run",
            "enabled": True,
            "current_version_id": "function-version-id",
            "updated_at": "2026-06-16T12:00:00Z",
        }
    ]
    workflows = [
        {
            "id": "workflow-id",
            "project_id": "project-id",
            "name": "forecast",
            "execution_backend": "prefect_cloud_run_service",
            "enabled": True,
            "current_version_id": "workflow-version-id",
            "schedule": {"type": "cron", "cron": "0 6 * * *", "active": True},
            "next_run_at": "2026-06-17T06:00:00Z",
            "updated_at": "2026-06-16T13:00:00Z",
        }
    ]
    workflow_version = {
        "id": "workflow-version-id",
        "workflow_id": "workflow-id",
        "step_graph": {
            "schema_version": 1,
            "engine": "prefect",
            "nodes": [
                {
                    "node_key": "normalize",
                    "name": "normalize",
                    "function_id": "function-id",
                    "upstream_node_keys": [],
                }
            ],
        },
    }
    endpoints = [
        {
            "id": "endpoint-id",
            "project_id": "project-id",
            "project_name": "energy",
            "name": "forecast",
            "method": "POST",
            "path": "/forecast",
            "auth": "workspace",
            "mode": "async",
            "target_type": "workflow",
            "target_id": "workflow-id",
            "enabled": True,
            "url_path": "/e/energy-workspace/energy/forecast",
            "updated_at": "2026-06-16T13:30:00Z",
        }
    ]
    run = {
        "id": "run-id",
        "target_type": "workflow",
        "status": "succeeded",
        "execution_backend": "prefect_cloud_run_service",
        "parameters": {"site_id": "site-001"},
        "result": {"ok": True},
        "created_at": "2026-06-16T14:00:00Z",
        "finished_at": "2026-06-16T14:01:00Z",
    }
    events = (
        run_events
        if run_events is not None
        else [
            {
                "id": "event-id",
                "stage": "dispatch",
                "status": "completed",
                "message": "Accepted run request.",
                "created_at": "2026-06-16T14:00:01Z",
            }
        ]
    )
    steps = [
        {
            "id": "step-id",
            "name": "load_weather",
            "status": "succeeded",
            "attempt": 1,
            "started_at": "2026-06-16T14:00:05Z",
            "finished_at": "2026-06-16T14:00:20Z",
            "error": None,
        }
    ]
    # One line inside each of the two windows above, so the timeline has to interleave
    # them rather than append them: log, event, log, step is the wrong order to show.
    log_entries = (
        log_entries
        if log_entries is not None
        else [
            {"timestamp": "2026-06-16T14:00:02Z", "severity": "INFO", "message": "Fetching curves."},
            {"timestamp": "2026-06-16T14:00:07Z", "severity": "INFO", "message": "Wrote 96 rows."},
        ]
    )

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            parsed = urlparse(self.path)
            query = parse_qs(parsed.query)
            seen_requests.append((parsed.path, query, self.headers.get("Authorization")))

            payload: Any
            if parsed.path == "/projects":
                payload = projects
            elif parsed.path == "/projects/project-id/functions":
                payload = functions
            elif parsed.path in {"/workflows", "/projects/project-id/workflows"}:
                payload = workflows
            elif parsed.path in {"/endpoints", "/projects/project-id/endpoints"}:
                payload = endpoints
            elif parsed.path == "/workflows/workflow-id/versions/workflow-version-id":
                payload = workflow_version
            elif parsed.path == "/runs":
                # Mirrors the real API: it filters on target_id and has no
                # workflow_id parameter at all.
                payload = [run] if query.get("target_id") == ["workflow-id"] else []
            elif parsed.path == "/runs/run-id":
                payload = run
            elif parsed.path == "/runs/run-id/events":
                payload = events
            elif parsed.path == "/runs/run-id/steps":
                payload = steps
            elif parsed.path == "/runs/run-id/logs":
                payload = {"run_id": "run-id", "source": "prefect", "entries": log_entries}
            else:
                self.send_response(404)
                self.end_headers()
                return

            body = json.dumps(payload).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: Any) -> None:
            return None

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}", seen_requests
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


def test_tui_end_to_end_against_local_rebase_api() -> None:
    async def scenario() -> None:
        with local_rebase_api() as (api_url, seen_requests):
            client = Client(api_key="rbw_test", api_url=api_url)
            app = RebaseTuiApp(data=RebaseTuiData(client, project="energy", limit=5))

            async with app.run_test(size=(140, 42)) as pilot:
                await pilot.pause(0.2)

                projects = app.query_one("#projects-table", DataTable)
                assert projects.row_count == 1
                # Project, Functions, Workflows, Cron jobs, Endpoints. The Last run,
                # Next run and Created columns follow; their values depend on when
                # this ran, so the counts are what is asserted.
                assert [str(cell) for cell in projects.get_row_at(0)][:5] == ["energy", "1", "1", "1", "1"]
                assert app.query_one("#workspace-view").styles.display == "block"
                assert app.query_one("#project-view").styles.display == "none"
                projects.focus()
                projects.move_cursor(row=0)
                await pilot.press("enter")
                await pilot.pause(0.2)

                assert app.title.endswith("/ energy")
                assert app.query_one("#workspace-view").styles.display == "none"
                assert app.query_one("#project-view").styles.display == "block"

                # The step graph came off the workflow version and named the function's
                # caller. Columns are Name, Origin, Workflow, Step.
                functions = app.query_one("#functions-table", DataTable)
                assert [str(cell) for cell in functions.get_row_at(0)][:4] == [
                    "normalize",
                    tui_module.ORIGIN_DEPLOYED,
                    "forecast",
                    "normalize",
                ]

                workflows = app.query_one("#workflows-table", DataTable)
                assert workflows.row_count == 1
                workflows.focus()
                workflows.move_cursor(row=0)
                await pilot.press("enter")
                await pilot.pause(0.2)

                runs = app.query_one("#runs-table", DataTable)
                assert runs.row_count == 1
                runs.focus()
                runs.move_cursor(row=0)
                await pilot.press("enter")
                await pilot.pause(0.2)

                # Activity: the event, both log lines and the step, interleaved by time.
                timeline = app.query_one("#timeline-table", DataTable)
                assert [str(timeline.get_cell_at(Coordinate(row, 1))) for row in range(4)] == [
                    "event",
                    "log",
                    "step",
                    "log",
                ]
                assert str(timeline.get_cell_at(Coordinate(1, 5))).strip() == "Fetching curves."

                # `l` narrows to the log output and its events; `l` again goes back.
                await pilot.press("l")
                await pilot.pause(0.3)
                assert timeline.row_count == 3  # the event and the two log lines
                await pilot.press("l")
                await pilot.pause(0.3)
                assert timeline.row_count == 4

                await pilot.press("b", "b", "b")
                await pilot.pause(0.2)
                assert app.query_one("#workspace-view").styles.display == "block"
                assert app.query_one("#project-view").styles.display == "none"

            assert ("/projects", {}, "Bearer rbw_test") in seen_requests
            assert any(path == "/projects/project-id/functions" for path, _, _ in seen_requests)
            assert any(path == "/projects/project-id/workflows" for path, _, _ in seen_requests)
            # The overview counts functions workspace-wide, in one request; only an opened
            # project is fetched per project. This used to be one request per project.
            assert any(path == "/functions" for path, _, _ in seen_requests)
            # The overview counts workflows workspace-wide; only an opened project is fetched per project.
            assert any(path == "/workflows" for path, _, _ in seen_requests)
            assert any(
                path == "/runs"
                and query.get("target_id") == ["workflow-id"]
                and query.get("target_type") == ["workflow"]
                and query.get("limit") == ["5"]
                for path, query, _ in seen_requests
            )
            assert any(path == "/workflows/workflow-id/versions/workflow-version-id" for path, _, _ in seen_requests)
            assert any(path == "/runs/run-id/events" for path, _, _ in seen_requests)
            assert any(path == "/runs/run-id/steps" for path, _, _ in seen_requests)

    asyncio.run(scenario())


def test_tui_log_rows_stay_flush_and_keep_severity_out_of_status() -> None:
    """A log line sits flush with the messages around it, and its severity is not a status.

    The Status column means lifecycle for an event and outcome for a step; `INFO` is neither
    and only ever repeated the `log` type beside it, so a routine severity says nothing and
    anything worth naming leads the message. The Stage cell stays empty rather than holding
    an indent, which only ever rendered as invisible whitespace.
    """

    async def scenario() -> None:
        entries = [
            {"timestamp": "2026-06-16T14:00:02Z", "severity": "INFO", "message": "Fetching curves."},
            {"timestamp": "2026-06-16T14:00:07Z", "severity": "WARNING", "message": "Retrying once."},
        ]
        with local_rebase_api(entries) as (api_url, _seen_requests):
            client = Client(api_key="rbw_test", api_url=api_url)
            app = RebaseTuiApp(data=RebaseTuiData(client, project="energy", limit=5))

            async with app.run_test(size=(140, 42)) as pilot:
                await pilot.pause(0.2)
                projects = app.query_one("#projects-table", DataTable)
                projects.focus()
                projects.move_cursor(row=0)
                await pilot.press("enter")
                await pilot.pause(0.2)
                workflows = app.query_one("#workflows-table", DataTable)
                workflows.focus()
                workflows.move_cursor(row=0)
                await pilot.press("enter")
                await pilot.pause(0.2)
                runs = app.query_one("#runs-table", DataTable)
                runs.focus()
                runs.move_cursor(row=0)
                await pilot.press("enter")
                await pilot.pause(0.2)

                # Rows are event, log, step, log. Columns are Time, Type, Scope,
                # Item, Status and Summary.
                timeline = app.query_one("#timeline-table", DataTable)
                kinds = [str(timeline.get_cell_at(Coordinate(row, 1))) for row in range(4)]
                assert kinds == ["event", "log", "step", "log"]

                # Left flush with the messages around it, and no stray indent in the Item
                # cell either — that only ever rendered as invisible whitespace.
                info_message = str(timeline.get_cell_at(Coordinate(1, 5)))
                assert info_message == "Fetching curves."
                assert str(timeline.get_cell_at(Coordinate(1, 3))) == ""

                # A routine severity says nothing; the event above it still owns Status.
                assert str(timeline.get_cell_at(Coordinate(1, 4))) == ""
                assert str(timeline.get_cell_at(Coordinate(0, 4))) == "completed"

                # A level worth noticing still names itself, on the line rather than in
                # the Status column.
                warning_message = str(timeline.get_cell_at(Coordinate(3, 5)))
                assert warning_message.strip() == "WARNING Retrying once."
                assert str(timeline.get_cell_at(Coordinate(3, 4))) == ""

    asyncio.run(scenario())


def test_tui_does_not_show_a_finished_run_as_still_running() -> None:
    """A stage left open in `running` must not read as live once the run is over.

    The platform used to open stages it never closed — `step-graph` on every workflow run —
    so a succeeded run kept a row claiming work was in flight, in the brightest green on the
    screen. That is fixed at the source, but events already written keep their status
    forever, so the timeline de-emphasises them rather than trusting the stream.
    """

    async def scenario() -> None:
        events = [
            {
                "id": "event-1",
                "stage": "dispatch",
                "status": "completed",
                "message": "Accepted run request.",
                "created_at": "2026-06-16T14:00:01Z",
            },
            {
                "id": "event-2",
                "stage": "step-graph",
                "status": "running",
                "message": "Materializing workflow steps.",
                "created_at": "2026-06-16T14:00:03Z",
            },
        ]
        with local_rebase_api(None, events) as (api_url, _seen_requests):
            client = Client(api_key="rbw_test", api_url=api_url)
            app = RebaseTuiApp(data=RebaseTuiData(client, project="energy", limit=5))

            async with app.run_test(size=(140, 42)) as pilot:
                await pilot.pause(0.2)
                projects = app.query_one("#projects-table", DataTable)
                projects.focus()
                projects.move_cursor(row=0)
                await pilot.press("enter")
                await pilot.pause(0.2)
                workflows = app.query_one("#workflows-table", DataTable)
                workflows.focus()
                workflows.move_cursor(row=0)
                await pilot.press("enter")
                await pilot.pause(0.2)
                runs = app.query_one("#runs-table", DataTable)
                runs.focus()
                runs.move_cursor(row=0)
                await pilot.press("enter")
                await pilot.pause(0.2)

                # Located by stage rather than by index: the run's log lines interleave with
                # its events by timestamp, so the row numbers depend on the fixture's clock.
                timeline = app.query_one("#timeline-table", DataTable)
                rows = {str(timeline.get_cell_at(Coordinate(row, 3))): row for row in range(timeline.row_count)}
                assert {"dispatch", "step-graph"} <= rows.keys()

                # The word is still what the API sent — inventing a status would be worse —
                # but it is muted rather than styled as a live state.
                stale = timeline.get_cell_at(Coordinate(rows["step-graph"], 4))
                assert str(stale) == "running"
                assert stale.style == BRAND_MEDIUM_GRAY
                assert stale.style != status_style("running")

                # A genuinely terminal status is untouched.
                closed = timeline.get_cell_at(Coordinate(rows["dispatch"], 4))
                assert str(closed) == "completed"
                assert closed.style == status_style("completed")

    asyncio.run(scenario())


def test_tui_workspace_title_opens_switcher_and_changes_profile(monkeypatch, tmp_path) -> None:
    async def scenario() -> None:
        with local_rebase_api() as (api_url, _seen_requests):
            config_path = tmp_path / "config.json"
            config_path.write_text(
                json.dumps(
                    {
                        "default_profile": "prod",
                        "profiles": {
                            "dev": {
                                "api_key": "rbw_dev",
                                "api_url": api_url,
                                "workspace_id": "workspace-dev",
                                "workspace_name": "Development",
                            },
                            "prod": {
                                "api_key": "rbw_prod",
                                "api_url": api_url,
                                "workspace_id": "workspace-prod",
                                "workspace_name": "Production",
                            },
                        },
                    }
                ),
                encoding="utf-8",
            )
            monkeypatch.setenv("REBASE_CONFIG_PATH", str(config_path))

            app = RebaseTuiApp(limit=5)

            async with app.run_test(size=(140, 42)) as pilot:
                await pilot.pause(0.2)

                assert app.title == "Rebase TUI - Workspace: Production (dev)"
                header = app.query_one(Header)
                assert header.size.height == 1
                assert header.tall is False

                assert await pilot.click("HeaderTitle")
                await pilot.pause(0.2)

                profiles = app.query_one("#workspace-profiles-table", DataTable)
                assert app.query_one("#workspace-switcher-view").styles.display == "block"
                assert profiles.row_count == 2
                assert header.size.height == 1
                assert header.tall is False

                profiles.focus()
                profiles.move_cursor(row=0)
                await pilot.press("enter")
                await pilot.pause(0.3)

                updated_config = json.loads(config_path.read_text(encoding="utf-8"))
                assert updated_config["default_profile"] == "dev"
                assert app.title == "Rebase TUI - Workspace: Development (dev)"
                assert app.query_one("#workspace-view").styles.display == "block"
                assert app.query_one("#projects-table", DataTable).row_count == 1

    asyncio.run(scenario())


def test_tui_w_opens_switcher_and_changes_workspace(monkeypatch) -> None:
    async def scenario() -> None:
        config_module.write_profile(
            api_key="rbw_dev",
            profile="dev",
            api_url="https://api.example.com",
            workspace={"id": "workspace-dev", "name": "Development"},
        )
        config_module.write_profile(
            api_key="rbw_prod",
            profile="prod",
            api_url="https://api.example.com",
            workspace={"id": "workspace-prod", "name": "Production"},
        )
        monkeypatch.setattr(tui_module, "Client", lambda **_kwargs: FakeClient())

        app = RebaseTuiApp(data=fake_tui_data(FakeClient(), limit=5))

        async with app.run_test(size=(140, 42)) as pilot:
            await pilot.pause(0.2)
            assert app.title == "Rebase TUI - Workspace: Production (dev)"

            await pilot.press("w")
            await pilot.pause(0.1)

            profiles = app.query_one("#workspace-profiles-table", DataTable)
            assert app.current_view == "workspace-switcher"
            assert profiles.has_focus
            assert profiles.row_count == 2

            profiles.move_cursor(row=0)
            await pilot.press("enter")
            await pilot.pause(0.2)

            assert config_module.selected_profile_name() == "dev"
            assert app.title == "Rebase TUI - Workspace: Development (dev)"
            assert app.current_view == "workspace"

    asyncio.run(scenario())


def test_tui_shift_arrows_mark_a_range_and_plain_movement_drops_it() -> None:
    async def scenario() -> None:
        app = RebaseTuiApp(data=fake_tui_data(FakeClient(), limit=5))

        async with app.run_test(size=(140, 42)) as pilot:
            await pilot.pause(0.2)
            projects = app.query_one("#projects-table", SelectableDataTable)
            projects.focus()
            projects.move_cursor(row=0)
            await pilot.pause(0.1)
            first_column = list(projects.columns)[0]

            await pilot.press("shift+down")
            await pilot.pause(0.1)
            assert projects.marked_keys == ["project-id", "other-project-id"]
            assert app.title.endswith("2 marked")
            marked_cell = projects.get_cell("project-id", first_column)
            assert MARK_STYLE in [span.style for span in marked_cell.spans]

            # Shrinking back onto the anchor leaves just the anchor row marked.
            await pilot.press("shift+up")
            await pilot.pause(0.1)
            assert projects.marked_keys == ["project-id"]

            await pilot.press("down")
            await pilot.pause(0.1)
            assert projects.marked_keys == []
            assert "marked" not in app.title
            assert projects.get_cell("project-id", first_column) == "energy"

    asyncio.run(scenario())


def test_tui_escape_goes_back_like_b_but_clears_marks_first() -> None:
    async def scenario() -> None:
        app = RebaseTuiApp(data=fake_tui_data(FakeClient(), limit=5))

        async with app.run_test(size=(140, 42)) as pilot:
            await pilot.pause(0.2)
            projects = app.query_one("#projects-table", SelectableDataTable)
            projects.focus()
            projects.move_cursor(row=0)
            await pilot.press("enter")
            await pilot.pause(0.2)
            assert app.query_one("#project-view").styles.display == "block"

            # With rows marked, escape spends itself on the marks and stays put.
            workflows = app.query_one("#workflows-table", SelectableDataTable)
            workflows.focus()
            workflows.move_cursor(row=0)
            await pilot.press("shift+down")
            await pilot.pause(0.1)
            assert workflows.marked_keys == ["workflow-id"]
            await pilot.press("escape")
            await pilot.pause(0.1)
            assert workflows.marked_keys == []
            assert app.query_one("#project-view").styles.display == "block"

            # With nothing marked it walks back, exactly like b.
            await pilot.press("escape")
            await pilot.pause(0.2)
            assert app.query_one("#workspace-view").styles.display == "block"
            assert app.query_one("#project-view").styles.display == "none"

    asyncio.run(scenario())


def test_tui_c_copies_the_highlighted_rows_identifier() -> None:
    class BucketClient(FakeClient):
        def list_buckets(self) -> list[dict[str, Any]]:
            # No id: the API identifies buckets by name, and the copy should say so.
            return [{"name": "raw-data", "uri": "gs://rb-raw-data"}]

    async def scenario() -> None:
        app = RebaseTuiApp(data=fake_tui_data(BucketClient(), limit=5))

        async with app.run_test(size=(140, 42)) as pilot:
            await pilot.pause(0.2)
            projects = app.query_one("#projects-table", SelectableDataTable)
            projects.focus()
            projects.move_cursor(row=0)
            await pilot.press("c")
            await pilot.pause(0.1)
            assert app.clipboard == "project_id=project-id"

            # Marked rows go together, one id per line.
            await pilot.press("shift+down")
            await pilot.press("c")
            await pilot.pause(0.1)
            assert app.clipboard == "project_id=project-id\nproject_id=other-project-id"
            await pilot.press("escape")

            # A resource the API identifies by name says so, rather than passing the
            # name off as an id.
            app.query_one("#workspace-resource-tabs", TabbedContent).active = "buckets-resource-tab"
            await pilot.pause(0.1)
            buckets = app.query_one("#buckets-table", SelectableDataTable)
            buckets.focus()
            buckets.move_cursor(row=0)
            await pilot.press("c")
            await pilot.pause(0.1)
            assert app.clipboard == "bucket_name=raw-data"

            # Deeper rows copy their own ids: the workflow, then its run.
            app.query_one("#workspace-resource-tabs", TabbedContent).active = "projects-resource-tab"
            await pilot.pause(0.1)
            projects.focus()
            projects.move_cursor(row=0)
            await pilot.press("enter")
            await pilot.pause(0.2)
            workflows = app.query_one("#workflows-table", SelectableDataTable)
            workflows.focus()
            workflows.move_cursor(row=0)
            await pilot.press("c")
            await pilot.pause(0.1)
            assert app.clipboard == "workflow_id=workflow-id"

            await pilot.press("enter")
            await pilot.pause(0.2)
            runs = app.query_one("#runs-table", DataTable)
            runs.focus()
            runs.move_cursor(row=0)
            await pilot.press("c")
            await pilot.pause(0.1)
            assert app.clipboard == "run_id=run-id"

            # Timeline rows are keyed by position, so `c` reaches for the record's own
            # id — and a log line, which has none, answers with the run it belongs to.
            await pilot.press("enter")
            await pilot.pause(0.2)
            timeline = app.query_one("#timeline-table", DataTable)
            timeline.focus()
            copied = []
            for row in range(3):
                timeline.move_cursor(row=row)
                await pilot.press("c")
                await pilot.pause(0.1)
                copied.append(app.clipboard)
            assert copied == ["event_id=event-id", "run_id=run-id", "step_id=step-id"]

    asyncio.run(scenario())


def test_tui_delete_of_one_project_requires_its_name_typed_back() -> None:
    async def scenario() -> None:
        client = FakeClient()
        app = RebaseTuiApp(data=fake_tui_data(client, limit=5))

        async with app.run_test(size=(140, 42)) as pilot:
            await pilot.pause(0.2)
            projects = app.query_one("#projects-table", SelectableDataTable)
            projects.focus()
            projects.move_cursor(row=1)
            await pilot.pause(0.1)

            await pilot.press("d")
            await pilot.pause(0.1)
            screen = app.screen
            assert isinstance(screen, DeleteConfirmScreen)
            assert screen.required_phrase == "trading"

            # The other project's name is still the wrong answer.
            screen.query_one("#delete-input", Input).value = "energy"
            await pilot.press("enter")
            await pilot.pause(0.1)
            assert isinstance(app.screen, DeleteConfirmScreen)
            assert client.deleted == []

            screen.query_one("#delete-input", Input).value = "trading"
            await pilot.press("enter")
            await pilot.pause(0.3)
            assert not isinstance(app.screen, DeleteConfirmScreen)
            assert client.deleted == [("project", "other-project-id", True)]
            assert projects.row_count == 1

    asyncio.run(scenario())


def test_tui_delete_of_marked_projects_requires_the_word_delete() -> None:
    async def scenario() -> None:
        client = FakeClient()
        app = RebaseTuiApp(data=fake_tui_data(client, limit=5))

        async with app.run_test(size=(140, 42)) as pilot:
            await pilot.pause(0.2)
            projects = app.query_one("#projects-table", SelectableDataTable)
            projects.focus()
            projects.move_cursor(row=0)
            await pilot.pause(0.1)

            await pilot.press("shift+down")
            await pilot.press("d")
            await pilot.pause(0.1)
            screen = app.screen
            assert isinstance(screen, DeleteConfirmScreen)
            assert screen.required_phrase == "delete"

            screen.query_one("#delete-input", Input).value = "energy"
            await pilot.press("enter")
            await pilot.pause(0.1)
            assert isinstance(app.screen, DeleteConfirmScreen)
            assert client.deleted == []

            screen.query_one("#delete-input", Input).value = "DELETE"
            await pilot.press("enter")
            await pilot.pause(0.3)
            # Order is not asserted: the deletes are issued concurrently.
            assert sorted((kind, object_id) for kind, object_id, _ in client.deleted) == [
                ("project", "other-project-id"),
                ("project", "project-id"),
            ]
            assert all(force for *_, force in client.deleted)
            assert projects.row_count == 0

    asyncio.run(scenario())


def test_tui_deletes_marked_projects_in_a_single_batch_call() -> None:
    """Projects have a batch route; the TUI must use it rather than one call each."""

    async def scenario() -> None:
        client = FakeClient()
        app = RebaseTuiApp(data=fake_tui_data(client, limit=5))

        async with app.run_test(size=(140, 42)) as pilot:
            await pilot.pause(0.2)
            projects = app.query_one("#projects-table", SelectableDataTable)
            projects.focus()
            projects.move_cursor(row=0)
            await pilot.pause(0.1)

            await pilot.press("shift+down")
            await pilot.press("d")
            await pilot.pause(0.1)
            screen = app.screen
            assert isinstance(screen, DeleteConfirmScreen)
            screen.query_one("#delete-input", Input).value = "delete"
            await pilot.press("enter")
            await pilot.pause(0.3)

            assert client.batch_calls == [["project-id", "other-project-id"]]
            assert projects.row_count == 0

    asyncio.run(scenario())


def test_tui_marked_rows_leave_the_table_before_the_deletes_return() -> None:
    """The rows should not sit there for a round trip each while the API is asked."""

    class SlowClient(FakeClient):
        def __init__(self) -> None:
            super().__init__()
            self.release = threading.Event()

        def delete_project(self, project_id: str, *, force: bool = False) -> None:
            self.release.wait(timeout=5)
            super().delete_project(project_id, force=force)

    async def scenario() -> None:
        client = SlowClient()
        app = RebaseTuiApp(data=fake_tui_data(client, limit=5))

        async with app.run_test(size=(140, 42)) as pilot:
            await pilot.pause(0.2)
            projects = app.query_one("#projects-table", SelectableDataTable)
            projects.focus()
            projects.move_cursor(row=0)
            await pilot.pause(0.1)

            await pilot.press("shift+down")
            await pilot.press("d")
            await pilot.pause(0.1)
            screen = app.screen
            assert isinstance(screen, DeleteConfirmScreen)
            screen.query_one("#delete-input", Input).value = "delete"
            await pilot.press("enter")
            await pilot.pause(0.1)

            # Still blocked in delete_project, yet the table is already clear.
            assert client.deleted == []
            assert projects.row_count == 0
            assert app.workspace_overview is not None
            assert app.workspace_overview.projects == []

            client.release.set()
            await pilot.pause(0.3)
            assert len(client.deleted) == 2
            assert projects.row_count == 0

    asyncio.run(scenario())


def test_tui_failed_delete_puts_the_row_back() -> None:
    """Removing rows up front is a bet; a lost bet has to be visibly undone."""

    class FailingClient(FakeClient):
        def delete_project(self, project_id: str, *, force: bool = False) -> None:
            if project_id == "other-project-id":
                raise RebaseWorkflowError("500 boom")
            super().delete_project(project_id, force=force)

    async def scenario() -> None:
        client = FailingClient()
        app = RebaseTuiApp(data=fake_tui_data(client, limit=5))

        async with app.run_test(size=(140, 42)) as pilot:
            await pilot.pause(0.2)
            projects = app.query_one("#projects-table", SelectableDataTable)
            projects.focus()
            projects.move_cursor(row=0)
            await pilot.pause(0.1)

            await pilot.press("shift+down")
            await pilot.press("d")
            await pilot.pause(0.1)
            screen = app.screen
            assert isinstance(screen, DeleteConfirmScreen)
            screen.query_one("#delete-input", Input).value = "delete"
            await pilot.press("enter")
            await pilot.pause(0.5)

            # The one that failed is back on screen; the one that worked stays gone.
            assert [str(cell) for cell in projects.get_row_at(0)][0] == "trading"
            assert projects.row_count == 1

    asyncio.run(scenario())


def test_tui_delete_follows_the_active_target_tab() -> None:
    async def scenario() -> None:
        client = FakeClient()
        app = RebaseTuiApp(data=fake_tui_data(client, project="energy", limit=5))

        async with app.run_test(size=(140, 42)) as pilot:
            await pilot.pause(0.2)
            projects = app.query_one("#projects-table", SelectableDataTable)
            projects.focus()
            projects.move_cursor(row=0)
            await pilot.press("enter")
            await pilot.pause(0.3)

            # Focus is still on the (now hidden) projects table: the active tab decides.
            await pilot.press("d")
            await pilot.pause(0.1)
            screen = app.screen
            assert isinstance(screen, DeleteConfirmScreen)
            assert screen.kind == "workflow"
            assert screen.required_phrase == "forecast"

            await pilot.press("escape")
            await pilot.pause(0.1)
            assert not isinstance(app.screen, DeleteConfirmScreen)
            assert client.deleted == []

            app.query_one("#target-tabs", TabbedContent).active = "workflows-tab"
            await pilot.pause(0.1)
            await pilot.press("d")
            await pilot.pause(0.1)
            screen = app.screen
            assert isinstance(screen, DeleteConfirmScreen)
            screen.query_one("#delete-input", Input).value = "forecast"
            await pilot.press("enter")
            await pilot.pause(0.3)

            assert client.deleted == [("workflow", "workflow-id", True)]
            assert app.query_one("#workflows-table", SelectableDataTable).row_count == 0

    asyncio.run(scenario())


def test_tui_text_selection_adjusts_with_shift_arrows_and_copies_with_cmd_c() -> None:
    async def scenario() -> None:
        app = RebaseTuiApp(data=fake_tui_data(EmptyWorkspaceClient(), limit=5))

        async with app.run_test(size=(140, 42)) as pilot:
            await pilot.pause(0.3)

            # Drag a selection across the first line of the empty-workspace notice. Pilot
            # has no drag helper, so the mid-drag move goes through the same private hook
            # its click helpers use.
            detail = app.query_one("#workspace-empty", Static)
            await pilot.mouse_down(detail, offset=(0, 0))
            await pilot._post_mouse_events([MouseMove], widget=detail, offset=(7, 0), button=1)
            await pilot.mouse_up(detail, offset=(7, 0))
            await pilot.pause(0.1)

            dragged = app.screen.get_selected_text()
            assert dragged is not None
            assert "No projects in workspace"[: len(dragged)] == dragged

            await pilot.press("shift+right")
            await pilot.pause(0.1)
            grown = app.screen.get_selected_text()
            assert grown is not None and grown == "No projects in workspace"[: len(dragged) + 1]

            await pilot.press("shift+left", "shift+left")
            await pilot.pause(0.1)
            shrunk = app.screen.get_selected_text()
            assert shrunk is not None and len(shrunk) == len(dragged) - 1

            # super+c is macOS cmd+c; Textual binds it next to ctrl+c on every screen.
            await pilot.press("super+c")
            await pilot.pause(0.1)
            assert app.clipboard == shrunk

    asyncio.run(scenario())


def test_tui_selection_adjustment_stops_at_the_line_edges() -> None:
    async def scenario() -> None:
        app = RebaseTuiApp(data=fake_tui_data(EmptyWorkspaceClient(), limit=5))

        async with app.run_test(size=(140, 42)) as pilot:
            await pilot.pause(0.3)

            detail = app.query_one("#workspace-empty", Static)
            first_line = str(detail.content).splitlines()[0]
            app.screen.selections = {detail: Selection(Offset(0, 0), Offset(len(first_line), 0))}
            await pilot.pause(0.1)

            # Already at the end of the line: growing further is a no-op, not a wrap.
            await pilot.press("shift+right")
            await pilot.pause(0.1)
            assert app.screen.get_selected_text() == first_line

            # And it can be pulled back to empty but never past the start.
            for _ in range(len(first_line) + 3):
                await pilot.press("shift+left")
            await pilot.pause(0.1)
            assert app.screen.get_selected_text() == ""

    asyncio.run(scenario())


def test_tui_s_hands_the_mouse_to_the_terminal_and_takes_it_back() -> None:
    async def scenario() -> None:
        app = RebaseTuiApp(data=fake_tui_data(FakeClient(), limit=5))

        async with app.run_test(size=(140, 42)) as pilot:
            await pilot.pause(0.2)
            # run_test's headless driver has no mouse hooks; record against the real names.
            calls: list[str] = []
            app._driver._disable_mouse_support = lambda: calls.append("disable")  # type: ignore[union-attr]
            app._driver._enable_mouse_support = lambda: calls.append("enable")  # type: ignore[union-attr]

            detail = app.query_one("#projects-table", SelectableDataTable)
            app.screen.selections = {detail: Selection(Offset(0, 0), Offset(4, 0))}

            await pilot.press("s")
            await pilot.pause(0.1)
            assert calls == ["disable"]
            assert app._terminal_select is True
            assert "select text (mouse off" in app.title
            # The app's own highlight would be stale once the terminal owns the mouse.
            assert app.screen.selections == {}

            await pilot.press("s")
            await pilot.pause(0.1)
            assert calls == ["disable", "enable"]
            assert app._terminal_select is False
            assert "select text" not in app.title

    asyncio.run(scenario())


def test_tui_terminal_select_relies_on_driver_hooks_that_still_exist() -> None:
    """The toggle reaches into Textual's driver; fail loudly if those hooks are renamed."""
    from textual.drivers.linux_driver import LinuxDriver

    assert callable(LinuxDriver._enable_mouse_support)
    assert callable(LinuxDriver._disable_mouse_support)


def _seed_open_source(
    monkeypatch, tmp_path, *, declaring_files: int = 1, project_name: str = "energy"
) -> tuple[list[list[str]], list[list[str]]]:
    """Point the active workspace at a directory declaring `project_name`.

    Returns (detached, foreground) recorders for the two launch paths.
    """
    config_path = tmp_path / "config.json"
    monkeypatch.setenv("REBASE_CONFIG_PATH", str(config_path))
    code = tmp_path / "code"
    code.mkdir()
    for index in range(declaring_files):
        (code / f"deploy_{index}.py").write_text(
            f'import rebase as rb\n\nPROJECT_NAME = "{project_name}"\n\nproject = rb.project(PROJECT_NAME)\n',
            encoding="utf-8",
        )
    config_path.write_text(
        json.dumps(
            {
                "default_profile": "default",
                "profiles": {"default": {"api_key": "rbw_test", "workspace_id": "ws"}},
                "workspaces": {"ws": {"search_paths": [str(code)]}},
            }
        ),
        encoding="utf-8",
    )

    # This suite runs inside a git repository, so the startup bootstrap would add the
    # toolkit's own tree as a second search path and find declarations in its tests.
    # Tests that are about the bootstrap re-patch this after calling the helper.
    monkeypatch.setattr("rebase.tui.git_toplevel", lambda cwd: None)

    detached: list[list[str]] = []
    foreground: list[list[str]] = []
    monkeypatch.setattr("rebase.tui.spawn_detached", lambda argv: detached.append(list(argv)))
    monkeypatch.setattr("rebase.tui.run_foreground", lambda argv: foreground.append(list(argv)) or 0)
    monkeypatch.setattr(
        "rebase.tui.resolve_editor",
        lambda **kwargs: EditorCommand(template="fake-editor -g {path}:{line}", terminal=False, source="test"),
    )
    return detached, foreground


def test_tui_open_source_opens_the_file_that_declares_the_selected_project(monkeypatch, tmp_path) -> None:
    async def scenario() -> None:
        detached, _ = _seed_open_source(monkeypatch, tmp_path)
        client = FakeClient()
        app = RebaseTuiApp(data=fake_tui_data(client, limit=5))

        async with app.run_test(size=(140, 42)) as pilot:
            await pilot.pause(0.2)
            projects = app.query_one("#projects-table", SelectableDataTable)
            projects.focus()
            projects.move_cursor(row=0)
            await pilot.press("o")
            await pilot.pause(0.3)

        assert detached == [["fake-editor", "-g", f"{tmp_path / 'code' / 'deploy_0.py'}:5"]]

    asyncio.run(scenario())


def test_tui_open_source_works_after_drilling_into_a_project(monkeypatch, tmp_path) -> None:
    async def scenario() -> None:
        detached, _ = _seed_open_source(monkeypatch, tmp_path)
        client = FakeClient()
        app = RebaseTuiApp(data=fake_tui_data(client, limit=5))

        async with app.run_test(size=(140, 42)) as pilot:
            await pilot.pause(0.2)
            projects = app.query_one("#projects-table", SelectableDataTable)
            projects.focus()
            projects.move_cursor(row=0)
            await pilot.press("enter")
            await pilot.pause(0.3)
            assert app.query_one("#project-view").styles.display == "block"

            await pilot.press("o")
            await pilot.pause(0.3)

        assert len(detached) == 1

    asyncio.run(scenario())


def test_tui_open_source_asks_which_file_when_two_files_declare_the_project(monkeypatch, tmp_path) -> None:
    async def scenario() -> None:
        detached, _ = _seed_open_source(monkeypatch, tmp_path, declaring_files=2)
        client = FakeClient()
        app = RebaseTuiApp(data=fake_tui_data(client, limit=5))

        async with app.run_test(size=(140, 42)) as pilot:
            await pilot.pause(0.2)
            projects = app.query_one("#projects-table", SelectableDataTable)
            projects.focus()
            projects.move_cursor(row=0)
            await pilot.press("o")
            await pilot.pause(0.3)

            screen = app.screen
            assert isinstance(screen, OpenSourceChoiceScreen)
            options = screen.query_one("#open-source-options", OptionList)
            options.highlighted = 1
            await pilot.press("enter")
            await pilot.pause(0.3)

        assert detached == [["fake-editor", "-g", f"{tmp_path / 'code' / 'deploy_1.py'}:5"]]

    asyncio.run(scenario())


def test_tui_open_source_picker_escape_launches_nothing(monkeypatch, tmp_path) -> None:
    async def scenario() -> None:
        detached, _ = _seed_open_source(monkeypatch, tmp_path, declaring_files=2)
        client = FakeClient()
        app = RebaseTuiApp(data=fake_tui_data(client, limit=5))

        async with app.run_test(size=(140, 42)) as pilot:
            await pilot.pause(0.2)
            projects = app.query_one("#projects-table", SelectableDataTable)
            projects.focus()
            projects.move_cursor(row=0)
            await pilot.press("o")
            await pilot.pause(0.3)
            assert isinstance(app.screen, OpenSourceChoiceScreen)

            await pilot.press("escape")
            await pilot.pause(0.1)
            assert not isinstance(app.screen, OpenSourceChoiceScreen)

        assert detached == []

    asyncio.run(scenario())


def test_tui_open_source_warns_when_the_workspace_has_no_search_paths(monkeypatch, tmp_path) -> None:
    async def scenario() -> None:
        detached, _ = _seed_open_source(monkeypatch, tmp_path)
        config_path = tmp_path / "config.json"
        data = json.loads(config_path.read_text(encoding="utf-8"))
        data.pop("workspaces")
        config_path.write_text(json.dumps(data), encoding="utf-8")

        client = FakeClient()
        app = RebaseTuiApp(data=fake_tui_data(client, limit=5))

        async with app.run_test(size=(140, 42)) as pilot:
            await pilot.pause(0.2)
            projects = app.query_one("#projects-table", SelectableDataTable)
            projects.focus()
            projects.move_cursor(row=0)
            await pilot.press("o")
            await pilot.pause(0.3)

            # A warning must not drag the user off the list they pressed `o` on.
            assert app.query_one("#workspace-view").styles.display == "block"
            assert app.query_one("#project-view").styles.display == "none"

        assert detached == []

    asyncio.run(scenario())


def test_tui_open_source_warns_when_no_file_declares_the_project(monkeypatch, tmp_path) -> None:
    async def scenario() -> None:
        detached, _ = _seed_open_source(monkeypatch, tmp_path, declaring_files=0)
        client = FakeClient()
        app = RebaseTuiApp(data=fake_tui_data(client, limit=5))

        async with app.run_test(size=(140, 42)) as pilot:
            await pilot.pause(0.2)
            projects = app.query_one("#projects-table", SelectableDataTable)
            projects.focus()
            projects.move_cursor(row=0)
            await pilot.press("o")
            await pilot.pause(0.3)

            assert app.query_one("#workspace-view").styles.display == "block"
            assert app.query_one("#project-view").styles.display == "none"

        assert detached == []

    asyncio.run(scenario())


def test_tui_open_source_suspends_the_app_for_terminal_editors(monkeypatch, tmp_path) -> None:
    """A terminal editor draws over the TUI, so the app has to yield the terminal."""

    async def scenario() -> None:
        detached, foreground = _seed_open_source(monkeypatch, tmp_path)
        monkeypatch.setattr(
            "rebase.tui.resolve_editor",
            lambda **kwargs: EditorCommand(template="nvim +{line} {path}", terminal=True, source="test"),
        )
        suspended: list[bool] = []

        @contextmanager
        def fake_suspend(self):
            suspended.append(True)
            yield

        # The headless test driver cannot really yield the terminal, so stand in for it.
        monkeypatch.setattr(RebaseTuiApp, "suspend", fake_suspend)

        client = FakeClient()
        app = RebaseTuiApp(data=fake_tui_data(client, limit=5))

        async with app.run_test(size=(140, 42)) as pilot:
            await pilot.pause(0.2)
            projects = app.query_one("#projects-table", SelectableDataTable)
            projects.focus()
            projects.move_cursor(row=0)
            await pilot.press("o")
            await pilot.pause(0.3)

        assert suspended == [True]
        assert foreground == [["nvim", "+5", f"{tmp_path / 'code' / 'deploy_0.py'}"]]
        assert detached == []

    asyncio.run(scenario())


def test_tui_open_source_spawns_detached_when_the_driver_cannot_suspend(monkeypatch, tmp_path) -> None:
    """Headless and web drivers cannot hand over the terminal; opening must still work."""

    async def scenario() -> None:
        detached, foreground = _seed_open_source(monkeypatch, tmp_path)
        monkeypatch.setattr(
            "rebase.tui.resolve_editor",
            lambda **kwargs: EditorCommand(template="nvim +{line} {path}", terminal=True, source="test"),
        )
        client = FakeClient()
        app = RebaseTuiApp(data=fake_tui_data(client, limit=5))

        async with app.run_test(size=(140, 42)) as pilot:
            await pilot.pause(0.2)
            projects = app.query_one("#projects-table", SelectableDataTable)
            projects.focus()
            projects.move_cursor(row=0)
            await pilot.press("o")
            await pilot.pause(0.3)

        assert detached == [["nvim", "+5", f"{tmp_path / 'code' / 'deploy_0.py'}"]]
        assert foreground == []

    asyncio.run(scenario())


def test_tui_open_source_reports_a_missing_editor_without_leaving_the_project_list(monkeypatch, tmp_path) -> None:
    async def scenario() -> None:
        detached, _ = _seed_open_source(monkeypatch, tmp_path)
        monkeypatch.setattr("rebase.tui.resolve_editor", lambda **kwargs: None)
        client = FakeClient()
        app = RebaseTuiApp(data=fake_tui_data(client, limit=5))

        async with app.run_test(size=(140, 42)) as pilot:
            await pilot.pause(0.2)
            projects = app.query_one("#projects-table", SelectableDataTable)
            projects.focus()
            projects.move_cursor(row=0)
            await pilot.press("o")
            await pilot.pause(0.3)

            assert app.query_one("#workspace-view").styles.display == "block"

        assert detached == []

    asyncio.run(scenario())


def test_tui_open_source_is_ignored_in_the_workspace_switcher(monkeypatch, tmp_path) -> None:
    async def scenario() -> None:
        detached, _ = _seed_open_source(monkeypatch, tmp_path)
        client = FakeClient()
        app = RebaseTuiApp(data=fake_tui_data(client, limit=5))

        async with app.run_test(size=(140, 42)) as pilot:
            await pilot.pause(0.2)
            assert await pilot.click("HeaderTitle")
            await pilot.pause(0.2)
            assert app.query_one("#workspace-switcher-view").styles.display == "block"

            await pilot.press("o")
            await pilot.pause(0.3)
            assert not isinstance(app.screen, OpenSourceChoiceScreen)

        assert detached == []

    asyncio.run(scenario())


def test_tui_records_the_cwd_git_root_as_a_search_path_on_start(monkeypatch, tmp_path) -> None:
    async def scenario() -> None:
        _seed_open_source(monkeypatch, tmp_path)
        repo = tmp_path / "repo"
        repo.mkdir()
        monkeypatch.setattr("rebase.tui.git_toplevel", lambda cwd: repo)

        client = FakeClient()
        app = RebaseTuiApp(data=fake_tui_data(client, limit=5))
        async with app.run_test(size=(140, 42)) as pilot:
            await pilot.pause(0.3)

        stored = json.loads((tmp_path / "config.json").read_text(encoding="utf-8"))
        assert str(repo) in stored["workspaces"]["ws"]["search_paths"]

    asyncio.run(scenario())


def test_tui_does_not_record_the_home_directory_as_a_search_path(monkeypatch, tmp_path) -> None:
    """Registering $HOME would turn every lookup into a scan of everything the user owns."""

    async def scenario() -> None:
        _seed_open_source(monkeypatch, tmp_path)
        monkeypatch.setattr("rebase.tui.git_toplevel", lambda cwd: Path.home())
        before = (tmp_path / "config.json").read_text(encoding="utf-8")

        client = FakeClient()
        app = RebaseTuiApp(data=fake_tui_data(client, limit=5))
        async with app.run_test(size=(140, 42)) as pilot:
            await pilot.pause(0.3)

        assert (tmp_path / "config.json").read_text(encoding="utf-8") == before

    asyncio.run(scenario())


def test_tui_does_not_rewrite_the_config_when_the_git_root_is_already_recorded(monkeypatch, tmp_path) -> None:
    async def scenario() -> None:
        _seed_open_source(monkeypatch, tmp_path)
        code = tmp_path / "code"
        monkeypatch.setattr("rebase.tui.git_toplevel", lambda cwd: code)

        writes: list[Path] = []
        original = config_module._write_config

        def counting_write(data, resolved_path):
            writes.append(resolved_path)
            return original(data, resolved_path)

        monkeypatch.setattr(config_module, "_write_config", counting_write)

        client = FakeClient()
        app = RebaseTuiApp(data=fake_tui_data(client, limit=5))
        async with app.run_test(size=(140, 42)) as pilot:
            await pilot.pause(0.3)

        assert writes == []

    asyncio.run(scenario())


def test_tui_tables_have_no_header_until_their_rows_arrive() -> None:
    """Headers sized against header text and then resized by the data is the jump to avoid."""

    class SlowClient(FakeClient):
        def __init__(self) -> None:
            super().__init__()
            self.release = threading.Event()

        def list_projects(self) -> list[dict[str, Any]]:
            self.release.wait(timeout=5)
            return super().list_projects()

    async def scenario() -> None:
        client = SlowClient()
        app = RebaseTuiApp(data=fake_tui_data(client, limit=5))

        async with app.run_test(size=(140, 42)) as pilot:
            await pilot.pause(0.2)
            projects = app.query_one("#projects-table", SelectableDataTable)
            assert projects.row_count == 0
            assert list(projects.columns) == []

            client.release.set()
            await pilot.pause(0.4)
            assert projects.row_count == 2
            assert [str(column.label) for column in projects.columns.values()] == [
                "Project",
                "Functions",
                "Workflows",
                "Cron jobs",
                "Endpoints",
                "Status",
                "Last run",
                "Next run",
                "Created",
            ]

            # Entering a project is the same story one level down.
            projects.focus()
            projects.move_cursor(row=0)
            await pilot.press("enter")
            await pilot.pause(0.3)
            workflows = app.query_one("#workflows-table", SelectableDataTable)
            assert workflows.row_count == 1
            assert [str(column.label) for column in workflows.columns.values()][0] == "Name"

            # And leaving takes the header with it, so no stale widths greet the next project.
            await pilot.press("b")
            await pilot.pause(0.2)
            assert list(workflows.columns) == []

    asyncio.run(scenario())


def test_tui_clock_names_its_zone_and_clicking_it_changes_every_time_shown() -> None:
    async def scenario() -> None:
        app = RebaseTuiApp(data=fake_tui_data(FakeClient(), project="energy", limit=5))

        async with app.run_test(size=(140, 42)) as pilot:
            await pilot.pause(0.3)
            clock = app.query_one(RebaseClock)
            assert re.fullmatch(r"\d\d:\d\d:\d\d \S+", str(clock.render()))

            projects = app.query_one("#projects-table", SelectableDataTable)
            projects.focus()
            projects.move_cursor(row=0)
            await pilot.press("enter")
            await pilot.pause(0.3)
            workflows = app.query_one("#workflows-table", SelectableDataTable)

            assert await pilot.click(RebaseClock)
            await pilot.pause(0.2)
            screen = app.screen
            assert isinstance(screen, TimezoneChoiceScreen)

            # Tokyo: UTC+9 the year round, so the arithmetic is not a DST coin flip.
            screen.query_one("#timezone-filter", Input).value = "tokyo"
            await pilot.pause(0.1)
            await pilot.press("enter")
            await pilot.pause(0.4)

            assert app._display_timezone == ZoneInfo("Asia/Tokyo")
            assert "Asia/Tokyo" in str(clock.render()) or "JST" in str(clock.render())
            # The reload rebuilt the columns, so the key has to be read back now.
            updated_column = list(workflows.columns)[-1]
            # The API sent 2026-06-16T13:00:00Z.
            assert workflows.get_cell("workflow-id", updated_column) == "2026-06-16 22:00:00"

    asyncio.run(scenario())


def test_tui_timezone_filter_matches_names_the_way_they_are_typed() -> None:
    screen = TimezoneChoiceScreen(current="UTC")
    assert screen.zones[:2] == [TimezoneChoiceScreen.SYSTEM, "UTC"]
    assert "Europe/Stockholm" in screen.matches("stockholm")
    assert "America/New_York" in screen.matches("new york")
    assert screen.matches("not-a-zone") == []
    assert screen.matches("") == screen.zones


def test_tui_header_parts_still_exist() -> None:
    """The header is recomposed from Textual's private pieces; fail loudly if they move."""
    from textual.widgets._header import HeaderClock, HeaderIcon, HeaderTitle

    assert issubclass(RebaseClock, HeaderClock)
    assert callable(HeaderIcon) and callable(HeaderTitle)


def test_tui_project_view_opens_a_box_at_a_time() -> None:
    """Entering a project shows the target table alone; each drill-down adds one box."""

    async def scenario() -> None:
        app = RebaseTuiApp(data=fake_tui_data(FakeClient(), project="energy", limit=5))

        async with app.run_test(size=(140, 42)) as pilot:
            await pilot.pause(0.2)
            projects = app.query_one("#projects-table", SelectableDataTable)
            projects.focus()
            projects.move_cursor(row=0)
            await pilot.press("enter")
            await pilot.pause(0.3)

            # Level 0: the switcher and its table, and nothing else.
            assert app._reveal_level == 0
            for selector in ("#runs-table", "#timeline-pane"):
                assert app.query_one(selector).styles.display == "none", selector
            assert app.query_one("#target-tabs").styles.height.value == 1  # 1fr
            assert app.title.endswith("/ energy")

            workflows = app.query_one("#workflows-table", SelectableDataTable)
            workflows.focus()
            workflows.move_cursor(row=0)
            await pilot.press("enter")
            await pilot.pause(0.3)

            # Level 1: the runs, still no timeline.
            assert app._reveal_level == 1
            assert app.query_one("#runs-table").styles.display == "block"
            assert app.query_one("#timeline-pane").styles.display == "none"

            runs = app.query_one("#runs-table", DataTable)
            runs.focus()
            runs.move_cursor(row=0)
            await pilot.press("enter")
            await pilot.pause(0.3)

            # Level 2: the run's timeline.
            assert app._reveal_level == 2
            assert app.query_one("#timeline-pane").styles.display == "block"

    asyncio.run(scenario())


def test_tui_tab_walks_every_box_on_screen() -> None:
    """`tab` used to switch the top box's two tabs and leave the rest unreachable."""

    async def scenario() -> None:
        app = RebaseTuiApp(data=fake_tui_data(FakeClient(), project="energy", limit=5))

        async with app.run_test(size=(140, 42)) as pilot:
            await pilot.pause(0.2)
            projects = app.query_one("#projects-table", SelectableDataTable)
            projects.focus()
            projects.move_cursor(row=0)
            await pilot.press("enter")
            await pilot.pause(0.3)

            # With only the target table open, tab has nowhere else to go.
            assert str(app.focused.id) == "workflows-table"
            await pilot.press("tab")
            await pilot.pause(0.1)
            assert str(app.focused.id) == "workflows-table"

            # Opening a box hands it the focus: the arrows drive it without a tab first.
            await pilot.press("enter")
            await pilot.pause(0.3)
            assert str(app.focused.id) == "runs-table"
            await pilot.press("enter")
            await pilot.pause(0.3)
            assert str(app.focused.id) == "timeline-table"

            # And tab still walks all three, wrapping at the bottom.
            await pilot.press("tab")
            await pilot.pause(0.1)
            assert str(app.focused.id) == "workflows-table"
            await pilot.press("tab")
            await pilot.pause(0.1)
            assert str(app.focused.id) == "runs-table"

            # Switching the target tab takes the focus with it, so tab keeps its place.
            app.query_one("#workflows-table", SelectableDataTable).focus()
            await pilot.press("right")
            await pilot.pause(0.1)
            assert str(app.focused.id) == "functions-table"
            await pilot.press("tab")
            await pilot.pause(0.1)
            assert str(app.focused.id) == "runs-table"

            # Closing a box moves the focus off it rather than stranding it off screen.
            app.query_one("#timeline-table", DataTable).focus()
            await pilot.pause(0.1)
            await pilot.press("b")
            await pilot.pause(0.3)
            assert str(app.focused.id) == "runs-table"

    asyncio.run(scenario())


def test_tui_plus_and_minus_resize_the_focused_box() -> None:
    async def scenario() -> None:
        app = RebaseTuiApp(data=fake_tui_data(FakeClient(), project="energy", limit=5))

        async with app.run_test(size=(140, 42)) as pilot:
            await pilot.pause(0.2)
            projects = app.query_one("#projects-table", SelectableDataTable)
            projects.focus()
            projects.move_cursor(row=0)
            await pilot.press("enter")
            await pilot.pause(0.3)

            # One box open: there is no second box to take the rows from.
            await pilot.press("+")
            await pilot.pause(0.1)
            assert any("Nothing to resize yet" in n.message for n in app._notifications)

            await pilot.press("enter")
            await pilot.pause(0.3)
            tabs = app.query_one("#target-tabs")
            runs = app.query_one("#runs-table")

            # The focus is on the bottom box, which grows by taking rows off the one above.
            assert str(app.focused.id) == "runs-table"
            before = runs.size.height
            await pilot.press("+")
            await pilot.pause(0.1)
            assert runs.size.height == before + 2
            await pilot.press("-", "-")
            await pilot.pause(0.1)
            assert runs.size.height == before - 2

            # On a fixed box, + grows the box itself.
            app.query_one("#workflows-table", SelectableDataTable).focus()
            await pilot.pause(0.1)
            tall = tabs.size.height
            await pilot.press("+")
            await pilot.pause(0.1)
            assert tabs.size.height == tall + 2

            # 0 puts every box back where it started.
            await pilot.press("0")
            await pilot.pause(0.1)
            assert runs.size.height == before

    asyncio.run(scenario())


def test_tui_a_squeezed_box_keeps_only_its_header() -> None:
    async def scenario() -> None:
        app = RebaseTuiApp(data=fake_tui_data(FakeClient(), project="energy", limit=5))

        async with app.run_test(size=(140, 42)) as pilot:
            await pilot.pause(0.2)
            projects = app.query_one("#projects-table", SelectableDataTable)
            projects.focus()
            projects.move_cursor(row=0)
            await pilot.press("enter")
            await pilot.pause(0.3)
            await pilot.press("enter")
            await pilot.pause(0.3)
            await pilot.press("enter")
            await pilot.pause(0.3)

            app.query_one("#workflows-table", SelectableDataTable).focus()
            await pilot.pause(0.1)
            assert app._reveal_level == 2
            await pilot.press(*["-"] * 20)
            await pilot.pause(0.2)
            # The tabbed box keeps its tab strip *and* the column header under it — Textual
            # spends two rows on the strip before the table gets to draw anything.
            assert app.query_one("#target-tabs").size.height == tui_module.MIN_TARGET_BOX_HEIGHT
            assert app.query_one("#workflows-table", SelectableDataTable).size.height >= 1

            await pilot.press(*["+"] * 40)
            await pilot.pause(0.2)
            # And the tables below it are down to a column header each.
            assert app.query_one("#runs-table").size.height == tui_module.MIN_TABLE_HEIGHT
            assert app.query_one("#timeline-pane").size.height == tui_module.MIN_TARGET_BOX_HEIGHT

    asyncio.run(scenario())


def test_tui_growing_a_box_eats_past_its_neighbour() -> None:
    """Pushing the timeline up should not stop the moment the runs box runs out."""

    async def scenario() -> None:
        app = RebaseTuiApp(data=fake_tui_data(FakeClient(), project="energy", limit=5))

        async with app.run_test(size=(140, 42)) as pilot:
            await pilot.pause(0.2)
            projects = app.query_one("#projects-table", SelectableDataTable)
            projects.focus()
            projects.move_cursor(row=0)
            await pilot.press("enter")
            await pilot.pause(0.3)
            await pilot.press("enter")
            await pilot.pause(0.3)
            await pilot.press("enter")
            await pilot.pause(0.3)
            assert str(app.focused.id) == "timeline-table"

            tabs, runs, timeline = (app.query_one(box) for box in tui_module.BOX_SELECTORS)
            tall = tabs.size.height

            # The neighbour goes first, and the box above it is untouched while it lasts.
            await pilot.press(*["+"] * 4)
            await pilot.pause(0.2)
            assert runs.size.height == tui_module.MIN_TABLE_HEIGHT
            assert tabs.size.height == tall

            # Then the pushing carries on into the box above.
            await pilot.press(*["+"] * 3)
            await pilot.pause(0.2)
            assert tabs.size.height < tall
            assert runs.size.height == tui_module.MIN_TABLE_HEIGHT

            await pilot.press(*["+"] * 20)
            await pilot.pause(0.2)
            assert tabs.size.height == tui_module.MIN_TARGET_BOX_HEIGHT
            assert timeline.size.height == 42 - 2 - tui_module.MIN_TARGET_BOX_HEIGHT - 1

    asyncio.run(scenario())


def test_tui_shrinking_a_box_cannot_push_the_one_below_off_screen() -> None:
    """`-` on the bottom box hands rows to its neighbour, and only what it has to give."""

    async def scenario() -> None:
        app = RebaseTuiApp(data=fake_tui_data(FakeClient(), project="energy", limit=5))

        async with app.run_test(size=(140, 42)) as pilot:
            await pilot.pause(0.2)
            projects = app.query_one("#projects-table", SelectableDataTable)
            projects.focus()
            projects.move_cursor(row=0)
            await pilot.press("enter")
            await pilot.pause(0.3)
            await pilot.press("enter")
            await pilot.pause(0.3)
            await pilot.press("enter")
            await pilot.pause(0.3)
            assert str(app.focused.id) == "timeline-table"

            await pilot.press(*["-"] * 40)
            await pilot.pause(0.3)
            timeline = app.query_one("#timeline-pane")
            assert timeline.styles.display == "block"
            assert timeline.size.height == tui_module.MIN_TARGET_BOX_HEIGHT
            # Everything still fits between the header and the footer.
            boxes = [app.query_one(box).size.height for box in tui_module.BOX_SELECTORS]
            assert sum(boxes) == app.query_one("#project-view").size.height

    asyncio.run(scenario())


def test_tui_m_maximises_the_focused_box_and_b_gives_it_back() -> None:
    async def scenario() -> None:
        app = RebaseTuiApp(data=fake_tui_data(FakeClient(), project="energy", limit=5))

        async with app.run_test(size=(140, 42)) as pilot:
            await pilot.pause(0.2)
            projects = app.query_one("#projects-table", SelectableDataTable)
            projects.focus()
            projects.move_cursor(row=0)
            await pilot.press("enter")
            await pilot.pause(0.3)

            # One box open: it already has the screen.
            await pilot.press("m")
            await pilot.pause(0.1)
            assert any("already has the screen" in n.message for n in app._notifications)

            await pilot.press("enter")
            await pilot.pause(0.3)
            await pilot.press("enter")
            await pilot.pause(0.3)
            tabs, runs, timeline = (app.query_one(box) for box in tui_module.BOX_SELECTORS)

            # Maximise the middle box, not just the bottom one. The others go off screen
            # rather than shrinking to their headers, and the header bar goes with them:
            # what is left is the box and the footer.
            header = app.query_one(tui_module.RebaseHeader)
            app.query_one("#runs-table", DataTable).focus()
            await pilot.pause(0.1)
            await pilot.press("m")
            await pilot.pause(0.2)
            assert (tabs.display, timeline.display, header.display) == (False, False, False)
            assert runs.size.height == 42 - 1  # everything but the footer

            # Nothing else is on screen, so tab stays put.
            await pilot.press("tab")
            await pilot.pause(0.1)
            assert str(app.focused.id) == "runs-table"

            # b gives the screen back without also closing the box.
            await pilot.press("b")
            await pilot.pause(0.2)
            assert app._reveal_level == 2
            assert (tabs.display, timeline.display, header.display) == (True, True, True)

            # And m toggles it off again itself.
            await pilot.press("m")
            await pilot.pause(0.2)
            assert timeline.display is False
            await pilot.press("m")
            await pilot.pause(0.2)
            assert (timeline.display, header.display) == (True, True)

            # Leaving the project restores the chrome even from inside `m`.
            await pilot.press("m")
            await pilot.pause(0.2)
            assert header.display is False
            app.query_one("#projects-table", SelectableDataTable).focus()
            app._show_workspace_view()
            await pilot.pause(0.2)
            assert header.display is True
            assert app._maximised is None

    asyncio.run(scenario())


def test_tui_dragging_a_column_header_resizes_the_box_above_it() -> None:
    """The splitter is the header itself, so no box spends a row on a handle."""

    async def scenario() -> None:
        app = RebaseTuiApp(data=fake_tui_data(FakeClient(), project="energy", limit=5))

        async with app.run_test(size=(140, 42)) as pilot:
            await pilot.pause(0.2)
            projects = app.query_one("#projects-table", SelectableDataTable)
            projects.focus()
            projects.move_cursor(row=0)
            await pilot.press("enter")
            await pilot.pause(0.3)
            await pilot.press("enter")
            await pilot.pause(0.3)

            runs = app.query_one("#runs-table", tui_module.DragHeaderTable)
            assert runs.resizes == "#target-tabs"
            start = app.query_one("#target-tabs").size.height

            # A press on a data row is still a press on a data row.
            await pilot.mouse_down(runs, offset=(4, 2))
            await pilot.pause(0.1)
            assert app._drag_box is None
            await pilot.mouse_up(runs, offset=(4, 2))

            # A press on the header row starts a drag, and releasing ends it.
            await pilot.mouse_down(runs, offset=(4, 0))
            await pilot.pause(0.1)
            assert app._drag_box is not None
            await pilot.mouse_up(runs, offset=(4, 0))
            await pilot.pause(0.1)
            assert app._drag_box is None
            assert app.query_one("#target-tabs").size.height == start

            # The move itself goes through the app rather than the pilot: a pilot offset
            # is relative to a widget that moves under the pointer mid-drag, where a real
            # pointer stays put and lets the widget come to it.
            grabbed_at = runs.region.y
            await pilot.mouse_down(runs, offset=(4, 0))
            await pilot.pause(0.1)
            app.drag_box_to(grabbed_at + 4)
            app.end_box_drag()
            await pilot.pause(0.2)
            assert app.query_one("#target-tabs").size.height == start + 4

    asyncio.run(scenario())


def test_tui_arrows_step_the_workspace_resource_chips() -> None:
    """`left`/`right` walk Projects/Buckets/Secrets, wrapping either way round, and the
    focus lands on the table the chip opened."""

    async def scenario() -> None:
        app = RebaseTuiApp(data=fake_tui_data(FakeClient(), project="energy", limit=5))

        async with app.run_test(size=(140, 42)) as pilot:
            await pilot.pause(0.3)
            tabs = app.query_one("#workspace-resource-tabs", TabbedContent)
            assert tabs.active == "projects-resource-tab"
            app.query_one("#projects-table", DataTable).focus()

            for expected, table_id in (
                ("buckets-resource-tab", "#buckets-table"),
                ("secrets-resource-tab", "#secrets-table"),
                ("projects-resource-tab", "#projects-table"),
            ):
                await pilot.press("right")
                await pilot.pause(0.1)
                assert tabs.active == expected
                # The focus follows the chip, so `tab` and the arrows carry on from the
                # table that is actually on screen.
                assert app.focused is app.query_one(table_id, DataTable)

            # And back the other way, wrapping off the first chip onto the last.
            await pilot.press("left")
            await pilot.pause(0.1)
            assert tabs.active == "secrets-resource-tab"

    asyncio.run(scenario())


def test_tui_two_finger_scroll_moves_a_table_sideways() -> None:
    """A trackpad swipe — the bytes a terminal sends for it — drives the scrollbar."""

    def wheel(sequence: str) -> list[Any]:
        return list(XTermParser().feed(sequence))

    async def scenario() -> None:
        app = RebaseTuiApp(data=fake_tui_data(FakeClient(), project="energy", limit=5))

        # Narrow enough that the columns outgrow the width and the bar appears.
        async with app.run_test(size=(60, 20)) as pilot:
            await pilot.pause(0.3)
            table = app.query_one("#projects-table", DataTable)
            assert table.show_horizontal_scrollbar

            # SGR buttons 66 and 67 are wheel-left and wheel-right: what a two-finger
            # swipe reaches the app as.
            for event in wheel("\x1b[<67;20;10M"):
                app.screen._forward_event(event)
            await pilot.pause(0.2)
            assert table.scroll_x == tui_module.HeaderSafeDataTable.HORIZONTAL_SCROLL_CELLS

            for event in wheel("\x1b[<66;20;10M"):
                app.screen._forward_event(event)
            await pilot.pause(0.2)
            assert table.scroll_x == 0

            # The tables of the project view that scroll take the same swipe; the ones of
            # fixed-width fields still clip, and a swipe over them does nothing.
            await _open_run(app, pilot)
            for table_id in ("#workflows-table", "#functions-table", "#timeline-table"):
                assert app.query_one(table_id, DataTable).styles.overflow_x == "auto"
            assert app.query_one("#runs-table", DataTable).styles.overflow_x == "hidden"

    asyncio.run(scenario())


def test_tui_question_mark_toggles_the_keys_panel_from_the_footer() -> None:
    """The way to the keys you cannot see is itself a key you can see."""

    async def scenario() -> None:
        app = RebaseTuiApp(data=fake_tui_data(FakeClient(), project="energy", limit=5))

        async with app.run_test(size=(140, 42)) as pilot:
            await pilot.pause(0.3)

            # In the footer, beside the four you move around with — not one keystroke
            # away in the panel it opens.
            footer_keys = [key.key for key in app.query_one(Footer).query(FooterKey)]
            assert "question_mark" in footer_keys
            hint = next(key for key in app.query_one(Footer).query(FooterKey) if key.key == "question_mark")
            assert hint.key_display == "?"
            assert hint.description == "Keys"

            await pilot.press("question_mark")
            await pilot.pause(0.2)
            assert app.screen.query(HelpPanel)

            # The same key puts it away again.
            await pilot.press("question_mark")
            await pilot.pause(0.2)
            assert not app.screen.query(HelpPanel)

    asyncio.run(scenario())


def test_tui_back_closes_the_keys_panel_first() -> None:
    """`b` and `escape` both close the keys panel, and only then navigate."""

    async def scenario() -> None:
        for key in ("b", "escape"):
            app = RebaseTuiApp(data=fake_tui_data(FakeClient(), project="energy", limit=5))

            async with app.run_test(size=(140, 42)) as pilot:
                await _open_run(app, pilot)
                revealed = app._reveal_level
                assert revealed > 0

                app.action_show_help_panel()
                await pilot.pause(0.2)
                assert app.screen.query(HelpPanel)

                # The press that closes the panel does nothing else: the run stays open.
                await pilot.press(key)
                await pilot.pause(0.2)
                assert not app.screen.query(HelpPanel)
                assert app.current_view == "project"
                assert app._reveal_level == revealed

                # The next one goes back, as it always did.
                await pilot.press(key)
                await pilot.pause(0.2)
                assert app._reveal_level == revealed - 1

    asyncio.run(scenario())


def test_tui_chrome_rows_share_one_background() -> None:
    """Header, chip strip and column header are one band, in both views."""

    async def scenario() -> None:
        app = RebaseTuiApp(data=fake_tui_data(FakeClient(), project="energy", limit=5))

        async with app.run_test(size=(140, 42)) as pilot:
            await pilot.pause(0.3)
            band = tui_module.CHROME_GRAY

            def background(selector: str) -> str:
                # The painted colour, not the declared one: a `transparent` widget in
                # the band — the clock — is right if what shows through is the band.
                return app.query_one(selector).background_colors[1].hex

            def header_background(selector: str) -> str:
                table = app.query_one(selector, DataTable)
                return table.get_component_styles("datatable--header").background.hex

            # The workspace view: the top row, the resource chips, the columns.
            assert background("RebaseHeader") == band
            assert background("RebaseClock") == band
            assert background("#workspace-resource-tabs Tabs") == band
            assert header_background("#projects-table") == band

            await _open_run(app, pilot)

            # The project view: same three rows, plus the two strips further down that
            # double as splitters.
            assert background("#target-tabs Tabs") == band
            assert background("#timeline-tabs") == band
            for table in ("#workflows-table", "#runs-table", "#timeline-table"):
                assert header_background(table) == band

    asyncio.run(scenario())


def test_tui_chip_strips_are_splitters_and_still_switch_on_a_click() -> None:
    """The tab rows drag like the header rows under them, without giving up their chips."""

    def assert_centred(strip: Tabs) -> None:
        chip_list = strip.query_one("#tabs-list").region
        lead = chip_list.x - strip.region.x
        trail = strip.region.right - chip_list.right
        assert lead > 0
        assert abs(lead - trail) <= 1

    async def scenario() -> None:
        app = RebaseTuiApp(data=fake_tui_data(FakeClient(), project="energy", limit=5))

        async with app.run_test(size=(140, 42)) as pilot:
            # The workspace strip is centred too — checked here, before drilling into
            # the project view hides it.
            await pilot.pause(0.2)
            assert_centred(app.query_one("#workspace-resource-tabs Tabs", Tabs))

            await _open_run(app, pilot)

            # The strips wear the header row's own background, so each two-row band
            # reads as one piece of chrome — including while the table under a strip
            # has the focus, when Textual would otherwise tint its header a shade
            # lighter and draw a seam between the two rows.
            timeline = app.query_one("#timeline-table", tui_module.TimelineTable)
            timeline.focus()
            await pilot.pause(0.1)
            header = timeline.get_component_styles("datatable--header")
            assert app.query_one("#timeline-tabs", tui_module.DragTabs).styles.background == header.background
            assert app.query_one("#target-tabs Tabs", Tabs).styles.background == header.background
            assert header.background_tint.a == 0

            # And the chips sit centred in their strips, not against the left edge.
            assert_centred(app.query_one("#timeline-tabs", Tabs))
            assert_centred(app.query_one("#target-tabs Tabs", Tabs))

            # The timeline chips sit on the runs/timeline boundary, same as the
            # timeline's column header, and drag the same box.
            chips = app.query_one("#timeline-tabs", tui_module.DragTabs)
            assert chips.resizes == "#runs-table"
            start = app.query_one("#runs-table").size.height
            grabbed_at = chips.region.y
            await pilot.mouse_down(chips, offset=(1, 0))
            await pilot.pause(0.1)
            assert app._drag_box is not None
            app.drag_box_to(grabbed_at + 3)
            app.end_box_drag()
            await pilot.mouse_up(chips, offset=(1, 0))
            await pilot.pause(0.2)
            assert app._drag_box is None
            assert app.query_one("#runs-table").size.height == start + 3

            # A grab on the strip's empty stretch — past the chips, where the widget
            # under the pointer allows text selection — must not arm one: mid-drag the
            # pointer crosses the tables, and an armed selection auto-scrolls whichever
            # it is over.
            await pilot.mouse_down(chips, offset=(chips.size.width - 2, 0))
            await pilot.pause(0.1)
            assert app._drag_box is not None
            assert app.screen._select_state is None
            await pilot.mouse_up(chips, offset=(chips.size.width - 2, 0))
            await pilot.pause(0.1)

            # A press and release that never leaves its chip is still a click.
            logs_chip = app.query_one("#timeline-logs", Tab)
            await pilot.mouse_down(logs_chip, offset=(1, 0))
            await pilot.pause(0.1)
            await pilot.mouse_up(logs_chip, offset=(1, 0))
            await pilot.pause(0.2)
            assert app.query_one("#timeline-tabs", Tabs).active == "timeline-logs"

            # The target chips have nothing above them, so their drag stretches their
            # own box: the pointer's travel goes to the box's far edge.
            target = app.query_one("#target-tabs", tui_module.DragTabbedContent)
            assert target.resizes == "#target-tabs"
            start = target.size.height
            grabbed_at = target.region.y
            await pilot.mouse_down(target, offset=(2, 0))
            await pilot.pause(0.1)
            assert app._drag_box is not None
            app.drag_box_to(grabbed_at + 4)
            app.end_box_drag()
            await pilot.mouse_up(target, offset=(2, 0))
            await pilot.pause(0.2)
            assert target.size.height == start + 4

            # And a chip click inside the tabbed box still switches the pane.
            functions_chip = next(tab for tab in app.query("#target-tabs Tab") if "Functions" in str(tab.label))
            await pilot.mouse_down(functions_chip, offset=(1, 0))
            await pilot.pause(0.1)
            await pilot.mouse_up(functions_chip, offset=(1, 0))
            await pilot.pause(0.2)
            assert app.query_one("#target-tabs", TabbedContent).active == "functions-tab"

            # A press on the tables inside the tabbed box is not a grab on the strip.
            functions = app.query_one("#functions-table", SelectableDataTable)
            await pilot.mouse_down(functions, offset=(4, 2))
            await pilot.pause(0.1)
            assert app._drag_box is None
            await pilot.mouse_up(functions, offset=(4, 2))

    asyncio.run(scenario())


def test_tui_p_opens_the_selected_row_as_json() -> None:
    async def scenario() -> None:
        app = RebaseTuiApp(data=fake_tui_data(FakeClient(), project="energy", limit=5))

        async with app.run_test(size=(140, 42)) as pilot:
            await pilot.pause(0.2)
            projects = app.query_one("#projects-table", SelectableDataTable)
            projects.focus()
            projects.move_cursor(row=0)

            # In the workspace view it is the project, counts included.
            await pilot.press("p")
            await pilot.pause(0.2)
            assert isinstance(app.screen, DetailDrawer)
            assert [(f.label, f.value) for f in app.screen.fields] == [("Project", "energy")]
            assert app.screen.payload["workflows"] == 1
            assert len(app.screen.sections) == 1
            await pilot.press("escape")
            await pilot.pause(0.2)

            await pilot.press("enter")
            await pilot.pause(0.3)
            await pilot.press("enter")
            await pilot.pause(0.3)
            app.query_one("#runs-table", DataTable).focus()
            await pilot.pause(0.1)

            # And on a run it is the parameters the run was dispatched with.
            await pilot.press("p")
            await pilot.pause(0.2)
            drawer = app.screen
            assert isinstance(drawer, DetailDrawer)
            # The id is spelled out in full, each on a labelled line of its own.
            assert [(f.label, f.value) for f in drawer.fields] == [
                ("Run ID", "run-id"),
                ("Status", "succeeded"),
            ]
            # Input and output are separate sections, each naming itself with the key
            # it actually has in the record rather than a caption above it.
            assert drawer.sections == [
                {"parameters": {"site_id": "site-001"}},
                {"result": {"ok": True}},
            ]

            # p closes it again, the way it opened it.
            await pilot.press("p")
            await pilot.pause(0.2)
            assert not isinstance(app.screen, DetailDrawer)

    asyncio.run(scenario())


def test_tui_detail_payload_elides_the_deployed_source() -> None:
    payload = detail_payload({"name": "forecast", "source_code": "x" * 4096}, url="https://example.com")
    assert payload["name"] == "forecast"
    assert payload["source_code"] == "<4096 characters — press o to open the source>"
    assert payload["url"] == "https://example.com"
    # Nothing to say is not worth a key.
    assert "endpoints" not in detail_payload({"name": "forecast"}, endpoints=[])


def test_tui_build_timeline_interleaves_events_steps_and_logs() -> None:
    events = [
        {"stage": "dispatch", "status": "completed", "message": "Accepted.", "created_at": "2026-06-16T14:00:01Z"}
    ]
    steps = [
        {
            "name": "load_weather",
            "status": "succeeded",
            "attempt": 1,
            "started_at": "2026-06-16T14:00:05Z",
            "finished_at": "2026-06-16T14:00:20Z",
        }
    ]
    logs = [
        {"timestamp": "2026-06-16T14:00:02Z", "severity": "INFO", "message": "Fetching."},
        {"timestamp": "2026-06-16T14:00:06Z", "severity": "ERROR", "message": "Retrying."},
    ]

    assert [(row.kind, row.stage) for row in tui_module.build_timeline(events, steps, None)] == [
        ("event", "dispatch"),
        ("step", "load_weather"),
    ]
    assert [(row.kind, row.stage) for row in tui_module.build_timeline(events, steps, logs)] == [
        ("event", "dispatch"),
        ("log", ""),
        ("step", "load_weather"),
        ("log", ""),
    ]
    # A step with no error says what it did instead of showing a bare dash.
    with_steps = tui_module.build_timeline([], steps, None)
    assert "attempt 1" in with_steps[0].message and "finished" in with_steps[0].message
    assert tui_module.build_timeline([], [{"name": "a", "error": "boom"}], None)[0].message == "boom"


def test_tui_build_timeline_keeps_rows_with_no_usable_timestamp() -> None:
    rows = tui_module.build_timeline(
        [{"stage": "late", "created_at": "2026-06-16T14:00:01Z"}, {"stage": "undated", "created_at": None}], [], None
    )
    assert [row.stage for row in rows] == ["undated", "late"]


def test_tui_build_timeline_preserves_a_steps_task_lineage() -> None:
    """A step reports one outcome; its tasks are where "which one failed" survives."""
    steps = [{"id": "step-1", "name": "fetch", "status": "succeeded", "started_at": "2026-06-16T14:00:05Z"}]
    tasks = [
        {
            "id": "task-0",
            "step_run_id": "step-1",
            "item_index": 0,
            "parameters": {"area": "NO1"},
            "status": "succeeded",
            "result": {"objects": 1},
            "started_at": "2026-06-16T14:00:06Z",
        },
        {
            "id": "task-1",
            "step_run_id": "step-1",
            "item_index": 1,
            "parameters": {"area": "SE3"},
            "status": "failed",
            "error": "401 Unauthorized",
            "started_at": "2026-06-16T14:00:07Z",
        },
    ]

    rows = tui_module.build_timeline([], steps, None, tasks)
    assert [(row.kind, row.stage, row.status) for row in rows] == [
        ("step", "fetch", "succeeded"),
        ("task", "task 0", "succeeded"),
        ("task", "task 1", "failed"),
    ]
    # The parameters say which unit of work it was; the error says what became of it.
    assert rows[1].message == '{"area": "NO1"} -> {"objects": 1}'
    assert rows[2].message == '{"area": "SE3"} -> 401 Unauthorized'
    assert [row.scope for row in rows] == ["run", "fetch", "fetch"]

    # A task that has not started yet sorts with its batch, not to the top of the run.
    queued = tui_module.build_timeline(
        [], steps, None, [{"item_index": 0, "status": "queued", "created_at": "2026-06-16T14:00:06Z"}]
    )
    assert [row.kind for row in queued] == ["step", "task"]


def test_tui_build_timeline_places_artifacts_under_their_declared_owner() -> None:
    steps = [{"id": "step-1", "name": "fetch", "started_at": "2026-06-16T14:00:05Z"}]
    tasks = [{"id": "task-1", "name": "Capture NO1", "step_run_id": "step-1"}]
    artifacts = [
        {"id": "a1", "name": "curves", "uri": "gs://curves/no1", "task_id": "task-1"},
        {"id": "a2", "name": "summary", "uri": "gs://curves/summary", "step_run_id": "step-1"},
        {"id": "a3", "name": "manifest", "uri": "gs://curves/manifest"},
    ]

    rows = tui_module.build_timeline([], steps, None, tasks, artifacts)
    scopes = {row.stage: row.scope for row in rows if row.kind == "artifact"}

    assert scopes == {
        "curves": "fetch › Capture NO1",
        "summary": "fetch",
        "manifest": "run",
    }


def test_tui_functions_table_names_the_workflow_each_step_belongs_to() -> None:
    """A step is registered as a function, with nothing on the row to say whose step it is."""

    async def scenario() -> None:
        app = RebaseTuiApp(data=fake_tui_data(SteppedClient(), project="energy", limit=5))

        async with app.run_test(size=(160, 42)) as pilot:
            await pilot.pause(0.2)
            projects = app.query_one("#projects-table", SelectableDataTable)
            projects.focus()
            projects.move_cursor(row=0)
            await pilot.press("enter")
            await pilot.pause(0.3)

            functions = app.query_one("#functions-table", DataTable)
            # Graph order first — load_weather then normalize — and the stray function last.
            assert [str(functions.get_cell_at(Coordinate(row, 0))) for row in range(3)] == [
                "load_weather",
                "normalize",
                "healthcheck",
            ]
            # Name, Origin, Workflow, Step.
            assert [str(functions.get_cell_at(Coordinate(row, 2))) for row in range(3)] == [
                "forecast",
                "forecast",
                "-",
            ]
            assert [str(functions.get_cell_at(Coordinate(row, 3))) for row in range(3)] == [
                "load_weather",
                "normalize",
                "-",
            ]

            # The drawer spells the wiring out, in both directions.
            app.query_one("#target-tabs", TabbedContent).active = "functions-tab"
            functions.focus()
            functions.move_cursor(row=1)
            await pilot.press("p")
            await pilot.pause(0.3)
            drawer = app.screen
            assert isinstance(drawer, DetailDrawer)
            assert drawer.payload["step_of"] == [
                {"workflow": "forecast", "node_key": "normalize", "after": ["load_weather"]}
            ]
            await pilot.press("escape")
            await pilot.pause(0.2)

            workflows = app.query_one("#workflows-table", SelectableDataTable)
            app.query_one("#target-tabs", TabbedContent).active = "workflows-tab"
            workflows.focus()
            workflows.move_cursor(row=0)
            await pilot.press("p")
            await pilot.pause(0.3)
            drawer = app.screen
            assert isinstance(drawer, DetailDrawer)
            assert [step["node_key"] for step in drawer.payload["steps"]] == ["load_weather", "normalize"]

    asyncio.run(scenario())


def test_tui_functions_table_leaves_the_step_columns_empty_for_a_plain_function() -> None:
    async def scenario() -> None:
        app = RebaseTuiApp(data=fake_tui_data(FakeClient(), project="energy", limit=5))

        async with app.run_test(size=(160, 42)) as pilot:
            await pilot.pause(0.2)
            projects = app.query_one("#projects-table", SelectableDataTable)
            projects.focus()
            projects.move_cursor(row=0)
            await pilot.press("enter")
            await pilot.pause(0.3)

            # Workflow and Step, which follow Name and Origin.
            functions = app.query_one("#functions-table", DataTable)
            assert str(functions.get_cell_at(Coordinate(0, 2))) == "-"
            assert str(functions.get_cell_at(Coordinate(0, 3))) == "-"

            # And the drawer carries no step keys for either side of the pair.
            app.query_one("#target-tabs", TabbedContent).active = "functions-tab"
            functions.focus()
            functions.move_cursor(row=0)
            await pilot.press("p")
            await pilot.pause(0.3)
            drawer = app.screen
            assert isinstance(drawer, DetailDrawer)
            assert "step_of" not in drawer.payload
            await pilot.press("escape")
            await pilot.pause(0.2)

            workflows = app.query_one("#workflows-table", SelectableDataTable)
            app.query_one("#target-tabs", TabbedContent).active = "workflows-tab"
            workflows.focus()
            workflows.move_cursor(row=0)
            await pilot.press("p")
            await pilot.pause(0.3)
            drawer = app.screen
            assert isinstance(drawer, DetailDrawer)
            assert "steps" not in drawer.payload

    asyncio.run(scenario())


def test_tui_runs_table_reports_when_each_run_started_and_how_long_it_took() -> None:
    async def scenario() -> None:
        app = RebaseTuiApp(data=fake_tui_data(FakeClient(), project="energy", limit=5))

        async with app.run_test(size=(160, 42)) as pilot:
            await pilot.pause(0.2)
            projects = app.query_one("#projects-table", SelectableDataTable)
            projects.focus()
            projects.move_cursor(row=0)
            await pilot.press("enter")
            await pilot.pause(0.3)

            workflows = app.query_one("#workflows-table", SelectableDataTable)
            workflows.focus()
            workflows.move_cursor(row=0)
            await pilot.press("enter")
            await pilot.pause(0.3)

            runs = app.query_one("#runs-table", DataTable)
            assert [str(column.label) for column in runs.columns.values()] == [
                "Run",
                "Status",
                "Trigger",
                "Created",
                "Started",
                "Finished",
                "Duration",
            ]

            # Timestamps render in the display zone, which is the machine's until changed.
            def shown(value: str) -> str:
                return format_timestamp(value, app.display_tzinfo)

            assert [str(cell) for cell in runs.get_row_at(0)] == [
                "run-id",
                "succeeded",
                "schedule",
                shown("2026-06-16T14:00:00Z"),
                shown("2026-06-16T14:00:10Z"),
                shown("2026-06-16T14:01:00Z"),
                "50.0s",
            ]

    asyncio.run(scenario())


def test_tui_says_so_when_the_workspace_has_no_projects() -> None:
    """A blank table reads as "still loading"; an empty workspace should say it is empty."""

    async def scenario() -> None:
        app = RebaseTuiApp(data=fake_tui_data(EmptyWorkspaceClient(), limit=5))

        async with app.run_test(size=(140, 42)) as pilot:
            await pilot.pause(0.3)
            empty = app.query_one("#workspace-empty", Static)
            assert empty.styles.display == "block"
            assert "No projects in workspace" in str(empty.content)
            assert app.query_one("#projects-table", SelectableDataTable).row_count == 0

    asyncio.run(scenario())


def test_tui_hides_the_empty_notice_once_projects_arrive() -> None:
    async def scenario() -> None:
        app = RebaseTuiApp(data=fake_tui_data(FakeClient(), limit=5))

        async with app.run_test(size=(140, 42)) as pilot:
            await pilot.pause(0.3)
            assert app.query_one("#projects-table", SelectableDataTable).row_count == 2
            assert app.query_one("#workspace-empty").styles.display == "none"

    asyncio.run(scenario())


def test_tui_warns_when_the_directory_pin_is_not_what_the_data_came_from() -> None:
    """The pin normally wins; when something overrode it, say so rather than guess."""

    async def scenario(monkeypatch) -> None:
        monkeypatch.setattr(tui_module, "local_workspace_id", lambda: "rebase-grid")
        app = RebaseTuiApp(data=fake_tui_data(FakeClient(), limit=5))

        async with app.run_test(size=(140, 42)) as pilot:
            await pilot.pause(0.3)
            messages = [notification.message for notification in app._notifications]
            assert any("pinned to workspace rebase-grid" in message for message in messages), messages
            assert any("data is coming from" in message for message in messages)

    with pytest.MonkeyPatch.context() as monkeypatch:
        asyncio.run(scenario(monkeypatch))


def test_tui_stays_quiet_when_the_directory_pins_nothing() -> None:
    async def scenario(monkeypatch) -> None:
        monkeypatch.setattr(tui_module, "local_workspace_id", lambda: None)
        app = RebaseTuiApp(data=fake_tui_data(FakeClient(), limit=5))

        async with app.run_test(size=(140, 42)) as pilot:
            await pilot.pause(0.3)
            messages = [notification.message for notification in app._notifications]
            assert not any("pinned to workspace" in message for message in messages), messages

    with pytest.MonkeyPatch.context() as monkeypatch:
        asyncio.run(scenario(monkeypatch))


def test_tui_title_names_the_workspace_a_marker_pinned() -> None:
    """The profile's own label would name the workspace the pin overrode."""

    async def scenario() -> None:
        client = FakeClient()
        client.workspace_id = "rebase-grid"
        app = RebaseTuiApp(data=fake_tui_data(client, limit=5))

        async with app.run_test(size=(140, 42)) as pilot:
            await pilot.pause(0.3)
            assert "rebase-grid" in app.title

    asyncio.run(scenario())


def test_tui_p_in_the_timeline_opens_the_run_it_belongs_to() -> None:
    """The timeline is one run's story; there is no per-row record behind a log line."""

    async def scenario() -> None:
        app = RebaseTuiApp(data=fake_tui_data(FakeClient(), project="energy", limit=5))

        async with app.run_test(size=(160, 42)) as pilot:
            await pilot.pause(0.2)
            projects = app.query_one("#projects-table", SelectableDataTable)
            projects.focus()
            projects.move_cursor(row=0)
            await pilot.press("enter")
            await pilot.pause(0.3)
            await pilot.press("enter")
            await pilot.pause(0.3)
            await pilot.press("enter")
            await pilot.pause(0.4)
            assert str(app.focused.id) == "timeline-table"

            await pilot.press("p")
            await pilot.pause(0.2)
            drawer = app.screen
            assert isinstance(drawer, DetailDrawer)
            assert [(f.label, f.value) for f in drawer.fields] == [
                ("Run ID", "run-id"),
                ("Status", "succeeded"),
            ]

    asyncio.run(scenario())


def test_tui_p_opens_the_task_or_artifact_behind_an_activity_row() -> None:
    """Execution children keep their own record instead of falling back to the run."""

    async def scenario() -> None:
        app = RebaseTuiApp(data=fake_tui_data(SteppedClient(), project="energy", limit=5))

        async with app.run_test(size=(180, 42)) as pilot:
            await _open_run(app, pilot)
            timeline = app.query_one("#timeline-table", DataTable)

            app._select_timeline_filter("timeline-tasks")
            timeline.focus()
            timeline.move_cursor(row=0)
            await pilot.press("p")
            await pilot.pause(0.2)
            task_drawer = app.screen
            assert isinstance(task_drawer, DetailDrawer)
            assert task_drawer.drawer_title == "Capture NO1"
            assert [(field.label, field.value) for field in task_drawer.fields[1:3]] == [
                ("Status", "succeeded"),
                ("Scope", "load_weather"),
            ]
            assert task_drawer.sections[0] == {"parameters": {"area": "NO1"}}

            await pilot.press("p")
            app._select_timeline_filter("timeline-artifacts")
            timeline.focus()
            timeline.move_cursor(row=0)
            await pilot.press("p")
            await pilot.pause(0.2)
            artifact_drawer = app.screen
            assert isinstance(artifact_drawer, DetailDrawer)
            assert artifact_drawer.drawer_title == "NO1 day-ahead curves"
            assert ("Scope", "load_weather › Capture NO1") in [
                (field.label, field.value) for field in artifact_drawer.fields
            ]
            assert ("Producer run", "mapped-run-id") in [(field.label, field.value) for field in artifact_drawer.fields]
            assert artifact_drawer.payload["uri"] == "gs://nordpool-curves/2026-06-16/NO1.json"

    asyncio.run(scenario())


def test_tui_run_drawer_puts_an_error_before_the_result() -> None:
    run = {"id": "run-9", "status": "failed", "parameters": {"a": 1}, "result": None, "error": "boom"}
    app = RebaseTuiApp(data=fake_tui_data(FakeClient(), limit=5))
    drawer = app._run_drawer(run)

    assert [(f.label, f.value) for f in drawer.fields] == [("Run ID", "run-9"), ("Status", "failed")]
    # Two sections, so two rules: what it was given, and what came back — the error
    # belonging with the result rather than to a section of its own.
    assert drawer.sections == [{"parameters": {"a": 1}}, {"error": "boom", "result": None}]


def test_tui_notifications_wear_the_app_s_colours_and_hug_their_text() -> None:
    """Textual's default toast is a grey slab 60 cells wide whatever the message."""

    async def scenario() -> None:
        app = RebaseTuiApp(data=fake_tui_data(FakeClient(), limit=5))

        # run_test disables notifications by default, which is why every other test in
        # this file reads app._notifications rather than the widget.
        async with app.run_test(size=(150, 26), notifications=True) as pilot:
            await pilot.pause(0.3)
            app.notify("Times now shown in UTC.")
            await pilot.pause(0.3)

            toast = app.query_one(Toast)
            assert toast.styles.border_top[1].hex == "#03C497"
            # Standing clear of the footer, not sharing its row: the footer is the last
            # line, and the toast's region has to end above it.
            footer = app.query_one(Footer)
            assert toast.region.y + toast.region.height <= footer.region.y

            # Each is sized to its own text rather than all being one fixed width.
            app.notify("Could not load logs for run 8547eb63: 404 Not Found", severity="error")
            await pilot.pause(0.3)
            assert len({t.region.width for t in app.query(Toast)}) == 2
            error = next(t for t in app.query(Toast) if t.has_class("-error"))
            assert error.styles.border_top[1].hex == "#E46962"

    asyncio.run(scenario())


def test_tui_footer_shows_only_the_keys_you_move_around_with() -> None:
    """Ten hints did not fit the width; the rest are one `?` away in the key panel."""

    async def scenario() -> None:
        app = RebaseTuiApp(data=fake_tui_data(FakeClient(), limit=5))

        async with app.run_test(size=(150, 26)) as pilot:
            await pilot.pause(0.3)
            shown = [
                binding.key
                for _, binding, enabled, _ in app.screen.active_bindings.values()
                if enabled and binding.show
            ]
            # The four you move around with, plus the one that shows you the rest.
            assert shown == ["tab", "q", "r", "b", "question_mark"]

            # Hidden, but still bound and still listed by the key panel.
            hidden = {
                binding.key: binding.description
                for _, binding, _, _ in app.screen.active_bindings.values()
                if not binding.show
            }
            for key in ("a", "d", "o", "g", "w", "s", "p", "l", "m"):
                assert key in hidden, key
            assert hidden["g"] == "Open deployed code on GitHub"
            assert hidden["w"] == "Switch workspace"
            assert hidden["a"] == "Open artifact"
            assert hidden["m"] == "Maximise pane"

            tab = next(b for _, b, _, _ in app.screen.active_bindings.values() if b.key == "tab")
            assert tab.description == "Next pane"

    asyncio.run(scenario())


def test_tui_background_is_the_same_in_every_view() -> None:
    """Textual tints the focused table, which changed the whole background per view."""

    async def scenario() -> None:
        app = RebaseTuiApp(data=fake_tui_data(FakeClient(), project="energy", limit=5))

        async with app.run_test(size=(120, 24)) as pilot:
            await pilot.pause(0.3)
            projects = app.query_one("#projects-table", SelectableDataTable)
            assert projects.has_focus
            # The one table filling the workspace view is focused, so its tint used to
            # repaint the entire screen a shade lighter than the project view's.
            assert projects.styles.background_tint.a == 0

            projects.move_cursor(row=0)
            await pilot.press("enter")
            await pilot.pause(0.3)
            workflows = app.query_one("#workflows-table", SelectableDataTable)
            assert workflows.has_focus
            assert workflows.styles.background.hex == projects.styles.background.hex
            assert workflows.styles.background_tint.a == 0

    asyncio.run(scenario())


def test_tui_run_detail_issues_its_reads_together() -> None:
    """Four sequential round trips was over a second of lag on a keypress."""
    order: list[str] = []
    started = threading.Barrier(4, timeout=5)

    class SlowClient(FakeClient):
        def _trip(self, name: str) -> None:
            order.append(name)
            # Blocks until all four have started; times out if any runs sequentially.
            started.wait()

        def get_run(self, run_id: str) -> dict[str, Any]:
            self._trip("run")
            return super().get_run(run_id)

        def list_run_events(self, run_id: str) -> list[dict[str, Any]]:
            self._trip("events")
            return super().list_run_events(run_id)

        def list_run_steps(self, run_id: str) -> list[dict[str, Any]]:
            self._trip("steps")
            return super().list_run_steps(run_id)

        def list_run_tasks(self, run_id: str, *, step_run_id: str | None = None) -> list[dict[str, Any]]:
            self._trip("tasks")
            return super().list_run_tasks(run_id, step_run_id=step_run_id)

    detail = fake_tui_data(SlowClient()).load_run_detail("run-id", target_type="workflow")
    assert sorted(order) == ["events", "run", "steps", "tasks"]
    assert detail.run["id"] == "run-id"
    assert [step["name"] for step in detail.steps] == ["load_weather"]


def test_tui_loads_named_tasks_for_function_runs() -> None:
    client = FakeClient()
    client.runs[0]["target_type"] = "function"
    client.tasks = [{"id": "task-id", "name": "Capture SE3", "status": "succeeded"}]

    detail = fake_tui_data(client).load_run_detail("run-id", target_type="function")

    assert detail.steps == []
    assert detail.tasks == client.tasks
    assert client.task_calls == ["run-id"]


def _open_run(app, pilot):
    """Drill workspace -> project -> workflow -> run, leaving the timeline focused."""

    async def go():
        await pilot.pause(0.2)
        projects = app.query_one("#projects-table", SelectableDataTable)
        projects.focus()
        projects.move_cursor(row=0)
        await pilot.press("enter")
        await pilot.pause(0.3)
        await pilot.press("enter")
        await pilot.pause(0.3)
        await pilot.press("enter")
        await pilot.pause(0.4)

    return go()


def test_tui_timeline_chips_filter_the_run_by_kind() -> None:
    """One table and its chips show the same run at different levels of detail."""

    async def scenario() -> None:
        app = RebaseTuiApp(data=fake_tui_data(SteppedClient(), project="energy", limit=5))

        async with app.run_test(size=(160, 40)) as pilot:
            await _open_run(app, pilot)
            timeline = app.query_one("#timeline-table", DataTable)
            assert [str(tab.label) for tab in app.query("#timeline-tabs Tab")] == [
                "[ Activity ]",
                "[ Steps 1 ]",
                "[ Tasks 2 ]",
                "[ Artifacts 1 ]",
                "[ Logs ]",
                "[ Events ]",
            ]

            def kinds() -> list[str]:
                return [str(timeline.get_cell_at(Coordinate(row, 1))) for row in range(timeline.row_count)]

            # Activity: the Type column says which each row is, and everything is present.
            assert set(kinds()) == {"artifact", "event", "log", "step", "task"}
            task_row = next(
                row for row in range(timeline.row_count) if str(timeline.get_cell_at(Coordinate(row, 1))) == "task"
            )
            artifact_row = next(
                row for row in range(timeline.row_count) if str(timeline.get_cell_at(Coordinate(row, 1))) == "artifact"
            )
            assert str(timeline.get_cell_at(Coordinate(task_row, 2))) == "load_weather"
            assert str(timeline.get_cell_at(Coordinate(artifact_row, 2))) == "load_weather › Capture NO1"

            # left/right steps the chips, the same gesture as the target pane's.
            await pilot.press("right")
            await pilot.pause(0.2)
            assert app._timeline_filter == "timeline-steps"
            # A filtered view drops the Type column: every row would say the same word.
            # Steps earn a `Depends on` column instead — the DAG edges are the reason
            # this view is more than a chronological filter.
            assert [str(column.label) for column in timeline.columns.values()] == [
                "Step",
                "Depends on",
                "Status",
                "Attempt",
                "Started",
                "Finished",
                "Duration",
                "Message",
            ]
            assert [str(timeline.get_cell_at(Coordinate(row, 0))) for row in range(timeline.row_count)] == [
                "load_weather"
            ]
            # Attempt, both timestamps and Duration are the step's own, read off the step
            # run rather than composed into Message as `attempt 1 · finished ...` prose.
            # A root step reads "-": blank would say "unknown" about a known fact.
            assert [str(timeline.get_cell_at(Coordinate(0, column))) for column in (1, 3, 6)] == [
                "-",
                "1",
                "15.0s",
            ]
            assert str(timeline.get_cell_at(Coordinate(0, 4))) != str(timeline.get_cell_at(Coordinate(0, 5)))

            await pilot.press("right")
            await pilot.pause(0.2)
            assert app._timeline_filter == "timeline-tasks"
            assert [str(column.label) for column in timeline.columns.values()] == [
                "Task",
                "Step",
                "Kind",
                "Status",
                "Started",
                "Duration",
                "Artifacts",
                "Result / Error",
            ]
            assert [str(timeline.get_cell_at(Coordinate(row, 0))) for row in range(timeline.row_count)] == [
                "Capture NO1",
                "Capture SE3",
            ]
            assert [str(timeline.get_cell_at(Coordinate(row, 1))) for row in range(timeline.row_count)] == [
                "load_weather",
                "load_weather",
            ]
            assert [str(timeline.get_cell_at(Coordinate(row, 6))) for row in range(timeline.row_count)] == ["1", "0"]

            await pilot.press("right")
            await pilot.pause(0.2)
            assert app._timeline_filter == "timeline-artifacts"
            assert [str(column.label) for column in timeline.columns.values()] == [
                "Artifact",
                "Produced by",
                "Disposition",
                "Type",
                "Size",
                "URI",
            ]
            assert str(timeline.get_cell_at(Coordinate(0, 0))) == "NO1 day-ahead curves"
            assert str(timeline.get_cell_at(Coordinate(0, 1))) == "load_weather › Capture NO1"
            assert str(timeline.get_cell_at(Coordinate(0, 4))) == "4.0 KiB"
            assert "gs://nordpool-curves/2026-06-16/NO1.json" in str(timeline.get_cell_at(Coordinate(0, 5)))

            await pilot.press("right")
            await pilot.pause(0.2)
            assert app._timeline_filter == "timeline-logs"
            # Logs is everything the run said: the stages and the output together.
            assert timeline.row_count == 2

            await pilot.press("right")
            await pilot.pause(0.2)
            assert app._timeline_filter == "timeline-events"
            # Events is the platform's own account of the run: stages, no output.
            assert [str(timeline.get_cell_at(Coordinate(row, 1))) for row in range(timeline.row_count)] == ["dispatch"]

            await pilot.press("right")
            await pilot.pause(0.2)
            assert app._timeline_filter == "timeline-all"

    asyncio.run(scenario())


def test_tui_hides_empty_steps_tasks_and_artifacts_filters() -> None:
    """A run with no execution children should not advertise empty branches."""

    class EmptyRunDetailClient(FakeClient):
        def list_run_steps(self, run_id: str) -> list[dict[str, Any]]:
            assert run_id == "run-id"
            return []

    async def scenario() -> None:
        app = RebaseTuiApp(data=fake_tui_data(EmptyRunDetailClient(), project="energy", limit=5))

        async with app.run_test(size=(160, 40)) as pilot:
            await _open_run(app, pilot)
            visible = [str(tab.id) for tab in app.query("#timeline-tabs Tab") if tab.display]
            assert visible == ["timeline-all", "timeline-logs", "timeline-events"]
            assert app._available_timeline_filters() == visible

            # Hidden filters are absent from both direct selection and arrow navigation.
            app._select_timeline_filter("timeline-tasks")
            assert app._timeline_filter == "timeline-all"
            await pilot.press("right")
            await pilot.pause(0.2)
            assert app._timeline_filter == "timeline-logs"

    asyncio.run(scenario())


def test_tui_returns_to_activity_when_the_next_run_lacks_the_open_branch() -> None:
    """A Tasks chip selected on one run cannot remain active but invisible on the next."""

    async def scenario() -> None:
        app = RebaseTuiApp(data=fake_tui_data(SteppedClient(), project="energy", limit=5))

        async with app.run_test(size=(160, 40)) as pilot:
            await _open_run(app, pilot)
            app._select_timeline_filter("timeline-tasks")
            assert app._timeline_filter == "timeline-tasks"
            detail = app._run_detail
            assert detail is not None
            app._run_detail = tui_module.RunDetailData(
                run={**detail.run, "id": "plain-run"},
                events=detail.events,
                steps=[],
                tasks=[],
                artifacts=[],
                logs=detail.logs,
            )

            app._render_timeline()
            await pilot.pause(0.2)

            assert app._timeline_filter == "timeline-all"
            assert app.query_one("#timeline-tabs", Tabs).active == "timeline-all"
            assert [str(tab.id) for tab in app.query("#timeline-tabs Tab") if tab.display] == [
                "timeline-all",
                "timeline-logs",
                "timeline-events",
            ]

            # A filter the next run cannot show is not merely deselected, it is
            # unreachable: selecting it directly is ignored rather than painting an
            # empty table for a branch this run does not have.
            app._select_timeline_filter("timeline-artifacts")
            await pilot.pause(0.2)
            assert app._timeline_filter == "timeline-all"

    asyncio.run(scenario())


def test_tui_a_resolves_and_opens_the_selected_bucket_artifact(monkeypatch) -> None:
    opened: list[str] = []
    monkeypatch.setattr("rebase.tui.webbrowser.open", lambda url: opened.append(url) or True)

    async def scenario() -> None:
        app = RebaseTuiApp(data=fake_tui_data(SteppedClient(), project="energy", limit=5))

        async with app.run_test(size=(160, 40)) as pilot:
            await _open_run(app, pilot)
            app._select_timeline_filter("timeline-artifacts")
            await pilot.pause(0.2)
            timeline = app.query_one("#timeline-table", DataTable)
            timeline.focus()
            timeline.move_cursor(row=0)
            await pilot.press("a")
            await pilot.pause(0.3)

    asyncio.run(scenario())
    assert opened == ["https://storage.example/signed-object"]


def test_tui_o_opens_the_marked_bucket_in_the_cloud_console(monkeypatch) -> None:
    """`o` reads as "open" on the buckets tab, where there is no source file to open."""
    opened: list[str] = []
    monkeypatch.setattr("rebase.tui.webbrowser.open", lambda url: opened.append(url) or True)

    async def scenario() -> None:
        app = RebaseTuiApp(data=fake_tui_data(EnvironmentClient()), refresh_interval=0)

        async with app.run_test(size=(140, 42)) as pilot:
            await pilot.pause(0.3)
            app.query_one("#workspace-resource-tabs", TabbedContent).active = "buckets-resource-tab"
            await pilot.pause(0.2)
            buckets = app.query_one("#buckets-table", SelectableDataTable)
            buckets.focus()
            buckets.move_cursor(row=0)
            await pilot.press("o")
            await pilot.pause(0.3)

    asyncio.run(scenario())
    assert opened == ["https://console.cloud.google.com/storage/browser/rb-dev-data-abc123?project=rebase-prod"]


def test_tui_o_opens_every_marked_bucket(monkeypatch) -> None:
    """Marking is why the buckets table is selectable at all — one `o`, every mark."""
    opened: list[str] = []
    monkeypatch.setattr("rebase.tui.webbrowser.open", lambda url: opened.append(url) or True)

    class TwoBucketClient(EnvironmentClient):
        def list_buckets(self) -> list[dict[str, Any]]:
            return [
                {"name": "agent-work", "uri": "gs://rb-agent-work-abc"},
                {"name": "forecasts", "uri": "gs://rb-forecasts-def"},
            ]

    async def scenario() -> None:
        app = RebaseTuiApp(data=fake_tui_data(TwoBucketClient()), refresh_interval=0)

        async with app.run_test(size=(140, 42)) as pilot:
            await pilot.pause(0.3)
            app.query_one("#workspace-resource-tabs", TabbedContent).active = "buckets-resource-tab"
            await pilot.pause(0.2)
            buckets = app.query_one("#buckets-table", SelectableDataTable)
            buckets.focus()
            buckets.move_cursor(row=0)
            await pilot.press("shift+down")
            await pilot.pause(0.1)
            await pilot.press("o")
            await pilot.pause(0.3)

    asyncio.run(scenario())
    assert opened == [
        "https://console.cloud.google.com/storage/browser/rb-agent-work-abc",
        "https://console.cloud.google.com/storage/browser/rb-forecasts-def",
    ]


def test_bucket_console_url_falls_back_to_the_gs_uri() -> None:
    """A server predating `console_url` still reports `uri`, which is enough."""
    assert bucket_console_url({"console_url": "https://console.cloud.google.com/x?project=p"}) == (
        "https://console.cloud.google.com/x?project=p"
    )
    assert bucket_console_url({"uri": "gs://rb-agent-work-data-abc123"}) == (
        "https://console.cloud.google.com/storage/browser/rb-agent-work-data-abc123"
    )
    assert bucket_console_url({"name": "agent-work"}) is None


def test_tui_l_jumps_to_the_logs_chip_and_back() -> None:
    async def scenario() -> None:
        app = RebaseTuiApp(data=fake_tui_data(FakeClient(), project="energy", limit=5))

        async with app.run_test(size=(160, 40)) as pilot:
            await _open_run(app, pilot)
            assert app._timeline_filter == "timeline-all"
            await pilot.press("l")
            await pilot.pause(0.2)
            assert app._timeline_filter == "timeline-logs"
            assert app.query_one("#timeline-tabs", Tabs).active == "timeline-logs"
            await pilot.press("l")
            await pilot.pause(0.2)
            assert app._timeline_filter == "timeline-all"

    asyncio.run(scenario())


def test_tui_enter_opens_a_timeline_row_out_to_its_full_text() -> None:
    """A log line is the one thing here that does not fit its row."""

    async def scenario() -> None:
        client = FakeClient()
        client.log_entries = [
            {
                "timestamp": "2026-06-16T14:00:02Z",
                "severity": "INFO",
                "message": "Worker 'CloudRunWorker d65ce4e6-7de4-4894-81d7-d403986f0ef5' submitting "
                "flow run '019fdd5e-2dba-7aaf-a9c2-3b9b26054810' to infrastructure",
            }
        ]
        app = RebaseTuiApp(data=fake_tui_data(client, project="energy", limit=5))

        async with app.run_test(size=(120, 40)) as pilot:
            await _open_run(app, pilot)
            timeline = app.query_one("#timeline-table", DataTable)
            log_row = next(
                row for row in range(timeline.row_count) if str(timeline.get_cell_at(Coordinate(row, 1))) == "log"
            )

            assert timeline.rows[timeline.coordinate_to_cell_key(Coordinate(log_row, 0)).row_key].height == 1
            timeline.focus()
            timeline.move_cursor(row=log_row)
            await pilot.press("enter")
            await pilot.pause(0.3)

            # Same row, now several lines tall, carrying the whole message.
            key = timeline.coordinate_to_cell_key(Coordinate(log_row, 0)).row_key
            assert timeline.rows[key].height > 1
            message = str(timeline.get_cell_at(Coordinate(log_row, 5)))
            assert "\n" in message
            # Wrapping only moves the line breaks; nothing is dropped or truncated.
            assert " ".join(message.split()) == client.log_entries[0]["message"]
            # Wrapped to the pane, not left running off the side.
            assert max(len(line) for line in message.splitlines()) <= 120

            await pilot.press("enter")
            await pilot.pause(0.3)
            key = timeline.coordinate_to_cell_key(Coordinate(log_row, 0)).row_key
            assert timeline.rows[key].height == 1

    asyncio.run(scenario())


def test_tui_timeline_scrolls_sideways_where_the_other_tables_clip() -> None:
    async def scenario() -> None:
        app = RebaseTuiApp(data=fake_tui_data(FakeClient(), project="energy", limit=5))

        async with app.run_test(size=(120, 40)) as pilot:
            await _open_run(app, pilot)
            timeline = app.query_one("#timeline-table", DataTable)
            assert timeline.styles.overflow_x == "auto"
            assert timeline.styles.scrollbar_size_horizontal == 1
            # The tables of fixed-width fields stay clipped; only prose scrolls.
            assert app.query_one("#runs-table").styles.overflow_x == "hidden"

    asyncio.run(scenario())


def test_tui_e_jumps_to_the_events_chip_and_back() -> None:
    """The stages on their own, without the runtime's output around them."""

    async def scenario() -> None:
        app = RebaseTuiApp(data=fake_tui_data(FakeClient(), project="energy", limit=5))

        async with app.run_test(size=(160, 40)) as pilot:
            await _open_run(app, pilot)
            timeline = app.query_one("#timeline-table", DataTable)

            await pilot.press("e")
            await pilot.pause(0.2)
            assert app._timeline_filter == "timeline-events"
            assert app.query_one("#timeline-tabs", Tabs).active == "timeline-events"
            assert timeline.row_count == 1  # the event, not the log line beside it

            await pilot.press("l")
            await pilot.pause(0.2)
            assert app._timeline_filter == "timeline-logs"
            assert timeline.row_count == 2  # the stage and the output together

            await pilot.press("e")
            await pilot.pause(0.2)
            assert app._timeline_filter == "timeline-events"
            await pilot.press("e")
            await pilot.pause(0.2)
            assert app._timeline_filter == "timeline-all"

    asyncio.run(scenario())


class LastRunClient:
    """Enough client for `load_last_runs`: a function run, and a workflow with steps.

    A standalone function gets its own run; a step never does, so its executions are
    only visible in the step rows of a workflow run. Both paths have to land in the
    same mapping.
    """

    def __init__(self) -> None:
        self.api_url = "https://api.example.com"
        self.step_calls: list[str] = []

    def list_runs(self, **kwargs: Any) -> list[dict[str, Any]]:
        # Newest first, the order the API returns.
        return [
            {
                "id": "wf-run-new",
                "target_type": "workflow",
                "target_id": "workflow-id",
                "created_at": "2026-08-09T18:15:00Z",
                "started_at": "2026-08-09T18:15:05Z",
            },
            {
                "id": "fn-run",
                "target_type": "function",
                "target_id": "standalone-fn",
                "created_at": "2026-08-09T17:00:00Z",
                "started_at": "2026-08-09T17:00:03Z",
            },
            {
                # Ephemeral: no registered target, so it belongs to no row.
                "id": "ephemeral-run",
                "target_type": "function",
                "target_id": None,
                "is_ephemeral": True,
                "created_at": "2026-08-09T19:00:00Z",
                "started_at": "2026-08-09T19:00:01Z",
            },
            {
                "id": "wf-run-old",
                "target_type": "workflow",
                "target_id": "workflow-id",
                "created_at": "2026-08-09T18:00:00Z",
                "started_at": "2026-08-09T18:00:05Z",
            },
        ]

    def list_run_steps(self, run_id: str) -> list[dict[str, Any]]:
        self.step_calls.append(run_id)
        stamp = {"wf-run-new": "2026-08-09T18:15:20Z", "wf-run-old": "2026-08-09T18:00:20Z"}[run_id]
        return [{"function_id": "step-fn", "started_at": stamp}]


def test_last_runs_covers_both_standalone_functions_and_steps() -> None:
    client = LastRunClient()
    data = fake_tui_data(client)

    last_runs = data.load_last_runs("project-id")

    assert last_runs["standalone-fn"] == "2026-08-09T17:00:03Z"
    # Workflows land in the same mapping, keyed the same way.
    assert last_runs["workflow-id"] == "2026-08-09T18:15:05Z"
    # The step's newest execution wins, not whichever run was read last.
    assert last_runs["step-fn"] == "2026-08-09T18:15:20Z"
    # An ephemeral run has no target to attribute, so it adds nothing.
    assert None not in last_runs and len(last_runs) == 3
    # Only workflow runs carry steps, and only the newest few are opened.
    assert sorted(client.step_calls) == ["wf-run-new", "wf-run-old"]


def test_last_runs_bounds_how_many_runs_it_opens() -> None:
    """The scan is capped: a step's history is per-run, so it must not be unbounded."""

    class ManyRuns(LastRunClient):
        def list_runs(self, **kwargs: Any) -> list[dict[str, Any]]:
            return [
                {
                    "id": f"wf-{index}",
                    "target_type": "workflow",
                    "target_id": "workflow-id",
                    "created_at": f"2026-08-09T18:{index:02d}:00Z",
                }
                for index in range(20)
            ]

        def list_run_steps(self, run_id: str) -> list[dict[str, Any]]:
            self.step_calls.append(run_id)
            return []

    client = ManyRuns()
    fake_tui_data(client).load_last_runs("project-id")

    assert len(client.step_calls) == tui_module.LAST_RUN_STEP_SCAN


def test_functions_table_shows_when_each_function_last_ran() -> None:
    async def scenario() -> None:
        client = FakeClient()
        app = RebaseTuiApp(data=fake_tui_data(client, project="energy", limit=5))

        async with app.run_test(size=(200, 42)) as pilot:
            await pilot.pause(0.3)
            projects = app.query_one("#projects-table", SelectableDataTable)
            projects.focus()
            projects.move_cursor(row=0)
            await pilot.press("enter")
            await pilot.pause(0.4)

            functions = app.query_one("#functions-table", SelectableDataTable)
            labels = [str(column.label) for column in functions.columns.values()]
            assert "Last run" in labels
            # Ahead of Version and Updated, so the useful column is the reachable one.
            assert labels.index("Last run") < labels.index("Version")

            workflows = app.query_one("#workflows-table", SelectableDataTable)
            wf_labels = [str(column.label) for column in workflows.columns.values()]
            # Straight after Origin, so they fit a terminal that clips the table's right
            # edge; and next to Next run, the schedule's two ends side by side.
            assert wf_labels[2:5] == ["Schedule", "Next run", "Last run"]
            # History follows Last run: the one run, then the day of runs before it. Its
            # header carries the time axis on a second line, which needs the room.
            history = next(label for label in wf_labels if label.startswith("History\n"))
            assert wf_labels.index(history) == wf_labels.index("Last run") + 1
            assert workflows.header_height == 2

    asyncio.run(scenario())


def test_overview_counts_functions_with_one_workspace_wide_call() -> None:
    """Opening the workspace view must not scale with the number of projects."""

    async def scenario() -> None:
        client = FakeClient()
        app = RebaseTuiApp(data=fake_tui_data(client, limit=5))

        async with app.run_test(size=(140, 42)) as pilot:
            await pilot.pause(0.4)
            assert app.query_one("#projects-table", SelectableDataTable).row_count == 2
            # One unfiltered call, not one per project — `None` is the workspace-wide read.
            assert client.function_calls == [None]
            assert app.workspace_overview is not None
            counts = {s.project["name"]: s.function_count for s in app.workspace_overview.project_summaries}
            assert counts == {"energy": 1, "trading": 0}

    asyncio.run(scenario())


def _ephemeral_run(
    run_id: str,
    name: str,
    *,
    target_type: str = "workflow",
    started: str = "2026-08-09T18:00:00Z",
) -> dict[str, Any]:
    return {
        "id": run_id,
        "target_type": target_type,
        "target_id": None,
        "is_ephemeral": True,
        "ephemeral_target": {"name": name, "project": "pypi-stats", "entrypoint": name},
        "run_type": "quick",
        "status": "succeeded",
        "created_at": started,
        "started_at": started,
    }


def test_ephemeral_runs_group_into_one_row_per_name_they_ran_as() -> None:
    groups = group_ephemeral_runs(
        [
            _ephemeral_run("r1", "collect", started="2026-08-09T18:52:48Z"),
            _ephemeral_run("r2", "collect", started="2026-08-09T18:44:02Z"),
            _ephemeral_run("r3", "backfill", target_type="function", started="2026-08-09T19:10:00Z"),
            # A registered run is not one of these and must not be swept in.
            {"id": "r4", "target_type": "workflow", "target_id": "workflow-id"},
        ]
    )

    # Most recently run first, not alphabetical: the useful question here is what you ran last.
    assert [(g.name, g.target_type, g.runs) for g in groups] == [
        ("backfill", "function", 1),
        ("collect", "workflow", 2),
    ]
    # The newest of the group's runs, not whichever was read last.
    assert groups[1].last_run == "2026-08-09T18:52:48Z"
    assert groups[1].row_key == "ephemeral:workflow:collect"


def test_a_one_off_run_of_a_deployed_target_gets_no_row_of_its_own() -> None:
    """`rebase run …::sync` is the deployed `sync`, not a second workflow of that name.

    Giving it its own row said the project had two `sync` workflows, one carrying the
    schedule and one carrying the last run — the same thing, split across two lines.
    """
    runs = [
        _ephemeral_run("r1", "sync", started="2026-08-11T12:38:14Z"),
        _ephemeral_run("r2", "health", started="2026-08-11T12:37:47Z"),
        _ephemeral_run("r3", "scratch", started="2026-08-11T10:00:00Z"),
    ]
    deployed = deployed_identities([{"name": "sync", "id": "w1"}, {"name": "health", "id": "w2"}])

    assert [g.name for g in group_ephemeral_runs(runs, deployed)] == ["scratch"]
    # Without the deployed names it is the old behaviour, so the filtering is what changed.
    assert {g.name for g in group_ephemeral_runs(runs)} == {"sync", "health", "scratch"}


def test_a_deployed_row_shows_when_it_last_ran_by_hand() -> None:
    """Dropping the duplicate row must not drop the timestamp that row carried."""
    client = FakeClient()
    data = RebaseTuiData(client)
    workflows = [{"name": "sync", "id": "w1"}]

    last_runs = data.load_last_runs(
        "p1",
        runs=[_ephemeral_run("r1", "sync", started="2026-08-11T12:38:14Z")],
        target_ids=target_ids_by_identity(workflows),
    )

    assert last_runs["w1"] == "2026-08-11T12:38:14Z"


def test_a_one_off_run_matching_nothing_deployed_is_left_for_its_own_row() -> None:
    """Only a name that is deployed folds in; the rest still need somewhere to appear."""
    client = FakeClient()
    data = RebaseTuiData(client)

    last_runs = data.load_last_runs(
        "p1",
        runs=[_ephemeral_run("r1", "scratch", started="2026-08-11T12:00:00Z")],
        target_ids=target_ids_by_identity([{"name": "sync", "id": "w1"}]),
    )

    assert last_runs == {}


def test_identity_helpers_keep_the_two_namespaces_apart() -> None:
    """A workflow and a function may share a name without being the same target."""
    workflows = [{"name": "collect", "id": "w1"}]
    functions = [{"name": "collect", "id": "f1"}]

    assert deployed_identities(workflows, functions) == {("workflow", "collect"), ("function", "collect")}
    assert target_ids_by_identity(workflows, functions) == {
        ("workflow", "collect"): "w1",
        ("function", "collect"): "f1",
    }


def test_selecting_a_deployed_target_shows_its_one_off_runs_too() -> None:
    """The runs table has to find the same runs the Last run column counted.

    `/runs` selects on target_id, which a one-off run has not got. A workflow run only
    ever by `rebase run …::sync` therefore matched nothing and showed an empty table
    beside a populated Last run — the same runs, found by name in one place and by id in
    the other.
    """

    class SplitClient(FakeClient):
        def list_runs(self, **kwargs: Any) -> list[dict[str, Any]]:
            super().list_runs(**kwargs)
            # Registered runs are selected by id; this target has none.
            if kwargs.get("workflow_id") is not None:
                return []
            return [
                _ephemeral_run("r1", "sync", started="2026-08-11T13:00:15Z"),
                _ephemeral_run("r2", "other", started="2026-08-11T12:00:00Z"),
            ]

    data = RebaseTuiData(SplitClient())

    assert data.load_target_runs("workflow", "w1") == []
    runs = data.load_target_runs("workflow", "w1", name="sync", project_id="p1")
    assert [run["id"] for run in runs] == ["r1"]


def test_registered_and_one_off_runs_come_back_newest_first() -> None:
    """One target, one history: the two sources interleave by time, not by kind."""

    class BothClient(FakeClient):
        def list_runs(self, **kwargs: Any) -> list[dict[str, Any]]:
            super().list_runs(**kwargs)
            if kwargs.get("workflow_id") is not None:
                return [
                    {"id": "scheduled", "target_id": "w1", "created_at": "2026-08-11T05:30:00Z"},
                ]
            return [_ephemeral_run("manual", "sync", started="2026-08-11T13:00:15Z")]

    data = RebaseTuiData(BothClient())
    runs = data.load_target_runs("workflow", "w1", name="sync", project_id="p1")

    assert [run["id"] for run in runs] == ["manual", "scheduled"]


def test_a_multi_line_log_row_stands_in_for_itself_rather_than_going_blank() -> None:
    """A block of captured output usually starts with a newline. That row was blank.

    One row is one line high, so the collapsed form has to be a line worth reading —
    "nothing was logged" and "press enter for eleven more lines" must not look alike.
    """
    captured = "\n=== Step 2: Fortnox Transaction Matching ===\n\nFound 124 MISSING transactions\n"

    collapsed = collapse_message(captured)

    assert collapsed.startswith("=== Step 2: Fortnox Transaction Matching ===")
    assert "(+1 more)" in collapsed
    # Single-line messages are handed back untouched, counter and all.
    assert collapse_message("Accepted run request.") == "Accepted run request."
    assert collapse_message("") == ""


def test_ephemeral_grouping_skips_a_run_that_declared_no_name() -> None:
    """A row has to be labelled something. Absence is not a name to group under."""
    groups = group_ephemeral_runs(
        [
            {"id": "r1", "target_type": "workflow", "target_id": None, "is_ephemeral": True},
            {**_ephemeral_run("r2", "collect"), "ephemeral_target": {"name": ""}},
        ]
    )

    assert groups == ()


def test_ephemeral_groups_are_split_by_kind_not_just_by_name() -> None:
    """Same name, two kinds: they belong to different tables and must not merge."""
    groups = group_ephemeral_runs(
        [
            _ephemeral_run("r1", "collect", target_type="workflow"),
            _ephemeral_run("r2", "collect", target_type="function"),
        ]
    )

    assert {(g.target_type, g.runs) for g in groups} == {("workflow", 1), ("function", 1)}


class EphemeralClient(FakeClient):
    """A project holding one deployed workflow and two one-off runs of `collect`."""

    def list_runs(self, **kwargs: Any) -> list[dict[str, Any]]:
        super().list_runs(**kwargs)
        if kwargs.get("workflow_id") is not None:
            return self.runs[: kwargs.get("limit", 100)]
        return [
            _ephemeral_run("eph-1", "collect", started="2026-08-09T18:52:48Z"),
            _ephemeral_run("eph-2", "collect", started="2026-08-09T18:44:02Z"),
            # A registered run alongside them, so the Last run column has something to
            # fill with. Its id is what `FakeClient.list_run_steps` expects to be asked for.
            {
                "id": "run-id",
                "target_type": "workflow",
                "target_id": "workflow-id",
                "status": "succeeded",
                "created_at": "2026-08-09T17:00:00Z",
                "started_at": "2026-08-09T17:00:05Z",
            },
        ]


class CompositeProjectClient(EphemeralClient):
    """A platform with the project composite route, serving the same rows in one answer.

    The fan-out methods refuse, so any of them being reached is a test failure rather
    than a slower pass.
    """

    def __init__(self) -> None:
        super().__init__()
        self.overview_calls: list[str] = []

    def get_project_overview(self, project_id: str) -> dict[str, Any]:
        self.overview_calls.append(project_id)
        runs = EphemeralClient.list_runs(self, limit=200)
        return {
            "project": {"id": project_id, "name": "energy"},
            "workflows": self.workflows,
            "functions": self.functions,
            "endpoints": self.endpoints,
            "runs": runs,
            "current_workflow_versions": {
                str(workflow["id"]): FakeClient.get_workflow_version(
                    self, str(workflow["id"]), str(workflow["current_version_id"])
                )
                for workflow in self.workflows
                if workflow.get("current_version_id")
            },
            "step_runs": FakeClient.list_run_steps(self, "run-id"),
        }

    def _refuse(self, *_a: Any, **_k: Any) -> Any:
        raise AssertionError("the fan-out ran even though the composite route answered")

    list_workflows = _refuse
    list_functions = _refuse
    list_endpoints = _refuse
    list_runs = _refuse
    get_workflow_version = _refuse
    list_run_steps = _refuse


def test_project_targets_prefer_the_composite_route() -> None:
    client = CompositeProjectClient()

    targets = fake_tui_data(client).load_project_targets({"id": "project-id", "name": "energy"})

    assert client.overview_calls == ["project-id"]
    assert [workflow["name"] for workflow in targets.workflows] == ["forecast"]
    # The two things that used to cost a request each are filled from the same answer.
    assert targets.workflow_versions
    assert targets.last_runs == {"workflow-id": "2026-08-09T17:00:05Z"}
    assert [group.name for group in targets.ephemeral] == ["collect"]


def _hour(offset: int) -> datetime:
    return datetime.now(UTC).replace(minute=0, second=0, microsecond=0) - timedelta(hours=offset)


class HistoryCompositeClient(CompositeProjectClient):
    """A platform whose overview also carries the run-history aggregate."""

    def get_project_overview(self, project_id: str) -> dict[str, Any]:
        payload = super().get_project_overview(project_id)
        payload["run_history"] = [
            {
                "target_type": "workflow",
                "target_id": "workflow-id",
                "ephemeral_name": None,
                "bucket": _hour(0).isoformat(),
                "status": "succeeded",
                "runs": 60,
            },
            # A one-off run of the deployed name is that workflow, run by hand.
            {
                "target_type": "workflow",
                "target_id": None,
                "ephemeral_name": "forecast",
                "bucket": _hour(1).isoformat(),
                "status": "failed",
                "runs": 1,
            },
            # A one-off of a name nothing is deployed under gets the one-off row's key.
            {
                "target_type": "workflow",
                "target_id": None,
                "ephemeral_name": "collect",
                "bucket": _hour(2).isoformat(),
                "status": "succeeded",
                "runs": 2,
            },
        ]
        return payload


def test_project_history_comes_from_the_platform_aggregate_when_it_is_sent() -> None:
    """The run list is capped; only the platform's count over the window is honest."""
    targets = fake_tui_data(HistoryCompositeClient()).load_project_targets({"id": "project-id", "name": "energy"})

    assert targets.run_history == {
        "workflow-id": {_hour(0): {"succeeded": 60}, _hour(1): {"failed": 1}},
        "ephemeral:workflow:collect": {_hour(2): {"succeeded": 2}},
    }


def test_history_from_runs_counts_each_run_in_its_hour_and_folds_one_offs_by_name() -> None:
    """Without the aggregate the runs read for the table are bucketed the same way."""
    now = datetime(2026, 9, 3, 10, 51, tzinfo=UTC)
    ten = datetime(2026, 9, 3, 10, tzinfo=UTC)

    def deployed(run_id: str, status: str, created: str) -> dict[str, Any]:
        return {"id": run_id, "target_type": "workflow", "target_id": "wf", "status": status, "created_at": created}

    history = history_from_runs(
        [
            deployed("r1", "succeeded", "2026-09-03T10:05:00Z"),
            deployed("r2", "failed", "2026-09-03T10:30:00Z"),
            # 23 hours back is the first hour of the window; 24 is outside it.
            deployed("r3", "succeeded", "2026-09-02T11:59:00Z"),
            deployed("r4", "succeeded", "2026-09-02T10:59:00Z"),
            _ephemeral_run("e1", "forecast", started="2026-09-03T09:10:00Z"),
            _ephemeral_run("e2", "collect", started="2026-09-03T09:20:00Z"),
            {
                "id": "r5",
                "target_type": "workflow",
                "target_id": None,
                "status": "failed",
                "created_at": "2026-09-03T10:00:00Z",
            },
        ],
        {("workflow", "forecast"): "wf"},
        now=now,
    )

    assert history == {
        "wf": {
            ten: {"succeeded": 1, "failed": 1},
            ten - timedelta(hours=1): {"succeeded": 1},
            ten - timedelta(hours=23): {"succeeded": 1},
        },
        "ephemeral:workflow:collect": {ten - timedelta(hours=1): {"succeeded": 1}},
    }
    # The scale is the mean bar height: three single-run hours and one with two.
    assert history_scale(history) == pytest.approx((3 * math.log2(2) + math.log2(3)) / 4)


def test_history_from_buckets_drops_what_it_cannot_attribute() -> None:
    history = history_from_buckets(
        [
            {
                "target_type": "workflow",
                "target_id": None,
                "ephemeral_name": None,
                "bucket": _hour(0).isoformat(),
                "status": "failed",
                "runs": 3,
            },
            {"target_type": "workflow", "target_id": "wf", "bucket": "not a time", "status": "failed", "runs": 3},
            {
                "target_type": "workflow",
                "target_id": "wf",
                "bucket": _hour(0).isoformat(),
                "status": "failed",
                "runs": 0,
            },
        ],
        {},
    )

    assert history == {}


def test_history_text_scales_height_by_count_and_colours_by_worst_status() -> None:
    now = datetime(2026, 9, 3, 10, 51, tzinfo=UTC)
    ten = datetime(2026, 9, 3, 10, tzinfo=UTC)
    text = history_text(
        {
            ten: {"succeeded": 60},
            ten - timedelta(hours=1): {"succeeded": 59, "failed": 1},
            ten - timedelta(hours=2): {"succeeded": 3, "running": 1},
            ten - timedelta(hours=23): {"succeeded": 1},
            ten - timedelta(hours=24): {"succeeded": 100},
        },
        now=now,
        # The four-run hour is the mean, so it draws at half height; sixty is far past
        # twice the mean and fills the cell; one run shrinks towards the floor.
        scale=math.log2(4 + 1),
    )

    assert len(text.plain) == HISTORY_HOURS
    # Oldest on the left, the current hour on the right, and a day-old hour not at all.
    assert text.plain[0] == "▂"
    assert text.plain[1:21] == "·" * 20
    assert text.plain[21:] == "▄██"
    styles = {span.start: str(span.style) for span in text.spans}
    # One failure in sixty is still a red hour; a running run outranks the successes.
    # Odd hours — 09:00 here, and 11:00 yesterday on the far left — take the darker
    # shade of their colour, so a run of same-height bars still reads as bars.
    assert styles[23] == status_style("succeeded")
    assert styles[22] == shaded(status_style("failed"))
    assert styles[21] == status_style("running")
    assert styles[0] == shaded(status_style("succeeded"))
    assert styles[1] == tui_module.BRAND_MEDIUM_GRAY


def test_history_text_is_all_quiet_for_a_row_with_no_runs() -> None:
    text = history_text({}, now=datetime.now(UTC), scale=0)

    assert text.plain == "·" * HISTORY_HOURS


def test_history_text_draws_a_flat_schedule_at_half_height() -> None:
    """A cron landing the same count every hour is a row of half bars, not a block."""
    now = datetime(2026, 9, 3, 10, 51, tzinfo=UTC)
    ten = datetime(2026, 9, 3, 10, tzinfo=UTC)
    hours = {ten - timedelta(hours=offset): {"succeeded": 6} for offset in range(HISTORY_HOURS)}

    text = history_text(hours, now=now, scale=history_scale({"wf": hours}))

    assert text.plain == "▄" * HISTORY_HOURS
    # 10:00 is on the right; the shade alternates hour by hour all the way back.
    styles = [str(span.style) for span in text.spans]
    assert styles[::-1] == [status_style("succeeded"), shaded(status_style("succeeded"))] * (HISTORY_HOURS // 2)


def test_shaded_darkens_a_colour_and_keeps_it_hex() -> None:
    assert shaded("#0D9373", 0.5) == "#064939"
    assert shaded("#ffffff", 0.0) == "#ffffff"


def test_history_column_label_axis_is_as_wide_as_the_bars() -> None:
    name, axis = history_column_label().plain.split("\n")

    assert name == "History"
    assert len(axis) == HISTORY_HOURS
    assert axis.startswith("-24h")
    assert axis.endswith("now")
    assert axis.index("-12h") == HISTORY_HOURS // 2


def test_project_targets_fall_back_without_the_composite_route() -> None:
    targets = fake_tui_data(EphemeralClient()).load_project_targets({"id": "project-id", "name": "energy"})

    assert [workflow["name"] for workflow in targets.workflows] == ["forecast"]
    assert targets.last_runs == {"workflow-id": "2026-08-09T17:00:05Z"}


def test_composite_and_fanout_agree_on_the_project_view() -> None:
    """One request and many must draw the same table, or it changes as platforms roll."""
    project = {"id": "project-id", "name": "energy"}

    composite = fake_tui_data(CompositeProjectClient()).load_project_targets(project)
    fanout = fake_tui_data(EphemeralClient()).load_project_targets(project)

    assert composite.workflows == fanout.workflows
    assert composite.functions == fanout.functions
    assert composite.last_runs == fanout.last_runs
    assert composite.steps == fanout.steps
    assert composite.workflow_versions == fanout.workflow_versions
    assert composite.ephemeral == fanout.ephemeral
    assert composite.run_history == fanout.run_history


def test_ephemeral_runs_filter_to_the_name_they_ran_as() -> None:
    """`/runs` cannot select on this, so the filtering has to happen here."""
    data = fake_tui_data(EphemeralClient())

    assert [run["id"] for run in data.load_ephemeral_runs("collect", "workflow", "project-id")] == [
        "eph-1",
        "eph-2",
    ]
    assert data.load_ephemeral_runs("collect", "function", "project-id") == []
    assert data.load_ephemeral_runs("nope", "workflow", "project-id") == []


def test_tui_one_off_runs_get_a_row_and_open_like_any_target() -> None:
    """The whole point: a run that registered nothing is still reachable from the table."""

    async def scenario() -> None:
        client = EphemeralClient()
        app = RebaseTuiApp(data=fake_tui_data(client, project="energy", limit=5))

        async with app.run_test(size=(140, 42)) as pilot:
            await pilot.pause(0.2)
            projects = app.query_one("#projects-table", SelectableDataTable)
            projects.focus()
            projects.move_cursor(row=0)
            await pilot.press("enter")
            await pilot.pause(0.3)

            workflows = app.query_one("#workflows-table", SelectableDataTable)
            rows = [[str(cell) for cell in workflows.get_row_at(index)] for index in range(workflows.row_count)]
            # The deployed workflow keeps its place; the one-off is appended below it, and
            # the Origin column is what tells them apart — no filter needed to see both.
            assert [(row[0], row[1]) for row in rows] == [
                ("forecast", tui_module.ORIGIN_DEPLOYED),
                ("collect", tui_module.ORIGIN_ONE_OFF),
            ]
            # A one-off has no schedule or next run, no deployed source, commit, state,
            # endpoint or version — only what it ran as and when.
            one_off = rows[1]
            assert one_off[2:4] == ["-", "-"]
            assert len(one_off[5]) == HISTORY_HOURS
            assert one_off[6:8] == ["-", "-"]
            assert one_off[8] == "quick"
            assert one_off[9:13] == ["-", "-", "-", "-"]

            workflows.focus()
            workflows.move_cursor(row=1)
            await pilot.press("enter")
            await pilot.pause(0.4)

            runs = app.query_one("#runs-table", DataTable)
            assert runs.row_count == 2
            assert [str(runs.get_row_at(index)[0]) for index in range(2)] == [
                compact_id("eph-1"),
                compact_id("eph-2"),
            ]

    asyncio.run(scenario())


def test_tui_delete_on_a_one_off_row_says_why_rather_than_nothing_selected() -> None:
    """It registered no target, so there is nothing for `d` to address."""

    async def scenario() -> None:
        client = EphemeralClient()
        app = RebaseTuiApp(data=fake_tui_data(client, project="energy", limit=5))

        async with app.run_test(size=(140, 42)) as pilot:
            await pilot.pause(0.2)
            projects = app.query_one("#projects-table", SelectableDataTable)
            projects.focus()
            projects.move_cursor(row=0)
            await pilot.press("enter")
            await pilot.pause(0.3)

            workflows = app.query_one("#workflows-table", SelectableDataTable)
            workflows.focus()
            workflows.move_cursor(row=1)
            await pilot.press("d")
            await pilot.pause(0.2)

            # No confirm screen, and nothing was asked of the API.
            assert not isinstance(app.screen, DeleteConfirmScreen)
            assert client.deleted == []
            # `run_test` suppresses the toast widget, so read the notifications directly,
            # as the rest of this file does.
            notices = [notification.message for notification in app._notifications]
            assert any("one-off run registers no target" in notice for notice in notices)

    asyncio.run(scenario())


class SlowDetailClient(EphemeralClient):
    """Fast target reads, slow step/last-run reads: the shape progressive paint is for."""

    def __init__(self) -> None:
        super().__init__()
        self.detail_started = threading.Event()
        self.release_detail = threading.Event()

    def _stall(self) -> None:
        self.detail_started.set()
        self.release_detail.wait(timeout=5)

    def get_workflow_version(self, workflow_id: str, version_id: str) -> dict[str, Any]:
        self._stall()
        return super().get_workflow_version(workflow_id, version_id)

    def list_run_steps(self, run_id: str) -> list[dict[str, Any]]:
        self._stall()
        return super().list_run_steps(run_id)


def test_project_base_holds_back_only_what_needs_a_second_round_trip() -> None:
    base, runs = fake_tui_data(EphemeralClient()).load_project_base({"id": "project-id", "name": "energy"})

    # Everything the first paint draws is here already, one-off rows included.
    assert [w["name"] for w in base.workflows] == ["forecast"]
    assert [g.name for g in base.ephemeral] == ["collect"]
    assert len(runs) == 3
    # And the two things that cost another round trip are not.
    assert base.steps == ()
    assert base.last_runs == {}


def test_project_detail_fills_in_what_the_base_left_empty() -> None:
    client = EphemeralClient()
    client.step_graph = {
        "schema_version": 1,
        "engine": "prefect",
        "nodes": [{"node_key": "collect", "name": "collect", "function_id": "function-id", "upstream_node_keys": []}],
    }
    data = fake_tui_data(client)
    base, runs = data.load_project_base({"id": "project-id", "name": "energy"})

    full = data.load_project_detail(base, runs)

    assert [step.node_key for step in full.steps] == ["collect"]
    assert full.last_runs == {"workflow-id": "2026-08-09T17:00:05Z"}
    # The base's own reads are carried through untouched rather than fetched again.
    assert full.workflows == base.workflows
    assert full.ephemeral == base.ephemeral


def test_tui_paints_the_targets_before_the_slower_detail_arrives() -> None:
    async def scenario() -> None:
        client = SlowDetailClient()
        app = RebaseTuiApp(data=fake_tui_data(client, project="energy", limit=5))

        async with app.run_test(size=(140, 42)) as pilot:
            await pilot.pause(0.2)
            projects = app.query_one("#projects-table", SelectableDataTable)
            projects.focus()
            projects.move_cursor(row=0)
            await pilot.press("enter")

            # Wait for the detail phase to be in flight, then look: the names are already
            # drawn even though the second round trip has not come back.
            for _ in range(50):
                await pilot.pause(0.05)
                if client.detail_started.is_set():
                    break
            assert client.detail_started.is_set()
            workflows = app.query_one("#workflows-table", SelectableDataTable)
            assert [str(workflows.get_row_at(i)[0]) for i in range(workflows.row_count)] == [
                "forecast",
                "collect",
            ]
            # The Last run column is what it is still waiting on.
            assert str(workflows.get_row_at(0)[4]) == "-"

            client.release_detail.set()
            for _ in range(50):
                await pilot.pause(0.05)
                if str(workflows.get_row_at(0)[4]) != "-":
                    break
            assert str(workflows.get_row_at(0)[4]) != "-"

    asyncio.run(scenario())


def test_tui_refresh_keeps_the_complete_frame_until_slow_detail_arrives() -> None:
    """A timer tick must not flash provenance and other second-phase cells back to dashes."""

    async def scenario() -> None:
        client = SlowDetailClient()
        app = RebaseTuiApp(data=fake_tui_data(client, project="energy", limit=5), refresh_interval=0)

        async with app.run_test(size=(140, 42)) as pilot:
            await pilot.pause(0.2)
            projects = app.query_one("#projects-table", SelectableDataTable)
            projects.focus()
            projects.move_cursor(row=0)
            await pilot.press("enter")

            # Let the initial progressive load finish so a complete frame is on screen.
            for _ in range(50):
                await pilot.pause(0.05)
                if client.detail_started.is_set():
                    break
            client.release_detail.set()
            workflows = app.query_one("#workflows-table", SelectableDataTable)
            for _ in range(50):
                await pilot.pause(0.05)
                if str(workflows.get_cell_at(Coordinate(0, 6))) == "GitHub":
                    break
            before = [str(cell) for cell in workflows.get_row_at(0)]
            assert before[6:8] == ["GitHub", "01234567..."]
            assert before[4] != "-"

            # Hold the detail phase of a refresh open. The old implementation painted
            # `base` here, making Source, Commit and Last run disappear every tick.
            client.detail_started.clear()
            client.release_detail.clear()
            app.action_refresh()
            for _ in range(50):
                await pilot.pause(0.05)
                if client.detail_started.is_set():
                    break
            assert client.detail_started.is_set()
            assert [str(cell) for cell in workflows.get_row_at(0)] == before

            client.release_detail.set()
            await pilot.pause(0.3)

    asyncio.run(scenario())


def test_tui_second_paint_keeps_the_row_you_already_opened() -> None:
    """The detail arrives unasked; it must not close what the reader opened meanwhile."""

    async def scenario() -> None:
        client = SlowDetailClient()
        app = RebaseTuiApp(data=fake_tui_data(client, project="energy", limit=5))

        async with app.run_test(size=(140, 42)) as pilot:
            await pilot.pause(0.2)
            projects = app.query_one("#projects-table", SelectableDataTable)
            projects.focus()
            projects.move_cursor(row=0)
            await pilot.press("enter")
            for _ in range(50):
                await pilot.pause(0.05)
                if client.detail_started.is_set():
                    break

            # Open the one-off row while the detail phase is still out.
            workflows = app.query_one("#workflows-table", SelectableDataTable)
            workflows.focus()
            workflows.move_cursor(row=1)
            await pilot.press("enter")
            await pilot.pause(0.3)
            runs = app.query_one("#runs-table", DataTable)
            assert runs.row_count == 2

            client.release_detail.set()
            for _ in range(50):
                await pilot.pause(0.05)
                if str(workflows.get_row_at(0)[4]) != "-":
                    break

            # Still on the same row, and its runs are still on screen.
            assert workflows.cursor_key == "ephemeral:workflow:collect"
            assert app.query_one("#runs-table", DataTable).row_count == 2

    asyncio.run(scenario())


async def _open_project(pilot, app) -> None:
    """Drill into the fake client's first project and wait for both paints."""
    await pilot.pause(0.3)
    projects = app.query_one("#projects-table", SelectableDataTable)
    projects.focus()
    projects.move_cursor(row=0)
    await pilot.press("enter")
    await pilot.pause(0.4)


def test_auto_refresh_keeps_the_cursor_and_the_marks() -> None:
    """A repaint the reader did not ask for must not take their place away."""

    async def scenario() -> None:
        app = RebaseTuiApp(data=fake_tui_data(FakeClient(), limit=5), refresh_interval=0)

        async with app.run_test(size=(200, 42)) as pilot:
            await pilot.pause(0.3)
            projects = app.query_one("#projects-table", SelectableDataTable)
            projects.focus()
            projects.move_cursor(row=1)
            await pilot.press("shift+down")
            await pilot.pause(0.1)
            marked_before = list(projects.marked_keys)
            cursor_before = projects.cursor_key
            assert marked_before and cursor_before is not None

            app._last_key_at = 0.0
            app._refresh_tick()
            await pilot.pause(0.5)

            assert projects.cursor_key == cursor_before
            assert projects.marked_keys == marked_before

    asyncio.run(scenario())


def test_auto_refresh_stands_down_while_the_reader_is_busy() -> None:
    """Each pause is a case where repainting would take something away."""

    async def scenario() -> None:
        client = FakeClient()
        app = RebaseTuiApp(data=fake_tui_data(client, limit=5), refresh_interval=0)

        async with app.run_test(size=(200, 42)) as pilot:
            await pilot.pause(0.3)
            app._last_key_at = 0.0
            assert app._refresh_tick_paused() is False

            # Mid-keystroke: the cursor is still being driven.
            app.note_interaction()
            assert app._refresh_tick_paused() is True
            app._last_key_at = 0.0

            # A dialog whose row list is what the reader is about to act on.
            app.push_screen(DeleteConfirmScreen(kind="project", names=["energy"]))
            await pilot.pause(0.2)
            assert app._refresh_tick_paused() is True
            app.pop_screen()
            await pilot.pause(0.2)
            app._last_key_at = 0.0

            # The terminal owns the mouse; a repaint erases its native selection.
            app._terminal_select = True
            assert app._refresh_tick_paused() is True
            app._terminal_select = False

            # A half-made copy.
            app.screen.selections = {app.query_one("#projects-table"): Selection(Offset(0, 0), Offset(4, 0))}
            assert app._refresh_tick_paused() is True

    asyncio.run(scenario())


def test_auto_refresh_leaves_a_finished_run_alone() -> None:
    """A finished run cannot change, and its timeline is where reading happens."""

    async def scenario() -> None:
        client = FakeClient()
        app = RebaseTuiApp(data=fake_tui_data(client, project="energy", limit=5), refresh_interval=0)

        async with app.run_test(size=(200, 42)) as pilot:
            await _open_project(pilot, app)
            workflows = app.query_one("#workflows-table", SelectableDataTable)
            workflows.focus()
            workflows.move_cursor(row=0)
            await pilot.press("enter")
            await pilot.pause(0.4)
            runs = app.query_one("#runs-table", DataTable)
            runs.focus()
            runs.move_cursor(row=0)
            await pilot.press("enter")
            await pilot.pause(0.4)

            assert app._reveal_level == 2
            # The fake's run is "succeeded", so there is nothing left to poll.
            assert app._live_run_id is None

            app._last_key_at = 0.0
            before = len(client.run_calls)
            app._refresh_tick()
            await pilot.pause(0.6)

            # The runs box is re-read — a new run can appear — but the timeline is not.
            assert len(client.run_calls) > before
            assert client.run_detail_calls == 1

    asyncio.run(scenario())


def test_a_failing_tick_is_quiet_until_it_stops_looking_like_a_blip() -> None:
    """One dropped request on a timer is not worth yanking the view for."""

    async def scenario() -> None:
        app = RebaseTuiApp(data=fake_tui_data(FakeClient(), limit=5), refresh_interval=0)

        async with app.run_test(size=(200, 42)) as pilot:
            await pilot.pause(0.3)
            shown: list[str] = []
            app._set_project_error = lambda message: shown.append(message)  # type: ignore[method-assign]

            for _ in range(AUTO_REFRESH_FAILURE_LIMIT - 1):
                app._set_error(RebaseWorkflowError("boom"), announce=False)
            assert shown == []

            app._set_error(RebaseWorkflowError("boom"), announce=False)
            assert len(shown) == 1
            # The counter resets, so a later blip is a blip again rather than the last straw.
            assert app._refresh_failures == 0

            # A refresh the reader asked for says so immediately.
            app._set_error(RebaseWorkflowError("boom"))
            assert len(shown) == 2

    asyncio.run(scenario())


def test_refresh_interval_zero_arms_no_timer() -> None:
    async def scenario() -> None:
        app = RebaseTuiApp(data=fake_tui_data(FakeClient(), limit=5), refresh_interval=0)

        async with app.run_test(size=(140, 42)) as pilot:
            await pilot.pause(0.3)
            assert app._refresh_interval == 0
            assert not any("_refresh_tick" in str(timer) for timer in app._timers)

    asyncio.run(scenario())


def test_the_timer_actually_fires() -> None:
    """Every other test drives the tick by hand; this one checks it is wired at all."""

    async def scenario() -> None:
        client = FakeClient()
        app = RebaseTuiApp(data=fake_tui_data(client, limit=5), refresh_interval=0.2)

        async with app.run_test(size=(140, 42)) as pilot:
            await pilot.pause(0.3)
            app._last_key_at = 0.0
            before = len(client.function_calls)
            # Long enough for several intervals; `exclusive=True` collapses any overlap.
            await pilot.pause(1.0)

            assert len(client.function_calls) > before

    asyncio.run(scenario())


def test_clicking_the_header_of_an_empty_table_does_not_crash() -> None:
    """Textual indexes `ordered_columns` on a header click without checking it is filled.

    Columns arrive with the rows, so every table here spends time with none while still
    drawing a header row. Clicking that band used to raise IndexError out of Textual and
    take the whole app down — reported from the timeline pane, reachable in all of them.
    """

    async def scenario() -> None:
        client = FakeClient()
        app = RebaseTuiApp(data=fake_tui_data(client, project="energy", limit=5), refresh_interval=0)

        async with app.run_test(size=(200, 42)) as pilot:
            await _open_project(pilot, app)
            workflows = app.query_one("#workflows-table", SelectableDataTable)
            workflows.focus()
            workflows.move_cursor(row=0)
            await pilot.press("enter")
            await pilot.pause(0.4)
            runs = app.query_one("#runs-table", DataTable)
            runs.focus()
            runs.move_cursor(row=0)
            await pilot.press("enter")
            await pilot.pause(0.4)

            timeline = app.query_one("#timeline-table", DataTable)
            assert timeline.row_count  # it filled, so the click below lands on a real widget
            timeline.clear(columns=True)
            assert list(timeline.columns) == []

            # Row 0 of a table is its header. Before the guard this raised IndexError.
            await pilot.click("#timeline-table", offset=(5, 0))
            await pilot.pause(0.2)

            # Still alive, and the header click changed nothing.
            assert app.query_one("#timeline-table", DataTable).row_count == 0

    asyncio.run(scenario())


def test_a_tick_leaves_the_timeline_you_are_reading_on_screen() -> None:
    """The runs box clears the timeline when a *different* target is opened, not on refresh.

    Reported as: open a run's logs, wait ten seconds, the screen goes empty until an arrow
    key redraws it from memory. The tick re-read the runs box, which wiped the timeline,
    and a finished run is deliberately not re-read — so nothing put it back.
    """

    async def scenario() -> None:
        client = FakeClient()
        app = RebaseTuiApp(data=fake_tui_data(client, project="energy", limit=5), refresh_interval=0)

        async with app.run_test(size=(200, 42)) as pilot:
            await _open_project(pilot, app)
            workflows = app.query_one("#workflows-table", SelectableDataTable)
            workflows.focus()
            workflows.move_cursor(row=0)
            await pilot.press("enter")
            await pilot.pause(0.4)
            runs = app.query_one("#runs-table", DataTable)
            runs.focus()
            runs.move_cursor(row=0)
            await pilot.press("enter")
            await pilot.pause(0.4)

            timeline = app.query_one("#timeline-table", DataTable)
            rows_before = timeline.row_count
            assert rows_before and app._reveal_level == 2
            assert app._live_run_id is None  # finished, so the tick will not re-read it

            app._last_key_at = 0.0
            app._refresh_tick()
            await pilot.pause(0.8)

            assert timeline.row_count == rows_before
            assert list(timeline.columns)

            # Opening a different target still clears it: that timeline is another run's.
            workflows.focus()
            await pilot.press("enter")
            await pilot.pause(0.4)
            assert app.query_one("#timeline-table", DataTable).row_count == 0

    asyncio.run(scenario())


# ---- the graph pane -------------------------------------------------------------


class FakePlotui:
    """What the pane asks of plotui, recorded: every plot, widget and repaint.

    Installed into `sys.modules` in place of the real package so the tests neither need
    the native wheel nor a terminal that can show images.
    """

    def __init__(self, render_mode: str = "placeholder") -> None:
        self.render_mode = render_mode
        self.plots: list[Any] = []
        self.widgets: list[Any] = []
        fake = self

        class Plot:
            def __init__(self) -> None:
                self.graphs: list[dict[str, Any]] = []
                self.colours: list[tuple[int, list[str], list[str] | None]] = []
                self.selected: Any = "untouched"
                fake.plots.append(self)

            def add_graph2d(self, xs: Any, ys: Any, edges: Any, **kwargs: Any) -> int:
                self.graphs.append({"xs": list(xs), "ys": list(ys), "edges": list(edges), **kwargs})
                return len(self.graphs) - 1

            def set_graph_colors(self, handle: int, node_colors: Any, edge_colors: Any = None) -> None:
                self.colours.append((handle, list(node_colors), None if edge_colors is None else list(edge_colors)))

            def set_selected(self, element: Any) -> None:
                self.selected = element

        class LayeredLayout:
            def __init__(self, n_nodes: int, edges: Any, rankdir: str = "TB") -> None:
                self.n_nodes = n_nodes
                self.edges = list(edges)
                self.rankdir = rankdir

            def positions(self) -> tuple[list[float], list[float]]:
                return [float(i) for i in range(self.n_nodes)], [0.0] * self.n_nodes

            def routes(self) -> list[list[tuple[float, float]]]:
                return [[] for _ in self.edges]

        class PlotWidget(Static):
            class ElementHovered(Message):
                def __init__(self, plot_widget: Any, element: tuple[str, int] | None) -> None:
                    super().__init__()
                    self.plot_widget = plot_widget
                    self.element = element

            class ElementPicked(Message):
                def __init__(self, plot_widget: Any, element: tuple[str, int] | None) -> None:
                    super().__init__()
                    self.plot_widget = plot_widget
                    self.element = element

            def __init__(self, plot: Any, *, pickable: bool = False, crosshair: bool = True, **kwargs: Any) -> None:
                super().__init__("fake plot", **kwargs)
                self.plot = plot
                self.pickable = pickable
                self.crosshair = crosshair
                self.invalidations = 0
                fake.widgets.append(self)

            def set_graph_colors(self, handle: int, node_colors: Any, edge_colors: Any = None) -> None:
                self.plot.set_graph_colors(handle, node_colors, edge_colors)

            def invalidate(self) -> None:
                self.invalidations += 1

        self.core = types.ModuleType("plotui")
        self.core.Plot = Plot  # type: ignore[attr-defined]
        self.core.LayeredLayout = LayeredLayout  # type: ignore[attr-defined]
        self.textual = types.ModuleType("plotui.textual")
        self.textual.PlotWidget = PlotWidget  # type: ignore[attr-defined]
        self.textual.detect_render_mode = lambda env=None: fake.render_mode  # type: ignore[attr-defined]

    def install(self, monkeypatch: pytest.MonkeyPatch) -> FakePlotui:
        monkeypatch.setitem(sys.modules, "plotui", self.core)
        monkeypatch.setitem(sys.modules, "plotui.textual", self.textual)
        return self

    @property
    def plot(self) -> Any:
        return self.plots[-1]

    @property
    def widget(self) -> Any:
        return self.widgets[-1]


def _graph_pane(app: RebaseTuiApp) -> GraphPane:
    return app.query_one(GraphPane)


def test_tui_i_toggles_the_graph_pane_and_back_closes_it_first(monkeypatch) -> None:
    """`i` opens the pane beside the tables; `b` and `escape` close it before going back."""
    monkeypatch.setitem(sys.modules, "plotui", None)

    async def scenario() -> None:
        for key in ("b", "escape"):
            app = RebaseTuiApp(data=fake_tui_data(FakeClient(), project="energy", limit=5))

            async with app.run_test(size=(140, 42)) as pilot:
                await _open_run(app, pilot)
                revealed = app._reveal_level
                pane = _graph_pane(app)
                assert not pane.display

                await pilot.press("i")
                await pilot.pause(0.2)
                assert pane.display
                # The tables keep the focus: the pane is looked at, not driven.
                assert isinstance(app.focused, DataTable)

                await pilot.press(key)
                await pilot.pause(0.2)
                assert not pane.display
                assert app._reveal_level == revealed

                await pilot.press(key)
                await pilot.pause(0.2)
                assert app._reveal_level == revealed - 1

                # Pressing it twice puts it away again.
                await pilot.press("i")
                await pilot.press("i")
                await pilot.pause(0.2)
                assert not pane.display

    asyncio.run(scenario())


def test_tui_graph_pane_wants_a_project_first(monkeypatch) -> None:
    monkeypatch.setitem(sys.modules, "plotui", None)

    async def scenario() -> None:
        app = RebaseTuiApp(data=fake_tui_data(FakeClient(), project="energy", limit=5))

        async with app.run_test(size=(140, 42)) as pilot:
            await pilot.pause(0.2)
            assert app.current_view == "workspace"
            await pilot.press("i")
            await pilot.pause(0.2)
            assert not _graph_pane(app).display
            assert any("Open a project first" in note.message for note in app._notifications)

    asyncio.run(scenario())


def test_tui_graph_pane_shows_a_notice_without_plotui(monkeypatch) -> None:
    monkeypatch.setitem(sys.modules, "plotui", None)

    async def scenario() -> None:
        app = RebaseTuiApp(data=fake_tui_data(SteppedClient(), project="energy", limit=5))

        async with app.run_test(size=(140, 42)) as pilot:
            await _open_run(app, pilot)
            await pilot.press("i")
            await pilot.pause(0.2)
            notice = _graph_pane(app).notice
            assert notice is not None
            assert 'pip install "rebase-toolkit[graph]"' in notice
            # As drawn, too: a bare string would be read as markup and lose `[graph]`.
            assert 'pip install "rebase-toolkit[graph]"' in str(app.query_one("#graph-notice", Static).render())

    asyncio.run(scenario())


def test_tui_graph_pane_shows_a_notice_in_an_unsupported_terminal(monkeypatch) -> None:
    fake = FakePlotui(render_mode="unsupported").install(monkeypatch)

    async def scenario() -> None:
        app = RebaseTuiApp(data=fake_tui_data(SteppedClient(), project="energy", limit=5))

        async with app.run_test(size=(140, 42)) as pilot:
            await _open_run(app, pilot)
            await pilot.press("i")
            await pilot.pause(0.2)
            notice = _graph_pane(app).notice
            assert notice is not None
            assert "Kitty, Ghostty" in notice
            assert fake.plots == []

    asyncio.run(scenario())


def test_tui_graph_pane_draws_the_open_run_and_repaints_on_refresh(monkeypatch) -> None:
    """The open run's steps, coloured by how far it got; `r` repaints rather than relays."""
    fake = FakePlotui().install(monkeypatch)

    async def scenario() -> None:
        app = RebaseTuiApp(data=fake_tui_data(SteppedClient(), project="energy", limit=5))

        async with app.run_test(size=(140, 42)) as pilot:
            await _open_run(app, pilot)
            await pilot.press("i")
            await pilot.pause(0.3)
            pane = _graph_pane(app)
            assert pane.notice is None
            graph = fake.plot.graphs[0]
            assert graph["labels"] == ["load_weather", "normalize"]
            assert graph["edges"] == [(0, 1)]
            assert graph["node_colors"] == [BRAND_MAIN_GREEN, BRAND_MEDIUM_GRAY]
            assert graph["node_shapes"] == ["rounded", "rounded"]
            assert (
                str(app.query_one("#graph-title", Static).render()) == f"forecast · {compact_id('run-id')} · succeeded"
            )
            assert fake.widget.pickable
            assert not fake.widget.crosshair
            assert not fake.widget.can_focus
            assert pane.rebuilds == 1

            await pilot.press("r")
            await pilot.pause(0.4)
            assert pane.rebuilds == 1
            assert pane.recolours >= 1
            assert fake.plot.colours[-1][1] == [BRAND_MAIN_GREEN, BRAND_MEDIUM_GRAY]

    asyncio.run(scenario())


def test_tui_graph_pane_lights_what_a_picked_step_waits_on(monkeypatch) -> None:
    fake = FakePlotui().install(monkeypatch)

    async def scenario() -> None:
        app = RebaseTuiApp(data=fake_tui_data(SteppedClient(), project="energy", limit=5))

        async with app.run_test(size=(140, 42)) as pilot:
            await _open_run(app, pilot)
            await pilot.press("i")
            await pilot.pause(0.3)
            widget = fake.widget
            readout = app.query_one("#graph-readout", Static)

            widget.post_message(widget.ElementPicked(widget, ("node", 1)))
            await pilot.pause(0.2)
            assert str(readout.render()) == "normalize waits on 1: load_weather"
            assert fake.plot.colours[-1][1] == [BRAND_MAIN_GREEN, BRAND_MEDIUM_GRAY]

            widget.post_message(widget.ElementHovered(widget, ("node", 0)))
            await pilot.pause(0.2)
            assert str(readout.render()) == "load_weather waits on nothing"
            assert fake.plot.colours[-1][1] == [BRAND_MAIN_GREEN, dim(BRAND_MEDIUM_GRAY)]

            widget.post_message(widget.ElementHovered(widget, ("edge", 0)))
            await pilot.pause(0.2)
            assert str(readout.render()) == "load_weather → normalize (ordering only)"
            assert fake.plot.colours[-1][1] == [BRAND_MAIN_GREEN, BRAND_MEDIUM_GRAY]

    asyncio.run(scenario())


class TwoRunClient(SteppedClient):
    """Two runs of the stepped workflow, one still going, so the cursor has somewhere to move."""

    def __init__(self) -> None:
        super().__init__()
        self.step_calls: list[str] = []
        self.runs = [
            self.runs[0],
            {
                **self.runs[0],
                "id": "run-2",
                "status": "running",
                "finished_at": None,
                "created_at": "2026-06-16T15:00:00Z",
                "started_at": "2026-06-16T15:00:10Z",
            },
        ]

    def get_run(self, run_id: str) -> dict[str, Any]:
        self.run_detail_calls += 1
        return next(run for run in self.runs if run["id"] == run_id)

    def list_run_steps(self, run_id: str) -> list[dict[str, Any]]:
        self.step_calls.append(run_id)
        if run_id == "run-2":
            return [
                {"id": "step-2a", "name": "load_weather", "status": "succeeded", "attempt": 1},
                {"id": "step-2b", "name": "normalize", "status": "running", "attempt": 1},
            ]
        return super().list_run_steps(run_id)


def test_tui_graph_pane_follows_the_cursor_through_the_runs(monkeypatch) -> None:
    """A run under the cursor is coloured from its own steps, fetched once while it is settled."""
    fake = FakePlotui().install(monkeypatch)
    client = TwoRunClient()

    async def scenario() -> None:
        app = RebaseTuiApp(data=fake_tui_data(client, project="energy", limit=5))

        async with app.run_test(size=(140, 42)) as pilot:
            await pilot.pause(0.2)
            projects = app.query_one("#projects-table", SelectableDataTable)
            projects.focus()
            projects.move_cursor(row=0)
            await pilot.press("enter")
            await pilot.pause(0.3)
            await pilot.press("enter")
            await pilot.pause(0.3)
            runs = app.query_one("#runs-table", DataTable)
            assert app.focused is runs
            assert app._run_detail is None

            await pilot.press("i")
            await pilot.pause(0.4)
            pane = _graph_pane(app)
            assert fake.plot.graphs[0]["labels"] == ["load_weather", "normalize"]
            assert client.step_calls == ["run-id"]
            assert fake.plot.colours[-1][1] == [BRAND_MAIN_GREEN, BRAND_MEDIUM_GRAY]

            await pilot.press("down")
            await pilot.pause(0.4)
            assert client.step_calls == ["run-id", "run-2"]
            assert fake.plot.colours[-1][1] == [BRAND_MAIN_GREEN, BRAND_BRIGHT_GREEN]
            assert pane.rebuilds == 1

            # Back up: the finished run's steps are remembered, not re-read.
            await pilot.press("up")
            await pilot.pause(0.4)
            assert client.step_calls == ["run-id", "run-2"]
            assert fake.plot.colours[-1][1] == [BRAND_MAIN_GREEN, BRAND_MEDIUM_GRAY]

            # A refresh forgets the run still in flight, and only that one.
            await pilot.press("r")
            await pilot.pause(0.4)
            assert "run-id" in app._step_runs
            assert "run-2" not in app._step_runs

    asyncio.run(scenario())


class TriggeredClient(FakeClient):
    """A second workflow that runs after the first, which is the edge the trigger graph draws."""

    def __init__(self) -> None:
        super().__init__()
        self.workflows = [
            self.workflows[0],
            {
                **self.workflows[0],
                "id": "publish-id",
                "name": "publish",
                "schedule": None,
                "next_run_at": None,
                "paused": True,
                "trigger": {"type": "on_workflow", "source": "forecast", "on": "success", "active": True},
            },
        ]


def test_tui_graph_pane_draws_a_workflow_as_deployed_from_the_workflows_table(monkeypatch) -> None:
    """The cursor on a stepped workflow draws its steps before any run is opened."""
    fake = FakePlotui().install(monkeypatch)

    async def scenario() -> None:
        app = RebaseTuiApp(data=fake_tui_data(SteppedClient(), project="energy", limit=5))

        async with app.run_test(size=(140, 42)) as pilot:
            await pilot.pause(0.2)
            projects = app.query_one("#projects-table", SelectableDataTable)
            projects.focus()
            projects.move_cursor(row=0)
            await pilot.press("enter")
            await pilot.pause(0.3)
            assert app.focused is app.query_one("#workflows-table", DataTable)

            await pilot.press("i")
            await pilot.pause(0.3)
            graph = fake.plot.graphs[0]
            assert graph["labels"] == ["load_weather", "normalize"]
            assert graph["node_colors"] == [BRAND_MEDIUM_GRAY, BRAND_MEDIUM_GRAY]
            assert str(app.query_one("#graph-title", Static).render()) == "forecast · steps"
            assert "Open one of its runs" in str(app.query_one("#graph-readout", Static).render())
            assert "open a run to colour it" in str(app.query_one("#graph-legend", Static).render())

            # Opening a run of it colours the same graph in place.
            await pilot.press("enter")
            await pilot.pause(0.3)
            await pilot.press("enter")
            await pilot.pause(0.4)
            assert _graph_pane(app).rebuilds == 1
            assert fake.plot.colours[-1][1] == [BRAND_MAIN_GREEN, BRAND_MEDIUM_GRAY]

    asyncio.run(scenario())


def test_tui_graph_pane_draws_the_triggers_from_the_workflows_table(monkeypatch) -> None:
    fake = FakePlotui().install(monkeypatch)

    async def scenario() -> None:
        app = RebaseTuiApp(data=fake_tui_data(TriggeredClient(), project="energy", limit=5))

        async with app.run_test(size=(140, 42)) as pilot:
            await pilot.pause(0.2)
            projects = app.query_one("#projects-table", SelectableDataTable)
            projects.focus()
            projects.move_cursor(row=0)
            await pilot.press("enter")
            await pilot.pause(0.3)
            workflows = app.query_one("#workflows-table", DataTable)
            assert app.focused is workflows

            await pilot.press("i")
            await pilot.pause(0.3)
            graph = fake.plot.graphs[0]
            assert graph["labels"] == ["forecast", "publish"]
            assert graph["edges"] == [(0, 1)]
            assert graph["node_shapes"] == ["box", "box"]
            assert graph["node_colors"] == [BRAND_MAIN_GREEN, BRAND_AMBER]
            assert "name" not in graph
            assert fake.plot.selected == ("node", 0)

            # The cursor's workflow is the one picked out, and moving it is a repaint.
            await pilot.press("down")
            await pilot.pause(0.3)
            assert fake.plot.selected == ("node", 1)
            assert _graph_pane(app).rebuilds == 1

            # Opening the workflow moves on to its runs, which have no step graph here.
            await pilot.press("enter")
            await pilot.pause(0.4)
            notice = _graph_pane(app).notice
            assert notice is not None
            assert "no step graph" in notice

    asyncio.run(scenario())


def test_tui_graph_pane_says_why_a_trigger_graph_has_no_edges(monkeypatch) -> None:
    fake = FakePlotui().install(monkeypatch)

    async def scenario() -> None:
        app = RebaseTuiApp(data=fake_tui_data(FakeClient(), project="energy", limit=5))

        async with app.run_test(size=(140, 42)) as pilot:
            await pilot.pause(0.2)
            projects = app.query_one("#projects-table", SelectableDataTable)
            projects.focus()
            projects.move_cursor(row=0)
            await pilot.press("enter")
            await pilot.pause(0.3)
            await pilot.press("i")
            await pilot.pause(0.3)
            assert fake.plot.graphs[0]["labels"] == ["forecast"]
            assert fake.plot.graphs[0]["edges"] == []
            readout = str(app.query_one("#graph-readout", Static).render())
            assert "no steps" in readout and "rb.OnWorkflow" in readout

    asyncio.run(scenario())


# --- Run scans against a project whose runs carry large results -------------------------
#
# `/runs` ships every run's full `result`. One project's runs were ~260 KiB each, so 200 of
# them made a ~50 MiB answer that the platform's front end refused with a bare 500, while
# the same request against a quiet project came back fine. The TUI must shrink what it
# asks for rather than error out, and must not keep paying for the failing request on
# every refresh tick — those repeated heavy reads were what made *unrelated* requests
# start failing too.


def _server_error(status: int = 500) -> RebaseWorkflowError:
    error = RebaseWorkflowError(f"HTTP {status} with an empty response body")
    error.status_code = status
    return error


class OversizedRunsClient(EphemeralClient):
    """A project whose run list only fits in an answer when at most `fits` rows are asked for."""

    fits = 50

    def list_runs(self, **kwargs: Any) -> list[dict[str, Any]]:
        rows = super().list_runs(**kwargs)
        if kwargs.get("limit", 100) > self.fits:
            raise _server_error()
        return rows

    def scan_limits(self) -> list[int]:
        return [call["limit"] for call in self.run_calls if call["project_id"] is not None]


def test_project_run_scan_halves_on_a_server_error_and_remembers_what_fit() -> None:
    client = OversizedRunsClient()
    data = fake_tui_data(client)

    base, runs = data.load_project_base({"id": "project-id", "name": "energy"})

    # Halved until the answer fit, and the rows that fit are the rows shown.
    assert client.scan_limits() == [200, 100, 50]
    assert len(runs) == 3
    assert [group.name for group in base.ephemeral] == ["collect"]

    # The next read of the same project starts where the last one succeeded: a refresh
    # every ten seconds must not re-pay two failing heavy reads each tick.
    data.load_project_base({"id": "project-id", "name": "energy"})
    assert client.scan_limits() == [200, 100, 50, 50]

    # And the one-off runs view scans through the same remembered cap.
    data.load_ephemeral_runs("collect", "workflow", "project-id")
    assert client.scan_limits() == [200, 100, 50, 50, 50]


def test_project_run_scan_gives_up_below_the_floor_without_taking_the_view_down() -> None:
    client = OversizedRunsClient()
    client.fits = 0
    data = fake_tui_data(client)

    base, runs = data.load_project_base({"id": "project-id", "name": "energy"})

    assert runs == []
    assert base.ephemeral == ()
    assert [workflow["name"] for workflow in base.workflows] == ["forecast"]
    assert client.scan_limits()[-1] == RUN_SCAN_FLOOR
    assert min(client.scan_limits()) == RUN_SCAN_FLOOR


def test_project_run_scan_does_not_shrink_on_a_client_error() -> None:
    """A 4xx is not a size problem; halving would only hide it. The scan stays optional."""

    class ForbiddenRunsClient(EphemeralClient):
        def list_runs(self, **kwargs: Any) -> list[dict[str, Any]]:
            super().list_runs(**kwargs)
            raise _server_error(403)

    client = ForbiddenRunsClient()
    _, runs = fake_tui_data(client).load_project_base({"id": "project-id", "name": "energy"})

    assert runs == []
    assert [call["limit"] for call in client.run_calls] == [200]


def test_target_runs_halve_on_a_server_error() -> None:
    class OversizedTargetRunsClient(FakeClient):
        def list_runs(self, **kwargs: Any) -> list[dict[str, Any]]:
            super().list_runs(**kwargs)
            if kwargs.get("limit", 100) > 25:
                raise _server_error()
            return self.runs[: kwargs["limit"]]

    client = OversizedTargetRunsClient()
    data = fake_tui_data(client, limit=100)

    runs = data.load_target_runs("workflow", "workflow-id")

    assert [call["limit"] for call in client.run_calls] == [100, 50, 25]
    assert runs == client.runs[:25]


def test_target_runs_still_raise_when_no_size_fits() -> None:
    class AlwaysFailingClient(FakeClient):
        def list_runs(self, **kwargs: Any) -> list[dict[str, Any]]:
            super().list_runs(**kwargs)
            raise _server_error()

    with pytest.raises(RebaseWorkflowError, match="HTTP 500"):
        fake_tui_data(AlwaysFailingClient(), limit=100).load_target_runs("workflow", "workflow-id")


def test_project_composite_server_error_falls_back_to_the_fan_out() -> None:
    """The composite answer carries the same 200 runs, so it hits the same wall.

    A route that is *present but failing* must not be treated like one that is absent
    (404: try again next time) nor like a fatal error (the old behaviour, an error pane
    over an empty table). Fall back to the piecewise reads, whose run scan can shrink.
    """

    class FailingCompositeClient(OversizedRunsClient):
        def __init__(self) -> None:
            super().__init__()
            self.overview_calls: list[str] = []

        def get_project_overview(self, project_id: str) -> dict[str, Any]:
            self.overview_calls.append(project_id)
            raise _server_error()

    client = FailingCompositeClient()
    data = fake_tui_data(client)

    targets = data.load_project_targets({"id": "project-id", "name": "energy"})

    assert client.overview_calls == ["project-id"]
    assert [workflow["name"] for workflow in targets.workflows] == ["forecast"]
    assert [group.name for group in targets.ephemeral] == ["collect"]
    assert targets.last_runs == {"workflow-id": "2026-08-09T17:00:05Z"}

    # The composite read is remembered as failing for this project. On the next tick the
    # view goes straight to the fan-out rather than paying for the oversized answer again.
    data.load_project_targets({"id": "project-id", "name": "energy"})
    assert client.overview_calls == ["project-id"]
    # Another project is not tarred with the same brush.
    assert data.load_project_composite({"id": "other-project-id", "name": "trading"}) is None
    assert client.overview_calls == ["project-id", "other-project-id"]
