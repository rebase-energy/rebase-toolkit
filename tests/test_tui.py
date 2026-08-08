from __future__ import annotations

import asyncio
import json
import re
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, cast
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo

import pytest
from textual.coordinate import Coordinate
from textual.events import MouseMove
from textual.geometry import Offset
from textual.selection import Selection
from textual.widgets import DataTable, Header, Input, OptionList, Static, TabbedContent

from rebase import config as config_module
from rebase.brand import BRAND_MEDIUM_GRAY
from rebase.client import Client, RebaseWorkflowError
from rebase.editor import EditorCommand
from rebase.tui import (
    MARK_STYLE,
    DeleteConfirmScreen,
    OpenSourceChoiceScreen,
    RebaseClock,
    RebaseTuiApp,
    RebaseTuiData,
    SelectableDataTable,
    TimezoneChoiceScreen,
    compact_id,
    endpoints_by_target,
    format_endpoint,
    format_json_summary,
    format_timestamp,
    status_style,
)


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
        self.asgi_apps = [
            {
                "id": "asgi-app-id",
                "project_id": "project-id",
                "name": "grid-api",
                "base_path": "/api",
                "auth": "api_key",
                "enabled": True,
                "current_version_id": "asgi-version-id-123456",
                "url_path": "/e/energy-workspace/energy/api",
                "updated_at": "2026-06-16T13:45:00Z",
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
                "created_at": "2026-06-16T14:00:00Z",
                "finished_at": "2026-06-16T14:01:00Z",
            }
        ]

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

    def list_asgi_apps(self, *, project: str | None = None, project_id: str | None = None) -> list[dict[str, Any]]:
        assert project is None
        return [item for item in self.asgi_apps if project_id is None or item["project_id"] == project_id]

    def list_runs(
        self,
        *,
        project_id: str | None = None,
        workflow_id: str | None = None,
        function_id: str | None = None,
        model_id: str | None = None,
        target_type: str | None = None,
        limit: int = 100,
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

    def get_run(self, run_id: str) -> dict[str, Any]:
        assert run_id == "run-id"
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


def fake_tui_data(client: Any, **kwargs: Any) -> RebaseTuiData:
    """FakeClient duck-types Client; the cast keeps the type checker honest at that seam."""
    return RebaseTuiData(cast(Client, client), **kwargs)


def test_tui_format_helpers() -> None:
    assert compact_id("123456789abcdef") == "12345678..."
    assert format_timestamp("2026-06-16T12:00:00Z") == "2026-06-16 12:00:00"
    assert format_json_summary({"b": 2, "a": 1}) == '{"a": 1, "b": 2}'
    assert status_style("failed") == "#E46962"


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


def test_tui_data_loads_endpoints_and_asgi_apps() -> None:
    data = fake_tui_data(FakeClient(), project="energy")
    overview = data.load_workspace_overview()

    targets = data.load_project_targets(overview.project_summaries[0].project)
    assert [item["name"] for item in targets.endpoints] == ["forecast"]
    assert [item["name"] for item in targets.asgi_apps] == ["grid-api"]

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


def test_tui_data_tolerates_missing_endpoint_and_asgi_routes() -> None:
    class Unsupported(FakeClient):
        def list_project_endpoints(self, project_id: str) -> list[dict[str, Any]]:
            raise RebaseWorkflowError("404 Not Found")

        def list_asgi_apps(self, *, project: str | None = None, project_id: str | None = None) -> list[dict[str, Any]]:
            raise RebaseWorkflowError("404 Not Found")

    data = fake_tui_data(Unsupported(), project="energy")
    overview = data.load_workspace_overview()

    targets = data.load_project_targets(overview.project_summaries[0].project)
    assert targets.endpoints == []
    assert targets.asgi_apps == []
    assert [item["name"] for item in targets.workflows] == ["forecast"]


def test_tui_overview_reads_all_workflows_and_endpoints_in_one_call_each() -> None:
    """Per-project requests for these were the bulk of the TUI's startup wait."""
    client = FakeClient()
    client.projects.append({"id": "third-project-id", "name": "storage"})

    overview = fake_tui_data(client).load_workspace_overview()

    assert client.workflow_calls == [None]
    assert client.endpoint_calls == [None]
    assert sorted(call or "" for call in client.function_calls) == [
        "other-project-id",
        "project-id",
        "third-project-id",
    ]
    assert [
        (summary.function_count, summary.workflow_count, summary.endpoint_count, summary.cron_count)
        for summary in overview.project_summaries
    ] == [(1, 1, 1, 1), (0, 0, 0, 0), (0, 0, 0, 0)]


def test_tui_counts_only_workflows_the_api_says_will_fire_as_cron_jobs() -> None:
    """A schedule that cannot fire is not a cron job, and the API is the judge of that."""
    client = FakeClient()
    client.workflows.append(
        {
            "id": "paused-workflow-id",
            "project_id": "project-id",
            "name": "paused",
            "enabled": True,
            # A schedule the API refused to give a next_run_at: paused, disabled,
            # or an unusable cron expression. Either way it is not a cron job.
            "schedule": {"type": "cron", "cron": "0 6 * * *", "active": False},
            "next_run_at": None,
        }
    )
    client.workflows.append({"id": "ad-hoc-workflow-id", "project_id": "project-id", "name": "ad-hoc", "enabled": True})

    overview = fake_tui_data(client).load_workspace_overview()

    energy = overview.project_summaries[0]
    assert energy.workflow_count == 3
    assert energy.cron_count == 1


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
            assert projects.styles.scrollbar_size_horizontal == 0
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
            for table in (functions, workflows, app.query_one("#runs-table", DataTable)):
                assert table.styles.scrollbar_size_horizontal == 0
                assert table.styles.scrollbar_background.hex == "#101412"
            assert workflows.styles.scrollbar_color.hex == "#03C497"
            assert functions.styles.scrollbar_color.hex != "#03C497"
            assert app.query_one("#runs-table", DataTable).styles.scrollbar_color.hex != "#03C497"

            workflows.focus()
            workflows.move_cursor(row=0)
            await pilot.press("enter")
            await pilot.pause(0.2)

            runs = app.query_one("#runs-table", DataTable)
            assert runs.row_count == 1
            assert client.run_calls[-1]["workflow_id"] == "workflow-id"

            runs.focus()
            runs.move_cursor(row=0)
            await pilot.press("enter")
            await pilot.pause(0.2)

            run_detail = app.query_one("#run-detail", Static)
            assert "Run run-id" in str(run_detail.content)
            assert app.query_one("#events-table", DataTable).row_count == 1
            assert app.query_one("#steps-table", DataTable).row_count == 1

            await pilot.press("b")
            await pilot.pause(0.2)
            assert app.query_one("#workspace-view").styles.display == "block"
            assert app.query_one("#project-view").styles.display == "none"
            assert projects.row_count == 2

    asyncio.run(scenario())


def test_tui_app_shows_endpoint_column_and_asgi_apps_tab() -> None:
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
            assert str(workflows.get_cell_at(Coordinate(0, 3))) == "POST /forecast"
            assert str(functions.get_cell_at(Coordinate(0, 3))) == "-"

            # Selecting the workflow surfaces its endpoint and full URL in the detail panel.
            workflows.focus()
            workflows.move_cursor(row=0)
            await pilot.press("enter")
            await pilot.pause(0.2)
            target_detail = str(app.query_one("#target-detail", Static).content)
            assert "Endpoint: POST /forecast | auth: workspace | mode: async | enabled" in target_detail
            assert "URL: https://api.example.com/e/energy-workspace/energy/forecast" in target_detail

            # tab cycles workflows -> functions -> asgi apps.
            tabs = app.query_one("#target-tabs", TabbedContent)
            assert tabs.active == "workflows-tab"
            await pilot.press("tab")
            await pilot.pause(0.1)
            assert tabs.active == "functions-tab"
            await pilot.press("tab")
            await pilot.pause(0.1)
            assert tabs.active == "asgi-apps-tab"

            asgi_apps = app.query_one("#asgi-apps-table", DataTable)
            assert asgi_apps.row_count == 1
            asgi_apps.focus()
            asgi_apps.move_cursor(row=0)
            await pilot.press("enter")
            await pilot.pause(0.2)

            asgi_detail = str(app.query_one("#target-detail", Static).content)
            assert "ASGI app grid-api" in asgi_detail
            # ASGI apps only carry url_path, so the TUI joins it onto the client's api_url.
            assert "URL: https://api.example.com/e/energy-workspace/energy/api" in asgi_detail
            assert app.query_one("#runs-table", DataTable).row_count == 0
            assert "no runs" in str(app.query_one("#run-detail", Static).content)

    asyncio.run(scenario())


@contextmanager
def local_rebase_api() -> Iterator[tuple[str, list[tuple[str, dict[str, list[str]], str | None]]]]:
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
    asgi_apps = [
        {
            "id": "asgi-app-id",
            "project_id": "project-id",
            "name": "grid-api",
            "base_path": "/api",
            "auth": "api_key",
            "enabled": True,
            "current_version_id": "asgi-version-id",
            "url_path": "/e/energy-workspace/energy/api",
            "updated_at": "2026-06-16T13:45:00Z",
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
    events = [
        {
            "id": "event-id",
            "stage": "dispatch",
            "status": "completed",
            "message": "Accepted run request.",
            "created_at": "2026-06-16T14:00:01Z",
        }
    ]
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
            elif parsed.path == "/projects/project-id/asgi-apps":
                payload = asgi_apps
            elif parsed.path == "/runs":
                payload = [run] if query.get("workflow_id") == ["workflow-id"] else []
            elif parsed.path == "/runs/run-id":
                payload = run
            elif parsed.path == "/runs/run-id/events":
                payload = events
            elif parsed.path == "/runs/run-id/steps":
                payload = steps
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
                # Project, Workflows, Cron jobs, Functions, Endpoints.
                assert [str(cell) for cell in projects.get_row_at(0)] == ["energy", "1", "1", "1", "1"]
                assert app.query_one("#workspace-view").styles.display == "block"
                assert app.query_one("#project-view").styles.display == "none"
                projects.focus()
                projects.move_cursor(row=0)
                await pilot.press("enter")
                await pilot.pause(0.2)

                detail = app.query_one("#project-detail", Static)
                assert "Project energy" in str(detail.content)
                assert "Functions: 1 | Workflows: 1 | Cron jobs: 1 | Endpoints: 1" in str(detail.content)
                assert app.query_one("#workspace-view").styles.display == "none"
                assert app.query_one("#project-view").styles.display == "block"

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

                assert "Run run-id" in str(app.query_one("#run-detail", Static).content)
                assert app.query_one("#events-table", DataTable).row_count == 1
                assert app.query_one("#steps-table", DataTable).row_count == 1

                await pilot.press("b")
                await pilot.pause(0.2)
                assert app.query_one("#workspace-view").styles.display == "block"
                assert app.query_one("#project-view").styles.display == "none"

            assert ("/projects", {}, "Bearer rbw_test") in seen_requests
            assert any(path == "/projects/project-id/functions" for path, _, _ in seen_requests)
            assert any(path == "/projects/project-id/workflows" for path, _, _ in seen_requests)
            assert not any(path == "/functions" for path, _, _ in seen_requests)
            # The overview counts workflows workspace-wide; only an opened project is fetched per project.
            assert any(path == "/workflows" for path, _, _ in seen_requests)
            assert any(
                path == "/runs"
                and query.get("workflow_id") == ["workflow-id"]
                and query.get("target_type") == ["workflow"]
                and query.get("limit") == ["5"]
                for path, query, _ in seen_requests
            )
            assert any(path == "/runs/run-id/events" for path, _, _ in seen_requests)
            assert any(path == "/runs/run-id/steps" for path, _, _ in seen_requests)

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

                assert app.title == "Rebase TUI - Workspace: Production"
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
                assert app.title == "Rebase TUI - Workspace: Development"
                assert app.query_one("#workspace-view").styles.display == "block"
                assert app.query_one("#projects-table", DataTable).row_count == 1

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


def test_tui_delete_follows_the_active_target_tab_and_skips_asgi_apps() -> None:
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

            # ASGI apps have no delete endpoint, so `d` must not offer one.
            app.query_one("#target-tabs", TabbedContent).active = "asgi-apps-tab"
            await pilot.pause(0.1)
            await pilot.press("d")
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
        app = RebaseTuiApp(data=fake_tui_data(FakeClient(), project="energy", limit=5))

        async with app.run_test(size=(140, 42)) as pilot:
            await pilot.pause(0.2)
            projects = app.query_one("#projects-table", SelectableDataTable)
            projects.focus()
            projects.move_cursor(row=0)
            await pilot.press("enter")
            await pilot.pause(0.3)

            # Drag a selection across the first line of the project detail panel. Pilot has
            # no drag helper, so the mid-drag move goes through the same private hook its
            # click helpers use.
            detail = app.query_one("#project-detail", Static)
            await pilot.mouse_down(detail, offset=(0, 0))
            await pilot._post_mouse_events([MouseMove], widget=detail, offset=(7, 0), button=1)
            await pilot.mouse_up(detail, offset=(7, 0))
            await pilot.pause(0.1)

            dragged = app.screen.get_selected_text()
            assert dragged is not None
            assert "Project"[: len(dragged)] == dragged

            await pilot.press("shift+right")
            await pilot.pause(0.1)
            grown = app.screen.get_selected_text()
            assert grown is not None and grown == "Project energy"[: len(dragged) + 1]

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
        app = RebaseTuiApp(data=fake_tui_data(FakeClient(), project="energy", limit=5))

        async with app.run_test(size=(140, 42)) as pilot:
            await pilot.pause(0.2)
            projects = app.query_one("#projects-table", SelectableDataTable)
            projects.focus()
            projects.move_cursor(row=0)
            await pilot.press("enter")
            await pilot.pause(0.3)

            detail = app.query_one("#project-detail", Static)
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

            detail = app.query_one("#project-detail", Static)
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
                "Workflows",
                "Cron jobs",
                "Functions",
                "Endpoints",
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
