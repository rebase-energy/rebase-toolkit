from __future__ import annotations

import asyncio
import json
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, cast
from urllib.parse import parse_qs, urlparse

import pytest
from textual.coordinate import Coordinate
from textual.widgets import DataTable, Header, Static, TabbedContent

from rebase.brand import BRAND_MEDIUM_GRAY
from rebase.client import Client, RebaseWorkflowError
from rebase.tui import (
    RebaseTuiApp,
    RebaseTuiData,
    compact_id,
    endpoints_by_target,
    format_endpoint,
    format_json_summary,
    format_timestamp,
    status_style,
)


class FakeClient:
    def __init__(self) -> None:
        self.api_url = "https://api.example.com"
        self.run_calls: list[dict[str, Any]] = []
        self.function_calls: list[str | None] = []
        self.workflow_calls: list[str | None] = []
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

    def list_functions(self, *, project: str | None = None, project_id: str | None = None) -> list[dict[str, Any]]:
        assert project is None
        self.function_calls.append(project_id)
        return [item for item in self.functions if project_id is None or item["project_id"] == project_id]

    def list_workflows(self, *, project: str | None = None, project_id: str | None = None) -> list[dict[str, Any]]:
        assert project is None
        self.workflow_calls.append(project_id)
        return [item for item in self.workflows if project_id is None or item["project_id"] == project_id]

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


def test_tui_overview_reads_all_workflows_in_one_call() -> None:
    """Per-project workflow requests were the bulk of the TUI's startup wait."""
    client = FakeClient()
    client.projects.append({"id": "third-project-id", "name": "storage"})

    overview = fake_tui_data(client).load_workspace_overview()

    assert client.workflow_calls == [None]
    assert sorted(call or "" for call in client.function_calls) == [
        "other-project-id",
        "project-id",
        "third-project-id",
    ]
    assert [(summary.function_count, summary.workflow_count) for summary in overview.project_summaries] == [
        (1, 1),
        (0, 0),
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
            elif parsed.path == "/projects/project-id/endpoints":
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
                assert app.query_one("#workspace-view").styles.display == "block"
                assert app.query_one("#project-view").styles.display == "none"
                projects.focus()
                projects.move_cursor(row=0)
                await pilot.press("enter")
                await pilot.pause(0.2)

                summary = app.query_one("#summary", Static)
                assert f"API: {api_url}" in str(summary.content)
                assert "Projects: 1" in str(summary.content)
                assert "Functions: 1" in str(summary.content)
                assert "Workflows: 1" in str(summary.content)
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
